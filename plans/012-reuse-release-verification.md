# Plan 012: Reuse durable verification and preserve publish diagnostics

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update this plan's status in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 44c0969..HEAD -- edge_deploy/publish.py edge_deploy/phases/publish.py edge_deploy/release.py tests/test_publish.py tests/test_phases_publish.py tests/test_cli.py docs/release-workflow.md docs/adr`
> If an in-scope file changed, compare the current behavior described below
> with the live code before proceeding. Treat a semantic mismatch as a STOP
> condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug / DX / architecture
- **Planned at**: `origin/main` commit `44c0969`, 2026-07-15

## Why this matters

`publish-phase` currently refuses to run until the run ledger records
`verify: passed`, then independently invokes the tool's `local_check.ps1` and
runs the test suite again. This duplicated gate added roughly seven minutes to
the Dispatch publish attempt and reintroduced a transient Windows/Textual test
failure after the source-bound release verification had already passed.

When the second gate fails, `edge_deploy.publish._run_local_check_ps1()` has a
captured output tail, but `publish_snapshot()` raises a generic `PublishError`
containing only the exit code. The run remains `publish: pending`, and neither
the ledger nor a run artifact identifies the failing test. The fix must reuse
valid verification evidence for normal ledger-backed releases while retaining
the local check for standalone publishing and incomplete legacy ledgers.

## Current state

- `edge_deploy/phases/verify.py:84-94` records the verified commit, successful
  GitHub CI, passed tests, and verification timestamp in the run ledger.
- `edge_deploy/phases/publish.py:70-103` requires `verify: passed` but then
  calls `publish_snapshot(..., run_local_check=not no_local_check)` without
  consulting that evidence.
- `edge_deploy/publish.py:142-167` captures the final 20 non-empty output lines
  from `local_check.ps1`.
- `edge_deploy/publish.py:254-262` discards that tail on nonzero exit and raises
  only `PublishError("local_check.ps1 failed with exit code ...")`.
- `edge_deploy/release.py:218-239` writes successful publish reports but has no
  failed-local-check report path.
- `docs/adr/0008-run-ledger-and-posture-phases.md` identifies repeated test
  suites on retry as a failure the durable ledger was intended to prevent.
- Secret-bearing persisted text must pass through
  `edge_deploy.reporting.redact()` before being written.

## Target behavior

| Scenario | Required behavior |
|---|---|
| Ledger verification is complete and bound to the run source SHA | Publish reuses it and does not run `local_check.ps1` |
| Verification evidence is missing, incomplete, or names another SHA | Publish runs `local_check.ps1` |
| Standalone `edge_deploy publish` | Local check remains enabled by default |
| Local check fails | Redacted tail is displayed and persisted; publish becomes `failed` |
| Failed publish is retried | Retry is safe; no valid verification is repeated |
| `--no-local-check` on a legacy/incomplete run | Existing explicit bypass remains supported |

## Commands you will need

| Purpose | Command | Expected result |
|---|---|---|
| Focused tests | `py -m pytest tests/test_publish.py tests/test_phases_publish.py tests/test_cli.py -q` | exit 0 |
| Full tests | `py -m pytest -n 4 --dist loadfile` | all tests pass |
| Lint | `py -m ruff check .` | exit 0, no findings |
| Diff hygiene | `git diff --check` | no output, exit 0 |

## Scope

**In scope**:

- `edge_deploy/publish.py`
- `edge_deploy/phases/publish.py`
- `edge_deploy/release.py`
- `tests/test_publish.py`
- `tests/test_phases_publish.py`
- `tests/test_cli.py`, only if operator-output coverage belongs there
- `docs/release-workflow.md`
- One new ADR under `docs/adr/`
- `plans/README.md`, status update only

**Out of scope**:

- Tool repositories and their `local_check.ps1` scripts
- Changes to verification commands or pytest worker counts
- Engine identity semantics
- Bitbucket, GitHub, deployment, tagging, or release execution
- Removing the standalone `publish` command's local gate
- Persisting raw subprocess output or any credential-bearing values

## Git workflow

- Start from a clean, current GitHub `main`; do not reuse a dirty operator
  checkout. Prefer a fresh worktree if another task owns the existing branch.
- Branch: `codex/reuse-release-verification`
- Run the full test and Ruff gates before pushing.
- Open a GitHub pull request; do not merge it or publish a release.

## Steps

### Step 1: Characterize source-bound verification reuse

Add tests in `tests/test_phases_publish.py` before changing implementation.
Cover these cases:

1. `verify.state == "passed"` with evidence containing the exact
   `source_sha`, `ci == "success"`, `tests == "passed"`, and `verified_at`
   causes `run_publish_phase()` to call the injected publisher with
   `run_local_check=False`.
2. A different evidence commit causes local check to remain enabled.
3. Missing `ci`, `tests`, or `verified_at` is treated as incomplete legacy
   evidence and causes local check to remain enabled.
4. `--no-local-check` still disables the fallback for incomplete evidence.

Update the test ledger factory so tests that mean "fully verified" create the
same evidence shape as `ensure_verified()` rather than only `{"commit": sha}`.

**Verify**:
`py -m pytest tests/test_phases_publish.py -q` should fail only on the new
expectations before implementation and pass after Step 2.

### Step 2: Reuse complete verification evidence

In `edge_deploy/phases/publish.py`, add a small helper that returns true only
when all source-bound verification fields are valid:

