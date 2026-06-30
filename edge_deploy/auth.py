"""The Operator auth seam (ADR-0002): turn the documented ``start_session() -> False`` RSA
prompt into an authenticated pane, and acquire Kerberos only when deep smoke needs it.

Secrets are read with ``getpass`` (injectable for tests), held only transiently in a local,
and forwarded through :meth:`TmuxDriver.submit_secret` (literal ``send_keys``) — never
through :meth:`run_remote`, which is echoed and captured into reports. RSA SecurID
tokencodes are single-use and rotate ~every 60s, so a stale/rejected code makes ``sshd``
re-display ``Enter PASSCODE:``; the seam re-prompts for a *fresh* code on rejection.
"""

from __future__ import annotations

import getpass
from typing import Callable

from edge_deploy.reporting import ReportCheck
from edge_deploy.tmux_driver import AuthenticationError, TmuxDriver


def authenticate_node(
    driver: TmuxDriver,
    label: str,
    *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    max_attempts: int = 3,
    connect_timeout: float | None = None,
    wait_timeout: float = 300.0,
) -> None:
    """Bring ``driver``'s pane to an authenticated shell, prompting for the RSA passcode.

    Reuses an existing authenticated pane when ``at_shell_prompt()`` is true. Otherwise
    starts a session when needed, then re-prompts for a fresh single-use code on a
    rejected/stale passcode, up to ``max_attempts``; re-raises :class:`AuthenticationError`
    once attempts are exhausted.
    """
    try:
        if driver.session_exists() and driver.at_shell_prompt():
            return
    except AttributeError:
        pass
    except Exception:
        pass

    if driver.start_session(connect_timeout=connect_timeout):
        return

    for attempt in range(1, max_attempts + 1):
        code = getpass_fn(f"[{label}] Enter RSA PASSCODE: ")  # transient; never stored
        driver.submit_secret(code)
        try:
            driver.await_authenticated(timeout=wait_timeout)
            return
        except AuthenticationError:
            # sshd re-displayed PASSCODE: the code was stale/wrong — loop to re-prompt for a
            # fresh single-use code, unless this was the last attempt.
            if attempt == max_attempts:
                raise


def authenticate_node_via_pane(
    driver: TmuxDriver,
    label: str,
    *,
    notify_fn: Callable[[str], None] | None = None,
    connect_timeout: float | None = None,
    wait_timeout: float = 300.0,
) -> None:
    """Authenticate by letting the Operator type RSA directly into the tmux pane.

    Cursor-owned terminals can be read-only to the user, so a hidden ``getpass`` prompt can
    trap the release. Pane auth makes the interactive boundary explicit: start the SSH pane,
    tell the Operator which tmux session is waiting, then poll until it reaches a shell.
    """
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
            if notify_fn is not None:
                notify_fn(
                    f"waiting for {label} RSA in existing tmux session {driver.session!r}; "
                    f"enter the current PASSCODE in that pane"
                )
            driver.await_authenticated(timeout=wait_timeout)
            return

    if driver.start_session(connect_timeout=connect_timeout):
        return

    if notify_fn is not None:
        notify_fn(
            f"waiting for {label} RSA in tmux session {driver.session!r}; "
            f"attach or open that pane and enter the current PASSCODE"
        )
    driver.await_authenticated(timeout=wait_timeout)


def ensure_kerberos(
    driver: TmuxDriver,
    label: str,
    *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    principal: str | None = None,
    max_attempts: int = 2,
) -> ReportCheck:
    """Ensure a valid Kerberos ticket exists, prompting for ``kinit`` only if needed.

    Returns a ``kerberos`` :class:`ReportCheck` (never raises): a valid existing ticket
    (``klist -s`` exit 0) short-circuits with no prompt; otherwise it runs ``kinit`` and
    forwards the password, re-checking ``klist -s`` up to ``max_attempts`` times.
    """
    _screen, code = driver.run_remote("klist -s")
    if code == 0:
        return ReportCheck("kerberos", True, "Existing Kerberos ticket")

    kinit_command = f"kinit {principal}" if principal else "kinit"
    for _attempt in range(1, max_attempts + 1):
        driver.send_text(kinit_command)
        driver.wait_for(r"[Pp]assword.*:", timeout=15)
        driver.submit_secret(getpass_fn(f"[{label}] Kerberos password: "))  # transient; never stored
        _screen, code = driver.run_remote("klist -s")
        if code == 0:
            return ReportCheck("kerberos", True, "Kerberos ticket acquired")
    return ReportCheck("kerberos", False, "Could not acquire a Kerberos ticket")
