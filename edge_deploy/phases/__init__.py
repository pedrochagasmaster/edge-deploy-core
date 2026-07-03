"""Release phase registry and shared ``enter_phase`` gate."""

from __future__ import annotations

import socket
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass

from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import RunLedger, engine_identity
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


def enter_phase(
    spec: PhaseSpec,
    operator: OperatorConfig | None,
    ledger: RunLedger,
    *,
    next_command: str,
    force_lock: bool = False,
    connect: SocketConnector = socket.create_connection,
) -> ExitStack:
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
            connect=connect,
        )
        ledger.record_event("phase_entered", phase=spec.name)
    except Exception:
        stack.close()
        raise
    return stack


from edge_deploy.phases.verify import VERIFY_SPEC, register_verify  # noqa: E402

PHASE_REGISTRY.append((VERIFY_SPEC, register_verify))

from . import publish as _publish  # noqa: E402, F401

from edge_deploy.phases.deploy import (  # noqa: E402, I001
    DEPLOY_SPEC,
    register as register_deploy,
)

PHASE_REGISTRY.append((DEPLOY_SPEC, register_deploy))

from edge_deploy.phases import tag as _tag  # noqa: E402, F401
