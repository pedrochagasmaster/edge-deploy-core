"""The Operator auth seam (ADR-0002): getpass forward, re-prompt on stale code,
exhaustion, Kerberos-only-when-needed, and the hard rule that a secret never travels
through ``run_remote`` (only through ``submit_secret``).

All secrets here are fakes typed by a monkeypatched ``getpass_fn``; no real tmux/SSH.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from edge_deploy.auth import authenticate_node, ensure_kerberos
from edge_deploy.tmux_driver import AuthenticationError


def _codes(*values: str):
    iterator: Iterator[str] = iter(values)

    def getpass_fn(prompt: str) -> str:
        return next(iterator)

    return getpass_fn


# ---------------------------------------------------------------------------
# RSA passcode forward / re-prompt / exhaustion
# ---------------------------------------------------------------------------


def test_authenticate_forwards_passcode_on_success(fake_tmux) -> None:
    driver = fake_tmux(auth_script=["accept"])
    prompts: list[str] = []

    authenticate_node(driver, "node03", getpass_fn=lambda prompt: prompts.append(prompt) or "11112222")

    assert driver.sent_secrets == ["11112222"]
    assert len(prompts) == 1
    assert "node03" in prompts[0] and "PASSCODE" in prompts[0]


def test_authenticate_reprompts_on_stale_code(fake_tmux) -> None:
    driver = fake_tmux(auth_script=["reject", "accept"])

    authenticate_node(driver, "node04", getpass_fn=_codes("staleCODE", "freshCODE"))

    # A stale single-use code forces a fresh re-prompt; both codes were forwarded.
    assert driver.sent_secrets == ["staleCODE", "freshCODE"]


def test_authenticate_raises_after_exhausting_attempts(fake_tmux) -> None:
    driver = fake_tmux(auth_script=["reject", "reject", "reject"])

    with pytest.raises(AuthenticationError):
        authenticate_node(driver, "node03", getpass_fn=_codes("a", "b", "c"), max_attempts=3)

    assert driver.sent_secrets == ["a", "b", "c"]


def test_authenticate_noop_when_preauthed(fake_tmux) -> None:
    driver = fake_tmux(auth_script=["preauthed"])
    prompts: list[str] = []

    authenticate_node(driver, "node03", getpass_fn=lambda prompt: prompts.append(prompt) or "x")

    assert prompts == []
    assert driver.sent_secrets == []


# ---------------------------------------------------------------------------
# Kerberos (only paid when deep smoke needs it)
# ---------------------------------------------------------------------------


def test_ensure_kerberos_short_circuits_on_existing_ticket(fake_tmux) -> None:
    driver = fake_tmux(klist_code=0)

    check = ensure_kerberos(driver, "node03", getpass_fn=lambda prompt: "should-not-be-asked")

    assert check.name == "kerberos"
    assert check.passed is True
    assert "Existing" in check.message
    assert driver.sent_secrets == []  # no kinit, no password prompt
    assert not driver.ran("kinit")


def test_ensure_kerberos_acquires_ticket(fake_tmux) -> None:
    driver = fake_tmux(klist_code=[1, 0])  # no ticket, then acquired after kinit
    prompts: list[str] = []

    check = ensure_kerberos(driver, "node03", getpass_fn=lambda prompt: prompts.append(prompt) or "kerbPW")

    assert check.passed is True
    assert "acquired" in check.message
    assert driver.sent_secrets == ["kerbPW"]
    assert any("kinit" in key for key in driver.sent_keys)
    assert len(prompts) == 1


def test_ensure_kerberos_fails_after_attempts(fake_tmux) -> None:
    driver = fake_tmux(klist_code=1)  # never acquires

    check = ensure_kerberos(driver, "node03", getpass_fn=lambda prompt: "kerbPW", max_attempts=2)

    assert check.passed is False
    assert "Could not acquire" in check.message
    assert len(driver.sent_secrets) == 2


# ---------------------------------------------------------------------------
# Secret hygiene (ADR-0002): never through run_remote
# ---------------------------------------------------------------------------


def test_passcode_never_travels_through_run_remote(fake_tmux) -> None:
    driver = fake_tmux(auth_script=["accept"])

    authenticate_node(driver, "node03", getpass_fn=lambda prompt: "SUPER-SECRET-CODE")

    assert "SUPER-SECRET-CODE" in driver.sent_secrets
    assert not any("SUPER-SECRET-CODE" in command for command in driver.commands)


def test_kerberos_password_never_travels_through_run_remote(fake_tmux) -> None:
    driver = fake_tmux(klist_code=[1, 0])

    ensure_kerberos(driver, "node03", getpass_fn=lambda prompt: "KERB-SECRET")

    assert "KERB-SECRET" in driver.sent_secrets
    assert not any("KERB-SECRET" in command for command in driver.commands)
