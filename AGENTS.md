# Agent Guide

`edge-deploy-core` is the operator-only release engine shared by Autobench and
Dispatch.

## Default workflow

Follow [CONTRIBUTING.md](CONTRIBUTING.md). Work from current GitHub `main`, use a
short-lived branch, run `python -m pytest`, and finish by opening a GitHub pull
request.

Agents may create branches, commit, push a branch, and open a pull request when
the user requests those actions. Agents must not merge pull requests, change
branch protection, create release tags, push to Bitbucket, or run a release
without an explicit Release Operator instruction.

Generated reports, credentials, RSA passcodes, Kerberos passwords, tokens, and
operator configuration must never enter GitHub.

## Project map

- `edge_deploy/`: package and CLI
- `tests/`: full validation suite
- `docs/release-workflow.md`: Release Operator procedure
- `docs/adr/`: durable release-engine decisions

## Learned User Preferences

- On Windows PowerShell, avoid `&&`; use `;` or separate shell calls.
- When executing an approved plan file, treat the plan as authoritative and do not edit it unless explicitly asked.
- If GitHub write access is blocked by network posture, finish local work and report exact push and PR commands for later.

## Learned Workspace Facts

- Release work spans five workstation postures (baseline, edge-vpn, bitbucket-vpn, both-vpns, firewall-off; ADR-0013); commands should pause or resume cleanly at posture boundaries.
- GitHub read works in every posture. GitHub write requires the firewall off, which drops both VPNs — so GitHub write and Bitbucket/Edge access can never be held at the same time. The Bitbucket and Edge VPNs are independent and may be held together.
- The release engine has a run ledger and phase modules under `edge_deploy/phases/` for resumable release operations.
- The controller machine has only Windows PowerShell 5.1 (no pwsh 7); scripts must not use `#requires` for PowerShell 7.
- `python` is not on the controller machine's PATH; invoke Python via the `py` launcher (e.g. `py -m pytest`).
- psmux (the Windows tmux port) `send-keys` drops everything after the first embedded newline; multi-line remote commands must be sent line by line. The tmux pane itself connects to a Linux shell on the edge node.
- Published release tags are immutable; never move or re-point a published tag — publish a new version instead.
- The release engine fingerprint is a content hash of the package, so any source edit changes the engine identity; open runs must be finished with the engine that created them or abandoned.
- Both tools (autobench and robocop/dispatch) pin `edge-deploy-core` by git version tag in their `pyproject.toml`; releasing a new engine version requires bumping that pinned dependency in each tool via a normal GitHub PR, only after the new engine tag is published.
