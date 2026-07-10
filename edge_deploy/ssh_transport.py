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

import hashlib
import queue
import re
import shlex
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

import paramiko

from edge_deploy.config import NodeConfig
from edge_deploy.preflight import endpoint_from_node
from edge_deploy.remote_paths import resolve_home_path
from edge_deploy.transport import (
    AuthenticationError,
    ConnectionLostError,
    HostKeyError,
    InteractiveChannelError,
    RemoteCommandTimeout,
    TransferError,
    TransferProgress,
    TransferProgressCallback,
    TransportUnavailable,
)

DEFAULT_KEEPALIVE_SECONDS = 5
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0
_AUTH_COMPLETION_TIMEOUT_SECONDS = 15.0
_PTY_TRANSCRIPT_LIMIT = 65536
_TRANSFER_TIMEOUT_SECONDS = 120.0
_TRANSFER_HASH_CHUNK_BYTES = 1024 * 1024
_PROGRESS_MIN_INTERVAL_S = 1.0


class SftpUnavailable(TransferError):
    """The authenticated transport rejected the ``sftp`` subsystem; fall back to a
    binary exec-channel transfer instead."""


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
        sftp_client_factory: Callable[[paramiko.Channel], object] = paramiko.SFTPClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self.session = session
        self._socket_factory = socket_factory
        self._transport_factory = transport_factory
        self._sftp_client_factory = sftp_client_factory
        self._clock = clock
        self._socket: socket.socket | None = None
        self._transport: paramiko.Transport | None = None
        self._remote_home: PurePosixPath | None = None
        self._interactive_channel: paramiko.Channel | None = None
        self._pty_transcript: bytearray = bytearray()
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
        """Forward a secret to the appropriate pending prompt.

        Before authentication completes, this feeds the keyboard-interactive queue for
        the RSA passcode. Once authenticated and a PTY dialogue is active (e.g. a
        ``kinit`` password prompt reached through :meth:`send_text` /
        :meth:`wait_for`), this writes the secret plus newline directly to the PTY
        instead. Never logged, echoed, or retained beyond the immediate handoff.
        """
        if self.at_shell_prompt() and self._interactive_channel is not None:
            self._submit_secret_to_pty(secret)
            return
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
        self._close_interactive_channel()
        self._best_effort_close(transport=self._transport, raw_socket=self._socket)
        self._transport = None
        self._socket = None

    def stop_session(self) -> None:
        if self._closed:
            return
        self._close_interactive_channel()
        self._best_effort_close(transport=self._transport, raw_socket=self._socket)
        self._transport = None
        self._socket = None
        self._closed = True

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _require_active_transport(self) -> paramiko.Transport:
        if self._transport is None or self._poisoned or not self._transport.is_active():
            raise ConnectionLostError("The Paramiko transport is not active")
        return self._transport

    def _channel_call_with_deadline(
        self, channel: paramiko.Channel, deadline: float, action: Callable[[], object]
    ) -> object:
        """Run a Paramiko channel call in a daemon worker and poison on deadline."""
        results: list[object] = []
        errors: list[BaseException] = []
        finished = threading.Event()

        def invoke() -> None:
            try:
                results.append(action())
            except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
                errors.append(exc)
            finally:
                finished.set()

        remaining = deadline - self._clock()
        if remaining <= 0:
            self._poison_and_close()
            raise RemoteCommandTimeout("Remote channel request exceeded its monotonic deadline")
        channel.settimeout(remaining)

        worker = threading.Thread(target=invoke, daemon=True, name=f"paramiko-exec-{self.session}")
        worker.start()
        remaining = deadline - self._clock()
        if remaining <= 0 or not finished.wait(remaining):
            self._poison_and_close()
            raise RemoteCommandTimeout("Remote channel request exceeded its monotonic deadline")
        if self._clock() >= deadline:
            self._poison_and_close()
            raise RemoteCommandTimeout("Remote channel request exceeded its monotonic deadline")
        if errors:
            if isinstance(errors[0], TimeoutError):
                self._poison_and_close()
                raise RemoteCommandTimeout("Remote channel request exceeded its monotonic deadline") from errors[0]
            raise errors[0]
        return results[0] if results else None

    def _open_session_channel(self, transport: paramiko.Transport, deadline: float) -> paramiko.Channel:
        remaining = deadline - self._clock()
        if remaining <= 0:
            self._poison_and_close()
            raise RemoteCommandTimeout("Session channel open exceeded its monotonic deadline")
        try:
            channel = transport.open_session(timeout=remaining)
        except BaseException as exc:
            self._poison_and_close()
            raise RemoteCommandTimeout("Session channel open exceeded its monotonic deadline") from exc
        if channel is None:
            self._poison_and_close()
            raise ConnectionLostError("Authenticated transport returned no session channel")
        channel.settimeout(max(deadline - self._clock(), 0.001))
        return channel

    def _drain_channel(self, channel: paramiko.Channel, *, deadline: float) -> tuple[bytearray, int]:
        """Interleave stdout/stderr reads in polling order and only fetch status once ready."""
        transcript = bytearray()
        while True:
            progressed = False
            while channel.recv_ready():
                block = channel.recv(65536)
                if block:
                    transcript.extend(block)
                    progressed = True
            while channel.recv_stderr_ready():
                block = channel.recv_stderr(65536)
                if block:
                    transcript.extend(block)
                    progressed = True

            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                exit_status = channel.recv_exit_status()
                return transcript, exit_status
            if self._clock() >= deadline:
                self._poison_and_close()
                raise RemoteCommandTimeout("Remote command exceeded its monotonic deadline")
            if not progressed:
                time.sleep(0.01)

    def run_remote(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        ensure_shell: bool = True,
    ) -> tuple[str, int]:
        """Run one command over a fresh session channel on the single reused transport.

        ``ensure_shell`` is accepted for :class:`RemoteTransport` compatibility; the
        Paramiko adapter always executes over a clean, non-interactive session channel
        so there is no pane state to return to.
        """
        del ensure_shell
        transport = self._require_active_transport()
        deadline = self._clock() + timeout
        channel = self._open_session_channel(transport, deadline)
        try:
            self._channel_call_with_deadline(channel, deadline, lambda: channel.exec_command(command))
            transcript, exit_status = self._drain_channel(channel, deadline=deadline)
        finally:
            if not self._poisoned:
                try:
                    channel.close()
                except BaseException:
                    pass
        text = transcript.decode("utf-8", errors="replace")
        return text, exit_status

    # ------------------------------------------------------------------
    # PTY dialogue
    # ------------------------------------------------------------------

    def _open_interactive_channel(self) -> paramiko.Channel:
        transport = self._require_active_transport()
        deadline = self._clock() + self._settings.connect_timeout_s
        channel = self._open_session_channel(transport, deadline)
        try:
            self._channel_call_with_deadline(
                channel, deadline, lambda: channel.get_pty(term="xterm", width=80, height=24)
            )
            self._channel_call_with_deadline(channel, deadline, lambda: channel.invoke_shell())
        except BaseException:
            try:
                channel.close()
            except BaseException:
                pass
            raise
        self._interactive_channel = channel
        self._pty_transcript = bytearray()
        return channel

    def _close_interactive_channel(self) -> None:
        channel = self._interactive_channel
        self._interactive_channel = None
        self._pty_transcript = bytearray()
        if channel is not None:
            try:
                channel.close()
            except BaseException:
                pass

    def send_text(self, text: str) -> None:
        """Send one line of non-secret text to the persistent PTY dialogue channel.

        Opens the PTY lazily on first use. Never send a secret through this method —
        use :meth:`submit_secret` so the value never travels through a transcript.
        """
        channel = self._interactive_channel or self._open_interactive_channel()
        try:
            channel.sendall(f"{text}\n".encode("utf-8"))
        except BaseException as exc:
            self._close_interactive_channel()
            raise InteractiveChannelError("Could not send text to the interactive channel") from exc

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> str:
        """Drain the interactive channel into a private bounded transcript until ``pattern``
        matches, then return the transcript decoded so far."""
        channel = self._interactive_channel
        if channel is None:
            raise InteractiveChannelError("No interactive channel is open")
        deadline = self._clock() + timeout
        compiled = re.compile(pattern)
        while True:
            while channel.recv_ready():
                block = channel.recv(4096)
                if block:
                    self._pty_transcript.extend(block)
                    if len(self._pty_transcript) > _PTY_TRANSCRIPT_LIMIT:
                        del self._pty_transcript[:-_PTY_TRANSCRIPT_LIMIT]
            text = self._pty_transcript.decode("utf-8", errors="replace")
            if compiled.search(text):
                return text
            if self._clock() >= deadline:
                self._close_interactive_channel()
                raise InteractiveChannelError(f"Timed out waiting for pattern {pattern!r}")
            time.sleep(poll_interval)

    def _submit_secret_to_pty(self, secret: str) -> None:
        channel = self._interactive_channel
        if channel is None:
            raise InteractiveChannelError("No interactive channel is open for a secret submission")
        try:
            channel.sendall(f"{secret}\n".encode("utf-8"))
        except BaseException as exc:
            self._close_interactive_channel()
            raise InteractiveChannelError("Could not send the secret to the interactive channel") from exc

    # ------------------------------------------------------------------
    # Verified binary transfer
    # ------------------------------------------------------------------

    def _resolve_remote_home(self, deadline: float) -> PurePosixPath:
        if self._remote_home is not None:
            return self._remote_home
        text, exit_status = self.run_remote('printf "%s" "$HOME"', timeout=max(deadline - self._clock(), 0.001))
        if exit_status != 0 or not text:
            raise TransferError("Could not resolve the remote home directory")
        if not text.startswith("/") or "\n" in text or "\r" in text:
            raise TransferError("Remote home directory was not an absolute single-line POSIX path")
        self._remote_home = PurePosixPath(text)
        return self._remote_home

    def _remote_sha256(self, remote_path: str, deadline: float) -> str | None:
        quoted = shlex.quote(remote_path)
        text, exit_status = self.run_remote(
            f"sha256sum -- {quoted} 2>/dev/null | awk '{{print $1}}'",
            timeout=max(deadline - self._clock(), 0.001),
        )
        digest = text.strip()
        if exit_status != 0 or len(digest) != 64:
            return None
        return digest

    def _atomic_mv(self, source: str, dest: str, deadline: float) -> None:
        quoted_source = shlex.quote(source)
        quoted_dest = shlex.quote(dest)
        _text, exit_status = self.run_remote(
            f"mv -f -- {quoted_source} {quoted_dest}",
            timeout=max(deadline - self._clock(), 0.001),
        )
        if exit_status != 0:
            raise TransferError("Could not atomically replace the destination file")

    def _remove_remote_best_effort(self, remote_path: str, deadline: float) -> None:
        quoted = shlex.quote(remote_path)
        try:
            self.run_remote(f"rm -f -- {quoted}", timeout=max(deadline - self._clock(), 1.0))
        except BaseException:
            pass

    def _verify_remote_size_and_digest(
        self, remote_path: str, *, expected_size: int, expected_digest: str, deadline: float
    ) -> None:
        quoted = shlex.quote(remote_path)
        text, exit_status = self.run_remote(
            f'stat -c "%s" -- {quoted}',
            timeout=max(deadline - self._clock(), 0.001),
        )
        if exit_status != 0 or text.strip() != str(expected_size):
            raise TransferError("Uploaded part file size verification failed")
        digest = self._remote_sha256(remote_path, deadline)
        if digest != expected_digest:
            raise TransferError("Uploaded part file digest verification failed")

    def upload_file(
        self,
        source: str | Path,
        remote_path: str,
        *,
        progress: TransferProgressCallback | None = None,
    ) -> str:
        """Stream ``source`` to ``remote_path`` over SFTP (falling back to a binary exec
        channel when the SFTP subsystem is unavailable), verifying the uploaded part
        file's size and SHA-256 digest before atomically replacing the destination.

        Reuses an existing destination whose remote digest already matches the local
        file's digest without transferring any bytes. Never reads the local file twice:
        it is hashed in 1 MiB chunks and then re-opened for the transfer itself.
        """
        source_path = Path(source)
        deadline = self._clock() + _TRANSFER_TIMEOUT_SECONDS
        home = self._resolve_remote_home(deadline)
        final_path = resolve_home_path(remote_path, str(home))

        local_digest = _sha256_file(source_path)
        total_bytes = source_path.stat().st_size
        started = self._clock()

        def emit(bytes_sent: int) -> None:
            if progress is not None:
                progress(
                    TransferProgress(
                        bytes_sent=bytes_sent,
                        total_bytes=total_bytes,
                        elapsed_s=self._clock() - started,
                    )
                )

        remote_digest = self._remote_sha256(final_path, deadline)
        if remote_digest == local_digest:
            emit(0)
            emit(total_bytes)
            return local_digest

        part_path = f"{final_path}.edge-deploy-{uuid.uuid4().hex}.part"
        emit(0)
        try:
            try:
                self._sftp_upload(source_path, part_path, total_bytes=total_bytes, deadline=deadline, emit=emit)
            except SftpUnavailable:
                self._stream_upload(source_path, part_path, total_bytes=total_bytes, deadline=deadline, emit=emit)
            self._verify_remote_size_and_digest(
                part_path, expected_size=total_bytes, expected_digest=local_digest, deadline=deadline
            )
            self._atomic_mv(part_path, final_path, deadline)
        except SftpUnavailable as exc:
            self._remove_remote_best_effort(part_path, deadline)
            raise TransferError("Binary transfer failed") from exc
        except TransferError:
            self._remove_remote_best_effort(part_path, deadline)
            raise
        except BaseException as exc:
            self._remove_remote_best_effort(part_path, deadline)
            raise TransferError("Binary transfer failed") from exc
        emit(total_bytes)
        return local_digest

    def _open_sftp_client(self, deadline: float) -> tuple[paramiko.SFTPClient, paramiko.Channel]:
        transport = self._require_active_transport()
        channel = self._open_session_channel(transport, deadline)
        try:
            try:
                self._channel_call_with_deadline(channel, deadline, lambda: channel.invoke_subsystem("sftp"))
            except paramiko.SSHException as exc:
                raise SftpUnavailable("The SFTP subsystem was rejected by the server") from exc
            try:
                sftp = self._channel_call_with_deadline(
                    channel, deadline, lambda: self._sftp_client_factory(channel)
                )
            except paramiko.SSHException as exc:
                raise SftpUnavailable("The SFTP subsystem was rejected by the server") from exc
            if sftp is None:
                raise TransferError("SFTP protocol construction returned no client")
            return sftp, channel
        except BaseException:
            if not self._poisoned:
                try:
                    channel.close()
                except BaseException:
                    pass
            raise

    def _sftp_upload(
        self,
        source_path: Path,
        part_path: str,
        *,
        total_bytes: int,
        deadline: float,
        emit: Callable[[int], None],
    ) -> None:
        sftp, channel = self._open_sftp_client(deadline)
        last_emit = self._clock()
        try:
            with source_path.open("rb") as handle:

                def on_sftp_progress(bytes_sent: int, _total: int) -> None:
                    nonlocal last_emit
                    now = self._clock()
                    if bytes_sent >= total_bytes or now - last_emit >= _PROGRESS_MIN_INTERVAL_S:
                        last_emit = now
                        emit(bytes_sent)

                self._channel_call_with_deadline(
                    channel,
                    deadline,
                    lambda: sftp.putfo(handle, part_path, file_size=total_bytes, callback=on_sftp_progress),
                )
            self._channel_call_with_deadline(channel, deadline, lambda: sftp.chmod(part_path, 0o600))
        finally:
            try:
                sftp.close()
            except BaseException:
                pass
            if not self._poisoned:
                try:
                    channel.close()
                except BaseException:
                    pass

    def _stream_upload(
        self,
        source_path: Path,
        part_path: str,
        *,
        total_bytes: int,
        deadline: float,
        emit: Callable[[int], None],
    ) -> None:
        """Upload over a binary exec channel by streaming raw chunks into ``cat``.

        No base64 encoding is used: the payload is sent as exact raw bytes via
        ``sendall()``, then the write side is closed so the remote ``cat`` sees EOF.
        """
        transport = self._require_active_transport()
        channel = self._open_session_channel(transport, deadline)
        quoted_part = shlex.quote(part_path)
        try:
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: channel.exec_command(f"umask 077; cat > {quoted_part}"),
            )
            sent = 0
            last_emit = self._clock()
            with source_path.open("rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    self._channel_call_with_deadline(channel, deadline, lambda c=chunk: channel.sendall(c))
                    sent += len(chunk)
                    now = self._clock()
                    if sent >= total_bytes or now - last_emit >= _PROGRESS_MIN_INTERVAL_S:
                        last_emit = now
                        emit(sent)
            self._channel_call_with_deadline(channel, deadline, channel.shutdown_write)
            _transcript, exit_status = self._drain_channel(channel, deadline=deadline)
        finally:
            if not self._poisoned:
                try:
                    channel.close()
                except BaseException:
                    pass
        if exit_status != 0:
            raise TransferError("Binary exec-channel upload failed")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_TRANSFER_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
