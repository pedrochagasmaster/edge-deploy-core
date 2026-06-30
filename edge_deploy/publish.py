"""Publish: create and push exactly one **Snapshot** to a Tool's Bitbucket deployment
remote, returning its SHA. The only step that talks to Bitbucket.

A Snapshot is a commit whose *tree* is the reviewed source build and whose *parent* is
the current ``bitbucket/main``. It is built with ``git commit-tree`` (Phase-2 Plan
Recommendation 2): the snapshot commit object is created directly from the source tree
and the remote parent **without** touching the working tree, ``HEAD`` or the current
branch — so there is no detached-HEAD risk and no fragile ``finally``-restore dance.

Gate (Plan §1.1):

* **default** (``commit=None``): working tree clean **and** ``HEAD`` on the profile's
  ``release_branch`` **and** ``local_check.ps1`` exit 0; source = ``HEAD``.
* **``--commit <sha>``**: source = ``<sha>``; relaxes the clean-tree / on-branch
  requirement (the Operator is explicitly naming a reviewed commit) but still runs
  ``local_check`` unless ``run_local_check=False``.

Secret hygiene (ADR-0002): the ``BB_TOKEN`` is passed to git via ``-c http.extraHeader``
only on the push; it is never stored in :class:`PublishResult` or any report, and the
shared :func:`~edge_deploy.reporting.redact` masks ``token=`` for defence in depth.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from edge_deploy.config import ToolProfile

# A git seam: takes an argv (without the leading ``git``) and returns stdout, raising
# :class:`PublishError` on a nonzero exit. Injectable so tests never shell out to git.
GitRunner = Callable[[Sequence[str]], str]

LOCAL_CHECK_RELATIVE = Path("tools") / "dev" / "local_check.ps1"


class PublishError(RuntimeError):
    """Raised when a Publish cannot proceed (gate failure, git error, missing token)."""


@dataclass(frozen=True)
class PublishResult:
    """The outcome of one successful Publish (one Snapshot pushed to ``bitbucket/main``)."""

    tool: str
    status: str  # "published"
    snapshot: str  # new Snapshot SHA (now HEAD of bitbucket/main)
    source_commit: str
    source_short: str
    branch: str
    previous_remote_commit: str
    message: str
    gate: dict[str, bool] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """A compact, secret-free dict for the consolidated release report."""
        return {
            "tool": self.tool,
            "status": self.status,
            "snapshot": self.snapshot,
            "source_short": self.source_short,
            "branch": self.branch,
            "previous_remote_commit": self.previous_remote_commit,
            "message": self.message,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_snapshot_message(tool: str, source_short: str, branch: str, when: datetime) -> str:
    """The standardized, converged Snapshot message (Plan §1.1)."""
    return f"Deploy snapshot: {tool} {source_short} on {branch} ({when:%Y-%m-%d %H:%M}) [edge-deploy]"


def _resolve_powershell() -> str | None:
    """Return the first available PowerShell binary: ``pwsh`` (cross-platform) then ``powershell``."""
    for candidate in ("pwsh", "powershell"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def run_local_check_ps1(repo_root: Path) -> int:
    """Invoke the repo's committed ``tools/dev/local_check.ps1`` and return its exit code.

    The checks are deliberately tool-specific and maintained in-repo (Plan Recommendation
    3); the engine only needs the gate's exit code. Resolves ``pwsh`` then ``powershell``
    and **fails loudly** (rather than silently skipping the gate) when neither shell or the
    script is present — a silent skip would let an unverified build publish (Risk #6).
    """
    repo_root = Path(repo_root)
    script = repo_root / LOCAL_CHECK_RELATIVE
    if not script.is_file():
        raise PublishError(
            f"local_check gate script not found: {script}. "
            "Commit tools/dev/local_check.ps1 or pass --no-local-check to bypass the gate."
        )
    shell = _resolve_powershell()
    if shell is None:
        raise PublishError(
            "Cannot run the local_check gate: neither 'pwsh' nor 'powershell' is on PATH. "
            "Install PowerShell or pass --no-local-check to bypass the gate."
        )
    completed = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return completed.returncode


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
            # Never echo argv (it may carry the Bearer header); surface stderr only.
            raise PublishError(
                f"git {args[0] if args else ''} failed (exit {completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout

    return run


def reparent_snapshot(
    git: GitRunner,
    *,
    source: str,
    remote: str,
    branch: str,
    message: str,
    token: str,
) -> str:
    """Build the Snapshot with ``git commit-tree`` and push it via a Bearer token.

    No working-tree mutation: the snapshot commit is created from ``source``'s tree and the
    remote parent directly. Returns the new Snapshot SHA.
    """
    tree = git(["rev-parse", f"{source}^{{tree}}"]).strip()
    parent = git(["rev-parse", f"{remote}/{branch}"]).strip()
    snapshot = git(["commit-tree", tree, "-p", parent, "-m", message]).strip()
    auth_header = f"http.extraHeader=Authorization: Bearer {token}"
    git(["-c", auth_header, "push", remote, f"{snapshot}:refs/heads/{branch}"])
    return snapshot


def publish_snapshot(
    profile: ToolProfile,
    *,
    repo_root: str | Path,
    remote: str = "bitbucket",
    commit: str | None = None,
    token_env: str = "BB_TOKEN",
    run_local_check: bool = True,
    clock: Callable[[], datetime] = _utc_now,
    git_runner: GitRunner | None = None,
    local_check_runner: Callable[[Path], int] = run_local_check_ps1,
) -> PublishResult:
    """Publish one Snapshot for ``profile`` and return its SHA + provenance.

    Raises :class:`PublishError` on any gate failure or git error.
    """
    repo_root = Path(repo_root)
    git = git_runner if git_runner is not None else _default_git_runner(repo_root)
    branch = profile.release_branch or "main"

    token = os.environ.get(token_env, "")
    if not token:
        raise PublishError(
            f"{token_env} is not set in the environment; cannot authenticate the Bitbucket push."
        )

    clean_tree = git(["status", "--porcelain"]).strip() == ""
    current_branch = git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    on_release_branch = current_branch == branch

    if commit is None:
        source = "HEAD"
        if not clean_tree:
            raise PublishError(
                "Working tree is not clean; commit or stash before publishing "
                "(or name a reviewed commit with --commit <sha>)."
            )
        if not on_release_branch:
            raise PublishError(
                f"HEAD is not on the release branch {branch!r} (currently on {current_branch!r}); "
                f"checkout {branch} or name a reviewed commit with --commit <sha>."
            )
    else:
        # Relaxed gate: the Operator is explicitly naming a reviewed commit. local_check
        # still runs against the working tree (Risk #2) unless run_local_check=False.
        source = commit

    local_check_passed = True
    if run_local_check:
        code = local_check_runner(repo_root)
        local_check_passed = code == 0
        if not local_check_passed:
            raise PublishError(f"local_check.ps1 failed with exit code {code}")

    gate = {
        "clean_tree": clean_tree,
        "on_release_branch": on_release_branch,
        "local_check": local_check_passed,
    }

    source_commit = git(["rev-parse", "--verify", source]).strip()
    source_short = git(["rev-parse", "--short", source]).strip()

    # Refresh the remote-tracking ref so the Snapshot's parent is the *current*
    # bitbucket/main (authed fetch; http.extraHeader is ignored by non-http remotes).
    auth_header = f"http.extraHeader=Authorization: Bearer {token}"
    git(["-c", auth_header, "fetch", remote, branch])
    previous_remote_commit = git(["rev-parse", "--verify", f"{remote}/{branch}"]).strip()

    message = build_snapshot_message(profile.tool, source_short, branch, clock())
    snapshot = reparent_snapshot(
        git, source=source, remote=remote, branch=branch, message=message, token=token
    )

    return PublishResult(
        tool=profile.tool,
        status="published",
        snapshot=snapshot,
        source_commit=source_commit,
        source_short=source_short,
        branch=branch,
        previous_remote_commit=previous_remote_commit,
        message=message,
        gate=gate,
    )
