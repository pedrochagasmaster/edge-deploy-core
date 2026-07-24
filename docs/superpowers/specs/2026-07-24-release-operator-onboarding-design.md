# Release Operator Onboarding Design

**Status:** Approved for implementation planning

**Date:** 2026-07-24

## Summary

Add a resumable `edge_deploy onboard` flow for a new Release Operator who has
no operator configuration and no tool checkouts. The flow starts from one
documented clone-and-install command, provisions the selected Autobench and/or
Dispatch checkout, validates the real controller and network capabilities, and
ends with a simulated guided release shown in the Edge Console.

The operator connects both VPNs before bootstrap and remains in the
`both-vpns` posture for the entire onboarding flow. GitHub read, Bitbucket, and
Edge are all available in that posture, and onboarding performs no GitHub
writes. Routine onboarding therefore requires zero posture switches.

The Edge Console's GitHub indicator is also corrected as part of this work. It
must represent current GitHub write capability for every watched tool, not TCP
reachability or GitHub read access.

## Problem

The current first-time procedure is distributed across the README, release
workflow, example configuration, tool repositories, and operator knowledge. A
new operator must discover and correctly perform all of these tasks:

- clone `edge-deploy-core` and one or both tool repositories;
- install the approved engine and tool dependencies;
- create private operator configuration in the correct location;
- configure GitHub and Bitbucket remotes;
- make the core checkout usable as the audit repository;
- authenticate `gh` and provide `BB_TOKEN` through the environment;
- install strict SSH known-host entries;
- validate Edge connectivity and the Paramiko transport; and
- learn the resumable phase and posture workflow without running a release.

There is no single readiness result and no resumable setup state. Several
important failures, including missing GitHub CLI authentication, unknown SSH
host keys, and an unusable audit remote, appear only after an operator starts
release work.

The console currently displays green GitHub endpoint lights when its TCP/read
checks succeed. GitHub read works in every supported posture, so those lights
do not establish whether the current posture and credentials permit the
GitHub write needed by `tag_github`.

## Goals

- Provide one resumable onboarding command after a minimal trusted bootstrap.
- Provision Autobench, Dispatch, or both at the operator's choice.
- Use bundled non-sensitive defaults plus a pre-provisioned private config
  file.
- Validate local prerequisites, repositories, configuration, authentication,
  Bitbucket, Edge, and transport readiness before a first release.
- Complete routine onboarding in `both-vpns` with no posture switches.
- Teach the real guided phase flow without remote writes or deployments.
- Make reruns idempotent and safe after interruptions or temporary access
  failures.
- Keep secrets, private configuration, generated evidence, and onboarding state
  out of every Git repository.
- Make the console's GitHub indicator mean write capability for all watched
  tools.

## Non-goals

- Perform, start, or authorize a real release.
- Test GitHub writes during routine onboarding by switching to `firewall-off`.
- Store credentials, passcodes, tokens, private endpoints, or operator
  configuration in `edge-deploy-core`.
- Silently repair conflicting checkouts, branches, remotes, or configuration.
- Replace the canonical release workflow or the production run ledger.
- Auto-enroll SSH host keys or weaken strict host-key verification.
- Provision corporate accounts, VPN clients, RSA access, Kerberos access, or
  GitHub/Bitbucket permissions.
- Support onboarding from a non-Windows controller.

## Considered Approaches

### Resumable onboarding command

Add `py -m edge_deploy onboard --config <private-file>`. The command performs
safe local setup, validates access, records non-secret progress, and drives an
isolated practice run. This is the selected approach because it removes the
error-prone manual steps while preserving explicit security and operator
boundaries.

### Documentation plus `doctor`

Keep cloning and configuration manual, then add a read-only readiness command.
This is smaller but leaves the most failure-prone work outside the tool and
cannot resume a partially configured workstation.

### Browser-led setup wizard

Let the Edge Console collect choices and invoke setup operations. This offers a
friendlier presentation but mixes a read-only monitoring surface with
privileged filesystem, credential, and repository operations. The console
remains a visualization surface instead.

## Operator Experience

### Bootstrap

The operator first connects both the Bitbucket and Edge VPNs. The documented
bootstrap then:

