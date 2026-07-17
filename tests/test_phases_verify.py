"""Verify phase: skip path, reverify, and failure recording."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from edge_deploy import cli
from edge_deploy.ledger import RunLedger
from edge_deploy.local_check import LocalCheckResult, LocalCheckUnavailableError


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
                "verification_command": "tools/dev/local_check.ps1",
            },
        )
    return ledger


def _events(ledger: RunLedger) -> list[dict]:
    path = ledger.run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_verify_skip_path_honors_complete_tool_evidence(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "a" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit, verify_state="passed")
    run_id = ledger.state["run_id"]

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.run_local_check",
        lambda root: pytest.fail("complete source-bound evidence must skip the gate"),
    )

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 0
    assert ledger.phase_state("verify") == "passed"
    assert any(event["event"] == "phase_skipped" and event["phase"] == "verify" for event in _events(ledger))


def test_verify_reverify_forces_tool_gate_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = "b" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit, verify_state="passed")
    run_id = ledger.state["run_id"]

    calls: list = []

    def track_gate(root):
        calls.append(root)
        return LocalCheckResult(0, "Local check passed.")

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr("edge_deploy.phases.verify.run_local_check", track_gate)

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id, "--reverify"])

    assert exit_code == 0
    assert calls == [tmp_path / "autobench"]
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "passed"
    evidence = reloaded.state["phases"]["verify"]["evidence"]
    assert evidence["commit"] == commit
    assert evidence["ci"] == "success"
    assert evidence["tests"] == "passed"
    assert "verified_at" in evidence
    assert evidence["verification_command"] == "tools/dev/local_check.ps1"
    assert "verify: already passed" not in capsys.readouterr().out


def test_verify_incomplete_legacy_evidence_runs_tool_gate(tmp_path, monkeypatch) -> None:
    commit = "b" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit, verify_state="passed")
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": commit,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    calls: list = []

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.run_local_check",
        lambda root: (calls.append(root) or LocalCheckResult(0, "ok")),
    )

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 0
    assert calls == [tmp_path / "autobench"]


def test_verify_failure_records_redacted_diagnostic(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        "edge_deploy.phases.verify.run_local_check",
        lambda root: LocalCheckResult(7, "token=super-secret\nfinal failure"),
    )

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 1
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "failed"
    evidence = reloaded.state["phases"]["verify"]["evidence"]
    assert evidence == {
        "commit": commit,
        "error_type": "LocalCheckError",
        "exit_code": 7,
        "diagnostic_artifact": "verify-local-check.log",
    }
    diagnostic = (ledger.run_dir / "verify-local-check.log").read_text(encoding="utf-8")
    assert "super-secret" not in diagnostic
    assert "final failure" in diagnostic
    stderr = capsys.readouterr().err
    assert "super-secret" not in stderr
    assert "final failure" in stderr


@pytest.mark.parametrize("unavailable_reason", ["missing script", "missing PowerShell"])
def test_verify_unavailable_gate_fails_closed(tmp_path, monkeypatch, unavailable_reason) -> None:
    commit = "9" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=commit)
    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.run_local_check",
        lambda root: (_ for _ in ()).throw(LocalCheckUnavailableError(unavailable_reason)),
    )

    assert cli.main(["--config", config_path, "verify", "--run", ledger.state["run_id"]]) == 1
    evidence = RunLedger.load(ledger.run_dir).state["phases"]["verify"]["evidence"]
    assert evidence["error_type"] == "LocalCheckUnavailableError"
    assert evidence["exit_code"] is None
    assert evidence["diagnostic_artifact"] == "verify-local-check.log"


def test_verify_checkout_drift_rejects_mismatched_commit(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bound_commit = "d" * 40
    checkout_commit = "e" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(tmp_path, source_sha=bound_commit)
    run_id = ledger.state["run_id"]

    monkeypatch.chdir(tmp_path / "autobench")
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=checkout_commit),
    )

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "checkout drift" in err
    assert bound_commit[:7] in err
    assert checkout_commit[:7] in err
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "failed"


def test_verify_rollback_skips_ci_and_tool_gate(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = "f" * 40
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    repo_root_path = tmp_path / "autobench"
    runs_root = repo_root_path / "edge-deploy" / "runs"
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha=commit,
        nodes=["node03"],
        operator="operator@example.com",
        kind="rollback",
        rollback_tag="release-20260703T120000Z-fffffff",
    )
    run_id = ledger.state["run_id"]
    monkeypatch.chdir(repo_root_path)
    monkeypatch.setattr("edge_deploy.posture.socket.create_connection", _reachable_connect)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr("edge_deploy.phases.verify.require_successful_github_ci", lambda state: None)
    monkeypatch.setattr(
        "edge_deploy.phases.verify.run_local_check",
        lambda root: pytest.fail("rollback must skip the gate"),
    )

    exit_code = cli.main(["--config", config_path, "verify", "--run", run_id])

    assert exit_code == 0
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "skipped"
    evidence = reloaded.state["phases"]["verify"]["evidence"]
    assert evidence["rollback_tag"] == "release-20260703T120000Z-fffffff"
    assert "skipped for rollback run" in capsys.readouterr().out
    assert any(
        event["event"] == "phase_skipped" and event["phase"] == "verify"
        for event in _events(reloaded)
    )
