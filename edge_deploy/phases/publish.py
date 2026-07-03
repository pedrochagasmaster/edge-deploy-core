"""Publish phase: posture-gated Bitbucket snapshot publication."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from edge_deploy.audit import AuditSyncError, check_audit_remote
from edge_deploy.config import OperatorConfig, load_tool_profile
from edge_deploy.ledger import RunLedger
from edge_deploy.phases import PHASE_REGISTRY, PhaseSpec, enter_phase
from edge_deploy.posture import PHASE_ENDPOINTS
from edge_deploy.publish import PublishResult, publish_snapshot
from edge_deploy.release import _write_publish_report

PUBLISH_SPEC = PhaseSpec(name="publish", order=20, endpoints=PHASE_ENDPOINTS["publish"])

RemoteBranchHeadFn = Callable[[Path, str, str], str]
PublishFn = Callable[..., PublishResult]


def _default_remote_branch_head(repo_root: Path, remote: str, branch: str) -> str:
    completed = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[0].split()[0]


def run_publish_phase(
    ledger: RunLedger,
    operator: OperatorConfig,
    repo_root: Path,
    *,
    no_local_check: bool = False,
    force_lock: bool = False,
    remote_head_fn: RemoteBranchHeadFn | None = None,
    publish_fn: PublishFn | None = None,
) -> int:
    run_id = ledger.state["run_id"]
    next_command = f"python -m edge_deploy publish-phase --run {run_id}"
    resolve_remote_head = remote_head_fn or _default_remote_branch_head
    do_publish = publish_fn or publish_snapshot

    stack = enter_phase(
        PUBLISH_SPEC,
        operator,
        ledger,
        next_command=next_command,
        force_lock=force_lock,
    )
    with stack:
        if ledger.state.get("kind") == "rollback":
            if not ledger.state.get("rollback_tag"):
                print(
                    f"publish refused: rollback_tag is required for rollback run {run_id}",
                    file=sys.stderr,
                )
                return 2
            return 0

        if ledger.phase_state("verify") != "passed":
            print(
                f"publish refused: verify has not passed for run {run_id}",
                file=sys.stderr,
            )
            return 2

        profile = load_tool_profile(repo_root)
        if not operator.audit_repo:
            raise AuditSyncError("operator config must define audit_repo")
        check_audit_remote(
            Path(operator.audit_repo),
            tool=profile.tool,
            source_sha=ledger.state["source_sha"],
        )

        branch = profile.release_branch or "main"
        publish_evidence = ledger.state["phases"]["publish"]["evidence"]
        if ledger.phase_state("publish") == "passed":
            snapshot_sha = publish_evidence.get("snapshot_sha")
            if snapshot_sha:
                remote_head = resolve_remote_head(repo_root, "bitbucket", branch)
                if remote_head == snapshot_sha:
                    print(f"publish: already published {snapshot_sha[:7]} (skipping)")
                    ledger.record_event("phase_skipped", phase="publish")
                    return 0

        result = do_publish(
            profile,
            repo_root=repo_root,
            commit=ledger.state["source_sha"],
            run_local_check=not no_local_check,
        )
        _write_publish_report(ledger.run_dir, profile.tool, result, str(repo_root))
        ledger.set_phase(
            "publish",
            "passed",
            evidence={
                "snapshot_sha": result.snapshot,
                "source_commit": result.source_commit,
                "previous_remote_commit": result.previous_remote_commit,
            },
        )
        return 0


def _cmd_publish_phase(args: argparse.Namespace, operator: OperatorConfig) -> int:
    repo_root = Path.cwd().resolve()
    runs_root = repo_root / "edge-deploy" / "runs"
    run_dir = runs_root / args.run
    if not run_dir.is_dir() or not (run_dir / "state.json").is_file():
        print(f"no such run: {args.run} under {runs_root}", file=sys.stderr)
        return 2
    ledger = RunLedger.load(run_dir)
    tool = ledger.state["tool"]
    if tool in operator.tools:
        repo_root = Path(operator.tool_path(tool)).resolve()
    return run_publish_phase(
        ledger,
        operator,
        repo_root,
        no_local_check=args.no_local_check,
        force_lock=args.force_lock,
    )


def register_publish_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "publish-phase",
        help="Publish reviewed source to Bitbucket (posture-gated)",
    )
    parser.add_argument("--run", required=True, help="Run id to publish for")
    parser.add_argument(
        "--no-local-check",
        action="store_true",
        help="Skip local_check.ps1 before publishing",
    )
    parser.add_argument(
        "--force-lock",
        action="store_true",
        help="Steal the run lock if another process holds it",
    )
    parser.set_defaults(func=_cmd_publish_phase)


PHASE_REGISTRY.append((PUBLISH_SPEC, register_publish_parser))
