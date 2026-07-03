# Release Authentication and Snapshot Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make release authentication single-owner and observable, make publication reuse an existing tree-equivalent Bitbucket snapshot, and deploy Autobench PR #35 snapshot `dd6907b77a94fcd85e97792b572caca3634c7a18` through a prompt-authenticated tmux controller.

**Architecture:** `_run_release_preflight` will remain a local repository, CI, test, and audit gate, while `run_release` becomes the only owner of node authentication and receives the CLI auth mode unchanged. `publish_snapshot` will compare the reviewed source tree with the fetched Bitbucket-tip tree and return the existing tip without a push when they match. The live release will run inside a dedicated tmux controller so `getpass` is visible and operator-controlled.

**Tech Stack:** Python 3.12, pytest/pytest-xdist, Git, PowerShell, tmux/psmux, OpenSSH, GitHub CLI.

---

## File structure

- `edge_deploy/publish.py`: add the tree-equivalent remote-tip reuse decision.
- `edge_deploy/cli.py`: remove node authentication from local preflight and forward the selected auth mode to release/rollback orchestration.
- `tests/test_publish.py`: prove equivalent trees reuse the exact remote snapshot and divergent trees retain current behavior.
- `tests/test_cli.py`: prove local preflight has no SSH side effects and release/rollback forward prompt mode.
- `tests/test_release.py`: prove reports and tracked auth state exist before the prompt and prompt auth occurs once per node.
- `docs/release-workflow.md`: document the dedicated tmux controller procedure.
- `docs/superpowers/specs/2026-07-02-release-auth-ownership-design.md`: approved design; no implementation edits expected.

### Task 1: Checkpoint inherited release-engine fixes

**Files:**
- Commit existing changes: `README.md`
- Commit existing changes: `docs/DESIGN.md`
- Commit existing changes: `docs/adr/0007-tree-equivalent-bitbucket-mirroring.md`
- Commit existing changes: `docs/release-workflow.md`
- Commit existing changes: `edge_deploy/audit.py`
- Commit existing changes: `edge_deploy/cli.py`
- Commit existing changes: `edge_deploy/dependencies.py`
- Commit existing changes: `edge_deploy/rollout.py`
- Commit existing changes: `tests/conftest.py`
- Commit existing changes: `tests/test_audit.py`
- Commit existing changes: `tests/test_cli.py`
- Commit existing changes: `tests/test_dependencies.py`
- Commit existing changes: `tests/test_rollout.py`
- Preserve untracked: `plans/`

- [ ] **Step 1: Review the inherited diff and confirm it is limited to release recovery**

Run:

```powershell
git diff --stat
git diff -- README.md docs/DESIGN.md docs/adr/0007-tree-equivalent-bitbucket-mirroring.md docs/release-workflow.md edge_deploy/audit.py edge_deploy/cli.py edge_deploy/dependencies.py edge_deploy/rollout.py tests/conftest.py tests/test_audit.py tests/test_cli.py tests/test_dependencies.py tests/test_rollout.py
```

Expected: changes cover audit-copy recovery, release/resume provenance, wrapped dependency-stage evidence, offline installer environment, tag-push handoff, and their tests. `plans/` remains untracked and excluded.

- [ ] **Step 2: Run the inherited changes' focused tests**

Run:

