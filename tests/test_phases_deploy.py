"""Deploy phase: per-node resumable rollout and ledger state mapping."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy import auth, drift, release
from edge_deploy.config import NodeConfig, OperatorConfig
from edge_deploy.ledger import RunLedger
from edge_deploy.phases.deploy import run_deploy

PREV = "0" * 40
SNAP = "5" * 40
SOURCE = "src1234567890123456789012345678901234abcd"

PROJECTS_ROOT = Path(__file__).resolve().parents[2]


def _write_tool_profile(tmp_path: Path, tool: str = "autobench") -> Path:
    repo = tmp_path / tool
    repo.mkdir(exist_ok=True)
    remote_name = "autobench" if tool == "autobench" else "dispatch"
    remote_path = "autobench" if tool == "autobench" else "dispatch"
    (repo / "edge_deploy.yaml").write_text(
        f"""\
tool: {tool}
repo_path: /ads_storage/{remote_path}
github_url: https://github.com/pedrochagasmaster/{tool}.git
bitbucket_url: https://scm.example/{remote_name}.git
release_branch: main
runtime_paths: ["src/**/*.py"]
compile_targets: src
version_files: [VERSION]
install_trigger_paths: [requirements.txt]
dependency_paths: [requirements.txt]
smoke:
  standard: ["echo ok"]
  deep: []
