# Plan 001: Make the money-path test suite run hermetically in CI

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 4ad2b28..HEAD -- tests/ pyproject.toml`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `4ad2b28`, 2026-07-02
- **Absorbed**: superseded by PR-16's fake-driver rework (hermetic money-path coverage delivered there)

## Why this matters

In CI (GitHub Actions, `.github/workflows/ci.yml`), roughly 60 of the ~215 tests
silently `pytest.skip` — **all of** `tests/test_release.py`, `tests/test_rollout.py`,
`tests/test_verify.py`, `tests/test_drift.py`, and `tests/test_scripts.py`, plus parts
of `tests/test_config.py` — because they load live Tool Profiles from sibling checkouts
(`../autobench/edge_deploy.yaml`, `../robocop/edge_deploy.yaml`) that CI never checks
out. A green CI build therefore never executes the release orchestrator
(`edge_deploy/release.py`), the rollout engine (`edge_deploy/rollout.py`), or
drift/verify — the exact code this package exists to run safely. Nothing surfaces the
skips (no `-ra`, no coverage tooling). After this plan, those engines run in CI against
committed fixture profiles, skips are visible in the log, and coverage is measurable.

## Current state

- `tests/conftest.py:257-262` — the skip guard every engine test funnels through:

  ```python
  def _load_real_profile(tool: str) -> ToolProfile:
      """Load a committed Tool Profile, skipping the test if the sibling repo is absent."""
      profile_path = PROJECTS_ROOT / tool / "edge_deploy.yaml"
      if not profile_path.exists():
          pytest.skip(f"real Tool Profile not found: {profile_path}")
      return ToolProfile.load(profile_path)
  ```

- `tests/test_release.py:23-38` — `PROJECTS_ROOT = Path(__file__).resolve().parents[2]`
  and `_operator()` skips the whole file when either sibling profile is missing, then
  builds `OperatorConfig(..., tools={"autobench": str(PROJECTS_ROOT / "autobench"), ...})`.
- `pyproject.toml` has **no** `[tool.pytest.ini_options]` section, so no `addopts` and
  skip reasons are invisible in CI logs. Dev extras (line 15):
  `dev = ["pytest>=7", "pytest-xdist>=3.6", "ruff>=0.4"]`.
- `ToolProfile` fields (from `edge_deploy/config.py:316-330`): `tool`, `repo_path`,
  `github_url`, `bitbucket_url`, `release_branch`, `runtime_paths`, `compile_targets`,
  `version_files`, `install_trigger_paths`, `dependency_paths`, `dependency_bundle`,
  `smoke`, `sensitive_paths`, `tui_chrome_regex`, `tui_exit`.
- Tests hardcode profile-derived values, e.g. `tests/test_release.py:134-135` asserts
  commands ran under `/ads_storage/autobench` and `/ads_storage/dispatch`, and
  `tests/test_release.py:497-498` references a smoke command `"<controlled Impala job>"`.
  The fixture profiles you create must reproduce every value the tests assert on.
- Domain vocabulary (from `CONTEXT.md`): a **Tool Profile** is "the tool's committed
  `edge_deploy.yaml` ... node-independent release metadata and no operator paths or
  credentials". Fixture profiles must also contain no credentials or operator paths.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `python -m pip install -e ".[dev]"` | exit 0 |
| Tests   | `python -m pytest -ra` | all pass; skip summary printed |
| One file | `python -m pytest tests/test_release.py -ra` | all pass, 0 skipped |
| Lint    | `python -m ruff check .` | exit 0 |

## Scope

**In scope** (the only files you should modify/create):
- `tests/fixtures/autobench/edge_deploy.yaml` (create)
- `tests/fixtures/robocop/edge_deploy.yaml` (create)
- `tests/conftest.py`
- `tests/test_release.py` (only the `PROJECTS_ROOT`/`_operator` wiring)
- `pyproject.toml` (add `[tool.pytest.ini_options]` with `addopts = "-ra"`; add
  `pytest-cov` to the `dev` extra)

**Out of scope** (do NOT touch):
- `edge_deploy/` — no production code changes in this plan.
- `.github/workflows/ci.yml` — CI flag changes are Plan 003.
- `tests/test_scripts.py` — it executes the sibling repos' real `update.sh`/`install.sh`
  (ADR-0004 interface tests); those scripts cannot be vendored here. It keeps its skip,
  which `-ra` will now make visible.
- The sibling repos themselves.

## Git workflow

- Branch off current `main`: `advisor/001-hermetic-money-path-tests`
- Commit style: repo uses conventional prefixes (`test:`, `fix:`, `docs:` — see
  `git log --oneline`). Suggested: `test: run money-path suite on committed fixture profiles`
- Do NOT push or open a PR unless the operator asks. Never push to Bitbucket or tags.

## Steps

### Step 1: Inventory every profile value the tests depend on

Run `grep -rn "real_profile\|load_profile\|_load_real_profile\|_operator()" tests/` and
read each consuming test. Collect every asserted profile-derived literal (repo paths,
smoke command strings, runtime/install-trigger/dependency path patterns, tui settings).
Write the inventory into the fixture YAML comments so future readers know which test
pins which value.

**Verify**: the inventory covers every test file that currently skips:
`python -m pytest tests/test_release.py tests/test_rollout.py tests/test_verify.py tests/test_drift.py -ra 2>&1 | tail -5`
→ note the current skip count; you will drive it to 0 (except documented real-git skips).

### Step 2: Create the two fixture profiles

Create `tests/fixtures/autobench/edge_deploy.yaml` and
`tests/fixtures/robocop/edge_deploy.yaml` containing exactly the fields
`ToolProfile.from_mapping` reads (list in "Current state") with the values from Step 1's
inventory (e.g. autobench `repo_path: /ads_storage/autobench`, robocop
`repo_path: /ads_storage/dispatch`). If the real sibling checkouts exist on this machine
(`../autobench/edge_deploy.yaml`), you may copy them as a starting point — but the
committed fixtures must contain no URLs/hosts you cannot verify are non-sensitive; keep
the real `github_url`/`bitbucket_url` shapes only if they already appear in committed
test assertions, otherwise use `https://github.com/example/<tool>.git` placeholders and
adjust nothing that tests don't assert.

