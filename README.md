# edge-deploy-core

Operator-only Python package for publishing reviewed Autobench and Dispatch
commits to corporate Bitbucket and deploying them to Mastercard Hadoop Edge
Nodes.

Normal development happens on GitHub and ends with a pull request. Contributors
do not need Bitbucket, Edge access, SSH, Kerberos, or RSA credentials. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

Python 3.10 and 3.12 are tested in CI.

## Operator configuration

Copy [config.example.yaml](config.example.yaml) to:

```text
%APPDATA%\edge-deploy\config.yaml
```

Keep the real file private. `BB_TOKEN` remains an environment variable and
interactive RSA or Kerberos responses are never persisted.

Each tool repository contains `edge_deploy.yaml`, which describes only its
node-independent deployment contract.

## Release

From the clean GitHub `main` checkout of the tool being released:

```powershell
python -m pip install -e ".[dev,release]"
python -m pytest
python -m edge_deploy release
```

A **Publish** fast-forwards the verified GitHub commit to Bitbucket. A
**Deploy** applies that exact commit to Edge. A **Release** performs both under
operator control. A **Rollback** explicitly restores a previously recorded
successful release.

```powershell
python -m edge_deploy rollback --tag release-<UTC>-<short-sha>
```

The release command validates repository state, remotes, post-merge GitHub CI,
local tests, audit availability, and interactive authentication before remote
mutation. Successful tool releases create an immutable local
`release-<UTC>-<short-sha>` tag and write a `push-release-*.ps1` handoff in the
report directory. Operators push that tag to GitHub and Bitbucket as separate
network phases, because this environment can reach either GitHub or
Bitbucket/Edge, but not both at the same time.

Redacted release bundles are appended to the Bitbucket-only `release-log`
branch of this repository. See [docs/release-workflow.md](docs/release-workflow.md)
for the operator procedure and [docs/DESIGN.md](docs/DESIGN.md) for engine
internals.
