# Source-bound verification reuse and publish diagnostics

## Context

The durable run ledger records successful GitHub CI and tool tests during the
verify phase. Publish nevertheless repeated each tool's `local_check.ps1`,
adding several minutes and reintroducing transient test failures after the
immutable release source had already been verified. When that fallback gate
failed, its captured output tail was discarded, leaving publish pending with
no actionable diagnostic artifact.

ADR-0008 introduced the ledger specifically so retries would not repeat valid
work. Reusing verification must remain fail-safe: legacy or incomplete ledger
evidence cannot be silently trusted, and standalone publishing still needs its
tool-owned local gate.

## Decision

1. Ledger verification is reusable only when the verify phase is `passed` and
   its evidence names the run's exact immutable `source_sha`, records
   `ci: success`, records `tests: passed`, and includes a non-empty
   `verified_at` timestamp.
2. A normal ledger-backed publish with reusable evidence does not execute
   `local_check.ps1`. Publish evidence and its report record
   `verification_source: run-ledger` and `local_check_ran: false`.
3. Missing, incomplete, or source-mismatched legacy evidence falls back to the
   tool's local check. A successful fallback records
   `verification_source: local-check` and `local_check_ran: true`.
4. Standalone `edge_deploy publish` continues to run the local check by
   default. The existing explicit `--no-local-check` operator bypass remains
   available and is reported as `operator-bypass`, never as an executed check.
5. A failed local check becomes a durable `publish: failed` state. Its final
   output tail is redacted before it reaches stderr or disk and is persisted as
   the relative run artifact `publish-local-check.log`. Ledger evidence stores
   only the error type, exit code, and relative artifact name.
6. A failed publish remains retryable. No fetch, snapshot creation, or push may
   occur before the applicable verification gate succeeds.

## Consequences

- Normal releases do not repeat a valid source-bound test suite at publish.
- Incomplete legacy runs fail safe by executing their committed tool gate.
- Operators receive durable, secret-redacted failure evidence instead of a
  generic exit code.
- Publish reports accurately distinguish reused verification from an actual
  local-check execution or explicit bypass.
- Any future verification-evidence change must update the reuse predicate and
  its legacy-fallback tests together.

## Relationship to prior decisions

This ADR completes ADR-0008's durable-work contract for publish retries. It
does not change engine identity, posture boundaries, standalone publication,
or the contents of tool-specific local checks.
