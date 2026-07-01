# edge-deploy-core — Design

Shared deployment toolkit that publishes a reviewed build of a **Tool**
(autobench, robocop/Dispatch) and rolls it out to the Mastercard Hadoop **Edge
Nodes**, automating everything except the **Operator's** interactive auth.

Read [CONTEXT.md](../CONTEXT.md) for the ubiquitous language. Decisions with
lasting consequences are recorded as ADRs in [docs/adr/](adr/).

## 1. Goals

- One command to release autobench, robocop, or **both** to one or more Edge
  Nodes.
- Fully automated except the moment the Operator types an RSA passcode (and,
  only for deep smoke, a Kerberos password).
- A single shared engine; per-tool differences live in **data** (Tool Profile),
  not branches in code.
- Honest safety: refuse what can't be done safely (e.g. dependency changes over
  the git path), never leave a node silently half-done.

## 2. Where code runs (foundational)

Everything in edge-deploy-core runs on the **Operator's Windows machine**: local
git, a local tmux/psmux pane, and SSH. The air-gapped Edge Node only ever runs
each tool's own `update.sh`/`install.sh` plus tiny inline Python sent over the
**Authenticated Pane**. The node never imports this package. → ADR-0001
(standalone operator package, not vendored per tool).

## 3. Distribution & layout

```
edge-deploy-core/                 # installable package on operator
  edge_deploy/
    config.py        # OperatorConfig + ToolProfile loaders
    tmux_driver.py   # shared Authenticated Pane driver (chrome injected)
    publish.py       # BB_TOKEN snapshot publish
    rollout.py       # update.sh/install.sh engine + verify (was robocop deploy.py)
    drift.py         # local/remote runtime manifest compare
    verify.py        # commit + drift + permissions + per-tool smoke
    release.py       # umbrella orchestrator (fan-out, auth seam, report)
    reporting.py     # per-node + consolidated JSON, redaction
    preflight.py     # DNS/TCP reachability
    cli.py           # python -m edge_deploy ...
  docs/ (DESIGN.md, adr/)
  CONTEXT.md

autobench/edge_deploy.yaml        # Tool Profile (committed in each tool repo)
robocop/edge_deploy.yaml
```

The robocop `tools/prod_tui` harness is the reference implementation most of this
is extracted from; autobench has no equivalent yet and gains it for free.

## 4. Configuration — two layers (Round 9)

### 4.1 Operator config (operator machine, e.g. `~/.edge-deploy/config.yaml`)

Edge Node inventory + per-tool working-copy paths. `BB_TOKEN` stays an env var.

```yaml
operator_email: e176097@mastercard.com
nodes:
  node03:
    host: e176097@hde2stl020003.mastercard.int
    ssh_options: "-p 2222 -o ServerAliveInterval=30"
    session: edge-node03
  node04:
    host: e176097@hde2stl020004.mastercard.int
    ssh_options: "-p 2222 -o ServerAliveInterval=30"
    session: edge-node04
tools:
  autobench: { path: D:/Projects/autobench }
  robocop:   { path: D:/Projects/robocop }
```

### 4.2 Tool Profile (`edge_deploy.yaml`, committed in each tool repo)

Tool-specific, node-independent. After ADR-0004 it carries no install command or
env-name mapping.

```yaml
# autobench/edge_deploy.yaml
tool: autobench
repo_path: /ads_storage/autobench
bitbucket_url: https://scm.mastercard.int/stash/scm/~e176097/autobench.git
release_branch: main
runtime_paths: [benchmark.py, tui_app.py, "core/**/*.py", "utils/**/*.py", "scripts/**/*.py"]
compile_targets: "benchmark.py tui_app.py core utils scripts tools"
version_files: [VERSION, pyproject.toml]
install_trigger_paths: [install.sh, requirements.txt, constraints.txt, pyproject.toml, VERSION]
smoke:
  standard:
    - "./run_tool.sh config list"
    - "./run_tool.sh share --help"
  deep: []                       # autobench's fixture run is offline; no Kerberos
sensitive_paths: []
tui_chrome_regex: "Privacy-Compliant Peer Benchmark|Control 3.2 dimensional analysis"
tui_exit: ctrl_c                 # autobench TUI has no q/escape quit; CLI smoke avoids the TUI anyway
```

