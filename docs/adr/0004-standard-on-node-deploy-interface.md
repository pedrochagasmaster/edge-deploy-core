# Standard EDGE_DEPLOY_* interface for on-node update.sh/install.sh

Both tools' `update.sh`/`install.sh` are converged onto one environment-variable
interface so the engine invokes every **Tool** identically and the **Tool
Profile** carries no install command or env-name mapping. The standard names are
`EDGE_DEPLOY_REMOTE`, `EDGE_DEPLOY_BRANCH`, `EDGE_DEPLOY_PYTHON_BIN`, and
`EDGE_DEPLOY_EMAIL` (optional; only robocop consumes it). Each tool's existing
names (`AUTOBENCH_*`, `DISPATCH_*`) are kept as fallback aliases for one release
to keep the transition safe.

## Considered Options

- **Describe each tool's existing interface in its Tool Profile.** Lower blast
  radius (scripts untouched) but keeps per-tool branching and a fatter profile;
  rejected as the opposite of the full-convergence goal.

## Consequences

- Editing both repos' `update.sh`/`install.sh` and their docs is in scope.
- The Tool Profile shrinks to paths, smoke, TUI chrome, Bitbucket URL, branch,
  and sensitive paths.
- The alias fallback must be removed deliberately in a later release, not left
  forever. Concrete trigger: remove once **both** tools have completed one
  successful Release on **both** nodes via the `EDGE_DEPLOY_*` interface, in the
  next change, updating each tool's deploy docs.
