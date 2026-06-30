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

## CLI — `release` (the umbrella command)

One command publishes a reviewed Snapshot of a Tool (or **both**) and rolls it out to
every selected Edge Node, then verifies (drift + smoke). The Operator is prompted (via
`getpass`) only for the RSA passcode — once per node — and, for `--smoke deep`, a Kerberos
password.

```bash
# Publish + roll out both tools to both nodes, then verify (standard smoke):
py -m edge_deploy release --tool both --nodes 03,04

# Resume / re-deploy an existing Snapshot (skips Publish entirely):
py -m edge_deploy release --tool robocop --nodes 04 --snapshot <snapshot-sha>

# Deep smoke (robocop's Impala scenario needs Kerberos), stop on first failure:
py -m edge_deploy release --tool robocop --smoke deep --fail-fast
```

Flags: `--tool {autobench|robocop|both}` (required), `--nodes 03,04` (default: all
configured; accepts `03` or `node03`), `--snapshot <sha>` (skip Publish), `--smoke
{standard|deep}`, `--fail-fast`, `--report-dir <path>` (default
`./edge-deploy/reports/release-<UTC>/`), `--max-auth-attempts N` (default 3).

Each run writes a detailed per-(tool×node) `OperationReport` JSON plus a consolidated
`release.json` (`edge-deploy/release/1`) with `summary.counts`, `summary.handoffs[]`
(ready-to-paste resume commands), and `summary.overall`. The process exit code is non-zero
if any Rollout failed/refused or any Publish failed (ADR-0003).

> `release --snapshot <sha>` *reuses* an existing Snapshot; the SHA must be present in the
> tool's local working copy so Drift can read its tree (the run fetches and, if still
> missing, emits a clear snapshot handoff — see Phase-2 plan Risk #1). This is the opposite
> of `publish --commit <sha>`, which *creates* a new Snapshot from that source.

## CLI — `publish` (standalone, per tool)

```bash
py -m edge_deploy publish --tool autobench                 # gate (clean tree + on release_branch + local_check.ps1) -> push Snapshot
py -m edge_deploy publish --tool robocop --commit <sha>    # name a reviewed source commit (relaxes tree/branch gate)
py -m edge_deploy publish --tool autobench --no-local-check # escape hatch: bypass the local_check.ps1 gate
```

`BB_TOKEN` (env) authenticates the push as a Bearer token; it is never written to a report
(ADR-0002). The Snapshot is built with `git commit-tree` (no working-tree mutation) and its
message is `Deploy snapshot: <tool> <source-short> on <branch> (<YYYY-MM-DD HH:mm>) [edge-deploy]`.

## CLI — lower-level Phase-1 commands

```bash
py -m edge_deploy preflight --node node03
py -m edge_deploy rollout   --tool robocop --node node03 --commit <snapshot-sha> --install auto
py -m edge_deploy drift     --tool robocop --node node03 --commit <snapshot-sha>
```

`--tool` / `--node` resolve against the two config layers.

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
