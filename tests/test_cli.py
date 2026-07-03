"""Thin CLI surface: argparse wiring, config resolution, and command dispatch.

Network and tmux are faked, so ``rollout`` / ``drift`` / ``preflight`` run end to end with
no nodes. A subprocess smoke test exercises the real ``python -m edge_deploy`` entry point.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy import cli
from edge_deploy.mirror import MirrorError, MirrorResult
from edge_deploy.publish import PublishError, PublishResult
from edge_deploy.reporting import ReleaseReport

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


def _write_operator_config(tmp_path: Path) -> Path:
    autobench_path = _write_tool_profile(tmp_path, "autobench")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        OPERATOR_CONFIG.format(autobench_path=autobench_path.as_posix()),
        encoding="utf-8",
    )
    return config_path


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
    assert "publish" in help_text
    assert "rollout" in help_text
    assert "drift" in help_text
    assert "preflight" in help_text


def test_parser_parses_release_args() -> None:
    args = cli.build_parser().parse_args(
        ["release", "--nodes", "03,04", "--auth-mode", "prompt",
         "--smoke", "deep", "--fail-fast", "--no-local-check", "--report-dir", "out", "--max-auth-attempts", "5"]
    )

    assert args.command == "release"
    assert args.tool is None
    assert args.nodes == "03,04"
    assert args.auth_mode == "prompt"
    assert args.smoke == "deep"
    assert args.fail_fast is True
    assert args.no_local_check is True
    assert args.report_dir == "out"
    assert args.max_auth_attempts == 5


def test_parser_release_defaults() -> None:
    args = cli.build_parser().parse_args(["release"])

    assert args.tool is None
    assert args.nodes is None
    assert args.auth_mode == "auto"
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


def test_parser_accepts_auto_prompt_and_pane_auth_modes() -> None:
    for mode in ("auto", "prompt", "pane"):
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
# release command dispatch (run_release monkeypatched)
# ---------------------------------------------------------------------------


def test_release_command_dispatches_and_writes_consolidated_report(tmp_path, monkeypatch, capsys) -> None:
    config_path = _write_operator_config_both(tmp_path)
    captured: dict = {}

    def fake_run_release(operator, selection, *, report_dir, max_auth_attempts, **kwargs) -> ReleaseReport:
        captured["selection"] = selection
        captured["report_dir"] = report_dir
        captured["max_auth_attempts"] = max_auth_attempts
        captured["auth_mode"] = kwargs["auth_mode"]
        captured["progress_fn"] = kwargs["progress_fn"]
        return ReleaseReport(
            selection={"tools": selection.tools},
            publishes=[{"tool": "autobench", "status": "published", "snapshot": "abc"}],
            rollouts=[{"tool": "autobench", "node": "node03", "status": "rolled_out", "state_left": ""}],
        )

    monkeypatch.setattr(cli, "run_release", fake_run_release)
    monkeypatch.setattr(cli, "_run_release_preflight", lambda *a, **k: SimpleNamespace(commit="a" * 40))
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit")
    monkeypatch.setattr(cli, "_tag_successful_release", lambda *a, **k: "tag")
    report_dir = tmp_path / "rep"

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--nodes", "03,04",
         "--smoke", "deep", "--report-dir", str(report_dir), "--max-auth-attempts", "5"]
    )

    assert rc == 0
    selection = captured["selection"]
    assert selection.tools == ["autobench"]
    assert selection.nodes == ["node03", "node04"]
    assert selection.smoke == "deep"
    assert selection.snapshot_by_tool == {}
    assert selection.run_local_check is True
    assert captured["auth_mode"] == "pane"
    assert callable(captured["progress_fn"])
    assert captured["max_auth_attempts"] == 5
    assert (report_dir / "release.json").exists()
    assert "Release: passed" in capsys.readouterr().out


def test_release_command_forwards_no_local_check(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    captured: dict = {}

    def fake_run_release(operator, selection, *, report_dir, max_auth_attempts, **kwargs) -> ReleaseReport:
        captured["selection"] = selection
        return ReleaseReport(
            selection={"tools": selection.tools},
            publishes=[{"tool": "autobench", "status": "published", "snapshot": "abc"}],
            rollouts=[{"tool": "autobench", "node": "node03", "status": "rolled_out", "state_left": ""}],
        )

    monkeypatch.setattr(cli, "run_release", fake_run_release)
    monkeypatch.setattr(cli, "_run_release_preflight", lambda *a, **k: SimpleNamespace(commit="a" * 40))
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit")
    monkeypatch.setattr(cli, "_tag_successful_release", lambda *a, **k: "tag")

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "release",
            "--tool",
            "autobench",
            "--report-dir",
            str(tmp_path / "rep"),
            "--no-local-check",
        ]
    )

    assert rc == 0
    assert captured["selection"].run_local_check is False


def test_release_command_nonzero_exit_on_failure(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)

    def fake_run_release(operator, selection, *, report_dir, max_auth_attempts, **kwargs) -> ReleaseReport:
        return ReleaseReport(
            selection={},
            rollouts=[{"tool": "autobench", "node": "node03", "status": "failed", "state_left": "boom"}],
        )

    monkeypatch.setattr(cli, "run_release", fake_run_release)
    monkeypatch.setattr(cli, "_run_release_preflight", lambda *a, **k: SimpleNamespace(commit="a" * 40))
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit")

    rc = cli.main(
        ["--config", str(config_path), "release", "--tool", "autobench", "--report-dir", str(tmp_path / "rep")]
    )

    assert rc == 1


def test_release_command_resume_loads_publish_snapshots(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config_both(tmp_path)
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    (resume_dir / "publish-autobench.json").write_text(
        json.dumps({"tool": "autobench", "status": "published", "deployment_commit": "a" * 40}) + "\n",
        encoding="utf-8",
    )
    captured: dict = {}

    def fake_run_release(operator, selection, *, report_dir, max_auth_attempts, **kwargs) -> ReleaseReport:
        captured["selection"] = selection
        captured["report_dir"] = report_dir
        return ReleaseReport(
            selection={"tools": selection.tools},
            publishes=[],
            rollouts=[{"tool": "autobench", "node": "node03", "status": "rolled_out", "state_left": ""}],
        )

    monkeypatch.setattr(cli, "run_release", fake_run_release)
    monkeypatch.setattr(cli, "_run_release_preflight", lambda *a, **k: SimpleNamespace(commit="a" * 40))
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit")
    monkeypatch.setattr(cli, "_tag_successful_release", lambda *a, **k: "tag")

    rc = cli.main(["--config", str(config_path), "release", "--tool", "autobench", "--resume", str(resume_dir)])

    assert rc == 0
    assert captured["report_dir"] == resume_dir
    assert captured["selection"].snapshot_by_tool == {"autobench": "a" * 40}
    assert (resume_dir / "release.json").exists()


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


def test_tag_successful_release_tags_deployment_commit_on_bitbucket(tmp_path, monkeypatch) -> None:
    commands: list = []
    monkeypatch.setattr(cli.subprocess, "run", _fake_git_subprocess(commands))
    monkeypatch.delenv("BB_TOKEN", raising=False)
    source = "a" * 40
    deployed = "d" * 40

    tag = cli._tag_successful_release(tmp_path, source, deployment_commit=deployed)

    assert tag.startswith("release-") and tag.endswith(source[:7])
    temp_tag = f"edge-deploy-mirror/{tag}"
    tag_creations = [cmd for cmd in commands if cmd[:2] == ["git", "tag"] and "-d" not in cmd]
    assert ["git", "tag", "-a", tag, source, "-m", f"Successful release {tag}"] in tag_creations
    assert any(cmd[4] == temp_tag and cmd[5] == deployed for cmd in tag_creations if "-f" in cmd)
    assert ["git", "push", "origin", f"refs/tags/{tag}"] in commands
    assert ["git", "push", "bitbucket", f"refs/tags/{temp_tag}:refs/tags/{tag}"] in commands
    assert ["git", "tag", "-d", temp_tag] in commands  # temp tag cleaned up


def test_tag_successful_release_pushes_same_tag_when_exact(tmp_path, monkeypatch) -> None:
    commands: list = []
    monkeypatch.setattr(cli.subprocess, "run", _fake_git_subprocess(commands))
    monkeypatch.delenv("BB_TOKEN", raising=False)
    source = "a" * 40

    tag = cli._tag_successful_release(tmp_path, source, deployment_commit=source)

    assert ["git", "push", "bitbucket", f"refs/tags/{tag}"] in commands
    assert not any("edge-deploy-mirror/" in part for cmd in commands for part in cmd)


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
