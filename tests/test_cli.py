"""Thin CLI surface: argparse wiring, config resolution, and command dispatch.

Network and tmux are faked, so ``rollout`` / ``drift`` / ``preflight`` run end to end with
no nodes. A subprocess smoke test exercises the real ``python -m edge_deploy`` entry point.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from edge_deploy import cli
from edge_deploy.ledger import RunLedger, RunLockError
from edge_deploy.mirror import MirrorError, MirrorResult
from edge_deploy.publish import PublishError, PublishResult

REPO_ROOT = Path(__file__).resolve().parents[1]

OPERATOR_CONFIG = """\
operator_email: operator@example.com
nodes:
  node03:
    host: "user@edge.example"
    ssh_options: "-p 2222"
tools:
  autobench: {autobench_path}
"""

OPERATOR_CONFIG_BOTH = """\
operator_email: operator@example.com
nodes:
  node03:
    host: "user@edge3.example"
    ssh_options: "-p 2222"
  node04:
    host: "user@edge4.example"
    ssh_options: "-p 2222"
tools:
  autobench: {autobench_path}
  robocop: {robocop_path}
"""


def _write_operator_config_both(tmp_path: Path) -> Path:
    autobench_path = _write_tool_profile(tmp_path, "autobench")
    robocop_path = _write_tool_profile(tmp_path, "robocop")
    config_path = tmp_path / "config-both.yaml"
    config_path.write_text(
        OPERATOR_CONFIG_BOTH.format(
            autobench_path=autobench_path.as_posix(),
            robocop_path=robocop_path.as_posix(),
        ),
        encoding="utf-8",
    )
    return config_path


def _ok_addrinfo(host: str, port: int, *, type: int):  # noqa: A002 - mirrors socket.getaddrinfo
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port))]


def _raise_timeout(address: tuple[str, int], timeout: float) -> object:
    raise TimeoutError("timed out")


SOURCE_SHA = "a" * 40
SNAPSHOT_SHA = "d" * 40
RELEASE_TAG = f"release-20260703T120000Z-{SOURCE_SHA[:7]}"


def _reachable_connect(address: tuple[str, int], timeout: float) -> object:
    return SimpleNamespace(close=lambda: None)


def _unreachable_bitbucket_connect(address: tuple[str, int], timeout: float) -> object:
    host, port = address
    if host == "scm.mastercard.int" and port == 443:
        raise TimeoutError("timed out")
    return SimpleNamespace(close=lambda: None)


def _fake_bitbucket_down_git_failures(bitbucket_reachable):
    """git_probe_failures double: bitbucket git endpoints fail until the
    ``bitbucket_reachable()`` callable flips true; everything else passes."""

    def fake(phase, repo_root, runner=None):
        probes = cli.PHASE_GIT_PROBES.get(phase, {})
        if "bitbucket" in probes and not bitbucket_reachable():
            return ["scm.mastercard.int:443"]
        return []

    return fake


def _patch_all_phases_pass(monkeypatch: pytest.MonkeyPatch, *, patch_probe: bool = True) -> None:
    def fake_ensure_verified(operator, profile, repo_root, ledger, **kwargs):
        ledger.set_phase(
            "verify",
            "passed",
            evidence={
                "commit": SOURCE_SHA,
                "ci": "success",
                "tests": "passed",
                "verified_at": "2026-07-03T12:00:00+00:00",
            },
        )
        return SimpleNamespace(commit=SOURCE_SHA)

    def fake_publish(ledger, operator, repo_root, **kwargs):
        ledger.set_phase(
            "publish",
            "passed",
            evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
        )
        return 0

    def fake_deploy(args, operator):
        from edge_deploy.phases.deploy import _load_run

        ledger, _repo_root = _load_run(args, operator)
        for node in ledger.state["nodes"]:
            ledger.set_phase("deploy", "passed", node=node, evidence={"status": "rolled_out"})
        return 0

    def fake_tag_bitbucket(args, operator):
        from edge_deploy.phases.deploy import _load_run

        ledger, _repo_root = _load_run(args, operator)
        ledger.set_phase("tag_bitbucket", "passed", evidence={"tag": RELEASE_TAG, "pushed_sha": SNAPSHOT_SHA})
        return 0

    def fake_tag_github(args, operator):
        from edge_deploy.phases.deploy import _load_run

        ledger, _repo_root = _load_run(args, operator)
        ledger.set_phase("tag_github", "passed", evidence={"tag": RELEASE_TAG, "pushed_sha": SOURCE_SHA})
        ledger.complete()
        return 0

    monkeypatch.setattr(cli, "ensure_verified", fake_ensure_verified)
    monkeypatch.setattr(cli, "run_publish_phase", fake_publish)
    monkeypatch.setattr(cli, "run_deploy", fake_deploy)
    monkeypatch.setattr(cli, "_cmd_tag_github", fake_tag_github)
    monkeypatch.setattr(cli, "_cmd_tag_bitbucket", fake_tag_bitbucket)
    if patch_probe:
        monkeypatch.setattr(cli, "probe", lambda *a, **k: [])
        monkeypatch.setattr(cli, "git_probe_failures", lambda *a, **k: [])
    monkeypatch.setattr(cli, "enter_phase", lambda *a, **k: __import__("contextlib").ExitStack())


def _write_operator_config(tmp_path: Path) -> Path:
    autobench_path = _write_tool_profile(tmp_path, "autobench")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        OPERATOR_CONFIG.format(autobench_path=autobench_path.as_posix()),
        encoding="utf-8",
    )
    return config_path


def _patch_inspect_repository(monkeypatch, commit: str = "a" * 40) -> None:
    monkeypatch.setattr(
        cli,
        "inspect_repository",
        lambda *a, **k: SimpleNamespace(commit=commit),
    )


def _autobench_repo_root(tmp_path: Path) -> Path:
    return _write_tool_profile(tmp_path, "autobench")


def _create_open_run(repo_root: Path, *, run_id: str | None = None) -> RunLedger:
    runs_root = repo_root / "edge-deploy" / "runs"
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="f" * 40,
        nodes=["node03", "node04"],
        operator="operator@example.com",
    )
    if run_id is not None:
        desired = runs_root / run_id
        if ledger.run_dir != desired:
            ledger.run_dir.rename(desired)
            ledger = RunLedger.load(desired)
    return ledger


def _write_tool_profile(tmp_path: Path, tool: str) -> Path:
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


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_parses_rollout_args() -> None:
    args = cli.build_parser().parse_args(
        ["rollout", "--tool", "autobench", "--node", "node03", "--commit", "abc123"]
    )

    assert args.command == "rollout"
    assert args.tool == "autobench"
    assert args.node == "node03"
    assert args.commit == "abc123"
    assert args.install == "auto"


def test_parser_parses_drift_and_preflight() -> None:
    drift_args = cli.build_parser().parse_args(["drift", "--tool", "t", "--node", "n", "--commit", "c"])
    preflight_args = cli.build_parser().parse_args(["preflight", "--node", "n"])

    assert drift_args.command == "drift"
    assert preflight_args.command == "preflight"


def test_parser_help_lists_all_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "release" in help_text
    assert "abandon" in help_text
    assert "publish" in help_text
    assert "rollout" in help_text
    assert "drift" in help_text
    assert "preflight" in help_text


def test_parser_parses_release_args() -> None:
    args = cli.build_parser().parse_args(
        ["release", "--nodes", "03,04", "--auth-mode", "prompt",
         "--smoke", "deep", "--fail-fast", "--no-local-check", "--max-auth-attempts", "5"]
    )

    assert args.command == "release"
    assert args.tool is None
    assert args.nodes == "03,04"
    assert args.auth_mode == "prompt"
    assert args.smoke == "deep"
    assert args.fail_fast is True
    assert args.no_local_check is True
    assert args.max_auth_attempts == 5


def test_parser_release_defaults() -> None:
    args = cli.build_parser().parse_args(["release"])

    assert args.tool is None
    assert args.nodes is None
    assert args.auth_mode == "prompt"
    assert args.smoke == "standard"
    assert args.fail_fast is False
    assert args.no_local_check is False
    assert args.max_auth_attempts == 3
    assert args.heartbeat_interval == 30.0
    assert args.stall_threshold == 300.0


def test_parser_parses_heartbeat_and_stall_threshold() -> None:
    args = cli.build_parser().parse_args(
        ["release", "--tool", "autobench", "--heartbeat-interval", "15", "--stall-threshold", "120"]
    )

    assert args.heartbeat_interval == 15.0
    assert args.stall_threshold == 120.0


def test_parser_accepts_prompt_and_pane_auth_modes() -> None:
    for mode in ("prompt", "pane"):
        args = cli.build_parser().parse_args(["release", "--tool", "autobench", "--auth-mode", mode])
        assert args.auth_mode == mode


def test_parser_parses_explicit_tagged_rollback() -> None:
    args = cli.build_parser().parse_args(
        ["rollback", "--tag", "release-20260701T143000Z-a1b2c3d", "--nodes", "03"]
    )
    assert args.command == "rollback"
    assert args.tag == "release-20260701T143000Z-a1b2c3d"
    assert args.nodes == "03"


def test_parser_parses_publish_args() -> None:
    args = cli.build_parser().parse_args(
        ["publish", "--tool", "robocop", "--commit", "deadbeef", "--no-local-check", "--remote", "origin"]
    )

    assert args.command == "publish"
    assert args.tool == "robocop"
    assert args.commit == "deadbeef"
    assert args.no_local_check is True
    assert args.remote == "origin"


def test_parser_publish_rejects_both() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["publish", "--tool", "both"])


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


# ---------------------------------------------------------------------------
# main() error handling
# ---------------------------------------------------------------------------


def test_main_missing_config_returns_2(tmp_path, capsys) -> None:
    rc = cli.main(["--config", str(tmp_path / "nope.yaml"), "preflight", "--node", "node03"])

    assert rc == 2
    assert "Operator config not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# preflight command (no real network)
# ---------------------------------------------------------------------------


def test_preflight_command_reports_failure(tmp_path, capsys, monkeypatch) -> None:
    config_path = _write_operator_config(tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)
    monkeypatch.setattr(socket, "create_connection", _raise_timeout)

    rc = cli.main(["--config", str(config_path), "preflight", "--node", "node03", "--timeout", "3"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "Endpoint: edge.example:2222" in out
    assert "Resolved addresses: 10.0.0.5" in out


def test_preflight_command_writes_json_report(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config(tmp_path)
    report_path = tmp_path / "reports" / "preflight.json"
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)
    monkeypatch.setattr(socket, "create_connection", _raise_timeout)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "preflight",
            "--node",
            "node03",
            "--timeout",
            "3",
            "--json-report",
            str(report_path),
        ]
    )

    assert rc == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["operation"] == "preflight"
    assert report["status"] == "blocked"
    # The node name comes from the operator-config key (NodeConfig.name), not the host.
    assert report["node"] == "node03"
    assert report["endpoint"] == "edge.example:2222"
    assert report["connected"] is False


# ---------------------------------------------------------------------------
# rollout command end to end (fake authenticated pane)
# ---------------------------------------------------------------------------


def _patch_driver_factory(monkeypatch, fake) -> None:
    monkeypatch.setattr(
        cli, "TmuxDriver", SimpleNamespace(from_node_and_profile=lambda node, profile, **kwargs: fake)
    )


def test_rollout_command_rolls_out_with_fake_pane(tmp_path, fake_tmux, monkeypatch) -> None:
    config_path = _write_operator_config(tmp_path)
    commit = "d" * 40
    fake = fake_tmux(head_commits=["0" * 40, commit], changed_paths=["benchmark.py"])
    _patch_driver_factory(monkeypatch, fake)

    rc = cli.main(
        ["--config", str(config_path), "rollout", "--tool", "autobench", "--node", "node03", "--commit", commit]
    )

    assert rc == 0
    assert any(step == "update" for _, _, step in fake.runner_step_commands)


def test_rollout_command_refused_returns_1(tmp_path, fake_tmux, monkeypatch) -> None:
    config_path = _write_operator_config(tmp_path)
    fake = fake_tmux(head_commits=["0" * 40], changed_paths=["requirements.txt"])
    _patch_driver_factory(monkeypatch, fake)

    rc = cli.main(
        ["--config", str(config_path), "rollout", "--tool", "autobench", "--node", "node03", "--commit", "d" * 40]
    )

    assert rc == 1
    assert not any(step == "update" for _, _, step in fake.runner_step_commands)


# ---------------------------------------------------------------------------
# release command dispatch (phase chain)
# ---------------------------------------------------------------------------


def test_release_chain_completes_all_phases(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)
    _patch_all_phases_pass(monkeypatch)

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--nodes", "03,04"]
    )

    assert rc == 0
    out = capsys.readouterr().out
    runs = list((autobench_path / "edge-deploy" / "runs").iterdir())
    assert len(runs) == 1
    loaded = RunLedger.load(runs[0])
    assert loaded.state["status"] == "complete"
    assert loaded.phase_state("verify") == "passed"
    assert loaded.phase_state("publish") == "passed"
    assert loaded.phase_state("deploy", node="node03") == "passed"
    assert loaded.phase_state("deploy", node="node04") == "passed"
    assert loaded.phase_state("tag_github") == "passed"
    assert loaded.phase_state("tag_bitbucket") == "passed"
    assert f"release complete: {loaded.state['run_id']}" in out


def test_release_chain_holds_lock_between_phases(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)
    monkeypatch.setattr(cli.socket, "create_connection", _reachable_connect)

    phases_checked: list[str] = []

    def _assert_locked_against_other_processes(run_dir) -> None:
        # A ledger loaded in the *same* process may borrow the held lock, so a
        # foreign process is simulated by reporting a different PID.
        foreign = RunLedger.load(run_dir)
        with patch("edge_deploy.ledger.os.getpid", return_value=os.getpid() + 1):
            with pytest.raises(RunLockError):
                foreign.acquire_lock()

    def fake_ensure_verified(operator, profile, repo_root, ledger, **kwargs):
        _assert_locked_against_other_processes(ledger.run_dir)
        phases_checked.append("verify")
        ledger.set_phase(
            "verify",
            "passed",
            evidence={
                "commit": SOURCE_SHA,
                "ci": "success",
                "tests": "passed",
                "verified_at": "2026-07-03T12:00:00+00:00",
            },
        )
        return SimpleNamespace(commit=SOURCE_SHA)

    def fake_publish(ledger, operator, repo_root, **kwargs):
        _assert_locked_against_other_processes(ledger.run_dir)
        phases_checked.append("publish")
        ledger.set_phase(
            "publish",
            "passed",
            evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
        )
        return 0

    def fake_deploy(args, operator):
        from edge_deploy.phases.deploy import _load_run

        ledger, _repo_root = _load_run(args, operator)
        _assert_locked_against_other_processes(ledger.run_dir)
        # Regression: the chained phase itself reloads the ledger and must be able
        # to borrow the outer lock instead of self-deadlocking on its own PID.
        ledger.acquire_lock()
        ledger.release_lock()
        assert (ledger.run_dir / "run.lock").is_file()
        phases_checked.append("deploy")
        for node in ledger.state["nodes"]:
            ledger.set_phase("deploy", "passed", node=node, evidence={"status": "rolled_out"})
        return 0

    def fake_tag_bitbucket(args, operator):
        from edge_deploy.phases.deploy import _load_run

        ledger, _repo_root = _load_run(args, operator)
        _assert_locked_against_other_processes(ledger.run_dir)
        phases_checked.append("tag_bitbucket")
        ledger.set_phase("tag_bitbucket", "passed", evidence={"tag": RELEASE_TAG, "pushed_sha": SNAPSHOT_SHA})
        return 0

    def fake_tag_github(args, operator):
        from edge_deploy.phases.deploy import _load_run

        ledger, _repo_root = _load_run(args, operator)
        _assert_locked_against_other_processes(ledger.run_dir)
        phases_checked.append("tag_github")
        ledger.set_phase("tag_github", "passed", evidence={"tag": RELEASE_TAG, "pushed_sha": SOURCE_SHA})
        ledger.complete()
        return 0

    monkeypatch.setattr(cli, "ensure_verified", fake_ensure_verified)
    monkeypatch.setattr(cli, "run_publish_phase", fake_publish)
    monkeypatch.setattr(cli, "run_deploy", fake_deploy)
    monkeypatch.setattr(cli, "_cmd_tag_github", fake_tag_github)
    monkeypatch.setattr(cli, "_cmd_tag_bitbucket", fake_tag_bitbucket)
    monkeypatch.setattr(cli, "probe", lambda *a, **k: [])
    monkeypatch.setattr(cli, "git_probe_failures", lambda *a, **k: [])

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--nodes", "03,04"]
    )

    assert rc == 0
    assert phases_checked == ["verify", "publish", "deploy", "tag_bitbucket", "tag_github"]
    runs = list((autobench_path / "edge-deploy" / "runs").iterdir())
    assert not (runs[0] / "run.lock").is_file()


def test_release_posture_failure_mid_chain_exits_zero(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path, run_id=None)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": SOURCE_SHA,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)
    monkeypatch.setattr(cli.socket, "create_connection", _unreachable_bitbucket_connect)
    monkeypatch.setattr(cli, "git_probe_failures", _fake_bitbucket_down_git_failures(lambda: False))

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--run", run_id]
    )

    assert rc == 0
    out = capsys.readouterr().out
    expected = (
        f"phase 'publish' requires posture [bitbucket]; unreachable: scm.mastercard.int:443.\n"
        f"Switch the firewall posture, then re-run: python -m edge_deploy release --run {run_id}\n"
    )
    assert out == expected
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("verify") == "passed"
    assert reloaded.phase_state("publish") == "pending"


def test_release_resumes_without_rerunning_verify(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": SOURCE_SHA,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)
    _patch_all_phases_pass(monkeypatch)

    def fail_verify(*args, **kwargs):
        raise AssertionError("verify must not re-run when already passed")

    monkeypatch.setattr(cli, "ensure_verified", fail_verify)
    pytest_runs: list = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: pytest_runs.append(a) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(cli, "probe", lambda *a, **k: [])

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--run", run_id]
    )

    assert rc == 0
    assert pytest_runs == []
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "complete"


def test_release_resume_with_verify_passed_skips_inspect_repository(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": SOURCE_SHA,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    inspect_calls: list = []

    def tracking_inspect(*args, **kwargs):
        inspect_calls.append(1)
        return SimpleNamespace(commit=SOURCE_SHA)

    monkeypatch.setattr(cli, "inspect_repository", tracking_inspect)
    _patch_all_phases_pass(monkeypatch)

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--run", run_id]
    )

    assert rc == 0
    assert inspect_calls == []
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "complete"


def test_release_guided_crosses_posture_boundary_after_enter(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": SOURCE_SHA,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    _patch_all_phases_pass(monkeypatch, patch_probe=False)
    bitbucket_reachable = False

    def toggling_connect(address: tuple[str, int], timeout: float) -> object:
        host, port = address
        if host == "scm.mastercard.int" and port == 443 and not bitbucket_reachable:
            raise TimeoutError("timed out")
        return SimpleNamespace(close=lambda: None)

    def fake_input(_prompt: str = "") -> str:
        nonlocal bitbucket_reachable
        bitbucket_reachable = True
        return ""

    monkeypatch.setattr(cli.socket, "create_connection", toggling_connect)
    monkeypatch.setattr(
        cli, "git_probe_failures", _fake_bitbucket_down_git_failures(lambda: bitbucket_reachable)
    )
    monkeypatch.setattr(cli, "_sleep", lambda *_: None)
    monkeypatch.setattr("builtins.input", fake_input)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--run",
            run_id,
            "--guided",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Phase 'publish' requires posture [bitbucket]." in out
    assert "Unreachable: scm.mastercard.int:443." in out
    assert f"release complete: {run_id}" in out
    assert "next: none (complete)" in out
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "complete"


def test_release_guided_reprompts_while_probe_still_fails(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": SOURCE_SHA,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    _patch_all_phases_pass(monkeypatch, patch_probe=False)
    bitbucket_reachable = False
    input_calls = 0

    def toggling_connect(address: tuple[str, int], timeout: float) -> object:
        host, port = address
        if host == "scm.mastercard.int" and port == 443 and not bitbucket_reachable:
            raise TimeoutError("timed out")
        return SimpleNamespace(close=lambda: None)

    def fake_input(_prompt: str = "") -> str:
        nonlocal bitbucket_reachable, input_calls
        input_calls += 1
        if input_calls >= 2:
            bitbucket_reachable = True
        return ""

    monkeypatch.setattr(cli.socket, "create_connection", toggling_connect)
    monkeypatch.setattr(
        cli, "git_probe_failures", _fake_bitbucket_down_git_failures(lambda: bitbucket_reachable)
    )
    monkeypatch.setattr(cli, "_sleep", lambda *_: None)
    monkeypatch.setattr("builtins.input", fake_input)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--run",
            run_id,
            "--guided",
        ]
    )

    assert rc == 0
    assert input_calls == 2
    out = capsys.readouterr().out
    assert out.count("Phase 'publish' requires posture [bitbucket].") >= 2
    assert out.count("Unreachable: scm.mastercard.int:443.") >= 2


def test_release_guided_interrupted_leaves_run_open(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={
            "commit": SOURCE_SHA,
            "ci": "success",
            "tests": "passed",
            "verified_at": "2026-07-03T12:00:00+00:00",
        },
    )
    run_id = ledger.state["run_id"]
    monkeypatch.setattr(cli.socket, "create_connection", _unreachable_bitbucket_connect)

    def fake_input(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "git_probe_failures", _fake_bitbucket_down_git_failures(lambda: False))
    monkeypatch.setattr("builtins.input", fake_input)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--run",
            run_id,
            "--guided",
        ]
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert f"Paused at posture boundary. Resume with: python -m edge_deploy release --guided --run {run_id}" in out
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "open"
    assert reloaded.phase_state("publish") == "pending"


def test_release_guided_retries_transient_remote_errors(tmp_path, monkeypatch) -> None:
    """ADR-0012: guided mode retries a phase that raises CalledProcessError
    (remote flake while a posture switch propagates) with backoff."""
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={"commit": SOURCE_SHA, "ci": "success", "tests": "passed", "verified_at": "t"},
    )
    run_id = ledger.state["run_id"]
    _patch_all_phases_pass(monkeypatch)
    publish_attempts: list[int] = []
    sleeps: list[float] = []

    def flaky_publish(inner_ledger, operator, repo_root, **kwargs):
        publish_attempts.append(1)
        if len(publish_attempts) < 3:
            raise subprocess.CalledProcessError(1, ["git", "push", "bitbucket"])
        inner_ledger.set_phase(
            "publish",
            "passed",
            evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
        )
        return 0

    monkeypatch.setattr(cli, "run_publish_phase", flaky_publish)
    monkeypatch.setattr(cli, "_sleep", lambda seconds: sleeps.append(seconds))

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--run", run_id, "--guided"]
    )

    assert rc == 0
    assert len(publish_attempts) == 3
    assert sleeps == [10.0, 20.0]
    assert RunLedger.load(ledger.run_dir).state["status"] == "complete"


def test_release_unguided_propagates_transient_remote_errors(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={"commit": SOURCE_SHA, "ci": "success", "tests": "passed", "verified_at": "t"},
    )
    run_id = ledger.state["run_id"]
    _patch_all_phases_pass(monkeypatch)
    publish_attempts: list[int] = []

    def flaky_publish(inner_ledger, operator, repo_root, **kwargs):
        publish_attempts.append(1)
        raise subprocess.CalledProcessError(1, ["git", "push", "bitbucket"])

    monkeypatch.setattr(cli, "run_publish_phase", flaky_publish)

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--run", run_id]
    )

    assert rc == 2
    assert len(publish_attempts) == 1
    assert "release failed: CalledProcessError" in capsys.readouterr().err


def test_release_guided_gives_up_after_retry_budget(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "verify",
        "passed",
        evidence={"commit": SOURCE_SHA, "ci": "success", "tests": "passed", "verified_at": "t"},
    )
    run_id = ledger.state["run_id"]
    _patch_all_phases_pass(monkeypatch)
    publish_attempts: list[int] = []

    def always_failing_publish(inner_ledger, operator, repo_root, **kwargs):
        publish_attempts.append(1)
        raise subprocess.CalledProcessError(1, ["git", "push", "bitbucket"])

    monkeypatch.setattr(cli, "run_publish_phase", always_failing_publish)
    monkeypatch.setattr(cli, "_sleep", lambda seconds: None)

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--run", run_id, "--guided"]
    )

    assert rc == 2
    assert len(publish_attempts) == 1 + len(cli._GUIDED_RETRY_DELAYS)
    assert RunLedger.load(ledger.run_dir).state["status"] == "open"


def test_release_command_forwards_no_local_check(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    captured: dict = {}
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)
    _patch_all_phases_pass(monkeypatch)

    def fake_publish(ledger, operator, repo_root, **kwargs):
        captured["no_local_check"] = kwargs.get("no_local_check")
        ledger.set_phase(
            "publish",
            "passed",
            evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
        )
        return 0

    monkeypatch.setattr(cli, "run_publish_phase", fake_publish)
    _autobench_repo_root(tmp_path)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--no-local-check",
        ]
    )

    assert rc == 0
    assert captured["no_local_check"] is True


def test_release_command_nonzero_exit_on_failure(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)
    monkeypatch.setattr(cli, "probe", lambda *a, **k: [])

    def fake_ensure_verified(operator, profile, repo_root, ledger, **kwargs):
        ledger.set_phase("verify", "passed", evidence={"commit": SOURCE_SHA})
        return SimpleNamespace(commit=SOURCE_SHA)

    def fake_publish(ledger, operator, repo_root, **kwargs):
        ledger.set_phase(
            "publish",
            "passed",
            evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
        )
        return 0

    monkeypatch.setattr(cli, "ensure_verified", fake_ensure_verified)
    monkeypatch.setattr(cli, "run_publish_phase", fake_publish)
    monkeypatch.setattr(cli, "run_deploy", lambda *a, **k: 1)
    monkeypatch.setattr(cli, "probe", lambda *a, **k: [])
    monkeypatch.setattr(cli, "git_probe_failures", lambda *a, **k: [])
    monkeypatch.setattr(cli, "enter_phase", lambda *a, **k: __import__("contextlib").ExitStack())

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench"]
    )

    assert rc == 1
    runs = list((autobench_path / "edge-deploy" / "runs").iterdir())
    assert len(runs) == 1
    loaded = RunLedger.load(runs[0])
    assert loaded.state["status"] == "open"


def test_release_run_resumes_same_directory(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    ledger.set_phase(
        "publish",
        "passed",
        evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
    )
    ledger.set_phase(
        "verify",
        "passed",
        evidence={"commit": SOURCE_SHA, "ci": "success", "tests": "passed", "verified_at": "t"},
    )
    captured: dict = {}
    _patch_inspect_repository(monkeypatch, commit=SOURCE_SHA)

    def fake_deploy(args, operator):
        captured["run_id"] = args.run
        for node in ["node03", "node04"]:
            RunLedger.load(ledger.run_dir).set_phase(
                "deploy", "passed", node=node, evidence={"status": "rolled_out"}
            )
        return 0

    monkeypatch.setattr(cli, "run_deploy", fake_deploy)
    monkeypatch.setattr(cli, "_cmd_tag_bitbucket", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_cmd_tag_github", lambda *a, **k: (RunLedger.load(ledger.run_dir).complete() or 0))
    monkeypatch.setattr(cli, "probe", lambda *a, **k: [])
    monkeypatch.setattr(cli, "git_probe_failures", lambda *a, **k: [])
    monkeypatch.setattr(cli, "enter_phase", lambda *a, **k: __import__("contextlib").ExitStack())

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--run",
            ledger.state["run_id"],
        ]
    )

    assert rc == 0
    assert captured["run_id"] == ledger.state["run_id"]


def test_release_refuses_when_open_run_exists(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    run = ledger.state

    rc = cli.main(["--config", str(config_path), "release", "--tool", "autobench"])

    assert rc == 2
    out = capsys.readouterr().out
    expected = (
        f"release refused: unresolved run {run['run_id']} for {run['tool']} "
        f"(source {run['source_sha'][:7]}, created {run['created_at']}) exists.\n"
        "Choose one:\n"
        f"  1. continue it:   python -m edge_deploy release --run {run['run_id']}\n"
        f'  2. abandon it:    python -m edge_deploy abandon --run {run["run_id"]} --reason "<why>"\n'
    )
    assert out == expected


def test_abandon_flips_status_and_excludes_from_find_open(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    monkeypatch.chdir(autobench_path)
    ledger = _create_open_run(autobench_path)
    run_id = ledger.state["run_id"]
    runs_root = autobench_path / "edge-deploy" / "runs"

    rc = cli.main(
        ["--config", str(config_path), "abandon", "--run", run_id, "--reason", "superseded"]
    )

    assert rc == 0
    assert capsys.readouterr().out == f"abandoned {run_id}\n"
    loaded = RunLedger.load(ledger.run_dir)
    assert loaded.state["status"] == "abandoned"
    assert loaded.state["abandon_reason"] == "superseded"
    assert RunLedger.find_open(runs_root) == []


def test_abandon_refuses_when_run_locked(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    monkeypatch.chdir(autobench_path)
    ledger = _create_open_run(autobench_path)
    run_id = ledger.state["run_id"]
    (ledger.run_dir / "run.lock").write_text(
        json.dumps(
            {
                "pid": 4242,
                "hostname": "edge-host",
                "acquired_at": "2026-07-03T12:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc = cli.main(
        ["--config", str(config_path), "abandon", "--run", run_id, "--reason", "superseded"]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "RunLockError" in err
    assert "is locked by PID 4242 on edge-host" in err
    loaded = RunLedger.load(ledger.run_dir)
    assert loaded.state["status"] == "open"


def test_release_refuses_abandoned_run(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    run_id = ledger.state["run_id"]
    ledger.abandon("superseded")
    _patch_inspect_repository(monkeypatch)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--run",
            run_id,
        ]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert f"release refused: run {run_id} is abandoned" in err


def test_release_run_lock_held_returns_exit_2(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    autobench_path = _autobench_repo_root(tmp_path)
    ledger = _create_open_run(autobench_path)
    (ledger.run_dir / "run.lock").write_text(
        json.dumps(
            {
                "pid": 4242,
                "hostname": "edge-host",
                "acquired_at": "2026-07-03T12:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _patch_inspect_repository(monkeypatch)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--run",
            ledger.state["run_id"],
        ]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "RunLockError" in err
    assert "is locked by PID 4242 on edge-host" in err


# ---------------------------------------------------------------------------
# publish command dispatch (publish_snapshot monkeypatched)
# ---------------------------------------------------------------------------


def test_publish_command_prints_snapshot(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)

    def fake_publish(profile, **kwargs) -> PublishResult:
        return PublishResult(
            tool=profile.tool, status="published", snapshot="snap123", source_commit="srccommit",
            source_short="srcshrt", branch="main", previous_remote_commit="prev999",
            message="Deploy snapshot: autobench srcshrt on main (2026-06-29 23:00) [edge-deploy]",
        )

    monkeypatch.setattr(cli, "publish_snapshot", fake_publish)

    rc = cli.main(["--config", str(config_path), "publish", "--tool", "autobench", "--no-local-check"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Published commit: snap123" in out
    assert "srcshrt" in out


def test_publish_command_publish_error_returns_2(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)

    def fake_publish(profile, **kwargs) -> PublishResult:
        raise PublishError("local_check.ps1 failed with exit code 1")

    monkeypatch.setattr(cli, "publish_snapshot", fake_publish)

    rc = cli.main(["--config", str(config_path), "publish", "--tool", "robocop"])

    assert rc == 2
    assert "publish failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# mirror command dispatch (mirror_release monkeypatched)
# ---------------------------------------------------------------------------


def test_parser_parses_mirror_args() -> None:
    args = cli.build_parser().parse_args(["mirror", "--tag", "v1.1.0", "--remote", "bb", "--branch", "release"])

    assert args.command == "mirror"
    assert args.tag == "v1.1.0"
    assert args.remote == "bb"
    assert args.branch == "release"
    assert args.repo_root == "."


def test_mirror_command_prints_result_and_needs_no_operator_config(tmp_path, monkeypatch, capsys) -> None:
    captured: dict = {}

    def fake_mirror(repo_root, *, tag, remote, branch) -> MirrorResult:
        captured.update(repo_root=repo_root, tag=tag, remote=remote, branch=branch)
        return MirrorResult(
            tag=tag, mode="mirrored", branch=branch, source_commit="src" + "0" * 37,
            deployed_commit="dep" + "1" * 37, tree="tree" + "2" * 36,
            previous_remote_commit="prev" + "3" * 36,
            message="Mirror release v1.1.0: source src tree tree [edge-deploy]",
        )

    monkeypatch.setattr(cli, "mirror_release", fake_mirror)

    rc = cli.main(["--config", str(tmp_path / "missing.yaml"), "mirror", "--tag", "v1.1.0"])

    assert rc == 0
    assert captured["tag"] == "v1.1.0"
    assert captured["remote"] == "bitbucket"
    out = capsys.readouterr().out
    assert "Mirrored v1.1.0 to bitbucket (mirrored)" in out
    assert "deployed commit: dep" in out


def test_mirror_command_error_returns_2(monkeypatch, capsys) -> None:
    def fake_mirror(repo_root, **kwargs):
        raise MirrorError("refusing a non-fast-forward mirror")

    monkeypatch.setattr(cli, "mirror_release", fake_mirror)

    rc = cli.main(["mirror", "--tag", "v1.1.0"])

    assert rc == 2
    assert "mirror failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# release tagging and rollback resolution are tree-equivalent (ADR-0007)
# ---------------------------------------------------------------------------


def _fake_git_subprocess(commands: list, stdout_by_ref: dict | None = None):
    stdout_by_ref = stdout_by_ref or {}

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        commands.append(list(cmd))
        stdout = ""
        if cmd[:2] == ["git", "rev-parse"]:
            stdout = stdout_by_ref.get(cmd[2], "f" * 40) + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return fake_run


def test_resolve_release_tag_accepts_tree_equivalent_mirror(tmp_path, monkeypatch) -> None:
    origin_sha = "a" * 40
    bitbucket_sha = "d" * 40
    tag = f"release-20260702T013000Z-{origin_sha[:7]}"
    shas = {"origin": origin_sha, "bitbucket": bitbucket_sha}
    monkeypatch.setattr(cli, "_remote_tag_sha", lambda root, remote, t: shas[remote])
    commands: list = []
    shared_tree = "7" * 40
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        _fake_git_subprocess(
            commands,
            {
                f"refs/tags/{tag}^{{commit}}": origin_sha,
                f"refs/edge-deploy/rollback/{tag}^{{tree}}": shared_tree,
                f"{origin_sha}^{{tree}}": shared_tree,
            },
        ),
    )

    resolved = cli._resolve_release_tag(tmp_path, tag)

    assert resolved == bitbucket_sha  # nodes fetch from bitbucket, so roll back to its commit
    assert ["git", "update-ref", "-d", f"refs/edge-deploy/rollback/{tag}"] in commands


def test_resolve_release_tag_rejects_tree_divergence(tmp_path, monkeypatch) -> None:
    origin_sha = "a" * 40
    bitbucket_sha = "d" * 40
    tag = f"release-20260702T013000Z-{origin_sha[:7]}"
    shas = {"origin": origin_sha, "bitbucket": bitbucket_sha}
    monkeypatch.setattr(cli, "_remote_tag_sha", lambda root, remote, t: shas[remote])
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        _fake_git_subprocess(
            [],
            {
                f"refs/tags/{tag}^{{commit}}": origin_sha,
                f"refs/edge-deploy/rollback/{tag}^{{tree}}": "7" * 40,
                f"{origin_sha}^{{tree}}": "8" * 40,
            },
        ),
    )

    with pytest.raises(cli.RepositoryError, match="not tree-equivalent"):
        cli._resolve_release_tag(tmp_path, tag)


# ---------------------------------------------------------------------------
# python -m edge_deploy entry point
# ---------------------------------------------------------------------------


def test_dunder_main_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "edge_deploy", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "rollout" in result.stdout
    assert "release" in result.stdout


def test_dunder_main_requires_subcommand() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "edge_deploy"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2


def test_phase_already_passed_treats_skipped_as_satisfied(tmp_path) -> None:
    """A rollback run's skipped verify must not re-enter the chain: re-invoking
    it is a no-op, but probing its posture first would wrongly demand GitHub
    reachability during a Bitbucket/Edge rollback."""
    from edge_deploy.ledger import RunLedger

    ledger = RunLedger.create(
        tmp_path / "runs",
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op@example.com",
        kind="rollback",
        rollback_tag="release-20260630T221900Z-5335a65",
    )
    ledger.set_phase("verify", "skipped", evidence={"reason": "rollback tag"})

    assert cli._phase_already_passed(ledger, "verify", ["node03"])
    assert not cli._phase_already_passed(ledger, "publish", ["node03"])
