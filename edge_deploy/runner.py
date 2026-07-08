"""On-node runner: file-based step results and wrap-immune remote reads (D8)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edge_deploy.tmux_driver import TmuxDriver

RUNNER_VERSION = "2"

RUNNER_SCRIPT = """#!/bin/sh
set -eu

run_id="$1"
step_name="$2"
b64_command="$3"
bundle_dir="${4:--}"

steps_dir="$HOME/.edge-deploy/runs/${run_id}/steps"
mkdir -p "$steps_dir"

out_file="${steps_dir}/${step_name}.out"
json_file="${steps_dir}/${step_name}.json"

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cmd=$(printf '%s' "$b64_command" | base64 -d)
exit_code=0
if [ "$step_name" = "install" ] && [ "$bundle_dir" != "-" ]; then
  export EDGE_DEPLOY_BUNDLE_DIR="$bundle_dir"
  export PIP_NO_INDEX=1
  export PIP_FIND_LINKS="${bundle_dir}/wheels"
fi
sh -c "$cmd" >"$out_file" 2>&1 || exit_code=$?
finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Bare python3 is not on PATH on the Edge Nodes; resolve a concrete interpreter.
edge_python="$(
  command -v python3.11 ||
    command -v python3.10 ||
    command -v python3 ||
    printf %s /sys_apps_01/python/python310/bin/python3.10
)"

"$edge_python" <<PY
import json
from pathlib import Path

out_file = Path("${out_file}")
json_file = Path("${json_file}")
lines = out_file.read_text(errors="replace").splitlines()
stdout_tail = "\\n".join(lines[-40:])
payload = {
    "schema": "edge-deploy/step/1",
    "step": "${step_name}",
    "exit_code": ${exit_code},
    "started_at": "${started_at}",
    "finished_at": "${finished_at}",
    "stdout_tail": stdout_tail,
}
json_file.write_text(json.dumps(payload))
PY

exit 0
"""

_RESULT_START = "__EDGE_RESULT_START__"
_RESULT_SHA_PREFIX = "__EDGE_RESULT_SHA_"
_RESULT_END = "__EDGE_RESULT_END__"
_READ_REMOTE_TIMEOUT = 60.0


class RunnerProtocolError(RuntimeError):
    """Raised when the on-node runner or D8 read protocol is violated."""


def runner_sha256() -> str:
    return hashlib.sha256(RUNNER_SCRIPT.encode()).hexdigest()


def bootstrap_runner(driver: TmuxDriver, run_id: str) -> str:
    digest = runner_sha256()
    remote_path = f"~/.edge-deploy/runner-{RUNNER_VERSION}-{digest[:8]}.sh"
    # newline="\n" is load-bearing: Windows text mode would write CRLF, and a
    # CRLF shell script dies on the node at `set -eu\r` before writing any step
    # results (the upload digest verify cannot catch it — both sides hash the
    # same CRLF bytes).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(RUNNER_SCRIPT)
        tmp_path = handle.name
    try:
        driver.upload_file(tmp_path, remote_path)
    finally:
        os.unlink(tmp_path)
    return remote_path


def _read_remote_bytes(driver: TmuxDriver, remote_path: str) -> bytes:
    command = (
        "printf '\\n__EDGE_RESULT_START__\\n'; "
        f"base64 -w0 {remote_path}; "
        f"printf '\\n__EDGE_RESULT_SHA_%s__\\n' \"$(sha256sum {remote_path} | cut -d' ' -f1)\"; "
        "printf '__EDGE_RESULT_END__\\n'"
    )
    screen, _exit_code = driver.run_remote(command, timeout=_READ_REMOTE_TIMEOUT)

    start_index = screen.rfind(_RESULT_START)
    if start_index == -1:
        raise RunnerProtocolError(f"missing {_RESULT_START!r} in remote read for {remote_path}")

    after_start = screen[start_index + len(_RESULT_START) :]
    end_index = after_start.find(_RESULT_END)
    result_span = after_start[:end_index] if end_index != -1 else after_start

    sha_index = result_span.find(_RESULT_SHA_PREFIX)
    if sha_index == -1:
        raise RunnerProtocolError(f"missing {_RESULT_SHA_PREFIX!r} in remote read for {remote_path}")

    b64 = re.sub(r"\s", "", result_span[:sha_index])
    sha_match = re.search(rf"{re.escape(_RESULT_SHA_PREFIX)}([0-9a-f]{{64}})__", result_span)
    if sha_match is None:
        raise RunnerProtocolError(f"missing digest marker in remote read for {remote_path}")

    try:
        decoded = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise RunnerProtocolError(f"invalid base64 in remote read for {remote_path}") from exc

    expected = sha_match.group(1)
    actual = hashlib.sha256(decoded).hexdigest()
    if actual != expected:
        raise RunnerProtocolError(f"remote result digest mismatch for {remote_path}")
    return decoded


def read_remote_text(driver: TmuxDriver, remote_path: str) -> str:
    try:
        return _read_remote_bytes(driver, remote_path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerProtocolError(f"invalid UTF-8 in remote read for {remote_path}") from exc


def read_remote_json(driver: TmuxDriver, remote_path: str) -> dict:
    decoded = _read_remote_bytes(driver, remote_path)
    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerProtocolError(f"invalid JSON in remote read for {remote_path}") from exc
    if not isinstance(payload, dict):
        raise RunnerProtocolError(f"remote read for {remote_path} did not return a JSON object")
    return payload


def run_step(
    driver: TmuxDriver,
    runner_path: str,
    run_id: str,
    step_name: str,
    command: str,
    *,
    timeout: float,
    bundle_dir: str | None = None,
) -> dict:
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    if step_name == "install":
        bundle_arg = bundle_dir if bundle_dir else "-"
        remote_command = f"sh {runner_path} {run_id} {step_name} {encoded} {bundle_arg}"
    else:
        remote_command = f"sh {runner_path} {run_id} {step_name} {encoded}"
    screen, exit_code = driver.run_remote(remote_command, timeout=timeout)
    if exit_code != 0:
        # The runner traps step-command failures into the JSON result and exits 0,
        # so a nonzero exit means the runner itself broke; fail fast with the
        # screen instead of a confusing protocol error at the JSON read.
        tail = "\n".join(
            line for line in screen.strip().splitlines() if line.strip()
        )[-2000:]
        raise RunnerProtocolError(
            f"runner invocation for step {step_name!r} exited {exit_code}; screen:\n{tail}"
        )

    json_path = f"~/.edge-deploy/runs/{run_id}/steps/{step_name}.json"
    result = read_remote_json(driver, json_path)
    if result.get("schema") != "edge-deploy/step/1":
        raise RunnerProtocolError(
            f"unexpected step schema {result.get('schema')!r} for {step_name}"
        )
    if result.get("step") != step_name:
        raise RunnerProtocolError(
            f"step name mismatch: expected {step_name!r}, got {result.get('step')!r}"
        )
    return result
