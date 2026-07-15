"""The Operator auth seam (ADR-0002): turn the documented ``start_session() -> False`` RSA
prompt into an authenticated transport, and acquire Kerberos only when deep smoke needs it.

Secrets are read from the operator terminal (injectable for tests), held only transiently
in a local, and forwarded through :meth:`RemoteTransport.submit_secret` (never echoed)
— never through :meth:`run_remote`, which is echoed and captured into reports. RSA SecurID
tokencodes are single-use and rotate ~every 60s, so a stale/rejected code makes ``sshd``
re-display ``Enter PASSCODE:``; the seam re-prompts for a *fresh* code on rejection.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from edge_deploy.reporting import ReportCheck
from edge_deploy.transport import AuthenticationError

if TYPE_CHECKING:
    from edge_deploy.transport import RemoteTransport


@runtime_checkable
class AuthProgress(Protocol):
    """The minimal progress surface :class:`AuthBroker` needs from a caller.

    :class:`~edge_deploy.progress.ReleaseProgressTracker` satisfies this during a
    Release; standalone CLI commands (``rollout``, ``drift``) supply a small
    print-based adapter instead.
    """

    def emit(self, message: str, **kwargs: object) -> None: ...

    def set_waiting(self, waiting_on: str | None) -> None: ...


def _prompt_for_secret(prompt: str) -> str:
    """Read a secret from the operator terminal without hidden prompts."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")


class AuthBroker:
    """Single owner for per-node RSA authentication during release deploy."""

    def __init__(
        self,
        tracker: AuthProgress,
        auth_mode: str,
        wait_seconds: float,
        max_attempts: int,
    ) -> None:
        self._tracker = tracker
        self._auth_mode = auth_mode
        self._wait_seconds = wait_seconds
        self._max_attempts = max_attempts

    def ensure_authenticated(self, driver: RemoteTransport, node_name: str) -> None:
        try:
            if driver.session_exists() and driver.at_shell_prompt():
                return
        except AttributeError:
            pass
        except Exception:
            pass

        # Windows psmux has proven unreliable for secret-bearing send-keys:
        # the visible digits can be correct while sshd receives a rejected
        # response. Pane transports therefore require the operator to type
        # directly in the attached pane even when the CLI default is prompt.
        if self._auth_mode == "pane" or getattr(driver, "requires_manual_rsa_entry", False):
            self._authenticate_via_pane(driver, node_name)
        else:
            self._authenticate_via_prompt(driver, node_name)

    def _authenticate_via_prompt(self, driver: RemoteTransport, label: str) -> None:
        if driver.start_session(connect_timeout=None):
            return

        for attempt in range(1, self._max_attempts + 1):
            self._tracker.set_waiting("operator")
            try:
                code = _prompt_for_secret(f"[{label}] Enter RSA PASSCODE: ")
            finally:
                self._tracker.set_waiting(None)
            driver.submit_secret(code)
            try:
                driver.await_authenticated(timeout=self._wait_seconds)
                return
            except AuthenticationError:
                if attempt == self._max_attempts:
                    raise

    def _authenticate_via_pane(self, driver: RemoteTransport, label: str) -> None:
        try:
            session_exists = driver.session_exists()
        except Exception:
            session_exists = False

        if session_exists:
            try:
                if driver.at_shell_prompt():
                    return
            except AttributeError:
                pass
            except Exception:
                session_exists = False
            else:
                self._tracker.emit(
                    f"waiting for {label} RSA in existing tmux session {driver.session!r}; "
                    f"enter the current PASSCODE in that pane"
                )
                self._tracker.set_waiting("operator")
                try:
                    driver.await_authenticated(timeout=self._wait_seconds)
                finally:
                    self._tracker.set_waiting(None)
                return

        if driver.start_session(connect_timeout=None):
            return

        self._tracker.emit(
            f"waiting for {label} RSA in tmux session {driver.session!r}; "
            f"attach or open that pane and enter the current PASSCODE"
        )
        self._tracker.set_waiting("operator")
        try:
            driver.await_authenticated(timeout=self._wait_seconds)
        finally:
            self._tracker.set_waiting(None)


def ensure_kerberos(
    driver: RemoteTransport,
    label: str,
    *,
    prompt_fn: Callable[[str], str] | None = None,
    principal: str | None = None,
    max_attempts: int = 2,
) -> ReportCheck:
    """Ensure a valid Kerberos ticket exists, prompting for ``kinit`` only if needed.

    Returns a ``kerberos`` :class:`ReportCheck` (never raises): a valid existing ticket
    (``klist -s`` exit 0) short-circuits with no prompt; otherwise it runs ``kinit`` and
    forwards the password, re-checking ``klist -s`` up to ``max_attempts`` times.
    """
    read_secret = prompt_fn or _prompt_for_secret
    _screen, code = driver.run_remote("klist -s")
    if code == 0:
        return ReportCheck("kerberos", True, "Existing Kerberos ticket")

    kinit_command = f"kinit {principal}" if principal else "kinit"
    for _attempt in range(1, max_attempts + 1):
        driver.send_text(kinit_command)
        driver.wait_for(r"[Pp]assword.*:", timeout=15)
        driver.submit_secret(read_secret(f"[{label}] Kerberos password: "))
        _screen, code = driver.run_remote("klist -s")
        if code == 0:
            return ReportCheck("kerberos", True, "Kerberos ticket acquired")
    return ReportCheck("kerberos", False, "Could not acquire a Kerberos ticket")
