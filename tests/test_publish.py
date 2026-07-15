"""Exact-source, fast-forward-only Publish tests."""

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
from edge_deploy.publish import LocalCheckError, PublishError, publish_snapshot

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
        push_error: str | None = None,
        remote_after_push_failure: str | None = None,
        merge_base_fails: bool = False,
        previous_subject: str = "Deploy snapshot: autobench cafe123 on main (2026-06-29 23:00 UTC) [edge-deploy]",
        snapshot_commit: str = "d" * 40,
    ) -> None:
        self.calls: list[list[str]] = []
        self.status = status
        self.branch = branch
        self.source_commit = source_commit
        self.short = short
        self.previous = previous
        self.push_error = push_error
        self.remote_after_push_failure = remote_after_push_failure
        self.merge_base_fails = merge_base_fails
        self.previous_subject = previous_subject
        self.snapshot_commit = snapshot_commit

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
        if args[:3] == ["log", "-1", "--format=%s"]:
            return self.previous_subject + "\n"
        if args[:1] == ["commit-tree"]:
            return self.snapshot_commit + "\n"
        if args[:2] == ["merge-base", "--is-ancestor"] and self.merge_base_fails:
            raise PublishError("not an ancestor")
        # fetch / push (with or without the leading -c auth header)
        if "push" in args and self.push_error is not None:
            push_error = self.push_error
            self.push_error = None
            if self.remote_after_push_failure is not None:
                self.previous = self.remote_after_push_failure
            raise PublishError(push_error)
        return ""

    def push_call(self) -> list[str] | None:
        return next((call for call in self.calls if "push" in call), None)

@pytest.fixture(autouse=True)
def _token(monkeypatch) -> None:
    monkeypatch.setenv("BB_TOKEN", TOKEN)


# ---------------------------------------------------------------------------
# Message format
# ---------------------------------------------------------------------------


def test_publish_message_uses_injected_clock_and_short() -> None:
    git = FakeGit(short="cafe123")

    result = publish_snapshot(
        AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False, clock=FIXED_CLOCK
    )

    assert result.message == "Publish exact source commit cafe123 to bitbucket/main"


# ---------------------------------------------------------------------------
# Exact-source fast-forward wiring
# ---------------------------------------------------------------------------


def test_publish_preserves_exact_source_sha() -> None:
    git = FakeGit(previous="0f0f0f0f0f0f", source_commit="a" * 40)

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    assert result.snapshot == "a" * 40
    assert result.source_commit == result.snapshot
    assert result.previous_remote_commit == "0f0f0f0f0f0f"
    assert ["merge-base", "--is-ancestor", "0f0f0f0f0f0f", "a" * 40] in git.calls
    assert not any(call[0] in {"checkout", "reset", "commit", "commit-tree"} for call in git.calls)


def test_publish_pushes_with_bearer_header_and_exact_refspec() -> None:
    git = FakeGit(source_commit="a" * 40)

    publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    push = git.push_call()
    assert push[0] == "-c"
    assert push[1] == f"http.extraHeader=Authorization: Bearer {TOKEN}"
    assert push[2] == "push"
    assert push[3] == "bitbucket"
    assert push[4] == f"{'a' * 40}:refs/heads/main"


def test_publish_accepts_ambiguous_push_failure_when_remote_matches_source() -> None:
    source = "a" * 40
    git = FakeGit(
        source_commit=source,
        push_error=(
            "git -c failed (exit 1): error: RPC failed; curl 55 Send failure: Connection was reset\n"
            "send-pack: unexpected disconnect while reading sideband packet\n"
            "fatal: the remote end hung up unexpectedly\n"
            "Everything up-to-date"
        ),
        remote_after_push_failure=source,
    )

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    assert result.status == "published"
    assert result.snapshot == source
    assert git.calls.count(["-c", f"http.extraHeader=Authorization: Bearer {TOKEN}", "fetch", "bitbucket", "main"]) == 2


def test_publish_rejects_ambiguous_push_failure_when_remote_does_not_match_source() -> None:
    git = FakeGit(
        source_commit="a" * 40,
        push_error="git -c failed (exit 1): fatal: the remote end hung up unexpectedly",
        remote_after_push_failure="b" * 40,
    )

    with pytest.raises(PublishError, match="remote end hung up"):
        publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)


