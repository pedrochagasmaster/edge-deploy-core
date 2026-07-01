# Development and Release Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate GitHub-based contribution from operator-only release work across edge-deploy-core, Autobench, and Robocop, with exact-SHA releases and centralized Bitbucket audit records.

**Architecture:** GitHub remains the reviewed source of truth. `edge-deploy-core` adds repository-state and audit boundaries around the existing release engine, while each tool exposes separate `dev` and pinned `release` extras. Documentation is reduced to one contributor path and one operator path per repository.

**Tech Stack:** Python 3.10/3.12, setuptools/pyproject.toml, pytest, Git, GitHub Actions, Bitbucket, YAML.

---

### Task 1: Core contributor baseline

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/pull_request_template.md`
- Create: `AGENTS.md`
- Create: `CONTRIBUTING.md`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Delete: `docs/PHASE2-IMPLEMENTATION-PLAN.md`

- [ ] **Step 1: Add a Python 3.10/3.12 CI matrix**

Create `.github/workflows/ci.yml` with checkout, `python -m pip install -e ".[dev]"`,
and `python -m pytest` on pull requests and pushes to `main`.

- [ ] **Step 2: Add the shared PR handoff**

Create `.github/pull_request_template.md`:

```markdown
## Summary

## Validation

`python -m pytest`:

## Risk or release impact

- [ ] No secrets or generated release reports are included.
```

- [ ] **Step 3: Set package version and contributor metadata**

Set `project.version = "1.0.0"` and retain only normal test/lint packages in
`project.optional-dependencies.dev`.

- [ ] **Step 4: Write role-specific core documentation**

`CONTRIBUTING.md` ends at a GitHub pull request and defines Contributor and
Maintainer responsibilities. `AGENTS.md` permits branch, commit, push, and PR
creation only when requested; it prohibits merge, tag, Bitbucket, and deploy
actions without explicit Release Operator instruction. `README.md` owns generic
release behavior and links to `docs/release-workflow.md`.

- [ ] **Step 5: Remove the completed implementation plan**

Delete `docs/PHASE2-IMPLEMENTATION-PLAN.md`; its implemented behavior remains
covered by tests, design docs, and ADRs.

- [ ] **Step 6: Validate**

Run:

```text
python -m pytest
```

Expected: all core tests pass.

- [ ] **Step 7: Commit**

```text
git add .github AGENTS.md CONTRIBUTING.md README.md pyproject.toml docs
git commit -m "docs: separate core contribution and release"
```

### Task 2: Exact-source repository gate

**Files:**
- Create: `edge_deploy/repository.py`
- Create: `tests/test_repository.py`
- Modify: `edge_deploy/publish.py`
- Modify: `tests/test_publish.py`

- [ ] **Step 1: Write failing repository-state tests**

Cover clean `main`, dirty worktree, non-`main` branch, local/origin divergence,
wrong remote URL, unsuccessful GitHub CI, and non-fast-forward Bitbucket
history.

The public interface is:

```python
@dataclass(frozen=True)
class RepositoryState:
    root: Path
    tool: str
    commit: str
    origin_url: str
    bitbucket_url: str

def inspect_repository(root: Path, *, expected_origin: str, expected_bitbucket: str) -> RepositoryState:
    ...

def require_successful_github_ci(state: RepositoryState, *, runner: CommandRunner | None = None) -> None:
    ...
```

- [ ] **Step 2: Verify the tests fail**

Run:

```text
python -m pytest tests/test_repository.py -q
```

Expected: import failure because `edge_deploy.repository` does not exist.

- [ ] **Step 3: Implement repository inspection**

Use non-mutating Git commands to require branch `main`, an empty porcelain
status, `HEAD == refs/remotes/origin/main`, and normalized expected remote URLs.
Use `gh run list --commit <sha> --branch main --workflow CI --json conclusion`
to require a successful exact-commit run.

- [ ] **Step 4: Replace rewritten snapshots with exact-SHA Publish**

`publish_snapshot` pushes `<source_sha>:refs/heads/main` without `commit-tree`.
It first verifies that `bitbucket/main` is an ancestor of the source SHA and
refuses otherwise. Remove `reparent_snapshot` and snapshot-message behavior.

The first release after this change may refuse because existing Bitbucket
snapshots have rewritten history. Preserve the old Bitbucket tip with a backup
tag and align `bitbucket/main` only as an explicit one-time Release Operator
migration; code never force-pushes automatically.

- [ ] **Step 5: Run focused tests**

Run:

```text
python -m pytest tests/test_repository.py tests/test_publish.py -q
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```text
git add edge_deploy/repository.py edge_deploy/publish.py tests/test_repository.py tests/test_publish.py
git commit -m "feat: enforce exact-source release state"
```

