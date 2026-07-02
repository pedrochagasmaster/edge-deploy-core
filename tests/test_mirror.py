"""Tree-equivalent release-tag mirroring tests (ADR-0007)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from edge_deploy.mirror import MirrorError, mirror_release

TOKEN = "s3cr3t-bearer-token"
FIXED_CLOCK = lambda: datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc)  # noqa: E731

SOURCE = "a" * 40
TREE = "b" * 40
PREVIOUS = "0" * 40
MIRROR_COMMIT = "d" * 40
TAG_OBJECT = "e" * 40

OWN_COMMIT_REJECTION = (
    "git push failed (exit 1): remote: You can only push your own commits in this repository\n"
    "remote: Commit " + SOURCE + " was committed by GitHub <noreply@github.com>\n"
    "! [remote rejected] main (pre-receive hook declined)"
)


class FakeGit:
    """A scriptable ``git_runner`` that records argv and returns canned stdout by content."""

    def __init__(
        self,
        *,
        origin_tag_target: str = SOURCE,
        previous: str = PREVIOUS,
        push_error: str | None = None,
        merge_base_fails: bool = False,
        previous_subject: str = "Mirror release v1.0.0: source 123 tree 456 (2026-06-01 00:00 UTC) [edge-deploy]",
        remote_branch_after: str | None = None,
        remote_tag_after: str | None = None,
        deployed_tree: str = TREE,
    ) -> None:
        self.calls: list[list[str]] = []
        self.origin_tag_target = origin_tag_target
        self.previous = previous
        self.push_error = push_error
        self.merge_base_fails = merge_base_fails
        self.previous_subject = previous_subject
        self.remote_branch_after = remote_branch_after
        self.remote_tag_after = remote_tag_after
        self.deployed_tree = deployed_tree
        self.pushed: list[list[str]] = []

    def __call__(self, args) -> str:
        args = list(args)
        self.calls.append(args)
        stripped = args[2:] if args[:1] == ["-c"] else args
        if stripped[:2] == ["rev-parse", "--verify"]:
            ref = stripped[2]
            if ref.endswith("^{tree}"):
                return (self.deployed_tree if ref.startswith(MIRROR_COMMIT) else TREE) + "\n"
            if ref.startswith("refs/tags/edge-deploy-mirror/"):
                return TAG_OBJECT + "\n"
            if "/" in ref and not ref.startswith("refs/tags/"):
                return self.previous + "\n"
            return SOURCE + "\n"
        if stripped[:2] == ["ls-remote", "origin"]:
            return (
                f"{TAG_OBJECT}\trefs/tags/v1.1.0\n"
                f"{self.origin_tag_target}\trefs/tags/v1.1.0^{{}}\n"
            )
        if stripped[:2] == ["ls-remote", "bitbucket"]:
            branch = self.remote_branch_after
            tag = self.remote_tag_after
            if branch is None or tag is None:
                deployed = self._deployed_from_pushes()
                branch = branch or deployed
                tag = tag or deployed
            return (
                f"{branch}\trefs/heads/main\n"
                f"{TAG_OBJECT}\trefs/tags/v1.1.0\n"
                f"{tag}\trefs/tags/v1.1.0^{{}}\n"
            )
        if stripped[:3] == ["log", "-1", "--format=%s"]:
            return self.previous_subject + "\n"
        if stripped[:1] == ["commit-tree"]:
            return MIRROR_COMMIT + "\n"
        if stripped[:2] == ["merge-base", "--is-ancestor"] and self.merge_base_fails:
            raise MirrorError("not an ancestor")
        if "push" in stripped:
            if self.push_error is not None:
                error = self.push_error
                self.push_error = None
                raise MirrorError(error)
            self.pushed.append(stripped)
        return ""

    def _deployed_from_pushes(self) -> str:
        for call in self.pushed:
            for arg in call:
                if arg.endswith(":refs/heads/main"):
                    return arg.split(":", 1)[0]
        return self.previous


@pytest.fixture(autouse=True)
def _token(monkeypatch) -> None:
    monkeypatch.setenv("BB_TOKEN", TOKEN)


def test_mirror_pushes_exact_commit_and_tag_when_accepted() -> None:
    git = FakeGit()

    result = mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)

    assert result.mode == "exact"
    assert result.deployed_commit == SOURCE
    assert result.tree == TREE
    assert [
        "push",
        "bitbucket",
        f"{SOURCE}:refs/heads/main",
        "refs/tags/v1.1.0:refs/tags/v1.1.0",
    ] in git.pushed
    assert not any(call[:1] == ["commit-tree"] for call in git.calls)


def test_mirror_uses_bearer_header_only_when_token_present(monkeypatch) -> None:
    git = FakeGit()

    mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)

    push = next(call for call in git.calls if "push" in call)
    assert push[:2] == ["-c", f"http.extraHeader=Authorization: Bearer {TOKEN}"]

    monkeypatch.delenv("BB_TOKEN")
    git = FakeGit()
    mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)
    push = next(call for call in git.calls if "push" in call)
    assert push[0] == "push"


def test_mirror_falls_back_to_operator_mirror_commit_on_own_commit_rejection() -> None:
    git = FakeGit(push_error=OWN_COMMIT_REJECTION)

    result = mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)

    assert result.mode == "mirrored"
    assert result.source_commit == SOURCE
    assert result.deployed_commit == MIRROR_COMMIT
    assert result.previous_remote_commit == PREVIOUS
    expected_message = (
        f"Mirror release v1.1.0: source {SOURCE} tree {TREE} (2026-07-02 01:30 UTC) [edge-deploy]"
    )
    assert result.message == expected_message
    assert ["commit-tree", TREE, "-p", PREVIOUS, "-m", expected_message] in git.calls
    assert [
        "push",
        "bitbucket",
        f"{MIRROR_COMMIT}:refs/heads/main",
        f"{TAG_OBJECT}:refs/tags/v1.1.0",
    ] in git.pushed
    assert ["tag", "-d", "edge-deploy-mirror/v1.1.0"] in git.calls  # temp tag cleaned up


def test_mirror_continues_existing_mirror_chain_without_ancestry() -> None:
    git = FakeGit(merge_base_fails=True)

    result = mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)

    assert result.mode == "mirrored"
    assert result.deployed_commit == MIRROR_COMMIT


def test_mirror_refuses_non_fast_forward_over_foreign_tip() -> None:
    git = FakeGit(merge_base_fails=True, previous_subject="some unrelated commit")

    with pytest.raises(MirrorError, match="non-fast-forward"):
        mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)


def test_mirror_refuses_tag_diverged_from_origin() -> None:
    git = FakeGit(origin_tag_target="f" * 40)

    with pytest.raises(MirrorError, match="diverged from the review source"):
        mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)


def test_mirror_reraises_non_hook_push_failures() -> None:
    git = FakeGit(push_error="git push failed (exit 1): fatal: unable to access")

    with pytest.raises(MirrorError, match="unable to access"):
        mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)


def test_mirror_fails_verification_when_remote_refs_do_not_match() -> None:
    git = FakeGit(remote_branch_after="9" * 40, remote_tag_after="9" * 40)

    with pytest.raises(MirrorError, match="post-push verification failed"):
        mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)


def test_mirror_result_payload_is_secret_free() -> None:
    git = FakeGit(push_error=OWN_COMMIT_REJECTION)

    result = mirror_release("/core", tag="v1.1.0", git_runner=git, clock=FIXED_CLOCK)
    payload = result.to_payload()

    assert TOKEN not in str(payload)
    assert payload["mode"] == "mirrored"
    assert payload["source_commit"] == SOURCE
    assert payload["deployed_commit"] == MIRROR_COMMIT
