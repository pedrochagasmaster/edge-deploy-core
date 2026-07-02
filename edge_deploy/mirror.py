"""Mirror a reviewed core release tag from GitHub to Bitbucket seamlessly.

GitHub is the review source of truth; Bitbucket is the delivery mirror. Bitbucket's
server-side pre-receive hook only accepts commits committed by the pushing operator,
so GitHub-authored PR merge commits (``GitHub <noreply@github.com>``) can never be
pushed verbatim. Mirror therefore guarantees **tree equivalence**, not commit-SHA
equality (ADR-0007):

* When the exact reviewed commit is operator-committed, it is pushed as-is together
  with the original annotated tag ("exact" mode).
* When Bitbucket's own-commits hook rejects the exact push, Mirror creates an
  operator-authored mirror commit carrying the reviewed commit's exact tree (parented
  on the current Bitbucket tip, fast-forward only) and an operator-authored annotated
  tag pointing at it. Both messages carry full provenance: the source commit SHA and
  the shared tree SHA ("mirrored" mode).

Secret hygiene (ADR-0002): when ``BB_TOKEN`` is set it is passed to git only via
``-c http.extraHeader`` and never stored in :class:`MirrorResult` or any report.
When unset, git's ambient credential helper is used.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

GitRunner = Callable[[Sequence[str]], str]

MIRROR_SUBJECT_PREFIX = "Mirror release"


class MirrorError(RuntimeError):
    """Raised when a release tag cannot be mirrored to Bitbucket."""


@dataclass(frozen=True)
class MirrorResult:
    """The verified outcome of one release-tag mirror to Bitbucket."""

    tag: str
    mode: str  # "exact" | "mirrored"
    branch: str
    source_commit: str
    deployed_commit: str
    tree: str
    previous_remote_commit: str
    message: str

    def to_payload(self) -> dict[str, Any]:
        """A compact, secret-free dict for audit records."""
        return {
            "tag": self.tag,
            "mode": self.mode,
            "branch": self.branch,
            "source_commit": self.source_commit,
            "deployed_commit": self.deployed_commit,
            "tree": self.tree,
            "previous_remote_commit": self.previous_remote_commit,
            "message": self.message,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_git_runner(repo_root: str | Path) -> GitRunner:
    """Build a :data:`GitRunner` that shells out to real ``git`` in ``repo_root``."""
    root = str(repo_root)

    def run(args: Sequence[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            # Never echo argv (it may carry the Bearer header); surface command output only.
            detail = "\n".join(
                part.strip() for part in (completed.stderr, completed.stdout) if part.strip()
            )
            raise MirrorError(
                f"git {args[0] if args else ''} failed (exit {completed.returncode}): {detail}"
            )
        return completed.stdout

    return run


def _is_own_commit_hook_rejection(exc: Exception) -> bool:
    message = str(exc).lower()
    return "you can only push your own commits" in message and "pre-receive hook declined" in message


def _parse_ls_remote_tag(output: str, tag: str) -> str | None:
    """Return the commit a remote tag ultimately points at (dereferenced when annotated)."""
    plain: str | None = None
    dereferenced: str | None = None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == f"refs/tags/{tag}^{{}}":
            dereferenced = sha
        elif ref == f"refs/tags/{tag}":
            plain = sha
    return dereferenced or plain


def _remote_ref_shas(git: GitRunner, remote: str, tag: str, branch: str) -> tuple[str | None, str | None]:
    output = git(
        ["ls-remote", remote, f"refs/heads/{branch}", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]
    )
    branch_sha: str | None = None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
            branch_sha = parts[0]
    return branch_sha, _parse_ls_remote_tag(output, tag)


def mirror_release(
    repo_root: str | Path,
    *,
    tag: str,
    remote: str = "bitbucket",
    branch: str = "main",
    source_remote: str = "origin",
    token_env: str = "BB_TOKEN",
    clock: Callable[[], datetime] = _utc_now,
    git_runner: GitRunner | None = None,
) -> MirrorResult:
    """Mirror one immutable release ``tag`` to ``remote`` and verify the result.

    Raises :class:`MirrorError` on any provenance, fast-forward, or push failure.
    """
    git = git_runner if git_runner is not None else _default_git_runner(Path(repo_root))

    token = os.environ.get(token_env, "")
    push_prefix = ["-c", f"http.extraHeader=Authorization: Bearer {token}"] if token else []

    source_commit = git(["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"]).strip()
    tree = git(["rev-parse", "--verify", f"{source_commit}^{{tree}}"]).strip()

    # The local tag must match the review source of truth before anything is mirrored.
    origin_target = _parse_ls_remote_tag(
        git(["ls-remote", source_remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]), tag
    )
    if origin_target != source_commit:
        raise MirrorError(
            f"local tag {tag!r} targets {source_commit} but {source_remote} targets "
            f"{origin_target}; refusing to mirror a tag that diverged from the review source"
        )

    git([*push_prefix, "fetch", remote, branch])
    previous_remote_commit = git(["rev-parse", "--verify", f"{remote}/{branch}"]).strip()

    exact_possible = True
    try:
        git(["merge-base", "--is-ancestor", previous_remote_commit, source_commit])
    except MirrorError as exc:
        exact_possible = False
        previous_subject = git(["log", "-1", "--format=%s", previous_remote_commit]).strip()
        if not previous_subject.startswith(MIRROR_SUBJECT_PREFIX):
            raise MirrorError(
                f"{remote}/{branch} is neither an ancestor of {source_commit} nor a previous "
                "mirror commit; refusing a non-fast-forward mirror. Inspect the Bitbucket tip "
                "before retrying."
            ) from exc

    mode = "exact"
    deployed_commit = source_commit
    message = f"{MIRROR_SUBJECT_PREFIX} {tag}: exact source {source_commit}"
    if exact_possible:
        try:
            git(
                [
                    *push_prefix,
                    "push",
                    remote,
                    f"{source_commit}:refs/heads/{branch}",
                    f"refs/tags/{tag}:refs/tags/{tag}",
                ]
            )
        except MirrorError as exc:
            if not _is_own_commit_hook_rejection(exc):
                raise
            exact_possible = False

    if not exact_possible:
        mode = "mirrored"
        stamp = clock().astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        message = (
            f"{MIRROR_SUBJECT_PREFIX} {tag}: source {source_commit} tree {tree} "
            f"({stamp} UTC) [edge-deploy]"
        )
        deployed_commit = git(["commit-tree", tree, "-p", previous_remote_commit, "-m", message]).strip()
        temp_tag = f"edge-deploy-mirror/{tag}"
        git(["tag", "-a", "-f", temp_tag, deployed_commit, "-m", message])
        try:
            deployed_tag_object = git(["rev-parse", "--verify", f"refs/tags/{temp_tag}"]).strip()
            git(
                [
                    *push_prefix,
                    "push",
                    remote,
                    f"{deployed_commit}:refs/heads/{branch}",
                    f"{deployed_tag_object}:refs/tags/{tag}",
                ]
            )
        finally:
            git(["tag", "-d", temp_tag])

    remote_branch_sha, remote_tag_target = _remote_ref_shas(git, remote, tag, branch)
    if remote_branch_sha != deployed_commit or remote_tag_target != deployed_commit:
        raise MirrorError(
            f"post-push verification failed: {remote}/{branch} is {remote_branch_sha} and "
            f"tag {tag!r} targets {remote_tag_target}, expected {deployed_commit}"
        )
    deployed_tree = git(["rev-parse", "--verify", f"{deployed_commit}^{{tree}}"]).strip()
    if deployed_tree != tree:
        raise MirrorError(
            f"post-push verification failed: deployed tree {deployed_tree} does not match "
            f"reviewed source tree {tree}"
        )

    return MirrorResult(
        tag=tag,
        mode=mode,
        branch=branch,
        source_commit=source_commit,
        deployed_commit=deployed_commit,
        tree=tree,
        previous_remote_commit=previous_remote_commit,
        message=message,
    )
