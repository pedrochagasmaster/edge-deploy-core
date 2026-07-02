"""Thin CLI surface: ``python -m edge_deploy {release|publish|rollout|drift|preflight}``.

Resolves ``--tool`` / ``--node`` against the two config layers (OperatorConfig + the
tool's ToolProfile), establishes an Authenticated Pane, and calls the engine. The umbrella
``release`` orchestrator (Publish + fan-out + getpass auth seam) and the standalone
``publish`` command are wired here on top of the Phase-1 ``rollout`` / ``drift`` engine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy import __version__, drift, preflight, rollout
from edge_deploy.audit import AuditAttempt, AuditSyncError, append_audit_attempt, check_audit_remote
from edge_deploy.auth import authenticate_node, authenticate_node_via_pane
from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, OperatorConfig, load_tool_profile
from edge_deploy.mirror import MirrorError, mirror_release
from edge_deploy.publish import PublishError, publish_snapshot
from edge_deploy.release import ReleaseSelection, resolve_nodes, run_release
from edge_deploy.reporting import OperationReport, redact, write_release_report, write_report
from edge_deploy.repository import RepositoryError, inspect_repository, require_successful_github_ci
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver

TOOL_CHOICES = ("autobench", "robocop")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edge_deploy",
        description="Publish and deploy an exact reviewed Tool commit, then verify it.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_OPERATOR_CONFIG_PATH),
        help="Operator config path (default: APPDATA/edge-deploy/config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    release_parser = subparsers.add_parser(
        "release", help="Publish and deploy the tool in the current checkout"
    )
    release_parser.add_argument("--tool", choices=TOOL_CHOICES, help=argparse.SUPPRESS)
    release_parser.add_argument("--nodes", default=None, help="Comma list (e.g. 03,04); default: all configured nodes")
    release_parser.add_argument(
        "--resume",
        default=None,
        help="Resume an incomplete attempt from its existing report directory",
    )
    release_parser.add_argument("--smoke", choices=("standard", "deep"), default="standard")
    release_parser.add_argument("--fail-fast", action="store_true", help="Stop on the first non-success (ADR-0003)")
    release_parser.add_argument("--report-dir", default=None, help="Default: ./edge-deploy/reports/release-<UTC>/")
    release_parser.add_argument("--max-auth-attempts", type=int, default=3)
    release_parser.add_argument("--auth-mode", choices=("auto", "prompt", "pane"), default="auto")
    release_parser.add_argument("--auth-wait-seconds", type=float, default=300.0)
    release_parser.add_argument("--heartbeat-interval", type=float, default=30.0)
    release_parser.add_argument("--stall-threshold", type=float, default=300.0)
    release_parser.add_argument("--no-local-check", action="store_true", help="Bypass the local_check.ps1 publish gate")

    rollback_parser = subparsers.add_parser(
        "rollback", help="Restore a previously successful immutable release tag"
    )
    rollback_parser.add_argument("--tag", required=True)
    rollback_parser.add_argument("--nodes", default=None)
    rollback_parser.add_argument("--smoke", choices=("standard", "deep"), default="standard")
    rollback_parser.add_argument("--report-dir", default=None)
    rollback_parser.add_argument("--max-auth-attempts", type=int, default=3)
    rollback_parser.add_argument("--auth-mode", choices=("auto", "prompt", "pane"), default="auto")
    rollback_parser.add_argument("--auth-wait-seconds", type=float, default=300.0)
    rollback_parser.add_argument("--heartbeat-interval", type=float, default=30.0)
    rollback_parser.add_argument("--stall-threshold", type=float, default=300.0)

    publish_parser = subparsers.add_parser("publish", help="Publish one exact Tool commit to Bitbucket")
    publish_parser.add_argument("--tool", required=True, choices=TOOL_CHOICES, help="Per-tool; no 'both'")
    publish_parser.add_argument("--commit", default=None, help="Optional source override (a reviewed commit SHA)")
    publish_parser.add_argument("--no-local-check", action="store_true", help="Bypass the local_check.ps1 gate")
    publish_parser.add_argument("--remote", default="bitbucket")

    mirror_parser = subparsers.add_parser(
        "mirror", help="Mirror a reviewed core release tag from GitHub to Bitbucket"
    )
    mirror_parser.add_argument("--tag", required=True, help="Immutable release tag (e.g. v1.1.0)")
    mirror_parser.add_argument("--remote", default="bitbucket")
    mirror_parser.add_argument("--branch", default="main")
    mirror_parser.add_argument("--repo-root", default=".", help="Core checkout (default: cwd)")

    rollout_parser = subparsers.add_parser("rollout", help="Roll one Edge Node to an exact commit")
    rollout_parser.add_argument("--tool", required=True, help="Tool name (key in operator config 'tools')")
    rollout_parser.add_argument("--node", required=True, help="Node name (key in operator config 'nodes')")
    rollout_parser.add_argument("--commit", required=True, help="Commit SHA to roll out")
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


def _load_resume_snapshots(report_dir: Path, tools: list[str]) -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for tool in tools:
        path = report_dir / f"publish-{tool}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = payload.get("deployment_commit") or payload.get("snapshot")
        if payload.get("status") != "published" or not snapshot:
            raise ValueError(f"Cannot resume {tool}: {path} is not a successful publish report")
        snapshots[tool] = str(snapshot)
    return snapshots


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
    repo_root = Path.cwd().resolve()
    if args.tool:
        repo_root = Path(operator.tool_path(args.tool)).resolve()
    profile = load_tool_profile(repo_root)
    if args.tool and args.tool != profile.tool:
        raise ValueError(f"--tool {args.tool!r} does not match checkout profile {profile.tool!r}")
    tools = [profile.tool]
    effective_operator = replace(operator, tools={profile.tool: str(repo_root)})
    if args.resume:
        report_dir = Path(args.resume)
    elif args.report_dir:
        report_dir = Path(args.report_dir)
    else:
        report_dir = _default_report_dir()
    snapshot_by_tool: dict[str, str] = {}
    if args.resume:
        snapshot_by_tool.update(_load_resume_snapshots(report_dir, tools))
    selection = ReleaseSelection(
        tools=tools,
        nodes=resolve_nodes(effective_operator, args.nodes),
        snapshot=None,
        snapshot_by_tool=snapshot_by_tool,
        smoke=args.smoke,
        fail_fast=args.fail_fast,
        run_local_check=not args.no_local_check,
    )
    state = _run_release_preflight(
        effective_operator,
        profile,
        repo_root,
        selection.nodes,
        auth_mode=args.auth_mode,
        max_auth_attempts=args.max_auth_attempts,
        auth_wait_seconds=args.auth_wait_seconds,
    )
    report = run_release(
        effective_operator,
        selection,
        report_dir=report_dir,
        max_auth_attempts=args.max_auth_attempts,
        auth_mode="pane",
        auth_wait_seconds=args.auth_wait_seconds,
        heartbeat_interval_s=args.heartbeat_interval,
        stall_threshold_s=args.stall_threshold,
        progress_fn=lambda message: print(redact(f"[release] {message}")),
    )
    consolidated_path = write_release_report(report_dir / "release.json", report)
    _record_release_attempt(
        effective_operator,
        profile.tool,
        state.commit,
        report_dir,
        report.summary()["overall"],
    )
    if report.exit_code() == 0:
        _tag_successful_release(
            repo_root,
            state.commit,
            deployment_commit=_deployment_commit_for_tool(report, profile.tool, snapshot_by_tool),
        )
    _print_release_summary(report, consolidated_path)
    return report.exit_code()


def _deployment_commit_for_tool(report, tool: str, snapshot_by_tool: dict[str, str]) -> str | None:
    """The commit actually pushed to the tool's Bitbucket main for this release, if known."""
    if snapshot_by_tool.get(tool):
        return snapshot_by_tool[tool]
    for item in report.publishes:
        if item.get("tool") == tool and item.get("snapshot"):
            return str(item["snapshot"])
    return None


