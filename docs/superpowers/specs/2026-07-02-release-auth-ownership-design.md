# Release Authentication Ownership Design

## Problem

`edge_deploy release` currently authenticates every selected node twice:

1. `_run_release_preflight` authenticates before `run_release` starts.
2. `run_release` authenticates again before rollout.

The CLI forwards the operator's `--auth-mode` choice to the first pass but
hardcodes `auth_mode="pane"` for the second pass. With the default `auto` mode
in an interactive Windows process, the first pass selects `getpass`. When the
controller is not attached to a usable terminal, it waits indefinitely before
the report directory and progress log exist.

Live stack inspection of the stalled Autobench PR #35 controllers confirmed
both were waiting in `getpass()` inside `_run_release_preflight`, not in
dependency-bundle construction, transfer, or installation.

The already-published PR #35 Bitbucket snapshot introduces a second retry
problem. Its operator-authored commit is tree-equivalent to reviewed GitHub
source `aa6d9a5f0fa5481ad75b938022b6a78b50b14a38`, but it is not that source
commit's ancestor. A fresh `publish_snapshot` call therefore creates another
synthetic commit instead of reusing
`dd6907b77a94fcd85e97792b572caca3634c7a18`. Release retries need an idempotent
way to select an existing tree-equivalent Bitbucket tip while retaining the
reviewed-source provenance required by dependency delivery.

## Goals

- Give authentication one owner: `run_release`.
- Honor the operator's explicit `--auth-mode` choice throughout release and
  rollback.
- Support `--auth-mode prompt` when the release controller runs inside a tmux
  pane that the operator can attach to.
- Create durable progress evidence before the first authentication prompt.
- Preserve the existing authenticated node-pane transport and dependency
  delivery behavior.
- Reuse the current Bitbucket deployment snapshot when its Git tree is
  identical to the reviewed source tree.
- Complete the Autobench PR #35 rollout from the exact published Bitbucket
  snapshot after the engine fix is verified.

## Non-goals

- Bypassing RSA authentication or storing credentials.
- Moving PASSCODE values into command arguments, logs, reports, or files.
- Replacing tmux/psmux or the authenticated node-pane transport.
- Changing dependency-bundle identity, staging, or installation contracts.
- Pushing release tags, pushing to Bitbucket, or merging a pull request without
  the separately required Release Operator action.

## Architecture

### Local preflight

`_run_release_preflight` remains the fail-closed local gate. It will:

- inspect the repository and configured remotes;
- require successful GitHub CI;
- run the local pytest gate;
- verify the audit repository state.

It will no longer create node drivers or authenticate nodes. Its parameters
will be reduced accordingly so authentication cannot drift back into this
phase accidentally.

### Release orchestration

`run_release` becomes the sole authentication owner. `_cmd_release` and
`_cmd_rollback` will forward `args.auth_mode` unchanged rather than hardcoding
pane mode.

`run_release` already constructs `ReleaseProgressTracker` before its
authentication loop. Therefore, after ownership is consolidated, the requested
report directory, `release.log`, and `release-progress.json` will exist before
the controller asks for the first PASSCODE.

Prompt mode will continue using `getpass` in the release controller. The
controller will run in a dedicated tmux session, so the operator can attach to
that pane, see `[nodeNN] Enter RSA PASSCODE:`, and enter the secret directly.
`authenticate_node` will forward the transient value into the corresponding
node pane using the existing secret-safe path.

### Idempotent publication

After fetching the configured Bitbucket release branch, `publish_snapshot`
will resolve the reviewed source tree and current remote-tip tree. When those
tree SHAs are equal, publication will:

- keep the current remote tip as the deployment snapshot;
- skip `commit-tree` and skip the branch push;
- return a normal successful `PublishResult` containing both the reviewed
  source commit and reused deployment commit;
- record a message stating that an existing tree-equivalent snapshot was
  reused.

Tree equality is the safety boundary established by ADR-0007. A different
remote tree retains the existing exact-commit or operator-authored synthetic
snapshot behavior. The reuse path never accepts an arbitrary operator assertion
and never fabricates a resume report.

### Runtime invocation

The Autobench PR #35 release controller will be started in a new, dedicated
tmux session with:

```powershell
$env:PYTHONPATH = 'D:\Projects\edge-deploy-core'
$env:EDGE_DEPLOY_SSH_MULTIPLEX = '0'
$stamp = Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ'
$report = "D:\Projects\autobench\edge-deploy\reports\release-$stamp-pr35-localcore"
py -m edge_deploy release `
  --auth-mode prompt `
  --report-dir $report
```

The operator will attach to the controller session when prompted. Existing
stalled controller processes will be stopped before the new attempt so they
cannot consume input or conflict with node-session names.

## Data flow

1. CLI resolves the tool, nodes, report directory, and requested auth mode.
2. Local preflight validates repository, CI, tests, and audit state without
   opening SSH sessions.
3. `run_release` creates the report directory and progress tracker.
4. Publish establishes the exact deployment snapshot, reusing the current
   Bitbucket tip when its tree equals the reviewed source tree.
5. For each node, `run_release` creates or reuses the node tmux driver.
6. In prompt mode, the attached controller pane reads one PASSCODE with
   `getpass` and forwards it to that node pane.
7. Rollout performs remote Git preflight, dependency delivery when required,
   exact-SHA update, offline install, smoke/drift checks, and report writing.
8. The consolidated report records the result for both nodes.

No credential is written at any step.

## Error handling and recovery

- A rejected or expired PASSCODE retains the existing bounded retry behavior.
- Authentication failures are written as per-node rollout failures because the
  progress tracker and report directory already exist.
- If the controller is interrupted after publish, the original report
  directory remains the resume boundary and preserves reviewed-source
  provenance for dependency bundles.
- A missing or closed controller tmux session is an operator-visible failure;
  it must not trigger an automatic fallback to a hidden desktop prompt.
- Dependency delivery and install failures retain their existing fail-closed
  reports and resume semantics.
- A remote tip with a different tree is never reused; publication follows the
  existing push or synthetic-snapshot path.

## Testing

Regression coverage will prove:

- `_run_release_preflight` performs no node authentication.
- Release and rollback forward `--auth-mode prompt` to `run_release`.
- Prompt authentication occurs exactly once per node.
- The report directory and initial progress log exist when the injected
  `getpass` callback is invoked.
- A tree-equivalent Bitbucket tip is reused without `commit-tree` or a Git
  push, and the result records reviewed-source and deployment SHAs.
- A tree-divergent Bitbucket tip continues through the existing synthetic
  snapshot path.
- Existing pane and auto-mode authentication tests continue to pass.
- The full suite passes with `python -m pytest -n 4 --dist loadfile`.

The original live failure will be rechecked by launching the release controller
inside tmux, confirming the prompt appears in the attached controller pane, and
confirming the report directory exists before entering the first PASSCODE.

## Completion criteria

- The engine changes and regression tests are committed on a short-lived
  branch and opened as a GitHub pull request.
- Autobench snapshot
  `dd6907b77a94fcd85e97792b572caca3634c7a18` is deployed to node03 and node04.
- Both nodes report that exact `HEAD`.
- Dependency build, transfer/reuse, verification, activation, install,
  smoke, and drift evidence is present in the release reports.
- Remote release tags are handled only through the documented explicit
  Release Operator finalization phase.
