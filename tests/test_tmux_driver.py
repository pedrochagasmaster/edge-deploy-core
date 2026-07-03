"""TmuxDriver: profile-driven tui_exit strategy selection and run_remote sentinel parsing.

These never start a real session; pane I/O is patched. The point is to prove the chrome
regex and exit strategy are *injected* per Tool (no hardcoded Dispatch constants) and that
the shared ``__RC_<nonce>_<code>__`` exit-code protocol still parses.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import call, patch

import pytest

from edge_deploy.config import NodeConfig
from edge_deploy.tmux_driver import AuthenticationError, TmuxDriver

ROBO_CHROME = "Active Jobs|esc Back|n New Job"
AUTOBENCH_CHROME = "Privacy-Compliant Peer Benchmark|Control 3.2 dimensional analysis"


def _driver(tui_exit: str, chrome: str) -> TmuxDriver:
    return TmuxDriver("user@edge", "sess", "/repo", tui_chrome_regex=chrome, tui_exit=tui_exit)


def test_from_node_and_profile_injects_profile_strategy(real_profile) -> None:
    node = NodeConfig(host="user@edge", session="prod-sess", ssh_options="-p 2222")

    driver = TmuxDriver.from_node_and_profile(node, real_profile)

    assert driver.host == "user@edge"
    assert driver.session == "prod-sess"
    assert driver.ssh_options == "-p 2222"
    assert driver.repo_path == real_profile.repo_path
    assert driver.tui_exit == real_profile.tui_exit
    assert driver.tui_chrome_regex == real_profile.tui_chrome_regex


def test_authenticated_session_exposes_control_socket_for_scp(tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"bundle")
    with patch.dict("edge_deploy.tmux_driver.os.environ", {"EDGE_DEPLOY_SSH_MULTIPLEX": "1"}):
        driver = TmuxDriver("user@edge", "sess", "/repo", ssh_options="-p 2222")

    pane_command = driver._build_pane_command()
    assert "ControlMaster=yes" in pane_command
    assert "ControlPath=" in pane_command

    with patch("edge_deploy.tmux_driver.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        driver.upload_file(source, "/remote/bundle.zip")

    argv = run.call_args.args[0]
    assert argv[0] == "scp"
    assert any(str(item).startswith("ControlPath=") for item in argv)
    assert str(source) in argv
    assert "user@edge:/remote/bundle.zip" in argv


def test_disabled_multiplex_session_uploads_via_authenticated_pane(tmp_path: Path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"bundle" * 4096)
    with patch.dict("edge_deploy.tmux_driver.os.environ", {"EDGE_DEPLOY_SSH_MULTIPLEX": "0"}):
        driver = TmuxDriver("user@edge", "sess", "/repo", ssh_options="-p 2222")

    pane_command = driver._build_pane_command()
    assert "ControlMaster=yes" not in pane_command
    assert "ControlPath=" not in pane_command

    commands: list[str] = []

    def fake_run_remote(command: str, **kwargs: object) -> tuple[str, int]:
        commands.append(command)
        return "", 0

    with (
        patch.object(driver, "run_remote", side_effect=fake_run_remote),
        patch("edge_deploy.tmux_driver.subprocess.run") as run,
    ):
        driver.upload_file(source, "/ads_storage/$USER/.edge-deploy/bundle.zip")

    run.assert_not_called()
    assert commands[0].startswith("mkdir -p ")
    assert "/ads_storage/$USER/.edge-deploy" in commands[0]
    assert any(
        command.startswith("cat >> ")
        and "/ads_storage/$USER/.edge-deploy/bundle.zip.edge-deploy-" in command
        for command in commands
    )
    assert any("base64.b64decode" in command for command in commands)


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


def test_await_authenticated_fails_when_ssh_closes_after_passcode() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    screen = """\
(user@edge) Enter PASSCODE:
Connection closed by 10.0.0.5 port 2222
PS D:\\Projects\\autobench>
"""

    with (
        patch.object(driver, "capture_screen", return_value=screen),
        patch("edge_deploy.tmux_driver.time.sleep"),
    ):
        with pytest.raises(AuthenticationError, match="closed the SSH connection"):
            driver.await_authenticated(timeout=1, poll_interval=0.01)


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


def test_run_remote_pastes_large_commands_through_tmux_buffer() -> None:
    driver = TmuxDriver("user@edge", "session", "/repo")
    captured: dict[str, str] = {}

    def fake_wait(pattern: str, **kwargs: object) -> str:
        nonce = pattern.split("__RC_")[1].split("_(")[0]
        captured["nonce"] = nonce
        return f"__START_{nonce}__\n__RC_{nonce}_0__\n"

    with (
        patch.object(driver, "return_to_shell", return_value=True),
        patch.object(driver, "send_key") as send_key,
        patch.object(driver, "send_keys") as send_keys,
        patch.object(driver, "paste_text", side_effect=lambda text: captured.setdefault("pasted", text)),
        patch.object(driver, "wait_for", side_effect=fake_wait),
        patch.object(driver, "capture_screen") as capture_screen,
    ):
        capture_screen.side_effect = lambda history_lines=0: (
            f"__START_{captured['nonce']}__\n__RC_{captured['nonce']}_0__\n"
        )
        _screen, code = driver.run_remote("printf " + ("x" * 5000))

    assert code == 0
    send_keys.assert_not_called()
    assert "printf " + ("x" * 5000) in captured["pasted"]
    assert not captured["pasted"].endswith("\n")
    assert call("Enter") in send_key.call_args_list


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
