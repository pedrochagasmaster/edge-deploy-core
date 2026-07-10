from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


class TransportError(RuntimeError):
    """Redacted failure owned by a remote transport."""


class TransportUnavailable(TransportError):
    """The selected transport cannot be constructed."""


class HostKeyError(TransportError):
    """The server key is missing or does not match known_hosts."""


class AuthenticationError(TransportError):
    """Keyboard-interactive authentication was rejected or timed out."""


class ConnectionLostError(TransportError):
    """An authenticated transport became unusable."""


class RemoteCommandTimeout(TransportError):
    """A remote channel exceeded its monotonic deadline."""


class TransferError(TransportError):
    """A binary transfer or its verification failed."""


class InteractiveChannelError(TransportError):
    """A PTY dialogue failed."""


@dataclass(frozen=True)
class TransferProgress:
    bytes_sent: int
    total_bytes: int
    elapsed_s: float

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 100.0
        return min(100.0, 100.0 * self.bytes_sent / self.total_bytes)

    @property
    def bytes_per_second(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        return self.bytes_sent / self.elapsed_s


TransferProgressCallback = Callable[[TransferProgress], None]


@runtime_checkable
class RemoteTransport(Protocol):
    session: str

    def start_session(
        self,
        *,
        connect_timeout: float | None = None,
        passcode: str | None = None,
    ) -> bool: ...

    def session_exists(self) -> bool: ...
    def submit_secret(self, secret: str) -> None: ...

    def await_authenticated(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
    ) -> None: ...

    def at_shell_prompt(self, screen: str | None = None) -> bool: ...

    def run_remote(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        ensure_shell: bool = True,
    ) -> tuple[str, int]: ...

    def upload_file(
        self,
        source: str | Path,
        remote_path: str,
        *,
        progress: TransferProgressCallback | None = None,
    ) -> str: ...

    def send_text(self, text: str) -> None: ...

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> str: ...

    def stop_session(self) -> None: ...


def transport_for_node(
    node: object,
    profile: object,
    *,
    retries: int = 2,
    pane_log_path: Path | None = None,
) -> RemoteTransport:
    selected = getattr(node, "transport", "ssh")
    if selected == "ssh":
        from edge_deploy.ssh_transport import ParamikoSshTransport

        return ParamikoSshTransport.from_node_and_profile(node, profile, retries=retries)
    if selected == "pane":
        from edge_deploy.tmux_driver import TmuxDriver

        return TmuxDriver.from_node_and_profile(
            node,
            profile,
            retries=retries,
            pane_log_path=pane_log_path,
        )
    raise TransportUnavailable(f"unsupported transport {selected!r}")