```powershell
py -m pytest tests/test_audit.py tests/test_cli.py tests/test_dependencies.py tests/test_rollout.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Stage only the reviewed inherited release files**

Run:

```powershell
git add -- README.md docs/DESIGN.md docs/adr/0007-tree-equivalent-bitbucket-mirroring.md docs/release-workflow.md edge_deploy/audit.py edge_deploy/cli.py edge_deploy/dependencies.py edge_deploy/rollout.py tests/conftest.py tests/test_audit.py tests/test_cli.py tests/test_dependencies.py tests/test_rollout.py
git diff --cached --check
git diff --cached --name-only
```

Expected: the cached file list contains exactly the 13 paths listed above; `plans/` is absent.

- [ ] **Step 4: Commit the inherited fixes**

Run:

```powershell
git commit -m "fix: harden release recovery paths"
```

Expected: one commit is created and `plans/` remains the only inherited untracked path.

### Task 2: Reuse a tree-equivalent Bitbucket deployment snapshot

**Files:**
- Modify: `tests/test_publish.py:24-82`
- Modify: `tests/test_publish.py:196-224`
- Modify: `edge_deploy/publish.py:264-330`

- [ ] **Step 1: Extend `FakeGit` and add the failing reuse test**

In `tests/test_publish.py`, add `source_tree` and `previous_tree` constructor parameters and retain them:

```python
class FakeGit:
    """A scriptable ``git_runner`` that records argv and returns canned stdout by content."""

    def __init__(
        self,
        *,
        status: str = "",
        branch: str = "main",
        source_commit: str = "a1b2c3d4e5f6a7b8",
        short: str = "a1b2c3d",
        previous: str = "0f0f0f0f0f0f",
        push_error: str | None = None,
        remote_after_push_failure: str | None = None,
        merge_base_fails: bool = False,
        previous_subject: str = "Deploy snapshot: autobench cafe123 on main (2026-06-29 23:00 UTC) [edge-deploy]",
        snapshot_commit: str = "d" * 40,
        source_tree: str = "1" * 40,
        previous_tree: str = "0" * 40,
    ) -> None:
        self.calls: list[list[str]] = []
        self.status = status
        self.branch = branch
        self.source_commit = source_commit
        self.short = short
        self.previous = previous
        self.push_error = push_error
        self.remote_after_push_failure = remote_after_push_failure
        self.merge_base_fails = merge_base_fails
        self.previous_subject = previous_subject
        self.snapshot_commit = snapshot_commit
        self.source_tree = source_tree
        self.previous_tree = previous_tree
```

Replace the `rev-parse --verify` branch in `FakeGit.__call__` with:

```python
        if args[:2] == ["rev-parse", "--verify"]:
            ref = args[2]
            if ref == f"{self.source_commit}^{{tree}}":
                return self.source_tree + "\n"
            if ref == "bitbucket/main^{tree}":
                return self.previous_tree + "\n"
            return (self.previous if "/" in ref else self.source_commit) + "\n"
```

Add this test after `test_publish_continues_existing_snapshot_chain_without_source_ancestry`:

```python
def test_publish_reuses_tree_equivalent_remote_snapshot_without_push() -> None:
    source = "aa6d9a5f0fa5481ad75b938022b6a78b50b14a38"
    snapshot = "dd6907b77a94fcd85e97792b572caca3634c7a18"
    shared_tree = "7" * 40
    git = FakeGit(
        source_commit=source,
        short=source[:7],
        previous=snapshot,
        merge_base_fails=True,
        source_tree=shared_tree,
        previous_tree=shared_tree,
    )

    result = publish_snapshot(
        AUTOBENCH,
        repo_root="/x",
        git_runner=git,
        run_local_check=False,
    )

    assert result.status == "published"
    assert result.source_commit == source
    assert result.snapshot == snapshot
    assert result.previous_remote_commit == snapshot
    assert result.message == (
        f"Reuse existing tree-equivalent snapshot {snapshot} "
        f"for reviewed source {source[:7]}"
    )
    assert not any("push" in call for call in git.calls)
    assert not any(call[:1] == ["commit-tree"] for call in git.calls)
    assert not any(call[:2] == ["merge-base", "--is-ancestor"] for call in git.calls)
