"""Publish the reviewed GitHub commit to a Tool's Bitbucket ``main``.

Publish is fast-forward-only. It never rewrites commits, force-pushes, or mutates the
working tree. When Bitbucket allows it, the reviewed GitHub commit itself is pushed.
When Bitbucket's "own commits only" hook rejects GitHub-authored merge commits, Publish
creates an operator-authored deployment snapshot commit with the reviewed commit's tree
and the Bitbucket tip as its parent.

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
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from edge_deploy.config import ToolProfile
from edge_deploy.local_check import LocalCheckUnavailableError
from edge_deploy.local_check import run_local_check as execute_local_check

# A git seam: takes an argv (without the leading ``git``) and returns stdout, raising
# :class:`PublishError` on a nonzero exit. Injectable so tests never shell out to git.
GitRunner = Callable[[Sequence[str]], str]

AMBIGUOUS_PUSH_FAILURE_MARKERS = (
    "connection was reset",
    "unexpected disconnect",
    "remote end hung up",
    "everything up-to-date",
)


class PublishError(RuntimeError):
    """Raised when a Publish cannot proceed (gate failure, git error, missing token)."""


class LocalCheckError(PublishError):
    """A failed local check whose captured output must be redacted before use."""

    def __init__(self, exit_code: int, output_tail: str) -> None:
        self.exit_code = exit_code
        self.output_tail = output_tail
        super().__init__(f"local_check.ps1 failed with exit code {exit_code}")


@dataclass(frozen=True)
class PublishResult:
    """The outcome of one reviewed source commit published to ``bitbucket/main``."""

    tool: str
    status: str  # "published"
    snapshot: str  # deployment SHA, retained as a report-schema compatibility key
    source_commit: str
    source_short: str
    branch: str
    previous_remote_commit: str
    message: str
    gate: dict[str, bool] = field(default_factory=dict)
    local_check_output_tail: str = ""
    verification_source: str = "local-check"
    local_check_ran: bool = True

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
            "verification_source": self.verification_source,
            "local_check_ran": self.local_check_ran,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_generated_release_report_status(line: str) -> bool:
    return line.startswith("?? edge-deploy/reports/")


def _has_blocking_status(status: str) -> bool:
    return any(
        line.strip() and not _is_generated_release_report_status(line)
        for line in status.splitlines()
    )


def _is_ambiguous_push_failure(exc: PublishError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in AMBIGUOUS_PUSH_FAILURE_MARKERS)


def _is_own_commit_hook_rejection(exc: PublishError) -> bool:
    message = str(exc).lower()
    return "you can only push your own commits" in message and "pre-receive hook declined" in message


def _is_deployment_snapshot_subject(subject: str) -> bool:
    return subject.startswith("Deploy snapshot:")


def _deployment_snapshot_message(profile: ToolProfile, source_short: str, branch: str, now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"Deploy snapshot: {profile.tool} {source_short} on {branch} ({stamp} UTC) [edge-deploy]"


def run_local_check_ps1(repo_root: Path) -> int:
    """Invoke the repo's committed ``tools/dev/local_check.ps1`` and return its exit code.

    The checks are deliberately tool-specific and maintained in-repo (Plan Recommendation
    3); the engine only needs the gate's exit code. Resolves ``pwsh`` then ``powershell``
    and **fails loudly** (rather than silently skipping the gate) when neither shell or the
    script is present — a silent skip would let an unverified build publish (Risk #6).
    """
    try:
        return execute_local_check(repo_root).exit_code
    except LocalCheckUnavailableError as exc:
        raise PublishError(f"{exc}; pass --no-local-check to bypass the publish gate") from exc


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
                part.strip()
                for part in (completed.stderr, completed.stdout)
                if part.strip()
            )
            raise PublishError(
                f"git {args[0] if args else ''} failed (exit {completed.returncode}): "
                f"{detail}"
            )
        return completed.stdout

    return run


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
    """Publish one reviewed source commit for ``profile`` and return its provenance.

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

    clean_tree = not _has_blocking_status(git(["status", "--porcelain", "--untracked-files=all"]))
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
    local_check_output_tail = ""
    if run_local_check:
        if local_check_runner is run_local_check_ps1:
            try:
                result = execute_local_check(repo_root)
            except LocalCheckUnavailableError as exc:
                raise PublishError(
                    f"{exc}; pass --no-local-check to bypass the publish gate"
                ) from exc
            code = result.exit_code
            local_check_output_tail = result.output_tail
        else:
            code = local_check_runner(repo_root)
        local_check_passed = code == 0
        if not local_check_passed:
            raise LocalCheckError(code, local_check_output_tail)

    gate = {
        "clean_tree": clean_tree,
        "on_release_branch": on_release_branch,
        "local_check": local_check_passed,
    }

    source_commit = git(["rev-parse", "--verify", source]).strip()
    source_short = git(["rev-parse", "--short", source]).strip()

    # Refresh and require either direct ancestry for exact-SHA publication, or an
    # existing deployment-snapshot Bitbucket tip for continuing the synthetic chain.
    auth_header = f"http.extraHeader=Authorization: Bearer {token}"
    git(["-c", auth_header, "fetch", remote, branch])
    previous_remote_commit = git(["rev-parse", "--verify", f"{remote}/{branch}"]).strip()
    remote_is_source_ancestor = True
    try:
        git(["merge-base", "--is-ancestor", previous_remote_commit, source_commit])
    except PublishError as exc:
        remote_is_source_ancestor = False
        previous_subject = git(["log", "-1", "--format=%s", previous_remote_commit]).strip()
        if not _is_deployment_snapshot_subject(previous_subject):
            raise PublishError(
                f"{remote}/{branch} is not an ancestor of {source_commit}; refusing a "
                "non-fast-forward publish. Preserve the legacy tip and perform the one-time "
                "operator migration before retrying."
            ) from exc

    message = f"Publish exact source commit {source_short} to {remote}/{branch}"
    deployment_commit = source_commit
    if remote_is_source_ancestor:
        try:
            git(["-c", auth_header, "push", remote, f"{source_commit}:refs/heads/{branch}"])
        except PublishError as exc:
            if _is_own_commit_hook_rejection(exc):
                remote_is_source_ancestor = False
            elif _is_ambiguous_push_failure(exc):
                git(["-c", auth_header, "fetch", remote, branch])
                confirmed_remote_commit = git(["rev-parse", "--verify", f"{remote}/{branch}"]).strip()
                if confirmed_remote_commit != source_commit:
                    raise
            else:
                raise

    if not remote_is_source_ancestor:
        message = _deployment_snapshot_message(profile, source_short, branch, clock())
        deployment_commit = git(
            ["commit-tree", f"{source_commit}^{{tree}}", "-p", previous_remote_commit, "-m", message]
        ).strip()
        try:
            git(["-c", auth_header, "push", remote, f"{deployment_commit}:refs/heads/{branch}"])
        except PublishError as exc:
            if not _is_ambiguous_push_failure(exc):
                raise
            git(["-c", auth_header, "fetch", remote, branch])
            confirmed_remote_commit = git(["rev-parse", "--verify", f"{remote}/{branch}"]).strip()
            if confirmed_remote_commit != deployment_commit:
                raise

    return PublishResult(
        tool=profile.tool,
        status="published",
        snapshot=deployment_commit,
        source_commit=source_commit,
        source_short=source_short,
        branch=branch,
        previous_remote_commit=previous_remote_commit,
        message=message,
        gate=gate,
        local_check_output_tail=local_check_output_tail,
        verification_source="local-check" if run_local_check else "operator-bypass",
        local_check_ran=run_local_check,
    )