""",
        encoding="utf-8",
    )
    return repo


def _operator(repo_root: Path) -> OperatorConfig:
    return OperatorConfig(
        operator_email="op@mastercard.com",
        nodes={
            "node03": NodeConfig(host="u@h3", session="s3", name="node03"),
            "node04": NodeConfig(host="u@h4", session="s4", name="node04"),
        },
        tools={"autobench": str(repo_root)},
    )


def _create_run(
    repo_root: Path,
    *,
    publish_passed: bool = False,
    deploy_states: dict[str, str] | None = None,
) -> RunLedger:
    ledger = RunLedger.create(
        repo_root / "edge-deploy" / "runs",
        tool="autobench",
        source_sha="f" * 40,
        nodes=["node03", "node04"],
        operator="operator@example.com",
    )
    if publish_passed:
        ledger.set_phase(
            "publish",
            "passed",
            evidence={"snapshot_sha": SNAP, "source_commit": SOURCE},
        )
        ledger = RunLedger.load(ledger.run_dir)
    for node, state in (deploy_states or {}).items():
        ledger.set_phase("deploy", state, node=node, evidence={"node": node, "status": state})
        ledger = RunLedger.load(ledger.run_dir)
    return ledger


def _write_publish_report(run_dir: Path) -> None:
    (run_dir / "publish-autobench.json").write_text(
        json.dumps(
            {
                "tool": "autobench",
                "status": "published",
                "deployment_commit": SNAP,
                "source_commit": SOURCE,
                "source_short": "src1234",
                "branch": "main",
                "previous_remote_commit": "prev1234",
                "message": "Deploy snapshot: autobench",
            }
        ),
        encoding="utf-8",
    )


def _make_factory(fake_tmux, drivers: dict):
    def factory(node, profile, **kwargs):
        driver = fake_tmux(
            head_commits=[PREV, SNAP],
            changed_paths=["benchmark.py"],
            remote_runtime={"a.py": "1"},
            auth_script=["accept"],
        )
        drivers[node.name] = driver
        return driver

    return factory


@pytest.fixture(autouse=True)
def _default_prompt_auth(monkeypatch):
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: "12345678")


def _deploy_args(run_id: str, **kwargs) -> SimpleNamespace:
    defaults = {
        "run": run_id,
        "nodes": None,
        "smoke": "standard",
        "auth_mode": "prompt",
        "auth_wait_seconds": 300.0,
        "force_lock": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patched_deploy_env(monkeypatch):
    monkeypatch.setattr("edge_deploy.phases.require_posture", lambda *a, **k: None)
    monkeypatch.setattr(release, "ensure_snapshot_available", lambda *a, **k: True)
    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: {"a.py": "1"})


def test_deploy_refused_without_publish(tmp_path, patched_deploy_env, capsys) -> None:
    repo_root = _write_tool_profile(tmp_path)
    ledger = _create_run(repo_root, publish_passed=False)
    operator = _operator(repo_root)

    code = run_deploy(_deploy_args(ledger.state["run_id"]), operator)

    assert code == 2
    assert "deploy refused: publish has not passed for run" in capsys.readouterr().err


def test_deploy_skips_when_all_nodes_passed(tmp_path, patched_deploy_env, monkeypatch, capsys) -> None:
    repo_root = _write_tool_profile(tmp_path)
    ledger = _create_run(
        repo_root,
        publish_passed=True,
        deploy_states={"node03": "passed", "node04": "passed"},
    )
    operator = _operator(repo_root)

    def fail_run_release(*args, **kwargs):
        raise AssertionError("run_release must not be called when all nodes passed")

    monkeypatch.setattr("edge_deploy.phases.deploy.run_release", fail_run_release)

    code = run_deploy(_deploy_args(ledger.state["run_id"]), operator)

    assert code == 0
    assert capsys.readouterr().out.strip() == "deploy: all nodes already rolled out (skipping)"


def test_deploy_excludes_already_passed_nodes(
    tmp_path, fake_tmux, patched_deploy_env, monkeypatch
) -> None:
    repo_root = _write_tool_profile(tmp_path)
    ledger = _create_run(
        repo_root,
        publish_passed=True,
        deploy_states={"node03": "passed"},
    )
    _write_publish_report(ledger.run_dir)
    operator = _operator(repo_root)
    drivers: dict = {}
    captured: list = []

    def capture_run_release(op, selection, **kwargs):
        captured.append(selection)
        return release.run_release(
            op,
            selection,
            driver_factory=_make_factory(fake_tmux, drivers),
            heartbeat_interval_s=3600.0,
            stall_threshold_s=7200.0,
            **kwargs,
        )

    monkeypatch.setattr("edge_deploy.phases.deploy.run_release", capture_run_release)

    code = run_deploy(_deploy_args(ledger.state["run_id"]), operator)

    assert code == 0
    assert len(captured) == 1
    assert captured[0].nodes == ["node04"]


def test_deploy_writes_ledger_states_from_report(
    tmp_path, fake_tmux, patched_deploy_env, monkeypatch
) -> None:
    repo_root = _write_tool_profile(tmp_path)
    ledger = _create_run(repo_root, publish_passed=True)
    _write_publish_report(ledger.run_dir)
    operator = _operator(repo_root)
    drivers: dict = {}

    def configure_factory(node, profile, **kwargs):
        kw = dict(
            head_commits=[PREV, SNAP],
            changed_paths=["benchmark.py"],
            remote_runtime={"a.py": "1"},
            auth_script=["accept"],
        )
        if node.name == "node04":
            kw["update_code"] = 1
        driver = fake_tmux(**kw)
        drivers[node.name] = driver
        return driver

    monkeypatch.setattr(
        "edge_deploy.phases.deploy.run_release",
        lambda op, selection, **kwargs: release.run_release(
            op,
            selection,
            driver_factory=configure_factory,
            heartbeat_interval_s=3600.0,
            stall_threshold_s=7200.0,
            **kwargs,
        ),
    )

    code = run_deploy(_deploy_args(ledger.state["run_id"], nodes="node03,node04"), operator)

    assert code == 1
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("deploy", node="node03") == "passed"
    assert reloaded.phase_state("deploy", node="node04") == "failed"


def test_deploy_happy_path_marks_all_nodes_passed(
    tmp_path, fake_tmux, patched_deploy_env, monkeypatch
) -> None:
    repo_root = _write_tool_profile(tmp_path)
    ledger = _create_run(repo_root, publish_passed=True)
    _write_publish_report(ledger.run_dir)
    operator = _operator(repo_root)
    drivers: dict = {}

    monkeypatch.setattr(
        "edge_deploy.phases.deploy.run_release",
        lambda op, selection, **kwargs: release.run_release(
            op,
            selection,
            driver_factory=_make_factory(fake_tmux, drivers),
            heartbeat_interval_s=3600.0,
            stall_threshold_s=7200.0,
            **kwargs,
        ),
    )

    code = run_deploy(_deploy_args(ledger.state["run_id"]), operator)

    assert code == 0
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("deploy", node="node03") == "passed"
    assert reloaded.phase_state("deploy", node="node04") == "passed"
