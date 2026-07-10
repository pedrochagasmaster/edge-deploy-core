from __future__ import annotations

import posixpath
import shlex
from pathlib import PurePosixPath

EDGE_DEPLOY_ROOT = "~/.edge-deploy"


def _safe_part(part: str) -> str:
    candidate = PurePosixPath(part)
    if not part or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"expected a safe relative POSIX path, got {part!r}")
    return candidate.as_posix()


def edge_deploy_path(*parts: str) -> str:
    cleaned = [_safe_part(part) for part in parts]
    return posixpath.join(EDGE_DEPLOY_ROOT, *cleaned)


def shell_remote_path(remote_path: str) -> str:
    if remote_path == "~":
        return "$HOME"
    if remote_path.startswith("~/"):
        return f"$HOME/{shlex.quote(remote_path[2:])}"
    return shlex.quote(remote_path)


def resolve_home_path(remote_path: str, home: str) -> str:
    if not home.startswith("/") or "\n" in home or "\r" in home:
        raise ValueError("remote home must be an absolute single-line POSIX path")
    if remote_path == "~":
        return home
    if remote_path.startswith("~/"):
        return posixpath.join(home, remote_path[2:])
    if remote_path.startswith("/"):
        return remote_path
    raise ValueError(
        f"remote path must be absolute or use the current user's home, got {remote_path!r}"
    )
