"""Dependency-aware readiness check runner (passed / failed / blocked)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from edge_deploy.reporting import redact

_VALID_OUTCOMES = frozenset({"passed", "failed", "blocked"})


@dataclass(frozen=True)
class CheckResult:
    id: str
    outcome: str
    summary: str
    remediation: str
    evidence_fingerprint: str | None = None


@dataclass(frozen=True)
class CheckSpec:
    id: str
    depends_on: tuple[str, ...]
    run: Callable[[], CheckResult]


def run_checks(specs: list[CheckSpec], *, max_workers: int = 4) -> list[CheckResult]:
    """Run readiness checks respecting dependencies; emit results in spec order.

    ``max_workers`` is accepted for API compatibility. Execution is serial and
    deterministic so report order always matches input order.
    """
    del max_workers  # serial scheduler; concurrency left for a later hardening pass
    _validate_graph(specs)

    outcomes: dict[str, str] = {}
    results: list[CheckResult] = []

    for spec in specs:
        unmet = [dep for dep in spec.depends_on if outcomes.get(dep) != "passed"]
        if unmet:
            result = CheckResult(
                spec.id,
                "blocked",
                f"blocked by unmet dependencies: {', '.join(unmet)}",
                "Resolve failed or blocked dependencies, then re-run readiness.",
            )
        else:
            result = _execute(spec)
        results.append(_redact_result(result))
        outcomes[spec.id] = result.outcome

    return results


def _validate_graph(specs: list[CheckSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ValueError(f"duplicate check id: {spec.id}")
        seen.add(spec.id)

    known = {spec.id for spec in specs}
    for spec in specs:
        for dep in spec.depends_on:
            if dep not in known:
                raise ValueError(
                    f"unknown dependency {dep!r} required by check {spec.id!r}"
                )


def _execute(spec: CheckSpec) -> CheckResult:
    try:
        result = spec.run()
    except Exception as exc:
        return CheckResult(
            spec.id,
            "failed",
            f"check raised {type(exc).__name__}: {exc}",
            "Inspect the check error, correct the environment, then re-run.",
        )

    if result.id != spec.id:
        raise ValueError(
            f"result id {result.id!r} does not match check id {spec.id!r}"
        )
    if result.outcome not in _VALID_OUTCOMES:
        raise ValueError(
            f"invalid outcome {result.outcome!r} for check {spec.id!r}; "
            f"expected one of {sorted(_VALID_OUTCOMES)}"
        )
    return result


def _redact_result(result: CheckResult) -> CheckResult:
    return CheckResult(
        result.id,
        result.outcome,
        redact(result.summary),
        redact(result.remediation),
        result.evidence_fingerprint,
    )
