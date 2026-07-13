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
py -m edge_deploy status
py -m edge_deploy release --tool autobench
```

Each release creates a durable **run** under `edge-deploy/runs/`. Phases are
short, idempotent commands (`verify`, `publish-phase`, `deploy`, `tag-github`,
`tag-bitbucket`) that declare which firewall posture they need. Use `status` to
see per-phase state and the exact next command.

```powershell
py -m edge_deploy rollback --tag release-<UTC>-<short-sha>
```

Successful tool releases receive an immutable `release-<UTC>-<short-sha>` tag on
GitHub and Bitbucket. Redacted release bundles are appended to the Bitbucket-only
`release-log` branch of this repository.

Remote work runs over `transport.py` and `ssh_transport.py`: a persistent,
digest-verified Paramiko SSH connection per node by default
(`transport: ssh`), with the local tmux/psmux pane kept as an explicit
per-node recovery override (`transport: pane`), not a universal channel.

See [docs/release-workflow.md](docs/release-workflow.md) for the operator
procedure and [docs/DESIGN.md](docs/DESIGN.md) for engine internals. Architecture
decisions: [ADR-0008](docs/adr/0008-run-ledger-and-posture-phases.md) (run
ledger and phases), [ADR-0009](docs/adr/0009-on-node-runner-file-evidence.md)
(runner and file evidence), [ADR-0013](docs/adr/0013-five-posture-capability-model.md)
(five-posture capability model), [ADR-0014](docs/adr/0014-paramiko-release-transport.md)
(Paramiko as the default release transport).
