"""The Operator auth seam (ADR-0002): prompt forward, re-prompt on stale code,
exhaustion, Kerberos-only-when-needed, and the hard rule that a secret never travels
through ``run_remote`` (only through ``submit_secret``).

All secrets here are fakes typed by a monkeypatched ``_prompt_for_secret``; no real tmux/SSH.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from edge_deploy import auth, release
from edge_deploy.auth import AuthBroker, ensure_kerberos
from edge_deploy.progress import ReleaseProgressTracker
from edge_deploy.tmux_driver import AuthenticationError


def _codes(*values: str):
    iterator: Iterator[str] = iter(values)

    def prompt_fn(prompt: str) -> str:
        return next(iterator)

    return prompt_fn


def _broker(
    fake_tmux,
    *,
    auth_mode: str = "prompt",
    wait_seconds: float = 300.0,
    max_attempts: int = 3,
    tmp_path=None,
) -> tuple[AuthBroker, ReleaseProgressTracker]:
    report_dir = tmp_path or Path(".")
    tracker = ReleaseProgressTracker(report_dir, heartbeat_interval_s=3600.0, stall_threshold_s=7200.0)
    return AuthBroker(tracker, auth_mode, wait_seconds, max_attempts), tracker


# ---------------------------------------------------------------------------
# No hidden getpass paths
# ---------------------------------------------------------------------------


def test_auth_and_release_modules_contain_no_getpass() -> None:
    assert "getpass" not in Path(auth.__file__).read_text(encoding="utf-8")
    assert "getpass" not in Path(release.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AuthBroker prompt mode
# ---------------------------------------------------------------------------


def test_broker_prompt_forwards_passcode_on_success(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["accept"])
    prompts: list[str] = []
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: prompts.append(prompt) or "11112222")
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path)

    broker.ensure_authenticated(driver, "node03")

    assert driver.sent_secrets == ["11112222"]
    assert len(prompts) == 1
    assert "node03" in prompts[0] and "PASSCODE" in prompts[0]


def test_broker_prompt_reprompts_on_stale_code(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["reject", "accept"])
    monkeypatch.setattr(auth, "_prompt_for_secret", _codes("staleCODE", "freshCODE"))
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path)

    broker.ensure_authenticated(driver, "node04")

    assert driver.sent_secrets == ["staleCODE", "freshCODE"]


def test_broker_prompt_raises_after_exhausting_attempts(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["reject", "reject", "reject"])
    monkeypatch.setattr(auth, "_prompt_for_secret", _codes("a", "b", "c"))
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path, max_attempts=3)

    with pytest.raises(AuthenticationError):
        broker.ensure_authenticated(driver, "node03")

    assert driver.sent_secrets == ["a", "b", "c"]


def test_broker_prompt_noop_when_preauthed(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["preauthed"])
    prompts: list[str] = []
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: prompts.append(prompt) or "x")
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path)

    broker.ensure_authenticated(driver, "node03")

    assert prompts == []
    assert driver.sent_secrets == []


def test_broker_prompt_reuses_existing_authenticated_pane_without_restart(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["accept"])
    driver.at_shell_prompt = lambda: True
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: "should-not-prompt")
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path)

    broker.ensure_authenticated(driver, "node03")

    assert driver.start_session_calls == []
    assert driver.sent_secrets == []


def test_broker_prompt_uses_auth_wait_seconds_after_submitting_code(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["accept"])
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: "11112222")
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path, wait_seconds=123.0)

    broker.ensure_authenticated(driver, "node03")

    assert driver.await_timeouts == [123.0]


def test_broker_prompt_toggles_waiting_on_around_secret_read(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["accept"])
    waiting_states: list[str | None] = []

    def record_waiting(waiting_on: str | None) -> None:
        waiting_states.append(waiting_on)

    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: "11112222")
    broker, tracker = _broker(fake_tmux, tmp_path=tmp_path)
    tracker.start("auth node03", phase="auth", node="node03")
    monkeypatch.setattr(tracker, "set_waiting", record_waiting)

    broker.ensure_authenticated(driver, "node03")

    assert waiting_states == ["operator", None]


# ---------------------------------------------------------------------------
# AuthBroker pane mode
# ---------------------------------------------------------------------------


def test_broker_pane_waits_for_operator_typed_passcode_without_prompt(fake_tmux, tmp_path) -> None:
    driver = fake_tmux(auth_script=["accept"])
    broker, tracker = _broker(fake_tmux, tmp_path=tmp_path, auth_mode="pane")

    broker.ensure_authenticated(driver, "node03")

    assert driver.sent_secrets == []
    assert any("waiting for node03 RSA" in event for event in tracker.events)


def test_broker_pane_reuses_existing_authenticated_session(fake_tmux, tmp_path) -> None:
    driver = fake_tmux(auth_script=["preauthed"])
    broker, tracker = _broker(fake_tmux, tmp_path=tmp_path, auth_mode="pane")

    broker.ensure_authenticated(driver, "node03")

    assert driver.sent_secrets == []
    assert not any("waiting for node03 RSA" in event for event in tracker.events)


def test_broker_pane_toggles_waiting_on_around_await(fake_tmux, tmp_path) -> None:
    driver = fake_tmux(auth_script=["accept"])
    waiting_states: list[str | None] = []
    broker, tracker = _broker(fake_tmux, tmp_path=tmp_path, auth_mode="pane")
    tracker.start("auth node03", phase="auth", node="node03")
    original_set_waiting = tracker.set_waiting

    def record_waiting(waiting_on: str | None) -> None:
        waiting_states.append(waiting_on)
        original_set_waiting(waiting_on)

    tracker.set_waiting = record_waiting  # type: ignore[method-assign]

    broker.ensure_authenticated(driver, "node03")

    assert waiting_states == ["operator", None]


# ---------------------------------------------------------------------------
# Kerberos (only paid when deep smoke needs it)
# ---------------------------------------------------------------------------


def test_ensure_kerberos_short_circuits_on_existing_ticket(fake_tmux) -> None:
    driver = fake_tmux(klist_code=0)

    check = ensure_kerberos(driver, "node03", prompt_fn=lambda prompt: "should-not-be-asked")

    assert check.name == "kerberos"
    assert check.passed is True
    assert "Existing" in check.message
    assert driver.sent_secrets == []
    assert not driver.ran("kinit")


def test_ensure_kerberos_acquires_ticket(fake_tmux) -> None:
    driver = fake_tmux(klist_code=[1, 0])
    prompts: list[str] = []

    check = ensure_kerberos(driver, "node03", prompt_fn=lambda prompt: prompts.append(prompt) or "kerbPW")

    assert check.passed is True
    assert "acquired" in check.message
    assert driver.sent_secrets == ["kerbPW"]
    assert any("kinit" in key for key in driver.sent_keys)
    assert len(prompts) == 1


def test_ensure_kerberos_fails_after_attempts(fake_tmux) -> None:
    driver = fake_tmux(klist_code=1)

    check = ensure_kerberos(driver, "node03", prompt_fn=lambda prompt: "kerbPW", max_attempts=2)

    assert check.passed is False
    assert "Could not acquire" in check.message
    assert len(driver.sent_secrets) == 2


# ---------------------------------------------------------------------------
# Secret hygiene (ADR-0002): never through run_remote
# ---------------------------------------------------------------------------


def test_passcode_never_travels_through_run_remote(fake_tmux, tmp_path, monkeypatch) -> None:
    driver = fake_tmux(auth_script=["accept"])
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: "SUPER-SECRET-CODE")
    broker, _tracker = _broker(fake_tmux, tmp_path=tmp_path)

    broker.ensure_authenticated(driver, "node03")

    assert "SUPER-SECRET-CODE" in driver.sent_secrets
    assert not any("SUPER-SECRET-CODE" in command for command in driver.commands)


def test_kerberos_password_never_travels_through_run_remote(fake_tmux) -> None:
    driver = fake_tmux(klist_code=[1, 0])

    ensure_kerberos(driver, "node03", prompt_fn=lambda prompt: "KERB-SECRET")

    assert "KERB-SECRET" in driver.sent_secrets
    assert not any("KERB-SECRET" in command for command in driver.commands)
