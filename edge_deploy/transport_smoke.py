"""Productized Paramiko transport smoke diagnostic (ADR-0014).

``run_transport_smoke()`` exercises one authenticated :class:`~edge_deploy.transport.RemoteTransport`
across the behaviors a release actually depends on — a remote command, an 8 MiB (by default)
digest-verified upload, a PTY dummy-secret dialogue, keepalive survival across two intervals,
and scratch-directory cleanup — using only the public transport protocol. It never imports
Paramiko or any other private transport internals directly, so it exercises exactly the
production seam a release uses.

Nothing sensitive (the generated dummy PTY secret, the node's SSH endpoint) is persisted or
included in a check message; :func:`edge_deploy.reporting.redact` further guards anything
printed by the CLI surface.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from edge_deploy.remote_paths import edge_deploy_path, shell_remote_path

if TYPE_CHECKING:
    from edge_deploy.transport import RemoteTransport

DEFAULT_PAYLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_KEEPALIVE_INTERVAL_S = 5.0
COMMAND_TIMEOUT_S = 30.0
TRANSFER_TIMEOUT_S = 120.0
PTY_TIMEOUT_S = 30.0
CLEANUP_TIMEOUT_S = 15.0

_COMMAND_STDOUT_TOKEN = "transport-smoke-stdout-ok"
_PTY_READY_TOKEN = "transport-smoke-pty-ready"
_PTY_SUCCESS_TOKEN = "transport-smoke-pty-success"
_KEEPALIVE_TOKEN = "keepalive-ok"


@dataclass(frozen=True)
class SmokeCheck:
    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class SmokeResult:
    node_label: str
    checks: list[SmokeCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _remote_sha256(driver: RemoteTransport, remote_path: str, *, timeout: float) -> str | None:
    quoted = shell_remote_path(remote_path)
    text, code = driver.run_remote(f"sha256sum -- {quoted} | awk '{{print $1}}'", timeout=timeout)
    if code != 0:
        return None
    digest = text.strip()
    if len(digest) != 64:
        return None
    return digest


def _probe_command(driver: RemoteTransport) -> SmokeCheck:
    text, code = driver.run_remote(f"printf '{_COMMAND_STDOUT_TOKEN}\\n'", timeout=COMMAND_TIMEOUT_S)
    if code != 0 or text.strip() != _COMMAND_STDOUT_TOKEN:
        return SmokeCheck("command", False, "remote command did not return the expected output")
    return SmokeCheck("command", True, "remote command executed and returned exact output")


def _probe_transfer(driver: RemoteTransport, scratch_dir: str, payload_bytes: int) -> SmokeCheck:
    payload = secrets.token_bytes(payload_bytes)
    local_digest = hashlib.sha256(payload).hexdigest()
    remote_path = f"{scratch_dir}/payload"
    quoted_scratch = shell_remote_path(scratch_dir)
    _text, mkdir_code = driver.run_remote(
        f"mkdir -p -- {quoted_scratch}", timeout=COMMAND_TIMEOUT_S
    )
    if mkdir_code != 0:
        return SmokeCheck("transfer", False, "remote scratch directory creation failed")

    tmp_file = NamedTemporaryFile(delete=False)
    try:
        tmp_file.write(payload)
        tmp_file.close()
        started = time.monotonic()
        driver.upload_file(Path(tmp_file.name), remote_path)
        elapsed = max(time.monotonic() - started, 1e-9)
    finally:
        Path(tmp_file.name).unlink(missing_ok=True)

    remote_digest = _remote_sha256(driver, remote_path, timeout=TRANSFER_TIMEOUT_S)
    if remote_digest != local_digest:
        return SmokeCheck("transfer", False, "uploaded payload SHA-256 did not match the remote copy")

    mib = payload_bytes / (1024 * 1024)
    rate = mib / elapsed
    return SmokeCheck(
        "transfer",
        True,
        f"{mib:.1f} MiB uploaded and digest-verified ({rate:.2f} MiB/s)",
    )


def _probe_pty(driver: RemoteTransport) -> SmokeCheck:
    dummy_secret = secrets.token_urlsafe(32)
    expected_digest = hashlib.sha256(dummy_secret.encode("utf-8")).hexdigest()
    driver.send_text(
        f"printf '{_PTY_READY_TOKEN}\\n'; "
        "stty -echo; IFS= read -r smoke_secret; stty echo; "
        "smoke_digest=$(printf '%s' \"$smoke_secret\" | sha256sum | awk '{print $1}'); "
        "unset smoke_secret; "
        f"if [ \"$smoke_digest\" = '{expected_digest}' ]; then "
        f"printf '{_PTY_SUCCESS_TOKEN}\\n'; "
        "else printf 'transport-smoke-pty-failure\\n'; fi; unset smoke_digest"
    )
    ready = driver.wait_for(_PTY_READY_TOKEN, timeout=PTY_TIMEOUT_S)
    if _PTY_READY_TOKEN not in ready:
        return SmokeCheck("pty", False, "PTY dialogue did not become ready for generated input")
    driver.submit_secret(dummy_secret)
    validated = driver.wait_for(_PTY_SUCCESS_TOKEN, timeout=PTY_TIMEOUT_S)
    if _PTY_SUCCESS_TOKEN not in validated:
        return SmokeCheck("pty", False, "PTY dialogue did not validate the generated input")
    return SmokeCheck("pty", True, "PTY dialogue accepted a generated dummy secret")


def _probe_keepalive(driver: RemoteTransport, keepalive_wait_s: float) -> SmokeCheck:
    started = time.monotonic()
    text, code = driver.run_remote(
        f"sleep {keepalive_wait_s:.3f}; printf '{_KEEPALIVE_TOKEN}\\n'",
        timeout=max(COMMAND_TIMEOUT_S, keepalive_wait_s + 10.0),
    )
    elapsed = time.monotonic() - started
    if code != 0 or text.strip() != _KEEPALIVE_TOKEN:
        return SmokeCheck("keepalive", False, "command did not survive the keepalive interval")
    return SmokeCheck("keepalive", True, f"connection survived {elapsed:.1f}s across keepalive intervals")


def _cleanup_scratch(driver: RemoteTransport, scratch_dir: str) -> SmokeCheck:
    quoted = shell_remote_path(scratch_dir)
    _text, code = driver.run_remote(f"rm -rf -- {quoted}", timeout=CLEANUP_TIMEOUT_S)
    if code != 0:
        return SmokeCheck("cleanup", False, "remote scratch directory removal failed")
    return SmokeCheck("cleanup", True, "remote scratch directory removed")


def run_transport_smoke(
    driver: RemoteTransport,
    *,
    node_label: str,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    keepalive_wait_s: float = DEFAULT_KEEPALIVE_INTERVAL_S * 2,
) -> SmokeResult:
    """Authenticate once, run the command/transfer/PTY/keepalive/cleanup diagnostic, and close.

    Exactly one connection is started and stopped: ``driver.start_session()`` is called
    once up front and ``driver.stop_session()`` exactly once in ``finally``, regardless of
    which checks pass or fail. Every check runs over that same connection (never a new
    one); the remote scratch directory is always removed, even when an earlier check fails.
    """
    scratch_dir = edge_deploy_path("smoke", uuid.uuid4().hex)
    checks: list[SmokeCheck] = []
    try:
        already_authenticated = False
        try:
            already_authenticated = driver.session_exists() and driver.at_shell_prompt()
        except Exception:
            already_authenticated = False
        if not already_authenticated:
            driver.start_session()
        checks.append(_probe_command(driver))
        checks.append(_probe_transfer(driver, scratch_dir, payload_bytes))
        checks.append(_probe_pty(driver))
        checks.append(_probe_keepalive(driver, keepalive_wait_s))
    finally:
        try:
            checks.append(_cleanup_scratch(driver, scratch_dir))
        except Exception:
            checks.append(SmokeCheck("cleanup", False, "remote scratch directory removal raised an error"))
        finally:
            driver.stop_session()
    return SmokeResult(node_label=node_label, checks=checks)
