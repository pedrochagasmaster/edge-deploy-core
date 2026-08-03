"""What will refuse a command before the operator presses the button.

The engine turns a release away for several reasons that are knowable from
local state: the run was created by a different engine build, another process
holds the run lock, ``BB_TOKEN`` is not in the environment the console will
hand to the child, the operator config is missing, or a node in the ledger is
no longer configured. Every one of those is a refusal the console can show
instead of letting the operator discover it — sometimes several minutes and a
posture switch into a guided release.

Nothing here runs a release command or writes anything. The engine identity is
read by asking the same source-bound child the console buttons will spawn, so
``--engine-python`` can change the interpreter without changing which
``edge_deploy`` source is under test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from edge_console.engine_exec import EngineExecContext, engine_identity_argv
from edge_deploy.audit import default_outbox
from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, load_operator_config

READINESS_CACHE_SECONDS = 30.0
IDENTITY_TIMEOUT = 20.0
BB_TOKEN_ENV = "BB_TOKEN"


def probe_engine_identity(engine: EngineExecContext, cwd: Path | None = None) -> dict:
    """The engine a console button would run: version and content hash.

    Uses the same source-bound argv as engine actions. ``cwd`` is still the
    tool checkout actions use; the bootstrap inserts ``engine.source_root`` at
    the front of ``sys.path`` so the checkout cannot supply a different package.
    """
    try:
        completed = subprocess.run(
            engine_identity_argv(engine),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=IDENTITY_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "detail": f"could not run {engine.python}: {exc}"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return {
            "status": "unknown",
            "detail": detail[-1] if detail else f"exit {completed.returncode}",
        }
    try:
        identity = json.loads(completed.stdout)
    except ValueError:
        return {"status": "unknown", "detail": "engine identity was not valid JSON"}
    return {
        "status": "ok",
        "version": identity.get("version"),
        "content_sha256": identity.get("content_sha256"),
        "package_dir": identity.get("package_dir"),
    }


def probe_operator_config(path: Path | None = None) -> dict:
    """Whether the config every engine command needs is there and loadable.

    Only ``status`` and the git actions survive without it; everything else
    exits 2, and a malformed file is an uncaught traceback.
    """
    config_path = Path(path or DEFAULT_OPERATOR_CONFIG_PATH)
    if not config_path.is_file():
        return {"status": "missing", "path": str(config_path), "nodes": []}
    try:
        operator = load_operator_config(config_path)
    except Exception as exc:
        return {
            "status": "invalid",
            "path": str(config_path),
            "detail": f"{type(exc).__name__}: {exc}",
            "nodes": [],
        }
    return {
        "status": "ok",
        "path": str(config_path),
        # name -> transport: the console drives Paramiko nodes only, because a
        # pane node's RSA passcode is typed in the tmux pane, which it cannot see.
        "nodes": {name: node.transport for name, node in sorted(operator.nodes.items())},
        "audit_repo": operator.audit_repo,
    }


def probe_powershell() -> dict:
    """Verify runs the tool's committed ``local_check.ps1`` through PowerShell.

    Without one, verify fails with a message that cannot distinguish this from
    a missing script, and with an empty diagnostic artifact.
    """
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return {"present": True, "path": found}
    return {"present": False, "path": None}


def probe_audit(audit_repo: str, outbox: Path | None = None) -> dict:
    """Publish refuses without an audit checkout, or with unsent records queued."""
    pending = Path(outbox) if outbox else default_outbox()
    try:
        queued = pending.is_dir() and any(pending.iterdir())
    except OSError:
        queued = False
    return {"repo": audit_repo or None, "outbox": str(pending), "queued": bool(queued)}


def probe_bb_token() -> dict:
    """``BB_TOKEN`` is read from the environment the child inherits — ours.

    An operator who exported it in some other shell does not help here, which
    is exactly why this is worth saying out loud before publish refuses.
    """
    return {"present": bool(os.environ.get(BB_TOKEN_ENV))}


class ReadinessProber:
    """Cached view of the preconditions that apply to every watched checkout."""

    def __init__(
        self,
        *,
        engine: EngineExecContext | None = None,
        probe_root: Path | None = None,
        demo: bool = False,
        demo_engine_sha: str | None = None,
    ) -> None:
        self._engine = engine
        self._probe_root = probe_root
        self._demo = demo
        self._demo_engine_sha = demo_engine_sha
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def node_transports(self) -> dict[str, str]:
        """Just the node→transport map, without the engine-identity subprocess.

        The pane-transport refusal needs this on the request path, and paying
        the ~20s identity probe there would stall the first action after
        startup. The operator config is a local file read.
        """
        if self._demo:
            return {node: "ssh" for node in ("node03", "node04", "node05")}
        return probe_operator_config().get("nodes") or {}

    def snapshot(self) -> dict:
        if self._demo:
            # --demo drives the simulator, not edge_deploy: reporting the real
            # engine's hash here would mark every fabricated run as orphaned.
            return {
                "engine": {
                    "status": "ok",
                    "version": "demo",
                    "content_sha256": self._demo_engine_sha,
                    "package_dir": "(demo)",
                },
                "operator_config": {
                    "status": "ok",
                    "path": "(demo)",
                    "nodes": {node: "ssh" for node in ("node03", "node04", "node05")},
                    "audit_repo": "(demo)",
                },
                "bb_token": {"present": True},
                "powershell": {"present": True, "path": "(demo)"},
                "audit": {"repo": "(demo)", "outbox": "(demo)", "queued": False},
                "engine_python": "(demo simulator)",
                "engine_source": "(demo simulator)",
            }
        if self._engine is None:
            # Fail closed: without a bound source there is no honest identity.
            return {
                "engine": {
                    "status": "unknown",
                    "detail": "engine source is not bound",
                },
                "operator_config": probe_operator_config(),
                "bb_token": probe_bb_token(),
                "powershell": probe_powershell(),
                "audit": {"repo": None, "outbox": None, "queued": False},
                "engine_python": None,
                "engine_source": None,
            }
        with self._lock:
            if self._cached and time.monotonic() - self._cached_at < READINESS_CACHE_SECONDS:
                return self._cached
        config = probe_operator_config()
        result = {
            "engine": probe_engine_identity(self._engine, cwd=self._probe_root),
            "operator_config": config,
            "bb_token": probe_bb_token(),
            "powershell": probe_powershell(),
            "audit": probe_audit(config.get("audit_repo") or ""),
            "engine_python": self._engine.python,
            "engine_source": self._engine.source_root,
        }
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result
