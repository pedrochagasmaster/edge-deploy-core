"""Release-time validation of the canonical GitHub checkout."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

CommandRunner = Callable[[Sequence[str]], str]


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
        completed = subprocess.run(args, cwd=root, capture_output=True, text=True)
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RepositoryError(f"{args[0]} failed: {detail}")
        return completed.stdout

    return run


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
    if run(["git", "status", "--porcelain"]).strip():
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


def require_successful_github_ci(
    state: RepositoryState,
    *,
    runner: CommandRunner | None = None,
) -> None:
    """Require a successful GitHub CI run for the exact release SHA."""
    run = runner or _runner(state.root)
    output = run(
        [
            "gh",
            "run",
            "list",
            "--commit",
            state.commit,
            "--branch",
            "main",
            "--workflow",
            "CI",
            "--json",
            "conclusion",
            "--limit",
            "20",
        ]
    )
    try:
        runs = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RepositoryError("GitHub CI status was not valid JSON") from exc
    if not any(item.get("conclusion") == "success" for item in runs):
        raise RepositoryError(f"no successful post-merge GitHub CI run for {state.commit}")
