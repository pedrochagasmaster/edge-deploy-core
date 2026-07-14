"""Resolve the Python environment owned by a tool repository."""

from __future__ import annotations

from pathlib import Path


def repo_venv_python(repo_root: str | Path) -> Path | None:
    """Return the repository virtualenv interpreter when one exists."""

    root = Path(repo_root)
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)