### Task 3: Centralized audit branch

**Files:**
- Create: `edge_deploy/audit.py`
- Create: `tests/test_audit.py`
- Modify: `edge_deploy/reporting.py`
- Modify: `tests/test_reporting.py`

- [ ] **Step 1: Write failing audit tests**

Cover path generation, recursive redaction, isolated temporary worktree use,
explicit `bitbucket` push to `release-log`, append-only collision refusal, retry
linkage, and local outbox preservation after push failure.

The boundary is:

```python
@dataclass(frozen=True)
class AuditAttempt:
    tool: str
    source_sha: str
    started_at: datetime
    report_dir: Path
    core_version: str
    operator: str
    linked_attempt: str | None = None

def check_audit_remote(core_repo: Path, *, runner: CommandRunner | None = None) -> None:
    ...

def append_audit_attempt(
    core_repo: Path,
    attempt: AuditAttempt,
    *,
    runner: CommandRunner | None = None,
) -> str:
    ...
```

- [ ] **Step 2: Verify the tests fail**

Run:

```text
python -m pytest tests/test_audit.py -q
```

Expected: import failure because `edge_deploy.audit` does not exist.

- [ ] **Step 3: Implement append-only audit writes**

Fetch only `refs/heads/release-log` from `bitbucket`. Create an orphan temporary
worktree when the branch is absent. Copy the already-redacted report bundle to
`releases/<tool>/<YYYY>/<MM>/<timestamp>-<short-sha>/`, add metadata JSON, commit,
and push `HEAD:refs/heads/release-log` explicitly to `bitbucket`. Refuse an
existing destination. Always remove the temporary worktree.

- [ ] **Step 4: Preserve failed pushes**

On final push failure, copy the audit bundle to
`%APPDATA%\edge-deploy\outbox\<attempt-id>` and raise `AuditSyncError`. A
non-empty outbox makes `check_audit_remote` fail until synchronized.

- [ ] **Step 5: Run focused tests**

Run:

