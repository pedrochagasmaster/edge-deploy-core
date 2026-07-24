# Release Operator onboarding

## Context

A new Release Operator historically assembled first-time setup from the README,
release workflow, private operator knowledge, and tool repositories. Missing
host keys, unusable audit remotes, and failed readiness often appeared only
after a real release started. There was no resumable setup state and no safe
practice path that taught the guided phase sequence without remote mutation.

Onboarding readiness treats authenticated GitHub CLI access (`gh auth`) as its
own check, separate from the console's GitHub **write** capability indicator.
That indicator (and posture gating) uses the non-mutating
`git push --dry-run` write-path probe from ADR-0012 — not `gh` auth and not
TCP/read reachability.

The five-posture model (ADR-0013) already makes routine operator work possible
in `both-vpns`: GitHub read, Bitbucket, and Edge are available together. GitHub
write still requires `firewall-off` (ADR-0012). Onboarding must not force that
switch. Interactive RSA/Kerberos secrets stay memory-only (ADR-0002). Durable
work and posture-scoped phases live in the run ledger (ADR-0008); Paramiko is
the default transport (ADR-0014). Tool-owned verification remains
`tools/dev/local_check.ps1` (ADR-0016).

## Decision

1. **Resumable `onboard` command.** After a documented Windows PowerShell 5.1
   bootstrap of the approved immutable core tag, the operator runs
   `py -m edge_deploy onboard --config <private-file>`. Stages are
   `prerequisites → config → repositories → readiness → practice → complete`,
   each persisted atomically under `%APPDATA%\edge-deploy\onboarding-state.json`.
   `--check` reruns diagnostics without provisioning. `--restart` discards
   onboarding evidence only (checkouts and private files stay); `--yes` is
   valid solely with `--restart`.

2. **Both-VPNs-only routine path.** The operator connects Bitbucket and Edge
   VPNs before bootstrap and never switches posture during onboarding. Real
   readiness probes run in `both-vpns`. Onboarding never changes VPN or
   firewall state and never requires `firewall-off`.

3. **Bootstrap core is the editable package parent.**
   `bootstrap_core_root()` is
   `Path(engine_identity()["package_dir"]).resolve().parent` — the same
   checkout installed with `py -m pip install -e ".[dev]"`. Onboarding
   validates that checkout as `audit_repo` and does not clone core again.
   Tools are cloned under the configured checkout root; `dispatch` is a CLI
   alias for canonical tool id `robocop`.

4. **Training isolation.** Practice ledgers live under
   `%APPDATA%\edge-deploy\training\<tool>\` with both markers
   `kind="training"` and `training=true`. Production ledgers omit `training`.
   Training handlers never import production phase executors; production
   entry points reject training ledgers. The Edge Console shows training
   cards read-only with a simulated posture rail that must not be treated as
   a real posture switch.

5. **Console GitHub write indicator is separate.** Aggregate green means every
   watched tool's non-mutating `git push --dry-run` write probe passed
   (ADR-0012 probe argv). Red or unavailable in `both-vpns` is expected and
   is not an onboarding failure. Detailed console write-indicator ownership
   remains the console plan; onboarding only documents the operator meaning.

6. **No secrets in Git; no auto host-key enrollment.** Private onboarding
   YAML and installed operator config forbid credential-shaped keys.
   `BB_TOKEN` stays environment-only. Durable state stores fingerprints and
   redacted outcomes only. Missing known_hosts entries fail closed with
   enrollment guidance; onboarding never appends host keys.

## Consequences

- Zero-state operators have one documented bootstrap plus one resumable
  command before a first real `release --guided`.
- Routine onboarding completes without a GitHub-write posture; operators
  learn the eventual `both-vpns → firewall-off` boundary in simulation only.
- Engine identity changes refuse implicit resume until `--restart`.
- Tool checkouts that declare `dev` and `release` extras continue to install
  `.[dev,release]` during provisioning (ADR-0001); core itself has no
  `release` extra.

## Relationship to prior decisions

Preserves ADR-0002 auth seam, ADR-0008 ledger/phases, ADR-0012 write-path
probes and tag ordering, ADR-0013 five postures, ADR-0014 Paramiko default
transport, and ADR-0016 tool-owned verification. Does not authorize real
releases, weaken host-key policy, or store private endpoints in Git.
