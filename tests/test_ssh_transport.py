"""Tests for the Paramiko connection/authentication seam.

Everything below the TCP/SSH-transport boundary is faked: sockets, the Paramiko
``Transport``, host keys, channels, and SFTP never touch the network. Real
``paramiko.RSAKey``/``paramiko.HostKeys`` objects are used for host-key comparison
because that verification logic is exactly what these tests must exercise.
"""

from __future__ import annotations

import hashlib
import stat
import threading
from pathlib import Path

import paramiko
import pytest

from edge_deploy.ssh_transport import ParamikoSshTransport, SshSettings
from edge_deploy.transport import (
    AuthenticationError,
    ConnectionLostError,
    HostKeyError,
    InteractiveChannelError,
    RemoteCommandTimeout,
    TransferError,
    TransferProgress,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSocket:
    """A closeable stand-in for a real TCP socket; never touches the network."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def fake_socket_factory(_address: tuple[str, int], _timeout: float) -> FakeSocket:
    return FakeSocket()


class FakeTransport:
    """Stands in for ``paramiko.Transport``: drives a scripted auth outcome."""

    def __init__(
        self,
        _sock: object,
        *,
        server_key: paramiko.PKey,
        accepted_secrets: tuple[str, ...] = ("one-time-code",),
        echo_prompt: bool = False,
        prompt_count: int = 1,
    ) -> None:
        self._server_key = server_key
        self._accepted_secrets = accepted_secrets
        self._echo_prompt = echo_prompt
        self._prompt_count = prompt_count
        self._active = True
        self._authenticated = False
        self.closed = False
        self.keepalive_interval: int | None = None
        self.secret_requests = 0

    def start_client(self, timeout: float | None = None) -> None:  # noqa: ARG002
        return None

    def get_remote_server_key(self) -> paramiko.PKey:
        return self._server_key

    def auth_interactive(self, _username: str, handler) -> None:
        self.secret_requests += 1
        prompts = [("Enter PASSCODE: ", self._echo_prompt)] * self._prompt_count
        responses = handler("", "", prompts)
        submitted = responses[0] if responses else ""
        if submitted in self._accepted_secrets:
            self._authenticated = True
        else:
            raise paramiko.AuthenticationException("rejected")

    def is_active(self) -> bool:
        return self._active

    def is_authenticated(self) -> bool:
        return self._authenticated

    def set_keepalive(self, interval: int) -> None:
        self.keepalive_interval = interval

    def close(self) -> None:
        self._active = False
        self.closed = True


class RejectThenAcceptTransport(FakeTransport):
    """Rejects the first submitted secret, accepts the second, on the same object."""

    def __init__(self, _sock: object, *, server_key: paramiko.PKey) -> None:
        super().__init__(_sock, server_key=server_key, accepted_secrets=("fresh-code",))


class HangingAuthTransport(FakeTransport):
    """Never completes ``auth_interactive`` — used to exercise the timeout path."""

    def auth_interactive(self, _username: str, handler) -> None:
        self.secret_requests += 1
        prompts = [("Enter PASSCODE: ", False)]
        handler("", "", prompts)
        # Deliberately never sets authenticated and never raises: simulates a
        # server that stops responding after the prompt is answered.
        blocker = threading.Event()
        blocker.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# Known-hosts helpers
# ---------------------------------------------------------------------------


def _write_known_hosts(path: Path, hostname: str, port: int, key: paramiko.PKey) -> None:
    lookup = hostname if port == 22 else f"[{hostname}]:{port}"
    path.write_text(f"{lookup} {key.get_name()} {key.get_base64()}\n", encoding="utf-8")


@pytest.fixture(scope="module")
def server_key() -> paramiko.RSAKey:
    return paramiko.RSAKey.generate(1024)


@pytest.fixture(scope="module")
def other_key() -> paramiko.RSAKey:
    return paramiko.RSAKey.generate(1024)


def _settings(tmp_path: Path, *, known_hosts_name: str = "known_hosts") -> SshSettings:
    return SshSettings(
        username="operator",
        hostname="edge03.example.internal",
        port=2222,
        connect_timeout_s=1.0,
        keepalive_s=5,
        known_hosts_path=tmp_path / known_hosts_name,
    )


# ---------------------------------------------------------------------------
# Fixtures under test
# ---------------------------------------------------------------------------


@pytest.fixture
def ssh_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    def transport_factory(sock: object) -> FakeTransport:
        return FakeTransport(sock, server_key=server_key)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


@pytest.fixture
def reject_then_accept_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    def transport_factory(sock: object) -> RejectThenAcceptTransport:
        return RejectThenAcceptTransport(sock, server_key=server_key)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


@pytest.fixture
def unknown_host_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    # known_hosts file exists but has no entry for this host at all.
    settings = _settings(tmp_path)
    settings.known_hosts_path.write_text("", encoding="utf-8")

    def transport_factory(sock: object) -> FakeTransport:
        return FakeTransport(sock, server_key=server_key)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


@pytest.fixture
def changed_host_key_transport(
    tmp_path: Path, server_key: paramiko.RSAKey, other_key: paramiko.RSAKey
) -> ParamikoSshTransport:
    settings = _settings(tmp_path)
    # known_hosts records a *different* key than the one the fake server presents.
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, other_key)

    def transport_factory(sock: object) -> FakeTransport:
        return FakeTransport(sock, server_key=server_key)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


@pytest.fixture
def echoed_prompt_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    def transport_factory(sock: object) -> FakeTransport:
        return FakeTransport(sock, server_key=server_key, echo_prompt=True)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


@pytest.fixture
def multi_prompt_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    def transport_factory(sock: object) -> FakeTransport:
        return FakeTransport(sock, server_key=server_key, prompt_count=2)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


@pytest.fixture
def hanging_auth_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    def transport_factory(sock: object) -> HangingAuthTransport:
        return HangingAuthTransport(sock, server_key=server_key)

    return ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_start_session_exposes_keyboard_interactive_prompt(ssh_transport) -> None:
    assert ssh_transport.start_session(connect_timeout=1.0) is False
    assert ssh_transport.session_exists() is True


def test_submit_secret_completes_auth_without_persisting_secret(ssh_transport) -> None:
    assert ssh_transport.start_session(connect_timeout=1.0) is False
    ssh_transport.submit_secret("one-time-code")
    ssh_transport.await_authenticated(timeout=1.0)
    assert ssh_transport.at_shell_prompt() is True
    assert "one-time-code" not in repr(ssh_transport)


def test_rejected_code_prepares_fresh_auth_attempt(reject_then_accept_transport) -> None:
    reject_then_accept_transport.start_session(connect_timeout=1.0)
    reject_then_accept_transport.submit_secret("stale-code")
    with pytest.raises(AuthenticationError):
        reject_then_accept_transport.await_authenticated(timeout=1.0)
    reject_then_accept_transport.submit_secret("fresh-code")
    reject_then_accept_transport.await_authenticated(timeout=1.0)


def test_unknown_host_key_fails_before_prompt(unknown_host_transport) -> None:
    with pytest.raises(HostKeyError, match="not present"):
        unknown_host_transport.start_session(connect_timeout=1.0)
    assert unknown_host_transport.secret_requests == 0


def test_changed_host_key_fails_before_prompt(changed_host_key_transport) -> None:
    with pytest.raises(HostKeyError, match="does not match"):
        changed_host_key_transport.start_session(connect_timeout=1.0)
    assert changed_host_key_transport.secret_requests == 0


def test_echoed_keyboard_prompt_is_rejected(echoed_prompt_transport) -> None:
    with pytest.raises(AuthenticationError):
        echoed_prompt_transport.start_session(connect_timeout=1.0)
        echoed_prompt_transport.submit_secret("one-time-code")
        echoed_prompt_transport.await_authenticated(timeout=1.0)


def test_multiple_unexpected_prompts_are_rejected(multi_prompt_transport) -> None:
    with pytest.raises(AuthenticationError):
        multi_prompt_transport.start_session(connect_timeout=1.0)
        multi_prompt_transport.submit_secret("one-time-code")
        multi_prompt_transport.await_authenticated(timeout=1.0)


def test_auth_timeout_poisons_and_closes_transport(hanging_auth_transport) -> None:
    hanging_auth_transport.start_session(connect_timeout=1.0)
    hanging_auth_transport.submit_secret("one-time-code")
    with pytest.raises(AuthenticationError):
        hanging_auth_transport.await_authenticated(timeout=0.3)
    assert hanging_auth_transport.session_exists() is False


def test_teardown_after_auth_failure_closes_underlying_transport(hanging_auth_transport) -> None:
    hanging_auth_transport.start_session(connect_timeout=1.0)
    hanging_auth_transport.submit_secret("one-time-code")
    with pytest.raises(AuthenticationError):
        hanging_auth_transport.await_authenticated(timeout=0.3)
    underlying = hanging_auth_transport._transport
    assert underlying is None or underlying.closed is True


# ---------------------------------------------------------------------------
# Command / PTY / keepalive / cleanup fakes
# ---------------------------------------------------------------------------


class FakeChannel:
    """Stands in for ``paramiko.Channel``: replays a scripted stdout/stderr/exit script."""

    def __init__(self, script: list[tuple[str, object]] | None = None, *, hang: bool = False) -> None:
        self._script = list(script or [])
        self._hang = hang
        self.closed = False
        self.exec_command_calls: list[str] = []
        self.pty_requested = False
        self._exit_status: int | None = None
        self._timeout: float | None = None
        self.sent: bytearray = bytearray()
        self.shutdown_write_called = False
        self._sent_via_pty = self.sent

    def settimeout(self, value: float | None) -> None:
        self._timeout = value

    def get_pty(self, term: str = "xterm", width: int = 80, height: int = 24) -> None:  # noqa: ARG002
        self.pty_requested = True

    def exec_command(self, command: str) -> None:
        self.exec_command_calls.append(command)
        if self._hang:
            return
        for i, (kind, value) in enumerate(self._script):
            if kind == "exit":
                self._exit_status = value
                del self._script[i]
                break

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def shutdown_write(self) -> None:
        self.shutdown_write_called = True

    def recv_ready(self) -> bool:
        return bool(self._script) and self._script[0][0] == "stdout"

    def recv(self, _n: int) -> bytes:
        if self._script and self._script[0][0] == "stdout":
            return self._script.pop(0)[1]
        return b""

    def recv_stderr_ready(self) -> bool:
        return bool(self._script) and self._script[0][0] == "stderr"

    def recv_stderr(self, _n: int) -> bytes:
        if self._script and self._script[0][0] == "stderr":
            return self._script.pop(0)[1]
        return b""

    def exit_status_ready(self) -> bool:
        if self._hang:
            return False
        return not self._script and self._exit_status is not None

    def recv_exit_status(self) -> int:
        assert self._exit_status is not None
        return self._exit_status

    def close(self) -> None:
        self.closed = True


class ScriptedSessionTransport(FakeTransport):
    """An already-authenticated fake transport that opens scripted command channels."""

    def __init__(
        self,
        _sock: object,
        *,
        server_key: paramiko.PKey,
        channel_script: list[tuple[str, object]] | None = None,
        hang: bool = False,
    ) -> None:
        super().__init__(_sock, server_key=server_key)
        self._authenticated = True
        self.channel_script = channel_script or []
        self.hang = hang
        self.opened_channels: list[FakeChannel] = []
        self.pty_input = bytearray()

    def open_session(self, timeout: float | None = None) -> FakeChannel:  # noqa: ARG002
        channel = FakeChannel(list(self.channel_script), hang=self.hang)
        self.opened_channels.append(channel)
        return channel


class _Driver:
    """Small holder exposing the transport under test as ``.driver`` plus test hooks."""

    def __init__(self, transport: ParamikoSshTransport, backing: ScriptedSessionTransport) -> None:
        self.driver = transport
        self._backing = backing

    @property
    def pty_input(self) -> bytes:
        return bytes(self._backing.pty_input)


def _authenticated_transport(
    tmp_path: Path,
    server_key: paramiko.RSAKey,
    *,
    channel_script: list[tuple[str, object]] | None = None,
    hang: bool = False,
) -> _Driver:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    backing_holder: dict[str, ScriptedSessionTransport] = {}

    def transport_factory(sock: object) -> ScriptedSessionTransport:
        backing = ScriptedSessionTransport(
            sock, server_key=server_key, channel_script=channel_script, hang=hang
        )
        backing_holder["transport"] = backing
        return backing

    transport = ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )
    transport.start_session(connect_timeout=1.0)
    transport.submit_secret("one-time-code")
    transport.await_authenticated(timeout=1.0)
    return _Driver(transport, backing_holder["transport"])


@pytest.fixture
def authenticated_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> _Driver:
    return _authenticated_transport(
        tmp_path,
        server_key,
        channel_script=[
            ("stdout", b"one\n"),
            ("stderr", b"two\n"),
            ("stdout", b"three\n"),
            ("exit", 7),
        ],
    )


@pytest.fixture
def timeout_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> ParamikoSshTransport:
    return _authenticated_transport(tmp_path, server_key, hang=True).driver


# ---------------------------------------------------------------------------
# Command-channel tests
# ---------------------------------------------------------------------------


def test_run_remote_returns_observed_transcript_and_real_exit_status(authenticated_transport) -> None:
    text, code = authenticated_transport.driver.run_remote("probe", timeout=1.0)
    assert text == "one\ntwo\nthree\n"
    assert code == 7


def test_command_timeout_poisons_connection(timeout_transport) -> None:
    with pytest.raises(RemoteCommandTimeout):
        timeout_transport.run_remote("sleep forever", timeout=0.05)
    assert timeout_transport.session_exists() is False


def test_run_remote_on_inactive_transport_raises_connection_lost(tmp_path, server_key) -> None:
    driver = _authenticated_transport(tmp_path, server_key).driver
    driver.stop_session()
    with pytest.raises(ConnectionLostError):
        driver.run_remote("echo hi", timeout=1.0)


# ---------------------------------------------------------------------------
# PTY dialogue tests
# ---------------------------------------------------------------------------


class PtyChannel(FakeChannel):
    """A PTY channel that streams a scripted transcript one chunk per ``recv`` call
    once its command has been fed via ``sendall``."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = [b"Password:"]
        self._served = False

    def invoke_shell(self) -> None:
        return None

    def recv_ready(self) -> bool:
        return bool(self._chunks)

    def recv(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def recv_stderr_ready(self) -> bool:
        return False

    def recv_stderr(self, _n: int) -> bytes:
        return b""

    def exit_status_ready(self) -> bool:
        return False


class PtyDialogueSessionTransport(ScriptedSessionTransport):
    def open_session(self, timeout: float | None = None) -> PtyChannel:  # noqa: ARG002
        channel = PtyChannel()
        self.opened_channels.append(channel)
        self._pty_channel = channel
        return channel


@pytest.fixture
def pty_authenticated_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> _Driver:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    backing_holder: dict[str, PtyDialogueSessionTransport] = {}

    def transport_factory(sock: object) -> PtyDialogueSessionTransport:
        backing = PtyDialogueSessionTransport(sock, server_key=server_key)
        backing_holder["transport"] = backing
        return backing

    transport = ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
    )
    transport.start_session(connect_timeout=1.0)
    transport.submit_secret("one-time-code")
    transport.await_authenticated(timeout=1.0)
    return _Driver(transport, backing_holder["transport"])


def test_pty_dialogue_sends_secret_without_command_logging(pty_authenticated_transport) -> None:
    driver = pty_authenticated_transport.driver
    driver.send_text("kinit")
    assert "Password:" in driver.wait_for(r"[Pp]assword.*:", timeout=1.0)
    driver.submit_secret("kerberos-secret")
    channel = pty_authenticated_transport._backing._pty_channel
    assert bytes(channel.sent).endswith(b"kerberos-secret\n")
    assert "kerberos-secret" not in repr(driver)


def test_wait_for_raises_interactive_channel_error_on_timeout(pty_authenticated_transport) -> None:
    driver = pty_authenticated_transport.driver
    driver.send_text("kinit")
    with pytest.raises(InteractiveChannelError):
        driver.wait_for(r"NEVER MATCHES", timeout=0.1)


# ---------------------------------------------------------------------------
# Keepalive and cleanup tests
# ---------------------------------------------------------------------------


def test_await_authenticated_configures_keepalive(ssh_transport) -> None:
    ssh_transport.start_session(connect_timeout=1.0)
    ssh_transport.submit_secret("one-time-code")
    ssh_transport.await_authenticated(timeout=1.0)
    assert ssh_transport._transport.keepalive_interval == ssh_transport._settings.keepalive_s


def test_stop_session_closes_pty_and_transport_and_socket(pty_authenticated_transport) -> None:
    driver = pty_authenticated_transport.driver
    driver.send_text("kinit")
    driver.wait_for(r"[Pp]assword.*:", timeout=1.0)
    underlying_transport = driver._transport
    underlying_socket = driver._socket
    pty_channel = pty_authenticated_transport._backing._pty_channel

    driver.stop_session()

    assert pty_channel.closed is True
    assert underlying_transport.closed is True
    assert underlying_socket.closed is True
    assert driver._interactive_channel is None
    assert driver.session_exists() is False


def test_stop_session_is_idempotent(authenticated_transport) -> None:
    driver = authenticated_transport.driver
    driver.stop_session()
    driver.stop_session()  # must not raise


# ---------------------------------------------------------------------------
# SFTP / binary transfer fakes
# ---------------------------------------------------------------------------


class FakeRemoteFile:
    """A remote file's tracked contents, mode, and existence for the fake filesystem."""

    def __init__(self, data: bytes, *, mode: int = 0o600) -> None:
        self.data = data
        self.mode = mode


class FakeSftpAttr:
    def __init__(self, size: int, mode: int) -> None:
        self.st_size = size
        self.st_mode = mode


class FakeSftpClient:
    """Stands in for ``paramiko.SFTPClient`` against an in-memory remote filesystem."""

    def __init__(self, files: dict[str, FakeRemoteFile], *, put_calls: list[str], atomic_renames: list[int]) -> None:
        self._files = files
        self._put_calls = put_calls
        self._atomic_renames = atomic_renames
        self.closed = False

    def putfo(self, source_handle, remote_path: str, *, file_size: int = 0, callback=None) -> None:  # noqa: ARG002
        self._put_calls.append(remote_path)
        data = source_handle.read()
        if callback is not None:
            callback(len(data), len(data))
        self._files[remote_path] = FakeRemoteFile(data, mode=0o644)

    def chmod(self, remote_path: str, mode: int) -> None:
        self._files[remote_path].mode = mode

    def stat(self, remote_path: str) -> FakeSftpAttr:
        if remote_path not in self._files:
            raise FileNotFoundError(remote_path)
        entry = self._files[remote_path]
        return FakeSftpAttr(len(entry.data), stat.S_IFREG | entry.mode)

    def posix_rename(self, source: str, dest: str) -> None:
        self._atomic_renames.append(1)
        self._files[dest] = self._files.pop(source)

    def remove(self, remote_path: str) -> None:
        self._files.pop(remote_path, None)

    def close(self) -> None:
        self.closed = True


class SftpSessionTransport(ScriptedSessionTransport):
    """An authenticated fake transport whose command channel answers ``$HOME``,
    ``sha256sum``, and ``stat``/``mv`` probes against an in-memory remote filesystem,
    and whose SFTP subsystem is backed by :class:`FakeSftpClient`."""

    def __init__(
        self,
        _sock: object,
        *,
        server_key: paramiko.PKey,
        home: str = "/home/operator",
        sftp_available: bool = True,
        files: dict[str, FakeRemoteFile] | None = None,
        digest_mismatch: bool = False,
    ) -> None:
        super().__init__(_sock, server_key=server_key)
        self.home = home
        self.sftp_available = sftp_available
        self.files: dict[str, FakeRemoteFile] = files if files is not None else {}
        self.digest_mismatch = digest_mismatch
        self.sftp_put_calls: list[str] = []
        self.atomic_renames: list[int] = []
        self.part_files_removed: list[str] = []
        self.binary_stream_uploads = 0
        self.base64_commands: list[str] = []

    def open_session(self, timeout: float | None = None) -> "FakeCommandOrSftpChannel":  # noqa: ARG002
        channel = FakeCommandOrSftpChannel(self)
        self.opened_channels.append(channel)
        return channel

    def _part_files(self) -> list[str]:
        return [name for name in self.files if ".edge-deploy-" in name and name.endswith(".part")]


class FakeCommandOrSftpChannel(FakeChannel):
    """A session channel that answers either shell commands (``$HOME``, ``sha256sum``,
    atomic ``mv``, cleanup) or an ``sftp`` subsystem request, mirroring how one Paramiko
    session channel can become either kind of conversation."""

    def __init__(self, backing: SftpSessionTransport) -> None:
        super().__init__()
        self._backing = backing
        self._reply = b""
        self._exit_status_value = 0
        self._is_sftp = False

    def invoke_subsystem(self, name: str) -> None:
        if name != "sftp":
            raise ValueError(f"unsupported subsystem {name!r}")
        if not self._backing.sftp_available:
            self.closed = True
            raise paramiko.SSHException("Channel closed.")
        self._is_sftp = True

    def exec_command(self, command: str) -> None:
        self.exec_command_calls.append(command)
        backing = self._backing
        if "base64" in command:
            backing.base64_commands.append(command)
        if command == 'printf "%s" "$HOME"' or command == "printf '%s' \"$HOME\"":
            self._reply = backing.home.encode("utf-8")
            self._exit_status_value = 0
        elif command.startswith("sha256sum"):
            target = _extract_quoted_path(command)
            entry = backing.files.get(target)
            if entry is None:
                self._exit_status_value = 1
            else:
                digest = hashlib.sha256(entry.data).hexdigest()
                if backing.digest_mismatch:
                    digest = "0" * 64
                # Mirrors "sha256sum -- path | awk '{print $1}'": only the digest field.
                self._reply = f"{digest}\n".encode()
                self._exit_status_value = 0
        elif command.startswith("stat -c"):
            target = _extract_quoted_path(command)
            entry = backing.files.get(target)
            if entry is None:
                self._exit_status_value = 1
            else:
                self._reply = f"{len(entry.data)}\n".encode()
                self._exit_status_value = 0
        elif command.startswith("mv "):
            parts = command.split()
            source, dest = parts[-2], parts[-1]
            source = source.strip("'")
            dest = dest.strip("'")
            if source in backing.files:
                backing.files[dest] = backing.files.pop(source)
                backing.atomic_renames.append(1)
                self._exit_status_value = 0
            else:
                self._exit_status_value = 1
        elif command.startswith("rm -f"):
            target = _extract_quoted_path(command)
            if target in backing.files:
                backing.files.pop(target)
                backing.part_files_removed.append(target)
            self._exit_status_value = 0
        elif "cat >" in command:
            backing.binary_stream_uploads += 1
            target = command.split("cat >", 1)[1].strip().strip("'")
            self._pending_stream_target = target
            self._exit_status_value = 0
        else:
            self._exit_status_value = 0

    def sendall(self, data: bytes) -> None:
        super().sendall(data)
        target = getattr(self, "_pending_stream_target", None)
        if target is not None:
            existing = self._backing.files.get(target)
            merged = (existing.data if existing else b"") + bytes(self.sent)
            self._backing.files[target] = FakeRemoteFile(merged)

    def shutdown_write(self) -> None:
        self.shutdown_write_called = True

    def recv_ready(self) -> bool:
        return bool(self._reply)

    def recv(self, _n: int) -> bytes:
        data, self._reply = self._reply, b""
        return data

    def recv_stderr_ready(self) -> bool:
        return False

    def recv_stderr(self, _n: int) -> bytes:
        return b""

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return self._exit_status_value


def _extract_quoted_path(command: str) -> str:
    """Return the path token immediately following a lone ``--`` argument separator."""
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token == "--" and index + 1 < len(tokens):
            return tokens[index + 1].strip("'\"")
    raise ValueError(f"could not find a path token in fake command {command!r}")


def _sftp_transport(
    tmp_path: Path,
    server_key: paramiko.RSAKey,
    *,
    home: str = "/home/operator",
    sftp_available: bool = True,
    files: dict[str, FakeRemoteFile] | None = None,
    digest_mismatch: bool = False,
) -> tuple[ParamikoSshTransport, SftpSessionTransport]:
    settings = _settings(tmp_path)
    _write_known_hosts(settings.known_hosts_path, settings.hostname, settings.port, server_key)

    backing_holder: dict[str, SftpSessionTransport] = {}

    def transport_factory(sock: object) -> SftpSessionTransport:
        backing = SftpSessionTransport(
            sock,
            server_key=server_key,
            home=home,
            sftp_available=sftp_available,
            files=files,
            digest_mismatch=digest_mismatch,
        )
        backing_holder["transport"] = backing
        return backing

    def sftp_client_factory(channel: object) -> FakeSftpClient:
        backing = backing_holder["transport"]
        if not backing.sftp_available:
            raise paramiko.SSHException("Channel closed.")
        return FakeSftpClient(backing.files, put_calls=backing.sftp_put_calls, atomic_renames=backing.atomic_renames)

    transport = ParamikoSshTransport(
        settings,
        session="edge-node03",
        socket_factory=fake_socket_factory,
        transport_factory=transport_factory,
        sftp_client_factory=sftp_client_factory,
    )
    transport.start_session(connect_timeout=1.0)
    transport.submit_secret("one-time-code")
    transport.await_authenticated(timeout=1.0)
    return transport, backing_holder["transport"]


class _SftpDriver:
    """Small holder exposing the transport under test plus fake-filesystem test hooks."""

    def __init__(self, driver: ParamikoSshTransport, backing: SftpSessionTransport) -> None:
        self.driver = driver
        self._backing = backing

    def remote_file(self, name: str, data: bytes) -> None:
        self._backing.files[f"{self._backing.home}/{name}"] = FakeRemoteFile(data)

    def upload_file(self, source: Path, remote_path: str, *, progress=None) -> str:
        return self.driver.upload_file(source, remote_path, progress=progress)

    @property
    def sftp_put_calls(self) -> list[str]:
        return self._backing.sftp_put_calls

    @property
    def atomic_renames(self) -> int:
        return len(self._backing.atomic_renames)

    @property
    def part_files(self) -> list[str]:
        return self._backing._part_files()


@pytest.fixture
def authenticated_sftp_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> _SftpDriver:
    driver, backing = _sftp_transport(tmp_path, server_key)
    return _SftpDriver(driver, backing)


@pytest.fixture
def mismatch_sftp_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> _SftpDriver:
    files = {"/home/operator/.edge-deploy/bundle.zip": FakeRemoteFile(b"previous-verified-bundle")}
    driver, backing = _sftp_transport(tmp_path, server_key, files=files, digest_mismatch=True)
    return _SftpDriver(driver, backing)


@pytest.fixture
def no_sftp_transport(tmp_path: Path, server_key: paramiko.RSAKey) -> _SftpDriver:
    driver, backing = _sftp_transport(tmp_path, server_key, sftp_available=False)
    return _SftpDriver(driver, backing)


# ---------------------------------------------------------------------------
# Verified binary transfer tests
# ---------------------------------------------------------------------------


def test_upload_reuses_matching_remote_digest(authenticated_sftp_transport, tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"already-present")
    authenticated_sftp_transport.remote_file(source.name, source.read_bytes())
    digest = authenticated_sftp_transport.upload_file(source, f"~/{source.name}")
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()
    assert authenticated_sftp_transport.sftp_put_calls == []


def test_upload_verifies_part_then_atomically_replaces(authenticated_sftp_transport, tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"new-bundle")
    snapshots: list[TransferProgress] = []
    digest = authenticated_sftp_transport.upload_file(
        source,
        "~/.edge-deploy/bundle.zip",
        progress=snapshots.append,
    )
    assert digest == hashlib.sha256(b"new-bundle").hexdigest()
    assert snapshots[0].bytes_sent == 0
    assert snapshots[-1].bytes_sent == len(b"new-bundle")
    assert authenticated_sftp_transport.atomic_renames == 1
    assert authenticated_sftp_transport.part_files == []


def test_upload_over_sftp_sets_final_file_mode_0600(authenticated_sftp_transport, tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"new-bundle")
    authenticated_sftp_transport.upload_file(source, "~/.edge-deploy/bundle.zip")
    final = authenticated_sftp_transport._backing.files["/home/operator/.edge-deploy/bundle.zip"]
    assert final.mode == 0o600


def test_digest_mismatch_removes_part_and_preserves_final(mismatch_sftp_transport, tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"corrupt-in-transit")
    with pytest.raises(TransferError, match="digest verification failed"):
        mismatch_sftp_transport.upload_file(source, "~/.edge-deploy/bundle.zip")
    final = mismatch_sftp_transport._backing.files["/home/operator/.edge-deploy/bundle.zip"]
    assert final.data == b"previous-verified-bundle"
    assert mismatch_sftp_transport.part_files == []


def test_sftp_unavailable_uses_binary_exec_channel(no_sftp_transport, tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"binary\x00payload")
    no_sftp_transport.upload_file(source, "~/.edge-deploy/bundle.zip")
    assert no_sftp_transport._backing.binary_stream_uploads == 1
    assert no_sftp_transport._backing.base64_commands == []


def test_binary_exec_channel_upload_restricts_part_file_mode(no_sftp_transport, tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"binary\x00payload")
    no_sftp_transport.upload_file(source, "~/.edge-deploy/bundle.zip")
    cat_commands = [
        command
        for channel in no_sftp_transport._backing.opened_channels
        for command in channel.exec_command_calls
        if "cat >" in command
    ]
    assert cat_commands, "expected a 'cat >' exec command for the binary-fallback upload"
    assert all(command.startswith("umask 077;") for command in cat_commands)