def test_publish_falls_back_to_snapshot_commit_when_bitbucket_rejects_github_commit() -> None:
    source = "a" * 40
    snapshot = "d" * 40
    previous = "0" * 40
    git = FakeGit(
        source_commit=source,
        short="cafe123",
        previous=previous,
        push_error=(
            "remote: You can only push your own commits in this repository\n"
            "remote: Commit f745066d6e14d36f2f858a31137b03f9fb2cd7c4 was committed by GitHub <noreply@github.com>\n"
            "! [remote rejected] main (pre-receive hook declined)"
        ),
        snapshot_commit=snapshot,
    )

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False, clock=FIXED_CLOCK)

    assert result.snapshot == snapshot
    assert result.source_commit == source
    assert result.previous_remote_commit == previous
    assert [
        "commit-tree",
        f"{source}^{{tree}}",
        "-p",
        previous,
        "-m",
        "Deploy snapshot: autobench cafe123 on main (2026-06-29 23:00 UTC) [edge-deploy]",
    ] in git.calls
    assert any(call[-1] == f"{snapshot}:refs/heads/main" for call in git.calls if "push" in call)


def test_publish_continues_existing_snapshot_chain_without_source_ancestry() -> None:
    source = "a" * 40
    snapshot = "d" * 40
    previous = "0" * 40
    git = FakeGit(
        source_commit=source,
        short="cafe123",
        previous=previous,
        merge_base_fails=True,
        snapshot_commit=snapshot,
    )

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False, clock=FIXED_CLOCK)

    assert result.snapshot == snapshot
    assert result.source_commit == source
    assert any(call[-1] == f"{snapshot}:refs/heads/main" for call in git.calls if "push" in call)


def test_publish_rejects_non_snapshot_remote_when_source_ancestry_fails() -> None:
    git = FakeGit(
        source_commit="a" * 40,
        merge_base_fails=True,
        previous_subject="not an edge deploy snapshot",
    )

    with pytest.raises(PublishError, match="non-fast-forward publish"):
        publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)


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


def test_publish_ignores_generated_release_reports_by_default() -> None:
    git = FakeGit(
        status=(
            "?? edge-deploy/reports/release-20260701T194538Z/release.json\n"
            "?? edge-deploy/reports/release-20260701T194538Z/release.log\n"
        )
    )

    result = publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git, run_local_check=False)

    assert result.status == "published"
    assert result.gate["clean_tree"] is True


def test_publish_rejects_other_edge_deploy_files_by_default() -> None:
    git = FakeGit(status="?? edge-deploy/config.yaml\n")

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


def test_local_check_failure_preserves_exit_code_and_output_tail(monkeypatch) -> None:
    git = FakeGit()
    monkeypatch.setattr(
        publish,
        "_run_local_check_ps1",
        lambda root: (7, "first detail\nfinal failure detail"),
    )

    with pytest.raises(LocalCheckError) as raised:
        publish_snapshot(AUTOBENCH, repo_root="/x", git_runner=git)

    assert raised.value.exit_code == 7
    assert raised.value.output_tail == "first detail\nfinal failure detail"
    assert "final failure detail" not in str(raised.value)
    assert not any(
        "fetch" in call or "push" in call or "commit" in call for call in git.calls
    )


def test_publish_runs_local_check_by_default() -> None:
    git = FakeGit()
    seen: list[Path] = []

    def local_check(root: Path) -> int:
        seen.append(Path(root))
        return 0

    result = publish_snapshot(
        AUTOBENCH,
        repo_root="/repo/autobench",
        git_runner=git,
        local_check_runner=local_check,
    )

    assert seen == [Path("/repo/autobench")]
    assert result.verification_source == "local-check"
    assert result.local_check_ran is True


def test_publish_commit_override_relaxes_tree_and_branch_gate() -> None:
    # Dirty tree AND off release branch, but --commit names a reviewed commit -> proceeds.
    git = FakeGit(status=" M benchmark.py", branch="feature/x")

    result = publish_snapshot(
        AUTOBENCH, repo_root="/x", git_runner=git, commit="deadbeefcafe", run_local_check=False
    )

    assert result.status == "published"
    assert result.gate["clean_tree"] is False
    assert result.gate["on_release_branch"] is False
    assert any(call == ["rev-parse", "--verify", "deadbeefcafe"] for call in git.calls)


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
    (work / "benchmark.py").write_text("print('v2')\n", encoding="utf-8")
    _git(["add", "."], work)
    _git(["commit", "-m", "reviewed change"], work)
    source = _git(["rev-parse", "HEAD"], work)

    result = publish_snapshot(
        AUTOBENCH, repo_root=work, run_local_check=False, clock=FIXED_CLOCK
    )

    assert result.status == "published"
    assert result.previous_remote_commit == previous
    # The bare remote's main now points at the exact reviewed source.
    remote_head = _git(["rev-parse", "main"], bare)
    assert remote_head == result.snapshot == source
    assert _git(["rev-parse", f"{result.snapshot}^"], work) == previous
