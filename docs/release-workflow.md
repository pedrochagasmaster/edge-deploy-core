# Release Operator Workflow

Only a Release Operator publishes `edge-deploy-core` and runs tool releases.
The release engine persists every attempt as a **run** under
`<tool repo>/edge-deploy/runs/<run-id>/`. Phases are short, idempotent commands
that declare which firewall posture they need. Use `status` to see state and the
exact next command.

## Prerequisites

1. Update a clean local `main` from GitHub with `git pull --ff-only origin main`.
2. Confirm `HEAD` equals `origin/main` and its post-merge GitHub CI succeeded.
3. Install and test from the tool checkout:

   ```powershell
   python -m pip install -e ".[dev,release]"
   python -m pytest -n 4 --dist loadfile
   ```

4. Copy [config.example.yaml](../config.example.yaml) to
   `%APPDATA%\edge-deploy\config.yaml` and set `BB_TOKEN` in the environment.

5. Before releasing to a node for the first time, or after any change to its
   `ssh_options`, known-hosts entry, or credentials, verify its transport:

   ```powershell
   py -m edge_deploy preflight --node node03
   py -m edge_deploy transport-smoke --node node03
   ```

   `transport-smoke` authenticates once and exercises command execution,
   verified file transfer, PTY dialogue, and keepalive over that single
   connection, then always tears it down and reports pass/fail per check.

## Transport (ADR-0014)

SSH (Paramiko) is the default transport (`transport: ssh` in
`config.example.yaml`): one persistent, digest-verified connection per node
per deploy invocation, reused for authentication, dependency transfer,
rollout commands, and drift checks. Matching dependency archives already
present on the node are reused by digest without transferring any bytes; new
archives stream over SFTP (falling back to a binary exec-channel stream if
the SFTP subsystem is unavailable) and report live byte progress — MiB sent,
percent complete, and MiB/s — both on the console and in
`release-progress.json`, which `edge_console.py` renders as a progress bar.

`transport: pane` remains an explicit per-node override for recovery when SSH
access to a node regresses (ADR-0011); selecting it restores the psmux pane
protocol unchanged. No transport failure silently falls back from `ssh` to
`pane` — a transport failure is a durable node failure (see status below), not
an automatic retry over a different channel.

No manual SCP or symlink workaround (for example, hand-copying a bundle to a
node, or a `/ads_storage/$USER` symlink) is part of the canonical 1.5.0
workflow. Release-owned remote state lives under the canonical
`~/.edge-deploy` path, resolved once per session against the authenticated
node's real home directory.

```powershell
py -m edge_deploy release --guided --tool autobench
```

## Release flow (run ledger)

Work from the clean GitHub `main` checkout of the tool being released
(Autobench or Dispatch).

### 1. Create or resume a run

Starting a new release when an unresolved run already exists is refused:

```text
release refused: unresolved run <run_id> for <tool> (source <sha7>, created <created_at>) exists.
Choose one:
  1. continue it:   py -m edge_deploy release --run <run_id>
  2. abandon it:    py -m edge_deploy abandon --run <run_id> --reason "<why>"
```

To start fresh (no open run):

```powershell
py -m edge_deploy release --tool autobench
```

This creates `edge-deploy/runs/run-<UTC>-<sha7>/` with `state.json`,
`events.jsonl`, and (while active) `run.lock`.

To continue an existing run:

```powershell
py -m edge_deploy release --run <run_id>
```

If another process holds the lock, the engine exits with exactly:

```text
run <run_id> is locked by PID <pid> on <hostname> (acquired <acquired_at>); if that process is dead, re-run with --force-lock
```

### 2. Inspect state

```powershell
py -m edge_deploy status
```

With no open runs:

```text
no open runs under <runs root>
```

With an active run, `status` prints per-phase state and the next command, for
example:

