"""Fake-transport tests for the productized Paramiko transport smoke diagnostic.

No real Paramiko connection or network is used: :class:`FakeSmokeTransport` implements
the :class:`~edge_deploy.transport.RemoteTransport` protocol surface entirely in memory,
so these tests assert the smoke orchestration (one connection, all checks, cleanup)
without touching a real node.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from edge_deploy.transport import RemoteTransport, TransferError, TransferProgress
from edge_deploy.transport_smoke import run_transport_smoke


class FakeSmokeTransport:
    """A minimal in-memory double satisfying :class:`RemoteTransport` for smoke tests."""

    session = "fake-smoke-session"

    def __init__(self, *, pty_success: bool = True) -> None:
        self.start_count = 0
        self.stop_count = 0
        self._authenticated = False
        self._remote_files: dict[str, bytes] = {}
        self._remote_dirs: set[str] = set()
        self._pty_open = False
        self._pty_secret: str | None = None
        self._pty_command = ""
        self._pty_success = pty_success
        self.commands: list[str] = []
        self.uploads: list[str] = []
        self.removed_paths: list[str] = []

    # -- lifecycle -----------------------------------------------------
    def start_session(self, *, connect_timeout: float | None = None, passcode: str | None = None) -> bool:
        self.start_count += 1
        self._authenticated = True
        return True

    def session_exists(self) -> bool:
        return self._authenticated

    def submit_secret(self, secret: str) -> None:
        if self._pty_open:
            self._pty_secret = secret

    def await_authenticated(self, *, timeout: float | None = None, poll_interval: float = 1.0) -> None:
        return None

    def at_shell_prompt(self, screen: str | None = None) -> bool:
        return self._authenticated

    def stop_session(self) -> None:
        self.stop_count += 1
        self._authenticated = False
        self._pty_open = False

    # -- command ---------------------------------------------------------
    @staticmethod
    def _canonical(path: str) -> str:
        # Normalize the ``$HOME``-expanded shell form back to the ``~``-relative form
        # used as the in-memory dict key, mirroring what a real shell/home dir would do.
        if path.startswith("$HOME/"):
            return "~/" + path[len("$HOME/") :]
        return path

    def run_remote(self, command: str, *, timeout: float = 30.0, ensure_shell: bool = True) -> tuple[str, int]:
        self.commands.append(command)
        if command.startswith("sha256sum"):
            # command shape: sha256sum -- <shell-quoted-path> | awk '{print $1}'
            path = self._canonical(command.split("--", 1)[1].split("|", 1)[0].strip())
            data = self._remote_files.get(path)
            if data is None:
                return "", 1
            return hashlib.sha256(data).hexdigest(), 0
        if command.startswith("rm -rf"):
            path = self._canonical(command.rsplit(" ", 1)[-1].strip())
            self.removed_paths.append(path)
            self._remote_files = {
                key: value for key, value in self._remote_files.items() if not key.startswith(path)
            }
            self._remote_dirs = {directory for directory in self._remote_dirs if not directory.startswith(path)}
            return "", 0
        if command.startswith("mkdir -p --"):
            path = self._canonical(command.split("--", 1)[1].strip())
            self._remote_dirs.add(path)
            return "", 0
        if command.startswith("printf '"):
            # command shape: printf '<text>\n' — mimic a real shell echoing the literal text.
            literal = command[len("printf '") : command.rindex("'")]
            return literal.replace("\\n", "\n"), 0
        if command.startswith("sleep ") and "keepalive-ok" in command:
            return "keepalive-ok\n", 0
        return "", 0

    # -- transfer ----------------------------------------------------------
    def upload_file(self, source, remote_path: str, *, progress=None) -> str:
        parent = remote_path.rsplit("/", 1)[0]
        if parent not in self._remote_dirs:
            raise TransferError("Binary transfer failed")
        data = Path(source).read_bytes()
        self.uploads.append(remote_path)
        self._remote_files[remote_path] = data
        total_bytes = len(data)
        if progress is not None:
            progress(TransferProgress(bytes_sent=0, total_bytes=total_bytes, elapsed_s=0.0))
            progress(TransferProgress(bytes_sent=total_bytes, total_bytes=total_bytes, elapsed_s=0.01))
        return hashlib.sha256(data).hexdigest()

    # -- pty dialogue --------------------------------------------------
    def send_text(self, text: str) -> None:
        if "transport-smoke-pty-ready" in text and "read -r smoke_secret" in text:
            self._pty_open = True
            self._pty_command = text

    def wait_for(self, pattern: str, timeout: float = 10.0, poll_interval: float = 0.5) -> str:
        if "pty-ready" in pattern:
            return "transport-smoke-pty-ready"
        if "pty-success" in pattern and self._pty_success and self._pty_secret is not None:
            digest = hashlib.sha256(self._pty_secret.encode("utf-8")).hexdigest()
            if digest in self._pty_command:
                return "transport-smoke-pty-success"
        return ""


@pytest.fixture
def fake_transport() -> FakeSmokeTransport:
    return FakeSmokeTransport()


def test_transport_smoke_satisfies_remote_transport_protocol(fake_transport: FakeSmokeTransport) -> None:
    assert isinstance(fake_transport, RemoteTransport)


def test_transport_smoke_reuses_one_connection_and_cleans_up(fake_transport, tmp_path) -> None:
    result = run_transport_smoke(
        fake_transport,
        node_label="node03",
        payload_bytes=1024,
        keepalive_wait_s=0.0,
    )
    assert result.passed is True
    assert [check.name for check in result.checks] == [
        "command",
        "transfer",
        "pty",
        "keepalive",
        "cleanup",
    ]
    assert fake_transport.start_count == 1
    assert fake_transport.stop_count == 1
    assert any(command.startswith("sleep ") and "keepalive-ok" in command for command in fake_transport.commands)


def test_transport_smoke_fails_when_pty_secret_is_not_validated() -> None:
    transport = FakeSmokeTransport(pty_success=False)

    result = run_transport_smoke(
        transport,
        node_label="node03",
        payload_bytes=256,
        keepalive_wait_s=0.0,
    )

    pty_check = next(check for check in result.checks if check.name == "pty")
    assert pty_check.passed is False


def test_transport_smoke_removes_scratch_directory_on_failure(fake_transport, monkeypatch) -> None:
    original_run_remote = fake_transport.run_remote

    def _broken_run_remote(command: str, *, timeout: float = 30.0, ensure_shell: bool = True):
        if command.startswith("sha256sum"):
            return "0" * 64, 0  # force a digest mismatch to fail the transfer check
        return original_run_remote(command, timeout=timeout, ensure_shell=ensure_shell)

    monkeypatch.setattr(fake_transport, "run_remote", _broken_run_remote)

    result = run_transport_smoke(
        fake_transport,
        node_label="node03",
        payload_bytes=256,
        keepalive_wait_s=0.0,
    )

    assert result.passed is False
    transfer_check = next(check for check in result.checks if check.name == "transfer")
    assert transfer_check.passed is False
    # Cleanup always runs, and the connection is still closed exactly once.
    assert fake_transport.stop_count == 1
    assert fake_transport.removed_paths


def test_transport_smoke_removes_scratch_directory_when_probe_raises(fake_transport, monkeypatch) -> None:
    def _broken_upload(*args, **kwargs):
        raise TransferError("simulated upload failure")

    monkeypatch.setattr(fake_transport, "upload_file", _broken_upload)

    with pytest.raises(TransferError, match="simulated upload failure"):
        run_transport_smoke(
            fake_transport,
            node_label="node03",
            payload_bytes=256,
            keepalive_wait_s=0.0,
        )

    assert fake_transport.stop_count == 1
    assert fake_transport.removed_paths


def test_transport_smoke_checks_never_leak_the_pty_secret(fake_transport) -> None:
    result = run_transport_smoke(
        fake_transport,
        node_label="node03",
        payload_bytes=256,
        keepalive_wait_s=0.0,
    )

    for check in result.checks:
        assert fake_transport._pty_secret not in check.message  # noqa: SLF001 - white-box redaction check
    assert fake_transport._pty_secret is not None  # noqa: SLF001
    assert fake_transport._pty_secret not in fake_transport._pty_command  # noqa: SLF001


def test_transport_smoke_result_message_has_no_endpoint_details(fake_transport) -> None:
    result = run_transport_smoke(
        fake_transport,
        node_label="node03",
        payload_bytes=256,
        keepalive_wait_s=0.0,
    )

    for check in result.checks:
        assert "@" not in check.message
