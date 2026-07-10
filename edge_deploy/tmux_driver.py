"""Local tmux/psmux driver whose pane holds one authenticated SSH session to an Edge Node.

Session model
-------------
``TmuxDriver`` creates and manages tmux sessions **locally** (using the ``tmux`` binary
available on the local machine — standard tmux on Linux/macOS, or psmux on Windows which
provides a ``tmux.exe`` shim).

The remote shell lives *inside* the tmux pane: ``start_session`` opens a new detached
local tmux session whose initial command is an SSH connection to the Edge Node, landing
in ``repo_path``. All subsequent ``send_keys``, ``capture_screen``, and ``stop_session``
calls operate on that local session without additional SSH round-trips.

One-off remote operations (file writes, inline Python, ``git`` queries) reuse the
already-authenticated tmux pane through ``run_remote()``.

This is the generalized extraction of robocop's ``robocop_tmux.py``: the TUI chrome
regex and the TUI-exit strategy are *injected* per Tool (via the Tool Profile) instead
of being hardcoded Dispatch constants.
"""

from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from edge_deploy.remote_paths import shell_remote_path
from edge_deploy.remote_python import REMOTE_PYTHON_EXPR
from edge_deploy.transport import (
    AuthenticationError,
    ConnectionLostError,
    TransferProgress,
    TransferProgressCallback,
)

__all__ = [
    "AuthenticationError",
    "SessionGoneError",
    "TmuxDriver",
]

# Matches an ANSI escape sequence so screen text can be inspected as plain text.
_ANSI_RE = r"\x1b\[[0-9;?]*[ -/]*[@-~]"


# A bash/sh *primary* prompt at the end of a line (``$`` or ``#``). The ``>`` PS2
# continuation prompt is intentionally excluded: treating it as a real prompt would let
# the harness append commands onto a dangling, unterminated line instead of recognising
# the pane is stuck.
SHELL_PROMPT_RE = r"[\$#]\s*$"
# Dispatch's Overview/dashboard is the app's *top* screen: pressing ``q`` there quits
# cleanly back to the shell. Any other TUI screen is a pushed sub-screen that ``Escape``
# pops (its footer shows ``esc Back``). Used only by the ``dispatch_dynamic`` strategy.
DASHBOARD_TOP_RE = r"running first|n New Job\b"

# Auth/prompt detection shared across strategies.
_AUTH_RE = r"PASSCODE:|[Pp]assword:|PIN:"


_PROMPT_RE = r"[\$#]\s*$"


class SessionGoneError(ConnectionLostError):
    """Raised when the tmux session has disappeared (pane process exited).

    Surfacing this distinctly stops the harness from cascading a dozen opaque
    ``capture-pane returned non-zero`` failures when the SSH pane has died.
    """


