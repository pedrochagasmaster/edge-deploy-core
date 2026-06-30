"""Thin CLI surface: ``python -m edge_deploy {release|publish|rollout|drift|preflight}``.

Resolves ``--tool`` / ``--node`` against the two config layers (OperatorConfig + the
tool's ToolProfile), establishes an Authenticated Pane, and calls the engine. The umbrella
``release`` orchestrator (Publish + fan-out + getpass auth seam) and the standalone
``publish`` command are wired here on top of the Phase-1 ``rollout`` / ``drift`` engine.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy import drift, preflight, rollout
from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, OperatorConfig, load_tool_profile
from edge_deploy.publish import PublishError, publish_snapshot
from edge_deploy.release import ReleaseSelection, resolve_nodes, resolve_tools, run_release
from edge_deploy.reporting import OperationReport, redact, write_release_report, write_report
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver

TOOL_CHOICES = ("autobench", "robocop")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edge_deploy",
        description="Roll a reviewed Snapshot of a Tool out to an Edge Node, and verify it.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_OPERATOR_CONFIG_PATH),
        help="Operator config path (default: ~/.edge-deploy/config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    release_parser = subparsers.add_parser(
        "release", help="Publish + roll out a Tool (or both) to one or more Edge Nodes, then verify"
    )
    release_parser.add_argument("--tool", required=True, choices=("autobench", "robocop", "both"))
    release_parser.add_argument("--nodes", default=None, help="Comma list (e.g. 03,04); default: all configured nodes")
    release_parser.add_argument("--snapshot", default=None, help="Skip Publish; roll out this existing Snapshot SHA")
    release_parser.add_argument("--smoke", choices=("standard", "deep"), default="standard")
    release_parser.add_argument("--fail-fast", action="store_true", help="Stop on the first non-success (ADR-0003)")
    release_parser.add_argument("--report-dir", default=None, help="Default: ./edge-deploy/reports/release-<UTC>/")
    release_parser.add_argument("--max-auth-attempts", type=int, default=3)

    publish_parser = subparsers.add_parser("publish", help="Publish one Tool's Snapshot to its Bitbucket remote")
    publish_parser.add_argument("--tool", required=True, choices=TOOL_CHOICES, help="Per-tool; no 'both'")
    publish_parser.add_argument("--commit", default=None, help="Optional source override (a reviewed commit SHA)")
    publish_parser.add_argument("--no-local-check", action="store_true", help="Bypass the local_check.ps1 gate")
    publish_parser.add_argument("--remote", default="bitbucket")

    rollout_parser = subparsers.add_parser("rollout", help="Roll one Edge Node to an exact Snapshot")
    rollout_parser.add_argument("--tool", required=True, help="Tool name (key in operator config 'tools')")
    rollout_parser.add_argument("--node", required=True, help="Node name (key in operator config 'nodes')")
    rollout_parser.add_argument("--commit", required=True, help="Snapshot SHA to roll out")
    rollout_parser.add_argument("--install", choices=["auto", "always", "never"], default="auto")
    rollout_parser.add_argument("--json-report", help="Optional path to write the JSON report")
    rollout_parser.add_argument("--reuse-session", action="store_true", help="Require a pre-authenticated pane")

    drift_parser = subparsers.add_parser("drift", help="Compare runtime-critical files against a commit")
    drift_parser.add_argument("--tool", required=True)
    drift_parser.add_argument("--node", required=True)
    drift_parser.add_argument("--commit", required=True)
    drift_parser.add_argument("--json-report")
    drift_parser.add_argument("--reuse-session", action="store_true")

    preflight_parser = subparsers.add_parser("preflight", help="Check DNS/TCP reachability for a node")
    preflight_parser.add_argument("--node", required=True)
    preflight_parser.add_argument("--tool", default=None, help="Optional: include the tool repo_path in the report")
    preflight_parser.add_argument("--timeout", type=float, default=None, help="TCP connect timeout (seconds)")
    preflight_parser.add_argument("--json-report")
    return parser


def _ensure_session(driver: TmuxDriver, reuse_session: bool) -> None:
    if driver.session_exists():
        return
    if reuse_session:
        raise RuntimeError(
            f"--reuse-session was set but no live tmux session {driver.session!r} exists. "
            "Authenticate one first by starting the pane and entering the PASSCODE."
        )
    ready = driver.start_session(passcode=None)
    if not ready:
        raise RuntimeError(
            "Started a tmux session but it still needs human authentication. "
            "Enter the PASSCODE in that pane, then rerun with --reuse-session."
        )


def _emit(report: OperationReport, json_report: str | None) -> None:
    for check in report.checks:
        outcome = "PASS" if check.passed else "FAIL"
        print(redact(f"[{outcome}] {check.name}: {check.message}"))
    print(f"status: {report.status}")
    if report.sensitive_changed:
        print(redact(f"sensitive_changed: {', '.join(report.sensitive_changed)}"))
    if json_report:
        report_path = write_report(json_report, report)
        print(f"JSON report: {report_path}")


def _default_report_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("edge-deploy") / "reports" / f"release-{stamp}"


def _print_release_summary(report, consolidated_path: Path) -> None:
    summary = report.summary()
    counts = summary["counts"]
    print(f"Release: {summary['overall']}")
    print("counts: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    for handoff in summary["handoffs"]:
        node = handoff.get("node") or "-"
        print(redact(
            f"[handoff:{handoff['kind']}] {handoff.get('tool', '')}/{node}: "
            f"{handoff['message']} -> {handoff['action']}"
        ))
    print(f"Consolidated report: {consolidated_path}")


def _cmd_release(args: argparse.Namespace, operator: OperatorConfig) -> int:
    tools = resolve_tools(args.tool)
    for tool in tools:
        operator.tool_path(tool)  # validate each selected tool is configured (raises KeyError)
    selection = ReleaseSelection(
        tools=tools,
        nodes=resolve_nodes(operator, args.nodes),
        snapshot=args.snapshot,
        smoke=args.smoke,
        fail_fast=args.fail_fast,
    )
    report_dir = Path(args.report_dir) if args.report_dir else _default_report_dir()
    report = run_release(operator, selection, report_dir=report_dir, max_auth_attempts=args.max_auth_attempts)
    consolidated_path = write_release_report(report_dir / "release.json", report)
    _print_release_summary(report, consolidated_path)
    return report.exit_code()


def _cmd_publish(args: argparse.Namespace, operator: OperatorConfig) -> int:
    tool_path = operator.tool_path(args.tool)
    profile = load_tool_profile(Path(tool_path))
    result = publish_snapshot(
        profile,
        repo_root=tool_path,
        remote=args.remote,
        commit=args.commit,
        run_local_check=not args.no_local_check,
    )
    print(f"Published Snapshot: {result.snapshot}")
    print(f"  source: {result.source_short} ({result.source_commit})")
    print(f"  branch: {result.branch}")
    print(f"  previous remote: {result.previous_remote_commit}")
    print(redact(f"  message: {result.message}"))
    return 0


def _cmd_rollout(args: argparse.Namespace, operator: OperatorConfig) -> int:
    node = operator.node(args.node)
    profile = load_tool_profile(Path(operator.tool_path(args.tool)))
    driver = TmuxDriver.from_node_and_profile(node, profile, retries=2)
    _ensure_session(driver, args.reuse_session)
    report = rollout.run_rollout(
        driver,
        profile,
        node,
        target_commit=args.commit,
        install_mode=args.install,
        operator_email=operator.operator_email,
    )
    _emit(report, args.json_report)
    return 0 if report.status == "rolled_out" else 1


def _cmd_drift(args: argparse.Namespace, operator: OperatorConfig) -> int:
    node = operator.node(args.node)
    tool_path = operator.tool_path(args.tool)
    profile = load_tool_profile(Path(tool_path))
    driver = TmuxDriver.from_node_and_profile(node, profile, retries=2)
    _ensure_session(driver, args.reuse_session)
    report = drift.check_drift(driver, profile, node, commit=args.commit, local_root=tool_path)
    _emit(report, args.json_report)
    return 0 if report.status == "passed" else 1


def _cmd_preflight(args: argparse.Namespace, operator: OperatorConfig) -> int:
    node = operator.node(args.node)
    repo_path = ""
    if args.tool:
        repo_path = load_tool_profile(Path(operator.tool_path(args.tool))).repo_path
    result = preflight.run_tcp_preflight(node, timeout=args.timeout)
    addresses = ", ".join(result.resolved_addresses) if result.resolved_addresses else "(none)"
    print(f"Host: {result.endpoint.user_host}")
    print(f"Endpoint: {result.endpoint.hostname}:{result.endpoint.port}")
    print(f"Resolved addresses: {addresses}")
    report = preflight.build_preflight_report(node, result, repo_path=repo_path)
    if args.json_report:
        report_path = write_report(args.json_report, report)
        print(f"JSON report: {report_path}")
    if result.connected:
        print("TCP preflight: PASS")
        return 0
    print(f"TCP preflight: FAIL - {result.error}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        operator = OperatorConfig.load(args.config)
    except FileNotFoundError:
        print(f"Operator config not found: {args.config}", file=sys.stderr)
        return 2

    try:
        if args.command == "release":
            return _cmd_release(args, operator)
        if args.command == "publish":
            return _cmd_publish(args, operator)
        if args.command == "rollout":
            return _cmd_rollout(args, operator)
        if args.command == "drift":
            return _cmd_drift(args, operator)
        if args.command == "preflight":
            return _cmd_preflight(args, operator)
    except (RuntimeError, PublishError, AuthenticationError, SessionGoneError, KeyError) as exc:
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
