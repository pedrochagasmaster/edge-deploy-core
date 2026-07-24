"""Training ledger markers, production rejection, and guided simulator."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy import cli
from edge_deploy.cli import RELEASE_PHASES
from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import (
    _VALID_PHASE_STATES,
    _VALID_STATUSES,
    LedgerError,
    RunLedger,
    is_training_ledger,
    reject_training_ledger,
)
from edge_deploy.onboarding.training import (
    TRAINING_PHASES,
    advance_training,
    create_training_workspace,
    run_guided_training,
    start_training_ledger,
)
from edge_deploy.phases import PHASE_REGISTRY, enter_phase, load_run
from edge_deploy.phases.publish import _cmd_publish_phase
from edge_deploy.phases.status import format_run_status

_TRAINING_PROFILE = (
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
    "  deep: []\n"
)


def _training_workspace_with_profile(tmp_path: Path, tool: str = "autobench") -> Path:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", tool)
    (ws / "edge_deploy.yaml").write_text(_TRAINING_PROFILE.replace("autobench", tool), encoding="utf-8")
    return ws


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


def test_reject_training_ledger_central_message() -> None:
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
        reject_training_ledger({"kind": "training", "training": True})


def test_enter_phase_rejects_training_ledger_before_lock(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "robocop")
    ledger = start_training_ledger(ws, tool="robocop", operator="trainee", nodes=["node03"])
    created_events = len(_events(ledger))
    spec = PHASE_REGISTRY[0][0]
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
        enter_phase(spec, None, ledger, next_command="x")
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    assert not any(event["event"] == "phase_entered" for event in _events(ledger))


def test_enter_phase_rejects_training_before_generic_status(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "robocop")
    ledger = start_training_ledger(ws, tool="robocop", operator="trainee", nodes=["node03"])
    ledger.state["status"] = "abandoned"
    ledger.state["abandon_reason"] = "practice done"
    ledger._persist_state()
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$") as exc:
        enter_phase(PHASE_REGISTRY[0][0], None, ledger, next_command="x")
    assert "abandoned" not in str(exc.value)
    assert not (ledger.run_dir / "run.lock").is_file()


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
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
        enter_phase(PHASE_REGISTRY[0][0], None, ledger, next_command="x")
    assert not (ledger.run_dir / "run.lock").is_file()


def test_load_run_rejects_training_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    monkeypatch.chdir(ws)
    operator = SimpleNamespace(tools={"autobench": str(ws)})
    args = SimpleNamespace(run=ledger.state["run_id"])
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
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
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
        _cmd_publish_phase(args, operator)  # type: ignore[arg-type]
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "open"
    assert reloaded.phase_state("publish") == "pending"


def test_release_continue_rejects_training_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _training_workspace_with_profile(tmp_path)
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
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
        cli._cmd_release(args, operator)
    assert not (ledger.run_dir / "run.lock").is_file()
    assert len(_events(ledger)) == created_events
    assert RunLedger.load(ledger.run_dir).state["status"] == "open"


def test_bare_release_refuses_open_training_without_production_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _training_workspace_with_profile(tmp_path)
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    run_id = ledger.state["run_id"]
    monkeypatch.chdir(ws)

    def fail_chain(*_args, **_kwargs):
        raise AssertionError("release chain must not run for training ledgers")

    monkeypatch.setattr(cli, "_chain_release_phases", fail_chain)
    args = SimpleNamespace(
        tool=None,
        run=None,
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
    rc = cli._cmd_release(args, operator)
    assert rc == 2
    out = capsys.readouterr().out
    assert "training" in out.lower()
    assert run_id in out
    assert f"release --run {run_id}" not in out
    assert f"abandon --run {run_id}" not in out
    assert "py -m edge_deploy release --run" not in out
    assert "py -m edge_deploy abandon --run" not in out
    assert not (ledger.run_dir / "run.lock").is_file()
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
    with pytest.raises(LedgerError, match="^training ledger rejected by production commands$"):
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
    assert "next: TRAINING ONLY (not a production command)" in output
    assert output.endswith("next: TRAINING ONLY (not a production command)")
    for forbidden in (
        "py -m edge_deploy verify",
        "py -m edge_deploy publish-phase",
        "py -m edge_deploy deploy",
        "py -m edge_deploy tag-github",
        "py -m edge_deploy tag-bitbucket",
        "py -m edge_deploy release",
        "py -m edge_deploy abandon",
        "release --run",
        "abandon --run",
    ):
        assert forbidden not in output, forbidden


_TRAINING_MODULE = Path("edge_deploy/onboarding/training.py")
_FIREWALL_OFF_PROMPT = (
    "TRAINING ONLY: a real guided release would switch both-vpns → firewall-off "
    "before tag_github. Do not change the workstation posture now; press Enter to "
    "simulate the acknowledgement."
)
_ENGINE_EVENTS = frozenset(
    {
        "run_created",
        "phase_entered",
        "phase_skipped",
        "lock_stolen",
        "run_abandoned",
        "run_completed",
    }
)
_ROLLOUT_EVIDENCE_KEYS = frozenset(
    {
        "tool",
        "node",
        "status",
        "state_left",
        "deployment_commit",
        "previous_remote_commit",
        "sensitive_changed",
        "drift",
        "smoke",
        "report_path",
        "dependency",
    }
)
_FORBIDDEN_EXECUTOR_PREFIXES = (
    "edge_deploy.phases.publish",
    "edge_deploy.phases.deploy",
    "edge_deploy.phases.tag",
    "edge_deploy.phases.verify",
    "edge_deploy.publish",
    "edge_deploy.rollout",
    "edge_deploy.mirror",
    "edge_deploy.release",
)


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_forbidden_module(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in _FORBIDDEN_EXECUTOR_PREFIXES
    )


def _forbidden_modules_from_source(source: str) -> set[str]:
    """Return forbidden production executor module names referenced in source."""
    tree = ast.parse(source)
    found: set[str] = set()

    def note(name: str | None) -> None:
        if name and _is_forbidden_module(name):
            found.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                note(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            note(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                note(f"{base}.{alias.name}" if base else alias.name)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__" and node.args:
                note(_literal_str(node.args[0]))
            if isinstance(func, ast.Attribute) and func.attr == "import_module" and node.args:
                note(_literal_str(node.args[0]))
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "__import__"
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
                and node.args
            ):
                note(_literal_str(node.args[0]))
    return found


def test_ast_guard_detects_aliases_prefixes_and_dynamic_imports() -> None:
    samples = {
        "import edge_deploy.phases.publish as pub": "edge_deploy.phases.publish",
        "from edge_deploy.phases import deploy as d": "edge_deploy.phases.deploy",
        "from edge_deploy.rollout.helpers import x": "edge_deploy.rollout.helpers",
        "from edge_deploy import release": "edge_deploy.release",
        '__import__("edge_deploy.phases.tag")': "edge_deploy.phases.tag",
        'importlib.import_module("edge_deploy.mirror")': "edge_deploy.mirror",
        'importlib.__import__("edge_deploy.publish")': "edge_deploy.publish",
        'importlib.import_module("edge_deploy.phases.verify.extra")': (
            "edge_deploy.phases.verify.extra"
        ),
    }
    for source, expected in samples.items():
        found = _forbidden_modules_from_source(source)
        assert any(_is_forbidden_module(name) for name in found), source
        assert expected in found or any(
            expected == name or name.startswith(expected + ".") or expected.startswith(name + ".")
            for name in found
        ), (source, found)


def test_training_module_does_not_import_production_executors() -> None:
    source = _TRAINING_MODULE.read_text(encoding="utf-8")
    assert _forbidden_modules_from_source(source) == set()


def test_training_module_has_no_subprocess_or_network_calls() -> None:
    tree = ast.parse(_TRAINING_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden_roots = {"subprocess", "socket", "urllib", "http", "requests", "paramiko"}
    assert not (imported & forbidden_roots)


def test_guided_training_advances_all_phases(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03", "node04"])
    prompts: list[str] = []

    def acknowledge(message: str) -> None:
        prompts.append(message)

    run_guided_training(ledger, acknowledge=acknowledge)
    ledger = RunLedger.load(ledger.run_dir)
    assert ledger.state["status"] == "complete"
    for phase in TRAINING_PHASES:
        if phase == "deploy":
            assert all(
                ledger.state["phases"]["deploy"][n]["state"] == "passed" for n in ("node03", "node04")
            )
        else:
            assert ledger.state["phases"][phase]["state"] == "passed"
    assert any("firewall-off" in p for p in prompts)
    assert any("simulated" in p.lower() or "training" in p.lower() for p in prompts)


def test_guided_training_prompt_text_and_order(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03"])
    prompts: list[str] = []
    phase_when_prompted: list[str] = []

    def acknowledge(message: str) -> None:
        prompts.append(message)
        phase_when_prompted.append(ledger.phase_state("tag_bitbucket"))
        assert ledger.phase_state("tag_github") == "pending"

    run_guided_training(ledger, acknowledge=acknowledge)
    assert prompts == [_FIREWALL_OFF_PROMPT]
    assert phase_when_prompted == ["passed"]
    assert "Do not change the workstation posture" in prompts[0]


def test_guided_training_console_compatible_shape_and_events(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(
        ws, tool="autobench", operator="op", nodes=["node03", "node04"]
    )
    run_guided_training(ledger, acknowledge=lambda _msg: None)
    ledger = RunLedger.load(ledger.run_dir)
    state = ledger.state
    assert state["schema"] == "edge-deploy/run/1"
    assert state["status"] in _VALID_STATUSES
    assert state["status"] == "complete"
    assert state["kind"] == "training"
    assert state["training"] is True
    assert sorted(state["phases"]["deploy"]) == sorted(state["nodes"])

    sha = state["source_sha"]
    verify = state["phases"]["verify"]
    assert verify["state"] == "passed"
    assert verify["evidence"]["commit"] == sha
    assert verify["evidence"]["ci"] == "success"
    assert verify["evidence"]["tests"] == "passed"
    assert "verified_at" in verify["evidence"]

    publish = state["phases"]["publish"]
    assert publish["state"] == "passed"
    assert publish["evidence"]["snapshot_sha"] == sha
    assert publish["evidence"]["source_commit"] == sha
    assert publish["evidence"]["previous_remote_commit"]

    for node in ("node03", "node04"):
        node_phase = state["phases"]["deploy"][node]
        assert node_phase["state"] in _VALID_PHASE_STATES
        assert node_phase["state"] == "passed"
        evidence = node_phase["evidence"]
        assert _ROLLOUT_EVIDENCE_KEYS <= set(evidence)
        assert evidence["tool"] == "autobench"
        assert evidence["node"] == node
        assert evidence["status"] == "rolled_out"
        assert evidence["deployment_commit"] == sha
        assert evidence["drift"] == "passed"
        assert evidence["smoke"] == "passed"

    tag = state["phases"]["tag_bitbucket"]["evidence"]["tag"]
    assert tag.startswith("release-")
    assert state["phases"]["tag_bitbucket"]["evidence"]["pushed_sha"] == sha
    assert state["phases"]["tag_github"]["evidence"]["tag"] == tag
    assert state["phases"]["tag_github"]["evidence"]["pushed_sha"] == sha

    events = _events(ledger)
    event_names = [event["event"] for event in events]
    assert event_names[0] == "run_created"
    assert event_names[-1] == "run_completed"
    entered = [e["phase"] for e in events if e["event"] == "phase_entered"]
    assert entered == list(TRAINING_PHASES)
    assert all(event["event"] in _ENGINE_EVENTS for event in events)


def test_advance_training_rejects_unknown_phase(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03"])
    with pytest.raises(LedgerError, match="unknown training phase"):
        advance_training(ledger, "mirror")


def test_advance_training_rejects_out_of_order(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03"])
    with pytest.raises(LedgerError, match="out of order|prerequisite"):
        advance_training(ledger, "publish")
    assert ledger.phase_state("publish") == "pending"
    assert ledger.phase_state("verify") == "pending"


def test_advance_training_rerun_is_idempotent(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03", "node04"])
    for phase in ("verify", "publish", "deploy", "tag_bitbucket", "tag_github"):
        advance_training(ledger, phase)
        snapshot = json.loads(json.dumps(ledger.state["phases"][phase]))
        events_before = _events(ledger)
        advance_training(ledger, phase)
        assert ledger.state["phases"][phase] == snapshot
        assert _events(ledger) == events_before
        if phase == "deploy":
            assert all(
                ledger.phase_state("deploy", node=n) == "passed"
                for n in ("node03", "node04")
            )
        else:
            assert ledger.phase_state(phase) == "passed"


def test_partial_deploy_reentry_does_not_duplicate_phase_entered(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03", "node04"])
    advance_training(ledger, "verify")
    advance_training(ledger, "publish")
    advance_training(ledger, "deploy", nodes=["node03"])
    assert ledger.phase_state("deploy", node="node03") == "passed"
    assert ledger.phase_state("deploy", node="node04") == "pending"
    first_entered = [e for e in _events(ledger) if e["event"] == "phase_entered"]
    assert [e["phase"] for e in first_entered] == ["verify", "publish", "deploy"]

    advance_training(ledger, "deploy", nodes=["node04"])
    assert ledger.phase_state("deploy", node="node04") == "passed"
    entered = [e["phase"] for e in _events(ledger) if e["event"] == "phase_entered"]
    assert entered == ["verify", "publish", "deploy"]
    assert entered.count("deploy") == 1


def test_advance_training_rejects_non_training_ledger(tmp_path: Path) -> None:
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
    with pytest.raises(LedgerError, match="training"):
        advance_training(ledger, "verify")
    assert ledger.phase_state("verify") == "pending"


def test_advance_training_rejects_when_run_not_open(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03"])
    ledger.complete()
    with pytest.raises(LedgerError, match="not open|complete"):
        advance_training(ledger, "verify")


def test_guided_training_preserves_markers_and_stays_in_training_root(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    real_tool = tmp_path / "checkouts" / "autobench"
    (real_tool / "edge-deploy" / "runs").mkdir(parents=True)
    before_real = {p for p in real_tool.rglob("*") if p.is_file()}

    ws = create_training_workspace(appdata, "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03", "node04"])
    run_guided_training(ledger, acknowledge=lambda _msg: None)
    ledger = RunLedger.load(ledger.run_dir)

    assert ledger.state["kind"] == "training"
    assert ledger.state["training"] is True
    assert is_training_ledger(ledger) is True
    assert ledger.run_dir.resolve().is_relative_to(ws.resolve())
    assert not any(
        path.resolve().is_relative_to(real_tool.resolve())
        for path in ledger.run_dir.rglob("*")
        if path.is_file()
    )
    after_real = {p for p in real_tool.rglob("*") if p.is_file()}
    assert after_real == before_real
    assert not (ledger.run_dir / "run.lock").is_file()


def test_training_phases_order_matches_production() -> None:
    assert TRAINING_PHASES == RELEASE_PHASES
    assert TRAINING_PHASES == ("verify", "publish", "deploy", "tag_bitbucket", "tag_github")