```text
run run-20260703T120000Z-aa6d9a5  tool=autobench  kind=release  source=aa6d9a5  created=2026-07-03T12:00:00+00:00
  verify:        passed
  publish:       passed (snapshot bb7c8d1)
  deploy:        node03=passed node04=failed
  tag_bitbucket: pending
  tag_github:    pending
next: py -m edge_deploy deploy --run run-20260703T120000Z-aa6d9a5 --nodes node04   [posture: both-vpns]
```

### 3. Run phases (posture-scoped)

The workstation has five postures (ADR-0013). GitHub *read* works in every
posture; GitHub *write* requires the firewall off, which drops both VPNs; the
Bitbucket and Edge VPNs are independent and may be held together:

| Posture | GitHub read | GitHub write | Bitbucket | Edge |
|---------|-------------|--------------|-----------|------|
| baseline | yes | no | no | no |
| edge-vpn | yes | no | no | yes |
| bitbucket-vpn | yes | no | yes | no |
| both-vpns | yes | no | yes | yes |
| firewall-off | yes | yes | no | no |

Switch the firewall manually before each phase. If the current posture does
not satisfy a phase, it exits immediately with exactly:

```text
phase '<phase>' requires posture [<satisfying postures>]; unreachable: <failures, comma-joined>.
Switch the firewall posture, then re-run: <next command>
```

Posture is verified with protocol-level git probes (`git ls-remote` for reads,
`git push --dry-run` for writes; ADR-0012), because the proxy accepts TCP
connects in every posture. Edge SSH endpoints are still TCP-probed.

| Phase | Command | Posture |
|-------|---------|---------|
| Verify | `py -m edge_deploy verify --run <run_id>` | any (GitHub read) |
| Publish | `py -m edge_deploy publish-phase --run <run_id>` | bitbucket-vpn or both-vpns |
| Deploy | `py -m edge_deploy deploy --run <run_id> [--nodes …]` | both-vpns |
| Tag Bitbucket | `py -m edge_deploy tag-bitbucket --run <run_id>` | bitbucket-vpn or both-vpns |
| Tag GitHub | `py -m edge_deploy tag-github --run <run_id>` | firewall-off |

Tag Bitbucket runs before Tag GitHub (ADR-0012/0013), and Verify runs in any
posture, so a release started in both-vpns needs exactly one posture switch:
both-vpns → firewall-off for the final Tag GitHub.

The `release` wrapper chains phases until the first posture boundary, saves
state, prints the posture message above, and exits 0. Re-run `release --run
<run_id>` (or the individual phase command from `status`) after switching the
firewall.

#### Guided release (single command)

To walk through all posture switches in one invocation, use `--guided`:

```powershell
py -m edge_deploy release --guided --tool autobench
```

At each posture boundary the engine prints the satisfying posture names and any
unreachable endpoints, then prompts:

```text
Switch firewall posture to [both-vpns], then press Enter to continue...
```

Switch the firewall, press Enter, and the engine polls the probes for up to
~90 seconds while the switch propagates, then continues. If endpoints are still
unreachable after that, it re-prompts with the updated unreachable list. If a
phase still hits a transient remote error right after a switch (for example an
HTTP 503 from the proxy), guided mode retries the phase with 10/20/30 s backoff
before giving up (ADR-0012). Press Ctrl+C (or EOF) to pause: the run stays
`open` and the engine prints the resume command, for example:

```text
Paused at posture boundary. Resume with: py -m edge_deploy release --guided --run <run_id>
```

Resume works across postures: when `verify` is already `passed` or `skipped`,
`release --run <id>` does not fetch from GitHub, so you can continue from
bitbucket-vpn or both-vpns without repeating verify.

Publish normally reuses the run ledger's verification instead of repeating a
tool's `local_check.ps1`. Reuse requires evidence bound to the run's exact
source SHA with successful CI, passed tests, and a verification timestamp. An
incomplete or legacy ledger safely falls back to the local check; standalone
`edge_deploy publish` also retains that gate by default. Publish evidence and
`publish-<tool>.json` record `verification_source` and `local_check_ran` so a
reused gate is never reported as though the script executed.

