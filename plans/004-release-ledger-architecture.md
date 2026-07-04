# Plan 004: Release-ledger architecture — posture-aware phases over durable state

> **Executor instructions**: This is an architecture plan, executed as five
> independently shippable milestones (M1–M5). Each milestone is one branch and
> one PR, following CONTRIBUTING.md. Run every verification command and confirm
> the expected result before moving to the next milestone. If a STOP condition
> occurs, stop and report — do not improvise.

## Status

- **Priority**: P0
- **Effort**: XL (split into 5 S/M milestones)
- **Risk**: MEDIUM (behavioral changes to the release CLI; mitigated by milestone sequencing)
- **Depends on**: none (supersedes the ad-hoc fixes on `codex/fix-release-auth-ownership`)
- **Category**: architecture
- **Planned at**: commit `ff3393f`, 2026-07-03

## Why this matters — evidence from three end-to-end release attempts

Three codex sessions (2026-07-01 → 2026-07-03) tried to run the full release
process end to end. Every failure traces to one root design flaw: **the release
is modeled as one long-lived interactive process, but the environment only
supports short, resumable, posture-scoped steps.** Observed failures, all from
session transcripts:

| # | Failure | Root cause |
|---|---------|-----------|
| 1 | Release died mid-run: GitHub tag push returned 503 / hung | **Firewall split**: GitHub access and Bitbucket+edge access are mutually exclusive postures; a single command needing both *cannot* succeed |
| 2 | Bitbucket rejected `v1.1.0` (`GitHub <noreply@github.com>` committer) | Server hook; solved by ADR-0007 tree-equivalent mirroring — but the engine still assumed one-shot cross-remote publication |
| 3 | Release silently hung forever; looked stuck; was actually a hidden `getpass()` | **Duplicated auth ownership**: preflight AND `run_release` both authenticated; one path hardcoded `pane`, another fell into `getpass` with no visible TTY |
| 4 | Release deployed the *wrong snapshot* (old rollback instead of PR #35) and reported `overall: passed` | **Implicit resume**: `release` silently picked up a stale report directory |
| 5 | `JSONDecodeError` crash after successful remote staging | **Screen-scraping as an API**: JSON evidence parsed from `tmux capture-pane` output that wrapped at pane width |
| 6 | `getsockname failed: Not a socket`; scp demanded a second RSA passcode; mangled prompt caused a failed auth window | Windows OpenSSH `ControlMaster` is broken; every new connection costs a fresh RSA passcode; prompt detection scrapes the pane |
| 7 | psmux refused nested `new-session`; node pane never started; prompt never appeared | Controller runs inside tmux; `PSMUX_SESSION` leaked into child tmux calls |
| 8 | Rollback failed: dependency gate wanted a reviewed-source SHA the rollback path didn't carry; separately, the old snapshot's `install.sh` ignored `EDGE_DEPLOY_BUNDLE_DIR` | Engine depends on the *target snapshot's* on-node scripts; contract drifts across history |
| 9 | Fixes existed locally but the pinned `v1.1.0` site-packages engine ran (agent hot-patched site-packages); later runs mixed editable/`PYTHONPATH`/installed engines | **Engine identity skew** — nothing asserts which engine executes a run |
| 10 | Every retry re-ran the full 373-test suite (3–5 min each, ~8 reruns in one evening); two concurrent controllers collided on tmux session names | No durable "already verified" fact; no run-level locking |

The ~15 commits of fixes on this branch each patch one symptom. The
architecture below removes the failure classes.

## Constraints that are permanent (design inputs, not bugs)

1. **Network postures are exclusive**: `github` XOR (`bitbucket` + edge nodes).
   Operator toggles the firewall manually between them.
2. **RSA SecurID auth is human and one-shot**: each new SSH connection costs a
   fresh passcode typed by the operator; passcodes expire in seconds.
3. **Tree-equivalent mirroring (ADR-0007)**: GitHub and Bitbucket tags point at
   different commits with identical trees.
4. **Windows host**: psmux tmux shim; OpenSSH `ControlMaster` unusable; pane
   text wraps; PowerShell is the shell.
5. **Operator-only**: everything runs on the operator's machine; nodes never
   pull; no wheels/reports/secrets in GitHub.

## Target architecture

### One sentence

The release becomes a **state machine persisted in a run ledger**, advanced by
**short idempotent phase commands** that each (a) declare which network posture
they need and fail fast if it is absent, (b) never prompt except through one
auth broker, and (c) exchange data with nodes through **files, not screen
text**.

### A. Run ledger (durable state, single source of truth)

New module `edge_deploy/ledger.py`.

- A release attempt = a **run**: `edge-deploy/runs/<run-id>/` created *before
  any side effect*, containing `state.json` (current phase per unit of work)
  and `events.jsonl` (append-only evidence: command, timestamp, outcome,
  artifact digests).
- `state.json` records at creation: tool, source SHA, **engine identity**
  (edge_deploy `__version__` + git SHA + import path), operator, node list.
- Every phase command starts by loading the ledger, validating preconditions,
  and ends by atomically writing the new state (write temp + rename).
- **Explicit resume only.** Starting a new run while an unresolved run exists
  is refused with the three legal options printed: `--run <id>` (continue),
  `edge-deploy abandon --run <id> --reason ...` (close it, recorded in the
  Bitbucket release-log), or `--nodes`-scoped continuation. Auto-resume is
  deleted. This is the fix for failure #4.
- A run holds a lock file (`run.lock` with PID); a second controller refuses to
  start. Fix for failure #10 (collisions).
- `edge-deploy status` prints: active run, per-phase/per-node state, and **the
  exact next command including which firewall posture to switch to**. This
  replaces "inspect the tmux pane and guess".

### B. Posture-aware phases (the release is five small commands)

`release` stops being a monolith. Each phase is idempotent, ledger-driven, and
declares its endpoints. A thin `edge-deploy release` wrapper may chain phases
until the first posture boundary, then prints the switch instruction and exits 0
with state saved — but the phases are the real interface.

| Phase | Command | Posture needed | Does | Records |
|-------|---------|----------------|------|---------|
| 1 | `verify` | none (local + GitHub read via `gh`) | clean-main gate, pytest, ruff, post-merge CI check for exact SHA | verification evidence, engine identity |
| 2 | `publish` | bitbucket | tree-equivalent snapshot to Bitbucket `main` (existing `publish.py`) | snapshot SHA ↔ source SHA provenance |
| 3 | `deploy --nodes node03,node04` | bitbucket + edge | per node: auth → bundle deliver → checkout update → install → smoke → drift | per-node status; resumable per node |
| 4 | `tag github` | github | annotated `release-*` tag on origin pointing at source SHA | tag object SHA |
| 5 | `tag bitbucket` | bitbucket | mirror tag at snapshot SHA + append redacted audit to `release-log` | tag object SHA, audit commit |

- Phase entry runs a **2-second TCP posture probe** against its declared
  endpoints. Wrong posture → immediate, named failure: *"deploy needs
  bitbucket+edge posture; currently github posture. Switch the firewall, then
  re-run: `edge-deploy deploy --run <id>`"*. No more discovering the posture
  four minutes into a run (failures #1, #2 aftermath).
- Phase entry asserts **engine identity** matches the ledger; mismatch (a
  different site-packages/editable/PYTHONPATH engine) is a refusal, not a
  warning. Fix for failure #9.
- `verify` records its result once; `publish`/`deploy` **trust the ledger**
  when the SHA is unchanged instead of re-running the 4-minute suite on every
  retry. Fix for failure #10 (wasted reruns). `--reverify` forces a fresh run.
- `rollback --tag release-*` becomes the same state machine seeded differently:
  the tag provides both the reviewed source SHA (GitHub tag) and snapshot SHA
  (Bitbucket tag), written into the ledger up front — no synthesized publish
  reports (failure #8a).

### C. Auth broker — one owner for every prompt

- All authentication lives in `edge_deploy/auth.py` behind a single
  `AuthBroker` used only by `deploy`. `_run_release_preflight` loses its auth
  pass permanently (the branch already moved this; the ledger design makes it
  structural). `getpass` is deleted from the codebase — prompt-in-controller-
  pane is the only mode. Fix for failure #3.
- The broker's contract: for each node, ensure an **authenticated tmux pane**
  exists (reuse if alive, else create + SSH + wait for the passcode prompt).
  While waiting it emits an unmissable heartbeat state — progress JSON gains
  `"waiting_on": "operator"` and the controller prints
  `>>> WAITING FOR OPERATOR — enter RSA passcode for node03 in this pane <<<`
  — so "stuck" and "waiting for a human" are never confused again.
- Child tmux invocations always run with `PSMUX_SESSION` stripped (already in
  `ff3393f`; keep, with regression test). Fix for failure #7.
- The Windows `ControlMaster` code path is **deleted**, not defaulted off. One
  transport: the authenticated pane. Fix for failure #6's zombie branch.

### D. On-node runner — files in, files out; the pane is for humans only

New: a small versioned POSIX-sh runner, `edge_deploy/node_runner.sh`, shipped
by the engine (not by the tool repo).

- After auth, `deploy` writes the runner to the node **through the
  authenticated pane** using chunked base64 → file with an end-to-end sha256
  check (payload is never *parsed from* the screen; only a fixed-width one-line
  checksum ack is read back). Bundle archives travel the same way, chunked,
  with per-chunk and final digests, resumable at the chunk level. No second SSH
  connection → no second RSA passcode → no interactive `scp` (failures #5, #6).
- Every remote step is `sh runner.sh <step> <args>`; the runner writes
  `~/.edge-deploy/runs/<run-id>/<step>.json` (exit code, evidence, digests).
  The controller retrieves that small JSON by the same checksummed one-liner.
  `tmux capture-pane` is demoted to **human display and prompt detection
  only** — never data. This deletes `_parse_stage_evidence`-style screen
  parsing and the `__RC_nonce__` sentinel as a data channel (failure #5's whole
  class, including plans/002).
- The runner owns `update`/`install` invocation semantics (bundle dir, env,
  legacy-installer compatibility shims like `PIP_NO_INDEX`/`PIP_FIND_LINKS`).
  The engine stops depending on whichever `update.sh`/`install.sh` version the
  *target snapshot* happens to contain — the executor is shipped per-run, the
  tool repo ships only configuration. Fix for failure #8b.
- Full pane output is additionally logged locally via `tmux pipe-pane` to
  `runs/<run-id>/pane-<node>.log` for forensics (unwrapped, complete).

### E. What stays

- `ToolProfile` / `edge_deploy.yaml` per-tool configuration (ADR-0004 shape).
- Content-addressed dependency bundles and staging layout (ADR-0006).
- Tree-equivalent mirroring (ADR-0007).
- Redacted audit on Bitbucket `release-log` — extended to record run ledger
  transitions including `abandon`.
- Fan-out/report semantics of ADR-0003 (`rolled_out|failed|refused|skipped`),
  now derived from the ledger.

## Implementation milestones

Each milestone is a separate branch + PR, keeps the full suite green, and is
independently valuable. Order matters: M1 removes the most dangerous behavior
first.

### M1 — Run ledger + explicit resume + lock (S/M)

1. Add `edge_deploy/ledger.py`: run creation, atomic `state.json` writes,
   `events.jsonl` append, lock file, engine-identity capture.
2. Wire `cli.py` `release`/`rollback` to create the ledger **before** publish
   (kills "no report dir until too late").
3. Replace implicit report-dir resume with the refusal + explicit `--run` /
   `abandon` flow. Delete the auto-resume code path.
4. Add `edge-deploy status`.
5. Tests: unresolved-run refusal; abandon writes audit intent; lock collision;
   ledger survives `kill -9` mid-phase (state reflects last completed step);
   wrong-snapshot regression — a stale ledger for SHA A must refuse a bare
   release of SHA B.

- Verification: `py -m pytest -n 4 --dist loadfile` green; manual: start a fake
  run, kill it, confirm `status` names the run and next command.

### M2 — Phase split + posture probes (M)

1. Extract `verify`, `publish`, `deploy`, `tag github|bitbucket` subcommands
   operating on the ledger; `release` becomes the chaining wrapper that stops
   cleanly at posture boundaries.
2. Add `edge_deploy/posture.py`: endpoint declarations per phase + TCP probe +
   named posture error messages.
3. `verify` records evidence; `publish`/`deploy` skip re-testing on unchanged
   SHA (`--reverify` escape hatch).
4. Move tag pushes fully out of `deploy` (finish what the "push-release
   handoff script" started, natively).
5. Tests: posture refusal messages; verify-once skip logic; each phase
   idempotent when re-run after success (no-op with "already done" event).

### M3 — Auth broker consolidation (S)

1. Single `AuthBroker`; delete `getpass` usage and the duplicate preflight auth
   (make the branch's `2472fdf` structural).
2. `waiting_on: operator` heartbeat state in `progress.py` + controller banner.
3. Delete the `ControlMaster` transport and `EDGE_DEPLOY_SSH_MULTIPLEX`; keep
   `PSMUX_SESSION` stripping with its regression test.
4. Tests: no code path can reach `getpass`; auth requested exactly once per
   node per run; heartbeat distinguishes operator-wait from remote-run.

### M4 — On-node runner + file-based evidence (M/L)

1. Add `node_runner.sh` (versioned; sha256-pinned in the engine) and the
   chunked checksummed pane-upload primitive in `tmux_driver.py`.
2. Convert remote steps (git preflight, dependency stage/verify, update,
   install, smoke, drift) to runner steps returning JSON files; retrieval via
   fixed-width checksummed line.
3. Delete screen-parsing of data: `_parse_stage_evidence` from capture output,
   RC-sentinel-as-data, interactive `scp` transfer path.
4. Add `pipe-pane` logging per node.
5. Tests: FakeTmuxDriver reworked to serve runner-step JSON; wrap-resilience
   test (narrow pane) proving evidence parsing no longer touches screen text;
   chunk-transfer resume test; runner version mismatch refusal.

### M5 — Contract hardening + docs (S)

1. Engine-identity assertion at every phase entry (refuse skew).
2. Runner-owned install compatibility (legacy installer shim) replacing
   env-var special cases in `rollout.py`.
3. Rewrite `docs/release-workflow.md` around phases/postures/`status`; add
   **ADR-0008 (run ledger & posture phases)** and **ADR-0009 (on-node runner,
   file-based evidence)**; mark superseded text in ADR-0005/0006 accordingly.
4. Delete dead code paths and stale plans (001–003 are absorbed: 001 by M4's
   fake-driver rework, 002 by M4 deleting screen parsing, 003 unchanged).

## STOP conditions

- Any milestone requires touching Autobench/Robocop `update.sh`/`install.sh`
  *behavior* (M4 runner must wrap them, not rewrite them) — stop and confirm
  scope with the Release Operator first.
- The full suite cannot be kept green within a milestone — stop; do not stack
  unmerged milestones.
- A live release is in progress on this machine — never run milestone testing
  against `D:\Projects\autobench` while a run ledger is unresolved.

## What this buys, mapped back

| Failure class | Killed by |
|---|---|
| Cross-posture commands half-fail | M2 posture phases |
| Hidden prompts / silent hangs | M3 single auth broker + waiting heartbeat |
| Wrong-snapshot implicit resume | M1 explicit resume |
| Screen-wrap JSON crashes, mangled prompt parsing | M4 file-based evidence |
| Second-passcode scp / ControlMaster breakage | M4 pane transfer, M3 transport deletion |
| Engine version skew | M1 identity capture + M5 assertion |
| 4-minute suite reruns on every retry | M2 verify-once |
| Legacy on-node script drift | M4/M5 shipped runner |
| Concurrent controllers | M1 run lock |
