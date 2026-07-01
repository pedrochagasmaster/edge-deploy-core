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