def _remote_tag_sha(repo_root: Path, remote: str, tag: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "ls-remote",
            "--tags",
            remote,
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    rows = [line.split() for line in completed.stdout.splitlines() if line.strip()]
    dereferenced = [sha for sha, ref in rows if ref.endswith("^{}")]
    values = dereferenced or [sha for sha, _ref in rows]
    if not values:
        raise RepositoryError(f"release tag {tag!r} does not exist on {remote}")
    return values[-1]


def _tree_sha(repo_root: Path, commitish: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{commitish}^{{tree}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _resolve_release_tag(repo_root: Path, tag: str) -> str:
    return _resolve_release_tag_pair(repo_root, tag)[1]


def _resolve_release_tag_pair(repo_root: Path, tag: str) -> tuple[str, str]:
    """Resolve a release tag to the Bitbucket-side commit the nodes can fetch.

    GitHub and Bitbucket tags may legitimately point at different commits (ADR-0007:
    Bitbucket carries the operator-authored mirror of a GitHub-authored source), so
    cross-remote equivalence is verified by tree SHA, not commit SHA.
    """
    if not tag.startswith("release-"):
        raise RepositoryError("rollback requires an immutable release-* tag")
    origin_sha = _remote_tag_sha(repo_root, "origin", tag)
    bitbucket_sha = _remote_tag_sha(repo_root, "bitbucket", tag)
    subprocess.run(["git", "fetch", "origin", f"refs/tags/{tag}:refs/tags/{tag}"], cwd=repo_root, check=True)
    local_sha = subprocess.run(
        ["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if local_sha != origin_sha:
        raise RepositoryError(f"local release tag {tag!r} does not match its remotes")
    if origin_sha != bitbucket_sha:
        temp_ref = f"refs/edge-deploy/rollback/{tag}"
        subprocess.run(
            ["git", "fetch", "bitbucket", f"{bitbucket_sha}:{temp_ref}"], cwd=repo_root, check=True
        )
        try:
            if _tree_sha(repo_root, temp_ref) != _tree_sha(repo_root, origin_sha):
                raise RepositoryError(
                    f"release tag {tag!r} is not tree-equivalent between GitHub and Bitbucket"
                )
        finally:
            subprocess.run(["git", "update-ref", "-d", temp_ref], cwd=repo_root, check=True)
    return origin_sha, bitbucket_sha


def _write_rollback_publish_provenance(
    report_dir: Path,
    *,
    tool: str,
    source_sha: str,
    snapshot_sha: str,
    tag: str,
) -> None:
    """Seed resume-style publish provenance for an immutable rollback tag.

    Dependency delivery must build from the reviewed GitHub source SHA, while
    rollback deploys the tree-equivalent Bitbucket snapshot SHA that Edge nodes
    can fetch.  The immutable release tag supplies both SHAs, so rollback can use
    the same provenance contract as a normal v2 resume.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"publish-{tool}.json").write_text(
        json.dumps(
            {
                "tool": tool,
                "status": "published",
                "deployment_commit": snapshot_sha,
                "source_commit": source_sha,
                "source_short": source_sha[:7],
                "branch": "rollback",
                "message": f"Rollback to {tag}",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _cmd_rollback(args: argparse.Namespace, operator: OperatorConfig) -> int:
    repo_root = Path.cwd().resolve()
    profile = load_tool_profile(repo_root)
    effective_operator = replace(operator, tools={profile.tool: str(repo_root)})
    source, target = _resolve_release_tag_pair(repo_root, args.tag)
    node_names = resolve_nodes(effective_operator, args.nodes)
    _run_release_preflight(
        effective_operator,
        profile,
        repo_root,
        node_names,
        auth_mode=args.auth_mode,
        max_auth_attempts=args.max_auth_attempts,
        auth_wait_seconds=args.auth_wait_seconds,
        release_sha=target,
        allow_unresolved=True,
    )
    report_dir = Path(args.report_dir) if args.report_dir else _default_report_dir()
    _write_rollback_publish_provenance(
        report_dir,
        tool=profile.tool,
        source_sha=source,
        snapshot_sha=target,
        tag=args.tag,
    )
    report = run_release(
        effective_operator,
        ReleaseSelection(
            tools=[profile.tool],
            nodes=node_names,
            snapshot_by_tool={profile.tool: target},
            smoke=args.smoke,
        ),
        report_dir=report_dir,
        max_auth_attempts=args.max_auth_attempts,
        auth_mode="pane",
        auth_wait_seconds=args.auth_wait_seconds,
        heartbeat_interval_s=args.heartbeat_interval,
        stall_threshold_s=args.stall_threshold,
        progress_fn=lambda message: print(redact(f"[rollback] {message}")),
    )
    consolidated_path = write_release_report(report_dir / "release.json", report)
    _record_release_attempt(
        effective_operator,
        profile.tool,
        target,
        report_dir,
        report.summary()["overall"],
        linked_attempt=args.tag,
    )
    _print_release_summary(report, consolidated_path)
    return report.exit_code()


def _run_release_preflight(
    operator: OperatorConfig,
    profile,
    repo_root: Path,
    node_names: list[str],
    *,
    auth_mode: str,
    max_auth_attempts: int,
    auth_wait_seconds: float,
    release_sha: str | None = None,
    allow_unresolved: bool = False,
):
    if not profile.github_url:
        raise RepositoryError("edge_deploy.yaml must define github_url")
    state = inspect_repository(
        repo_root,
        tool=profile.tool,
        expected_origin=profile.github_url,
        expected_bitbucket=profile.bitbucket_url,
    )
    require_successful_github_ci(state)
    pytest_command = [sys.executable, "-m", "pytest", "-n", "8", "--dist", "loadfile"]
    completed = subprocess.run(pytest_command, cwd=repo_root)
    if completed.returncode:
        raise RuntimeError("python -m pytest -n 12 --dist loadfile failed; release blocked")
    if not operator.audit_repo:
        raise AuditSyncError("operator config must define audit_repo")
    check_audit_remote(
        Path(operator.audit_repo),
        tool=profile.tool,
        source_sha=release_sha or state.commit,
        allow_unresolved=allow_unresolved,
    )

    for node_name in node_names:
        node = operator.node(node_name)
        driver = TmuxDriver.from_node_and_profile(node, profile, retries=2)
        if auth_mode == "pane" or (auth_mode == "auto" and not sys.stdin.isatty()):
            authenticate_node_via_pane(
                driver,
                node_name,
                notify_fn=lambda message: print(redact(f"[release] {message}")),
                wait_timeout=auth_wait_seconds,
            )
        else:
            authenticate_node(
                driver,
                node_name,
                max_attempts=max_auth_attempts,
                wait_timeout=auth_wait_seconds,
            )
    return state


def _record_release_attempt(
    operator: OperatorConfig,
    tool: str,
    commit: str,
    report_dir: Path,
    status: str,
    linked_attempt: str | None = None,
) -> str:
    attempt = AuditAttempt(
        tool=tool,
        source_sha=commit,
        started_at=datetime.now(timezone.utc),
        report_dir=report_dir,
        core_version=__version__,
        operator=operator.operator_email,
        status=status,
        linked_attempt=linked_attempt,
    )
    return append_audit_attempt(Path(operator.audit_repo), attempt)


def _tag_successful_release(repo_root: Path, commit: str, deployment_commit: str | None = None) -> str:
    """Tag one successful release on both remotes with tree-equivalent targets (ADR-0007).

    GitHub gets the reviewed source commit. Bitbucket gets the commit that was actually
    deployed there (the operator-authored snapshot when Bitbucket's own-commits hook
    rejected the GitHub-authored source); both tags share one name and one tree.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"release-{stamp}-{commit[:7]}"
    subprocess.run(["git", "tag", "-a", tag, commit, "-m", f"Successful release {tag}"], cwd=repo_root, check=True)
    subprocess.run(["git", "push", "origin", f"refs/tags/{tag}"], cwd=repo_root, check=True)
    command = ["git"]
    token = os.environ.get("BB_TOKEN")
    if token:
        command.extend(["-c", f"http.extraHeader=Authorization: Bearer {token}"])
    deployed = deployment_commit or commit
    if deployed == commit:
        command.extend(["push", "bitbucket", f"refs/tags/{tag}"])
        subprocess.run(command, cwd=repo_root, check=True)
        return tag
    temp_tag = f"edge-deploy-mirror/{tag}"
    subprocess.run(
        ["git", "tag", "-a", "-f", temp_tag, deployed,
         "-m", f"Successful release {tag} (source {commit}) [edge-deploy]"],
        cwd=repo_root,
        check=True,
    )
    try:
        command.extend(["push", "bitbucket", f"refs/tags/{temp_tag}:refs/tags/{tag}"])
        subprocess.run(command, cwd=repo_root, check=True)
    finally:
        subprocess.run(["git", "tag", "-d", temp_tag], cwd=repo_root, check=True)
    return tag


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
    print(f"Published commit: {result.snapshot}")
    print(f"  source: {result.source_short} ({result.source_commit})")
    print(f"  branch: {result.branch}")
    print(f"  previous remote: {result.previous_remote_commit}")
    print(redact(f"  message: {result.message}"))
    return 0


def _cmd_mirror(args: argparse.Namespace) -> int:
    result = mirror_release(
        Path(args.repo_root).resolve(),
        tag=args.tag,
        remote=args.remote,
        branch=args.branch,
    )
    print(f"Mirrored {result.tag} to {args.remote} ({result.mode})")
    print(f"  source commit: {result.source_commit}")
    print(f"  deployed commit: {result.deployed_commit}")
    print(f"  shared tree: {result.tree}")
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
    if args.command == "mirror":
        # Mirror operates on the core checkout itself and needs no operator config.
        try:
            return _cmd_mirror(args)
        except MirrorError as exc:
            print(f"mirror failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    try:
        operator = OperatorConfig.load(args.config)
    except FileNotFoundError:
        print(f"Operator config not found: {args.config}", file=sys.stderr)
        return 2

    try:
        if args.command == "release":
            return _cmd_release(args, operator)
        if args.command == "rollback":
            return _cmd_rollback(args, operator)
        if args.command == "publish":
            return _cmd_publish(args, operator)
        if args.command == "rollout":
            return _cmd_rollout(args, operator)
        if args.command == "drift":
            return _cmd_drift(args, operator)
        if args.command == "preflight":
            return _cmd_preflight(args, operator)
    except (
        RuntimeError,
        PublishError,
        RepositoryError,
        AuditSyncError,
        AuthenticationError,
        SessionGoneError,
        KeyError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