```

- [ ] **Step 2: Run the new test and verify the red state**

Run:

```powershell
py -m pytest tests/test_publish.py::test_publish_reuses_tree_equivalent_remote_snapshot_without_push -q
```

Expected: FAIL because `publish_snapshot` continues into the synthetic snapshot path instead of returning `dd6907b...`.

- [ ] **Step 3: Implement the tree-equivalent early return**

In `edge_deploy/publish.py`, immediately after resolving `previous_remote_commit`, add:

```python
    source_tree = git(["rev-parse", "--verify", f"{source_commit}^{{tree}}"]).strip()
    previous_tree = git(
        ["rev-parse", "--verify", f"{remote}/{branch}^{{tree}}"]
    ).strip()
    if source_tree == previous_tree:
        message = (
            f"Reuse existing tree-equivalent snapshot {previous_remote_commit} "
            f"for reviewed source {source_short}"
        )
        return PublishResult(
            tool=profile.tool,
            status="published",
            snapshot=previous_remote_commit,
            source_commit=source_commit,
            source_short=source_short,
            branch=branch,
            previous_remote_commit=previous_remote_commit,
            message=message,
            gate=gate,
            local_check_output_tail=local_check_output_tail,
        )
```

Do not move the token, local-check, fetch, or reviewed-source resolution gates. Tree comparison occurs only after the authenticated fetch establishes the current remote tip.

- [ ] **Step 4: Run publish tests**

Run:

```powershell
py -m pytest tests/test_publish.py -q
```

Expected: all publish tests pass, including the existing divergent-tree synthetic snapshot tests.

- [ ] **Step 5: Commit snapshot reuse**

Run:

```powershell
git add -- edge_deploy/publish.py tests/test_publish.py
git diff --cached --check
git commit -m "fix: reuse tree-equivalent deployment snapshot"
```

Expected: one focused commit containing only publish implementation and tests.

### Task 3: Consolidate release authentication ownership

**Files:**
- Modify: `tests/test_cli.py:326-365`
- Modify: `tests/test_cli.py:657-700`
- Modify: `tests/test_release.py:376-450`
- Modify: `edge_deploy/cli.py:19-28`
- Modify: `edge_deploy/cli.py:174-222`
- Modify: `edge_deploy/cli.py:365-470`

- [ ] **Step 1: Add the failing local-only preflight test**

Add this test before the release command dispatch tests in `tests/test_cli.py`:

```python
def test_release_preflight_only_runs_local_gates(tmp_path, monkeypatch) -> None:
    source = "a" * 40
    state = SimpleNamespace(commit=source)
    profile = SimpleNamespace(
        tool="autobench",
        github_url="https://github.example/autobench.git",
        bitbucket_url="https://bitbucket.example/autobench.git",
    )
    operator = SimpleNamespace(audit_repo=str(tmp_path / "audit"))
    observed: dict[str, object] = {}

    def inspect(repo_root, **kwargs):
        observed["inspect"] = (repo_root, kwargs)
        return state

    def check_audit(audit_root, **kwargs):
        observed["audit"] = (audit_root, kwargs)

    def unexpected_driver(*args, **kwargs):
        pytest.fail("local release preflight must not create or authenticate a node driver")

    monkeypatch.setattr(cli, "inspect_repository", inspect)
    monkeypatch.setattr(cli, "require_successful_github_ci", lambda actual: observed.setdefault("ci", actual))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(cli, "check_audit_remote", check_audit)
    monkeypatch.setattr(cli.TmuxDriver, "from_node_and_profile", unexpected_driver)

    result = cli._run_release_preflight(
        operator,
        profile,
        tmp_path,
        release_sha=source,
    )

    assert result is state
    assert observed["ci"] is state
    assert observed["audit"] == (
        tmp_path / "audit",
        {
            "tool": "autobench",
            "source_sha": source,
            "allow_unresolved": False,
        },
    )
```

- [ ] **Step 2: Make release and rollback forwarding tests request prompt mode**

In `test_release_command_dispatches_and_writes_consolidated_report`, add `"--auth-mode", "prompt"` to the CLI arguments and replace the auth assertion with:

```python
    assert captured["auth_mode"] == "prompt"
