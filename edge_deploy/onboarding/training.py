"""Isolated training workspace and no-I/O guided release simulator.

Mutates training ledgers only via RunLedger APIs. Does not import or call
production publish/deploy/tag/verify/rollout/mirror executors, spawn
subprocesses, or touch the network.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy.ledger import LedgerError, RunLedger, is_training_ledger

TRAINING_PHASES: tuple[str, ...] = (
    "verify",
    "publish",
    "deploy",
    "tag_bitbucket",
    "tag_github",
)

_FIREWALL_OFF_ACK = (
    "TRAINING ONLY: a real guided release would switch both-vpns → firewall-off "
    "before tag_github. Do not change the workstation posture now; press Enter to "
    "simulate the acknowledgement."
)

_FABRICATED_PREVIOUS = "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e"


def create_training_workspace(app_dir: Path, tool: str) -> Path:
    root = Path(app_dir) / "training" / tool
    (root / "edge-deploy" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "edge_deploy.yaml").write_text(f"tool: {tool}\n", encoding="utf-8")
    return root


def start_training_ledger(
    workspace: Path, *, tool: str, operator: str, nodes: list[str]
) -> RunLedger:
    runs_root = workspace / "edge-deploy" / "runs"
    return RunLedger.create(
        runs_root,
        tool=tool,
        source_sha="0" * 40,
        nodes=nodes,
        operator=operator,
        kind="training",
        training=True,
    )


def _default_acknowledge(message: str) -> None:
    input(f"{message}\n")


def _require_open_training(ledger: RunLedger) -> None:
    if not is_training_ledger(ledger):
        raise LedgerError("advance_training requires a training ledger")
    status = ledger.state["status"]
    if status != "open":
        raise LedgerError(
            f"advance_training refused: run {ledger.state['run_id']} is {status}"
        )


def _phase_passed(ledger: RunLedger, phase: str) -> bool:
    if phase == "deploy":
        deploy = ledger.state["phases"]["deploy"]
        return all(entry["state"] == "passed" for entry in deploy.values())
    return ledger.phase_state(phase) == "passed"


def _ensure_prerequisites(ledger: RunLedger, phase: str) -> None:
    if phase not in TRAINING_PHASES:
        raise LedgerError(f"unknown training phase: {phase!r}")
    index = TRAINING_PHASES.index(phase)
    for prior in TRAINING_PHASES[:index]:
        if not _phase_passed(ledger, prior):
            raise LedgerError(
                f"training phase {phase!r} is out of order: prerequisite {prior!r} not passed"
            )


def _training_tag(source_sha: str) -> str:
    return f"release-training-{source_sha[:7]}"


def _fabricate_verify(ledger: RunLedger) -> None:
    sha = ledger.state["source_sha"]
    ledger.record_event("phase_entered", phase="verify")
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": sha,
            "ci": "success",
            "tests": "passed",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verification_command": "tools/dev/local_check.ps1",
        },
    )


def _fabricate_publish(ledger: RunLedger) -> None:
    sha = ledger.state["source_sha"]
    ledger.record_event("phase_entered", phase="publish")
    ledger.set_phase(
        "publish",
        "passed",
        evidence={
            "snapshot_sha": sha,
            "source_commit": sha,
            "previous_remote_commit": _FABRICATED_PREVIOUS,
            "verification_source": "training",
            "local_check_ran": False,
        },
    )


def _fabricate_deploy(ledger: RunLedger, nodes: list[str]) -> None:
    sha = ledger.state["source_sha"]
    tool = ledger.state["tool"]
    # Partial re-entry (remaining nodes) must not duplicate phase_entered.
    deploy_already_started = any(
        ledger.phase_state("deploy", node=node) != "pending"
        for node in ledger.state["nodes"]
    )
    if not deploy_already_started:
        ledger.record_event("phase_entered", phase="deploy")
    for node in nodes:
        if ledger.phase_state("deploy", node=node) == "passed":
            continue
        ledger.set_phase(
            "deploy",
            "passed",
            node=node,
            evidence={
                "tool": tool,
                "node": node,
                "status": "rolled_out",
                "state_left": "",
                "deployment_commit": sha,
                "previous_remote_commit": _FABRICATED_PREVIOUS,
                "sensitive_changed": [],
                "drift": "passed",
                "smoke": "passed",
                "report_path": f"(training)/rollout-{tool}-{node}.json",
                "dependency": None,
            },
        )


def _fabricate_tag(ledger: RunLedger, phase: str) -> None:
    sha = ledger.state["source_sha"]
    publish_evidence = ledger.state["phases"]["publish"]["evidence"]
    pushed_sha = str(publish_evidence.get("snapshot_sha") or sha)
    prior_tag = None
    for candidate in ("tag_bitbucket", "tag_github"):
        prior_tag = ledger.state["phases"][candidate]["evidence"].get("tag")
        if prior_tag:
            break
    tag = prior_tag or _training_tag(sha)
    ledger.record_event("phase_entered", phase=phase)
    ledger.set_phase(
        phase,
        "passed",
        evidence={"tag": tag, "pushed_sha": pushed_sha},
    )


def advance_training(
    ledger: RunLedger, phase: str, *, nodes: list[str] | None = None
) -> None:
    """Advance one training phase with fabricated evidence (no I/O)."""
    _require_open_training(ledger)
    if phase not in TRAINING_PHASES:
        raise LedgerError(f"unknown training phase: {phase!r}")

    if phase == "deploy":
        target_nodes = list(nodes) if nodes is not None else list(ledger.state["nodes"])
        if all(ledger.phase_state("deploy", node=n) == "passed" for n in target_nodes):
            return
    elif _phase_passed(ledger, phase):
        return

    _ensure_prerequisites(ledger, phase)

    if phase == "verify":
        _fabricate_verify(ledger)
    elif phase == "publish":
        _fabricate_publish(ledger)
    elif phase == "deploy":
        _fabricate_deploy(ledger, target_nodes)
    else:
        _fabricate_tag(ledger, phase)


def run_guided_training(
    ledger: RunLedger,
    *,
    acknowledge: Callable[[str], None] = _default_acknowledge,
) -> None:
    """Drive every training phase in order with one simulated posture acknowledgement."""
    _require_open_training(ledger)
    for phase in TRAINING_PHASES:
        if phase == "tag_github":
            acknowledge(_FIREWALL_OFF_ACK)
        advance_training(ledger, phase)
    if ledger.state["status"] == "open":
        ledger.complete()
