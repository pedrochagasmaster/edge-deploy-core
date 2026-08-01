"""Read-only access to run ledgers under ``<checkout>/edge-deploy/runs/``.

Raw JSON only: the console never imports the engine's ledger module and never
writes to a ledger. Engine Identity (ADR-0008) hashes every ``edge_deploy``
package file, so this package stays outside it.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "edge-deploy/run/1"
EVENT_TAIL = 60
CLOSED_RUN_LIMIT = 20


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tail_events(run_dir: Path) -> list[dict]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-EVENT_TAIL:]:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def runs_root_for(root: Path) -> Path:
    return Path(root) / "edge-deploy" / "runs"


def is_training_state(state: dict) -> bool:
    """Mirror :func:`edge_deploy.ledger.is_training_ledger`: either marker, strict."""
    return state.get("kind") == "training" or state.get("training") is True


def collect_runs(runs_root: Path) -> list[dict]:
    if not runs_root.is_dir():
        return []
    runs: list[dict] = []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        state = read_json(entry / "state.json")
        if not state or state.get("schema") != SCHEMA:
            continue
        lock = read_json(entry / "run.lock") if (entry / "run.lock").is_file() else None
        progress = read_json(entry / "release-progress.json")
        runs.append(
            {"state": state, "events": tail_events(entry), "lock": lock, "progress": progress}
        )
    # Open runs first, then newest first within each group.
    runs.sort(
        key=lambda r: (
            r["state"].get("status") != "open",
            r["state"].get("created_at", ""),
        ),
    )
    open_runs = [r for r in runs if r["state"].get("status") == "open"]
    closed = [r for r in runs if r["state"].get("status") != "open"]
    closed.sort(key=lambda r: r["state"].get("created_at", ""), reverse=True)
    return open_runs + closed[:CLOSED_RUN_LIMIT]


def collect_runs_multi(roots: list[Path]) -> list[dict]:
    """Merge runs from several tool checkouts, tagging each run with its root."""
    merged: list[dict] = []
    for root in roots:
        for run in collect_runs(runs_root_for(root)):
            run["root"] = str(root)
            merged.append(run)
    open_runs = [r for r in merged if r["state"].get("status") == "open"]
    open_runs.sort(key=lambda r: r["state"].get("created_at", ""))
    closed = [r for r in merged if r["state"].get("status") != "open"]
    closed.sort(key=lambda r: r["state"].get("created_at", ""), reverse=True)
    return open_runs + closed[:CLOSED_RUN_LIMIT]


def find_run_state(root: Path, run_id: str) -> dict | None:
    """The ``state.json`` of one run under ``root``, or None when it is not there."""
    run_dir = runs_root_for(root) / run_id
    if not run_dir.is_dir():
        return None
    state = read_json(run_dir / "state.json")
    if not state or state.get("schema") != SCHEMA:
        return None
    return state
