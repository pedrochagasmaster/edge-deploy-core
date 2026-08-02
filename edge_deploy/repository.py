"""Release-time validation of the canonical GitHub checkout."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

CommandRunner = Callable[[Sequence[str]], str]

_CI_WORKFLOW = "CI"
_API_TIMEOUT = 20.0

_TRANSIENT_GITHUB_MARKERS = (
    "unexpected eof",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "couldn't fetch",
    "could not resolve host",
    "tls handshake timeout",
    "temporary failure",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)


class RepositoryError(RuntimeError):
    """Raised when a checkout is not a valid release source."""


@dataclass(frozen=True)
class RepositoryState:
    root: Path
    tool: str
    commit: str
    origin_url: str
    bitbucket_url: str


def _normalize_url(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git").lower()


def _runner(root: Path) -> CommandRunner:
    def run(args: Sequence[str]) -> str:
        try:
            completed = subprocess.run(args, cwd=root, capture_output=True, text=True)
        except OSError as exc:
            # A missing `gh` or `git` is a setup problem with a clear remedy,
            # not a traceback for the operator to decode mid-release.
            raise RepositoryError(f"{args[0]} could not be run: {exc}") from exc
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RepositoryError(f"{args[0]} failed: {detail}")
        return completed.stdout

    return run


def _is_transient_github_error(exc: RepositoryError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_GITHUB_MARKERS)


def _is_generated_release_report_status(line: str) -> bool:
    return line.startswith("?? edge-deploy/reports/")


def _has_blocking_status(status: str) -> bool:
    return any(
        line.strip() and not _is_generated_release_report_status(line)
        for line in status.splitlines()
    )


def inspect_repository(
    root: Path,
    *,
    tool: str,
    expected_origin: str,
    expected_bitbucket: str,
    runner: CommandRunner | None = None,
) -> RepositoryState:
    """Require a clean local ``main`` exactly equal to ``origin/main``."""
    root = Path(root).resolve()
    run = runner or _runner(root)
    run(["git", "fetch", "origin", "main"])
    branch = run(["git", "branch", "--show-current"]).strip()
    if branch != "main":
        raise RepositoryError(f"release requires branch 'main', found {branch!r}")
    if _has_blocking_status(run(["git", "status", "--porcelain", "--untracked-files=all"])):
        raise RepositoryError("release requires a clean working tree")
    commit = run(["git", "rev-parse", "HEAD"]).strip()
    origin_main = run(["git", "rev-parse", "refs/remotes/origin/main"]).strip()
    if commit != origin_main:
        raise RepositoryError("local main must exactly match origin/main")

    origin_url = run(["git", "remote", "get-url", "origin"]).strip()
    bitbucket_url = run(["git", "remote", "get-url", "bitbucket"]).strip()
    if _normalize_url(origin_url) != _normalize_url(expected_origin):
        raise RepositoryError(f"origin points to unexpected repository: {origin_url}")
    if _normalize_url(bitbucket_url) != _normalize_url(expected_bitbucket):
        raise RepositoryError(f"bitbucket points to unexpected repository: {bitbucket_url}")
    return RepositoryState(root, tool, commit, origin_url, bitbucket_url)


def github_repo_path(remote_url: str) -> str | None:
    """``owner/repo`` for a GitHub remote, or None when it is not one."""
    remote_url = remote_url.strip()
    if remote_url.startswith("git@github.com:"):
        path = remote_url.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlsplit(remote_url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    return path if path.count("/") == 1 else None


def _git_credential_token(root: Path) -> str | None:
    """The token git already uses for github.com, via its credential helper."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    try:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            cwd=root,
            input=b"protocol=https\nhost=github.com\n\n",
            capture_output=True,
            timeout=_API_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode:
        return None
    for line in completed.stdout.splitlines():
        if line.startswith(b"password="):
            return line.removeprefix(b"password=").decode()
    return None


def _fetch_json(url: str, token: str) -> dict | None:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "edge-deploy-ci-gate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def github_ci_conclusions_via_api(
    root: Path,
    commit: str,
    *,
    run: CommandRunner | None = None,
    credential: Callable[[Path], str | None] | None = None,
    fetch: Callable[[str, str], dict | None] | None = None,
) -> list[str] | None:
    """CI conclusions for exactly ``commit``, read straight from the REST API.

    A conclusion exists only in GitHub's API — git publishes ``refs/heads``,
    ``refs/tags`` and ``refs/pull`` and nothing about checks — so this is the
    only way to answer without ``gh``. It uses the credential git's helper
    already holds, so it needs nothing the operator has not already given git.

    Returns ``None`` when the question could not be asked at all (not a GitHub
    remote, no credential, ``api.github.com`` unreachable). Callers must treat
    that as "unknown", never as "no CI run".
    """
    run = run or _runner(root)
    try:
        remote = run(["git", "remote", "get-url", "origin"]).strip()
    except RepositoryError:
        return None
    path = github_repo_path(remote)
    if not path:
        return None
    token = (credential or _git_credential_token)(root)
    if not token:
        return None
    payload = (fetch or _fetch_json)(
        f"https://api.github.com/repos/{path}/actions/runs"
        f"?head_sha={urllib.parse.quote(commit)}&per_page=20",
        token,
    )
    if payload is None:
        return None
    conclusions = []
    for workflow_run in payload.get("workflow_runs") or []:
        if workflow_run.get("name") != _CI_WORKFLOW:
            continue
        if workflow_run.get("status") != "completed":
            conclusions.append("pending")
        else:
            conclusions.append(str(workflow_run.get("conclusion") or ""))
    return conclusions


def require_successful_github_ci(
    state: RepositoryState,
    *,
    runner: CommandRunner | None = None,
    attempts: int = 3,
    retry_delay_seconds: float = 2.0,
    api_probe: Callable[..., list[str] | None] | None = None,
) -> None:
    """Require a successful GitHub CI run for the exact release SHA.

    ``gh`` is asked first. The REST API is a fallback for a machine that has
    git credentials but no ``gh`` — and only when ``gh`` could not answer at
    all. An answer from ``gh`` is final either way: a gate that shopped for a
    second opinion after a refusal would not be a gate.
    """
    run = runner or _runner(state.root)
    command = [
        "gh",
        "run",
        "list",
        "--commit",
        state.commit,
        "--branch",
        "main",
        "--workflow",
        _CI_WORKFLOW,
        "--json",
        "conclusion",
        "--limit",
        "20",
    ]
    conclusions: list[str] | None = None
    gh_error: RepositoryError | None = None
    for attempt in range(1, attempts + 1):
        try:
            output = run(command)
        except RepositoryError as exc:
            gh_error = exc
            if _is_transient_github_error(exc) and attempt < attempts:
                time.sleep(retry_delay_seconds)
                continue
            break
        try:
            conclusions = [str(item.get("conclusion") or "") for item in json.loads(output)]
        except json.JSONDecodeError as exc:
            gh_error = RepositoryError(f"GitHub CI status was not valid JSON: {exc}")
        break

    if conclusions is None:
        conclusions = (api_probe or github_ci_conclusions_via_api)(
            state.root, state.commit, run=run
        )
    if conclusions is None:
        # Neither source could answer: refuse, and keep gh's diagnosis.
        raise gh_error or RepositoryError("GitHub CI status could not be fetched")
    if "success" not in conclusions:
        raise RepositoryError(f"no successful post-merge GitHub CI run for {state.commit}")
