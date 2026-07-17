"""Run a tool repository's committed Windows verification gate."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from edge_deploy.python_env import repo_venv_python

LOCAL_CHECK_RELATIVE = Path("tools") / "dev" / "local_check.ps1"
LOCAL_CHECK_OUTPUT_TAIL_LINES = 20


class LocalCheckUnavailableError(RuntimeError):
    """Raised when the committed gate cannot be executed."""


@dataclass(frozen=True)
class LocalCheckResult:
    """Process result with a bounded, unredacted tail for the caller to handle."""

    exit_code: int
    output_tail: str


def _resolve_powershell() -> str | None:
    for candidate in ("pwsh", "powershell"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _output_tail_text(text: str, *, limit: int = LOCAL_CHECK_OUTPUT_TAIL_LINES) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def run_local_check(repo_root: Path) -> LocalCheckResult:
    """Execute ``tools/dev/local_check.ps1`` from *repo_root* exactly once."""
    repo_root = Path(repo_root)
    script = repo_root / LOCAL_CHECK_RELATIVE
    if not script.is_file():
        raise LocalCheckUnavailableError(
            f"committed local-check gate is missing: {LOCAL_CHECK_RELATIVE.as_posix()}"
        )
    shell = _resolve_powershell()
    if shell is None:
        raise LocalCheckUnavailableError(
            "cannot run the committed local-check gate: neither 'pwsh' nor "
            "'powershell' is on PATH"
        )

    venv_python = repo_venv_python(repo_root)
    shim_dir: Path | None = None
    env = os.environ.copy()
    if venv_python is not None:
        shim_dir = Path(tempfile.mkdtemp(prefix="edge-deploy-pyshim-", dir=repo_root))
        shim = shim_dir / "py.cmd"
        shim.write_text(f'@"{venv_python}" %*\n', encoding="utf-8")
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    try:
        try:
            completed = subprocess.run(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                env=env,
            )
        except OSError as exc:
            raise LocalCheckUnavailableError(
                "cannot start the committed local-check gate"
            ) from exc
        return LocalCheckResult(
            exit_code=completed.returncode,
            output_tail=_output_tail_text(completed.stdout + completed.stderr),
        )
    finally:
        if shim_dir is not None:
            shutil.rmtree(shim_dir, ignore_errors=True)
