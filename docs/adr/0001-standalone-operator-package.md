# edge-deploy-core is a standalone operator-machine package, not vendored per tool

The toolkit executes entirely on the operator's Windows machine (local
tmux/psmux pane, local git, SSH); the air-gapped Edge Nodes only ever run each
tool's own `update.sh`/`install.sh` plus inline base64'd Python snippets sent
over the pane, so they never import this package. We therefore distribute
`edge-deploy-core` as a standalone package cloned and `pip install -e`'d on the
operator machine. Each **Tool** repo commits only a small `edge_deploy.yaml`
**Tool Profile**; no library code is vendored into the tools.

## Considered Options

- **Git subtree into each tool repo.** Initially chosen on the assumption the
  code had to ship to the node. Rejected once we confirmed the node never runs
  it: subtree adds duplicate copies, a perpetual `git subtree pull` sync chore,
  and forces the `--tool both` umbrella runner to live awkwardly inside one
  designated tool repo.
- **Versioned wheel on the operator machine.** Viable, but heavier than needed
  during early iteration; revisit if we want pinned, reproducible operator
  installs later.

## Consequences

- Single source of truth; no cross-repo code sync.
- `py -m edge_deploy release --tool both` runs from one place, reading each
  tool's working-copy path and Tool Profile.
- Tools stay almost untouched: only a small committed profile is added.
- The operator machine becomes a required, configured environment (git, SSH,
  tmux/psmux, BB_TOKEN, the tools' working copies).
