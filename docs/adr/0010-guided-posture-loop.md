# Guided posture loop and cross-posture resume fix

## Context

ADR-0008 established the `release` wrapper that chains phases until the first
posture boundary, saves state, prints the required posture keys, and exits 0.
Operators then re-run `release --run <id>` (or the scoped phase command from
`status`) after manually switching the firewall.

Two gaps remained for single-command end-to-end releases:

1. **Resume defect**: `release --run <id>` always called `inspect_repository()`,
   which runs `git fetch origin main`. Resuming in Bitbucket-only posture (after
   verify passed in GitHub posture) failed before publish could start.

2. **Operator friction**: three manual re-invocations per release (one per
   posture switch) plus remembering to pass `--run`. ADR-0008 rejected
   mid-run auto-detection of posture changes; operators still need explicit
   confirmation at each boundary.

## Decision

1. **Cross-posture resume fix**: when resuming with `--run <id>` and the run's
   `verify` phase is already `passed` or `skipped`, skip `inspect_repository()`
   entirely. Reconstruct a minimal `RepositoryState` from the ledger's
   `source_sha` and the tool profile URLs for any code paths that still accept
   `repo_state`; no `git fetch` runs on such resumes.

2. **`release --guided`**: at each posture boundary, instead of exiting 0:
   - Print the required posture keys and unreachable endpoints when probing fails.
   - Prompt: `Switch firewall posture to [<keys>], then press Enter to continue...`
   - Re-probe with the existing `probe()`; loop until reachable or the operator
     aborts with Ctrl+C / EOF.
   - On abort, print the resume command (including `--guided`) and exit nonzero;
     the run stays `open`.
   - On full completion, print `release complete: <run-id>` and a status summary
     (same formatting as `status`).

3. **Default unchanged**: without `--guided`, ADR-0008's chain-until-boundary,
   exit-0 contract remains.

RSA prompting is unchanged: deploy forwards existing auth flags to
`AuthBroker`; guided mode adds no new auth surface.

## Considered options

- **Auto-detect posture from TCP success mid-run.** Rejected in ADR-0008; guided
  mode keeps the operator in the loop with an explicit Enter confirmation.
- **Keep calling `inspect_repository()` on every resume.** Rejected: breaks
  cross-posture resume and offers no value once verify is satisfied.

## Consequences

- One `release --guided` invocation can walk through all four posture switches
  when the operator confirms each boundary.
- `release --run <id>` is safe to resume from Bitbucket or edge posture after
  verify without GitHub reachability.
- E2E driver scripts can wrap `release --guided` for hands-off orchestration
  aside from posture switches and RSA prompts.
- Version 1.2.0 ships these behaviors; the loaded `__version__` proves the
  implementation on the controller.

## Amends / supersedes

- **Amends ADR-0008**: the default non-guided `release` chain-until-boundary
  contract is unchanged; `--guided` adds an operator-confirmed re-probe loop at
  boundaries instead of exiting 0.
- Does not supersede ADR-0008's rejection of mid-run auto-detection.
