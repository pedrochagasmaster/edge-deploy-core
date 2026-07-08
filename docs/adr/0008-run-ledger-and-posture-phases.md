# Run ledger and posture-scoped release phases

> Posture model refined by
> [ADR-0013](0013-five-posture-capability-model.md): the firewall has five
> states, not two exclusive ones. The run ledger and phase decisions here
> stand unchanged.

## Context

Three codex release sessions (2026-07-01 → 2026-07-03) attempted full end-to-end
releases. Failures included: cross-posture tag pushes half-succeeding then
hanging; silent `getpass()` waits mistaken for stuck remote work; implicit
resume picking up a stale report directory and deploying the wrong snapshot;
concurrent controllers colliding without coordination; and repeated 4-minute test
suites on every retry because verification was not recorded durably.

The environment requires **exclusive firewall postures** (GitHub vs Bitbucket +
edge), **human RSA authentication per SSH connection**, and **Windows tmux
pane transport** without working OpenSSH multiplexing.

## Decision

1. **Run ledger** (`edge_deploy/ledger.py`): every release attempt creates
   `edge-deploy/runs/<run-id>/` before any remote mutation. `state.json`
   records tool, source SHA, engine identity, per-phase/per-node status, and
   run-level `open | complete | abandoned`. `events.jsonl` is append-only.
   `run.lock` (PID + hostname) prevents concurrent controllers.

2. **Explicit resume only**: starting a bare `release` while an open run exists
   is refused with the three legal options (`--run`, `abandon`, or scoped
   deploy). The old `--resume <report-dir>` path is deleted.

3. **Posture-scoped phases**: `verify`, `publish-phase`, `deploy`, `tag-github`,
   and `tag-bitbucket` are separate subcommands. Each declares required TCP
   endpoints and fails fast with a named posture error when unreachable. The
   `release` wrapper chains phases until the first posture boundary, saves
   state, and exits 0.

4. **Engine identity**: every phase entry compares the ledger's
   `engine.content_sha256` to the running process and refuses skew.

5. **`status` command**: prints per-phase state and the exact next command plus
   required posture keys.

## Considered options

- **Keep monolithic `release` with implicit resume.** Rejected: caused wrong-
  snapshot deploys and opaque recovery.
- **Auto-detect posture from failures mid-run.** Rejected: operators discovered
  posture problems minutes into deploy; fail-fast at phase entry is cheaper.

## Consequences

- Operators switch firewall posture between phases using `status` output.
- Partial deploy is visible in the ledger; per-node retry via
  `deploy --run <id> --nodes …` without re-verifying unchanged SHAs.
- Report artifacts live under the run directory, not `edge-deploy/reports/`.
- Abandon is a first-class, audited terminal state.

## Amends / supersedes

- **ADR-0003** (continue-and-report): fan-out semantics unchanged; state now
  derives from the ledger as well as consolidated reports.
- **ADR-0002** (operator auth seam): auth ownership moves to deploy-only via
  `AuthBroker` (see PR-18); preflight no longer authenticates nodes.
- Implicit report-directory resume described in pre-v1.1.0 operator docs is
  superseded by this ADR.
