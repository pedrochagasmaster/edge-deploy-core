import pytest

from edge_deploy.onboarding.checks import CheckResult, CheckSpec, run_checks
from edge_deploy.reporting import redact


def test_dependency_block_and_independent_failure_continue() -> None:
    calls: list[str] = []

    def ok(cid: str) -> CheckResult:
        calls.append(cid)
        return CheckResult(cid, "passed", "ok", "")

    def fail(cid: str) -> CheckResult:
        calls.append(cid)
        return CheckResult(cid, "failed", "boom", "fix it")

    specs = [
        CheckSpec("a", (), lambda: fail("a")),
        CheckSpec("b", ("a",), lambda: ok("b")),  # should block
        CheckSpec("c", (), lambda: ok("c")),  # independent, still runs
    ]
    results = run_checks(specs, max_workers=1)
    assert [r.id for r in results] == ["a", "b", "c"]
    assert results[0].outcome == "failed"
    assert results[1].outcome == "blocked"
    assert results[2].outcome == "passed"
    assert "b" not in calls
    assert calls == ["a", "c"]


def test_redacted_summary_never_embeds_token_assignment() -> None:
    result = CheckResult(
        "bb_token",
        "failed",
        redact("token=supersecret missing"),
        "Set BB_TOKEN in the environment",
    )
    assert "supersecret" not in result.summary
    assert "***REDACTED***" in result.summary


def test_duplicate_check_ids_rejected() -> None:
    specs = [
        CheckSpec("a", (), lambda: CheckResult("a", "passed", "ok", "")),
        CheckSpec("a", (), lambda: CheckResult("a", "passed", "ok", "")),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        run_checks(specs, max_workers=1)


def test_unknown_dependency_rejected() -> None:
    specs = [
        CheckSpec("a", ("missing",), lambda: CheckResult("a", "passed", "ok", "")),
    ]
    with pytest.raises(ValueError, match="unknown dependency"):
        run_checks(specs, max_workers=1)


def test_result_id_mismatch_rejected() -> None:
    specs = [
        CheckSpec("a", (), lambda: CheckResult("other", "passed", "ok", "")),
    ]
    with pytest.raises(ValueError, match="result id"):
        run_checks(specs, max_workers=1)


def test_invalid_outcome_rejected() -> None:
    specs = [
        CheckSpec("a", (), lambda: CheckResult("a", "skipped", "ok", "")),
    ]
    with pytest.raises(ValueError, match="outcome"):
        run_checks(specs, max_workers=1)


def test_check_exception_becomes_failed_and_continues() -> None:
    calls: list[str] = []

    def boom() -> CheckResult:
        calls.append("a")
        raise RuntimeError("token=leaked-secret exploded")

    def ok() -> CheckResult:
        calls.append("b")
        return CheckResult("b", "passed", "ok", "")

    results = run_checks(
        [
            CheckSpec("a", (), boom),
            CheckSpec("b", (), ok),
        ],
        max_workers=1,
    )
    assert [r.id for r in results] == ["a", "b"]
    assert results[0].outcome == "failed"
    assert "leaked-secret" not in results[0].summary
    assert "leaked-secret" not in results[0].remediation
    assert "***REDACTED***" in results[0].summary
    assert results[1].outcome == "passed"
    assert calls == ["a", "b"]


def test_runner_redacts_summary_and_remediation() -> None:
    results = run_checks(
        [
            CheckSpec(
                "secret",
                (),
                lambda: CheckResult(
                    "secret",
                    "failed",
                    "missing token=supersecret value",
                    "export token=supersecret and retry",
                ),
            ),
        ],
        max_workers=1,
    )
    assert "supersecret" not in results[0].summary
    assert "supersecret" not in results[0].remediation
    assert "***REDACTED***" in results[0].summary
    assert "***REDACTED***" in results[0].remediation


def test_base_exception_is_not_caught() -> None:
    def raise_keyboard_interrupt() -> CheckResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_checks(
            [CheckSpec("a", (), raise_keyboard_interrupt)],
            max_workers=1,
        )


def test_blocked_summary_names_failed_dependency() -> None:
    results = run_checks(
        [
            CheckSpec(
                "a",
                (),
                lambda: CheckResult("a", "failed", "boom", "fix a"),
            ),
            CheckSpec(
                "b",
                ("a",),
                lambda: CheckResult("b", "passed", "should not run", ""),
            ),
        ],
        max_workers=1,
    )
    assert results[1].outcome == "blocked"
    assert "a" in results[1].summary
