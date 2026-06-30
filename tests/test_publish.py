"""Publish: BB_TOKEN snapshot publish (gate + ``git commit-tree`` reparent).

Two layers: a fake ``git_runner`` for the unit cases (message exactness, commit-tree
parent/tree wiring, gate failures, Bearer header on push, token never leaking into the
result) and a real temporary git repo + local bare remote for the happy path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from edge_deploy import publish
from edge_deploy.config import ToolProfile
from edge_deploy.publish import (
    PublishError,
    build_snapshot_message,
    publish_snapshot,
)

TOKEN = "s3cr3t-bearer-token"
FIXED_CLOCK = lambda: datetime(2026, 6, 29, 23, 0, tzinfo=timezone.utc)  # noqa: E731

AUTOBENCH = ToolProfile(tool="autobench", repo_path="/ads_storage/autobench", release_branch="main")


class FakeGit:
    """A scriptable ``git_runner`` that records argv and returns canned stdout by content."""

    def __init__(
        self,
        *,
        status: str = "",
        branch: str = "main",
        source_commit: str = "a1b2c3d4e5f6a7b8",
        short: str = "a1b2c3d",
        previous: str = "0f0f0f0f0f0f",
        tree: str = "7ree7ree7ree",
        snapshot: str = "5nap5napsnap5nap",
    ) -> None:
        self.calls: list[list[str]] = []
        self.status = status
        self.branch = branch
        self.source_commit = source_commit
        self.short = short
        self.previous = previous
        self.tree = tree
        self.snapshot = snapshot

    def __call__(self, args) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["status", "--porcelain"]:
            return self.status
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return self.branch + "\n"
        if args[:2] == ["rev-parse", "--short"]:
            return self.short + "\n"
        if args[:2] == ["rev-parse", "--verify"]:
            ref = args[2]
            return (self.previous if "/" in ref else self.source_commit) + "\n"
        if args[0] == "rev-parse" and len(args) == 2:
            return (self.tree if args[1].endswith("^{tree}") else self.previous) + "\n"
        if args[0] == "commit-tree":
            return self.snapshot + "\n"
        # fetch / push (with or without the leading -c auth header)
        return ""

    def push_call(self) -> list[str] | None:
        return next((call for call in self.calls if "push" in call), None)

    def commit_tree_call(self) -> list[str] | None:
        return next((call for call in self.calls if call and call[0] == "commit-tree"), None)


@pytest.fixture(autouse=True)
def _token(monkeypatch) -> None:
    monkeypatch.setenv("BB_TOKEN", TOKEN)


# ---------------------------------------------------------------------------
# Message format
# ---------------------------------------------------------------------------


def test_build_snapshot_message_exact_format() -> None:
    message = build_snapshot_message("autobench", "a1b2c3d", "main", FIXED_CLOCK())

    assert message == "Deploy snapshot: autobench a1b2c3d on main (2026-06-29 23:00) [edge-deploy]"


def test_publish_message_uses_injected_clock_and_short() -> None:
    git = FakeGit(short="cafe123")

    result = publish_snapshot(
        AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False, clock=FIXED_CLOCK
    )

    assert result.message == "Deploy snapshot: autobench cafe123 on main (2026-06-29 23:00) [edge-deploy]"


# ---------------------------------------------------------------------------
# commit-tree reparent wiring
# ---------------------------------------------------------------------------


def test_publish_reparents_with_commit_tree_parent_and_tree() -> None:
    git = FakeGit(previous="0f0f0f0f0f0f", tree="7ree7ree7ree", snapshot="5nap5nap")

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    assert result.snapshot == "5nap5nap"
    assert result.previous_remote_commit == "0f0f0f0f0f0f"
    commit_tree = git.commit_tree_call()
    # commit-tree <tree> -p <remote-parent> -m <message>
    assert commit_tree[1] == "7ree7ree7ree"
    assert commit_tree[2] == "-p"
    assert commit_tree[3] == "0f0f0f0f0f0f"
    assert commit_tree[4] == "-m"
    assert "Deploy snapshot: autobench" in commit_tree[5]
    # The working tree is never mutated: no checkout/reset/commit ran.
    assert not any(call[0] in {"checkout", "reset", "commit"} for call in git.calls)


def test_publish_pushes_with_bearer_header_and_snapshot_refspec() -> None:
    git = FakeGit(snapshot="5nap5nap")

    publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    push = git.push_call()
    assert push[0] == "-c"
    assert push[1] == f"http.extraHeader=Authorization: Bearer {TOKEN}"
    assert push[2] == "push"
    assert push[3] == "bitbucket"
    assert push[4] == "5nap5nap:refs/heads/main"


# ---------------------------------------------------------------------------
# Secret hygiene (ADR-0002)
# ---------------------------------------------------------------------------


def test_token_never_appears_in_publish_result() -> None:
    git = FakeGit()

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    # The token is used on the push argv (proven above) but never stored in the result.
    assert TOKEN not in json.dumps(result.to_payload())
    for value in vars(result).values():
        assert TOKEN not in str(value)


# ---------------------------------------------------------------------------
# Gate semantics
# ---------------------------------------------------------------------------


def test_publish_refuses_dirty_tree_by_default() -> None:
    git = FakeGit(status=" M benchmark.py")

    with pytest.raises(PublishError, match="not clean"):
        publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)


def test_publish_refuses_off_release_branch_by_default() -> None:
    git = FakeGit(branch="feature/x")

    with pytest.raises(PublishError, match="release branch"):
        publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)


def test_publish_fails_when_local_check_nonzero() -> None:
    git = FakeGit()

    with pytest.raises(PublishError, match="exit code 1"):
        publish_snapshot(
            AUTOBENCH, repo_root="/x", git_runner=git, local_check_runner=lambda root: 1
        )


def test_publish_runs_local_check_by_default() -> None:
    git = FakeGit()
    seen: list[Path] = []

    def local_check(root: Path) -> int:
        seen.append(Path(root))
        return 0

    publish_snapshot(AUTOBENCH, repo_root="/repo/autobench", git_runner=git, local_check_runner=local_check)

    assert seen == [Path("/repo/autobench")]


def test_publish_commit_override_relaxes_tree_and_branch_gate() -> None:
    # Dirty tree AND off release branch, but --commit names a reviewed commit -> proceeds.
    git = FakeGit(status=" M benchmark.py", branch="feature/x")

    result = publish_snapshot(
        AUTOBENCH, repo_root="/x", git_runner=git, commit="deadbeefcafe", run_local_check=False
    )

    assert result.status == "published"
    assert result.gate["clean_tree"] is False
    assert result.gate["on_release_branch"] is False
    # commit-tree source was the named commit (its ^{tree} was resolved).
    assert any(call == ["rev-parse", "deadbeefcafe^{tree}"] for call in git.calls)


def test_publish_requires_token() -> None:
    git = FakeGit()

    with pytest.raises(PublishError, match="is not set"):
        publish_snapshot(
            AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False, token_env="MISSING_TOKEN_ENV"
        )


# ---------------------------------------------------------------------------
# local_check.ps1 runner (shell resolution, fail loudly)
# ---------------------------------------------------------------------------


def test_run_local_check_missing_script_raises(tmp_path) -> None:
    with pytest.raises(PublishError, match="local_check gate script not found"):
        publish.run_local_check_ps1(tmp_path)


def test_run_local_check_no_powershell_raises(tmp_path, monkeypatch) -> None:
    script = tmp_path / "tools" / "dev" / "local_check.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("exit 0\n", encoding="utf-8")
    monkeypatch.setattr(publish, "_resolve_powershell", lambda: None)

    with pytest.raises(PublishError, match="neither 'pwsh' nor 'powershell'"):
        publish.run_local_check_ps1(tmp_path)


def test_run_local_check_prepends_repo_venv_py_shim(tmp_path, monkeypatch) -> None:
    script = tmp_path / "tools" / "dev" / "local_check.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("py -m pytest\n", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(publish, "_resolve_powershell", lambda: "pwsh")
    seen: dict[str, object] = {}

    def fake_run(argv, *, cwd, capture_output, text, env):
        shim_dir = Path(str(env["PATH"]).split(os.pathsep)[0])
        shim = shim_dir / "py.cmd"
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["shim_text"] = shim.read_text(encoding="utf-8")
        seen["path_head"] = shim_dir
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    assert publish.run_local_check_ps1(tmp_path) == 0

    assert seen["argv"] == ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    assert seen["cwd"] == str(tmp_path)
    assert str(venv_python) in str(seen["shim_text"])
    assert Path(seen["path_head"]).parent == tmp_path


# ---------------------------------------------------------------------------
# Happy path against a real temporary git repo + local bare remote
# ---------------------------------------------------------------------------

GIT = shutil.which("git")


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_publish_against_real_repo_advances_remote_main(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(["init"], work)
    _git(["config", "user.email", "op@example.com"], work)
    _git(["config", "user.name", "Operator"], work)
    (work / "benchmark.py").write_text("print('v1')\n", encoding="utf-8")
    _git(["add", "."], work)
    _git(["commit", "-m", "initial"], work)
    _git(["branch", "-M", "main"], work)

    bare = tmp_path / "remote.git"
    _git(["init", "--bare", str(bare)], tmp_path)
    _git(["remote", "add", "bitbucket", str(bare)], work)
    _git(["push", "bitbucket", "main"], work)

    previous = _git(["rev-parse", "main"], work)

    result = publish_snapshot(
        AUTOBENCH, repo_root=work, run_local_check=False, clock=FIXED_CLOCK
    )

    assert result.status == "published"
    assert result.previous_remote_commit == previous
    # The bare remote's main now points at the new Snapshot.
    remote_head = _git(["rev-parse", "main"], bare)
    assert remote_head == result.snapshot
    # The Snapshot's tree equals the reviewed source tree; its parent is the old remote head.
    assert _git(["rev-parse", f"{result.snapshot}^{{tree}}"], work) == _git(["rev-parse", "HEAD^{tree}"], work)
    assert _git(["rev-parse", f"{result.snapshot}^"], work) == previous
