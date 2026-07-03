"""TmuxDriver: profile-driven tui_exit strategy selection and run_remote sentinel parsing.

These never start a real session; pane I/O is patched. The point is to prove the chrome
regex and exit strategy are *injected* per Tool (no hardcoded Dispatch constants) and that
the shared ``__RC_<nonce>_<code>__`` exit-code protocol still parses.
"""

from __future__ import annotations

import hashlib
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
        ["pipe-pane", "-t", "sess", "-o", f"cat >> {log_path}"],
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
        ["pipe-pane", "-t", "sess", "-o", f"cat >> {log_path}"],
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
        patch.object(driver, "send_key"),
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