1. verifies Windows PowerShell 5.1, the `py` launcher, Git, and `gh`;
2. clones `edge-deploy-core` from GitHub;
3. checks out the approved immutable engine tag rather than an arbitrary
   development branch; and
4. installs the engine's operator dependencies.

The bootstrap surface remains a short, auditable sequence of commands. It does
not introduce a downloaded PowerShell installer or a second distribution
mechanism. The documented command names the approved tag explicitly; onboarding
does not discover or guess a release version. The core checkout later becomes
the configured `audit_repo`.

### Invocation

The primary command is:

```powershell
py -m edge_deploy onboard --config C:\secure\operator.yaml
```

The operator chooses Autobench, Dispatch, or both through an interactive
multi-select prompt. Automation and repeatable support procedures can avoid
prompts:

```powershell
py -m edge_deploy onboard `
  --config C:\secure\operator.yaml `
  --root C:\edge-deploy `
  --tool autobench `
  --tool dispatch
```

`--check` reruns diagnostics without provisioning. `--restart` resets only
onboarding evidence after confirmation; it never removes a checkout or private
configuration. Explicit CLI arguments override private-file values, which
override bundled defaults.

### Configuration import

Onboarding parses and validates the supplied private file before writing local
state. The file provides protected organization and workstation values such as
private remote URLs, operator identity, node definitions, SSH settings, and the
checkout root.

The engine bundles only non-sensitive defaults:

- supported tool identifiers and display names;
- public GitHub clone URLs;
- default directory names;
- expected tool-profile and local-check paths; and
- the checks required for each supported tool.

Credentials are forbidden in YAML. `BB_TOKEN` remains environment-only, and
RSA/Kerberos secrets remain interactive and memory-only. The validated config
is installed at `%APPDATA%\edge-deploy\config.yaml` with restrictive Windows
permissions. Durable onboarding state records only its fingerprint and check
outcomes, never values from the file.

### Repository provisioning

For each selected tool, onboarding:

1. clones the GitHub repository when the destination is absent;
2. verifies that an existing destination is the expected repository;
3. verifies `origin` against the bundled public URL;
4. configures the private Bitbucket remote from operator configuration;
5. verifies `edge_deploy.yaml` and `tools/dev/local_check.ps1`;
6. installs the tool's declared operator dependencies; and
7. confirms that its immutable `edge-deploy-core` pin exactly matches the
   approved engine tag before dependency installation can replace the installed
   engine.

For the core checkout, onboarding configures and validates the private
Bitbucket remote, confirms access to the Bitbucket-only `release-log` branch,
and writes its path as `audit_repo`.

Correct existing state is reused. A non-empty unexpected directory, incorrect
remote, unsupported branch, dirty checkout, or incompatible engine pin stops
that step with exact remediation. If two selected tools pin different engine
tags, onboarding fails before installing either tool. Onboarding does not
overwrite or reset conflicting state.

### Readiness pass

All real access checks run in the existing `both-vpns` posture:

- GitHub read and authenticated `gh` access;
- selected tool checkouts on clean current `origin/main`;
- Bitbucket read and non-mutating dry-run write access;
- core audit remote and `release-log` access;
- operator-config and tool-profile completeness;
- `BB_TOKEN` presence without displaying or persisting it;
- strict known-host coverage for configured nodes;
- Edge TCP preflight;
- Paramiko command, transfer, PTY, keepalive, and cleanup smoke checks;
- required RSA and Kerberos authentication paths; and
- each selected tool's authoritative `tools/dev/local_check.ps1`.

Independent checks continue after an ordinary failure so one invocation
reports every actionable issue. A check that cannot safely continue because a
prerequisite failed is `blocked`, not falsely reported as failed.

### Guided practice

After readiness succeeds, onboarding creates an isolated training workspace
outside every real tool checkout and production run directory. A simulator
drives the production phase definitions through the same visible sequence:

`verify → publish → deploy → tag_bitbucket → tag_github`

The simulator uses fabricated repositories, nodes, tags, progress, reports,
and outcomes. It prompts for simulated posture acknowledgements but never asks
the operator to change the real workstation posture. In particular, it teaches
that a real guided release normally makes one
`both-vpns → firewall-off` transition before `tag_github`.

Training handlers have no references to production publish, deploy, or tag
implementations. Production commands reject paths and ledgers marked as
training data. These two independent boundaries make a real remote side effect
structurally unavailable from practice mode.

