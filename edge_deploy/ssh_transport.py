"""Paramiko-backed :class:`RemoteTransport`: one persistent, keyboard-interactively
authenticated SSH ``Transport`` per Edge Node connection.

This module owns connection settings, strict host-key verification, and the
AuthBroker-compatible ``start_session`` / ``submit_secret`` / ``await_authenticated``
state machine (ADR-0002's documented ``start_session() -> False`` RSA prompt).
Command execution, PTY dialogue, keepalive, cleanup, and verified binary transfer are
implemented in later tasks; this module intentionally raises ``NotImplementedError``
for those seams until then.

No operator configuration, endpoint, or one-time passcode is ever logged, persisted, or
included in a ``repr()``.
"""

from __future__ import annotations

import queue
import shlex
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

import paramiko

from edge_deploy.config import NodeConfig
from edge_deploy.preflight import endpoint_from_node
from edge_deploy.transport import AuthenticationError, HostKeyError, TransportUnavailable

DEFAULT_KEEPALIVE_SECONDS = 5
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0
_AUTH_COMPLETION_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class SshSettings:
    """Private in-memory connection settings; never print or persist this object."""

    username: str
    hostname: str
    port: int
    connect_timeout_s: float
    keepalive_s: int
    known_hosts_path: Path


def _parse_open_ssh_option(option: str) -> tuple[str, str] | None:
    """Parse the ``keyword=value`` and quoted ``keyword value`` forms accepted by ``ssh -o``."""
    candidate = option.strip()
    if not candidate:
        return None
    if "=" in candidate:
        key, value = candidate.split("=", 1)
        return key.strip().lower(), value.strip()
    parts = candidate.split(None, 1)
    if len(parts) == 2:
        return parts[0].lower(), parts[1].strip()
    return None


def settings_from_node(node: NodeConfig) -> SshSettings:
    """Build :class:`SshSettings` from a :class:`NodeConfig`, parsing only the supported
    ``-o`` options: ``ServerAliveInterval``, ``ConnectTimeout``, ``UserKnownHostsFile``.

    ``StrictHostKeyChecking=no`` is rejected outright: the Paramiko adapter always
    verifies the server key and never weakens that verification to accommodate a
    convenience option meant for interactive OpenSSH use.
    """
    endpoint = endpoint_from_node(node)
    username, separator, _ = node.host.rpartition("@")
    if not separator or not username:
        raise TransportUnavailable("Node host must use the user@host form for the Paramiko transport")

    hostname = endpoint.hostname.strip()
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    if not hostname:
        raise TransportUnavailable("Node hostname is empty")

    keepalive = DEFAULT_KEEPALIVE_SECONDS
    connect_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"

    for part in shlex.split(node.ssh_options or ""):
        option = _parse_open_ssh_option(part)
        if option is None:
            continue
        key, value = option
        if key == "serveraliveinterval":
            keepalive = int(value)
        elif key == "connecttimeout":
            connect_timeout = float(value)
        elif key == "userknownhostsfile":
            known_hosts_path = Path(value).expanduser()
        elif key == "stricthostkeychecking" and value.strip().lower() == "no":
            raise TransportUnavailable(
                "StrictHostKeyChecking=no is not supported by the Paramiko transport"
            )

    if keepalive <= 0:
        raise TransportUnavailable("ServerAliveInterval must be a positive integer")
    if connect_timeout <= 0:
        raise TransportUnavailable("ConnectTimeout must be a positive number")

    return SshSettings(
        username=username,
        hostname=hostname,
        port=endpoint.port,
        connect_timeout_s=connect_timeout,
        keepalive_s=keepalive,
        known_hosts_path=known_hosts_path,
    )


def _known_host_lookup_name(hostname: str, port: int) -> str:
    return hostname if port == 22 else f"[{hostname}]:{port}"