```

In `test_rollback_seeds_publish_provenance_for_dependency_delivery`, capture the run-release auth mode:

```python
    def fake_run_release(operator, selection, *, report_dir, max_auth_attempts, **kwargs) -> ReleaseReport:
        captured["selection"] = selection
        captured["report_dir"] = report_dir
        captured["auth_mode"] = kwargs["auth_mode"]
        return ReleaseReport(
            selection={"tools": selection.tools},
            publishes=[],
            rollouts=[
                {
                    "tool": "autobench",
                    "node": "node03",
                    "status": "rolled_out",
                    "state_left": "",
                }
            ],
        )
```

Add `"--auth-mode", "prompt"` to that rollback invocation and add:

```python
    assert captured["auth_mode"] == "prompt"
```

- [ ] **Step 3: Run the CLI tests and verify the red state**

Run:

```powershell
py -m pytest tests/test_cli.py::test_release_preflight_only_runs_local_gates tests/test_cli.py::test_release_command_dispatches_and_writes_consolidated_report tests/test_cli.py::test_rollback_seeds_publish_provenance_for_dependency_delivery -q
```

Expected: FAIL because preflight still requires node/auth parameters and release/rollback still pass `auth_mode="pane"`.

- [ ] **Step 4: Remove authentication from `_run_release_preflight`**

In `edge_deploy/cli.py`, remove:

```python
from edge_deploy.auth import authenticate_node, authenticate_node_via_pane
```

Replace `_run_release_preflight` with:

```python
def _run_release_preflight(
    operator: OperatorConfig,
    profile,
    repo_root: Path,
    *,
    release_sha: str | None = None,
    allow_unresolved: bool = False,
):
    if not profile.github_url:
        raise RepositoryError("edge_deploy.yaml must define github_url")
    state = inspect_repository(
        repo_root,
        tool=profile.tool,
        expected_origin=profile.github_url,
        expected_bitbucket=profile.bitbucket_url,
    )
    require_successful_github_ci(state)
    pytest_command = [sys.executable, "-m", "pytest", "-n", "8", "--dist", "loadfile"]
    completed = subprocess.run(pytest_command, cwd=repo_root)
    if completed.returncode:
        raise RuntimeError("python -m pytest -n 8 --dist loadfile failed; release blocked")
    if not operator.audit_repo:
        raise AuditSyncError("operator config must define audit_repo")
    check_audit_remote(
        Path(operator.audit_repo),
        tool=profile.tool,
        source_sha=release_sha or state.commit,
        allow_unresolved=allow_unresolved,
    )
    return state
```

- [ ] **Step 5: Update release and rollback call sites**

Replace the release preflight call with:

```python
    state = _run_release_preflight(
        effective_operator,
        profile,
        repo_root,
        release_sha=release_sha,
    )
```

In the following `run_release` call, replace:

```python
        auth_mode="pane",
```

with:

```python
        auth_mode=args.auth_mode,
```

Replace the rollback preflight call with:

```python
    _run_release_preflight(
        effective_operator,
        profile,
        repo_root,
        release_sha=target,
        allow_unresolved=True,
    )
```

In the rollback `run_release` call, replace:

```python
        auth_mode="pane",
```

with:

```python
        auth_mode=args.auth_mode,