### Console visualization

Onboarding launches the Edge Console against the training workspace. The
console shows phase progression, per-node state, posture gates, retry and
resume guidance, and the exact simulated next command.

The console continues to read both real and training ledgers, but it does not
mutate either. A completed practice run is recorded in onboarding state.

### Completion

Onboarding exits successfully only after provisioning, readiness validation,
and guided practice are complete. Its final redacted report includes:

- selected tools and checkout locations;
- approved engine version;
- operator-config fingerprint;
- repository and audit readiness;
- per-node transport readiness;
- current capability results;
- practice completion; and
- the exact command for beginning a first real guided release.

The report and onboarding state live under `%APPDATA%\edge-deploy\`. They must
never be written into a checkout or committed.

## Architecture

### `edge_deploy/onboarding/manifest.py`

Defines typed, non-sensitive defaults for supported tools. Keeping these
defaults in package Python makes changes part of Engine Identity rather than
creating an untracked package-data configuration seam.

### `edge_deploy/onboarding/state.py`

Owns the versioned onboarding state and atomic replacement writes. State
contains:

- schema version and engine identity;
- selected tools and checkout root;
- private-config fingerprint;
- input fingerprints for completed stages;
- structured stage and check outcomes; and
- training completion.

It contains no credentials, private endpoints, config values, command
transcripts, or generated release evidence.

### `edge_deploy/onboarding/config_import.py`

Validates the private source, rejects credential-shaped fields, merges only
allowed bundled defaults, writes the operator config, and verifies its Windows
permissions.

### `edge_deploy/onboarding/repositories.py`

Provisions and validates core and tool checkouts. Filesystem and Git operations
are exposed behind injected runners so conflict, interruption, timeout, and
idempotency behavior can be tested without network access.

### `edge_deploy/onboarding/checks.py`

Defines checks with explicit dependencies and structured `passed`, `failed`,
or `blocked` results. Every result includes a stable identifier, redacted
summary, evidence fingerprint where useful, and remediation. The runner may
execute independent checks concurrently but must preserve deterministic report
ordering.

### `edge_deploy/onboarding/training.py`

Creates and advances training ledgers through no-I/O handlers. It may consume
production phase names and presentation metadata, but it must not import
production phase executors.

### `edge_deploy/onboarding/runner.py`

Coordinates the state machine, persists after every transition, invalidates
stale evidence, handles interactive choices, and renders the final report.
`edge_deploy/cli.py` only parses arguments and delegates to this runner.

### `edge_console.py`

Remains a standalone, zero-external-dependency, read-only UI outside the
`edge_deploy` package. It gains support for explicitly marked training ledgers
and replaces GitHub TCP/read lights with non-mutating write probes.

## State and Resume Model

The ordered stages are:

`prerequisites → config → repositories → readiness → practice → complete`

Every stage persists atomically before the next begins. Outcomes mean:

- `passed`: reusable while all recorded inputs still match;
- `failed`: invalid input or conflicting local state requires correction; and
- `blocked`: temporary credentials, connectivity, or posture prevents a safe
  attempt.

Resume applies these invalidation rules:

- a changed private-config fingerprint reruns config-dependent stages;
- changed checkout HEADs, remotes, or dirtiness rerun repository and readiness
  checks;
- changed check definitions rerun the affected checks; and
- changed engine identity refuses implicit continuation.

After an engine change, `--restart` discards onboarding evidence but leaves all
repositories and private files intact. Previously provisioned state is then
revalidated rather than recreated.

## Console GitHub Write Indicator

GitHub TCP connectivity and `git ls-remote` prove only read access. They must
not drive the console's GitHub capability light.

For every watched tool checkout, the console runs the same non-interactive
write-path probe used by posture gating:

```text
git push --dry-run --force origin HEAD:refs/edge-deploy/posture-probe
```

The explicit destination and `--force` prevent detached HEAD or stale branch
state from masquerading as a posture failure. `--dry-run` exercises GitHub
authentication, repository authorization, and write-path reachability without
sending a pack or updating a ref. The probe has a deadline and disables
interactive credential prompts.

The aggregate indicator is:

- green only when the probe passes for every watched tool;
- red when at least one watched tool returns a definitive failure; and
- unknown when any required probe cannot run, times out, or lacks a valid
  checkout and no definitive aggregate result can be claimed.

The detail view identifies each tool's outcome. It does not guess whether a
failure came from posture, network policy, credentials, or repository
authorization.

In `both-vpns`, GitHub write is expected to show red even though divergence
checks continue to read GitHub successfully. In `firewall-off`, authorized
repositories show green. Bitbucket and Edge keep their capability-specific
indicators.

## Error and Safety Model

- All subprocesses use deadlines and disable unexpected credential prompts.
- Credentials are scoped to the operation that needs them and are never added
  to state, arguments, logs, exceptions, or reports.
- Existing redaction applies before any output becomes durable.
- Existing repositories, remotes, and private files are never overwritten
  silently.
- Unknown or changed SSH host keys fail with enrollment guidance; onboarding
  never auto-accepts them.
- A partial clone or install is detected and reported on resume rather than
  treated as complete.
- Training directories and ledgers carry an explicit marker that production
  release commands reject.
- Onboarding never invokes a GitHub write operation other than `git push
  --dry-run`.
- Onboarding never changes VPN or firewall state.
- No generated output enters GitHub.

## Test Strategy

### Unit tests

Cover:

- manifest schema and Engine Identity participation;
- private-config validation, merge allowlist, credential rejection, and
  permissions;
- atomic state writes and interrupted-write recovery;
- state input fingerprints and invalidation;
- idempotent repository provisioning;
- refusal to modify conflicting destinations, branches, or remotes;
- deterministic `passed`, `failed`, and `blocked` readiness reports;
- redaction across output, logs, exceptions, and saved state;
- training-ledger markers and production-command rejection;
- absence of production phase executors from training handlers;
- console write-probe pass, failure, timeout, and missing-checkout behavior;
- aggregation across one and multiple watched tools; and
- proof that the dry-run probe creates no remote ref.

### Integration tests

Use temporary local Git repositories, fake process runners, fake transports,
and representative private configuration to test:

- a complete empty-workspace onboarding;
- selecting Autobench, Dispatch, and both;
- interruption and resume after each stage;
- recovery from temporary credential and connectivity failures;
- changed config, checkout, remote, and engine inputs;
- rerunning a completed onboarding without duplicate work;
- the full simulated guided release and console-readable ledger; and
- exact redacted final reports and resume commands.

### Windows acceptance

On a clean supported controller:

1. connect both VPNs once;
2. bootstrap the approved engine tag;
3. import representative private configuration;
4. select one or both tools;
5. complete repository and transport validation;
6. complete the simulated guided release;
7. verify that the console reports GitHub write unavailable in `both-vpns`;
8. verify that no posture switch occurred;
9. verify that no remote ref, deployment, release run, or release report was
   created; and
10. rerun onboarding and confirm all reusable work is skipped safely.

A separate one-time acceptance check in `firewall-off` confirms that the
console turns the GitHub indicator green for every authorized watched tool.
That check validates the console change; it is not part of routine onboarding.

## Documentation

Update the README and canonical release workflow with:

- the minimal trusted bootstrap;
- the requirement to begin onboarding in `both-vpns`;
- private-config preparation and secure delivery expectations;
- interactive and non-interactive onboarding examples;
- resume, `--check`, and `--restart` behavior;
- the distinction between training and a real release; and
- the console indicator's GitHub write semantics.

Correct any operator installation examples that reference an optional
dependency group not declared by the relevant project.

## Acceptance Criteria

- A new operator can start with no local config or tool repositories and reach
  a complete readiness result through one resumable command.
- The operator can select Autobench, Dispatch, or both.
- Routine onboarding begins and ends in `both-vpns` with zero posture switches.
- Existing correct setup is reused; conflicting state is never overwritten.
- Private config and credentials remain outside Git and durable onboarding
  evidence.
- All selected repositories, audit access, local checks, known hosts, and Edge
  transports are validated before completion.
- Practice exercises every release phase without real remote writes,
  deployments, or production run artifacts.
- Production commands reject training ledgers.
- A second identical invocation performs no duplicate provisioning.
- Console GitHub green means dry-run write access passed for every watched
  tool; GitHub read/TCP success alone can never make it green.
- Automated tests and the clean-controller acceptance procedure pass.
