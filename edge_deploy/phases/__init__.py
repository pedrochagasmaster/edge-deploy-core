"""Release phase registry and shared ``enter_phase`` gate."""

from __future__ import annotations

import argparse
import socket
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import LedgerError, RunLedger, engine_identity, reject_training_ledger
from edge_deploy.posture import require_posture

SocketConnector = Callable[[tuple[str, int], float], object]


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    order: int
    endpoints: tuple[str, ...]


# Each phase module (PR-10..13) appends a (PhaseSpec, register_fn) pair here.
# register_fn(subparsers) adds its argparse subcommand.
PHASE_REGISTRY: list[tuple[PhaseSpec, Callable]] = []


class EngineMismatchError(RuntimeError):
    """Raised when the run ledger engine identity differs from this process."""


def _runs_root(repo_root: Path) -> Path:
    return repo_root / "edge-deploy" / "runs"


def load_run(args: argparse.Namespace, operator: OperatorConfig) -> tuple[RunLedger, Path]:
    """Locate a run directory under configured tool roots, then cwd."""
    for tool_path in operator.tools.values():
        repo_root = Path(tool_path).resolve()
        runs_root = _runs_root(repo_root)
        run_dir = runs_root / args.run
        if run_dir.is_dir() and (run_dir / "state.json").is_file():
            ledger = RunLedger.load(run_dir)
            reject_training_ledger(ledger)
            return ledger, repo_root

    repo_root = Path.cwd().resolve()
    runs_root = _runs_root(repo_root)
    run_dir = runs_root / args.run
    if not run_dir.is_dir() or not (run_dir / "state.json").is_file():
        print(f"no such run: {args.run} under {runs_root}", file=sys.stderr)
        raise SystemExit(2)
    ledger = RunLedger.load(run_dir)
    reject_training_ledger(ledger)
    return ledger, repo_root


def run_repo_root(ledger: RunLedger, operator: OperatorConfig, fallback: Path) -> Path:
    """The tool checkout a run operates on.

    The operator ``tools`` mapping is optional ("backward-compatible only" per
    docs/DESIGN.md; the real config defines none), so fall back to the repo root
    the run was found under (:func:`load_run`'s validated cwd fallback) instead
    of raising ``KeyError`` from ``operator.tool_path``.
    """
    tool = ledger.state["tool"]
    if tool in operator.tools:
        return Path(operator.tools[tool]).resolve()
    return fallback


def enter_phase(
    spec: PhaseSpec,
    operator: OperatorConfig | None,
    ledger: RunLedger,
    *,
    next_command: str,
    force_lock: bool = False,
    connect: SocketConnector | None = None,
    repo_root: Path | None = None,
    git_probe_runner: Callable | None = None,
) -> ExitStack:
    reject_training_ledger(ledger)
    run_status = ledger.state["status"]
    if run_status != "open":
        run_id = ledger.state["run_id"]
        raise LedgerError(
            f"phase '{spec.name}' refused: run {run_id} is {run_status}"
        )

    stack = ExitStack()
    ledger.acquire_lock(force=force_lock)
    stack.callback(ledger.release_lock)
    try:
        run_engine = ledger.state["engine"]["content_sha256"]
        current_engine = engine_identity()["content_sha256"]
        if run_engine != current_engine:
            run_id = ledger.state["run_id"]
            old8 = run_engine[:8]
            new8 = current_engine[:8]
            raise EngineMismatchError(
                f"engine mismatch: run {run_id} was created by engine {old8} "
                f"but this process is {new8}; finish the run with the original engine or abandon it"
            )
        require_posture(
            spec.name,
            operator,
            next_command=next_command,
            connect=connect or socket.create_connection,
            repo_root=repo_root,
            git_runner=git_probe_runner,
        )
        ledger.record_event("phase_entered", phase=spec.name)
    except Exception:
        stack.close()
        raise
    return stack


from edge_deploy.phases.verify import VERIFY_SPEC, register_verify  # noqa: E402

PHASE_REGISTRY.append((VERIFY_SPEC, register_verify))

from . import publish as _publish  # noqa: E402, F401, I001

from edge_deploy.phases.deploy import (  # noqa: E402, I001
    DEPLOY_SPEC,
    register as register_deploy,
)

PHASE_REGISTRY.append((DEPLOY_SPEC, register_deploy))

from edge_deploy.phases import tag as _tag  # noqa: E402, F401
