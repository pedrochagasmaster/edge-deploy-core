# edge-deploy-core

Operator-only Python package for publishing reviewed Autobench and Dispatch
commits to corporate Bitbucket and deploying them to Mastercard Hadoop Edge
Nodes.

Normal development happens on GitHub and ends with a pull request. Contributors
do not need Bitbucket, Edge access, SSH, Kerberos, or RSA credentials. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```powershell
uv sync --extra dev
uv run pytest
```

Python 3.10 and 3.12 are tested in CI. Core declares only the `dev` extra;
there is no core `release` extra.

## Release Operator onboarding (zero state)

A Remotion motion tutorial walks the same path:
[release-operator-onboarding/](release-operator-onboarding/) (`npm run dev` to
preview, or render composition `ReleaseOperatorOnboarding`).

First-time operators use Windows PowerShell 5.1 on the controller. Connect
**both** the Bitbucket and Edge VPNs (`both-vpns`) before any clone or install,
and do not switch VPN or firewall posture during onboarding
([ADR-0013](docs/adr/0013-five-posture-capability-model.md),
[ADR-0017](docs/adr/0017-release-operator-onboarding.md)).

1. Bootstrap the approved immutable engine tag and install editable core:

```powershell
git clone https://github.com/pedrochagasmaster/edge-deploy-core.git
cd edge-deploy-core
git checkout v1.5.4
uv sync --extra dev
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

Edge Console launches `--read-only` against the training roots (`--root`) for
ledger rendering and against the selected real tool checkouts
(`--github-write-root`) for GitHub write probes. It shows a **simulated**
posture rail — do not switch workstation posture for it — and no command
buttons. Training roots may
lack git; divergence against them is intentionally soft. The console GitHub
write indicator is green only when every write-root's authenticated, empty
`git-receive-pack` POST passes. The probe sends no update commands and changes
no refs; **red in `both-vpns` is expected and is not an onboarding failure**.
A first real release is a separate boundary after onboarding completes, for
example:

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

## Edge Console

A local web console over the same commands
([ADR-0018](docs/adr/0018-console-orchestrated-release.md)). Launch it from
this checkout, watching the tool checkouts you release from:

```powershell
py -m edge_console --root D:\autobench --root D:\robocop
```

It opens `http://127.0.0.1:7643/` and shows:

- **whatever is open or live**, spotlighted: the run's rail through the five
  postures, per-node deploy state, live transfer progress, and one button per
  remaining command — `release --guided`, the next phase on its own, `status`,
  `abandon`;
- **a release decision card** for every checkout with no run in flight: the
  verdict, the deployed / checkout / GitHub-main commits it rests on, a
  precondition checklist with `git pull` / `git push` / `preflight` /
  `transport-smoke` buttons, and one "Start guided release" call to action;
- **closed runs**, in a collapsed history section.

Buttons run the exact command shown beside them, in that checkout, and stream
the engine's output. When the engine stops for the operator — the RSA passcode,
the guided posture acknowledgement, a `[y/N]` gate — the console shows the
prompt and relays your answer. Secrets go straight to the running process and
are masked in the transcript; they are never stored.

Two cases still need a terminal: a deep-smoke release (`--smoke deep`, the only
thing that asks for a Kerberos password) has no console action, and a node
configured `transport: pane` takes its RSA passcode in the attached tmux pane,
so the console can only show that it is waiting. Releasing a run whose lock was
left behind by a dead process also needs a terminal — the console will not
steal a lock.

**Start the console from a shell that has `BB_TOKEN` set.** Every command
inherits the console process's environment, so exporting it later, or in
another window, does not reach the buttons; the console says so rather than
letting publish discover it. The same applies to the interpreter: buttons run
`--engine-python` (default: the console's own), and if that is not the engine
you would get in your own terminal, runs created from one side are refused by
the other on Engine Identity ([ADR-0008](docs/adr/0008-run-ledger-and-posture-phases.md)).
The console reads the identity from the interpreter it will actually spawn and
flags any open run that does not match.

Changing the workstation firewall posture stays manual. The console names the
posture a phase needs and waits for you to confirm the switch; it never makes
one.

| Flag | Behavior |
|------|----------|
| `--root` | Tool checkout to watch; repeat for several (default: cwd) |
| `--github-write-root` | Checkout(s) used for GitHub write probes (default: same as `--root`) |
| `--read-only` | Serve the dashboard with every command button disabled |
| `--demo` | Fabricated checkouts driven by an offline simulator; no network |
| `--engine-python` | Interpreter for `python -m edge_deploy` (default: the console's own) |
| `--port`, `--no-browser` | Listen port (default 7643); skip opening a browser |

`--demo` needs no operator config, network, or credentials, and walks the whole
guided release including the RSA prompts and the firewall-off boundary:

```powershell
py -m edge_console --demo
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
onboarding), [ADR-0018](docs/adr/0018-console-orchestrated-release.md)
(console-orchestrated release).
