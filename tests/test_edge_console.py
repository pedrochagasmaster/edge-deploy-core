"""edge_console.collect_runs: progress-file inclusion, absent-safe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge_console import _SCHEMA, collect_runs  # noqa: E402


def _write_state(run_dir: Path, run_id: str) -> None:
    run_dir.mkdir(parents=True)
    state = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "tool": "autobench",
        "source_sha": "a" * 40,
        "operator": "pedro.chagas",
        "created_at": "2026-07-10T00:00:00+00:00",
        "kind": "release",
        "rollback_tag": None,
        "engine": {"version": "1.4.0", "package_dir": "(test)", "content_sha256": "b" * 64},
        "nodes": ["03"],
        "status": "open",
        "abandon_reason": None,
        "phases": {},
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def test_collect_runs_includes_progress_file_unchanged(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260710T000000Z-aaaaaaa"
    _write_state(run_dir, run_dir.name)
    progress_payload = {
        "schema": "edge-deploy/release-progress/1",
        "updated_at": "2026-07-10T00:00:05+00:00",
        "elapsed_s": 5.0,
        "active": {
            "phase": "rollout",
            "label": "rollout autobench/node03",
            "tool": "autobench",
            "node": "node03",
            "tmux_session": None,
            "last_meaningful_output_at": "2026-07-10T00:00:05+00:00",
            "waiting_on": None,
            "transfer": {
                "artifact": "autobench dependency bundle",
                "bytes_sent": 25,
                "total_bytes": 100,
                "percent": 25.0,
                "bytes_per_second": 12.5,
                "updated_at": "2026-07-10T00:00:05+00:00",
            },
        },
        "inactive_s": 0.0,
    }
    (run_dir / "release-progress.json").write_text(json.dumps(progress_payload), encoding="utf-8")

    runs = collect_runs(runs_root)

    assert len(runs) == 1
    assert runs[0]["progress"] == progress_payload


def test_collect_runs_returns_none_progress_when_file_absent(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260710T000100Z-bbbbbbb"
    _write_state(run_dir, run_dir.name)

    runs = collect_runs(runs_root)

    assert len(runs) == 1
    assert runs[0]["progress"] is None
