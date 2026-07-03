# Architecture

`edge-deploy-core` runs only on the Release Operator’s machine. Edge Nodes run
each Tool’s own `update.sh` and `install.sh`; they never import this package.

## Inputs

- Current Tool checkout containing `edge_deploy.yaml`
- Private `%APPDATA%\edge-deploy\config.yaml`
- `BB_TOKEN` environment variable
- Interactive RSA and, when required, Kerberos credentials

The Tool Profile owns runtime paths, install triggers, smoke commands, remote
URLs, and on-node paths. Operator configuration owns identity, node inventory,
and the local core checkout used for private audit writes.

## Release flow

Releases are **run-ledger state machines** (ADR-0008) advanced by posture-scoped
phase commands:

1. Create or resume a run under `edge-deploy/runs/<run-id>/`.
2. `verify` — clean local `main`, GitHub CI, pytest (once per SHA unless
   `--reverify`).
3. `publish-phase` — tree-equivalent snapshot to Bitbucket `main` (ADR-0007).
4. `deploy` — per node: auth broker → dependency bundle → checkout update →
   install → smoke → drift (ADR-0009 runner file evidence).
5. `tag-github` / `tag-bitbucket` — immutable release tags + audit append.

`status` prints phase state and the exact next command including required
firewall posture. The `release` wrapper chains phases until a posture boundary.

No phase mutates a remote when preconditions fail. Publish never rewrites
commits or force-pushes. An open run blocks a new release for the same tool
until continued (`--run`), abandoned, or completed.

## Boundaries

- `ledger.py`: durable run state, lock, engine identity
- `posture.py`: phase endpoint declarations and TCP probes
- `phases/`: verify, publish, deploy, tag, status subcommands
- `runner.py`: on-node step executor and D8 read protocol
- `repository.py`: canonical checkout and GitHub CI gates
- `publish.py`: exact-SHA, fast-forward-only Bitbucket Publish
- `auth.py`: `AuthBroker` interactive secret boundary (deploy only)
- `release.py`: per-node deploy orchestration inside the deploy phase
- `audit.py`: isolated append-only Bitbucket audit worktree
- `reporting.py`: redacted machine-readable evidence
- `config.py`: operator and Tool Profile contracts

Run artifacts (reports, bundles, pane logs) live under the run directory.
Audit synchronization uses an isolated temporary worktree and the explicit
refspec `HEAD:refs/heads/release-log` against the `bitbucket` remote.

See [adr/0008-run-ledger-and-posture-phases.md](adr/0008-run-ledger-and-posture-phases.md)
and [adr/0009-on-node-runner-file-evidence.md](adr/0009-on-node-runner-file-evidence.md).