```yaml
# robocop/edge_deploy.yaml
tool: robocop
repo_path: /ads_storage/dispatch
bitbucket_url: https://scm.mastercard.int/stash/scm/~e176097/dispatch.git
release_branch: main
runtime_paths: ["dispatch/**/*.py", "dispatch/**/*.tcss", "scr/**/*.py"]
compile_targets: "dispatch scr"
version_files: [VERSION, dispatch/version.py, pyproject.toml]
install_trigger_paths: [install.sh, requirements.txt, pyproject.toml, VERSION, "dispatch/**"]
smoke:
  standard: ["dispatch --help"]
  deep: ["<controlled Impala job>"]    # needs Kerberos
sensitive_paths: ["scr/"]
tui_chrome_regex: "Dispatch \u2014 Impala|Active Jobs|esc Back|n New Job"
tui_exit: dispatch_dynamic       # keep robocop's dashboard-aware return_to_shell (q at top, esc to pop)
```

## 5. The standardized on-node interface (ADR-0004)

Both tools' `update.sh`/`install.sh` read one namespace, with old names as
fallback aliases for one release:

- `EDGE_DEPLOY_REMOTE` (was `*_GIT_REMOTE` / `DISPATCH_UPDATE_REMOTE`)
- `EDGE_DEPLOY_BRANCH` (was `*_GIT_BRANCH` / `DISPATCH_UPDATE_BRANCH`)
- `EDGE_DEPLOY_PYTHON_BIN` (was `*_PYTHON_BIN`)
- `EDGE_DEPLOY_EMAIL` (optional; only robocop consumes it)

## 6. The Release pipeline

```
edge_deploy release --tool {autobench|robocop|both} [--nodes 03,04]
                    [--snapshot <sha>] [--smoke standard|deep]
                    [--fail-fast]

for each selected Tool:
  PUBLISH (skipped if --snapshot given)                    [auto, operator machine]
    - guard: clean tree + on release_branch + local_check.ps1 green   (Round 7)
    - reparent HEAD tree onto bitbucket/main, push via BB_TOKEN Bearer
    - message: "Deploy snapshot: <tool> <source-short> on <branch> (<YYYY-MM-DD HH:mm>) [edge-deploy]"
    - capture Snapshot SHA
    - on failure: mark tool failed, skip its Rollouts                 (ADR-0003)

for each selected Edge Node (serial):
  ESTABLISH Authenticated Pane                              [Operator steps in]
    - open local tmux pane -> ssh -> park at PASSCODE
    - getpass prompt -> forward code -> confirm shell                 (ADR-0002)
  for each selected Tool on this node:
    ROLLOUT (reuses the node's pane)                        [auto]
      - diff changed paths (previous HEAD -> Snapshot)
      - REFUSE if requirements/constraints changed                    (ADR-0005)
      - update.sh <snapshot>  (EDGE_DEPLOY_REMOTE/BRANCH)
      - install.sh only if install_trigger_paths changed
      - verify final commit == Snapshot
    VERIFY                                                  [auto]
      - Drift == 0 over runtime_paths
      - permissions (root traversable, scripts executable)
      - smoke: standard (no auth) or deep (Kerberos getpass)          (Round 8)
      - flag sensitive_paths changes in report                        (Round 12)

REPORT
  - per (tool x node) JSON + consolidated summary
  - status per pair: rolled-out | failed(state left=…) | skipped | refused
  - non-zero exit if anything failed; explicit handoff flags
  - secrets redacted everywhere                                       (ADR-0002)
```

