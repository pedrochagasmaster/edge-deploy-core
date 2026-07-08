# Plan 007: Migrate the drift remote scan to D8 file evidence and batch local blob hashing

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d8ec786..HEAD -- edge_deploy/drift.py edge_deploy/verify.py edge_deploy/release.py edge_deploy/cli.py tests/test_drift.py tests/conftest.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW (drift check is read-only and idempotent; the runner-file pattern this migrates to is already proven in `dependencies.py`)
- **Depends on**: none
- **Category**: tech-debt (plus a performance win in step 1)
- **Planned at**: commit `d8ec786`, 2026-07-07

## Why this matters

ADR-0009 introduced the D8 protocol: remote results are written to files on the
Edge Node and read back base64-encoded between unique sentinels with SHA-256
digest verification, because the tmux/psmux pane is a lossy screen-scraped
channel (line wrap, interleaving, truncation — see ADR-0011). Dependency
delivery and rollout already use it. **Drift verification is the last
production path that still scrapes payload markers off the pane screen**
(`DRIFT_PAYLOAD_START`/`END` in `edge_deploy/drift.py`), and ADR-0009 itself
lists this as a known remainder. Drift is the final gate that declares a
deployed node correct, so it should be the *most* corruption-resistant read,
not the least. Separately, the local side of the same check spawns one
`git show` subprocess per runtime-critical file — on a Windows workstation
that is 50–150 ms per file, multiplied by file count and node count on every
deploy and every standalone `drift` command.

## Current state

Relevant files:

- `edge_deploy/drift.py` — drift check; contains both problems (details below).
- `edge_deploy/runner.py` — the D8 helpers to reuse: `read_remote_json(driver, remote_path)` (line 146) reads a remote file wrap-immune; `_read_remote_bytes` (line 101) does the sentinel/digest work.
- `edge_deploy/dependencies.py:390-402` — the exemplar consumer: runs a remote step, then `read_remote_json(driver, f"~/.edge-deploy/runs/{run_id}/steps/dependency-stage-evidence.json")`.
- `edge_deploy/verify.py:50` — `verify_after_rollout(...)` calls `check_drift(driver, profile, node, commit=commit, local_root=local_root)`.
- `edge_deploy/release.py:650-657` — the deploy-phase caller of `verify_after_rollout`; the run id is available there as `report_dir.name` (used at `release.py:599` as `run_id=report_dir.name`).
- `edge_deploy/cli.py:793` — the standalone `drift` command calls `check_drift(...)`. Note `cli.py:779`: the standalone `rollout` command uses the synthetic run id `"edge-deploy"` when no run ledger exists — reuse that convention.
- `tests/test_drift.py`, `tests/conftest.py` — the `FakeTmuxDriver` serves the current marker protocol at `conftest.py:384-390` and already has D8 plumbing (`runner_step_results`, and a `__EDGE_RESULT_START__`/`base64 -w0` branch at `conftest.py:364`).

### Problem 1 — per-file `git show` loop (local side)

`edge_deploy/drift.py:98-110`:

```python
def local_runtime_map(profile: ToolProfile, root: str | Path, commit: str) -> dict[str, str]:
    """Map each runtime-critical path in ``commit``'s tree to the md5 of its blob."""
    _git_output(["git", "rev-parse", "--verify", commit], root)
    mapping: dict[str, str] = {}
    for path in snapshot_runtime_paths(profile, root, commit):
        blob = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        mapping[path] = hashlib.md5(blob).hexdigest()
    return mapping
```

The docstring at `drift.py:86-91` records a deliberate decision you must
preserve: paths and blobs are enumerated from the **snapshot commit's tree**,
never the working tree (commit `c19d11e` fixed exactly this). Only the
*batching* changes.

### Problem 2 — screen-scraped remote scan

`edge_deploy/drift.py:113-150`: `_extract_payload()` finds
`DRIFT_PAYLOAD_START`/`DRIFT_PAYLOAD_END` markers on the captured screen, and
`remote_runtime_map()` sends a base64-encoded Python script whose output is
`print`ed to the pane and re-joined across wrapped lines:

```python
print("DRIFT_PAYLOAD_START")
print(json.dumps(payload, sort_keys=True))
print("DRIFT_PAYLOAD_END")
```

```python
    screen, code = _remote_python(driver, script, timeout=120)
    ...
    payload = "".join(_extract_payload(screen, "DRIFT_PAYLOAD_START", "DRIFT_PAYLOAD_END").splitlines())
    return json.loads(payload)
```

The `"".join(...splitlines())` is a wrap workaround, not wrap-proofing: it has
no digest check and a large JSON payload interleaved with a stray pane line
parses wrong or not at all. `runner.read_remote_json` already solves this
class of problem.

### Vocabulary to honor (CONTEXT.md)

- **Drift** — "a difference between runtime-critical files in the released commit and files present on an Edge Node."
- **Snapshot** — every drift judgment is against the Snapshot's tree, never the working tree.
- **Pane-Safe** — arbitrary content crosses the pane base64-encoded in bounded chunks, results read back between unique sentinels with digest verification. That is what this plan brings to drift.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `py -m pip install -e ".[dev]"` | exit 0 |
| Full tests | `py -m pytest -n 4 --dist loadfile` | all pass (369+ at planning time) |
| Focused tests | `py -m pytest tests/test_drift.py tests/test_verify.py tests/test_release.py tests/test_phases_deploy.py -q` | all pass |
| Lint | `py -m ruff check .` | only the pre-existing E501 at `edge_deploy/runner.py:44` (none if plan 003 landed first) |

(Windows controller: `python` is not on PATH; use the `py` launcher. On
Linux/CI use `python -m ...`.)

## Scope

**In scope** (the only files you should modify):
- `edge_deploy/drift.py`
- `edge_deploy/verify.py` (thread `run_id` through `verify_after_rollout`)
- `edge_deploy/release.py` (pass `run_id=report_dir.name` at the `verify_after_rollout` call site)
- `edge_deploy/cli.py` (pass `run_id="edge-deploy"` from `_cmd_drift`)
- `tests/test_drift.py`, `tests/test_verify.py`, `tests/conftest.py` (fake-driver routing), and any test that monkeypatches `drift.local_runtime_map` only if its signature interaction breaks (it should not — the signature is unchanged)

**Out of scope** (do NOT touch, even though they look related):
- `edge_deploy/runner.py` — reuse it as-is; do not refactor its marker constants (a shared-constants cleanup was considered and deferred).
- `edge_deploy/rollout.py`, `edge_deploy/dependencies.py` — already on D8.
- `edge_deploy/tmux_driver.py` — the transport layer is stable and heavily churned; no changes needed here.
- The engine-identity consequence: this change alters the package content hash, which **orphans open runs by design** (CONTEXT.md "Engine Identity"). Do not try to work around that.

## Git workflow

- Branch: `advisor/007-drift-d8-file-evidence` off current `main` (per CONTRIBUTING.md: short-lived branch, PR to `main`, squash merge by a human).
- Commit message style (match git log): `fix: ...` / `refactor: ...` conventional prefixes, e.g. `refactor: read drift evidence through the D8 runner protocol`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Batch the local blob hashing

In `edge_deploy/drift.py`, rewrite the loop body of `local_runtime_map` to use
a single `git cat-file --batch` subprocess instead of one `git show` per path:

1. Keep the `git rev-parse --verify` guard and the call to
   `snapshot_runtime_paths` unchanged.
2. Start one process: `git cat-file --batch` with `cwd=root`, feed it
   `f"{commit}:{path}\n"` for every path on stdin (bytes), read stdout.
   Simplest robust shape: build the full input, call
   `subprocess.run(["git", "cat-file", "--batch"], input=..., capture_output=True, check=True, cwd=root)`,
   then parse the concatenated output.