- phase state is `passed`;
- evidence commit exactly equals `ledger.state["source_sha"]`;
- CI evidence is `success`;
- test evidence is `passed`;
- `verified_at` is a non-empty string.

Derive `run_local_check` as follows:

- false when complete verification evidence is reusable;
- false when the operator supplied the existing `--no-local-check` bypass;
- true otherwise.

Do not change `publish_snapshot()` defaults: direct, non-ledger publication
must continue to run the tool-specific gate.

**Verify**: `py -m pytest tests/test_phases_publish.py -q` exits 0.

### Step 3: Preserve structured local-check failure evidence

In `edge_deploy/publish.py`, introduce a specific `LocalCheckError` subclass of
`PublishError` carrying `exit_code` and `output_tail`. Raise it when the
captured local check exits nonzero. The generic string form may include the
exit code, but raw output must not be interpolated into an exception that can
be logged without redaction.

Add unit tests in `tests/test_publish.py` proving:

- exit code and output tail survive the failure;
- no git fetch, commit, or push occurs after a failed local check;
- successful and standalone local-check behavior is unchanged.

**Verify**: `py -m pytest tests/test_publish.py -q` exits 0.

### Step 4: Redact, persist, and ledger the failure

In `run_publish_phase()`, catch `LocalCheckError`. Pass its tail through
`edge_deploy.reporting.redact()` before any console or filesystem write.

Persist the redacted text as `publish-local-check.log` under the run directory.
Set the publish phase to `failed` with compact evidence containing:

- `error_type: "LocalCheckError"`;
- the exit code;
- the relative diagnostic artifact name;
- no raw output and no absolute operator configuration paths.

Print the concise failure plus the redacted tail to stderr, then preserve the
CLI's established nonzero result. A subsequent `publish-phase --run ...` must
remain legal.

Add tests proving password, token, passcode, and Bearer-header patterns are
masked in console output, ledger evidence, and the diagnostic artifact. Model
the assertions after existing `reporting.redact()` coverage.

**Verify**:
`py -m pytest tests/test_phases_publish.py tests/test_cli.py -q` exits 0.

### Step 5: Record how the publish gate was satisfied

Publish evidence and the successful publish report must distinguish:

- `verification_source: "run-ledger"` when durable verification was reused;
- `verification_source: "local-check"` when the script actually ran;
- `local_check_ran: true|false`.

Do not report a skipped local check as though it executed successfully. Keep
existing snapshot, source, and previous-remote evidence compatible with status,
deploy, and tag phases.

**Verify**: focused tests assert both evidence variants and existing publish
report consumers continue to pass.

### Step 6: Document the durable gate contract

Add an ADR describing these decisions:

- verification evidence is reusable only when bound to the immutable run
  source SHA and contains successful CI and test evidence;
- normal ledger-backed publish does not repeat the verified test suite;
- standalone publish retains its local check;
- incomplete legacy evidence falls back safely;
- failed local-check diagnostics are redacted before persistence.

Update `docs/release-workflow.md` so operators know that publish normally
reuses verify evidence and that a legacy run may execute the fallback gate.

**Verify**: `rg -n "verification_source|local.check|legacy" docs` finds the
new contract in both the workflow and ADR.

### Step 7: Run the repository gates

```powershell
py -m pytest tests/test_publish.py tests/test_phases_publish.py tests/test_cli.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
git diff --check
```

All commands must exit 0. Confirm `git status --short` contains only the files
listed under **In scope**.

## Done criteria

- [ ] A fully verified ledger-backed release does not invoke
      `local_check.ps1` during publish.
- [ ] Verification reuse requires the exact run source SHA and complete CI,
      tests, and timestamp evidence.
- [ ] Incomplete or legacy verification evidence falls back to local check.
- [ ] Standalone publishing still runs local check by default.
- [ ] A local-check failure produces a redacted diagnostic artifact.
- [ ] The publish phase becomes `failed`, not `pending`, on that failure.
- [ ] Retry remains legal and performs no duplicate valid verification.
- [ ] No Bitbucket mutation occurs before all applicable gates pass.
- [ ] Publish evidence states whether verification came from the ledger or the
      local check.
- [ ] Focused tests, full pytest, Ruff, and diff hygiene all pass.
- [ ] CI passes on Python 3.10 and 3.12 and the change is reviewed through a
      GitHub pull request.

## STOP conditions

Stop and report instead of improvising if:

- Current verification evidence cannot be tied conclusively to
  `ledger.state["source_sha"]`.
- Supporting a legacy ledger would require silently trusting incomplete
  evidence.
- Diagnostic output cannot be redacted before it reaches disk or stderr.
- The fix requires changing a tool repository's `local_check.ps1`.
- Existing publish report consumers require a breaking schema change.
- An in-scope file has semantically drifted from commit `44c0969`.
- A verification command fails twice after a reasonable correction.

## Maintenance notes

- Future changes to verification evidence must update the reuse predicate and
  its legacy-fallback tests together.
- Reviewers should scrutinize the boundary between raw subprocess output and
  `reporting.redact()`; no raw tail may escape it.
- The engine fingerprint will change when this lands. Existing open runs must
  be completed with their original engine or abandoned, per engine-identity
  policy.
- This plan deliberately does not remove tool-specific local checks. It makes
  their use source-aware and preserves them for standalone and legacy paths.
