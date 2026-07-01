# Contributing

Development ends with a reviewed GitHub pull request. Deployment is a separate
Release Operator responsibility.

## Contributor

```bash
git switch main
git pull --ff-only origin main
git switch -c <short-branch-name>
python -m pip install -e ".[dev]"
python -m pytest
```

Commit the focused change, push the branch to GitHub, and open a pull request
against `main`. Contributors without write access may use a fork.

The pull request must include its test result and release risk. Do not include
credentials, operator configuration, or generated release reports.

## Maintainer

Merge only after CI passes for Python 3.10 and 3.12 and one human Maintainer
approves. Use squash merge and delete the merged branch. Direct pushes to
`main` are not part of this workflow.

Publishing `edge-deploy-core` is not a contribution step. See
[docs/release-workflow.md](docs/release-workflow.md).
