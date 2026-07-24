"""Training ledger markers and production rejection."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy import cli
from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import LedgerError, RunLedger, is_training_ledger
from edge_deploy.onboarding.training import create_training_workspace, start_training_ledger
from edge_deploy.phases import PHASE_REGISTRY, enter_phase, load_run
from edge_deploy.phases.publish import _cmd_publish_phase
from edge_deploy.phases.status import format_run_status


def _events(ledger: RunLedger) -> list[dict]:
    path = ledger.run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_training_ledger_has_both_markers(tmp_path: Path) -> None:
    app_dir = tmp_path / "appdata" / "edge-deploy"
    real_tool_root = tmp_path / "checkouts" / "autobench"
    real_tool_root.mkdir(parents=True)
    ws = create_training_workspace(app_dir, "autobench")
    assert ws == app_dir / "training" / "autobench"
    assert ws.resolve() != real_tool_root.resolve()
    assert (ws / "edge-deploy" / "runs").is_dir()
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    assert ledger.state["kind"] == "training"
    assert ledger.state["training"] is True
    assert is_training_ledger(ledger) is True
    assert is_training_ledger(ledger.state) is True


def test_production_ledger_omits_training_key(tmp_path: Path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    runs_root.mkdir(parents=True)
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op",
        kind="release",
    )
    assert "training" not in ledger.state
    assert is_training_ledger(ledger) is False
    on_disk = json.loads((ledger.run_dir / "state.json").read_text(encoding="utf-8"))
    assert "training" not in on_disk


def test_rollback_ledger_omits_training_key(tmp_path: Path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    runs_root.mkdir(parents=True)
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op",
        kind="rollback",
        rollback_tag="release-1.0.0",
    )
    assert "training" not in ledger.state
    assert is_training_ledger(ledger) is False


def test_create_with_either_flag_sets_both_markers(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    by_kind = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op",
        kind="training",
    )
    by_flag = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="b" * 40,
        nodes=["node03"],
        operator="op",
        training=True,
    )
    for ledger in (by_kind, by_flag):
        assert ledger.state["kind"] == "training"
        assert ledger.state["training"] is True
        assert is_training_ledger(ledger) is True


def test_is_training_ledger_accepts_either_marker_alone() -> None:
    assert is_training_ledger({"kind": "training"}) is True
    assert is_training_ledger({"kind": "release", "training": True}) is True
    assert is_training_ledger({"kind": "release"}) is False
    assert is_training_ledger({"kind": "release", "training": False}) is False


def test_legacy_ledger_without_training_key_remains_valid(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-legacy"
    run_dir.mkdir(parents=True)
    state = {
        "schema": "edge-deploy/run/1",
        "run_id": "run-legacy",
        "tool": "autobench",
        "source_sha": "a" * 40,
        "operator": "op",
        "created_at": "2026-07-03T12:00:00+00:00",
        "kind": "release",
        "rollback_tag": None,
        "engine": {
            "version": "0.0.0",
            "package_dir": "/pkg",
            "content_sha256": "c" * 64,
        },
        "nodes": ["node03"],
        "status": "open",
        "abandon_reason": None,
        "phases": {
            "verify": {"state": "pending", "updated_at": None, "evidence": {}},
            "publish": {"state": "pending", "updated_at": None, "evidence": {}},
            "deploy": {"node03": {"state": "pending", "updated_at": None, "evidence": {}}},
            "tag_bitbucket": {"state": "pending", "updated_at": None, "evidence": {}},
            "tag_github": {"state": "pending", "updated_at": None, "evidence": {}},
        },
    }
    assert "training" not in state
    (run_dir / "state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    loaded = RunLedger.load(run_dir)
    assert loaded.state["kind"] == "release"
    assert "training" not in loaded.state
    assert is_training_ledger(loaded) is False


def test_training_workspace_never_under_real_tool_runs_root(tmp_path: Path) -> None:
    app_dir = tmp_path / "appdata" / "edge-deploy"
    real_tool = tmp_path / "checkouts" / "robocop"
    (real_tool / "edge-deploy" / "runs").mkdir(parents=True)
    ws = create_training_workspace(app_dir, "robocop")
    assert ws == app_dir / "training" / "robocop"
    assert real_tool.resolve() not in ws.resolve().parents
    assert ws.resolve() != real_tool.resolve()
    assert not str(ws.resolve()).startswith(str((real_tool / "edge-deploy" / "runs").resolve()))


def test_enter_phase_rejects_training_ledger_before_lock(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "robocop")
    ledger = start_training_ledger(ws, tool="robocop", operator="trainee", nodes=["node03"])
    created_events = len(_events(ledger))
    spec = PHASE_REGISTRY[0][0]
    with pytest.raises(LedgerError, match="training ledger rejected"):
        enter_phase(spec, None, ledger, next_command="x")
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    assert not any(event["event"] == "phase_entered" for event in _events(ledger))


@pytest.mark.parametrize(
    "state_patch",
    [
        {"kind": "training"},
        {"kind": "release", "training": True},
    ],
)
def test_enter_phase_rejects_either_marker(tmp_path: Path, state_patch: dict) -> None:
    ledger = RunLedger.create(
        tmp_path / "runs",
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op",
    )
    ledger.state.update(state_patch)
    ledger._persist_state()
    with pytest.raises(LedgerError, match="training ledger rejected"):
        enter_phase(PHASE_REGISTRY[0][0], None, ledger, next_command="x")
    assert not (ledger.run_dir / "run.lock").is_file()


def test_load_run_rejects_training_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    monkeypatch.chdir(ws)
    operator = SimpleNamespace(tools={"autobench": str(ws)})
    args = SimpleNamespace(run=ledger.state["run_id"])
    with pytest.raises(LedgerError, match="training ledger rejected"):
        load_run(args, operator)  # type: ignore[arg-type]


def test_publish_phase_rejects_training_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    created_events = len(_events(ledger))
    monkeypatch.chdir(ws)

    def fail_run_publish(*_args, **_kwargs):
        raise AssertionError("publish must not run for training ledgers")

    monkeypatch.setattr(
        "edge_deploy.phases.publish.run_publish_phase",
        fail_run_publish,
    )
    args = SimpleNamespace(run=ledger.state["run_id"], no_local_check=False, force_lock=False)
    operator = SimpleNamespace(tools={"autobench": str(ws)}, tool_path=lambda t: ws)
    with pytest.raises(LedgerError, match="training ledger rejected"):
        _cmd_publish_phase(args, operator)  # type: ignore[arg-type]
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "open"
    assert reloaded.phase_state("publish") == "pending"


def test_release_continue_rejects_training_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "autobench")
    (ws / "edge_deploy.yaml").write_text(
        "tool: autobench\n"
        "repo_path: /ads_storage/autobench\n"
        "github_url: https://github.com/example/autobench.git\n"
        "bitbucket_url: https://scm.example/autobench.git\n"
        "release_branch: main\n"
        "runtime_paths: []\n"
        "compile_targets: src\n"
        "version_files: []\n"
        "install_trigger_paths: []\n"
        "dependency_paths: []\n"
        "smoke:\n"
        "  standard: []\n"
        "  deep: []\n",
        encoding="utf-8",
    )
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    created_events = len(_events(ledger))
    monkeypatch.chdir(ws)

    def fail_chain(*_args, **_kwargs):
        raise AssertionError("release chain must not run for training ledgers")

    monkeypatch.setattr(cli, "_chain_release_phases", fail_chain)
    args = SimpleNamespace(
        tool=None,
        run=ledger.state["run_id"],
        nodes=None,
        force_lock=False,
        guided=False,
        no_local_check=False,
        smoke="standard",
        max_auth_attempts=3,
        auth_mode="prompt",
        auth_wait_seconds=300.0,
        heartbeat_interval=30.0,
        stall_threshold=300.0,
    )
    operator = OperatorConfig.from_mapping(
        {
            "operator_email": "op@example.com",
            "nodes": {"node03": {"host": "user@edge.example"}},
            "tools": {"autobench": str(ws)},
        }
    )
    with pytest.raises(LedgerError, match="training ledger rejected"):
        cli._cmd_release(args, operator)
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    assert RunLedger.load(ledger.run_dir).state["status"] == "open"


def test_abandon_rejects_training_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    created_events = len(_events(ledger))
    monkeypatch.chdir(ws)
    args = SimpleNamespace(run=ledger.state["run_id"], reason="practice done")
    operator = SimpleNamespace(tools={})
    with pytest.raises(LedgerError, match="training ledger rejected"):
        cli._cmd_abandon(args, operator)  # type: ignore[arg-type]
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "open"
    assert reloaded.state["abandon_reason"] is None


def test_status_does_not_present_training_next_as_production_safe(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    output = format_run_status(ledger)
    assert "TRAINING" in output.upper()
    assert "py -m edge_deploy verify" not in output
    assert "py -m edge_deploy publish-phase" not in output
    assert "py -m edge_deploy deploy" not in output