Auth is **serial per node** because RSA codes are single-use (~60s); one
Authenticated Pane per node is reused across both tools, so you pay auth once per
node, not per tool or per command.

## 6.1 Reporting

A consolidated `edge-deploy/release/1` report embeds a compact per-rollout
summary (`status` ∈ `rolled_out | failed | skipped | refused`, `state_left`,
`sensitive_changed`, pointer) and references detailed per-(tool×node) files that
reuse robocop's existing `OperationReport`/`ReportCheck` schema unchanged.
`summary` carries counts + `handoffs[]` + `overall`. All output redacted
(ADR-0002).

## 7. Resume / re-run (Round 6)

- Default run publishes a fresh Snapshot then rolls out.
- `--snapshot <sha>` skips Publish and rolls out an existing Snapshot — this one
  flag covers resume-after-failure, re-rollout of a known-good build, and
  exact-SHA deploys.
- `--nodes` subsets which Edge Nodes a run touches.
- `update.sh`/`install.sh` are idempotent, so re-running a succeeded node is safe.

Recovery example: `release --tool robocop --nodes 04 --snapshot <sha>`.

## 8. Out of scope for v1

- Offline dependency/wheel refresh (`deploy_and_install.ps1`) stays a separate
  manual path; Releases refuse dependency-changing Snapshots (ADR-0005).
- Auto-rollback (rollback stays explicit, ADR-0003).

## 9. Phasing

1. Extract robocop's harness into `edge-deploy-core` + add OperatorConfig/
   ToolProfile; write autobench's profile. autobench gains real Rollout + remote
   Drift immediately.
2. Standardize on-node interface (ADR-0004); converge Publish on `BB_TOKEN`;
   build the umbrella `release` orchestrator + auth seam.
3. Wrap in the actual skill (deferred per the Operator's earlier note).

The driver's `return_to_shell` takes a per-tool `tui_exit` strategy
(`ctrl_c` | `dispatch_dynamic` | `none`); the shared default sends `Escape`
then `Ctrl+C`. autobench rarely needs it (CLI smoke); robocop keeps its
dashboard-aware strategy.

Node Rollouts run **serially** (legible output, deterministic reports);
`--concurrent` is a possible future option.

The `EDGE_DEPLOY_*` alias fallback is removed once both tools complete one
successful Release on both nodes via the new interface (ADR-0004).

## 10. Open items

All design-tree branches grilled and closed. The two items left for implementation
are now resolved in Phase 2 code:

- **Consolidated-report JSON field names — resolved.** The `edge-deploy/release/1`
  schema is finalized in `reporting.ReleaseReport.to_payload()`: top-level
  `schema`, `timestamp`, `operator_email`, `selection`, `publishes[]`, `rollouts[]`,
  `summary`, `exit_code`. Each `rollouts[]` entry carries
  `tool, node, status, state_left, deployment_commit, previous_remote_commit,
  sensitive_changed, drift, smoke, report_path`; `summary` carries
  `counts` (the four pair statuses plus `published`/`publish_failed`),
  `handoffs[]` (`kind ∈ publish | mid_state | refused | snapshot`, each with a
  ready-to-paste `action`), and `overall`.
- **Per-tool `tui_exit` wiring — resolved (Phase 1).** `TmuxDriver.return_to_shell`
  already dispatches on the injected `tui_exit` strategy (`ctrl_c` |
  `dispatch_dynamic` | `none`); Phase 2's per-node pane takes its chrome/`tui_exit`
  from the first selected tool and relies on each engine call's explicit `cd`
  (see Phase-2 plan Risk #7).

Remaining cross-repo follow-up (tracked, not in this phase): wire each tool repo's
`prod_tui` deploy/drift paths to call the package (Plan T7), then remove the
`EDGE_DEPLOY_*` alias fallback after one green Release on both nodes × both tools
(ADR-0004 / Plan T9).
