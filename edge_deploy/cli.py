"""Thin CLI surface: ``python -m edge_deploy {release|publish|rollout|drift|preflight}``.

Resolves ``--tool`` / ``--node`` against the two config layers (OperatorConfig + the
tool's ToolProfile), establishes an Authenticated Pane, and calls the engine. The umbrella
``release`` orchestrator (Publish + fan-out + getpass auth seam) and the standalone
``publish`` command are wired here on top of the Phase-1 ``rollout`` / ``drift`` engine.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy import __version__, drift, preflight, rollout
from edge_deploy.audit import AuditAttempt, AuditSyncError, append_audit_attempt
from edge_deploy.auth import authenticate_node, authenticate_node_via_pane
from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, OperatorConfig, load_tool_profile
from edge_deploy.ledger import LedgerError, RunLedger
from edge_deploy.mirror import MirrorError, mirror_release
from edge_deploy.phases import PHASE_REGISTRY, EngineMismatchError, enter_phase
from edge_deploy.phases.deploy import run_deploy
from edge_deploy.phases.publish import run_publish_phase
from edge_deploy.phases.tag import _cmd_tag_bitbucket, _cmd_tag_github
from edge_deploy.phases.verify import VERIFY_SPEC, ensure_verified
from edge_deploy.posture import PHASE_ENDPOINTS, PostureError, SocketConnector, endpoints_for, probe
from edge_deploy.publish import PublishError, publish_snapshot
from edge_deploy.release import resolve_nodes
from edge_deploy.reporting import OperationReport, redact, write_report
from edge_deploy.repository import RepositoryError, inspect_repository
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver

TOOL_CHOICES = ("autobench", "robocop")
RELEASE_PHASES = ("verify", "publish", "deploy", "tag_github", "tag_bitbucket")


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
    release_parser.add_argument("--run", default=None, help="Resume an existing run by run id")
    release_parser.add_argument("--force-lock", action="store_true")
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
    rollback_parser.add_argument("--force-lock", action="store_true")
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

    abandon_parser = subparsers.add_parser("abandon", help="Abandon an open run")
    abandon_parser.add_argument("--run", required=True)
    abandon_parser.add_argument("--reason", required=True)

    for _spec, register_fn in sorted(PHASE_REGISTRY, key=lambda item: item[0].order):
        register_fn(subparsers)

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


def _runs_root(repo_root: Path) -> Path:
    return repo_root / "edge-deploy" / "runs"


def _default_report_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("edge-deploy") / "reports" / f"release-{stamp}"


def _print_open_run_refusal(run: dict) -> None:
    run_id = run["run_id"]
    tool = run["tool"]
    sha7 = run["source_sha"][:7]
    created_at = run["created_at"]
    print(
        f"release refused: unresolved run {run_id} for {tool} "
        f"(source {sha7}, created {created_at}) exists."
    )
    print("Choose one:")
    print(f"  1. continue it:   python -m edge_deploy release --run {run_id}")
    print(f'  2. abandon it:    python -m edge_deploy abandon --run {run_id} --reason "<why>"')


def _deploy_auth_mode(auth_mode: str) -> str:
    if auth_mode == "pane" or (auth_mode == "auto" and not sys.stdin.isatty()):
        return "pane"
    return "prompt"


def _phase_already_passed(ledger: RunLedger, phase: str, requested_nodes: list[str]) -> bool:
    if phase == "deploy":
        return all(ledger.phase_state("deploy", node=node) == "passed" for node in requested_nodes)
    return ledger.phase_state(phase) == "passed"


def _posture_blocks_phase(
    phase: str,
    operator: OperatorConfig,
    run_id: str,
    *,
    connect: SocketConnector | None = None,
) -> bool:
    connector = connect or socket.create_connection
    unreachable = probe(endpoints_for(phase, operator), connect=connector)
    if not unreachable:
        return False
    posture_keys = ", ".join(PHASE_ENDPOINTS[phase])
    unreachable_hosts = ", ".join(f"{endpoint.host}:{endpoint.port}" for endpoint in unreachable)
    next_command = f"python -m edge_deploy release --run {run_id}"
    print(
        f"phase '{phase}' requires posture [{posture_keys}]; unreachable: {unreachable_hosts}.\n"
        f"Switch the firewall posture, then re-run: {next_command}"
    )
    return True


def _run_verify_phase(
    operator: OperatorConfig,
    profile,
    repo_root: Path,
    ledger: RunLedger,
    *,
    force_lock: bool,
    repo_state=None,
    connect: SocketConnector | None = None,
) -> int:
    run_id = ledger.state["run_id"]
    next_command = f"python -m edge_deploy verify --run {run_id}"
    connector = connect or socket.create_connection
    stack = enter_phase(
        VERIFY_SPEC,
        operator,
        ledger,
        next_command=next_command,
        force_lock=force_lock,
        connect=connector,
    )
    try:
        ensure_verified(
            operator,
            profile,
            repo_root,
            ledger,
            reverify=False,
            repo_state=repo_state,
        )
        return 0
    except (RepositoryError, RuntimeError) as exc:
        print(f"verify failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        stack.close()


def _invoke_release_phase(
    phase: str,
    args: argparse.Namespace,
    operator: OperatorConfig,
    ledger: RunLedger,
    *,
    repo_root: Path,
    profile,
    effective_operator: OperatorConfig,
    node_names: list[str],
    repo_state=None,
    connect: SocketConnector | None = None,
) -> int:
    run_id = ledger.state["run_id"]
    connector = connect or socket.create_connection
    if phase == "verify":
        return _run_verify_phase(
            effective_operator,
            profile,
            repo_root,
            ledger,
            force_lock=args.force_lock,
            repo_state=repo_state,
            connect=connector,
        )
    if phase == "publish":
        return run_publish_phase(
            ledger,
            effective_operator,
            repo_root,
            no_local_check=args.no_local_check,
            force_lock=args.force_lock,
        )
    if phase == "deploy":
        deploy_args = argparse.Namespace(
            run=run_id,
            nodes=args.nodes,
            smoke=args.smoke,
            auth_mode=_deploy_auth_mode(args.auth_mode),
            auth_wait_seconds=args.auth_wait_seconds,
            force_lock=args.force_lock,
        )
        return run_deploy(deploy_args, operator)
    if phase == "tag_github":
        tag_args = argparse.Namespace(run=run_id, force_lock=args.force_lock)
        return _cmd_tag_github(tag_args, operator)
    if phase == "tag_bitbucket":
        tag_args = argparse.Namespace(run=run_id, force_lock=args.force_lock)
        return _cmd_tag_bitbucket(tag_args, operator)
    raise ValueError(f"unknown release phase: {phase}")


def _chain_release_phases(
    args: argparse.Namespace,
    operator: OperatorConfig,
    ledger: RunLedger,
    *,
    repo_root: Path,
    profile,
    effective_operator: OperatorConfig,
    node_names: list[str],
    repo_state=None,
    connect: SocketConnector | None = None,
) -> int:
    run_id = ledger.state["run_id"]
    connector = connect or socket.create_connection
    for phase in RELEASE_PHASES:
        if _phase_already_passed(ledger, phase, node_names):
            continue
        if _posture_blocks_phase(phase, effective_operator, run_id, connect=connector):
            return 0
        code = _invoke_release_phase(
            phase,
            args,
            operator,
            ledger,
            repo_root=repo_root,
            profile=profile,
            effective_operator=effective_operator,
            node_names=node_names,
            repo_state=repo_state,
            connect=connector,
        )
        if code != 0:
            return code
        ledger = RunLedger.load(ledger.run_dir)
    print(f"release complete: {run_id}")
    return 0


def _cmd_release(args: argparse.Namespace, operator: OperatorConfig) -> int:
    repo_root = Path.cwd().resolve()
    if args.tool:
        repo_root = Path(operator.tool_path(args.tool)).resolve()
    profile = load_tool_profile(repo_root)
    if args.tool and args.tool != profile.tool:
        raise ValueError(f"--tool {args.tool!r} does not match checkout profile {profile.tool!r}")
    effective_operator = replace(operator, tools={profile.tool: str(repo_root)})
    node_names = resolve_nodes(effective_operator, args.nodes)
    runs_root = _runs_root(repo_root)
    repo_state = None
    if args.run:
        run_dir = runs_root / args.run
        if not run_dir.is_dir() or not (run_dir / "state.json").is_file():
            print(f"no such run: {args.run} under {runs_root}", file=sys.stderr)
            return 2
        ledger = RunLedger.load(run_dir)
    else:
        open_runs = RunLedger.find_open(runs_root)
        if open_runs:
            for open_ledger in open_runs:
                _print_open_run_refusal(open_ledger.state)
            return 2
        repo_state = inspect_repository(
            repo_root,
            tool=profile.tool,
            expected_origin=profile.github_url,
            expected_bitbucket=profile.bitbucket_url,
        )
        ledger = RunLedger.create(
            runs_root,
            tool=profile.tool,
            source_sha=repo_state.commit,
            nodes=node_names,
            operator=operator.operator_email,
        )
    if repo_state is None:
        repo_state = inspect_repository(
            repo_root,
            tool=profile.tool,
            expected_origin=profile.github_url,
            expected_bitbucket=profile.bitbucket_url,
        )
    return _chain_release_phases(
        args,
        operator,
        ledger,
        repo_root=repo_root,
        profile=profile,
        effective_operator=effective_operator,
        node_names=node_names,
        repo_state=repo_state,
    )


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
    """Seed resume-style publish provenance for an immutable rollback tag."""
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
    runs_root = _runs_root(repo_root)
    ledger = RunLedger.create(
        runs_root,
        tool=profile.tool,
        source_sha=source,
        nodes=node_names,
        operator=operator.operator_email,
        kind="rollback",
        rollback_tag=args.tag,
    )
    report_dir = ledger.run_dir
    _write_rollback_publish_provenance(
        report_dir,
        tool=profile.tool,
        source_sha=source,
        snapshot_sha=target,
        tag=args.tag,
    )
    ledger.set_phase(
        "publish",
        "passed",
        evidence={"snapshot_sha": target, "source_commit": source},
    )
    repo_state = inspect_repository(
        repo_root,
        tool=profile.tool,
        expected_origin=profile.github_url,
        expected_bitbucket=profile.bitbucket_url,
    )
    return _chain_release_phases(
        args,
        operator,
        ledger,
        repo_root=repo_root,
        profile=profile,
        effective_operator=effective_operator,
        node_names=node_names,
        repo_state=repo_state,
    )


def _run_release_preflight(
    operator: OperatorConfig,
    profile,
    repo_root: Path,
    node_names: list[str],
    ledger: RunLedger,
    *,
    auth_mode: str,
    max_auth_attempts: int,
    auth_wait_seconds: float,
    repo_state=None,
):
    state = ensure_verified(
        operator,
        profile,
        repo_root,
        ledger,
        reverify=False,
        repo_state=repo_state,
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


def _cmd_abandon(args: argparse.Namespace, operator: OperatorConfig) -> int:
    repo_root = Path.cwd().resolve()
    runs_root = _runs_root(repo_root)
    run_dir = runs_root / args.run
    if not run_dir.is_dir() or not (run_dir / "state.json").is_file():
        print(f"no such run: {args.run} under {runs_root}", file=sys.stderr)
        return 2
    ledger = RunLedger.load(run_dir)
    ledger.abandon(args.reason)
    print(f"abandoned {args.run}")
    return 0


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
        run_id="edge-deploy",
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
        if args.command == "abandon":
            return _cmd_abandon(args, operator)
        if args.command == "publish":
            return _cmd_publish(args, operator)
        if args.command == "rollout":
            return _cmd_rollout(args, operator)
        if args.command == "drift":
            return _cmd_drift(args, operator)
        if args.command == "preflight":
            return _cmd_preflight(args, operator)
        func = getattr(args, "func", None)
        if func is not None:
            return func(args, operator)
    except (
        RuntimeError,
        PublishError,
        RepositoryError,
        AuditSyncError,
        AuthenticationError,
        SessionGoneError,
        LedgerError,
        EngineMismatchError,
        PostureError,
        KeyError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
