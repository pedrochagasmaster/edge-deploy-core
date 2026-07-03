# Release Operator Workflow

Only a Release Operator publishes `edge-deploy-core`.

1. Update a clean local `main` from GitHub with `git pull --ff-only origin main`.
2. Confirm `HEAD` equals `origin/main` and its post-merge GitHub CI succeeded.
3. Install and test:

   ```powershell
   python -m pip install -e ".[dev]"
   python -m pytest -n 4 --dist loadfile
   ```

4. Create the next immutable semantic-version tag from that exact commit.
5. Push the commit and tag to `origin`, then switch firewall/network posture and
   mirror them to Bitbucket:

   ```powershell
   git push origin main "refs/tags/vX.Y.Z"
   python -m edge_deploy mirror --tag vX.Y.Z
   ```

   Mirror pushes the exact commit when Bitbucket accepts it; when Bitbucket's
   own-commits hook rejects a GitHub-authored merge commit, it creates an
   operator-authored mirror commit and tag carrying the identical tree (ADR-0007)
   and verifies the pushed refs.
6. Record the redacted release attempt on Bitbucket’s `release-log` branch.
7. Update the pinned core version in each tool through a normal GitHub pull
   request.

The dependency-delivery package contract is `v1.1.0`. Never move or reuse a published
tag. The `release-log` branch is Bitbucket-only and must never be pushed to GitHub.

Dependency bundles are generated under the release report directory and are never
committed. Per-node reports record build, transfer, verification, installation, and
resume provenance. A failed dependency phase is resumed from the original report
directory so the reviewed source SHA remains available.

## Interactive Tool release authentication

Run `edge_deploy release` in a dedicated tmux controller whenever
`--auth-mode prompt` is selected. The authentication prompt belongs to the
controller process, not to the per-node SSH panes.

```powershell
tmux new-session -d -s edge-release-pr35 -c D:\Projects\autobench
$releaseCommand = '$env:PYTHONPATH=''D:\Projects\edge-deploy-core''; $env:EDGE_DEPLOY_SSH_MULTIPLEX=''0''; $stamp=(Get-Date).ToUniversalTime().ToString(''yyyyMMddTHHmmssZ''); $env:EDGE_DEPLOY_PR35_REPORT="D:\Projects\autobench\edge-deploy\reports\release-$stamp-pr35-localcore"; py -m edge_deploy release --auth-mode prompt --report-dir $env:EDGE_DEPLOY_PR35_REPORT'
tmux send-keys -t edge-release-pr35 -l $releaseCommand
tmux send-keys -t edge-release-pr35 Enter
tmux attach -t edge-release-pr35
```

Enter each fresh RSA PASSCODE only at `[nodeNN] Enter RSA PASSCODE:`. It is
forwarded transiently to the per-node SSH pane and must never be copied into
logs, reports, shell history, or config.

## Tool release tag finalization

The `edge_deploy release` and rollback resume paths run while the operator can
reach Edge Nodes and Bitbucket. On this machine that network posture cannot also
push GitHub tags. A passed release therefore creates the annotated release tag
locally and writes `push-release-*.ps1` in the report directory instead of
pushing remote tags inline.

Finalize tool tags in two explicit phases:

```powershell
# Phase 1: switch to GitHub access.
git push origin refs/tags/release-<UTC>-<short-sha>
git ls-remote --tags origin refs/tags/release-<UTC>-<short-sha>

# Phase 2: switch to Bitbucket/Edge access.
# Use the exact Bitbucket command from push-release-*.ps1. Rollback/mirror
# releases may require a temporary edge-deploy-mirror/* tag because the
# Bitbucket tag targets the deployed snapshot commit, not the GitHub source
# commit.
```

Do not rerun a completed rollout just to retry a blocked GitHub tag push.