3. Parse each record: a header line `<sha> <type> <size>\n` (ASCII), followed
   by exactly `<size>` bytes of content, followed by one `\n`. Records appear
   in the same order as the requested paths. If a header line ends in
   `missing`, raise `subprocess.CalledProcessError`-equivalent or a clear
   `RuntimeError` naming the path (should be impossible since paths came from
   `ls-tree` of the same commit).
4. `mapping[path] = hashlib.md5(content).hexdigest()` exactly as before.

Do not change the function signature or the md5 algorithm (remote side and
recorded evidence use md5 — changing it breaks comparison).

**Verify**: `py -m pytest tests/test_drift.py -q` → all pass, including
`test_local_runtime_map_reads_snapshot_tree_not_working_tree` (this test
creates a real git repo in `tmp_path`, deletes a file from the working tree,
and asserts the snapshot's file is still hashed — it exercises your parser
end-to-end).

### Step 2: Write the remote scan result to a file and read it via D8

In `edge_deploy/drift.py`:

1. Change `remote_runtime_map(driver, repo_path, profile)` to
   `remote_runtime_map(driver, repo_path, profile, *, run_id: str)`.
2. In the embedded Python script, replace the three `print(...)` marker lines
   with writing the JSON to a file:

   ```python
   out = Path.home() / ".edge-deploy" / "runs" / {run_id!r} / "steps" / "drift-scan.json"
   out.parent.mkdir(parents=True, exist_ok=True)
   out.write_text(json.dumps(payload, sort_keys=True))
   ```

   (Use interpolation with `{run_id!r}` the same way the script already
   interpolates `{repo_path!r}` and `{profile.runtime_paths!r}`.)
3. Keep `_remote_python` (base64-piped script execution) — it is pane-safe for
   *sending*; only the *result read-back* was fragile. After it returns with
   exit code 0, read the result with:

   ```python
   from edge_deploy.runner import read_remote_json
   return read_remote_json(driver, f"~/.edge-deploy/runs/{run_id}/steps/drift-scan.json")
   ```

   Import at module top, not inline. `read_remote_json` raises
   `RunnerProtocolError` on wrap/digest problems — let it propagate.
4. Delete `_extract_payload` entirely (its only caller is gone) and the
   `DRIFT_PAYLOAD_START`/`END` literals.

**Verify**: `grep -rn "DRIFT_PAYLOAD\|_extract_payload" edge_deploy/` → no matches.

### Step 3: Thread `run_id` through the three callers

1. `check_drift` (`drift.py:153`): add keyword-only `run_id: str`, pass it to
   `remote_runtime_map`.
2. `verify_after_rollout` (`verify.py:40`): add keyword-only `run_id: str`,
   pass through to `check_drift`.
3. `release.py:650`: add `run_id=report_dir.name` to the
   `verify_after_rollout(...)` call (same value the rollout call at
   `release.py:599` already uses).
4. `cli.py:793` (`_cmd_drift`): pass `run_id="edge-deploy"` (the synthetic-id
   convention from `_cmd_rollout` at `cli.py:779`).

**Verify**: `grep -rn "remote_runtime_map\|check_drift\|verify_after_rollout" edge_deploy/ tests/ | grep -v "def \|conftest"` — every call site passes `run_id`.

### Step 4: Update the fake driver and tests

1. `tests/conftest.py:384-390`: the `DRIFT_PAYLOAD_START` branch in
   `FakeTmuxDriver` no longer matches anything. Replace it: when the decoded
   remote-python script contains `drift-scan.json`, record the scan and return
   success with empty output; then make the D8 file-read branch
   (`__EDGE_RESULT_START__` + `base64 -w0`, `conftest.py:364`) serve
   `json.dumps(self._remote_runtime, sort_keys=True)` when the requested path
   ends with `steps/drift-scan.json`. Follow the existing pattern used for
   `dependency-stage-evidence.json` (`conftest.py:317-319`).
