"""Shared committed local-check runner tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from edge_deploy import local_check


def _script(repo: Path) -> Path:
    script = repo / local_check.LOCAL_CHECK_RELATIVE
    script.parent.mkdir(parents=True)
    script.write_text("py -m pytest\n", encoding="utf-8")
    return script


def test_missing_script_fails_closed(tmp_path) -> None:
    with pytest.raises(local_check.LocalCheckUnavailableError, match="committed local-check"):
        local_check.run_local_check(tmp_path)


def test_missing_powershell_fails_closed(tmp_path, monkeypatch) -> None:
    _script(tmp_path)
    monkeypatch.setattr(local_check, "_resolve_powershell", lambda: None)

    with pytest.raises(local_check.LocalCheckUnavailableError, match="neither 'pwsh'"):
        local_check.run_local_check(tmp_path)


def test_success_uses_repo_venv_shim_and_cleans_it(tmp_path, monkeypatch) -> None:
    script = _script(tmp_path)
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(local_check, "_resolve_powershell", lambda: "pwsh")
    seen: dict[str, object] = {}

    def fake_run(argv, *, cwd, capture_output, text, env):
        shim_dir = Path(str(env["PATH"]).split(os.pathsep)[0])
        seen.update(
            argv=argv,
            cwd=cwd,
            shim_dir=shim_dir,
            shim_text=(shim_dir / "py.cmd").read_text(encoding="utf-8"),
        )
        return subprocess.CompletedProcess(argv, 0, stdout="Local check passed.\n", stderr="")

    monkeypatch.setattr(local_check.subprocess, "run", fake_run)

    result = local_check.run_local_check(tmp_path)

    assert result.exit_code == 0
    assert result.output_tail == "Local check passed."
    assert seen["argv"] == [
        "pwsh",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    assert seen["cwd"] == str(tmp_path)
    assert str(venv_python) in str(seen["shim_text"])
    assert not Path(seen["shim_dir"]).exists()


def test_nonzero_result_preserves_raw_tail_without_putting_it_in_exception(
    tmp_path, monkeypatch
) -> None:
    _script(tmp_path)
    monkeypatch.setattr(local_check, "_resolve_powershell", lambda: "powershell")
    monkeypatch.setattr(
        local_check.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 17, stdout="detail\n", stderr="final failure\n"
        ),
    )

    result = local_check.run_local_check(tmp_path)

    assert result.exit_code == 17
    assert result.output_tail == "detail\nfinal failure"


def test_process_start_failure_is_reported_as_unavailable(tmp_path, monkeypatch) -> None:
    _script(tmp_path)
    monkeypatch.setattr(local_check, "_resolve_powershell", lambda: "powershell")
    monkeypatch.setattr(
        local_check.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("operator path detail")),
    )

    with pytest.raises(local_check.LocalCheckUnavailableError) as raised:
        local_check.run_local_check(tmp_path)

    assert "operator path detail" not in str(raised.value)


def test_output_tail_is_bounded(tmp_path, monkeypatch) -> None:
    _script(tmp_path)
    monkeypatch.setattr(local_check, "_resolve_powershell", lambda: "pwsh")
    output = "\n".join(f"line-{index}" for index in range(30))
    monkeypatch.setattr(
        local_check.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=output, stderr=""),
    )

    result = local_check.run_local_check(tmp_path)

    assert result.output_tail.splitlines() == [f"line-{index}" for index in range(10, 30)]
