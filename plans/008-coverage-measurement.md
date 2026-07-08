# Plan 008: Measure test coverage and gate CI on a recorded baseline

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d8ec786..HEAD -- .github/workflows/ci.yml pyproject.toml`
> If either file changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch,
> treat it as a STOP condition. **Exception**: if plan 003 (CI gates) already
> landed, the ci.yml excerpt below will differ by a ruff step, parallel pytest
> flags, and possibly pip caching — that exact difference is expected;
> integrate with it rather than stopping.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW (tooling only; no production code changes)
- **Depends on**: plans/003-ci-gates.md (both edit `.github/workflows/ci.yml`; land 003 first to avoid conflicts — if 003 is abandoned, this plan may proceed alone)
- **Category**: tests
- **Planned at**: commit `d8ec786`, 2026-07-07

## Why this matters

The suite is strong (369 tests, zero skips) but coverage is invisible: no
`pytest-cov`, no `[tool.coverage]` config, no CI report. A deep audit
(2026-07-07) found the real gaps by reading code and tests side by side —
dependency-delivery error paths, ledger retry logic — which is exactly the
work a coverage report automates. Without a measured baseline, future
regressions (a new module landing untested) are undetectable until they hurt.
This repo is a release engine for production Edge Nodes; untested error paths
are the ones that fire mid-release.

## Current state

- `pyproject.toml:14-15` — dev extras today:

  ```toml
  [project.optional-dependencies]
  dev = ["pytest>=7", "pytest-xdist>=3.6", "ruff>=0.4"]
  ```

  There is no `[tool.coverage]` section and no `.coveragerc` anywhere.

- `.github/workflows/ci.yml` (complete file at planning time):

  ```yaml
  name: CI
  on:
    push:
      branches: [main]
    pull_request:
      branches: [main]
  jobs:
    test:
      runs-on: ubuntu-latest
      strategy:
        fail-fast: false
        matrix:
          python-version: ["3.10", "3.12"]
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with:
            python-version: ${{ matrix.python-version }}
        - run: python -m pip install -e ".[dev]"
        - run: python -m pytest
  ```

- Convention note: contributors run `python -m pytest -n 4 --dist loadfile`
  (CONTRIBUTING.md:13). `pytest-cov` composes with `pytest-xdist` out of the
  box (coverage is combined across workers automatically).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `py -m pip install -e ".[dev]"` | exit 0 |
| Coverage run | `py -m pytest -n 4 --dist loadfile --cov=edge_deploy --cov-report=term-missing` | all pass + coverage table |
| Lint | `py -m ruff check .` | no new violations |

(Windows controller: use `py`; CI/Linux uses `python`.)

## Scope

**In scope** (the only files you should modify):
- `pyproject.toml` (add `pytest-cov` to dev extras; add `[tool.coverage.*]` config)
- `.github/workflows/ci.yml` (add coverage flags to the pytest step)

**Out of scope**:
- Any file under `edge_deploy/` or `tests/` — this plan measures; it does not
  add tests (plan 009 does that).
- Coverage upload services (Codecov etc.) — this is a corporate/proprietary
  repo; keep coverage local to the CI log.
- Per-module thresholds — a single global floor only, for now (see step 3).

## Git workflow

- Branch: `advisor/008-coverage-measurement` off `main`; PR per CONTRIBUTING.md.
- Commit style: `chore: measure and gate test coverage in CI` (match `git log` conventional prefixes).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add pytest-cov and coverage config

1. In `pyproject.toml`, change the dev extras line to include
   `"pytest-cov>=5"`.
2. Append:

   ```toml
   [tool.coverage.run]
   source = ["edge_deploy"]
   branch = true

   [tool.coverage.report]
   show_missing = true
   skip_covered = true
   ```

**Verify**: `py -m pip install -e ".[dev]"` → exit 0, pytest-cov installed
(`py -m pip show pytest-cov` → found).

### Step 2: Measure the baseline

Run: `py -m pytest -n 4 --dist loadfile --cov=edge_deploy --cov-report=term-missing`

Record the TOTAL percentage printed at the bottom of the coverage table. Write
it into the PR description and into this plan file's status section as
`Baseline: NN% (branch) at <today's date>`.

**Verify**: command exits 0 and prints a `TOTAL ... NN%` row.

### Step 3: Gate CI five points below the measured baseline

The gate exists to catch *regressions*, not to force a number up. Set
`fail_under` to (baseline − 5), rounded down to an integer:

```toml
[tool.coverage.report]
show_missing = true
skip_covered = true
fail_under = <baseline minus 5>
```

Then update the pytest step in `.github/workflows/ci.yml` to:

```yaml
      - run: python -m pytest -n 4 --dist loadfile --cov=edge_deploy --cov-report=term-missing
```

If plan 003 already landed, edit its pytest step to append the two `--cov*`
flags rather than adding a second pytest run; keep 003's ruff step and flags
intact.

**Verify**: `py -m pytest -n 4 --dist loadfile --cov=edge_deploy` → exits 0
(the gate passes at the measured baseline).

### Step 4: Prove the gate fires

Temporarily set `fail_under = 99` in `pyproject.toml`, rerun the coverage
command, confirm pytest exits non-zero with a "Coverage failure" message, then
restore the real threshold. Do not commit the 99 value.

**Verify**: `git diff pyproject.toml` shows only the intended final config.

## Test plan

No new tests — this plan is verification infrastructure. The machine check is
the gate itself (step 4 proves it can fail).

## Done criteria

- [ ] `py -m pip install -e ".[dev]"` installs pytest-cov
- [ ] `py -m pytest -n 4 --dist loadfile --cov=edge_deploy` exits 0 locally
- [ ] `.github/workflows/ci.yml` pytest step includes `--cov=edge_deploy`
- [ ] `pyproject.toml` has `[tool.coverage.run]` with `branch = true` and a `fail_under` equal to measured baseline − 5
- [ ] Baseline percentage recorded in this plan file and the PR description
- [ ] No files outside the in-scope list modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- `pytest-cov` cannot be installed in this environment (corporate index
  restrictions) — report the pip error verbatim.
- Coverage combined across xdist workers reports an implausible number (e.g.
  under 30% — the suite is far too thorough for that; suspect a
  worker-combination problem and report rather than gating on a wrong number).
- Plan 003's ci.yml changes conflict in a way not covered by the integration
  note in step 3.

## Maintenance notes

- When plan 009 (error-path tests) lands, re-measure and raise `fail_under`
  to the new baseline − 5.
- The `skip_covered = true` report option keeps CI logs short; drop it locally
  when hunting gaps.
- Reviewers: check that the CI step change did not drop plan 003's ruff gate
  or parallel flags.
