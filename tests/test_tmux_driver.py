"""TmuxDriver: profile-driven tui_exit strategy selection and run_remote sentinel parsing.

These never start a real session; pane I/O is patched. The point is to prove the chrome
regex and exit strategy are *injected* per Tool (no hardcoded Dispatch constants) and that
the shared ``__RC_<nonce>_<code>__`` exit-code protocol still parses.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from unittest.mock import call, patch

import pytest

from edge_deploy.config import NodeConfig
from edge_deploy.tmux_driver import TmuxDriver

ROBO_CHROME = "Active Jobs|esc Back|n New Job"
AUTOBENCH_CHROME = "Privacy-Compliant Peer Benchmark|Control 3.2 dimensional analysis"


def _driver(tui_exit: str, chrome: str) -> TmuxDriver:
    return TmuxDriver("user@edge", "sess", "/repo", tui_chrome_regex=chrome, tui_exit=tui_exit)


def test_enable_pane_log_invokes_pipe_pane(tmp_path: Path) -> None:
    log_path = tmp_path / "pane.log"
    driver = TmuxDriver("user@edge", "sess", "/repo")

    with patch.object(driver, "_tmux") as tmux:
        tmux.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        result = driver.enable_pane_log(log_path)

    assert result is True
    assert driver.pane_log_supported is True
    tmux.assert_called_once_with(
        ["pipe-pane", "-t", "sess", "-o", f"cat >> {shlex.quote(log_path.as_posix())}"],
        check=False,
    )


def test_enable_pane_log_unsupported_sets_flag_without_raise(tmp_path: Path) -> None:
    log_path = tmp_path / "pane.log"
    driver = TmuxDriver("user@edge", "sess", "/repo")

    with patch.object(driver, "_tmux") as tmux:
        tmux.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        result = driver.enable_pane_log(log_path)

    assert result is False
    assert driver.pane_log_supported is False


def test_start_session_enables_pane_log_when_configured(tmp_path: Path) -> None:
    log_path = tmp_path / "pane.log"
    driver = TmuxDriver("user@edge", "sess", "/repo", pane_log_path=log_path)

    with (
        patch.object(driver, "_tmux") as tmux,
        patch.object(driver, "send_keys"),
        patch.object(driver, "wait_for", return_value="user@host:/repo$ "),
        patch("edge_deploy.tmux_driver.time.sleep"),
    ):
        tmux.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        driver.start_session()

    pipe_pane_calls = [
        call_args
        for call_args in tmux.call_args_list
        if call_args[0][0] and call_args[0][0][0] == "pipe-pane"
    ]
    assert len(pipe_pane_calls) == 1
    assert pipe_pane_calls[0] == call(
        ["pipe-pane", "-t", "sess", "-o", f"cat >> {shlex.quote(log_path.as_posix())}"],
        check=False,
    )


def test_start_session_unsupported_pane_log_does_not_raise(tmp_path: Path) -> None:
    log_path = tmp_path / "pane.log"
    driver = TmuxDriver("user@edge", "sess", "/repo", pane_log_path=log_path)

    def fake_tmux(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv and argv[0] == "pipe-pane":
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with (
        patch.object(driver, "_tmux", side_effect=fake_tmux),
        patch.object(driver, "send_keys"),
        patch.object(driver, "wait_for", return_value="user@host:/repo$ "),
        patch("edge_deploy.tmux_driver.time.sleep"),
    ):
        driver.start_session()

    assert driver.pane_log_supported is False


def test_from_node_and_profile_injects_profile_strategy(real_profile) -> None:
    node = NodeConfig(host="user@edge", session="prod-sess", ssh_options="-p 2222")

    driver = TmuxDriver.from_node_and_profile(node, real_profile)

    assert driver.host == "user@edge"
    assert driver.session == "prod-sess"
    assert driver.ssh_options == "-p 2222"
    assert driver.repo_path == real_profile.repo_path
    assert driver.tui_exit == real_profile.tui_exit
    assert driver.tui_chrome_regex == real_profile.tui_chrome_regex


def test_pane_command_omits_control_master_and_uploads_via_authenticated_pane(tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"bundle")
    local_digest = hashlib.sha256(b"bundle").hexdigest()
    driver = TmuxDriver("user@edge", "sess", "/repo", ssh_options="-p 2222")

    pane_command = driver._build_pane_command()
    for token in ("Control" + "Master", "Control" + "Path"):
        assert token not in pane_command

    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if command.startswith("sha256sum ") and "cut -d' ' -f1" in command:
            return f"{local_digest}\n", 0
        return "", 0

    with (
        patch.object(driver, "run_remote", side_effect=fake_run_remote),
        patch("edge_deploy.tmux_driver.subprocess.run") as run,
    ):
        digest = driver.upload_file(source, "/remote/bundle.zip")

    assert digest == local_digest
    run.assert_not_called()
    assert commands[0].startswith("test -f /remote/bundle.zip")
    assert commands[1].startswith("mkdir -p /remote")
    assert any("cat >> /remote/bundle.zip.edge-deploy-" in command for command in commands)
    assert any("base64.b64decode" in command for command in commands)
    assert any(
        command.startswith("sha256sum /remote/bundle.zip") and "cut -d' ' -f1" in command
        for command in commands
    )


def test_upload_file_skips_transfer_when_precheck_digest_matches(tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"already-there")
    local_digest = hashlib.sha256(b"already-there").hexdigest()
    driver = TmuxDriver("user@edge", "sess", "/repo")
    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        if "test -f" in command and "sha256sum" in command:
            return f"{local_digest}\n", 0
        raise AssertionError(f"unexpected remote command during reuse: {command!r}")

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        digest = driver.upload_file(source, "/remote/bundle.zip")

    assert digest == local_digest
    assert len(commands) == 1
    assert commands[0].startswith("test -f /remote/bundle.zip")


def test_upload_file_digest_mismatch_deletes_remote_and_raises(tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"fresh-bundle")
    driver = TmuxDriver("user@edge", "sess", "/repo")
    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if command.startswith("mkdir -p"):
            return "", 0
        if "cat >>" in command:
            return "", 0
        if "base64.b64decode" in command:
            return "", 0
        if command.startswith("sha256sum /remote/bundle.zip") and "cut -d' ' -f1" in command:
            return "deadbeef" * 8 + "\n", 0
        if command == "rm -f /remote/bundle.zip":
            return "", 0
        raise AssertionError(f"unexpected remote command: {command!r}")

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        with pytest.raises(RuntimeError, match="digest mismatch for /remote/bundle.zip"):
            driver.upload_file(source, "/remote/bundle.zip")

    assert any(command == "rm -f /remote/bundle.zip" for command in commands)


def test_upload_file_success_returns_local_digest(tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"verified-upload")
    local_digest = hashlib.sha256(b"verified-upload").hexdigest()
    driver = TmuxDriver("user@edge", "sess", "/repo")
    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if command.startswith("mkdir -p"):
            return "", 0
        if "cat >>" in command:
            return "", 0
        if "base64.b64decode" in command:
            return "", 0
        if command.startswith("sha256sum /remote/bundle.zip") and "cut -d' ' -f1" in command:
            return f"{local_digest}\n", 0
        raise AssertionError(f"unexpected remote command: {command!r}")

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        digest = driver.upload_file(source, "/remote/bundle.zip")

    assert digest == local_digest
    assert commands[0].startswith("test -f /remote/bundle.zip")
    assert any("base64.b64decode" in command for command in commands)
    assert any(
        command.startswith("sha256sum /remote/bundle.zip") and "cut -d' ' -f1" in command
        for command in commands
    )


def test_upload_file_tilde_path_expands_home_in_every_remote_command(tmp_path: Path) -> None:
    """Regression: mkdir/append/decode/verify must never quote the tilde.

    ``shlex.quote('~/...')`` yields ``'~/...'`` which the remote shell treats as a
    literal ``./~`` directory, so the payload lands in the wrong place and the
    ``$HOME``-based digest verify can never match.
    """
    source = tmp_path / "runner.sh"
    source.write_bytes(b"#!/bin/sh\n")
    local_digest = hashlib.sha256(b"#!/bin/sh\n").hexdigest()
    driver = TmuxDriver("user@edge", "sess", "/repo")
    remote_path = "~/.edge-deploy/runner-2-deadbeef.sh"
    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if command.startswith("mkdir -p"):
            return "", 0
        if "cat >>" in command:
            return "", 0
        if "base64.b64decode" in command:
            return "", 0
        if command.startswith("sha256sum $HOME/") and "cut -d' ' -f1" in command:
            return f"{local_digest}\n", 0
        raise AssertionError(f"unexpected remote command: {command!r}")

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        digest = driver.upload_file(source, remote_path)

    assert digest == local_digest
    for command in commands:
        if "base64.b64decode" in command:
            continue  # tilde inside the Python script is resolved via .expanduser()
        assert "'~" not in command, f"quoted tilde would not expand: {command!r}"
    mkdir_cmd = next(c for c in commands if c.startswith("mkdir -p"))
    assert mkdir_cmd.startswith("mkdir -p $HOME/")
    append_cmd = next(c for c in commands if "cat >>" in c)
    assert "cat >> $HOME/" in append_cmd
    decode_cmd = next(c for c in commands if "base64.b64decode" in c)
    assert decode_cmd.count(".expanduser()") == 2


def test_upload_file_decode_resolves_concrete_interpreter(tmp_path: Path) -> None:
    """Regression: bare ``python3`` is not on PATH on the Edge Nodes; the decode
    step must resolve an interpreter and stay compatible with old platform
    Pythons (no ``unlink(missing_ok=...)``, which needs 3.8+)."""
    source = tmp_path / "runner.sh"
    source.write_bytes(b"#!/bin/sh\n")
    local_digest = hashlib.sha256(b"#!/bin/sh\n").hexdigest()
    driver = TmuxDriver("user@edge", "sess", "/repo")
    decode_cmds: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if "base64.b64decode" in command:
            decode_cmds.append(command)
            return "", 0
        if command.startswith("sha256sum "):
            return f"{local_digest}\n", 0
        return "", 0

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        driver.upload_file(source, "/remote/runner.sh")

    (decode_cmd,) = decode_cmds
    assert not decode_cmd.startswith("python3")
    assert decode_cmd.startswith('"$(command -v python3.11 || command -v python3.10')
    assert "missing_ok" not in decode_cmd


def test_upload_file_decode_failure_reports_exit_code_and_screen(tmp_path: Path) -> None:
    source = tmp_path / "runner.sh"
    source.write_bytes(b"#!/bin/sh\n")
    driver = TmuxDriver("user@edge", "sess", "/repo")

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if "base64.b64decode" in command:
            return "bash: python3.11: command not found\n", 127
        return "", 0

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        with pytest.raises(RuntimeError, match=r"(?s)exit 127.*command not found") as excinfo:
            driver.upload_file(source, "/remote/runner.sh")

    assert "could not decode /remote/runner.sh" in str(excinfo.value)


def test_upload_file_splits_base64_payload_into_short_heredoc_lines(tmp_path: Path) -> None:
    """Regression: each heredoc line travels as one psmux send-keys argument, so no
    line may approach the Windows 32 KiB process command-line limit."""
    source = tmp_path / "bundle.bin"
    source.write_bytes(b"x" * 10_000)  # base64 length > 13k, forces multiple lines
    driver = TmuxDriver("user@edge", "sess", "/repo")
    append_cmds: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        if "test -f" in command and "sha256sum" in command:
            return "MISSING\n", 0
        if "cat >>" in command:
            append_cmds.append(command)
            return "", 0
        if command.startswith("sha256sum "):
            return f"{hashlib.sha256(b'x' * 10_000).hexdigest()}\n", 0
        return "", 0

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        driver.upload_file(source, "/remote/bundle.bin")

    assert append_cmds
    for command in append_cmds:
        lines = command.split("\n")
        # opener, payload lines, terminator
        assert len(lines) >= 3
        assert all(len(line) <= 2048 for line in lines[1:-1])
        assert lines[-1].startswith("__EDGE_DEPLOY_UPLOAD_")


def test_upload_file_tilde_path_uses_home_expansion_for_precheck(tmp_path: Path) -> None:
    source = tmp_path / "runner.sh"
    source.write_bytes(b"#!/bin/sh\n")
    local_digest = hashlib.sha256(b"#!/bin/sh\n").hexdigest()
    driver = TmuxDriver("user@edge", "sess", "/repo")
    remote_path = "~/.edge-deploy/runner-2-deadbeef.sh"
    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        if "test -f" in command and "sha256sum" in command:
            return f"{local_digest}\n", 0
        raise AssertionError(f"unexpected remote command during reuse: {command!r}")

    with patch.object(driver, "run_remote", side_effect=fake_run_remote):
        digest = driver.upload_file(source, remote_path)

    assert digest == local_digest
    assert len(commands) == 1
    assert commands[0].startswith("test -f $HOME/")
    assert shlex.quote(".edge-deploy/runner-2-deadbeef.sh") in commands[0]
    assert "~" not in commands[0]


def test_dispatch_dynamic_quits_from_dashboard_top() -> None:
    driver = _driver("dispatch_dynamic", ROBO_CHROME)
    with patch.object(driver, "send_key") as send_key:
        driver._send_exit_keys("Dispatch Dashboard\n  n New Job   Active Jobs\n")

    assert send_key.call_args_list == [call("q")]


def test_dispatch_dynamic_escapes_pushed_subscreen() -> None:
    driver = _driver("dispatch_dynamic", ROBO_CHROME)
    with patch.object(driver, "send_key") as send_key:
        driver._send_exit_keys("Active Jobs detail\n esc Back \n")

    assert send_key.call_args_list == [call("Escape")]


def test_dispatch_dynamic_clears_line_when_not_in_tui() -> None:
    driver = _driver("dispatch_dynamic", ROBO_CHROME)
    with patch.object(driver, "send_key") as send_key:
        driver._send_exit_keys("user@host:/repo$ ")

    assert send_key.call_args_list == [call("C-u")]


def test_ctrl_c_strategy_escapes_then_interrupts_in_tui() -> None:
    driver = _driver("ctrl_c", AUTOBENCH_CHROME)
    with patch.object(driver, "send_key") as send_key:
        driver._send_exit_keys("Privacy-Compliant Peer Benchmark\n")

    assert send_key.call_args_list == [call("Escape"), call("C-c")]


def test_ctrl_c_strategy_clears_line_when_not_in_tui() -> None:
    driver = _driver("ctrl_c", AUTOBENCH_CHROME)
    with patch.object(driver, "send_key") as send_key:
        driver._send_exit_keys("user@host:/repo$ ")

    assert send_key.call_args_list == [call("C-u")]


def test_none_strategy_always_clears_line() -> None:
    driver = _driver("none", "Some Standalone TUI")
    with patch.object(driver, "send_key") as send_key:
        driver._send_exit_keys("Some Standalone TUI on screen\n")

    assert send_key.call_args_list == [call("C-u")]


def test_at_shell_prompt_respects_chrome_and_prompt() -> None:
    driver = _driver("ctrl_c", "MYAPP CHROME")

    assert driver.at_shell_prompt("user@host:~$ ") is True
    assert driver.at_shell_prompt("root@host:/srv# ") is True
    assert driver.at_shell_prompt("MYAPP CHROME visible\nuser@host:~$ ") is False
    assert driver.at_shell_prompt("just some output, no prompt") is False
    assert driver.at_shell_prompt("") is False


def test_run_remote_returns_output_and_exit_code() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    captured: dict[str, str] = {}

    def fake_send_keys(keys: str, *, literal: bool = False) -> None:
        captured["cmd"] = keys

    with (
        patch.object(driver, "return_to_shell", return_value=True),
        patch.object(driver, "send_keys", side_effect=fake_send_keys),
        patch.object(driver, "send_key") as send_key,
        patch.object(driver, "wait_for") as wait_for,
        patch.object(driver, "capture_screen") as capture_screen,
    ):

        def fake_wait(pattern: str, **kwargs: object) -> str:
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            captured["nonce"] = nonce
            return f"some output\n__RC_{nonce}_0__\n"

        wait_for.side_effect = fake_wait
        capture_screen.side_effect = lambda history_lines=0: f"some output\n__RC_{captured['nonce']}_0__\n"
        screen, code = driver.run_remote("which dispatch")

    assert code == 0
    assert "some output" in screen
    assert "which dispatch" in captured["cmd"]
    # The sentinel is split so the echoed command line can never match it.
    assert "__RC''_" in captured["cmd"]
    assert [call.args[0] for call in send_key.call_args_list[:2]] == ["C-u", "Enter"]


def test_run_remote_parses_nonzero_exit_code() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    captured: dict[str, str] = {}

    with (
        patch.object(driver, "return_to_shell", return_value=True),
        patch.object(driver, "send_keys"),
        patch.object(driver, "send_key"),
        patch.object(driver, "wait_for") as wait_for,
        patch.object(driver, "capture_screen") as capture_screen,
    ):

        def fake_wait(pattern: str, **kwargs: object) -> str:
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            captured["nonce"] = nonce
            return f"boom\n__RC_{nonce}_7__\n"

        wait_for.side_effect = fake_wait
        capture_screen.side_effect = lambda history_lines=0: f"boom\n__RC_{captured['nonce']}_7__\n"
        _, code = driver.run_remote("false", ensure_shell=False)

    assert code == 7


def test_run_remote_sends_multiline_command_line_by_line() -> None:
    """Regression: psmux ``send-keys`` drops everything after the first embedded
    newline, so heredocs must be sent one line at a time with the RC sentinel on
    its own line (never appended to the heredoc terminator)."""
    driver = TmuxDriver("user@edge", "session", "/repo")
    sent: list[tuple[str, bool]] = []
    captured: dict[str, str] = {}

    def fake_send_keys(keys: str, *, literal: bool = False) -> None:
        sent.append((keys, literal))

    heredoc = "cat >> /tmp/x.b64 <<'__MARKER__'\nAAAA\nBBBB\n__MARKER__"

    with (
        patch.object(driver, "return_to_shell", return_value=True),
        patch.object(driver, "send_keys", side_effect=fake_send_keys),
        patch.object(driver, "send_key"),
        patch.object(driver, "wait_for") as wait_for,
        patch.object(driver, "capture_screen") as capture_screen,
    ):

        def fake_wait(pattern: str, **kwargs: object) -> str:
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            captured["nonce"] = nonce
            return f"done\n__RC_{nonce}_0__\n"

        wait_for.side_effect = fake_wait
        capture_screen.side_effect = lambda history_lines=0: f"done\n__RC_{captured['nonce']}_0__\n"
        _, code = driver.run_remote(heredoc)

    assert code == 0
    # No send may contain an embedded newline.
    assert all("\n" not in keys for keys, _ in sent)
    # All multi-line sends are literal so tmux cannot reinterpret payload bytes.
    assert all(literal for _, literal in sent)
    # The heredoc terminator must be sent alone; the RC sentinel is the next send.
    terminator_index = [keys for keys, _ in sent].index("__MARKER__")
    assert sent[terminator_index + 1][0].startswith("printf '\\n__RC''_")
    # Payload lines arrive in order between opener and terminator.
    keys_only = [keys for keys, _ in sent]
    assert keys_only.index("AAAA") < keys_only.index("BBBB") < terminator_index


def test_run_remote_single_line_command_keeps_inline_sentinel() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    sent: list[tuple[str, bool]] = []
    captured: dict[str, str] = {}

    def fake_send_keys(keys: str, *, literal: bool = False) -> None:
        sent.append((keys, literal))

    with (
        patch.object(driver, "return_to_shell", return_value=True),
        patch.object(driver, "send_keys", side_effect=fake_send_keys),
        patch.object(driver, "send_key"),
        patch.object(driver, "wait_for") as wait_for,
        patch.object(driver, "capture_screen") as capture_screen,
    ):

        def fake_wait(pattern: str, **kwargs: object) -> str:
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            captured["nonce"] = nonce
            return f"ok\n__RC_{nonce}_0__\n"

        wait_for.side_effect = fake_wait
        capture_screen.side_effect = lambda history_lines=0: f"ok\n__RC_{captured['nonce']}_0__\n"
        _, code = driver.run_remote("echo hi")

    assert code == 0
    assert len(sent) == 1
    keys, literal = sent[0]
    assert literal is False
    assert "echo hi" in keys and "__RC''_" in keys


def test_run_remote_returns_only_latest_command_block_from_history() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    captured: dict[str, str] = {}

    def fake_send_keys(keys: str, *, literal: bool = False) -> None:
        captured["cmd"] = keys

    with (
        patch.object(driver, "return_to_shell", return_value=True),
        patch.object(driver, "send_keys", side_effect=fake_send_keys),
        patch.object(driver, "send_key"),
        patch.object(driver, "wait_for") as wait_for,
        patch.object(driver, "capture_screen") as capture_screen,
    ):

        def fake_wait(pattern: str, **kwargs: object) -> str:
            nonce = pattern.split("__RC_")[1].split("_(")[0]
            captured["nonce"] = nonce
            return f"new output\n__RC_{nonce}_0__\n"

        wait_for.side_effect = fake_wait

        def fake_capture(history_lines: int = 0) -> str:
            nonce = captured["nonce"]
            return (
                "old command\n__START_old__\nold output\n__RC_old_0__\n"
                f"echoed command\n__START_{nonce}__\nnew output\n__RC_{nonce}_0__\n"
                "prompt$ "
            )

        capture_screen.side_effect = fake_capture
        screen, code = driver.run_remote("echo new")

    assert code == 0
    assert "new output" in screen
    assert "old output" not in screen
    assert "__START''_" in captured["cmd"]