```text
python -m pytest tests/test_audit.py tests/test_reporting.py -q
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```text
git add edge_deploy/audit.py edge_deploy/reporting.py tests/test_audit.py tests/test_reporting.py
git commit -m "feat: centralize release audit records"
```

### Task 4: One-checkout release command

**Files:**
- Modify: `edge_deploy/config.py`
- Modify: `edge_deploy/cli.py`
- Modify: `edge_deploy/release.py`
- Modify: `edge_deploy/progress.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_release.py`
- Modify: `config.example.yaml`

- [ ] **Step 1: Write failing CLI and config tests**

Require `%APPDATA%\edge-deploy\config.yaml` by default, infer the tool profile
from the current checkout, accept no `release --tool` or multi-tool mode, and
reject fixed local tool paths in operator configuration.

- [ ] **Step 2: Verify the tests fail**

Run:

```text
python -m pytest tests/test_config.py tests/test_cli.py tests/test_release.py -q
```

Expected: failures for the old path-based, multi-tool contract.

- [ ] **Step 3: Simplify operator configuration**

`OperatorConfig` contains operator identity, node inventory, expected remotes,
and the local core repository used for audit. It contains no tool checkout
paths. `load_tool_profile(Path.cwd())` identifies the released tool.

- [ ] **Step 4: Apply hard preflight gates**

Before Publish, run repository inspection, exact-commit GitHub CI verification,
`python -m pytest`, audit reachability, network preflight, and interactive auth.
No Publish mutation may occur before all gates pass.

- [ ] **Step 5: Release and audit one tool**

Run the existing per-node rollout for the inferred tool. Write the local report
bundle, append every success/failure attempt to `release-log`, and create
`release-<UTC>-<shortsha>` on both `origin` and `bitbucket` only after all
verification succeeds.

- [ ] **Step 6: Enforce incomplete-release blocking**

An incomplete attempt for a SHA may be resumed with `--resume`. A different SHA
is rejected while the audit log or local outbox records an unresolved attempt.
Rollback is explicit and targets a prior successful release tag without moving
Bitbucket `main`.

- [ ] **Step 7: Update the example**

`config.example.yaml` points to `%APPDATA%\edge-deploy\config.yaml`, contains no
real user, hostname, email, or `D:\Projects` path, and documents expected
GitHub/Bitbucket URLs by tool.

- [ ] **Step 8: Run the full core suite**

Run:

```text
python -m pytest
```

Expected: all core tests pass.

- [ ] **Step 9: Commit**

```text
git add edge_deploy config.example.yaml tests
git commit -m "feat: make releases checkout-driven and auditable"
```

### Task 5: Autobench contributor package and documentation

**Files:**
- Modify: `D:\Projects\autobench\pyproject.toml`
- Modify: `D:\Projects\autobench\.github\workflows\ci.yml`
- Create: `D:\Projects\autobench\.github\pull_request_template.md`
- Modify: `D:\Projects\autobench\README.md`
- Modify: `D:\Projects\autobench\CONTRIBUTING.md`
- Modify: `D:\Projects\autobench\AGENTS.md`
- Create: `D:\Projects\autobench\docs\release-workflow.md`
- Delete: `D:\Projects\autobench\docs\development-workflow.md`
- Delete: `D:\Projects\autobench\requirements-dev.txt`
- Modify: `D:\Projects\autobench\scripts\cloud_install.sh`
- Modify: `D:\Projects\autobench\tools\prod_tui\harness.py`
- Modify: `D:\Projects\autobench\update.sh`
- Modify: `D:\Projects\autobench\tests\test_production_scripts.py`
- Delete: `D:\Projects\autobench\.agents\skills\autobench-edge-deploy\`
- Delete: `D:\Projects\autobench\.gemini\skills\code-simplifier\`
- Delete: `D:\Projects\autobench\.github\agents\code-simplifier.agent.md`
- Delete: `D:\Projects\autobench\.cursor\skills\README.md`

- [ ] **Step 1: Consolidate packaging metadata**

Move runtime dependencies from `requirements.txt` and developer tools from
`requirements-dev.txt` into:

```toml
dependencies = [
  "pandas>=2.0,<3",
  "numpy>=1.24,<3",
  "openpyxl>=3.1,<4",
  "PyYAML>=6.0,<7",
  "scipy>=1.10,<2",
  "textual>=0.40.0,<7",
]