**Verify**: `python -c "from edge_deploy.config import ToolProfile; p=ToolProfile.load('tests/fixtures/autobench/edge_deploy.yaml'); print(p.tool, p.repo_path)"`
→ prints `autobench /ads_storage/autobench` (analogous for robocop).

### Step 3: Point the loaders at the fixtures

In `tests/conftest.py`, change `_load_real_profile` to load
`Path(__file__).parent / "fixtures" / tool / "edge_deploy.yaml"` and remove the
`pytest.skip` (a missing fixture is now a hard failure — it's committed). In
`tests/test_release.py`, change `_operator()` to stop probing `PROJECTS_ROOT` and build
`tools={"autobench": str(FIXTURES / "autobench"), "robocop": str(FIXTURES / "robocop")}`.
Keep the fixture directory layout `<dir>/edge_deploy.yaml` because `OperatorConfig`
tool paths point at checkout roots, not YAML files.

**Verify**: `python -m pytest tests/test_release.py tests/test_rollout.py tests/test_verify.py tests/test_drift.py -ra`
→ all pass, **0 skipped** (if a handful of tests still skip for a different stated
reason — e.g. "requires real git" — list them in your report; there are two known ones
at `tests/test_publish.py:389` and `tests/test_rollout.py:341` which use temp repos and
should NOT skip).

### Step 4: Make skips visible and add coverage tooling

In `pyproject.toml` add:

```toml
[tool.pytest.ini_options]
addopts = "-ra"
```

and extend the dev extra to `["pytest>=7", "pytest-xdist>=3.6", "pytest-cov>=5", "ruff>=0.4"]`.

**Verify**: `python -m pip install -e ".[dev]" && python -m pytest --cov=edge_deploy --cov-report=term-missing:skip-covered 2>&1 | tail -30`
→ suite passes; coverage table prints; `edge_deploy/release.py` and
`edge_deploy/rollout.py` each report **>60%** line coverage (they were ~0% in CI before).

## Test plan

This plan is itself test infrastructure. New assertions to add:
- One sanity test in `tests/test_config.py`: loading each committed fixture profile
  yields a `ToolProfile` with non-empty `tool`, `repo_path`, `runtime_paths` (model
  after the existing profile-loading tests in that file).
- No test may import `PROJECTS_ROOT`-relative sibling paths afterward:
  `grep -rn "parents\[2\]" tests/` → only hits that remain are in `tests/test_scripts.py`.

## Done criteria

- [ ] `python -m pytest -ra` exits 0
- [ ] `python -m pytest tests/test_release.py tests/test_rollout.py tests/test_verify.py tests/test_drift.py -ra` shows 0 skipped
- [ ] `grep -rn "pytest.skip" tests/conftest.py` returns no matches
- [ ] `python -m pytest --cov=edge_deploy -q` prints coverage with release.py and rollout.py > 60%
- [ ] `python -m ruff check .` exits 0
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- A test asserts a profile value you cannot infer from the test source itself and the
  sibling repos are not available to consult — do not guess node paths or smoke strings.
- Driving skips to zero requires changing any assertion's *expected value* (that means
  the fixture diverged from what the test was written against).
- Any test starts failing that passed at Step 1 with siblings present — the fixtures
  changed behavior, not just location.
- The fix appears to require touching `edge_deploy/` production code.

## Maintenance notes

- Fixture profiles are now the contract the engine tests run against. When a real tool's
  `edge_deploy.yaml` gains a field, add it to the fixtures in the same PR that teaches
  `config.py` about it.
- Plan 003 (CI gates) should add `--cov` to the CI invocation once this lands.
- Plan 004 (resume fix) depends on this plan: its regression tests live in
  `tests/test_release.py`, which currently skips in CI.
- Deferred: making `tests/test_scripts.py` hermetic would require vendoring both tools'
  `update.sh`/`install.sh`, which belong to the tool repos (ADR-0004) — out of scope.
