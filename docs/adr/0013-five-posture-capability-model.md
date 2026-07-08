# Five-posture capability model

## Context

ADR-0008/0010/0012 modeled Posture as *exclusive*: at most one of GitHub or
Bitbucket-plus-Edge writable, "no posture allows writing to both", and a
release therefore needing two switches (github → bitbucket+edge → github).
That was an approximation. The workstation firewall actually has five states:

| Posture       | GitHub read | GitHub write | Bitbucket | Edge |
|---------------|-------------|--------------|-----------|------|
| baseline      | yes         | no           | no        | no   |
| edge-vpn      | yes         | no           | no        | yes  |
| bitbucket-vpn | yes         | no           | yes       | no   |
| both-vpns     | yes         | no           | yes       | yes  |
| firewall-off  | yes         | yes          | no        | no   |

Two facts the exclusive model got wrong:

1. **GitHub read works everywhere.** The "read-only GitHub posture" ADR-0012
   observed is not one odd state — it is every posture except firewall-off.
   `verify` (which only reads GitHub) is therefore never posture-blocked.
2. **Bitbucket and Edge are independent VPNs.** They can be held separately
   (bitbucket-vpn, edge-vpn) or together (both-vpns); `deploy` needs both,
   but `publish` and `tag_bitbucket` are satisfied by bitbucket-vpn alone.

What the old model got right survives: no posture grants GitHub write
together with any corporate access, so the wall between GitHub-write and
Bitbucket/Edge is real; and a switch still propagates slowly, so the settle
loop and guided retries of ADR-0012 stay.

## Decision

1. **Name the five postures in the engine.** `posture.POSTURES` maps each
   posture to its granted capabilities (`github-read`, `github-write`,
   `bitbucket`, `edge`); `posture.PHASE_CAPABILITIES` maps each phase to the
   capabilities it needs. The satisfying postures are derived
   (`postures_satisfying`), never hand-maintained per phase:

   | Phase         | Needs             | Satisfied by               |
   |---------------|-------------------|----------------------------|
   | verify        | github-read       | any posture                |
   | publish       | bitbucket         | bitbucket-vpn or both-vpns |
   | deploy        | bitbucket + edge  | both-vpns                  |
   | tag_bitbucket | bitbucket         | bitbucket-vpn or both-vpns |
   | tag_github    | github-write      | firewall-off               |

2. **Messages name postures, not endpoints.** `PostureError`, the guided
   prompts, and `status`'s `[posture: …]` hint print the satisfying posture
   names (e.g. `requires posture [both-vpns]`), so the operator is told which
   firewall state to select rather than which hosts failed to answer.
   Unreachable endpoints are still listed as evidence.

3. **Probe mechanics are unchanged.** No probe can observe a posture name —
   the corporate proxy accepts TCP connects in every posture — so gating
   stays capability-level: protocol git probes per phase access direction
   (ADR-0012) and TCP probes for Edge SSH endpoints.

## Consequences

- The phase order of ADR-0012 now yields **one mandatory switch per
  release** when the operator starts in both-vpns: verify, publish, deploy,
  and tag_bitbucket all run there, then one switch to firewall-off for
  tag_github. Starting anywhere else only adds VPN joins, not extra
  round-trips through the wall.
- A rollback (verify skipped) completes entirely in both-vpns until its
  final tag_github step.
- `verify` still probes the GitHub API before running — reads work in every
  posture, so a failure there indicates an outage or a propagating switch,
  not a wrong posture.
- CONTEXT.md, the operator workflow doc, and the posture console present the
  same five-posture table; the exclusive-posture wording in earlier ADRs is
  superseded by this one.
