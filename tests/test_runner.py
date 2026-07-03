"""Tests for the on-node runner and D8 wrap-immune remote read protocol."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from edge_deploy.runner import (
    RUNNER_VERSION,
    RunnerProtocolError,
    bootstrap_runner,
    read_remote_json,
    run_step,
    runner_sha256,
)


def _wrap_base64(b64: str, *, width: int = 80) -> str:
    return "\n".join(b64[i : i + width] for i in range(0, len(b64), width))


def build_d8_screen(remote_path: str, payload: dict, *, digest: str | None = None) -> str:
    """Build a canned pane screen with realistically wrapped base64 (D8)."""
    content = json.dumps(payload).encode("utf-8")
    expected = digest if digest is not None else hashlib.sha256(content).hexdigest()
    wrapped = _wrap_base64(base64.b64encode(content).decode("ascii"))
    return (
        f"noise before\n"
        f"\n__EDGE_RESULT_START__\n"
        f"{wrapped}\n"
        f"__EDGE_RESULT_SHA_{expected}__\n"
        f"__EDGE_RESULT_END__\n"
        f"noise after {remote_path}\n"
    )


class ScriptedDriver:
    """Minimal driver double for runner unit tests."""

    def __init__(self, *, screens: list[str] | None = None) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []
        self.upload_contents: list[str] = []
        self._screens = list(screens or [])
        self._screen_index = 0

    def upload_file(self, source: str | Path, remote_path: str) -> str:
        path = Path(source)
        content = path.read_bytes()
        self.uploads.append((path, remote_path))
        self.upload_contents.append(content.decode("utf-8"))
        return hashlib.sha256(content).hexdigest()

    def run_remote(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        ensure_shell: bool = True,
    ) -> tuple[str, int]:
        self.commands.append(command)
        if not self._screens:
            return "", 0
        screen = self._screens[min(self._screen_index, len(self._screens) - 1)]
        if self._screen_index < len(self._screens) - 1:
            self._screen_index += 1
        return screen, 0


def test_read_remote_json_survives_wrapped_base64() -> None:
    remote_path = "~/.edge-deploy/runs/run-1/steps/stage.json"
    payload = {
        "schema": "edge-deploy/step/1",
        "step": "stage",
        "exit_code": 0,
        "started_at": "2026-07-03T12:00:00Z",
        "finished_at": "2026-07-03T12:00:01Z",
        "stdout_tail": "ok",
    }
    driver = ScriptedDriver(screens=[build_d8_screen(remote_path, payload)])

    result = read_remote_json(driver, remote_path)

    assert result == payload
    assert "__EDGE_RESULT_START__" in driver.commands[0]
    assert remote_path in driver.commands[0]


def test_read_remote_json_digest_mismatch_raises() -> None:
    remote_path = "~/.edge-deploy/runs/run-1/steps/bad.json"
    payload = {"schema": "edge-deploy/step/1", "step": "bad", "exit_code": 1}
    wrong_digest = "0" * 64
    driver = ScriptedDriver(screens=[build_d8_screen(remote_path, payload, digest=wrong_digest)])

    with pytest.raises(RunnerProtocolError, match=f"remote result digest mismatch for {remote_path}"):
        read_remote_json(driver, remote_path)


def test_run_step_composes_runner_command_line() -> None:
    runner_path = "~/.edge-deploy/runner-1-deadbeef.sh"
    run_id = "run-20260703T120000Z-aa6d9a5"
    step_name = "dependency-stage"
    command = "python3 /tmp/stage.py --verbose"
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    step_payload: dict[str, Any] = {
        "schema": "edge-deploy/step/1",
        "step": step_name,
        "exit_code": 0,
        "started_at": "2026-07-03T12:00:00Z",
        "finished_at": "2026-07-03T12:00:01Z",
        "stdout_tail": "done",
    }
    json_path = f"~/.edge-deploy/runs/{run_id}/steps/{step_name}.json"
    driver = ScriptedDriver(
        screens=[
            "__RC_fake_0__",
            build_d8_screen(json_path, step_payload),
        ]
    )

    result = run_step(
        driver,
        runner_path,
        run_id,
        step_name,
        command,
        timeout=120.0,
    )

    assert result == step_payload
    assert driver.commands[0] == f"sh {runner_path} {run_id} {step_name} {encoded}"
    assert json_path in driver.commands[1]


def test_bootstrap_runner_target_path_embeds_version_and_digest() -> None:
    driver = ScriptedDriver()
    expected_suffix = runner_sha256()[:8]

    remote_path = bootstrap_runner(driver, "run-unused")

    assert remote_path == f"~/.edge-deploy/runner-{RUNNER_VERSION}-{expected_suffix}.sh"
    assert len(driver.uploads) == 1
    _source, uploaded_remote = driver.uploads[0]
    assert uploaded_remote == remote_path
    assert driver.upload_contents[0].startswith("#!/bin/sh")
