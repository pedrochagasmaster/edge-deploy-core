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

Python 3.10 and 3.12 are tested in CI. Core declares only the `dev` extra;
there is no core `release` extra.

## Release Operator onboarding (zero state)

First-time operators use Windows PowerShell 5.1 on the controller. Connect
**both** the Bitbucket and Edge VPNs (`both-vpns`) before any clone or install,
and do not switch VPN or firewall posture during onboarding
([ADR-0013](docs/adr/0013-five-posture-capability-model.md),
[ADR-0017](docs/adr/0017-release-operator-onboarding.md)).

1. Bootstrap the approved immutable engine tag and install editable core:

```powershell
git clone https://github.com/pedrochagasmaster/edge-deploy-core.git
cd edge-deploy-core
git checkout v1.5.3
py -m pip install -e ".[dev]"
```

Use the tag equal to the package version (`v` + `__version__` /
`approved_engine_tag()`). Onboarding reuses this same checkout as `audit_repo`
via `bootstrap_core_root()`; it does not clone core again.

2. Prepare a **private** onboarding source YAML outside every Git repository
   (never commit it). Required: `operator_email`, `nodes`,
   `bitbucket_remotes.core`, and one `bitbucket_remotes.<tool>` entry per
   selected **canonical** tool (`autobench` and/or `robocop`). Optional:
   `checkout_root` only (defaults to `%USERPROFILE%\edge-deploy`). Selecting
   `--tool dispatch` still requires the `robocop` remote key — there is no
   `bitbucket_remotes.dispatch`. Put `BB_TOKEN` in the environment only. See
   [config.example.yaml](config.example.yaml) for the allowlisted shape with
   neutral placeholders.

3. Run onboarding:

```powershell
py -m edge_deploy onboard --config C:\secure\operator.yaml
```

Omit `--tool` to choose Autobench and/or Dispatch interactively, or select
explicitly (`dispatch` is a CLI alias that maps to canonical tool id
`robocop`):

```powershell
py -m edge_deploy onboard `
  --config C:\secure\operator.yaml `
  --tool autobench `
  --tool dispatch
```

Useful flags (see `py -m edge_deploy onboard --help`):

| Flag | Behavior |
|------|----------|
| `--root` | Checkout root (overrides private `checkout_root`) |
| `--check` | Rerun diagnostics without provisioning |
| `--restart` | Discard onboarding evidence only (keeps checkouts and private config) |
| `--restart --yes` | Confirm restart non-interactively (`--yes` is valid only with `--restart`) |

State and the redacted report live under `%APPDATA%\edge-deploy\`
(`onboarding-state.json`, `onboarding-report.json`). Training ledgers are
isolated under `%APPDATA%\edge-deploy\training\<tool>\` with both
`kind=training` and `training=true`; they are not real releases. Re-run the
same `onboard` command to resume; completed runs refresh the report without
re-practicing.

Edge Console launches against the training roots (`--root`) for ledger
rendering and against the selected real tool checkouts
(`--github-write-root`) for GitHub write probes. It shows a **simulated**
posture rail — do not switch workstation posture for it. Training roots may
lack git; divergence against them is intentionally soft. The console GitHub
write indicator is green only when every write-root's `git push --dry-run`
probe passes; **red in `both-vpns` is expected and is not an onboarding
failure**. A first real release is a separate boundary after onboarding
completes, for example:

```powershell
py -m edge_deploy release --guided --tool autobench
```

(That real guided release later needs one `both-vpns → firewall-off` switch for
`tag_github`; onboarding itself never requires it.)

## Operator configuration (legacy / manual)

Zero-state operators should use `onboard` above: it installs
`%APPDATA%\edge-deploy\config.yaml` from the private source. The manual copy
below is only for operators who are **not** using `onboard` and already have
checkouts and remotes prepared.

Copy [config.example.yaml](config.example.yaml) to:

```text
%APPDATA%\edge-deploy\config.yaml
```

Keep the real file private. `BB_TOKEN` remains an environment variable and
interactive RSA or Kerberos responses are never persisted.

Each tool repository contains `edge_deploy.yaml`, which describes only its
node-independent deployment contract.

## Release

From the clean GitHub `main` checkout of the **tool** being released (Autobench
or Dispatch/robocop). Tool repos declare a `release` extra that pins
`edge-deploy-core` ([ADR-0001](docs/adr/0001-standalone-operator-package.md));
install that tool checkout with:

```powershell
py -m pip install -e ".[dev,release]"
py -m pytest
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
(Paramiko as the default release transport),
[ADR-0017](docs/adr/0017-release-operator-onboarding.md) (Release Operator
onboarding).