```

- [ ] **Step 6: Add report-before-prompt and once-per-node coverage**

Add this test after `test_release_pane_auth_mode_does_not_prompt_for_passcode` in `tests/test_release.py`:

```python
def test_release_prompt_auth_is_tracked_before_one_prompt_per_node(
    fake_tmux,
    tmp_path,
    patched_drift,
) -> None:
    operator = _operator()
    drivers: dict = {}
    prompts: list[str] = []

    def getpass_with_progress(prompt: str) -> str:
        prompts.append(prompt)
        assert (tmp_path / "release.log").is_file()
        progress = json.loads(
            (tmp_path / "release-progress.json").read_text(encoding="utf-8")
        )
        assert progress["active"]["phase"] == "auth"
        assert progress["active"]["node"] in {"node03", "node04"}
        return "12345678"

    report = run_release(
        operator,
        ReleaseSelection(
            tools=["autobench"],
            nodes=["node03", "node04"],
        ),
        report_dir=tmp_path,
        getpass_fn=getpass_with_progress,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    assert report.exit_code() == 0
    assert prompts == [
        "[node03] Enter RSA PASSCODE: ",
        "[node04] Enter RSA PASSCODE: ",
    ]
```

- [ ] **Step 7: Run authentication and CLI tests**

Run:

```powershell
py -m pytest tests/test_auth_seam.py tests/test_cli.py tests/test_release.py tests/test_progress.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit authentication ownership**

Run:

```powershell
git add -- edge_deploy/cli.py tests/test_cli.py tests/test_release.py
git diff --cached --check
git commit -m "fix: give release orchestration sole auth ownership"
```

Expected: one focused commit containing CLI/auth orchestration and regression tests.

### Task 4: Document the tmux-hosted prompt workflow

**Files:**
- Modify: `docs/release-workflow.md:31-39`

- [ ] **Step 1: Add the operator procedure**

Insert this section before `## Tool release tag finalization`:

````markdown
## Interactive Tool release authentication

Run `edge_deploy release` in a dedicated tmux controller whenever
`--auth-mode prompt` is selected. The prompt belongs to the controller process,
not the per-node SSH panes.

```powershell
tmux new-session -d -s edge-release-pr35 -c D:\Projects\autobench
$releaseCommand = '$env:PYTHONPATH=''D:\Projects\edge-deploy-core''; $env:EDGE_DEPLOY_SSH_MULTIPLEX=''0''; $stamp=(Get-Date).ToUniversalTime().ToString(''yyyyMMddTHHmmssZ''); $env:EDGE_DEPLOY_PR35_REPORT="D:\Projects\autobench\edge-deploy\reports\release-$stamp-pr35-localcore"; py -m edge_deploy release --auth-mode prompt --report-dir $env:EDGE_DEPLOY_PR35_REPORT'
tmux send-keys -t edge-release-pr35 -l $releaseCommand
tmux send-keys -t edge-release-pr35 Enter
tmux attach -t edge-release-pr35
```

Enter each current RSA PASSCODE only at the controller's
`[nodeNN] Enter RSA PASSCODE:` prompt. The value is forwarded transiently to
the corresponding node pane and must never be copied into logs, reports, shell
history, or configuration.
````

- [ ] **Step 2: Verify documentation formatting and wording**

Run:

```powershell
git diff --check -- docs/release-workflow.md
rg -n "Interactive Tool release authentication|--auth-mode prompt|edge-release-pr35|Enter RSA PASSCODE" docs/release-workflow.md
```

Expected: `git diff --check` exits 0 and all four workflow terms are found.

- [ ] **Step 3: Commit the workflow documentation**

Run:

```powershell
git add -- docs/release-workflow.md
git diff --cached --check
git commit -m "docs: run prompt-auth releases from tmux"
```

Expected: one documentation-only commit.

### Task 5: Full verification and GitHub pull request

**Files:**
- Verify: all tracked project files
- Preserve untracked: `plans/`

- [ ] **Step 1: Run the full test suite**

Run:

```powershell
py -m pytest -n 4 --dist loadfile
```

Expected: exit code 0 with no failed tests.

- [ ] **Step 2: Verify repository scope**

Run:

```powershell
git status --short --branch
git diff origin/main...HEAD --stat
git log --oneline --decorate origin/main..HEAD
```

Expected: only `plans/` remains untracked; no tracked implementation changes remain uncommitted. The branch contains the inherited release fixes, design commits, snapshot-reuse commit, auth-ownership commit, and workflow documentation commit.

- [ ] **Step 3: Push the branch**

Run:

```powershell
git push -u origin codex/fix-release-auth-ownership
```

Expected: GitHub accepts the branch and configures the upstream.

- [ ] **Step 4: Open the pull request**

Run:

```powershell
gh pr create --base main --head codex/fix-release-auth-ownership --title "Fix release authentication and snapshot reuse" --body "## Summary
- give run_release sole ownership of node authentication
- forward prompt auth consistently through release and rollback
- reuse an existing tree-equivalent Bitbucket deployment snapshot
- preserve durable progress evidence before authentication

## Verification
- py -m pytest -n 4 --dist loadfile

## Release risk
- authentication sequencing and Bitbucket publication selection changed
- tree reuse is gated by exact Git tree SHA equality
- no credentials or generated reports are committed"
```

Expected: `gh` prints the new GitHub pull-request URL.

### Task 6: Run the Autobench PR #35 release through tmux

**Files:**
- Read only: `D:\Projects\autobench`
- Generate outside Git: a timestamped `release-*-pr35-localcore` directory under `D:\Projects\autobench\edge-deploy\reports`

- [ ] **Step 1: Verify source, remote snapshot, and tree equivalence**

Run:

```powershell
$autobench = 'D:\Projects\autobench'
$source = 'aa6d9a5f0fa5481ad75b938022b6a78b50b14a38'
$snapshot = 'dd6907b77a94fcd85e97792b572caca3634c7a18'
git -C $autobench fetch origin main
git -C $autobench fetch bitbucket main
$head = git -C $autobench rev-parse HEAD
$origin = git -C $autobench rev-parse origin/main
$bitbucket = git -C $autobench rev-parse bitbucket/main
$sourceTree = git -C $autobench rev-parse "$source`^{tree}"
$snapshotTree = git -C $autobench rev-parse "$snapshot`^{tree}"
if ($head -ne $source -or $origin -ne $source) { throw "Autobench source is not the reviewed PR #35 commit" }
if ($bitbucket -ne $snapshot) { throw "Bitbucket main moved from the approved PR #35 snapshot" }
if ($sourceTree -ne $snapshotTree) { throw "Reviewed source and deployment snapshot trees differ" }
git -C $autobench status --short --branch
```

Expected: local `HEAD` and `origin/main` equal `aa6d9a5...`, `bitbucket/main` equals `dd6907b...`, both trees are identical, and the Autobench working tree is clean apart from ignored/generated release reports.

- [ ] **Step 2: Stop only the two confirmed stale release controllers**

Run:

```powershell
$stale = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and
  $_.CommandLine -like '*edge_deploy release*' -and
  (
    $_.CommandLine -like '*release-20260702T233216Z-pr35-clean*' -or
    $_.CommandLine -like '*release-20260702T234200Z-pr35-localcore*'
  )
}
$stale | Select-Object ProcessId,CommandLine | Format-List
$stale | ForEach-Object { Stop-Process -Id $_.ProcessId }
```

Expected: only the two stale controllers are selected and stopped. No unrelated Python process is touched.

- [ ] **Step 3: Start the dedicated release controller**

Run:

```powershell
tmux kill-session -t edge-release-pr35 2>$null
tmux new-session -d -s edge-release-pr35 -c D:\Projects\autobench
$releaseCommand = '$env:PYTHONPATH=''D:\Projects\edge-deploy-core''; $env:EDGE_DEPLOY_SSH_MULTIPLEX=''0''; $stamp=(Get-Date).ToUniversalTime().ToString(''yyyyMMddTHHmmssZ''); $env:EDGE_DEPLOY_PR35_REPORT="D:\Projects\autobench\edge-deploy\reports\release-$stamp-pr35-localcore"; py -m edge_deploy release --auth-mode prompt --report-dir $env:EDGE_DEPLOY_PR35_REPORT'
tmux send-keys -t edge-release-pr35 -l $releaseCommand
tmux send-keys -t edge-release-pr35 Enter
tmux capture-pane -t edge-release-pr35 -p -S -100
```

Expected: the controller runs local gates, creates a new `release-*-pr35-localcore` directory, and eventually displays `[node03] Enter RSA PASSCODE:`.

- [ ] **Step 4: Operator authentication checkpoint**

Run:

```powershell
tmux attach -t edge-release-pr35
```

Expected: the operator enters a fresh PASSCODE at the node03 controller prompt and later another fresh PASSCODE at the node04 controller prompt. No PASSCODE is pasted into chat, commands, files, or reports.

- [ ] **Step 5: Monitor the release without restarting it**

Run from a second PowerShell:

```powershell
tmux capture-pane -t edge-release-pr35 -p -S -500
$reportDir = Get-ChildItem 'D:\Projects\autobench\edge-deploy\reports' -Directory -Filter 'release-*-pr35-localcore' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
Get-Content -LiteralPath ($reportDir.FullName + '\release.log') -Tail 100
Get-Content -LiteralPath ($reportDir.FullName + '\release-progress.json') -Raw
```

Expected: progress advances through publish, auth, rollout, dependency delivery/install, and verify. The publish report records source `aa6d9a5...` and reused snapshot `dd6907b...`.

- [ ] **Step 6: Verify consolidated and per-node evidence**

Run:

```powershell
$expectedSource = 'aa6d9a5f0fa5481ad75b938022b6a78b50b14a38'
$expectedSnapshot = 'dd6907b77a94fcd85e97792b572caca3634c7a18'
$release = Get-Content -LiteralPath ($reportDir.FullName + '\release.json') -Raw | ConvertFrom-Json
$publish = Get-Content -LiteralPath ($reportDir.FullName + '\publish-autobench.json') -Raw | ConvertFrom-Json
if ($release.exit_code -ne 0) { throw "Release report is not successful" }
if ($publish.source_commit -ne $expectedSource -and $publish.extra.source_commit -ne $expectedSource) {
  throw "Publish report does not identify reviewed PR #35 source"
}
if ($publish.deployment_commit -ne $expectedSnapshot) { throw "Publish did not reuse dd6907b snapshot" }
foreach ($node in 'node03','node04') {
  $rollout = Get-Content -LiteralPath ($reportDir.FullName + "\rollout-autobench-$node.json") -Raw | ConvertFrom-Json
  if ($rollout.status -ne 'rolled_out') { throw "$node did not roll out successfully" }
  if ($rollout.deployment_commit -ne $expectedSnapshot) { throw "$node report has the wrong deployment commit" }
  $checks = @($rollout.checks)
  foreach ($required in 'remote_git_preflight','dependency_delivery','update','final_commit','install_preflight','install','dependency_activate','permissions','runtime_drift') {
    $check = $checks | Where-Object name -eq $required
    if (-not $check -or -not $check.passed) { throw "$node missing successful $required evidence" }
  }
  $smoke = $checks | Where-Object { $_.name -like 'smoke:*' }
  if (-not $smoke -or @($smoke | Where-Object { -not $_.passed }).Count) {
    throw "$node smoke evidence is missing or failed"
  }
}
$release.rollouts | Select-Object tool,node,status,deployment_commit,drift,smoke | Format-Table -AutoSize
```

Expected: release exit code is 0; publish provenance is `aa6d9a5... -> dd6907b...`; both node reports are `rolled_out` at `dd6907b...`; dependency, install, final-commit, permissions, drift, and smoke checks pass.

- [ ] **Step 7: Preserve the tag finalization boundary**

Run:

```powershell
Get-ChildItem -LiteralPath $reportDir.FullName -Filter 'push-release-*.ps1' |
  Select-Object FullName,Length,LastWriteTime
```

Expected: the release created a local tag-push handoff script. Do not execute it, push tags, merge the pull request, push to Bitbucket, or run another release without a separate Release Operator instruction.
