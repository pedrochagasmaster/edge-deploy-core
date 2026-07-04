# Plan 003: Gate CI with ruff, parallel pytest, and honest failure messages

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 4ad2b28..HEAD -- .github/workflows/ci.yml edge_deploy/cli.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (composes with 001; see maintenance notes)
- **Category**: dx
- **Planned at**: commit `4ad2b28`, 2026-07-02

## Why this matters

Three cheap gaps in the merge gate: (1) ruff is configured in `pyproject.toml` but never
runs in CI, so lint/import-order drift can merge unchecked; (2) CI runs single-process
pytest even though `pytest-xdist` is a declared dev dependency, contributors are told to
use `-n 4 --dist loadfile` (`CONTRIBUTING.md:13`), and the release preflight itself runs
`-n 8 --dist loadfile` — so CI is slower than needed *and* never exercises the parallel
execution mode that actually gates releases; (3) when the release-preflight pytest run
fails, the operator sees an error naming a command that never ran (`-n 12` vs the real
`-n 8`), which wastes triage time and undermines trust in release logs.

## Current state

- `.github/workflows/ci.yml:16-22` — the whole job:

  ```yaml
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with:
            python-version: ${{ matrix.python-version }}
        - run: python -m pip install -e ".[dev]"
        - run: python -m pytest
  ```

- `pyproject.toml:23-28` — ruff is configured (`line-length = 120`, `target-version =
  "py310"`, `select = ["E", "F", "W", "I"]`) and `ruff>=0.4` is in the `dev` extra.
  `python -m ruff check .` passes clean today.
- `edge_deploy/cli.py:431-434` — the stale message:

  ```python
  pytest_command = [sys.executable, "-m", "pytest", "-n", "8", "--dist", "loadfile"]
  completed = subprocess.run(pytest_command, cwd=repo_root)
  if completed.returncode:
      raise RuntimeError("python -m pytest -n 12 --dist loadfile failed; release blocked")
  ```

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `python -m pip install -e ".[dev]"` | exit 0 |
| Lint | `python -m ruff check .` | exit 0 |
| Tests (as CI will run them) | `python -m pytest -n auto --dist loadfile` | all pass |
| YAML sanity | `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` | exit 0 |

## Scope

**In scope**:
- `.github/workflows/ci.yml`
- `edge_deploy/cli.py` — only the error message at line 434
- `tests/test_cli.py` — one new/updated assertion if a test already pins that message

**Out of scope**:
- `pyproject.toml` (Plan 001 owns `addopts`/`pytest-cov`; do not add them here).
- `CONTRIBUTING.md` worker counts — `-n 4` there is a local suggestion, not a defect.
- Adding mypy to CI (Plan 012).

## Git workflow

- Branch: `advisor/003-ci-gates`
- Commit style: `fix: lint + parallel pytest in CI; truthful preflight failure message`
- Do NOT push or open a PR unless the operator asks.

## Steps

### Step 1: Add ruff and xdist to the CI job

Edit `.github/workflows/ci.yml` steps to:

```yaml
      - run: python -m pip install -e ".[dev]"
      - run: python -m ruff check .
      - run: python -m pytest -n auto --dist loadfile
```

`--dist loadfile` matters: it is the distribution mode the release preflight uses, so
CI now exercises the same test-isolation assumptions.

**Verify**: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
→ exit 0, and `python -m ruff check . && python -m pytest -n auto --dist loadfile` → all pass locally.

### Step 2: Make the preflight failure message report the real command

In `edge_deploy/cli.py`, replace the hardcoded string so the message derives from
`pytest_command` (single source of truth):

```python
if completed.returncode:
    raise RuntimeError(f"{' '.join(pytest_command)} failed; release blocked")
```

Note the first element is `sys.executable` (a full interpreter path) — that is fine and
more honest than the previous literal.

**Verify**: `grep -n '"-n", "8"' edge_deploy/cli.py` still matches;
`grep -rn "n 12" edge_deploy/` → no matches.

### Step 3: Check for a pinned-message test

`grep -rn "n 12\|release blocked" tests/` — if a test asserts the old literal, update it
to assert the new derived message (match on the `"failed; release blocked"` suffix, not
the interpreter path).

**Verify**: `python -m pytest tests/test_cli.py -ra` → all pass.

## Test plan

- Updated assertion from Step 3 (if any). No other new tests — the CI change is
  verified by running the exact commands locally and by the first CI run on the branch.

## Done criteria

- [ ] `python -m ruff check .` exits 0
- [ ] `python -m pytest -n auto --dist loadfile` exits 0
- [ ] `.github/workflows/ci.yml` contains a `ruff check` step and `-n auto --dist loadfile`
- [ ] `grep -rn "n 12" edge_deploy/ tests/` returns no matches
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- `python -m pytest -n auto --dist loadfile` fails locally while plain
  `python -m pytest` passes — that is an xdist isolation bug that must be reported as a
  finding, not silently worked around by dropping `-n`.
- `python -m ruff check .` reports violations (the audit found it clean at `4ad2b28`;
  violations mean drift — fix only if trivial mechanical, otherwise report).

## Maintenance notes

- After Plan 001 lands (pytest-cov in dev extras), extend the CI pytest step with
  `--cov=edge_deploy --cov-report=term-missing:skip-covered` so the money-path coverage
  is visible in every run.
- Plan 012 adds a mypy step next to the ruff step.
- Reviewer focus: confirm the matrix (3.10/3.12) is untouched and `fail-fast: false` retained.