class TmuxDriver:
    """Drive a local tmux/psmux session whose pane is an SSH connection to the Edge Node.

    Session lifecycle
    -----------------
    ``start_session()`` creates a **local** detached tmux session. Its initial command is
    ``ssh [options] host "cd repo_path && exec bash -l"``, so the remote shell is already
    at the right working directory.

    All pane control (``send_keys``, ``capture_screen``, ``attach``, ``stop_session``)
    runs tmux commands locally — no extra SSH hops.

    One-off remote operations run through ``run_remote()`` in the *already-authenticated*
    pane. A second ``ssh host cmd`` is intentionally not used: with single-use RSA / 2FA
    it would block forever on the ``Enter PASSCODE:`` prompt.

    Parameters
    ----------
    tui_chrome_regex:
        Regex matching this Tool's on-screen TUI chrome. When matched, the pane is treated
        as *not* at a shell prompt. Empty disables chrome detection (CLI-only tools).
    tui_exit:
        Return-to-shell strategy: ``ctrl_c`` (shared default: Escape then C-c),
        ``dispatch_dynamic`` (robocop's dashboard-aware q/Escape), or ``none``.
    """

    def __init__(
        self,
        host: str,
        session: str,
        repo_path: str,
        width: int = 120,
        height: int = 40,
        ssh_options: str = "",
        *,
        tui_chrome_regex: str = "",
        tui_exit: str = "ctrl_c",
        retries: int = 0,
        retry_backoff: float = 3.0,
        ssh_connect_timeout: int = 15,
        pane_log_path: Path | None = None,
    ) -> None:
        self.host = host
        self.session = session
        self.repo_path = repo_path
        self.width = width
        self.height = height
        self.ssh_options = ssh_options
        self.tui_chrome_regex = tui_chrome_regex
        self.tui_exit = tui_exit
        self.ssh_connect_timeout = ssh_connect_timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.pane_log_path = pane_log_path
        self.pane_log_supported: bool | None = None

    @classmethod
    def from_node_and_profile(
        cls,
        node: "object",
        profile: "object",
        *,
        width: int = 120,
        height: int = 40,
        retries: int = 0,
        pane_log_path: Path | None = None,
    ) -> "TmuxDriver":
        """Build a driver from a :class:`NodeConfig` and a :class:`ToolProfile`.

        Typed loosely to avoid importing the config layer (no runtime coupling); only the
        documented attributes are read.
        """
        return cls(
            host=node.host,  # type: ignore[attr-defined]
            session=node.session,  # type: ignore[attr-defined]
            repo_path=profile.repo_path,  # type: ignore[attr-defined]
            width=width,
            height=height,
            ssh_options=node.ssh_options,  # type: ignore[attr-defined]
            tui_chrome_regex=profile.tui_chrome_regex,  # type: ignore[attr-defined]
            tui_exit=profile.tui_exit,  # type: ignore[attr-defined]
            retries=retries,
            pane_log_path=pane_log_path,
        )

    # ------------------------------------------------------------------
    # Local tmux helpers
    # ------------------------------------------------------------------

    def _tmux(
        self,
        argv: list[str],
        *,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a tmux command locally (works with psmux on Windows)."""
        return subprocess.run(
            ["tmux"] + argv,
            check=check,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Session management (local tmux)
    # ------------------------------------------------------------------

    def _build_pane_command(self) -> str:
        """Build the shell command that the tmux pane will run on startup.

        This command SSHes into the remote host and lands at ``repo_path``. ``-t`` forces
        PTY allocation so the remote login shell is fully interactive (correct prompt, job
        control, readline, etc.).
        """
        ssh_parts = ["ssh", "-t"]
        if self.ssh_options:
            ssh_parts.extend(shlex.split(self.ssh_options))
        ssh_parts.append(self.host)
        remote_cmd = f"cd {shlex.quote(self.repo_path)} && exec bash -l"
        ssh_parts.append(remote_cmd)
        if os.name == "nt":
            return subprocess.list2cmdline(ssh_parts)
        return " ".join(shlex.quote(p) for p in ssh_parts)

    def session_exists(self) -> bool:
        """Return True if the local tmux session is currently alive."""
        result = self._tmux(["has-session", "-t", self.session], check=False)
        return result.returncode == 0

    def enable_pane_log(self, log_path: Path) -> bool:
        """Mirror pane output to a local log file via ``tmux pipe-pane``."""
        quoted_path = shlex.quote(log_path.as_posix())
        result = self._tmux(
            ["pipe-pane", "-t", self.session, "-o", f"cat >> {quoted_path}"],
            check=False,
        )
        self.pane_log_supported = result.returncode == 0
        return self.pane_log_supported

    def start_session(self, *, connect_timeout: float | None = None, passcode: str | None = None) -> bool:
        """Create a local tmux session whose pane SSHes into the Edge Node.

        Blocks until a shell prompt or an authentication prompt appears.

        Returns
        -------
        True
            Shell prompt seen — session is fully ready.
        False
            Authentication prompt (PASSCODE / password) seen. If ``passcode`` is supplied
            it is sent automatically and the method waits for the shell. Otherwise the
            pane is left at the prompt for the caller to handle via ``send_keys``.

        Raises
        ------
        TimeoutError
            Neither a shell nor an auth prompt appeared within the timeout.
        """
        timeout = connect_timeout if connect_timeout is not None else getattr(self, "ssh_connect_timeout", 20)

        # Kill any stale local session with the same name.
        self._tmux(["kill-session", "-t", self.session], check=False)

        self._tmux(
            [
                "new-session", "-d",
                "-s", self.session,
                "-x", str(int(self.width)),
                "-y", str(int(self.height)),
            ]
        )
        time.sleep(0.2)
        self.send_keys(self._build_pane_command())

        if self.pane_log_path is not None:
            self.enable_pane_log(self.pane_log_path)

        combined = rf"(?:{_PROMPT_RE})|(?:{_AUTH_RE})"

        try:
            screen = self.wait_for(combined, timeout=float(timeout), poll_interval=1.0)
        except TimeoutError as exc:
            try:
                last = self.capture_screen()
            except Exception:
                last = "(pane capture failed)"
            raise TimeoutError(
                f"SSH did not produce a shell or auth prompt within {timeout}s.\n"
                f"Pane contents:\n{last}"
            ) from exc

        if re.search(_AUTH_RE, screen):
            if passcode:
                self.send_keys(passcode, literal=True)
                self.send_key("Enter")
                self._await_auth_result(timeout=float(timeout))
                return True
            return False

        return True

    def _await_auth_result(self, *, timeout: float, poll_interval: float = 1.0) -> None:
        """Block until the pane reaches a shell prompt after a passcode is sent.

        Fails fast (rather than waiting out the full timeout) the moment the Edge Node
        signals rejection: ``sshd`` re-displays the auth prompt — so a *second* ``Enter
        PASSCODE:`` on screen, or an explicit ``Permission denied`` / ``Authentication
        failed`` line, means the credential was stale or wrong.

        Raises
        ------
        AuthenticationError
            The credential was rejected, or no shell appeared in ``timeout``.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            screen = self.capture_screen()
            if self.at_shell_prompt(screen):
                return
            plain = re.sub(_ANSI_RE, "", screen)
            if re.search(r"Permission denied|Authentication failed|Too many authentication", plain, re.IGNORECASE):
                raise AuthenticationError(
                    "Edge Node rejected the credential (authentication failed). "
                    "RSA passcodes are single-use and rotate ~every 60s — request a fresh one."
                )
            # A re-displayed auth prompt (>= 2 on screen) means the first code was refused.
            if len(re.findall(r"PASSCODE:|[Pp]assword:|PIN:", plain)) >= 2:
                raise AuthenticationError(
                    "Edge Node re-prompted for a PASSCODE — the code was stale or wrong. "
                    "RSA passcodes are single-use and rotate ~every 60s; send a fresh one."
                )
            time.sleep(poll_interval)

        raise AuthenticationError(
            f"Sent a passcode but no shell prompt appeared within {timeout:.0f}s. "
            "The credential was likely wrong or expired; request a fresh RSA passcode."
        )

    def await_authenticated(self, *, timeout: float | None = None, poll_interval: float = 1.0) -> None:
        """Public alias of :meth:`_await_auth_result`: block until the pane reaches a shell.

        Used by the auth seam (:func:`edge_deploy.auth.authenticate_node`) after a secret is
        submitted. Defaults ``timeout`` to ``ssh_connect_timeout`` and raises
        :class:`AuthenticationError` if the credential was rejected.
        """
        effective = timeout if timeout is not None else float(self.ssh_connect_timeout)
        self._await_auth_result(timeout=effective, poll_interval=poll_interval)

    def stop_session(self) -> None:
        """Kill the local tmux session."""
        self._tmux(
            ["kill-session", "-t", self.session],
            check=False,
        )

    def upload_file(
        self,
        source: str | Path,
        remote_path: str,
        *,
        progress: TransferProgressCallback | None = None,
    ) -> str:
        """Copy a file through the authenticated tmux pane."""
        source_path = Path(source)
        source_bytes = source_path.read_bytes()
        total_bytes = len(source_bytes)
        local_digest = hashlib.sha256(source_bytes).hexdigest()
        start_time = time.monotonic()
        if progress is not None:
            progress(TransferProgress(bytes_sent=0, total_bytes=total_bytes, elapsed_s=0.0))
        shell_path = shell_remote_path(remote_path)
        precheck_cmd = (
            f"test -f {shell_path} && "
            f"sha256sum {shell_path} | cut -d' ' -f1 || echo MISSING"
        )
        screen, _rc = self.run_remote(precheck_cmd, timeout=60.0)
        if self._extract_hex_digest(screen) == local_digest:
            if progress is not None:
                progress(
                    TransferProgress(
                        bytes_sent=total_bytes,
                        total_bytes=total_bytes,
                        elapsed_s=time.monotonic() - start_time,
                    )
                )
            return local_digest

        encoded = base64.b64encode(source_bytes).decode("ascii")
        remote_dir = posixpath.dirname(remote_path) or "."
        upload_id = uuid.uuid4().hex[:12]
        remote_b64 = f"{remote_path}.edge-deploy-{upload_id}.b64"
        shell_b64 = shell_remote_path(remote_b64)
        mkdir_cmd = f"mkdir -p {shell_remote_path(remote_dir)} && rm -f {shell_b64}"
        _screen, rc = self.run_remote(mkdir_cmd, timeout=60.0)
        if rc:
            raise RuntimeError(f"authenticated bundle transfer failed: could not prepare {remote_dir}")

        marker = f"__EDGE_DEPLOY_UPLOAD_{upload_id}__"
        # Each heredoc line travels as one psmux ``send-keys`` argument; keep lines well
        # under the Windows 32 KiB process command-line limit. ``base64.b64decode``
        # discards the extra newlines when the remote side reassembles the payload.
        line_size = 2048
        lines_per_heredoc = 16
        chunk_size = line_size * lines_per_heredoc
        for offset in range(0, len(encoded), chunk_size):
            chunk = encoded[offset : offset + chunk_size]
            chunk_lines = [
                chunk[i : i + line_size] for i in range(0, len(chunk), line_size)
            ]
            body = "\n".join(chunk_lines)
            append_cmd = f"cat >> {shell_b64} <<'{marker}'\n{body}\n{marker}"
            screen, rc = self.run_remote(append_cmd, timeout=120.0)
            if rc:
                raise RuntimeError(
                    f"authenticated bundle transfer failed: could not write {remote_b64} "
                    f"(exit {rc}); last screen:\n{self._screen_tail(screen)}"
                )
            if progress is not None:
                # Base64 expands source bytes by 4/3; report progress in source bytes so
                # callers see a monotonic count against the file's real size.
                source_bytes_sent = min(total_bytes, ((offset + len(chunk)) * 3) // 4)
                progress(
                    TransferProgress(
                        bytes_sent=source_bytes_sent,
                        total_bytes=total_bytes,
                        elapsed_s=time.monotonic() - start_time,
                    )
                )

        # The decode script travels base64-encoded on a single line: psmux's
        # tokenizer strips quote characters from whitespace-free arguments (the
        # Windows side only double-quotes args containing spaces), so raw Python
        # source must never be sent as pane lines. Bare ``python3`` is not on
        # PATH on the Edge Nodes; resolve a concrete interpreter. ``unlink()``
        # without ``missing_ok`` keeps the script compatible with older
        # platform interpreters (< 3.8).
        decode_source = (
            "import base64, pathlib\n"
            f"source = pathlib.Path({remote_b64!r}).expanduser()\n"
            f"target = pathlib.Path({remote_path!r}).expanduser()\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "target.write_bytes(base64.b64decode(source.read_text(encoding='ascii')))\n"
            "source.unlink()\n"
        )
        encoded_decode = base64.b64encode(decode_source.encode("ascii")).decode("ascii")
        decode_script = f"printf %s {encoded_decode} | base64 -d | {REMOTE_PYTHON_EXPR} -"
        screen, rc = self.run_remote(decode_script, timeout=300.0)
        if rc:
            cleanup_cmd = f"rm -f {shell_b64}"
            self.run_remote(cleanup_cmd, timeout=30.0, ensure_shell=False)
            raise RuntimeError(
                f"authenticated bundle transfer failed: could not decode {remote_path} "
                f"(exit {rc}); last screen:\n{self._screen_tail(screen)}"
            )

        verify_cmd = f"sha256sum {shell_path} | cut -d' ' -f1"
        screen, _rc = self.run_remote(verify_cmd, timeout=60.0)
        remote_digest = self._extract_hex_digest(screen)
        if remote_digest is None:
            screen = self.capture_screen(history_lines=4000)
            remote_digest = self._extract_hex_digest(screen)
        if remote_digest != local_digest:
            cleanup_cmd = f"rm -f {shell_path}"
            self.run_remote(cleanup_cmd, timeout=30.0, ensure_shell=False)
            raise RuntimeError(
                f"authenticated bundle transfer failed: digest mismatch for {remote_path}"
            )
        if progress is not None:
            progress(
                TransferProgress(
                    bytes_sent=total_bytes,
                    total_bytes=total_bytes,
                    elapsed_s=time.monotonic() - start_time,
                )
            )
        return local_digest

    # ------------------------------------------------------------------
    # Pane control (local tmux)
    # ------------------------------------------------------------------

    def send_keys(self, keys: str, *, literal: bool = False) -> None:
        argv = ["send-keys"]
        if literal:
            argv.append("-l")
        argv += ["-t", self.session, keys]
        if not literal:
            argv.append("Enter")
        self._tmux(argv)

    def send_key(self, key: str) -> None:
        """Send a single tmux key name without appending Enter."""
        self._tmux(["send-keys", "-t", self.session, key])

    def send_text(self, text: str) -> None:
        self.send_keys(text, literal=True)
        self.send_key("Enter")

    def submit_secret(self, secret: str) -> None:
        """Type a secret (RSA passcode / Kerberos password) into the pane, then Enter.

        The single secret-bearing seam (ADR-0002): secrets are sent **literally** through
        ``send_keys``/``send_key`` and never travel through :meth:`run_remote` (which echoes
        and is captured into reports). Held only transiently by the caller; redaction masks
        any accidental leak for defence in depth.
        """
        self.send_keys(secret, literal=True)
        self.send_key("Enter")

    def capture_screen(self, history_lines: int = 0) -> str:
        argv = ["capture-pane", "-t", self.session, "-p"]
        if history_lines > 0:
            argv.extend(["-S", f"-{int(history_lines)}"])
        result = self._tmux(argv, check=False)
        if result.returncode != 0:
            if not self.session_exists():
                raise SessionGoneError(
                    f"tmux session {self.session!r} no longer exists "
                    "(the SSH pane process has exited)."
                )
            raise subprocess.CalledProcessError(
                result.returncode, ["tmux"] + argv, result.stdout, result.stderr
            )
        return result.stdout.rstrip()

    def resize_window(self, width: int, height: int) -> None:
        """Resize the (possibly detached) tmux window for taller TUI screens."""
        self._tmux(
            ["resize-window", "-t", self.session, "-x", str(int(width)), "-y", str(int(height))],
            check=False,
        )

    def attach(self) -> None:
        """Attach the current terminal to the local tmux session."""
        subprocess.run(["tmux", "attach", "-t", self.session], check=False)

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> str:
        deadline = time.monotonic() + timeout
        last_screen = ""
        while time.monotonic() < deadline:
            last_screen = self.capture_screen()
            if re.search(pattern, last_screen, flags=re.MULTILINE):
                return last_screen
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Timed out after {timeout:.1f}s waiting for {pattern!r}.\n"
            f"Last screen:\n{last_screen}"
        )

    # ------------------------------------------------------------------
    # Shell-prompt detection and recovery
    # ------------------------------------------------------------------

    def at_shell_prompt(self, screen: str | None = None) -> bool:
        """Return True when the pane is sitting at a bash/sh prompt.

        Robust against a Tool's TUI: if any injected ``tui_chrome_regex`` is visible the
        pane is considered *not* at a shell prompt, even when a prompt-like character
        happens to appear inside a widget (e.g. a focused search ``Input``).
        """
        text = screen if screen is not None else self.capture_screen()
        text = re.sub(_ANSI_RE, "", text)
        if self.tui_chrome_regex and re.search(self.tui_chrome_regex, text):
            return False
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return False
        return re.search(SHELL_PROMPT_RE, lines[-1]) is not None

    def _send_exit_keys(self, screen: str) -> None:
        """Send the correct return-to-shell key(s) for the current screen and strategy.

        Re-evaluated on every poll so dropped keys (common on high-latency SSH) are
        retried with the *right* key rather than blindly escalating.
        """
        plain = re.sub(_ANSI_RE, "", screen)
        in_tui = bool(self.tui_chrome_regex) and re.search(self.tui_chrome_regex, plain) is not None

        if self.tui_exit == "dispatch_dynamic":
            # robocop's dashboard-aware strategy: q quits from the top screen, Escape pops
            # a pushed sub-screen, and C-u clears leftover input when not in the app.
            if in_tui:
                if re.search(DASHBOARD_TOP_RE, plain) and not re.search(r"esc Back", plain):
                    self.send_key("q")
                else:
                    self.send_key("Escape")
            else:
                self.send_key("C-u")
        elif self.tui_exit == "none":
            # No TUI to exit; just clear any partial/leftover input on the line.
            self.send_key("C-u")
        else:
            # Shared default ("ctrl_c"): Escape to drop any modal, then C-c to interrupt.
            if in_tui:
                self.send_key("Escape")
                self.send_key("C-c")
            else:
                self.send_key("C-u")

    def return_to_shell(self, timeout: float = 25.0, poll_interval: float = 0.6) -> bool:
        """Deterministically return the pane to a bash prompt.

        Designed for high-latency SSH where individual key presses are occasionally
        dropped. Rather than escalating through a fixed sequence, this re-evaluates the
        screen on every poll and re-sends the *correct* key for the current screen (per
        the injected ``tui_exit`` strategy) until a shell prompt appears.

        Raises
        ------
        SessionGoneError
            If the tmux pane has exited (propagated from ``capture_screen``).
        RuntimeError
            If no shell prompt appears within ``timeout``.
        """
        deadline = time.monotonic() + timeout
        last_key_at = 0.0
        while time.monotonic() < deadline:
            screen = self.capture_screen()
            if self.at_shell_prompt(screen):
                return True
            # Re-send at most ~every 1.2s so a transition has time to render but a dropped
            # key is retried promptly.
            if time.monotonic() - last_key_at >= 1.2:
                self._send_exit_keys(screen)
                last_key_at = time.monotonic()
            time.sleep(poll_interval)

        raise RuntimeError(
            "Could not return to a shell prompt; the Tool's TUI may be stuck.\n"
            f"Last screen:\n{self.capture_screen()}"
        )

    def run_remote(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        ensure_shell: bool = True,
    ) -> tuple[str, int]:
        """Run a one-off command in the authenticated pane; return ``(screen, exit_code)``.

        This replaces direct ``ssh host cmd`` calls for one-off remote work. Because it
        reuses the already-authenticated tmux pane, it works with single-use RSA / 2FA
        logins, where a second non-interactive ``ssh`` would block forever on the ``Enter
        PASSCODE:`` prompt.

        A unique, split exit-code sentinel is appended so the echoed command line can never
        be mistaken for command output, and the real exit status is recovered.
        """
        if ensure_shell:
            self.return_to_shell()
        # Discard any stray/partial input left on the line so the command we are about to
        # send cannot be concatenated onto leftover characters.
        self.send_key("C-u")
        self.send_key("Enter")

        nonce = uuid.uuid4().hex[:12]
        # The ``'`` split keeps literal markers out of the echoed command line: bash
        # concatenates them so only printed output contains the start/end sentinels.
        start_marker = f"__START_{nonce}__"
        start_cmd = f"printf '\\n__START''_{nonce}__\\n'; "
        rc_cmd = f"printf '\\n__RC''_{nonce}_%s__\\n' \"$?\""
        if "\n" in command:
            # psmux ``send-keys`` drops everything after the first embedded newline, so
            # multi-line commands (heredocs) must go over line by line. The RC sentinel is
            # sent as its own line: appending ``; printf`` to the last command line would
            # corrupt a heredoc terminator, which must appear alone on its line.
            for line in (start_cmd + command).split("\n"):
                self.send_keys(line, literal=True)
                self.send_key("Enter")
            self.send_keys(rc_cmd, literal=True)
            self.send_key("Enter")
        else:
            self.send_keys(f"{start_cmd}{command}; {rc_cmd}")

        pattern = rf"__RC_{nonce}_(\d+)__"
        self.wait_for(pattern, timeout=timeout, poll_interval=0.5)
        screen = self.capture_screen(history_lines=2000)
        start_index = screen.rfind(start_marker)
        if start_index != -1:
            match_after_start = re.search(pattern, screen[start_index:])
            if match_after_start:
                screen = screen[start_index:start_index + match_after_start.end()]
        match = re.search(pattern, screen)
        exit_code = int(match.group(1)) if match else -1
        return screen, exit_code

    def _screen_tail(self, screen: str, lines: int = 15) -> str:
        """Last non-blank screen lines, for embedding in transfer error messages."""
        plain = re.sub(_ANSI_RE, "", screen)
        kept = [line for line in plain.splitlines() if line.strip()]
        return "\n".join(kept[-lines:])

    def _last_nonempty_line(self, screen: str | None = None) -> str:
        text = screen if screen is not None else self.capture_screen()
        text = re.sub(_ANSI_RE, "", text)
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _extract_hex_digest(self, screen: str) -> str | None:
        """Find the most recent sha256 on the screen, scanning lines bottom-up.

        ``run_remote`` screens end with the ``__RC_<nonce>_<code>__`` sentinel
        line, so the digest printed by ``sha256sum | cut`` sits *above* the last
        line. Only whole-line matches count: echoed command lines can embed
        64-hex strings inside file names (e.g. bundle digests) and must never be
        mistaken for command output.
        """
        text = re.sub(_ANSI_RE, "", screen)
        for line in reversed(text.splitlines()):
            match = re.fullmatch(r"[0-9a-f]{64}", line.strip())
            if match:
                return match.group(0)
        return None

    def type_command_confirmed(
        self,
        command: str,
        *,
        confirm_timeout: float = 6.0,
        retries: int = 3,
        poll_interval: float = 0.3,
    ) -> bool:
        """Type a shell command, confirm it echoed intact, then press Enter.

        On a high-latency SSH PTY the first character of a freshly typed command is
        occasionally dropped, especially right after a full-screen TUI restores the
        terminal. This types the command *without* Enter, waits until the prompt line
        actually contains the full command text, and only then sends Enter — clearing the
        line (``C-u``) and retyping if the echo came back corrupted.

        Returns
        -------
        bool
            True if the echo was confirmed before Enter, False if it pressed Enter as a
            last resort after exhausting ``retries``.
        """
        for _ in range(max(1, retries)):
            self.send_key("C-u")  # discard any partial/dropped input on the line
            time.sleep(0.15)
            self.send_keys(command, literal=True)
            deadline = time.monotonic() + confirm_timeout
            while time.monotonic() < deadline:
                if command in self._last_nonempty_line():
                    self.send_key("Enter")
                    return True
                time.sleep(poll_interval)
        # Last resort: submit whatever is on the line so the caller's wait_for can still
        # fail loudly rather than hang on a half-typed command.
        self.send_key("Enter")
        return False
