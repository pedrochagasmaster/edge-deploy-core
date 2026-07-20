# Tool-owned source verification

## Context

The release engine previously selected pytest and hard-coded eight xdist
workers after GitHub CI succeeded. That command drifted from the tool's CI and
from its committed Windows validation. On the Windows Release Operator host,
the extra parallelism exposed process-cleanup races and could hang without a
test summary. A green source SHA therefore did not imply that the engine's
separate command would pass or even finish.

Each tool already owns `tools/dev/local_check.ps1`. It is the reviewed place
for test selection, worker count, temporary-directory isolation, compile and
lint checks, and platform setup.

## Decision

1. Verify binds the exact checkout SHA, requires successful GitHub CI for that
   SHA, and then executes the checkout's committed
   `tools/dev/local_check.ps1` exactly once.
2. Only exit zero records `ci: success`, `tests: passed`, a non-empty
   `verified_at`, and `verification_command: tools/dev/local_check.ps1`.
   Failures persist only a redacted diagnostic tail and compact ledger
   metadata; missing scripts or PowerShell fail closed.
3. Publish reuses complete source-bound verification using ADR-0015's exact
   predicate. Legacy or incomplete evidence retains its publish fallback.
4. `--no-local-check` bypasses only that publish fallback. It never skips
   verify and cannot manufacture successful verify evidence.
5. A tool that changes its local check changes future verification through
   its reviewed source SHA. No release-engine API or inferred pytest command
   changes with it.
6. Tools released from a Windows controller must continuously run the
   committed gate on Windows CI. Linux-only CI does not cover this release
   boundary.

## Consequences

- Verification contents have one owner and one source-bound implementation.
- The engine remains responsible for provenance, ordering, redaction, and
  fail-closed evidence, but not tool-specific test flags.
- Verify does not repeat a successful exact-source gate on resume; publish
  does not repeat evidence that satisfies ADR-0015.
- Adding a non-PowerShell gate requires an explicit Tool Profile contract and
  a new decision rather than filename inference or an engine-selected
  fallback.

## Relationship to prior decisions

This decision preserves ADR-0015's publish reuse predicate unchanged and
supersedes Plan 003's proposal to make engine-selected parallel pytest the
release/CI parity command. Ruff and cache improvements from that plan require
separate, verification-neutral work.
