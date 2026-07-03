"""Run ledger: create/load, phases, locking, and engine identity."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from edge_deploy.ledger import (
    LedgerError,
    RunLedger,
    RunLockError,
    _content_sha256,
    engine_identity,
)


def _create_ledger(tmp_path: Path, **kwargs: object) -> RunLedger:
    defaults = {
        "tool": "autobench",
        "source_sha": "a" * 40,
        "nodes": ["node03", "node04"],
        "operator": "operator@example.com",
    }
    defaults.update(kwargs)
    return RunLedger.create(tmp_path / "runs", **defaults)


def test_create_load_round_trip(tmp_path: Path) -> None:
    created = _create_ledger(tmp_path)
    loaded = RunLedger.load(created.run_dir)
    assert loaded.state == created.state


def test_set_phase_persists_atomically(tmp_path: Path) -> None:
    ledger = _create_ledger(tmp_path)
    ledger.set_phase("verify", "passed", evidence={"commit": "abc"})
    on_disk = json.loads((ledger.run_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk["phases"]["verify"]["state"] == "passed"
    assert on_disk["phases"]["verify"]["evidence"] == {"commit": "abc"}
    assert on_disk["phases"]["verify"]["updated_at"] is not None


def test_deploy_requires_node(tmp_path: Path) -> None:
    ledger = _create_ledger(tmp_path)
    with pytest.raises(LedgerError, match="deploy phase requires node"):
        ledger.set_phase("deploy", "passed")
    ledger.set_phase("deploy", "passed", node="node03")
    assert ledger.phase_state("deploy", node="node03") == "passed"


def test_find_open_excludes_abandoned_and_complete(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    open_run = _create_ledger(tmp_path)
    complete = _create_ledger(
        tmp_path,
        source_sha="b" * 40,
    )
    complete.complete()
    abandoned = _create_ledger(
        tmp_path,
        source_sha="c" * 40,
    )
    abandoned.abandon("operator cancelled")

    found = RunLedger.find_open(runs_root)
    assert len(found) == 1
    assert found[0].state["run_id"] == open_run.state["run_id"]


def test_lock_collision_raises_exact_message(tmp_path: Path) -> None:
    ledger = _create_ledger(tmp_path)
    lock_payload = {
        "pid": 4242,
        "hostname": "edge-host",
        "acquired_at": "2026-07-03T12:00:00+00:00",
    }
    (ledger.run_dir / "run.lock").write_text(json.dumps(lock_payload), encoding="utf-8")
    run_id = ledger.state["run_id"]
    expected = (
        f"run {run_id} is locked by PID 4242 on edge-host "
        f"(acquired 2026-07-03T12:00:00+00:00); if that process is dead, "
        f"re-run with --force-lock"
    )
    with pytest.raises(RunLockError, match=re.escape(expected)):
        ledger.acquire_lock()


def test_locked_force_steals_stale_lock(tmp_path: Path) -> None:
    ledger = _create_ledger(tmp_path)
    lock_payload = {
        "pid": 9999,
        "hostname": "stale-host",
        "acquired_at": "2026-07-03T12:00:00+00:00",
    }
    (ledger.run_dir / "run.lock").write_text(json.dumps(lock_payload), encoding="utf-8")
    with ledger.locked(force=True):
        assert (ledger.run_dir / "run.lock").is_file()
    events = (ledger.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    stolen = [json.loads(line) for line in events if json.loads(line)["event"] == "lock_stolen"]
    assert len(stolen) == 1


def test_engine_identity_content_sha256(tmp_path: Path) -> None:
    identity = engine_identity()
    digest = identity["content_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)

    package_dir = Path(identity["package_dir"])
    copy_dir = tmp_path / "edge_deploy_copy"
    shutil.copytree(package_dir, copy_dir)
    assert _content_sha256(copy_dir) == digest

    (copy_dir / "extra_module.py").write_text("# added\n", encoding="utf-8")
    assert _content_sha256(copy_dir) != digest

    nested_dir = copy_dir / "nested_pkg"
    nested_dir.mkdir()
    (nested_dir / "nested_module.py").write_text("# nested\n", encoding="utf-8")
    assert _content_sha256(copy_dir) != digest


def test_acquire_lock_reentrant_same_instance(tmp_path: Path) -> None:
    ledger = _create_ledger(tmp_path)
    ledger.acquire_lock()
    ledger.acquire_lock()
    assert (ledger.run_dir / "run.lock").is_file()
    ledger.release_lock()
    assert (ledger.run_dir / "run.lock").is_file()
    ledger.release_lock()
    assert not (ledger.run_dir / "run.lock").is_file()
