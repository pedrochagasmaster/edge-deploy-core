# Plan 009: Test the error paths that fire mid-release — dependency delivery, ledger atomic writes, drift globs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d8ec786..HEAD -- edge_deploy/dependencies.py edge_deploy/ledger.py edge_deploy/drift.py tests/test_dependencies.py tests/test_ledger.py tests/test_drift.py`
> If any in-scope source file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition. **Exception**: if plan 007 landed,
> `drift.py` will have a batched `local_runtime_map` and a D8-based
> `remote_runtime_map` — Part C below is unaffected (it targets `_glob_regex`,
> which plan 007 does not touch).

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (tests only; no production code changes — with one narrow escape hatch in Part C)
- **Depends on**: none (composes with 008: re-measure coverage after landing)
- **Category**: tests
- **Planned at**: commit `d8ec786`, 2026-07-07

## Why this matters

The happy paths of this release engine are thoroughly tested (369 tests,
FakeTmuxDriver end-to-end coverage). The 2026-07-07 deep audit found the gaps
concentrated in *error paths* — precisely the code that runs when a production
release is going wrong, when the operator most needs correct behavior:

1. `deliver_dependency_bundle` (`dependencies.py`) raises `BundleError` at
   four distinct failure points; tests exercise none of them.
2. `_write_json_atomic` (`ledger.py`) contains Windows-specific retry logic
   guarding every ledger write of every phase; it has zero direct tests.
3. `_glob_regex` (`drift.py`) decides which files count as runtime-critical
   for drift — the last gate of a deploy; only well-formed patterns are tested.

## Current state

### Part A — dependency delivery error paths

`edge_deploy/dependencies.py:345-409` (`deliver_dependency_bundle`). The
untested raise sites:

```python
    if config is None:
        raise BundleError(f"{profile.tool} has no dependency bundle configuration")   # line 355
    ...
    if code:
        raise BundleError(f"could not create remote bundle staging directory: {screen.strip()}")  # line 370
    ...
    if step_result.get("exit_code") != 0:
        tail = step_result.get("stdout_tail", "")
        raise BundleError(f"remote dependency verification failed: {tail}")           # lines 398-400
    evidence_path = f"~/.edge-deploy/runs/{run_id}/steps/dependency-stage-evidence.json"
    evidence = read_remote_json(driver, evidence_path)
    if "remote_dir" not in evidence or "reused" not in evidence:
        raise BundleError("remote dependency verification returned no provenance")    # lines 403-404
```

Existing tests (`tests/test_dependencies.py`, 6 tests) cover bundle identity,
manifest determinism, and two delivery happy paths:
`test_delivery_transfers_then_records_verified_remote_stage` (line 122) and
`test_delivery_reuse_path_unchanged` (line 176). Model new tests on those two —
they build a real bundle in `tmp_path` and drive a `FakeTmuxDriver`.

`FakeTmuxDriver` knobs you will need (documented at `tests/conftest.py:96-126`):
- `command_codes` — `{substring: exit_code}` overrides, e.g. make the
  `mkdir -p ...` staging command fail with `{"mkdir -p": 1}`.
- `runner_step_results` — `{step_name: payload}`; set
  `{"dependency-stage": {"schema": "edge-deploy/step/1", "step": "dependency-stage", "exit_code": 1, "stdout_tail": "boom"}}`
  to exercise the step-failure raise, or
  `{"dependency-stage-evidence": {"unexpected": true}}` to exercise the
  missing-provenance raise.

### Part B — ledger atomic write retry

`edge_deploy/ledger.py:71-84`:

```python
def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Windows refuses os.replace while a concurrent reader (status, another
    # phase's load) holds the target open; retry briefly instead of crashing
    # a release mid-phase.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
```

`tests/test_ledger.py` (10 tests) never touches this function directly.

### Part C — drift glob translator edge cases

`edge_deploy/drift.py:58-82` (`_glob_regex`) hand-translates `**/`, `**`, `*`,
`?` and escapes everything else, compiled with a `\Z` anchor. Existing
coverage: `tests/test_drift.py:104-119` parametrizes nine *valid* patterns.
Untested shapes that profiles could plausibly contain: `""` (empty pattern),
`"**"` alone, `"a[b].py"` (regex metachars — should be literal via
`re.escape`), trailing `"dir/"`, `"a**b"` (adjacent `**` mid-segment),
Windows-style `"core\\engine.py"` (must NOT match — paths are posix).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `py -m pip install -e ".[dev]"` | exit 0 |
| Focused | `py -m pytest tests/test_dependencies.py tests/test_ledger.py tests/test_drift.py -q` | all pass |
| Full | `py -m pytest -n 4 --dist loadfile` | all pass |
| Lint | `py -m ruff check .` | no new violations |

(Windows controller: use `py`; Linux/CI uses `python`.)

## Scope

**In scope**:
- `tests/test_dependencies.py`, `tests/test_ledger.py`, `tests/test_drift.py` (add tests)
- `tests/conftest.py` — ONLY if a FakeTmuxDriver knob listed above turns out
  not to support an injection you need; extend the fake following its
  documented knob style (additive, defaulting to current behavior)
- `edge_deploy/drift.py` — ONLY under the Part C escape hatch below

**Out of scope**:
- Any production behavior change in `dependencies.py` or `ledger.py`. If a
  test reveals a real bug, STOP and report it — do not fix it in this plan.
