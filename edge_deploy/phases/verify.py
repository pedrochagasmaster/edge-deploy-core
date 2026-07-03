"""Verify phase: repository inspection, GitHub CI, and pytest gate."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy.config import OperatorConfig, load_tool_profile
from edge_deploy.ledger import RunLedger
from edge_deploy.phases import PhaseSpec, enter_phase
from edge_deploy.posture import PHASE_ENDPOINTS
from edge_deploy.repository import RepositoryError, inspect_repository, require_successful_github_ci

VERIFY_SPEC = PhaseSpec(name="verify", order=10, endpoints=PHASE_ENDPOINTS["verify"])


def ensure_verified(
    operator: OperatorConfig,
    profile,
    repo_root: Path,
    ledger: RunLedger,
    *,
    reverify: bool = False,
    repo_state=None,
):
    if not profile.github_url:
        raise RepositoryError("edge_deploy.yaml must define github_url")
    state = repo_state or inspect_repository(
        repo_root,
        tool=profile.tool,
        expected_origin=profile.github_url,
        expected_bitbucket=profile.bitbucket_url,
    )
    if (
        not reverify
        and ledger.phase_state("verify") == "passed"
        and ledger.state["source_sha"] == state.commit
    ):
        sha7 = state.commit[:7]
        print(f"verify: already passed for {sha7} (skipping)")
        ledger.record_event("phase_skipped", phase="verify")
        return state
    try:
        require_successful_github_ci(state)
        pytest_command = [sys.executable, "-m", "pytest", "-n", "8", "--dist", "loadfile"]
        completed = subprocess.run(pytest_command, cwd=repo_root)
        if completed.returncode:
            raise RuntimeError("python -m pytest -n 8 --dist loadfile failed; release blocked")
        verified_at = datetime.now(timezone.utc).isoformat()
        ledger.set_phase(
            "verify",
            "passed",
            evidence={
                "commit": state.commit,
                "ci": "success",
                "tests": "passed",
                "verified_at": verified_at,
            },
        )
        return state
    except Exception:
        ledger.set_phase(
            "verify",
            "failed",
            evidence={"commit": state.commit},
        )
        raise


def _cmd_verify(args: argparse.Namespace, operator: OperatorConfig) -> int:
    runs_root = Path.cwd().resolve() / "edge-deploy" / "runs"
    run_dir = runs_root / args.run
    if not run_dir.is_dir() or not (run_dir / "state.json").is_file():
        print(f"no such run: {args.run} under {runs_root}", file=sys.stderr)
        return 2
    ledger = RunLedger.load(run_dir)
    tool = ledger.state["tool"]
    repo_root = Path(operator.tool_path(tool)).resolve()
    profile = load_tool_profile(repo_root)
    next_command = f"python -m edge_deploy verify --run {args.run}"
    stack = enter_phase(
        VERIFY_SPEC,
        operator,
        ledger,
        next_command=next_command,
        force_lock=args.force_lock,
    )
    try:
        ensure_verified(
            operator,
            profile,
            repo_root,
            ledger,
            reverify=args.reverify,
        )
        return 0
    except (RepositoryError, RuntimeError) as exc:
        print(f"verify failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        stack.close()


def register_verify(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "verify",
        help="Verify repository state, GitHub CI, and pytest for a run",
    )
    parser.add_argument("--run", required=True, help="Run id to verify")
    parser.add_argument(
        "--reverify",
        action="store_true",
        help="Re-run verification even when already passed for the current source SHA",
    )
    parser.add_argument(
        "--force-lock",
        action="store_true",
        help="Steal the run lock if the holding process is dead",
    )
    parser.set_defaults(func=_cmd_verify)
