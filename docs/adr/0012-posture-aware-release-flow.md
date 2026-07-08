# Posture-aware release flow

> Posture model refined by
> [ADR-0013](0013-five-posture-capability-model.md): GitHub read works in
> every posture and the VPNs are independent, so the "two switches" here is
> now one mandatory switch when starting in both-vpns. The probe mechanics,
> settle loop, retries, and tag ordering decided here stand unchanged.

## Context

The workstation firewall enforces an exclusive Posture: at most one of GitHub
or Bitbucket-plus-Edge is writable at a time, everything crosses a corporate
proxy, and a posture switch takes up to about a minute to propagate.

The 2026-07-03 release and rollback validations showed three recurring failure
modes in the engine (all worked around operator-side at the time):

1. **TCP probes lie.** The proxy accepts TCP connects to `github.com:443` and
   `scm.mastercard.int:443` in *every* posture and only fails at the HTTP
   layer (503). The engine's posture gate therefore waved phases through in
   the wrong posture, and the failure surfaced later as a raw
   `CalledProcessError` from `git push`. There is also a read-only GitHub
   posture in which reads succeed but pushes 503, which no reachability-style
   probe can distinguish.
2. **Post-switch flakiness.** The first remote operations after a genuine
   posture switch can still fail transiently (HTTP 503, `git ls-remote` exit
   128) while the change propagates. One such flake crashed the whole guided
   run even though an immediate retry passed.
3. **One switch too many.** The phase order verify → publish → deploy →
   tag_github → tag_bitbucket required github → bitbucket → github →
   bitbucket: three switches per release, each one an opportunity for the two
   failure modes above.

## Decision

1. **Protocol-level posture probes.** Git endpoints are probed with the
   protocol and access direction the phase actually uses
   (`posture.PHASE_GIT_PROBES`):
   - read: `git ls-remote <remote> HEAD` (negotiates git-upload-pack);
   - write: `git push --dry-run --force <remote> HEAD:refs/edge-deploy/posture-probe`
     (negotiates git-receive-pack — the real write path — without sending a
     pack or updating any ref; `--force` plus an explicit destination keeps
     detached HEAD or a stale local branch from masquerading as a posture
     failure).
   TCP probing remains for Edge SSH endpoints, and as the fallback when no
   checkout is available to run git in (`repo_root=None`). Probe runs never
   prompt (`GIT_TERMINAL_PROMPT=0`) and time out after 20 s.
2. **Guided mode absorbs posture transients.** After the operator confirms a
   switch, the guided gate polls the probes for up to ~90 s before
   re-prompting (posture settling). If a phase then still raises
   `CalledProcessError` or `PostureError`, guided mode retries the phase with
   10/20/30 s backoff before giving up. This is safe because phases are
   idempotent and ledger-guarded; unguided invocations still fail fast.
3. **Tag order cut to two switches.** `tag_bitbucket` now runs directly after
   `deploy` (still in bitbucket+edge posture) and `tag_github` runs last:
   github → bitbucket → github, two switches per release. The release tag is
   minted by whichever tag phase runs first and recorded in its evidence; the
   other phase reuses it. The run completes when both tag phases have passed,
   in either order, so runs opened under the old order still resolve. The
   audit attempt is still appended during `tag_bitbucket`, which is where the
   Bitbucket `release-log` branch is writable.

## Consequences

- The posture gate now proves write capability, not just reachability, so
  wrong-posture failures surface at the gate with a clear message instead of
  mid-phase as a raw git error.
- A release needs two posture switches instead of three; a rollback still
  needs only bitbucket+edge until its final `tag_github`-equivalent step.
- Probes cost one or two git round-trips (~1–3 s) per phase entry.
- `tag_bitbucket` no longer requires `tag_github` to have passed; immutable
  tag-name equality across remotes is still enforced by each phase verifying
  the pushed, dereferenced SHA against the run ledger.
