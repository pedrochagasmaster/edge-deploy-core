# edge-deploy-core

Shared deployment toolkit that rolls a reviewed build of a **Tool** (autobench,
robocop/Dispatch) out to the Mastercard Hadoop **Edge Nodes**, automating everything
except the **Operator's** interactive auth.

Everything runs on the Operator's Windows machine: local git, a local tmux/psmux pane,
and SSH. The air-gapped Edge Node only ever runs each Tool's own `update.sh` / `install.sh`
plus tiny inline Python sent over the **Authenticated Pane**. The node never imports this
package. See [`CONTEXT.md`](CONTEXT.md), [`docs/DESIGN.md`](docs/DESIGN.md) and the ADRs
under [`docs/adr/`](docs/adr/) for the language and the decisions.

## Install (operator machine)

```bash
py -m pip install -e .
# optional dev tooling (tests + lint)
py -m pip install -e ".[dev]"
```

Confirm the package imports:

```bash
py -c "import edge_deploy; print(edge_deploy.__version__)"
```

## Configuration — two layers

1. **Operator config** (`~/.edge-deploy/config.yaml`) — Edge Node inventory + per-tool
   working-copy paths. Copy [`config.example.yaml`](config.example.yaml) to get started.
2. **Tool Profile** (`edge_deploy.yaml`, committed in each Tool's repo) — tool-specific,
   node-independent deploy data: `runtime_paths`, `install_trigger_paths`,
   `dependency_paths`, `sensitive_paths`, smoke commands, `tui_chrome_regex`, `tui_exit`,
   Bitbucket URL and release branch.

`BB_TOKEN` stays an environment variable and is never written to disk or reports.

## CLI (Phase 1 surface)

```bash
py -m edge_deploy preflight --node node03
py -m edge_deploy rollout   --tool robocop --node node03 --commit <snapshot-sha> --install auto
py -m edge_deploy drift     --tool robocop --node node03 --commit <snapshot-sha>
```

`--tool` / `--node` resolve against the two config layers. The umbrella `release`
orchestrator (publish + fan-out + auth seam) is Phase 2.

## On-node interface (ADR-0004)

Both tools' `update.sh` / `install.sh` read one namespace:
`EDGE_DEPLOY_REMOTE`, `EDGE_DEPLOY_BRANCH`, `EDGE_DEPLOY_PYTHON_BIN`, and
`EDGE_DEPLOY_EMAIL` (optional; only robocop consumes it).

## Safety

- **Refuse on dependency change (ADR-0005):** if a Snapshot's changed paths touch a
  Tool's `dependency_paths` (e.g. `requirements.txt`), the Rollout is *refused* before any
  `update.sh` runs — offline wheels do not travel in git.
- **Sensitive paths (ADR-0003 / Round 12):** changes touching `sensitive_paths` are
  *flagged* (`sensitive_changed`) but never block a Rollout.
- **Redaction (ADR-0002):** `passcode=` / `password=` / `token=` values are masked in all
  written reports and printed output.
