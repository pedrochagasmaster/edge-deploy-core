"""Phase scaffold: enter_phase gate and registry-driven CLI hooks."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from edge_deploy.cli import build_parser
from edge_deploy.ledger import LedgerError, RunLedger, RunLockError
from edge_deploy.phases import (
    PHASE_REGISTRY,
    EngineMismatchError,
    PhaseSpec,
    enter_phase,
)
from edge_deploy.posture import PHASE_ENDPOINTS, PostureError


def _create_ledger(tmp_path) -> RunLedger:
    return RunLedger.create(
        tmp_path / "runs",
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="operator@example.com",
    )


def _verify_spec() -> PhaseSpec:
    return PhaseSpec(name="verify", order=10, endpoints=PHASE_ENDPOINTS["verify"])


def _events(ledger: RunLedger) -> list[dict]:
    path = ledger.run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_enter_phase_lock_failure_before_engine_and_posture(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _create_ledger(tmp_path)
    (ledger.run_dir / "run.lock").write_text(
        json.dumps(
            {
                "pid": 4242,
                "hostname": "edge-host",
                "acquired_at": "2026-07-03T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    def fail_engine_check() -> None:
        raise AssertionError("engine check must not run when lock fails")

    def fail_posture(*args, **kwargs) -> None:
        raise AssertionError("posture check must not run when lock fails")

    monkeypatch.setattr("edge_deploy.phases.engine_identity", fail_engine_check)
    monkeypatch.setattr("edge_deploy.phases.require_posture", fail_posture)

    with pytest.raises(RunLockError):
        enter_phase(_verify_spec(), None, ledger, next_command="python -m edge_deploy verify --run x")


def test_enter_phase_engine_mismatch_before_posture(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _create_ledger(tmp_path)
    ledger.state["engine"]["content_sha256"] = "a" * 64
    posture_called: list[bool] = []

    def track_posture(*args, **kwargs) -> None:
        posture_called.append(True)

    monkeypatch.setattr(
        "edge_deploy.phases.engine_identity",
        lambda: {"content_sha256": "b" * 64, "version": "1.0.0", "package_dir": "/pkg"},
    )
    monkeypatch.setattr("edge_deploy.phases.require_posture", track_posture)

    with pytest.raises(EngineMismatchError):
        enter_phase(_verify_spec(), None, ledger, next_command="python -m edge_deploy verify --run x")

    assert not posture_called
    assert not (ledger.run_dir / "run.lock").is_file()
    assert not any(event["event"] == "phase_entered" for event in _events(ledger))


def test_enter_phase_engine_mismatch_exact_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _create_ledger(tmp_path)
    ledger.state["engine"]["content_sha256"] = "0123456789abcdef" + "0" * 48
    monkeypatch.setattr(
        "edge_deploy.phases.engine_identity",
        lambda: {
            "content_sha256": "fedcba9876543210" + "0" * 48,
            "version": "1.0.0",
            "package_dir": "/pkg",
        },
    )

    run_id = ledger.state["run_id"]
    expected = (
        f"engine mismatch: run {run_id} was created by engine 01234567 "
        f"but this process is fedcba98; finish the run with the original engine or abandon it"
    )

    with pytest.raises(EngineMismatchError, match=re.escape(expected)):
        enter_phase(_verify_spec(), None, ledger, next_command="python -m edge_deploy verify --run x")


def test_enter_phase_posture_failure_before_phase_entered_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _create_ledger(tmp_path)
    current = ledger.state["engine"]["content_sha256"]
    monkeypatch.setattr(
        "edge_deploy.phases.engine_identity",
        lambda: {"content_sha256": current, "version": "1.0.0", "package_dir": "/pkg"},
    )

    def unreachable_connect(address: tuple[str, int], timeout: float) -> object:
        raise OSError("connection refused")

    with pytest.raises(PostureError):
        enter_phase(
            _verify_spec(),
            None,
            ledger,
            next_command="python -m edge_deploy verify --run x",
            connect=unreachable_connect,
        )

    assert not any(event["event"] == "phase_entered" for event in _events(ledger))
    assert not (ledger.run_dir / "run.lock").is_file()


def test_enter_phase_success_records_event_and_exit_stack_releases_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _create_ledger(tmp_path)
    current = ledger.state["engine"]["content_sha256"]
    monkeypatch.setattr(
        "edge_deploy.phases.engine_identity",
        lambda: {"content_sha256": current, "version": "1.0.0", "package_dir": "/pkg"},
    )

    def reachable_connect(address: tuple[str, int], timeout: float) -> object:
        return SimpleNamespace(close=lambda: None)

    stack = enter_phase(
        _verify_spec(),
        None,
        ledger,
        next_command="python -m edge_deploy verify --run x",
        connect=reachable_connect,
    )
    assert (ledger.run_dir / "run.lock").is_file()
    entered = [event for event in _events(ledger) if event["event"] == "phase_entered"]
    assert len(entered) == 1
    assert entered[0]["event"] == "phase_entered"
    assert entered[0]["phase"] == "verify"

    stack.close()
    assert not (ledger.run_dir / "run.lock").is_file()


def test_enter_phase_refuses_non_open_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _create_ledger(tmp_path)
    ledger.complete()
    run_id = ledger.state["run_id"]
    current = ledger.state["engine"]["content_sha256"]
    monkeypatch.setattr(
        "edge_deploy.phases.engine_identity",
        lambda: {"content_sha256": current, "version": "1.0.0", "package_dir": "/pkg"},
    )

    with pytest.raises(LedgerError, match=f"phase 'verify' refused: run {run_id} is complete"):
        enter_phase(_verify_spec(), None, ledger, next_command="python -m edge_deploy verify --run x")


def test_registry_driven_subcommand_appears_in_help() -> None:
    def register_dummy(subparsers) -> None:
        parser = subparsers.add_parser("dummy-phase", help="Dummy phase for registry test")
        parser.set_defaults(func=lambda args, operator: 0)

    dummy_spec = PhaseSpec(name="dummy", order=5, endpoints=())
    PHASE_REGISTRY.append((dummy_spec, register_dummy))
    try:
        help_text = build_parser().format_help()
        assert "dummy-phase" in help_text
        assert "Dummy phase for registry test" in help_text
    finally:
        PHASE_REGISTRY.pop()


# ---------------------------------------------------------------------------
# run_repo_root: operator `tools` mapping is optional (docs/DESIGN.md calls it
# backward-compatible only; the real operator config defines none)
# ---------------------------------------------------------------------------


def test_run_repo_root_falls_back_without_tools_mapping(tmp_path) -> None:
    from pathlib import Path

    from edge_deploy.config import OperatorConfig
    from edge_deploy.phases import run_repo_root

    ledger = _create_ledger(tmp_path)
    operator = OperatorConfig(operator_email="op@example.com")  # no tools mapping
    fallback = Path(tmp_path / "checkout")

    assert run_repo_root(ledger, operator, fallback) == fallback


def test_run_repo_root_prefers_tools_mapping(tmp_path) -> None:
    from pathlib import Path

    from edge_deploy.config import OperatorConfig
    from edge_deploy.phases import run_repo_root

    ledger = _create_ledger(tmp_path)
    mapped = tmp_path / "mapped-checkout"
    operator = OperatorConfig(
        operator_email="op@example.com",
        tools={"autobench": str(mapped)},
    )

    assert run_repo_root(ledger, operator, Path(tmp_path / "cwd")) == mapped.resolve()
