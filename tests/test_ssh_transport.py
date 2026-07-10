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
from edge_deploy.transport import AuthenticationError, HostKeyError

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