2. `tests/test_drift.py:135-142`
   (`test_remote_runtime_map_parses_base64_payload`): update to pass a
   `run_id`, and change the assertion `driver.ran("base64 -d")` if the fake
   routing changed the observable command; also rename the test to reflect the
   new protocol (e.g. `test_remote_runtime_map_reads_d8_file_evidence`).
3. Add one new test: the D8 read raising `RunnerProtocolError` (e.g. fake
   returns a corrupt digest) propagates out of `remote_runtime_map` — this is
   the corruption-detection property the migration buys.
4. Update `tests/test_verify.py` / `tests/test_release.py` /
   `tests/test_phases_deploy.py` call sites only as needed for the new
   required kwarg (most stub `local_runtime_map` and use the fake driver; the
   compile-time change is adding `run_id`).

**Verify**: `py -m pytest tests/test_drift.py tests/test_verify.py tests/test_release.py tests/test_phases_deploy.py -q` → all pass.

### Step 5: Full suite and docs touch-up

1. Run the full suite.
2. Update the module docstring of `drift.py` and, in
   `docs/adr/0009-on-node-runner-file-evidence.md`, amend the "known
   remainder" note to record that drift now uses D8 file evidence (append a
   dated line; do not rewrite the ADR's decision history).

**Verify**: `py -m pytest -n 4 --dist loadfile` → all pass. `py -m ruff check .` → no new violations.

## Test plan

- Updated: `test_remote_runtime_map_*` (D8 file evidence happy path, per Step 4).
- New: `RunnerProtocolError` propagation on corrupt D8 read (Step 4.3).
- New (Step 1): no new test strictly required — the existing snapshot-tree test
  covers the batch parser — but add one test with a runtime file whose content
  contains `\n<sha> blob ` -like bytes to prove the size-based record parser
  doesn't get confused by content that looks like a header.
- Pattern to model after: `tests/test_dependencies.py:122`
  (`test_delivery_transfers_then_records_verified_remote_stage`) for
  fake-driver D8 evidence tests.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `py -m pytest -n 4 --dist loadfile` exits 0
- [ ] `grep -rn "DRIFT_PAYLOAD" edge_deploy/ tests/` → no matches in `edge_deploy/`; test matches only if asserting on legacy history (should be none)
- [ ] `grep -n "_extract_payload" edge_deploy/drift.py` → no matches
- [ ] `grep -c "git show" edge_deploy/drift.py` → 0 (docstring mentions allowed; adjust grep to code lines if needed)
- [ ] `py -m ruff check .` reports no violations in the files this plan touched
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The code at the "Current state" locations doesn't match the excerpts.
- `read_remote_json` cannot be reused without modifying `runner.py` (the plan
  assumes it works for arbitrary remote paths, as `dependencies.py:402` does).
- Threading `run_id` requires changing `rollout.py` or the phase modules —
  that means the call graph differs from the one mapped here.
- A step's verification fails twice after a reasonable fix attempt.
- You discover drift evidence files under `~/.edge-deploy/runs/<run_id>/steps/`
  colliding with runner step names (`drift-scan` must not collide with any
  `run_step` step name; at planning time step names in use are `update`,
  `install`, `dependency-stage`).

## Maintenance notes

- This change alters the engine content hash (`ledger.py:_content_sha256`), so
  any open run is orphaned when it lands — release operators must complete or
  abandon open runs before upgrading. This is by design (CONTEXT.md "Engine
  Identity"); note it in the PR description.
- Reviewers should scrutinize the `cat-file --batch` record parser (sizes are
  bytes, not characters — keep everything in `bytes` until the md5).
- Deferred follow-ups, deliberately out of scope: sharing the D8 marker
  constants between `runner.py` and `tmux_driver.py` (small DRY win); the
  `FakeTmuxDriver` `PERMISSION_PAYLOAD` branch at `conftest.py:377-383`
  appears to serve no production code path anymore and could be deleted in a
  test-cleanup pass.
- If a future plan replaces the pane transport (plans/006, Paramiko), drift is
  then already file-evidence based and needs no further migration.
