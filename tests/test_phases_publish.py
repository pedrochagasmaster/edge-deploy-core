"""Publish phase: verify gate, audit relocation, idempotent skip."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy import cli
from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import RunLedger
from edge_deploy.phases.publish import PUBLISH_SPEC, run_publish_phase
from edge_deploy.publish import PublishResult

OPERATOR_CONFIG_WITH_AUDIT = """\
operator_email: operator@example.com
audit_repo: {audit_repo}
nodes:
  node03:
    host: "user@edge.example"
    ssh_options: "-p 2222"
tools:
  autobench: {autobench_path}
"""

SNAPSHOT_SHA = "5" * 40
SOURCE_SHA = "a" * 40
PREV_REMOTE = "b" * 40


def _write_tool_profile(tmp_path: Path) -> Path:
    repo = tmp_path / "autobench"
    repo.mkdir()
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
    return repo


def _write_operator_config(tmp_path: Path, repo_root: Path) -> Path:
    audit_repo = tmp_path / "audit-core"
    audit_repo.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        OPERATOR_CONFIG_WITH_AUDIT.format(
            audit_repo=audit_repo.as_posix(),
            autobench_path=repo_root.as_posix(),
        ),
        encoding="utf-8",
    )
    return config_path


def _create_ledger(
    repo_root: Path,
    *,
    verify_state: str = "pending",
    publish_state: str = "pending",
    publish_evidence: dict | None = None,
) -> RunLedger:
    runs_root = repo_root / "edge-deploy" / "runs"
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha=SOURCE_SHA,
        nodes=["node03"],
        operator="operator@example.com",
    )
    if verify_state != "pending":
        ledger.set_phase("verify", verify_state, evidence={"commit": SOURCE_SHA})
    if publish_state != "pending":
        ledger.set_phase(
            "publish",
            publish_state,
            evidence=publish_evidence
            or {
                "snapshot_sha": SNAPSHOT_SHA,
                "source_commit": SOURCE_SHA,
                "previous_remote_commit": PREV_REMOTE,
            },
        )
    return ledger


def _operator(config_path: Path) -> OperatorConfig:
    return OperatorConfig.load(config_path)


def _patch_posture_and_engine(monkeypatch: pytest.MonkeyPatch, ledger: RunLedger) -> None:
    current = ledger.state["engine"]["content_sha256"]
    monkeypatch.setattr(
        "edge_deploy.phases.engine_identity",
        lambda: {"content_sha256": current, "version": "1.0.0", "package_dir": "/pkg"},
    )

    def reachable_connect(address: tuple[str, int], timeout: float) -> object:
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("edge_deploy.phases.socket.create_connection", reachable_connect)


def _noop_audit(*args, **kwargs) -> None:
    return None


def _fake_publish_result(profile, **kwargs) -> PublishResult:
    return PublishResult(
        tool=profile.tool,
        status="published",
        snapshot=SNAPSHOT_SHA,
        source_commit=SOURCE_SHA,
        source_short=SOURCE_SHA[:7],
        branch="main",
        previous_remote_commit=PREV_REMOTE,
        message="published",
        gate={"clean_tree": True, "on_release_branch": True, "local_check": True},
    )


def test_publish_refused_without_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(repo_root, verify_state="pending")
    operator = _operator(config_path)
    _patch_posture_and_engine(monkeypatch, ledger)
    monkeypatch.setattr("edge_deploy.phases.publish.check_audit_remote", _noop_audit)

    code = run_publish_phase(ledger, operator, repo_root, no_local_check=True)

    assert code == 2
    assert (
        f"publish refused: verify has not passed for run {ledger.state['run_id']}"
        in capsys.readouterr().err
    )


def test_publish_idempotent_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(
        repo_root,
        verify_state="passed",
        publish_state="passed",
        publish_evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
    )
    operator = _operator(config_path)
    _patch_posture_and_engine(monkeypatch, ledger)
    monkeypatch.setattr("edge_deploy.phases.publish.check_audit_remote", _noop_audit)
    publish_calls: list[str] = []

    def fake_publish(profile, **kwargs) -> PublishResult:
        publish_calls.append(profile.tool)
        return _fake_publish_result(profile, **kwargs)

    monkeypatch.setattr("edge_deploy.phases.publish.publish_snapshot", fake_publish)
    code = run_publish_phase(
        ledger,
        operator,
        repo_root,
        no_local_check=True,
        remote_head_fn=lambda repo_root, remote, branch: SNAPSHOT_SHA,
    )

    assert code == 0
    assert publish_calls == []
    assert f"publish: already published {SNAPSHOT_SHA[:7]} (skipping)" in capsys.readouterr().out
    events = [
        json.loads(line)
        for line in (ledger.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "phase_skipped" and event["phase"] == "publish" for event in events)


def test_publish_records_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(repo_root, verify_state="passed")
    operator = _operator(config_path)
    _patch_posture_and_engine(monkeypatch, ledger)
    monkeypatch.setattr("edge_deploy.phases.publish.check_audit_remote", _noop_audit)
    code = run_publish_phase(
        ledger,
        operator,
        repo_root,
        no_local_check=True,
        publish_fn=_fake_publish_result,
        remote_head_fn=lambda repo_root, remote, branch: "c" * 40,
    )

    assert code == 0
    evidence = ledger.state["phases"]["publish"]["evidence"]
    assert evidence["snapshot_sha"] == SNAPSHOT_SHA
    assert evidence["source_commit"] == SOURCE_SHA
    assert evidence["previous_remote_commit"] == PREV_REMOTE
    assert (ledger.run_dir / "publish-autobench.json").is_file()


def test_check_audit_remote_runs_in_publish_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audit-remote gate needs Bitbucket, so it belongs to the publish
    # phase; the old pre-publish preflight (which also pre-authenticated
    # nodes) was deleted outright rather than relocated.
    assert not hasattr(cli, "_run_release_preflight")

    repo_root = _write_tool_profile(tmp_path)
    config_path = _write_operator_config(tmp_path, repo_root)
    ledger = _create_ledger(repo_root, verify_state="passed")
    operator = _operator(config_path)
    audit_calls: list[tuple] = []

    def track_audit(*args, **kwargs) -> None:
        audit_calls.append((args, kwargs))

    monkeypatch.setattr("edge_deploy.phases.publish.check_audit_remote", track_audit)

    _patch_posture_and_engine(monkeypatch, ledger)
    run_publish_phase(
        ledger,
        operator,
        repo_root,
        no_local_check=True,
        publish_fn=_fake_publish_result,
        remote_head_fn=lambda repo_root, remote, branch: "c" * 40,
    )

    assert len(audit_calls) == 1
    assert audit_calls[0][1]["tool"] == "autobench"
    assert audit_calls[0][1]["source_sha"] == SOURCE_SHA


def test_publish_phase_registered_in_cli_help() -> None:
    help_text = cli.build_parser().format_help()
    assert "publish-phase" in help_text


def test_publish_phase_parser_args() -> None:
    args = cli.build_parser().parse_args(
        ["publish-phase", "--run", "run-20260703T120000Z-aa6d9a5", "--no-local-check", "--force-lock"]
    )
    assert args.command == "publish-phase"
    assert args.run == "run-20260703T120000Z-aa6d9a5"
    assert args.no_local_check is True
    assert args.force_lock is True


def test_publish_spec_order_and_endpoints() -> None:
    assert PUBLISH_SPEC.name == "publish"
    assert PUBLISH_SPEC.order == 20
    assert PUBLISH_SPEC.endpoints == ("bitbucket",)