[project.optional-dependencies]
dev = ["pytest>=7,<9", "ruff>=0.4,<1", "mypy>=1.8,<2"]
release = [
  "edge-deploy-core @ git+https://github.com/pedrochagasmaster/edge-deploy-core.git@v1.0.0"
]
```

Retain `requirements.txt`, `constraints.txt`, and the generated Linux
constraints because the offline production installer reads them. Document them
as production bundle inputs, not contributor setup paths.

- [ ] **Step 2: Align CI**

Install `.[dev]` and run the canonical `python -m pytest`. Keep existing Ruff
and gate checks after the canonical test command.

- [ ] **Step 3: Replace mixed workflow docs**

`CONTRIBUTING.md` covers local/Cursor setup, branching, validation, and PR
handoff only. `docs/release-workflow.md` covers `.[dev,release]`, exact-main
verification, local pytest, and `python -m edge_deploy release`. `README.md`
links to both. `AGENTS.md` states agent authority boundaries.

- [ ] **Step 4: Remove duplicate agent and deployment guidance**

Delete the superseded deployment skill, duplicate simplifier instructions, and
old combined development/release guide. Remove `requirements-dev.txt` from
`scripts/cloud_install.sh`, production-harness runtime names, `update.sh`, and
their tests; contributor tooling now comes only from `.[dev]`.

- [ ] **Step 5: Add the PR template**

Use the same four-field template as Task 1.

- [ ] **Step 6: Validate without the release extra**

Run:

```text
python -m pip install -e ".[dev]"
python -m pytest
```

Expected: installation does not contact corporate Bitbucket and all tests pass.

- [ ] **Step 7: Commit**

```text
git add -A
git commit -m "docs: separate Autobench contribution and release"
```

### Task 6: Robocop contributor package and documentation

**Files:**
- Modify: `D:\Projects\robocop\pyproject.toml`
- Modify: `D:\Projects\robocop\.github\workflows\ci.yml`
- Create: `D:\Projects\robocop\.github\pull_request_template.md`
- Modify: `D:\Projects\robocop\README.md`
- Modify: `D:\Projects\robocop\CONTRIBUTING.md`
- Modify: `D:\Projects\robocop\AGENTS.md`
- Create: `D:\Projects\robocop\docs\release-workflow.md`
- Delete: `D:\Projects\robocop\docs\development-workflow.md`
- Delete: `D:\Projects\robocop\.agents\skills\dispatch-edge-deploy\`

- [ ] **Step 1: Split dependency extras**

Remove core from `dev` and add:

```toml
release = [
  "edge-deploy-core @ git+https://github.com/pedrochagasmaster/edge-deploy-core.git@v1.0.0"
]
```

- [ ] **Step 2: Make pytest canonical in CI**

Retain the 3.10/3.12 matrix and existing compile/lint/type/smoke checks. The
required test command is `python -m pytest`; pytest configuration supplies both
`tests` and `tools/prod_tui/tests`.

- [ ] **Step 3: Replace mixed workflow docs**

Apply the same role split and agent boundaries as Autobench, retaining only
Robocop-specific local mock setup and release prerequisites.

- [ ] **Step 4: Remove duplicate deployment guidance**

Delete the superseded release skill and combined development/release guide.
Keep the Textual development skill because it describes product development,
not operator release.

- [ ] **Step 5: Add the PR template**

Use the same four-field template as Task 1.

- [ ] **Step 6: Validate without the release extra**

Run:

```text
python -m pip install -e ".[dev]"
python -m pytest
```

Expected: installation does not contact corporate Bitbucket and all tests pass.

- [ ] **Step 7: Commit**

```text
git add -A
git commit -m "docs: separate Dispatch contribution and release"
```

### Task 7: Cross-repository stale-reference audit

**Files:**
- Modify only files identified by the searches below.

- [ ] **Step 1: Search obsolete setup and release surfaces**

Run:

```text
rg -n "requirements-dev|edge-deploy-core.*scm\.mastercard|@main|development-workflow|--tool both|--tool autobench|--tool robocop|D:\\Projects|normal development release|release handoff" D:\Projects\edge-deploy-core D:\Projects\autobench D:\Projects\robocop
```

Expected: no active documentation or dependency declaration uses an obsolete
surface. Historical ADR text may remain only when explicitly labeled as
historical.

- [ ] **Step 2: Search role and authority language**

Run:

```text
rg -n "Contributor|Maintainer|Release Operator|GitHub.*main|Bitbucket.*release" README.md CONTRIBUTING.md AGENTS.md docs
```

Expected: every repository exposes the three roles and the GitHub-to-Bitbucket
direction without contradictory commands.

- [ ] **Step 3: Run all suites**

Run `python -m pytest` in each repository.

Expected: all three suites pass.

- [ ] **Step 4: Check clean scoped diffs**

Run `git status --short --branch` and `git diff --check` in each repository.

Expected: only intended workflow changes are present and no whitespace errors
exist.

### Task 8: Deferred external setup

**Files:** none.

- [ ] **Step 1: Retry the deferred core GitHub push**

Run:

```text
git push --set-upstream origin main
```

Expected: GitHub `main` receives the local history. If HTTP 503 persists, record
the operation as deferred without changing local work.

- [ ] **Step 2: Configure GitHub repository controls**

After pushes are available, require PRs, one human approval, required CI, linear
history, and branch deletion for `main` in all three repositories.

- [ ] **Step 3: Tag core v1.0.0**

Only after core GitHub CI succeeds for exact `main`, create immutable `v1.0.0`
and push it to GitHub and Bitbucket.

- [ ] **Step 4: Perform the one-time Bitbucket history migration**

For each tool, preserve the legacy Bitbucket main tip with an immutable backup
tag. After explicit Release Operator review, align Bitbucket `main` to canonical
GitHub `main` once. Thereafter all code paths require fast-forward-only pushes.

- [ ] **Step 5: Initialize the audit branch**

Allow the first audited release to create the orphan `release-log` branch in
the existing edge-deploy-core Bitbucket repository. Confirm no GitHub ref named
`release-log` exists.