- `edge_deploy/auth.py` / CLI error-path tests (audited as minor; deliberately
  deferred).

## Git workflow

- Branch: `advisor/009-error-path-tests` off `main`; PR per CONTRIBUTING.md.
- Commit style: `test: cover dependency-delivery, ledger-retry, and drift-glob error paths`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Dependency delivery failure tests (Part A)

Add to `tests/test_dependencies.py`, modeled structurally on
`test_delivery_transfers_then_records_verified_remote_stage` (line 122):

1. `test_delivery_refuses_profile_without_bundle_config` — profile whose
   `dependency_bundle` is None → `pytest.raises(BundleError, match="no dependency bundle")`.
2. `test_delivery_fails_when_staging_dir_cannot_be_created` —
   `command_codes={"mkdir -p": 1}` → `BundleError` matching "staging directory".
3. `test_delivery_fails_when_remote_stage_step_fails` —
   `runner_step_results={"dependency-stage": {...exit_code: 1, stdout_tail: "boom"...}}`
   → `BundleError` matching "boom" (proves the stdout tail is surfaced to the operator).
4. `test_delivery_fails_on_missing_provenance` — evidence payload without
   `remote_dir`/`reused` keys → `BundleError` matching "no provenance".

**Verify**: `py -m pytest tests/test_dependencies.py -q` → all pass (6 old + 4 new).

### Step 2: Ledger atomic-write retry tests (Part B)

Add to `tests/test_ledger.py`, using `monkeypatch`:

1. `test_write_json_atomic_retries_transient_permission_error` — patch
   `os.replace` with a callable raising `PermissionError` twice then
   delegating to the real `os.replace`; also patch `time.sleep` to record
   sleeps without waiting. Assert: file lands with the payload, exactly 2
   sleeps occurred, backoff values are `0.05` then `0.10` (approx).
2. `test_write_json_atomic_raises_after_exhausting_retries` — patch
   `os.replace` to always raise `PermissionError`, patch `time.sleep` to
   no-op. Assert `pytest.raises(PermissionError)` and that it attempted 5
   times (count calls).

Patch the names as used inside `edge_deploy.ledger` (`monkeypatch.setattr("edge_deploy.ledger.os.replace", ...)`,
`monkeypatch.setattr("edge_deploy.ledger.time.sleep", ...)`).

**Verify**: `py -m pytest tests/test_ledger.py -q` → all pass.

### Step 3: Drift glob edge-case tests (Part C)

Extend the parametrize table at `tests/test_drift.py:104-117` (or add a
sibling test) with the edge shapes from "Current state" Part C. For each,
first determine the *actual* current behavior by running the translator, then
encode the sane expectation:

- `"a[b].py"` matches the literal path `a[b].py` and not `ab.py` (re.escape guarantees this — assert it).
- Backslash path `"core\\engine.py"` does not match `core/engine.py`.
- `"**"` alone matches any path.
- `""` matches only the empty string (degenerate but harmless), or — if it
  matches everything — see the escape hatch.

**Escape hatch (the only permitted production edit)**: if the empty pattern
turns out to match every file (over-inclusive drift surface), add a guard to
`ToolProfile`-adjacent validation ONLY if it is a one-line reject in
`_glob_regex` (e.g. raise `ValueError` on empty pattern) — and add the test
for the raise. If the fix would touch `config.py` or profile loading, STOP
and report instead.

**Verify**: `py -m pytest tests/test_drift.py -q` → all pass.

### Step 4: Full suite

**Verify**: `py -m pytest -n 4 --dist loadfile` → all pass;
`py -m ruff check .` → no new violations.

## Test plan

The plan *is* the test plan: ~10 new tests across three files, enumerated in
steps 1–3, each modeled on a named existing test.

## Done criteria

- [ ] `py -m pytest -n 4 --dist loadfile` exits 0
- [ ] `py -m pytest tests/test_dependencies.py -q` shows ≥ 4 new tests, all raising `BundleError` paths covered (spot-check: `grep -c "BundleError" tests/test_dependencies.py` ≥ 4)
- [ ] `grep -c "_write_json_atomic" tests/test_ledger.py` ≥ 2
- [ ] New glob edge cases present: `grep -n "a\[b\]" tests/test_drift.py` matches
- [ ] No production files modified except (possibly) the one-line Part C escape hatch (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- Any new test reveals a genuine production bug (e.g. the retry loop miscounts,
  a BundleError path is unreachable, the empty glob matches everything and the
  fix exceeds one line). Report the failing behavior with the test output —
  the fix belongs in its own reviewed change.
- `FakeTmuxDriver` cannot inject a failure listed in Part A even after an
  additive knob extension (would indicate the fake's routing diverged from
  `conftest.py:96-126`).
- The code at the "Current state" locations doesn't match the excerpts
  (beyond the documented plan-007 exception).

## Maintenance notes

- After this lands, re-run plan 008's baseline measurement and raise
  `fail_under` accordingly.
- Reviewers: check the ledger tests patch `edge_deploy.ledger.os.replace`
  (module attribute), not global `os.replace` — patching the wrong target
  passes trivially.
- Deferred deliberately: auth-seam timeout/session-gone tests and CLI
  rollback error-path tests (audited as lower risk; candidates for a future
  plan if churn returns to those modules).
