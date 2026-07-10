"""Tests for the Paramiko connection/authentication seam.

Everything below the TCP/SSH-transport boundary is faked: sockets, the Paramiko
``Transport``, host keys, channels, and SFTP never touch the network. Real
``paramiko.RSAKey``/``paramiko.HostKeys`` objects are used for host-key comparison
because that verification logic is exactly what these tests must exercise.
"""

from __future__ import annotations

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
