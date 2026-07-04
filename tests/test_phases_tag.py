"""Tag-github and tag-bitbucket phase git sequences and gating."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from edge_deploy import cli
from edge_deploy.ledger import RunLedger
from edge_deploy.phases import tag as tag_phase
from tests.test_cli import _autobench_repo_root, _write_operator_config

SOURCE_SHA = "a" * 40
SNAPSHOT_SHA = "d" * 40
TAG = f"release-20260703T120000Z-{SOURCE_SHA[:7]}"


@contextmanager
def _noop_enter_phase(*args, **kwargs):
    yield


def _ledger_ready_for_tags(repo_root, *, snapshot_sha: str = SOURCE_SHA) -> RunLedger:
    ledger = RunLedger.create(
        repo_root / "edge-deploy" / "runs",
        tool="autobench",
        source_sha=SOURCE_SHA,
        nodes=["node03", "node04"],
        operator="operator@example.com",
    )
    ledger.set_phase("publish", "passed", evidence={"snapshot_sha": snapshot_sha, "source_commit": SOURCE_SHA})
    for node in ledger.state["nodes"]:
        ledger.set_phase("deploy", "passed", node=node, evidence={"status": "rolled_out"})
    return ledger


def _git_recorder(
    commands: list[list[str]],
    *,
    ls_remote_by_remote_tag: dict[tuple[str, str], str] | None = None,
):
    ls_remote_by_remote_tag = ls_remote_by_remote_tag or {}

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        commands.append(list(cmd))
        stdout = ""
        if "ls-remote" in cmd:
            remote = cmd[cmd.index("--tags") + 1] if "--tags" in cmd else ""
            ref = cmd[-1]
            tag_name = ref.removeprefix("refs/tags/").removesuffix("^{}")
            key = (remote, tag_name)
            sha = ls_remote_by_remote_tag.get(key, SOURCE_SHA)
            stdout = f"{sha}\trefs/tags/{tag_name}^{{}}\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return fake_run


def test_tag_github_push_and_verify(tmp_path, monkeypatch, capsys) -> None:
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = _ledger_ready_for_tags(repo_root)
    commands: list[list[str]] = []
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)
    monkeypatch.setattr(
        tag_phase,
        "_tag_successful_release",
        lambda root, commit: TAG,
    )
    monkeypatch.setattr(
        tag_phase.subprocess,
        "run",
        _git_recorder(commands, ls_remote_by_remote_tag={("origin", TAG): SOURCE_SHA}),
    )

    rc = cli.main(["--config", str(config_path), "tag-github", "--run", ledger.state["run_id"]])

    assert rc == 0
    assert ["git", "push", "origin", f"refs/tags/{TAG}"] in commands
    assert [
        "git",
        "ls-remote",
        "--exit-code",
        "--tags",
        "origin",
        f"refs/tags/{TAG}^{{}}",
    ] in commands
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("tag_github") == "passed"
    assert reloaded.state["phases"]["tag_github"]["evidence"]["pushed_sha"] == SOURCE_SHA
    assert "tag-github: already pushed" not in capsys.readouterr().out


def test_tag_github_refused_when_deploy_not_passed(tmp_path, monkeypatch, capsys) -> None:
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = RunLedger.create(
        repo_root / "edge-deploy" / "runs",
        tool="autobench",
        source_sha=SOURCE_SHA,
        nodes=["node03"],
        operator="operator@example.com",
    )
    ledger.set_phase("deploy", "failed", node="node03", evidence={})
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)

    rc = cli.main(["--config", str(config_path), "tag-github", "--run", ledger.state["run_id"]])

    assert rc == 2
    assert "not all deploy nodes passed" in capsys.readouterr().err


def test_tag_github_idempotent_skip(tmp_path, monkeypatch, capsys) -> None:
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = _ledger_ready_for_tags(repo_root)
    ledger.set_phase("tag_github", "passed", evidence={"tag": TAG, "pushed_sha": SOURCE_SHA})
    commands: list[list[str]] = []
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)
    monkeypatch.setattr(tag_phase.subprocess, "run", _git_recorder(commands))

    rc = cli.main(["--config", str(config_path), "tag-github", "--run", ledger.state["run_id"]])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "tag-github: already pushed (skipping)"
    assert commands == []


def test_tag_bitbucket_pushes_same_tag_when_snapshot_equals_source(
    tmp_path, monkeypatch
) -> None:
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = _ledger_ready_for_tags(repo_root, snapshot_sha=SOURCE_SHA)
    ledger.set_phase("tag_github", "passed", evidence={"tag": TAG, "pushed_sha": SOURCE_SHA})
    commands: list[list[str]] = []
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("BB_TOKEN", "test-token")
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit-sha")
    monkeypatch.setattr(
        tag_phase.subprocess,
        "run",
        _git_recorder(commands, ls_remote_by_remote_tag={("bitbucket", TAG): SOURCE_SHA}),
    )

    rc = cli.main(["--config", str(config_path), "tag-bitbucket", "--run", ledger.state["run_id"]])

    assert rc == 0
    assert [
        "git",
        "-c",
        "http.extraHeader=Authorization: Bearer test-token",
        "push",
        "bitbucket",
        f"refs/tags/{TAG}",
    ] in commands
    assert [
        "git",
        "-c",
        "http.extraHeader=Authorization: Bearer test-token",
        "ls-remote",
        "--exit-code",
        "--tags",
        "bitbucket",
        f"refs/tags/{TAG}^{{}}",
    ] in commands
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.state["status"] == "complete"


def test_tag_bitbucket_mirror_push_when_snapshot_differs(tmp_path, monkeypatch) -> None:
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = _ledger_ready_for_tags(repo_root, snapshot_sha=SNAPSHOT_SHA)
    ledger.set_phase("tag_github", "passed", evidence={"tag": TAG, "pushed_sha": SOURCE_SHA})
    commands: list[list[str]] = []
    temp_tag = f"edge-deploy-mirror/{TAG}"
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("BB_TOKEN", "test-token")
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit-sha")
    monkeypatch.setattr(
        tag_phase.subprocess,
        "run",
        _git_recorder(commands, ls_remote_by_remote_tag={("bitbucket", TAG): SNAPSHOT_SHA}),
    )

    rc = cli.main(["--config", str(config_path), "tag-bitbucket", "--run", ledger.state["run_id"]])

    assert rc == 0
    message = f"Successful release {TAG} (source {SOURCE_SHA}) [edge-deploy]"
    assert [
        "git",
        "tag",
        "-a",
        "-f",
        temp_tag,
        SNAPSHOT_SHA,
        "-m",
        message,
    ] in commands
    assert [
        "git",
        "-c",
        "http.extraHeader=Authorization: Bearer test-token",
        "push",
        "bitbucket",
        f"refs/tags/{temp_tag}:refs/tags/{TAG}",
    ] in commands
    assert ["git", "tag", "-d", temp_tag] in commands


def test_tag_bitbucket_first_mints_tag_and_leaves_run_open(tmp_path, monkeypatch) -> None:
    """ADR-0012: tag_bitbucket runs before tag_github, minting the release tag."""
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = _ledger_ready_for_tags(repo_root, snapshot_sha=SOURCE_SHA)
    commands: list[list[str]] = []
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("BB_TOKEN", "test-token")
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)
    monkeypatch.setattr(tag_phase, "_tag_successful_release", lambda root, commit: TAG)
    monkeypatch.setattr(cli, "_record_release_attempt", lambda *a, **k: "audit-sha")
    monkeypatch.setattr(
        tag_phase.subprocess,
        "run",
        _git_recorder(commands, ls_remote_by_remote_tag={("bitbucket", TAG): SOURCE_SHA}),
    )

    rc = cli.main(["--config", str(config_path), "tag-bitbucket", "--run", ledger.state["run_id"]])

    assert rc == 0
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("tag_bitbucket") == "passed"
    assert reloaded.state["phases"]["tag_bitbucket"]["evidence"]["tag"] == TAG
    assert reloaded.phase_state("tag_github") == "pending"
    assert reloaded.state["status"] == "open"


def test_tag_github_after_bitbucket_reuses_tag_and_completes_run(tmp_path, monkeypatch) -> None:
    repo_root = _autobench_repo_root(tmp_path)
    config_path = _write_operator_config(tmp_path)
    ledger = _ledger_ready_for_tags(repo_root, snapshot_sha=SOURCE_SHA)
    ledger.set_phase("tag_bitbucket", "passed", evidence={"tag": TAG, "pushed_sha": SOURCE_SHA})
    commands: list[list[str]] = []
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(tag_phase, "enter_phase", _noop_enter_phase)

    def refuse_minting(root, commit):
        raise AssertionError("tag_github must reuse the tag minted by tag_bitbucket")

    monkeypatch.setattr(tag_phase, "_tag_successful_release", refuse_minting)
    monkeypatch.setattr(
        tag_phase.subprocess,
        "run",
        _git_recorder(commands, ls_remote_by_remote_tag={("origin", TAG): SOURCE_SHA}),
    )

    rc = cli.main(["--config", str(config_path), "tag-github", "--run", ledger.state["run_id"]])

    assert rc == 0
    assert ["git", "push", "origin", f"refs/tags/{TAG}"] in commands
    reloaded = RunLedger.load(ledger.run_dir)
    assert reloaded.phase_state("tag_github") == "passed"
    assert reloaded.state["status"] == "complete"


def test_tag_successful_release_creates_local_tag_only(tmp_path, monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(tag_phase.subprocess, "run", _git_recorder(commands))

    tag = tag_phase._tag_successful_release(tmp_path, SOURCE_SHA)

    assert tag.startswith("release-") and tag.endswith(SOURCE_SHA[:7])
    assert len(commands) == 1
    assert commands[0][:3] == ["git", "tag", "-a"]
    assert not any("push" in cmd for cmd in commands)
