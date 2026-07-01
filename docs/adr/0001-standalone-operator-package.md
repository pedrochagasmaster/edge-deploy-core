# edge-deploy-core is a tagged operator package

Edge Nodes never import this package, so deployment logic is not vendored into
Tool repositories.

Autobench and Dispatch declare `edge-deploy-core` only in their `release`
optional dependency. The dependency points to an immutable semantic-version
tag on GitHub. Contributors and GitHub PR CI install only `.[dev]` and require
no corporate connectivity.

The Release Operator installs `.[dev,release]` in the Tool checkout and runs
`python -m edge_deploy release` there. The package infers the Tool from
`edge_deploy.yaml`.

## Consequences

- One implementation and version contract serve both Tools.
- Core upgrades are explicit dependency-bump pull requests.
- Contributor setup is independent of Bitbucket and Edge infrastructure.
- The operator maintains private configuration and a local core checkout for
  the Bitbucket-only audit branch.
