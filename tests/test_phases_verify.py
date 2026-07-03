"""Verify phase: skip path, reverify, and failure recording."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from edge_deploy import cli
from edge_deploy.ledger import RunLedger


def _reachable_connect(address: tuple[str, int], timeout: float) -> object:
    return SimpleNamespace(close=lambda: None)


def _write_operator_config(tmp_path, repo_root: str) -> str:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""\
operator_email: operator@example.com
nodes:
  node03:
    host: "user@edge.example"
    ssh_options: "-p 2222"
tools:
  autobench: {repo_root}
""",
        encoding="utf-8",
    )
    return str(config_path)


def _write_tool_profile(tmp_path) -> str:
    repo = tmp_path / "autobench"
    repo.mkdir(exist_ok=True)
    (repo / "edge_deploy.yaml").write_text(
        """\
tool: autobench
repo_path: /ads_storage/autobench
github_url: https://github.com/pedrochagasmaster/autobench.git
bitbucket_url: https://scm.example/autobench.git
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
    return str(repo)


def _create_ledger(tmp_path, *, source_sha: str, verify_state: str = "pending") -> RunLedger:
    repo_root = tmp_path / "autobench"
    repo_root.mkdir(exist_ok=True)
    runs_root = repo_root / "edge-deploy" / "runs"
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha=source_sha,
        nodes=["node03"],
        operator="operator@example.com",
    )
    if verify_state != "pending":
        ledger.set_phase(
            "verify",
            verify_state,
            evidence={
                "commit": source_sha,
                "ci": "success",
                "tests": "passed",
                "verified_at": "2026-07-03T12:00:00+00:00",
            },
        )
    return ledger


def _events(ledger: RunLedger) -> list[dict]:
    path = ledger.run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _pytest_calls(subprocess_calls: list) -> list:
    return [
        call
        for call in subprocess_calls
        if call.args and any(arg == "pytest" for arg in call.args)
    ]


def test_verify_skip_path_honors_evidence_without_pytest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "a" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit, verify_state="passed")
    run_id = ledger.state["run_id"]

    subprocess_calls: list = []

    def track_subprocess(command, **kwargs):
        subprocess_calls.append(SimpleNamespace(args=command, kwargs=kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr("edge_deploy.phases.verify.subprocess.run", track_subprocess)

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 0
    assert _pytest_calls(subprocess_calls) == []
    assert ledger.phase_state("verify") == "passed"
    assert any(event["event"] == "phase_skipped" and event["phase"] == "verify" for event in _events(ledger))


def test_verify_reverify_forces_pytest_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = "b" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit, verify_state="passed")
    run_id = ledger.state["run_id"]

    subprocess_calls: list = []

    def track_subprocess(command, **kwargs):
        subprocess_calls.append(SimpleNamespace(args=command, kwargs=kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr("edge_deploy.phases.verify.subprocess.run", track_subprocess)

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id, "--reverify"])

    assert exit_code == 0
    assert len(_pytest_calls(subprocess_calls)) == 1
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "passed"
    evidence = reloaded.state["phases"]["verify"]["evidence"]
    assert evidence["commit"] == commit
    assert evidence["ci"] == "success"
    assert evidence["tests"] == "passed"
    assert "verified_at" in evidence
    assert "verify: already passed" not in capsys.readouterr().out


def test_verify_failure_records_failed_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "c" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit)
    run_id = ledger.state["run_id"]

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1),
    )

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 1
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "failed"
    assert reloaded.state["phases"]["verify"]["evidence"]["commit"] == commit
