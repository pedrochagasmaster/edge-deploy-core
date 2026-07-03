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

## Release flow (run ledger)

Work from the clean GitHub `main` checkout of the tool being released
(Autobench or Dispatch).

### 1. Create or resume a run

Starting a new release when an unresolved run already exists is refused:

```text
release refused: unresolved run <run_id> for <tool> (source <sha7>, created <created_at>) exists.
Choose one:
  1. continue it:   python -m edge_deploy release --run <run_id>
  2. abandon it:    python -m edge_deploy abandon --run <run_id> --reason "<why>"
```

To start fresh (no open run):

```powershell
python -m edge_deploy release --tool autobench
```

This creates `edge-deploy/runs/run-<UTC>-<sha7>/` with `state.json`,
`events.jsonl`, and (while active) `run.lock`.

To continue an existing run:

```powershell
python -m edge_deploy release --run <run_id>
```

If another process holds the lock, the engine exits with exactly:

```text
run <run_id> is locked by PID <pid> on <hostname> (acquired <acquired_at>); if that process is dead, re-run with --force-lock
```

### 2. Inspect state

```powershell
python -m edge_deploy status
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
  tag_github:    pending
  tag_bitbucket: pending
next: python -m edge_deploy deploy --run run-20260703T120000Z-aa6d9a5 --nodes node04   [posture: bitbucket+edge]
```

### 3. Run phases (posture-scoped)

Network postures are exclusive: **github** or **bitbucket + edge**. Switch the
firewall manually before each phase. If the wrong posture is active, the phase
exits immediately with exactly:

```text
phase '<phase>' requires posture [<keys, comma-joined>]; unreachable: <host:port, comma-joined>.
Switch the firewall posture, then re-run: <next command>
```

| Phase | Command | Posture |
|-------|---------|---------|
| Verify | `python -m edge_deploy verify --run <run_id>` | local + GitHub API |
| Publish | `python -m edge_deploy publish-phase --run <run_id>` | bitbucket |
| Deploy | `python -m edge_deploy deploy --run <run_id> [--nodes …]` | bitbucket + edge |
| Tag GitHub | `python -m edge_deploy tag-github --run <run_id>` | github |
| Tag Bitbucket | `python -m edge_deploy tag-bitbucket --run <run_id>` | bitbucket |

The `release` wrapper chains phases until the first posture boundary, saves
state, prints the posture message above, and exits 0. Re-run `release --run
<run_id>` (or the individual phase command from `status`) after switching the
firewall.

During deploy, enter the RSA passcode in the controller tmux pane when prompted.
The progress heartbeat shows `>>> WAITING FOR OPERATOR - …` while waiting.

### 4. Complete

When both tag phases pass, the run is marked complete:

```text
release complete: <run_id>
```

Then mirror the core package tag if releasing `edge-deploy-core` itself:

```powershell
git push origin main "refs/tags/vX.Y.Z"
python -m edge_deploy mirror --tag vX.Y.Z
```

Mirror pushes the exact commit when Bitbucket accepts it; when Bitbucket's
own-commits hook rejects a GitHub-authored merge commit, it creates an
operator-authored mirror commit and tag carrying the identical tree (ADR-0007).

Record the redacted release attempt on Bitbucket's `release-log` branch (the
`tag-bitbucket` phase appends audit evidence). Update the pinned core version
in each tool through a normal GitHub pull request.

## Rollback

```powershell
python -m edge_deploy rollback --tag release-<UTC>-<short-sha>
```

Rollback creates a run with `kind=rollback` seeded from the tag's provenance,
then follows the same phase chain.

## Abandon

Close a run that will not be finished (recorded in the ledger and audit):

```powershell
python -m edge_deploy abandon --run <run_id> --reason "why this run stops"
```

## Artifacts

Each run directory holds `state.json`, `events.jsonl`, per-node
`pane-<node>.log` files, publish reports, rollout reports, and dependency
bundles. Bundles are content-addressed and never committed. A failed deploy
node can be retried with `deploy --run <run_id> --nodes <node>` without
re-running verify or publish when their ledger state is already `passed`.

## Rules

- Never move or reuse a published semantic-version tag.
- The `release-log` branch is Bitbucket-only and must never be pushed to GitHub.
- Dependency bundles follow the ADR-0006 contract (`v1.1.0`).
- Do not start a second release for a different SHA while an open run for the
  same tool remains unresolved.

See [docs/DESIGN.md](DESIGN.md) for module boundaries and
[docs/adr/0008-run-ledger-and-posture-phases.md](adr/0008-run-ledger-and-posture-phases.md)
for architecture decisions.
