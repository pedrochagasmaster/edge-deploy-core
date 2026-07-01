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

1. Require clean local `main` exactly equal to GitHub `origin/main`.
2. Verify expected `origin` and `bitbucket` URLs.
3. Require successful post-merge GitHub CI for the exact SHA.
4. Run `python -m pytest` locally.
5. Require a reachable, synchronized Bitbucket audit branch.
6. Authenticate every selected Edge Node.
7. Fast-forward the exact source SHA to Bitbucket `main`.
8. Deploy and verify each Edge Node.
9. Append the redacted report bundle to Bitbucket-only `release-log`.
10. After complete success, create the same immutable release tag on GitHub and
    Bitbucket.

No preflight failure mutates a remote. Publish never rewrites commits or
force-pushes. A partial Deploy is an unresolved release for that SHA; another
SHA is blocked until retry or explicit Rollback resolves it.

## Boundaries

- `repository.py`: canonical checkout and GitHub CI gates
- `publish.py`: exact-SHA, fast-forward-only Bitbucket Publish
- `auth.py`: interactive secret boundary
- `release.py`: per-node Deploy and verification orchestration
- `audit.py`: isolated append-only Bitbucket audit worktree
- `reporting.py`: redacted machine-readable evidence
- `config.py`: operator and Tool Profile contracts

Reports are written locally first. Audit synchronization uses an isolated
temporary worktree and the explicit refspec
`HEAD:refs/heads/release-log` against the `bitbucket` remote. Failed audit
pushes remain in the private operator outbox and block later releases.
