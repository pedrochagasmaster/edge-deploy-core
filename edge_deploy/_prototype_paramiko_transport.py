"""DISPOSABLE PROTOTYPE ONLY: one persistent Paramiko Transport per Edge Node.

This module intentionally lives beside the production package to test a possible
transport seam. It is not a production module, must remain untracked, and should
be deleted with ``../prototype_paramiko_transport.py`` after the experiment.

No operator configuration, endpoint, passcode, dummy PTY secret, digest, or
generated evidence is logged or persisted by this prototype.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import secrets
import shlex
import socket
import stat
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, TypeVar

import paramiko

from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, load_operator_config
from edge_deploy.preflight import endpoint_from_node

DEFAULT_KEEPALIVE_SECONDS = 5
TRANSFER_BYTES = 8 * 1024 * 1024
TRANSFER_TIMEOUT_SECONDS = 120.0
AUTH_COMPLETION_TIMEOUT_SECONDS = 15.0
COMMAND_STDOUT = b"prototype stdout exact\n"
COMMAND_STDERR = b"prototype stderr exact\n"
PTY_READY_LF = b"PTY_READY\n"
PTY_READY_CRLF = b"PTY_READY\r\n"
PTY_EXPECTED_TRANSCRIPT = b"PTY_READY\nDUMMY_SECRET_OK\n"

ResultT = TypeVar("ResultT")


class PrototypeError(RuntimeError):
    """A deliberately endpoint-safe prototype failure."""


class SftpUnavailable(PrototypeError):
    """The authenticated transport could not open an SFTP subsystem."""

    def __init__(self, reason: str) -> None:
        super().__init__("SFTP subsystem unavailable")
        self.reason = reason


@dataclass(frozen=True)
class NodeSettings:
    """Private in-memory connection settings; never print or persist this object."""

    label: str
    username: str
    hostname: str
    port: int
    keepalive_seconds: int


@dataclass(frozen=True)
class CommandResult:
    stdout: bytes
    stderr: bytes
    exit_status: int


@dataclass(frozen=True)
class TransferResult:
    sftp_available: bool
    fallback_reason: str
    upload_mib_per_second: float
    download_mib_per_second: float

    @property
    def summary(self) -> str:
        rates = (
            f"upload {self.upload_mib_per_second:.1f} MiB/s, "
            f"download {self.download_mib_per_second:.1f} MiB/s, SHA-256 identical"
        )
        if self.sftp_available:
            return f"SFTP available; {rates}"
        return f"SFTP unavailable ({self.fallback_reason}); binary exec fallback passed; {rates}"


@dataclass(frozen=True)
class ProbeResult:
    summary: str


@dataclass
class ChecklistItem:
    key: str
    name: str
    action: Callable[[], ProbeResult]
    status: str = "PENDING"
    detail: str = ""


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


def _ssh_option_values(ssh_options: str) -> tuple[int | None, int]:
    """Return an optional port override and keepalive interval from common OpenSSH CLI forms."""
    parts = shlex.split(ssh_options or "")
    port: int | None = None
    keepalive = DEFAULT_KEEPALIVE_SECONDS
    index = 0
    while index < len(parts):
        part = parts[index]
        lowered = part.lower()
        option: tuple[str, str] | None = None

        if part == "-p" and index + 1 < len(parts):
            port = int(parts[index + 1])
            index += 2
            continue
        if lowered.startswith("-p") and len(part) > 2:
            port = int(part[2:])
            index += 1
            continue
        if part == "-o" and index + 1 < len(parts):
            option = _parse_open_ssh_option(parts[index + 1])
            index += 2
        elif lowered.startswith("-o") and len(part) > 2:
            option = _parse_open_ssh_option(part[2:])
            index += 1
        else:
            option = _parse_open_ssh_option(part)
            index += 1

        if option is None:
            continue
        key, value = option
        if key == "port":
            port = int(value)
        elif key == "serveraliveinterval":
            keepalive = int(value)

    if port is not None and not 1 <= port <= 65535:
        raise PrototypeError("SSH port is outside the valid range")
    if keepalive <= 0:
        raise PrototypeError("ServerAliveInterval must be a positive integer")
    return port, keepalive


def load_node_settings(
    node_label: str,
    config_path: str | Path = DEFAULT_OPERATOR_CONFIG_PATH,
) -> NodeSettings:
    """Load one node through repository interfaces without exposing private configuration."""
    operator = load_operator_config(config_path)
    node = operator.node(node_label)
    endpoint = endpoint_from_node(node)
    username, separator, configured_hostname = node.host.rpartition("@")
    if not separator or not username or not configured_hostname:
        raise PrototypeError("Node host must use the user@host form")

    port_override, keepalive = _ssh_option_values(node.ssh_options)
    hostname = endpoint.hostname.strip()
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    if not hostname:
        raise PrototypeError("Node hostname is empty")

    return NodeSettings(
        label=node_label,
        username=username,
        hostname=hostname,
        port=port_override if port_override is not None else endpoint.port,
        keepalive_seconds=keepalive,
    )


def _known_host_lookup_name(hostname: str, port: int) -> str:
    return hostname if port == 22 else f"[{hostname}]:{port}"


def _verify_server_key(server_key: paramiko.PKey, hostname: str, port: int) -> None:
    """Require an exact key match in ~/.ssh/known_hosts, including nonstandard-port syntax."""
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    host_keys = paramiko.HostKeys()
    try:
        host_keys.load(str(known_hosts_path))
    except OSError as exc:
        raise PrototypeError("Strict known-host verification could not load ~/.ssh/known_hosts") from exc

    lookup_name = _known_host_lookup_name(hostname, port)
    known_for_host = host_keys.lookup(lookup_name)
    if known_for_host is None:
        raise PrototypeError("Server key is not present in ~/.ssh/known_hosts")
    expected_key = known_for_host.get(server_key.get_name())
    if expected_key is None or expected_key != server_key:
        raise PrototypeError("Server key does not exactly match ~/.ssh/known_hosts")


class PersistentParamikoPrototype:
    """Own exactly one authenticated Paramiko Transport and open stateless channels on it."""

    def __init__(self, settings: NodeSettings) -> None:
        self._settings = settings
        self._transport: paramiko.Transport | None = None
        self._transport_identity: int | None = None
        self._connection_count = 0
        self._remote_home: PurePosixPath | None = None
        token = uuid.uuid4().hex
        self._remote_run_relative = f".edge-deploy/.paramiko-prototype-{token}"
        self._remote_paths = {
            "sentinel": "sentinel",
            "sftp": "sftp-payload",
            "fallback": "stream-payload",
        }
        self._scratch_prepared = False
        self._closed = False
        self._poisoned = False
        self._cleanup_outcome: bool | None = None

    def connect(self) -> None:
        if self._closed:
            raise PrototypeError("Closed prototype cannot create another Transport")
        if self._poisoned:
            raise PrototypeError("Poisoned prototype cannot create another Transport")
        if self._transport is not None:
            raise PrototypeError("Prototype permits only one Transport")

        raw_socket: socket.socket | None = None
        transport: paramiko.Transport | None = None
        try:
            raw_socket = socket.create_connection(
                (self._settings.hostname, self._settings.port),
                timeout=15.0,
            )
            transport = paramiko.Transport(raw_socket)
            transport.start_client(timeout=15.0)
            _verify_server_key(
                transport.get_remote_server_key(),
                self._settings.hostname,
                self._settings.port,
            )

            prompt_count = 0
            submitted = threading.Event()
            auth_finished = threading.Event()
            auth_errors: list[BaseException] = []

            def hidden_keyboard_interactive(
                _title: str,
                _instructions: str,
                prompts: list[tuple[str, bool]],
            ) -> list[str]:
                nonlocal prompt_count
                if not prompts:
                    return []
                if prompt_count != 0 or len(prompts) != 1:
                    raise PrototypeError("Expected exactly one keyboard-interactive prompt")
                _prompt, echo = prompts[0]
                if echo:
                    raise PrototypeError("Keyboard-interactive passcode prompt requested echo")
                prompt_count += 1
                with warnings.catch_warnings():
                    # Refuse getpass's echoed-input fallback when no controlling
                    # terminal exists; a hidden controller-terminal prompt is mandatory.
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    passcode = getpass.getpass("SSH passcode: ")
                submitted.set()
                return [passcode]

            def authenticate() -> None:
                try:
                    transport.auth_interactive(self._settings.username, hidden_keyboard_interactive)
                except BaseException as exc:
                    auth_errors.append(exc)
                finally:
                    auth_finished.set()

            auth_worker = threading.Thread(
                target=authenticate,
                daemon=True,
                name="paramiko-prototype-auth",
            )
            auth_worker.start()
            while not submitted.is_set() and not auth_finished.wait(0.05):
                pass
            if submitted.is_set() and not auth_finished.wait(AUTH_COMPLETION_TIMEOUT_SECONDS):
                self._poison_and_teardown(transport=transport, raw_socket=raw_socket)
                raise PrototypeError("Keyboard-interactive authentication exceeded its post-submission deadline")
            if auth_errors:
                raise auth_errors[0]
            if prompt_count != 1:
                raise PrototypeError("Keyboard-interactive authentication did not issue exactly one prompt")
            if not transport.is_authenticated():
                raise PrototypeError("Keyboard-interactive authentication did not complete")
            transport.set_keepalive(self._settings.keepalive_seconds)

            self._transport = transport
            self._transport_identity = id(transport)
            self._connection_count = 1
            raw_socket = None
            transport = None
            try:
                self._prepare_remote_scratch()
            except BaseException:
                self.close()
                raise
        except BaseException:
            if self._poisoned:
                self._poison_and_teardown(transport=transport, raw_socket=raw_socket)
            else:
                try:
                    if transport is not None:
                        transport.close()
                except BaseException:
                    pass
                try:
                    if raw_socket is not None:
                        raw_socket.close()
                except BaseException:
                    pass
            raise

    def _poison_and_teardown(
        self,
        *,
        channel: paramiko.Channel | None = None,
        transport: paramiko.Transport | None = None,
        raw_socket: socket.socket | None = None,
    ) -> None:
        """Permanently reject reuse and tear down blocking Paramiko objects asynchronously."""
        self._poisoned = True
        target_transport = transport if transport is not None else self._transport

        resources: list[tuple[str, object]] = []
        if raw_socket is not None:
            resources.append(("socket", raw_socket))
        if target_transport is not None:
            transport_socket = getattr(target_transport, "sock", None)
            if transport_socket is not None and transport_socket is not raw_socket:
                resources.append(("transport-socket", transport_socket))
            resources.append(("transport", target_transport))
        if channel is not None:
            resources.append(("channel", channel))

        def close_resource(resource: object) -> None:
            try:
                close = getattr(resource, "close")
                close()
            except BaseException:
                pass

        for label, resource in resources:
            threading.Thread(
                target=close_resource,
                args=(resource,),
                daemon=True,
                name=f"paramiko-prototype-teardown-{label}",
            ).start()

    def _require_reused_transport(self) -> paramiko.Transport:
        transport = self._transport
        if (
            transport is None
            or self._closed
            or self._poisoned
            or not transport.is_active()
            or not transport.is_authenticated()
            or id(transport) != self._transport_identity
            or self._connection_count != 1
        ):
            raise PrototypeError("Persistent transport reuse invariant failed")
        return transport

    def _remaining(self, deadline: float, *, channel: paramiko.Channel | None = None) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._poison_and_teardown(channel=channel)
            raise PrototypeError("Remote operation exceeded its monotonic deadline")
        return remaining

    def _channel_call_with_deadline(
        self,
        channel: paramiko.Channel,
        deadline: float,
        action: Callable[[], ResultT],
    ) -> ResultT:
        """Run a Paramiko channel call in a daemon worker and close on deadline."""
        results: list[ResultT] = []
        errors: list[BaseException] = []
        finished = threading.Event()

        def invoke() -> None:
            try:
                results.append(action())
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        channel.settimeout(self._remaining(deadline, channel=channel))
        worker = threading.Thread(target=invoke, daemon=True, name="paramiko-prototype-request")
        worker.start()
        if not finished.wait(self._remaining(deadline, channel=channel)):
            self._poison_and_teardown(channel=channel)
            raise PrototypeError("Remote channel request exceeded its monotonic deadline")
        if time.monotonic() >= deadline:
            self._poison_and_teardown(channel=channel)
            raise PrototypeError("Remote channel request exceeded its monotonic deadline")
        if errors:
            if isinstance(errors[0], TimeoutError):
                self._poison_and_teardown(channel=channel)
                raise PrototypeError("Remote channel request exceeded its monotonic deadline") from errors[0]
            raise errors[0]
        if not results:
            raise PrototypeError("Remote channel request returned no result")
        return results[0]

    def _open_session(self, transport: paramiko.Transport, deadline: float) -> paramiko.Channel:
        try:
            channel = transport.open_session(timeout=self._remaining(deadline))
        except BaseException as exc:
            timed_out = isinstance(exc, TimeoutError) or str(exc) == "Timeout opening channel."
            if timed_out or time.monotonic() >= deadline:
                self._poison_and_teardown(transport=transport)
                raise PrototypeError("Session channel open exceeded its monotonic deadline") from exc
            raise
        if channel is None:
            raise PrototypeError("Authenticated transport returned no session channel")
        channel.settimeout(self._remaining(deadline, channel=channel))
        return channel

    def _execute(
        self,
        command: str,
        *,
        input_bytes: bytes | None = None,
        pty: bool = False,
        timeout_seconds: float = 30.0,
        deadline: float | None = None,
    ) -> CommandResult:
        transport = self._require_reused_transport()
        effective_deadline = deadline if deadline is not None else time.monotonic() + timeout_seconds
        channel = self._open_session(transport, effective_deadline)
        try:
            if pty:
                self._channel_call_with_deadline(
                    channel,
                    effective_deadline,
                    lambda: channel.get_pty(term="xterm", width=80, height=24),
                )
            self._channel_call_with_deadline(
                channel,
                effective_deadline,
                lambda: channel.exec_command(command),
            )
            if input_bytes is not None:
                self._channel_call_with_deadline(
                    channel,
                    effective_deadline,
                    lambda: channel.sendall(input_bytes),
                )
                self._channel_call_with_deadline(
                    channel,
                    effective_deadline,
                    channel.shutdown_write,
                )
            return self._drain_channel(channel, deadline=effective_deadline)
        finally:
            if not self._poisoned:
                channel.close()

    def _drain_channel(
        self,
        channel: paramiko.Channel,
        *,
        deadline: float,
        initial_stdout: bytes = b"",
    ) -> CommandResult:
        """Interleave exact-byte stream reads and only fetch status once it is ready."""
        stdout = bytearray(initial_stdout)
        stderr = bytearray()
        while True:
            progressed = False
            while channel.recv_ready():
                block = channel.recv(65536)
                if block:
                    stdout.extend(block)
                    progressed = True
            while channel.recv_stderr_ready():
                block = channel.recv_stderr(65536)
                if block:
                    stderr.extend(block)
                    progressed = True

            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                exit_status = channel.recv_exit_status()
                return CommandResult(bytes(stdout), bytes(stderr), exit_status)
            if time.monotonic() >= deadline:
                self._poison_and_teardown(channel=channel)
                raise PrototypeError("Remote command exceeded its monotonic deadline")
            if not progressed:
                time.sleep(0.01)

    def _remote_path(self, name: str) -> str:
        if self._remote_home is None:
            raise PrototypeError("Remote scratch is not prepared")
        return str(self._remote_home / self._remote_run_relative / self._remote_paths[name])

    def _remote_run_directory(self) -> str:
        if self._remote_home is None:
            raise PrototypeError("Remote scratch is not prepared")
        return str(self._remote_home / self._remote_run_relative)

    def _prepare_remote_scratch(self) -> None:
        home_result = self._execute('printf "%s" "$HOME"')
        if home_result.exit_status != 0 or home_result.stderr or not home_result.stdout:
            raise PrototypeError("Could not resolve remote home directory")
        try:
            home = home_result.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PrototypeError("Remote home directory was not UTF-8") from exc
        if not home.startswith("/") or "\n" in home or "\r" in home:
            raise PrototypeError("Remote home directory was not an absolute POSIX path")
        self._remote_home = PurePosixPath(home)

        parent_directory = shlex.quote(str(self._remote_home / ".edge-deploy"))
        prepare_parent = self._execute(
            f"umask 077; mkdir -p -- {parent_directory} && "
            f"test -d {parent_directory} && "
            f"test ! -L {parent_directory} && "
            f'test "$(stat -c \'%u\' -- {parent_directory})" = "$(id -u)"'
        )
        if prepare_parent.exit_status != 0:
            raise PrototypeError("Remote scratch parent ownership verification failed")

        run_directory = shlex.quote(self._remote_run_directory())
        self._scratch_prepared = True
        prepare_run = self._execute(
            f"umask 077; mkdir -m 700 -- {run_directory} && "
            f"test -d {run_directory} && "
            f"test ! -L {run_directory} && "
            f'test "$(stat -c \'%u\' -- {run_directory})" = "$(id -u)" && '
            f'test "$(stat -c \'%a\' -- {run_directory})" = 700'
        )
        if prepare_run.exit_status != 0:
            raise PrototypeError("Unique remote scratch directory could not be securely created")
        self._create_remote_owned_file(self._remote_path("sentinel"))

    def _create_remote_owned_file(self, remote_path: str, *, deadline: float | None = None) -> None:
        """Exclusively create one unique 0600 regular file owned by the remote user."""
        quoted_path = shlex.quote(remote_path)
        create = self._execute(
            f"umask 077; set -C; : > {quoted_path} && "
            f"test ! -L {quoted_path} && "
            f'test "$(stat -c \'%u\' -- {quoted_path})" = "$(id -u)" && '
            f"chmod 600 -- {quoted_path} && "
            f'test "$(stat -c \'%a\' -- {quoted_path})" = 600 && '
            f"test -f {quoted_path}",
            deadline=deadline,
        )
        if create.exit_status != 0:
            raise PrototypeError("Remote scratch file could not be securely created")

    def _verify_remote_owned_file(
        self,
        remote_path: str,
        *,
        expected_size: int | None = None,
        deadline: float | None = None,
    ) -> None:
        """Verify owner and exact mode before any optional size integrity check."""
        quoted_path = shlex.quote(remote_path)
        checks = (
            f"test ! -L {quoted_path} && "
            f'test "$(stat -c \'%u\' -- {quoted_path})" = "$(id -u)" && '
            f'test "$(stat -c \'%a\' -- {quoted_path})" = 600 && '
            f"test -f {quoted_path}"
        )
        if expected_size is not None:
            checks += f' && test "$(stat -c \'%s\' -- {quoted_path})" = {expected_size}'
        verified = self._execute(checks, deadline=deadline)
        if verified.exit_status != 0:
            raise PrototypeError("Remote scratch file ownership, mode, or size verification failed")

    def probe_command(self) -> ProbeResult:
        result = self._execute(
            "printf 'prototype stdout exact\\n'; "
            "printf 'prototype stderr exact\\n' >&2; "
            "exit 7"
        )
        if (
            result.stdout != COMMAND_STDOUT
            or result.stderr != COMMAND_STDERR
            or result.exit_status != 7
        ):
            raise PrototypeError("Command streams or exit status were not exact")
        self._require_reused_transport()
        return ProbeResult("exact stdout/stderr and exit 7; transport #1 reused")

    def _remote_sha256(self, remote_path: str, *, deadline: float | None = None) -> str:
        quoted_path = shlex.quote(remote_path)
        result = self._execute(
            f"sha256sum -- {quoted_path} | awk '{{print $1}}'",
            deadline=deadline,
        )
        if result.exit_status != 0 or result.stderr:
            raise PrototypeError("Remote SHA-256 calculation failed")
        digest = result.stdout.decode("ascii", errors="strict").strip()
        if len(digest) != 64:
            raise PrototypeError("Remote SHA-256 result was malformed")
        return digest

    def _open_sftp(self, deadline: float) -> tuple[paramiko.SFTPClient, paramiko.Channel]:
        transport = self._require_reused_transport()
        channel = self._open_session(transport, deadline)
        try:
            try:
                self._channel_call_with_deadline(
                    channel,
                    deadline,
                    lambda: channel.invoke_subsystem("sftp"),
                )
            except paramiko.SSHException as exc:
                same_healthy_transport = (
                    self._transport is transport
                    and id(transport) == self._transport_identity
                    and self._connection_count == 1
                    and transport.is_active()
                    and transport.is_authenticated()
                )
                confirmed_rejection = (
                    same_healthy_transport
                    and channel.closed
                    and str(exc) == "Channel closed."
                )
                if confirmed_rejection:
                    raise SftpUnavailable("SubsystemRejected") from exc
                raise

            sftp = self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: paramiko.SFTPClient(channel),
            )
            if sftp is None:
                raise PrototypeError("SFTP protocol construction returned no client")
            return sftp, channel
        except BaseException:
            if not self._poisoned:
                channel.close()
            raise

    def _sftp_transfer(
        self,
        payload: bytes,
        remote_path: str,
        deadline: float,
    ) -> tuple[bytes, float, float]:
        self._create_remote_owned_file(remote_path, deadline=deadline)
        sftp, channel = self._open_sftp(deadline)
        started_upload = time.monotonic()
        try:
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: sftp.putfo(
                    io.BytesIO(payload),
                    remote_path,
                    file_size=len(payload),
                    confirm=False,
                ),
            )
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: sftp.chmod(remote_path, 0o600),
            )
            upload_seconds = time.monotonic() - started_upload
            self._verify_remote_owned_file(remote_path, deadline=deadline)
            remote_stat = self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: sftp.stat(remote_path),
            )
            if stat.S_IMODE(remote_stat.st_mode) != 0o600 or remote_stat.st_size != len(payload):
                raise PrototypeError("SFTP upload mode or size verification failed")
            downloaded = io.BytesIO()
            started_download = time.monotonic()
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: sftp.getfo(remote_path, downloaded),
            )
            download_seconds = time.monotonic() - started_download
        finally:
            if not self._poisoned:
                channel.close()
        return downloaded.getvalue(), upload_seconds, download_seconds

    def _stream_transfer(
        self,
        payload: bytes,
        remote_path: str,
        deadline: float,
    ) -> tuple[bytes, float, float]:
        quoted_path = shlex.quote(remote_path)
        started_upload = time.monotonic()
        upload = self._execute(
            f"umask 077; set -C; cat > {quoted_path}; "
            f"status=$?; test $status -eq 0 || exit $status; "
            f"test ! -L {quoted_path} && "
            f'test "$(stat -c \'%u\' -- {quoted_path})" = "$(id -u)" && '
            f"chmod 600 -- {quoted_path}",
            input_bytes=payload,
            deadline=deadline,
        )
        upload_seconds = time.monotonic() - started_upload
        if upload.exit_status != 0 or upload.stdout or upload.stderr:
            raise PrototypeError("Binary exec-channel upload failed")
        self._verify_remote_owned_file(
            remote_path,
            expected_size=len(payload),
            deadline=deadline,
        )

        started_download = time.monotonic()
        download = self._execute(f"cat -- {quoted_path}", deadline=deadline)
        download_seconds = time.monotonic() - started_download
        if download.exit_status != 0 or download.stderr:
            raise PrototypeError("Binary exec-channel download failed")
        return download.stdout, upload_seconds, download_seconds

    def probe_transfer(self) -> ProbeResult:
        deadline = time.monotonic() + TRANSFER_TIMEOUT_SECONDS
        payload = secrets.token_bytes(TRANSFER_BYTES)
        local_digest = hashlib.sha256(payload).hexdigest()
        sftp_available = True
        fallback_reason = ""
        remote_path = self._remote_path("sftp")
        try:
            downloaded, upload_seconds, download_seconds = self._sftp_transfer(
                payload,
                remote_path,
                deadline,
            )
        except SftpUnavailable as exc:
            sftp_available = False
            fallback_reason = exc.reason
            remote_path = self._remote_path("fallback")
            downloaded, upload_seconds, download_seconds = self._stream_transfer(
                payload,
                remote_path,
                deadline,
            )

        remote_digest = self._remote_sha256(remote_path, deadline=deadline)
        downloaded_digest = hashlib.sha256(downloaded).hexdigest()
        if len(downloaded) != TRANSFER_BYTES or not (
            local_digest == remote_digest == downloaded_digest
        ):
            raise PrototypeError("Transferred payload SHA-256 values did not match")
        self._require_reused_transport()

        mib = TRANSFER_BYTES / (1024 * 1024)
        result = TransferResult(
            sftp_available=sftp_available,
            fallback_reason=fallback_reason,
            upload_mib_per_second=mib / max(upload_seconds, 1e-9),
            download_mib_per_second=mib / max(download_seconds, 1e-9),
        )
        return ProbeResult(f"{result.summary}; transport #1 reused")

    def probe_pty(self) -> ProbeResult:
        dummy_secret = secrets.token_urlsafe(32)
        dummy_secret_bytes = dummy_secret.encode("utf-8")
        transmitted_secret = dummy_secret_bytes + b"\n"
        expected_digest = hashlib.sha256(dummy_secret_bytes).hexdigest()
        command = (
            "restore_echo() { stty echo >/dev/null 2>&1 || :; }; "
            "trap restore_echo EXIT; "
            "trap 'restore_echo; exit 12' HUP INT TERM; "
            "stty -echo || exit 10; "
            "printf 'PTY_READY\\n'; "
            "IFS= read -r candidate || exit 11; "
            "actual=$(printf '%s' \"$candidate\" | sha256sum | awk '{print $1}'); "
            f"if [ \"$actual\" = {shlex.quote(expected_digest)} ]; then "
            "printf 'DUMMY_SECRET_OK\\n'; "
            "else exit 9; fi"
        )
        transport = self._require_reused_transport()
        deadline = time.monotonic() + 30.0
        channel = self._open_session(transport, deadline)
        try:
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: channel.get_pty(term="xterm", width=80, height=24),
            )
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: channel.exec_command(command),
            )
            prelude = bytearray()
            while PTY_READY_CRLF not in prelude and PTY_READY_LF not in prelude:
                block = self._channel_call_with_deadline(
                    channel,
                    deadline,
                    lambda: channel.recv(1024),
                )
                if not block:
                    raise PrototypeError("PTY closed before disabling terminal echo")
                prelude.extend(block)
                if len(prelude) > 4096:
                    raise PrototypeError("PTY readiness output was unexpectedly large")
            wire_marker = PTY_READY_CRLF if PTY_READY_CRLF in prelude else PTY_READY_LF
            marker_end = prelude.index(wire_marker) + len(wire_marker)
            self._channel_call_with_deadline(
                channel,
                deadline,
                lambda: channel.sendall(transmitted_secret),
            )
            self._channel_call_with_deadline(channel, deadline, channel.shutdown_write)
            result = self._drain_channel(
                channel,
                deadline=deadline,
                initial_stdout=bytes(prelude[marker_end:]),
            )
            complete_stdout = bytes(prelude[:marker_end]) + result.stdout
            all_received = complete_stdout + result.stderr
            normalized_stdout = complete_stdout.replace(b"\r\n", b"\n")
            if (
                dummy_secret_bytes in all_received
                or result.exit_status != 0
                or result.stderr
                or normalized_stdout != PTY_EXPECTED_TRANSCRIPT
            ):
                raise PrototypeError("PTY dummy-secret validation failed")
            self._require_reused_transport()
            return ProbeResult("generated dummy secret validated remotely via PTY; transport #1 reused")
        finally:
            dummy_secret = ""
            dummy_secret_bytes = b""
            transmitted_secret = b""
            expected_digest = ""
            if not self._poisoned:
                channel.close()

    def probe_keepalive(self) -> ProbeResult:
        transport = self._require_reused_transport()
        interval = self._settings.keepalive_seconds
        transport.set_keepalive(interval)
        duration = (interval * 2) + 1
        started = time.monotonic()
        result = self._execute(
            f"sleep {duration}; printf 'KEEPALIVE_OK\\n'",
            timeout_seconds=duration + 30.0,
        )
        elapsed = time.monotonic() - started
        if (
            result.exit_status != 0
            or result.stderr
            or result.stdout != b"KEEPALIVE_OK\n"
            or elapsed < interval * 2
        ):
            raise PrototypeError("Command did not survive two keepalive intervals")
        self._require_reused_transport()
        return ProbeResult(
            f"{interval}s keepalive; command completed after {elapsed:.1f}s across two intervals; "
            "transport #1 reused"
        )

    def close(self) -> bool:
        """Best-effort cleanup every owned path, remove the run directory, and close."""
        if self._closed:
            return bool(self._cleanup_outcome)
        cleanup_confirmed = not self._scratch_prepared
        transport = self._transport
        try:
            if self._poisoned:
                cleanup_confirmed = False
                self._poison_and_teardown(transport=transport)
            elif transport is not None and transport.is_active() and self._scratch_prepared:
                cleanup_confirmed = True
                quoted_paths = [
                    shlex.quote(self._remote_path(name))
                    for name in self._remote_paths
                ]

                for path in quoted_paths:
                    try:
                        removal = self._execute(
                            f"if test -e {path} || test -L {path}; then "
                            f"test ! -L {path} && "
                            f"test -f {path} && "
                            f'test "$(stat -c \'%u\' -- {path})" = "$(id -u)" && '
                            f"rm -f -- {path}; fi",
                            timeout_seconds=5.0,
                        )
                        cleanup_confirmed &= removal.exit_status == 0
                    except BaseException:
                        cleanup_confirmed = False

                for path in quoted_paths:
                    try:
                        absence = self._execute(
                            f"test ! -e {path} && test ! -L {path}",
                            timeout_seconds=5.0,
                        )
                        cleanup_confirmed &= absence.exit_status == 0
                    except BaseException:
                        cleanup_confirmed = False

                run_directory = shlex.quote(self._remote_run_directory())
                try:
                    remove_directory = self._execute(
                        f"if test -e {run_directory} || test -L {run_directory}; then "
                        f"test ! -L {run_directory} && "
                        f"test -d {run_directory} && "
                        f'test "$(stat -c \'%u\' -- {run_directory})" = "$(id -u)" && '
                        f"rmdir -- {run_directory}; fi",
                        timeout_seconds=5.0,
                    )
                    cleanup_confirmed &= remove_directory.exit_status == 0
                except BaseException:
                    cleanup_confirmed = False

                try:
                    directory_absent = self._execute(
                        f"test ! -e {run_directory} && test ! -L {run_directory}",
                        timeout_seconds=5.0,
                    )
                    cleanup_confirmed &= directory_absent.exit_status == 0
                except BaseException:
                    cleanup_confirmed = False
            elif self._scratch_prepared:
                cleanup_confirmed = False
        except BaseException:
            cleanup_confirmed = False
        finally:
            if transport is not None:
                if self._poisoned:
                    cleanup_confirmed = False
                    self._poison_and_teardown(transport=transport)
                else:
                    try:
                        transport.close()
                    except BaseException:
                        cleanup_confirmed = False
            self._cleanup_outcome = cleanup_confirmed
            self._closed = True
            self._transport = None
        return cleanup_confirmed


class PrototypeChecklist:
    """Small terminal checklist whose probes all share one authenticated Transport."""

    def __init__(self, node_label: str, prototype: PersistentParamikoPrototype) -> None:
        self._node_label = node_label
        self._items = [
            ChecklistItem("c", "command", prototype.probe_command),
            ChecklistItem("t", "transfer", prototype.probe_transfer),
            ChecklistItem("p", "PTY input", prototype.probe_pty),
            ChecklistItem("k", "keepalive", prototype.probe_keepalive),
        ]

    def _render(self) -> None:
        print("\033[2J\033[H", end="")
        print(f"DISPOSABLE Paramiko prototype — {self._node_label}")
        print("One authenticated transport; stateless channels per probe.\n")
        for item in self._items:
            detail = f" — {item.detail}" if item.detail else ""
            print(f"[{item.key}] {item.name:<10} {item.status}{detail}")
        print(f"\nVerdict: {self._verdict()}")
        print("[a] run all  [q] cleanup, close, quit")

    def _verdict(self) -> str:
        if any(item.status == "FAILED" for item in self._items):
            return "FAILED"
        if all(item.status == "PASSED" for item in self._items):
            return "PASS"
        return "INCOMPLETE"

    def _run_item(self, item: ChecklistItem) -> bool:
        item.status = "RUNNING"
        item.detail = ""
        self._render()
        try:
            result = item.action()
        except Exception as exc:  # Suppress exception messages because libraries may include endpoint details.
            item.status = "FAILED"
            item.detail = type(exc).__name__
            return False
        item.status = "PASSED"
        item.detail = result.summary
        return True

    def run(self) -> int:
        while True:
            self._render()
            try:
                action = input("> ").strip().lower()
            except EOFError:
                action = "q"
            if action == "q":
                return 0 if self._verdict() == "PASS" else 1
            if action == "a":
                for item in self._items:
                    self._run_item(item)
                continue
            selected = next((item for item in self._items if item.key == action), None)
            if selected is not None:
                self._run_item(selected)
