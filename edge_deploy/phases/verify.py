"""Verify phase: repository inspection, GitHub CI, and tool-owned gate."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy.config import OperatorConfig, load_tool_profile
from edge_deploy.ledger import RunLedger
from edge_deploy.local_check import (
    LOCAL_CHECK_RELATIVE,
    LocalCheckUnavailableError,
    run_local_check,
)
from edge_deploy.phases import PhaseSpec, enter_phase, load_run, run_repo_root
from edge_deploy.posture import PHASE_ENDPOINTS
from edge_deploy.reporting import redact
from edge_deploy.repository import RepositoryError, inspect_repository, require_successful_github_ci

VERIFY_SPEC = PhaseSpec(name="verify", order=10, endpoints=PHASE_ENDPOINTS["verify"])
VERIFY_DIAGNOSTIC = "verify-local-check.log"


def _has_complete_tool_verification(ledger: RunLedger, commit: str) -> bool:
    if ledger.phase_state("verify") != "passed":
        return False
    evidence = ledger.state["phases"]["verify"].get("evidence", {})
    verified_at = evidence.get("verified_at")
    return (
        ledger.state["source_sha"] == commit
        and evidence.get("commit") == commit
        and evidence.get("ci") == "success"
        and evidence.get("tests") == "passed"
        and isinstance(verified_at, str)
        and bool(verified_at.strip())
        and evidence.get("verification_command") == LOCAL_CHECK_RELATIVE.as_posix()
    )


def _record_gate_failure(
    ledger: RunLedger,
    *,
    commit: str,
    error_type: str,
    exit_code: int | None,
    output_tail: str,
) -> None:
    redacted_tail = redact(output_tail)
    (ledger.run_dir / VERIFY_DIAGNOSTIC).write_text(
        redacted_tail + ("\n" if redacted_tail else ""), encoding="utf-8"
    )
    ledger.set_phase(
        "verify",
        "failed",
        evidence={
            "commit": commit,
            "error_type": error_type,
            "exit_code": exit_code,
            "diagnostic_artifact": VERIFY_DIAGNOSTIC,
        },
    )
    if redacted_tail:
        print(redacted_tail, file=sys.stderr)


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

    if ledger.state.get("kind") == "rollback":
        tag = ledger.state.get("rollback_tag")
        if ledger.phase_state("verify") != "skipped":
            ledger.set_phase(
                "verify",
                "skipped",
                evidence={
                    "reason": "rollback tag provides reviewed source and snapshot SHA",
                    "rollback_tag": tag,
                    "source_sha": ledger.state["source_sha"],
                },
            )
            ledger.record_event("phase_skipped", phase="verify")
        print(f"verify: skipped for rollback run (tag {tag} provides reviewed SHA)")
        return repo_state or inspect_repository(
            repo_root,
            tool=profile.tool,
            expected_origin=profile.github_url,
            expected_bitbucket=profile.bitbucket_url,
        )

    state = repo_state or inspect_repository(
        repo_root,
        tool=profile.tool,
        expected_origin=profile.github_url,
        expected_bitbucket=profile.bitbucket_url,
    )
    try:
        if ledger.state.get("kind", "release") == "release":
            bound_sha = ledger.state["source_sha"]
            if state.commit != bound_sha:
                raise RepositoryError(
                    f"checkout drift: run expects source {bound_sha[:7]} but checkout is "
                    f"{state.commit[:7]}; switch the tool checkout to the reviewed commit "
                    f"or abandon the run"
                )
        if not reverify and _has_complete_tool_verification(ledger, state.commit):
            sha7 = state.commit[:7]
            print(f"verify: already passed for {sha7} (skipping)")
            ledger.record_event("phase_skipped", phase="verify")
            return state
        require_successful_github_ci(state)
        try:
            result = run_local_check(repo_root)
        except LocalCheckUnavailableError as exc:
            _record_gate_failure(
                ledger,
                commit=state.commit,
                error_type=type(exc).__name__,
                exit_code=None,
                output_tail="",
            )
            raise RuntimeError("committed tool verification gate is unavailable; release blocked") from exc
        if result.exit_code:
            _record_gate_failure(
                ledger,
                commit=state.commit,
                error_type="LocalCheckError",
                exit_code=result.exit_code,
                output_tail=result.output_tail,
            )
            raise RuntimeError(
                f"committed tool verification gate failed with exit code {result.exit_code}; "
                f"see {VERIFY_DIAGNOSTIC}"
            )
        verified_at = datetime.now(timezone.utc).isoformat()
        ledger.set_phase(
            "verify",
            "passed",
            evidence={
                "commit": state.commit,
                "ci": "success",
                "tests": "passed",
                "verified_at": verified_at,
                "verification_command": LOCAL_CHECK_RELATIVE.as_posix(),
            },
        )
        return state
    except Exception:
        if ledger.phase_state("verify") != "failed":
            ledger.set_phase(
                "verify",
                "failed",
                evidence={"commit": state.commit},
            )
        raise


def _cmd_verify(args: argparse.Namespace, operator: OperatorConfig) -> int:
    ledger, repo_root = load_run(args, operator)
    repo_root = run_repo_root(ledger, operator, repo_root)
    profile = load_tool_profile(repo_root)
    next_command = f"py -m edge_deploy verify --run {args.run}"
    stack = enter_phase(
        VERIFY_SPEC,
        operator,
        ledger,
        next_command=next_command,
        force_lock=args.force_lock,
        repo_root=repo_root,
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
        help="Verify repository state, GitHub CI, and the committed tool gate for a run",
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
