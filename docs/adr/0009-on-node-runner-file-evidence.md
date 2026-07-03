# On-node runner and file-based remote evidence

## Context

Release session failures showed that **tmux `capture-pane` text is not a data
transport**: pane width wraps JSON and base64 mid-token, causing
`JSONDecodeError` after otherwise successful remote staging. Separate SSH/scp
connections on Windows demanded a second RSA passcode and hit broken
`ControlMaster` behavior. Dependency staging and rollout steps printed marker
blocks (`DEPENDENCY_STAGE_*`, `PERMISSION_PAYLOAD`, `DRIFT_PAYLOAD`) into the
pane for the controller to scrape.

## Decision

1. **On-node runner** (`edge_deploy/runner.py`): a versioned POSIX shell script
   uploaded once per node session. Each step writes structured results to
   `~/.edge-deploy/runs/<run-id>/steps/<step>.json` (and optional `.out` tail).

2. **D8 wrap-immune read protocol**: the controller retrieves small remote files
   by sending a fixed command that prints base64 + sha256 markers; whitespace is
   stripped from the captured pane span before decode and digest verification.
   This is the only sanctioned way to move structured data node → controller.

3. **Verified pane upload** (`TmuxDriver.upload_file`): chunked base64 transfer
   with local/remote sha256 pre-check reuse and post-upload verification. No
   scp/ControlMaster path.

4. **Runner-owned install shim** (runner v2): `PIP_NO_INDEX`, `PIP_FIND_LINKS`,
   and `EDGE_DEPLOY_BUNDLE_DIR` are exported inside the runner `install` step,
   not by `rollout.py` string injection.

5. **Pane logging** (`pipe-pane`): full-fidelity local logs at
   `runs/<run-id>/pane-<node>.log` when the tmux backend supports it.

6. **Screen scraping demoted**: the pane is for human display and prompt
   detection only. Marker-based evidence parsing is removed from dependency
   delivery and rollout.

## Considered options

- **Widen tmux panes and parse harder.** Rejected: wrapping is environmental;
  parsing remains fragile.
- **Keep ControlMaster with a fallback flag.** Rejected: the Windows path is
  permanently broken; deleting it avoids zombie branches.

## Consequences

- `dependencies.py` and `rollout.py` thread `run_id` and use `bootstrap_runner`
  + `run_step` + `read_remote_json` / `read_remote_text`.
- Fake drivers in tests serve wrapped D8 screens to prove wrap resilience.
- Runner version bumps re-upload automatically (path embeds version + digest).

## Known remainder

- **`drift.py`** still emits and parses `DRIFT_PAYLOAD_*` markers from pane
  output. Convert to a runner step in a follow-up; until then drift checks
  remain the one production screen-scrape data path.

## Amends / supersedes

- **ADR-0006** § transfer mechanism: "authenticated SSH control connection" is
  replaced by verified pane upload + runner steps; bundle identity and staging
  layout are unchanged.
- **ADR-0005** § changed-paths diff: preflight still refuses dependency changes,
  but the diff is now read via runner file evidence in rollout, not pane
  payloads.
- **ADR-0004** on-node deploy interface: update/install commands are invoked
  through the shipped runner; tools still own `update.sh`/`install.sh` behavior
  but the engine no longer depends on snapshot-local env injection in
  `rollout.py`.
