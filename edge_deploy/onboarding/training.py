"""Isolated training workspace and ledger factory (no production phase executors)."""

from __future__ import annotations

from pathlib import Path

from edge_deploy.ledger import RunLedger


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
