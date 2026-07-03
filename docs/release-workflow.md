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
