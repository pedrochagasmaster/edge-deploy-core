"""Append redacted release evidence to Bitbucket's private ``release-log`` branch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from edge_deploy.reporting import redact


class AuditSyncError(RuntimeError):
    """Raised when durable audit state cannot be read or written."""


@dataclass(frozen=True)
class AuditAttempt:
    tool: str
    source_sha: str
    started_at: datetime
    report_dir: Path
    core_version: str
    operator: str
    status: str
    linked_attempt: str | None = None

    @property
    def attempt_id(self) -> str:
        stamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{self.source_sha[:7]}"


def default_outbox() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return base / "edge-deploy" / "outbox"


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AuditSyncError(f"git {args[0]} failed: {detail}")
    return completed


def _relative_path(attempt: AuditAttempt) -> Path:
    return (
        Path("releases")
        / attempt.tool
        / f"{attempt.started_at:%Y}"
        / f"{attempt.started_at:%m}"
        / attempt.attempt_id
    )


def _copy_redacted(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(path, target)
        else:
            target.write_text(redact(text), encoding="utf-8")


def check_audit_remote(
    core_repo: Path,
    *,
    outbox: Path | None = None,
    tool: str | None = None,
    source_sha: str | None = None,
) -> None:
    """Require the private audit remote to be reachable and locally synchronized."""
    pending = outbox or default_outbox()
    if pending.exists() and any(pending.iterdir()):
        raise AuditSyncError(f"unsynchronized audit records exist in {pending}")
    core_repo = Path(core_repo)
    _run(core_repo, "remote", "get-url", "bitbucket")
    remote = _run(core_repo, "ls-remote", "bitbucket", "refs/heads/release-log").stdout.strip()
    if not remote or not tool:
        return
    _run(
        core_repo,
        "fetch",
        "bitbucket",
        "refs/heads/release-log:refs/remotes/bitbucket/release-log",
    )
    paths = _run(
        core_repo,
        "ls-tree",
        "-r",
        "--name-only",
        "refs/remotes/bitbucket/release-log",
        f"releases/{tool}",
    ).stdout.splitlines()
    metadata_paths = sorted(path for path in paths if path.endswith("/metadata.json"))
    if not metadata_paths:
        return
    payload = json.loads(
        _run(
            core_repo,
            "show",
            f"refs/remotes/bitbucket/release-log:{metadata_paths[-1]}",
        ).stdout
    )
    if payload.get("status") != "passed" and payload.get("source_sha") != source_sha:
        raise AuditSyncError(
            f"latest {tool} release attempt is unresolved at {payload.get('source_sha')}; "
            "resume or roll it back before releasing a different SHA"
        )


def append_audit_attempt(
    core_repo: Path,
    attempt: AuditAttempt,
    *,
    outbox: Path | None = None,
) -> str:
    """Append one immutable attempt and push only to Bitbucket ``release-log``."""
    core_repo = Path(core_repo).resolve()
    pending = outbox or default_outbox()
    worktree = Path(tempfile.mkdtemp(prefix="edge-deploy-audit-"))
    worktree.rmdir()
    added = False
    destination: Path | None = None
    try:
        remote = _run(
            core_repo,
            "ls-remote",
            "--heads",
            "bitbucket",
            "refs/heads/release-log",
        ).stdout.strip()
        if remote:
            _run(
                core_repo,
                "fetch",
                "bitbucket",
                "refs/heads/release-log:refs/remotes/bitbucket/release-log",
            )
            _run(core_repo, "worktree", "add", "--detach", str(worktree), "refs/remotes/bitbucket/release-log")
        else:
            _run(core_repo, "worktree", "add", "--detach", str(worktree), "HEAD")
            _run(worktree, "switch", "--orphan", "release-log")
        added = True

        destination = worktree / _relative_path(attempt)
        _copy_redacted(attempt.report_dir, destination)
        metadata = asdict(attempt)
        metadata["started_at"] = attempt.started_at.isoformat()
        metadata["report_dir"] = "."
        (destination / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _run(worktree, "add", str(_relative_path(attempt)))
        _run(worktree, "commit", "-m", f"audit: {attempt.tool} {attempt.attempt_id} {attempt.status}")
        commit = _run(worktree, "rev-parse", "HEAD").stdout.strip()
        _run(worktree, "push", "bitbucket", "HEAD:refs/heads/release-log")
        return commit
    except Exception:
        if destination is not None and destination.exists():
            preserved = pending / attempt.attempt_id
            if not preserved.exists():
                _copy_redacted(destination, preserved)
        raise
    finally:
        if added:
            _run(core_repo, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
