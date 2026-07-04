# Plan 002: Stop pane line-wrap from crashing dependency stage-evidence parsing

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 4ad2b28..HEAD -- edge_deploy/dependencies.py tests/test_dependencies.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `4ad2b28`, 2026-07-02
- **Absorbed**: superseded by PR-14/16 deleting screen parsing (D8 wrap-immune runner protocol replaces dependency stage-evidence parsing)

## Why this matters

The remote dependency stage script prints a single JSON line between
`DEPENDENCY_STAGE_START`/`DEPENDENCY_STAGE_END` markers. That line contains a
content-addressed path and two 64-hex digests, so it is far wider than the tmux pane
(120 columns, `edge_deploy/tmux_driver.py:116`). The pane is captured with
`tmux capture-pane -p` **without `-J`** (`tmux_driver.py:446-450`), so a wrapped line
comes back as two physical lines with a newline inside the JSON string value.
`_parse_stage_evidence` feeds that span straight to `json.loads`, which raises
`json.JSONDecodeError` (a `ValueError`). No caller catches `ValueError`:
`run_rollout` catches only `BundleError` (`edge_deploy/rollout.py:545-547`) and the
release fan-out catches `(RuntimeError, SessionGoneError, AuthenticationError)`
(`edge_deploy/release.py:642`), so one wrapped line aborts the **entire release** for
all remaining nodes and tools. Sibling parsers already solved this exact problem.

## Current state

- `edge_deploy/dependencies.py:392-396` — the buggy parser:

  ```python
  def _parse_stage_evidence(screen: str) -> dict[str, object] | None:
      match = re.search(r"DEPENDENCY_STAGE_START\s*(\{.*?\})\s*DEPENDENCY_STAGE_END", screen, re.DOTALL)
      if not match:
          return None
      return json.loads(match.group(1))
  ```

- `edge_deploy/dependencies.py:323-326` — what the remote emits (inside `_stage_script`):

  ```python
  def emit(reused):
      print("DEPENDENCY_STAGE_START")
      print(json.dumps({{"remote_dir": str(final), "reused": reused, "bundle_digest": expected_digest}}))
      print("DEPENDENCY_STAGE_END")
  ```

  `final` is `/ads_storage/$USER/.edge-deploy/bundles/<tool>/<64-hex-digest>` and
  `expected_digest` is another 64-hex string — the JSON line is ~180+ characters.

- The repo's existing convention for exactly this problem — `edge_deploy/drift.py:108`
  (and the same pattern at `edge_deploy/rollout.py:449-451`): payloads are rejoined with
  `"".join(payload.splitlines())` before `json.loads`, because capture-pane wraps long
  lines. **Match this pattern.**
- Callers of `_parse_stage_evidence`: `deliver_dependency_bundle` at
  `edge_deploy/dependencies.py:443` (reuse path) and `:459` (fresh-stage path). Both
  already raise `BundleError` when the parser returns `None`, and `BundleError` is
  defined at `dependencies.py:28` as `class BundleError(RuntimeError)`.
- Existing tests fabricate the evidence in one unwrapped line — see the `Driver` doubles
  in `tests/test_dependencies.py` (around lines 179-242), which respond to the stage
  command with `DEPENDENCY_STAGE_START\n{...}\nDEPENDENCY_STAGE_END`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `python -m pip install -e ".[dev]"` | exit 0 |
| Focused tests | `python -m pytest tests/test_dependencies.py -ra` | all pass |
| Full suite | `python -m pytest` | all pass |
| Lint | `python -m ruff check .` | exit 0 |

## Scope

**In scope**:
- `edge_deploy/dependencies.py` — only `_parse_stage_evidence`
- `tests/test_dependencies.py` — new tests

**Out of scope**:
- `_stage_script` itself and the marker format (Plan 010 covers executing it in tests).
- `edge_deploy/tmux_driver.py` — do not add `-J` to `capture_screen`; other parsers
  depend on current capture semantics and Plan 013 audits that surface.
- `edge_deploy/drift.py`, `edge_deploy/rollout.py` — their parsers already rejoin.

## Git workflow

- Branch: `advisor/002-stage-evidence-linewrap`
- Commit style: `fix: rejoin wrapped stage-evidence JSON before parsing`
- Do NOT push or open a PR unless the operator asks.

## Steps

### Step 1: Rejoin wrapped lines and fail scoped, not global

Replace `_parse_stage_evidence` with:

```python
def _parse_stage_evidence(screen: str) -> dict[str, object] | None:
    match = re.search(r"DEPENDENCY_STAGE_START\s*(\{.*?\})\s*DEPENDENCY_STAGE_END", screen, re.DOTALL)
    if not match:
        return None
    payload = "".join(match.group(1).splitlines())
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise BundleError(f"unparseable dependency stage evidence: {exc}") from exc
```

Rationale to preserve in a short comment: capture-pane wraps lines wider than the pane;
newlines inside the marker span are wrap artifacts, never JSON content. Raising
`BundleError` (instead of letting `ValueError` escape) means `run_rollout`'s existing
`except BundleError` converts a garbled screen into a per-node `failed` report instead
of aborting the whole release.

**Verify**: `python -m pytest tests/test_dependencies.py -ra` → all existing tests pass.

### Step 2: Add regression tests

In `tests/test_dependencies.py`, following the style of the existing `_parse_stage_evidence`
/ `Driver`-double tests in that file, add:

1. `test_parse_stage_evidence_rejoins_wrapped_lines` — build a valid evidence JSON
   string for a digest-length `remote_dir`, insert `\n` mid-string-value (e.g. after
   character 120), wrap it in the markers, and assert the parsed dict has the full
   un-wrapped `remote_dir`.
2. `test_parse_stage_evidence_raises_bundle_error_on_garbage` — markers present but the
   span is not valid JSON even after rejoining (e.g. `{"remote_dir": ...` truncated);
   assert `pytest.raises(BundleError)`.
3. `test_parse_stage_evidence_returns_none_without_markers` — no markers → `None`
   (protects the existing `None` contract used by `deliver_dependency_bundle`).

**Verify**: `python -m pytest tests/test_dependencies.py -ra` → all pass including 3 new tests.

## Test plan

Covered by Step 2. Full-suite check: `python -m pytest` → all pass.

## Done criteria

- [ ] `python -m pytest` exits 0; the 3 new tests exist and pass
- [ ] `python -m ruff check .` exits 0
- [ ] `grep -n "json.loads(match.group(1))" edge_deploy/dependencies.py` returns no matches
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- `_parse_stage_evidence` no longer matches the "Current state" excerpt (someone
  already changed it).
- Removing newlines breaks an existing test that embeds a *legitimate* newline inside
  the JSON payload — that would mean the marker contract differs from this plan's
  understanding.
- You find callers of `_parse_stage_evidence` other than the two in
  `deliver_dependency_bundle` (the `BundleError` escalation might not be safe there).

## Maintenance notes

- If the stage script ever emits multi-key or pretty-printed JSON, the
  rejoin-then-parse approach still works (JSON ignores inter-token whitespace; only
  in-string newlines are wrap artifacts) — but string values containing *real* newlines
  would break; keep the evidence payload single-line.
- Plan 010 makes the stage script itself executable under test; when it lands, add one
  end-to-end case asserting the emitted line parses through this function.
- Reviewer focus: confirm `BundleError` (not bare `ValueError`) reaches `run_rollout`
  so a garbled screen fails only that (tool × node) pair.
