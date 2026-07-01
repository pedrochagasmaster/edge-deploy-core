# v1 covers the git-update path; Rollout refuses on dependency changes

Offline wheels do not travel in git (autobench ignores `/offline_packages/*`;
robocop commits no `.whl` and ignores its deploy zip), so a Bitbucket
The published source commit + `update.sh` cannot deliver new or upgraded dependencies — the air-
gapped node's `install.sh` installs only from wheels already present from a prior
`deploy_and_install.ps1`. Therefore v1 of edge-deploy-core covers the git-update
path only. When a release commit's changed-paths diff touches `requirements.txt` or
`constraints.txt`, the **Rollout** is refused (not warned) with guidance to run
the offline bundle refresh first, so we never start an offline `install.sh` that
fails halfway.

## Considered Options

- **Automate the offline wheel build + SCP in the pipeline.** Deferred to a later
  phase; it's the heavier, rarer, air-gap-specific path.
- **Warn but still attempt `install.sh`.** Rejected: leaves the node in a broken,
  half-installed state on the very change most likely to break.

## Consequences

- The engine must compute the changed-paths diff before rolling out (robocop's
  `_remote_changed_paths` already does this) and classify dependency changes.
- Dependency releases remain a deliberate two-step Operator action for now.