Verify itself always runs the exact source checkout's committed
`tools/dev/local_check.ps1` after GitHub CI succeeds. The tool owns test
selection, parallelism, temporary isolation, and platform setup; the engine
owns ordering and evidence. Only a successful exit records passed tests. A
failure writes a redacted tail to `verify-local-check.log` and blocks every
later phase. Because the Release Operator is on Windows, each tool must run
this authoritative gate in Windows CI as well as retaining its supported
Linux jobs.

`--no-local-check` bypasses only a publish fallback; verify always runs the
source-bound committed tool gate. It is not a verification bypass.

If the fallback local check fails, publish becomes `failed` and writes its
redacted output tail to `publish-local-check.log` in the run directory. Inspect
that artifact, correct the failure, and retry the same `publish-phase` command;
no Bitbucket mutation occurs before the gate passes.

On completion, guided mode prints `release complete: <run_id>` followed by the
same status summary as `py -m edge_deploy status --run <run_id>`.

During deploy, enter the RSA passcode at the interactive prompt when asked (the
keyboard-interactive SSH prompt for `transport: ssh` nodes, or the controller
tmux pane for `transport: pane` nodes). The progress heartbeat shows
`>>> WAITING FOR OPERATOR - …` while waiting.

### 4. Complete

When both tag phases pass, the run is marked complete:

```text
release complete: <run_id>
```

Then mirror the core package tag if releasing `edge-deploy-core` itself:

```powershell
git push origin main "refs/tags/vX.Y.Z"
py -m edge_deploy mirror --tag vX.Y.Z
```

Mirror pushes the exact commit when Bitbucket accepts it; when Bitbucket's
own-commits hook rejects a GitHub-authored merge commit, it creates an
operator-authored mirror commit and tag carrying the identical tree (ADR-0007).

Record the redacted release attempt on Bitbucket's `release-log` branch (the
`tag-bitbucket` phase appends audit evidence). Update the pinned core version
in each tool through a normal GitHub pull request.

## Rollback

```powershell
py -m edge_deploy rollback --tag release-<UTC>-<short-sha>
```

Rollback creates a run with `kind=rollback` seeded from the tag's provenance,
then follows the same phase chain.

## Abandon

Close a run that will not be finished (recorded in the ledger and audit):

```powershell
py -m edge_deploy abandon --run <run_id> --reason "why this run stops"
```

## Artifacts

Each run directory holds `state.json`, `events.jsonl`, `release-progress.json`
(live transfer byte progress), per-node `pane-<node>.log` files (when
`transport: pane` is in use), publish reports, rollout reports, and
dependency bundles. Bundles are content-addressed and never committed. A
failed deploy node can be retried with `deploy --run <run_id> --nodes <node>`
without re-running verify or publish when their ledger state is already
`passed`.

## Rules

- Never move or reuse a published semantic-version tag.
- The `release-log` branch is Bitbucket-only and must never be pushed to GitHub.
- Dependency bundles follow the ADR-0006 contract (`v1.1.0`).
- Do not start a second release for a different SHA while an open run for the
  same tool remains unresolved.

See [docs/DESIGN.md](DESIGN.md) for module boundaries,
[docs/adr/0008-run-ledger-and-posture-phases.md](adr/0008-run-ledger-and-posture-phases.md)
for architecture decisions,
[docs/adr/0015-source-bound-verification-reuse.md](adr/0015-source-bound-verification-reuse.md)
for the durable publish verification contract,
[docs/adr/0016-tool-owned-verification.md](adr/0016-tool-owned-verification.md)
for tool-owned source verification,
[docs/adr/0010-guided-posture-loop.md](adr/0010-guided-posture-loop.md) for
guided mode and cross-posture resume, and
[docs/adr/0014-paramiko-release-transport.md](adr/0014-paramiko-release-transport.md)
(with [ADR-0011](adr/0011-pane-safe-remote-transport.md)) for the transport
layer.