def _verify_server_key(server_key: paramiko.PKey, hostname: str, port: int, known_hosts_path: Path) -> None:
    """Require an exact key-type and key-value match in the configured known-hosts file.

    Error messages are deliberately generic (no hostnames or ports) so a raised
    :class:`HostKeyError` never leaks a private endpoint.
    """
    host_keys = paramiko.HostKeys()
    try:
        host_keys.load(str(known_hosts_path))
    except OSError as exc:
        raise HostKeyError("Strict known-host verification could not load the configured known_hosts file") from exc

    lookup_name = _known_host_lookup_name(hostname, port)
    known_for_host = host_keys.lookup(lookup_name)
    if known_for_host is None:
        raise HostKeyError("Server key is not present in the configured known_hosts file")
    expected_key = known_for_host.get(server_key.get_name())
    if expected_key is None or expected_key != server_key:
        raise HostKeyError("Server key does not match the configured known_hosts file")


class ParamikoSshTransport:
    """Own exactly one authenticated Paramiko ``Transport`` for one Edge Node."""

    def __init__(
        self,
        settings: SshSettings,
        *,
        session: str,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        transport_factory: Callable[[socket.socket], paramiko.Transport] = paramiko.Transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self.session = session
        self._socket_factory = socket_factory
        self._transport_factory = transport_factory
        self._clock = clock
        self._socket: socket.socket | None = None
        self._transport: paramiko.Transport | None = None
        self._remote_home: PurePosixPath | None = None
        self._interactive_channel: paramiko.Channel | None = None
        self._poisoned = False
        self._closed = False

        self._secret_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self._prompt_ready = threading.Event()
        self._auth_done = threading.Event()
        self._auth_error: BaseException | None = None
        self.secret_requests = 0

    @classmethod
    def from_node_and_profile(
        cls,
        node: object,
        profile: object,
        *,
        retries: int = 2,
    ) -> "ParamikoSshTransport":
        """Build a transport from a ``NodeConfig`` and a ``ToolProfile``.

        ``retries`` is accepted for factory compatibility but connection retry policy
        remains in release orchestration; this transport never silently reconnects an
        authenticated operation.
        """
        del profile, retries
        settings = settings_from_node(node)  # type: ignore[arg-type]
        return cls(settings, session=getattr(node, "session", ""))

    def __repr__(self) -> str:
        return f"ParamikoSshTransport(session={self.session!r})"

    # ------------------------------------------------------------------
    # Connection / authentication state machine
    # ------------------------------------------------------------------

    def session_exists(self) -> bool:
        """True if the TCP/SSH transport is active (authenticated or not)."""
        return self._transport is not None and not self._poisoned and self._transport.is_active()

    def at_shell_prompt(self, screen: str | None = None) -> bool:
        """True once the transport is authenticated. ``screen`` is accepted for
        :class:`RemoteTransport` compatibility and ignored."""
        del screen
        return (
            self._transport is not None
            and not self._poisoned
            and self._transport.is_active()
            and self._transport.is_authenticated()
        )

    def start_session(self, *, connect_timeout: float | None = None, passcode: str | None = None) -> bool:
        """Open the TCP socket, start the SSH transport, verify the host key, and begin
        keyboard-interactive authentication in a daemon worker.

        Returns
        -------
        True
            Already authenticated (only possible if ``passcode`` completes auth inline).
        False
            Authentication prompt is pending; call :meth:`submit_secret` then
            :meth:`await_authenticated`.
        """
        if self._closed:
            raise TransportUnavailable("Closed transport cannot start another session")
        if self._poisoned:
            raise TransportUnavailable("Poisoned transport cannot start another session")
        if self._transport is not None:
            raise TransportUnavailable("This transport permits only one connection")

        effective_timeout = connect_timeout if connect_timeout is not None else self._settings.connect_timeout_s

        raw_socket: socket.socket | None = None
        transport: paramiko.Transport | None = None
        try:
            raw_socket = self._socket_factory(
                (self._settings.hostname, self._settings.port),
                effective_timeout,
            )
            transport = self._transport_factory(raw_socket)
            transport.start_client(timeout=effective_timeout)
            _verify_server_key(
                transport.get_remote_server_key(),
                self._settings.hostname,
                self._settings.port,
                self._settings.known_hosts_path,
            )
        except HostKeyError:
            self._best_effort_close(transport=transport, raw_socket=raw_socket)
            raise
        except TransportUnavailable:
            self._best_effort_close(transport=transport, raw_socket=raw_socket)
            raise
        except Exception as exc:
            self._best_effort_close(transport=transport, raw_socket=raw_socket)
            raise TransportUnavailable("Could not establish the SSH transport") from exc

        self._socket = raw_socket
        self._transport = transport
        self._start_auth_worker()

        if passcode is not None:
            self.submit_secret(passcode)
            self.await_authenticated(timeout=effective_timeout)
            return True
        return False

    def _start_auth_worker(self) -> None:
        assert self._transport is not None
        transport = self._transport
        self._auth_done.clear()
        self._auth_error = None

        def handler(_title: str, _instructions: str, prompts: list[tuple[str, bool]]) -> list[str]:
            if len(prompts) != 1:
                raise AuthenticationError("Expected exactly one keyboard-interactive prompt")
            _prompt, echo = prompts[0]
            if echo:
                raise AuthenticationError("Keyboard-interactive passcode prompt requested echo")
            self._prompt_ready.set()
            code = self._secret_queue.get()
            try:
                return [code]
            finally:
                code = ""  # noqa: F841 - drop the local reference to the secret

        def authenticate() -> None:
            try:
                self.secret_requests += 1
                transport.auth_interactive(self._settings.username, handler)
            except BaseException as exc:  # noqa: BLE001 - forwarded to await_authenticated
                self._auth_error = exc
            finally:
                self._auth_done.set()

        worker = threading.Thread(target=authenticate, daemon=True, name=f"paramiko-auth-{self.session}")
        worker.start()

    def submit_secret(self, secret: str) -> None:
        """Forward a one-time passcode to the pending keyboard-interactive prompt.

        Never logged, echoed, or retained beyond the queue handoff.
        """
        if self._transport is None or self._poisoned:
            raise TransportUnavailable("No pending authentication prompt")
        if not self._prompt_ready.wait(timeout=self._settings.connect_timeout_s):
            raise AuthenticationError("No keyboard-interactive prompt was issued in time")
        self._prompt_ready.clear()
        try:
            self._secret_queue.put_nowait(secret)
        except queue.Full:
            # A previous secret is still queued (unread): drop it and requeue.
            with self._secret_queue.mutex:  # type: ignore[attr-defined]
                self._secret_queue.queue.clear()
            self._secret_queue.put_nowait(secret)

    def await_authenticated(self, *, timeout: float | None = None, poll_interval: float = 1.0) -> None:
        """Block until the pending keyboard-interactive attempt resolves.

        On rejection, a new keyboard-interactive attempt is started on the same active
        SSH transport so the existing AuthBroker retry loop can call
        :meth:`submit_secret` again with a fresh code. On timeout, the transport is
        closed and poisoned.
        """
        del poll_interval
        if self._transport is None:
            raise TransportUnavailable("No session to authenticate")
        effective_timeout = timeout if timeout is not None else self._settings.connect_timeout_s

        if not self._auth_done.wait(timeout=effective_timeout):
            self._poison_and_close()
            raise AuthenticationError("Authentication exceeded its deadline")

        error = self._auth_error
        self._auth_error = None
        if error is not None:
            if self._transport is not None and not self._poisoned and self._transport.is_active():
                # Preserve the active SSH transport and prepare a fresh attempt so the
                # caller can submit a fresh (non-stale) one-time code.
                self._prompt_ready.clear()
                self._start_auth_worker()
            else:
                self._poison_and_close()
            raise AuthenticationError("Keyboard-interactive authentication was rejected") from error

        if not self.at_shell_prompt():
            self._poison_and_close()
            raise AuthenticationError("Authentication did not complete")

        self._transport.set_keepalive(self._settings.keepalive_s)

    # ------------------------------------------------------------------
    # Teardown helpers
    # ------------------------------------------------------------------

    def _best_effort_close(self, *, transport: object | None, raw_socket: object | None) -> None:
        try:
            if transport is not None:
                transport.close()  # type: ignore[union-attr]
        except BaseException:
            pass
        try:
            if raw_socket is not None:
                raw_socket.close()  # type: ignore[union-attr]
        except BaseException:
            pass

    def _poison_and_close(self) -> None:
        self._poisoned = True
        self._best_effort_close(transport=self._transport, raw_socket=self._socket)
        self._transport = None
        self._socket = None

    def stop_session(self) -> None:
        if self._closed:
            return
        self._best_effort_close(transport=self._transport, raw_socket=self._socket)
        self._transport = None
        self._socket = None
        self._closed = True
