# Development and Release Workflow Design

## Goal

Make contribution routine for local and Cursor-based developers while keeping
deployment explicit, controlled, reproducible, and auditable.

This design applies to:

- `edge-deploy-core`
- `autobench`
- `robocop`

## Roles

The workflow uses three responsibility-based roles. One person may perform more
than one role, but each phase keeps its own controls.

- **Contributor**: develops locally or through a Cursor agent, runs validation,
  and opens a GitHub pull request.
- **Maintainer**: reviews the pull request and squash-merges it after required
  CI and human approval.
- **Release Operator**: updates a clean local checkout from merged GitHub
  `main`, validates it, and explicitly performs a release.

A Contributor is done when the pull request and test evidence are ready. The
development workflow is done when a Maintainer merges the pull request.
Deployment is not part of normal development.

## Repository Authority

GitHub `main` is the canonical reviewed source for all three repositories.
Bitbucket is the corporate release mirror, not a development target.

Every checkout uses these remote names:

- `origin`: canonical GitHub repository
- `bitbucket`: corporate mirror

All changes branch from current GitHub `main` and return through a pull request
targeting `main`. Direct pushes to `main` are prohibited. Contributors with
write access may push short-lived branches to the canonical repository; other
Contributors may use forks.

Pull requests require:

- passing required CI;
- at least one human Maintainer approval;
- squash merge;
- deletion of the merged branch.

CI runs on pull requests and again after merge to `main`. Python 3.10 and 3.12
are the supported CI versions, while project metadata declares Python 3.10 or
newer.

## Contributor Workflows

Cursor and local development have the same boundary:

1. Start from current GitHub `main`.
2. Create a short-lived branch.
3. Install with `python -m pip install -e ".[dev]"`.
4. Develop and run `python -m pytest`.
5. Commit and push the branch to GitHub.
6. Open a pull request using the shared minimal template.

The pull-request template records:

- change summary;
- test command and result;
- risk or deployment impact;
- confirmation that no secrets or generated release reports are included.

Cursor agents may create branches, commit, push, and open pull requests. They
must not merge, change branch protection, create release tags, push Bitbucket,
or deploy.

Normal contributor installation and CI must not require corporate network
access, Bitbucket, Edge nodes, SSH, Kerberos, RSA credentials, or
`edge-deploy-core`.

Each repository has one root `AGENTS.md`. It is the authoritative agent
contract and links to `CONTRIBUTING.md` instead of duplicating procedures.

## Dependency Model

Every repository declares dependencies in `pyproject.toml`. The only documented
contributor installation method is:

```text
python -m pip install -e ".[dev]"
```

Autobench and Robocop expose a separate `release` extra. Release Operators use:

```text
python -m pip install -e ".[dev,release]"
```

The `release` extra pins an immutable semantic-version tag of
`edge-deploy-core` from GitHub. It never tracks `main`. Updating the core
dependency is a deliberate GitHub pull request in each consuming repository.

`edge-deploy-core` uses semantic versions such as `v1.2.0`. The first supported
package contract is `v1.0.0`. A core tag is created only from tested GitHub
`main` and is never moved or reused.

## Release Configuration

Real operator configuration lives outside public repositories at:

```text
%APPDATA%\edge-deploy\config.yaml
```

Core owns a safe example configuration. Real node names, credentials, and
corporate paths are not committed to public repositories. `edge_deploy`
discovers the operator configuration automatically.

Releases operate on one tool checkout at a time. The Release Operator runs the
command from the repository being released; the package infers the tool and
repository root from the current directory. Configuration does not contain
fixed checkout paths such as `D:\Projects\autobench`.

The canonical command is:

```text
python -m edge_deploy release
```

The `edge-deploy` console script remains a convenience alias.

## Tool Release Preconditions

Merging a pull request never deploys automatically. A Release Operator
explicitly starts every release.

Before Publish, `edge_deploy` enforces:

- the current branch is `main`;
- the working tree is clean;
- local `main` exactly equals GitHub `origin/main`;
- `origin` and `bitbucket` exist and match the configured repositories;
- the update was fast-forward-only;
- post-merge GitHub CI succeeded for the exact commit SHA;
- `python -m pytest` passed locally;
- the centralized audit repository is reachable and writable;
- interactive authentication succeeds during preflight.

The operator installs `.[dev,release]` before running local tests. Secrets and
interactive responses stay in memory and are never written to logs or reports.
Publish does not begin until all preflight checks pass.

## Publish, Deploy, Release, and Rollback

The canonical terms are:

- **Publish**: copy the verified GitHub commit to Bitbucket.
- **Deploy**: apply the published snapshot to Edge.
- **Release**: the complete operator-controlled Publish-and-Deploy process.
- **Rollback**: restore a previously recorded successful release.

A normal Release publishes current GitHub `main` to Bitbucket `main` using a
fast-forward-only update. Force-pushing Bitbucket `main` is prohibited.

After deployment and verification succeed, the operator creates an immutable
tag in this format:

```text
release-20260701T143000Z-a1b2c3d
```

The tag is pushed to GitHub and Bitbucket. Failed or incomplete attempts are
audited but do not receive a release tag.

GitHub `main`, the published Bitbucket commit, the deployed Edge state, and the
successful release tag must identify the same full commit SHA.

A normal Release can deploy only current GitHub `main`. A Rollback is an
explicit separate operation targeting a prior successful release tag. It does
not rewind or force-push Bitbucket `main`.

If Publish succeeds but Deploy fails, the attempt is incomplete. A retry resumes
the same SHA safely. A different SHA cannot be released until the incomplete
attempt is resolved or explicitly rolled back.

## Core Package Release

Releasing `edge-deploy-core` does not deploy to Edge. The Release Operator:

1. verifies clean GitHub `main`, successful post-merge CI, and local tests;
2. creates the next immutable semantic-version tag;
3. pushes the exact source commit and tag to GitHub and Bitbucket;
4. records the release in the centralized audit log;
5. updates Autobench and Robocop pins through normal GitHub pull requests.

## Centralized Audit Log

Release records for all three repositories live in the existing
`edge-deploy-core` Bitbucket repository on a Bitbucket-only orphan branch named
`release-log`. That branch must never be pushed to GitHub.

Core accesses the branch through an isolated temporary worktree and uses an
explicit `bitbucket` push refspec. Code enforces this isolation; it does not rely
on operator discipline.

The branch is append-only with one commit per release attempt. Reports use:

```text
releases/<tool>/<YYYY>/<MM>/<timestamp>-<commit-sha>/
```

Retries create a new attempt linked to the original attempt. Existing reports
are never edited or deleted.

Each attempt stores the complete redacted bundle:

- consolidated release report;
- Publish reports;
- rollout reports;
- progress state;
- execution log;
- tool commit SHA;
- installed core version;
- operator identity;
- timestamps;
- retry or Rollback linkage.

Audit reachability and write permission are hard preflight gates. If a final
audit push fails after remote state changed, the local report is preserved and
the release remains unresolved. Further releases are blocked until the audit
record is synchronized.

## Documentation Ownership

Each repository uses this structure:

- `README.md`: brief workflow overview and links;
- `CONTRIBUTING.md`: Contributor and Maintainer workflows only;
- `docs/release-workflow.md`: repository-specific Release Operator procedure;
- `AGENTS.md`: agent authority boundaries and links.

`edge-deploy-core` owns generic release semantics and command behavior. Tool
release guides contain only repository-specific prerequisites and commands.

Commands and rules have one canonical home. Other documents summarize and
link. Superseded workflow guides, handoffs, duplicate dependency files, and
overlapping agent instructions are removed instead of archived in active
repositories.

## Validation

The implementation is complete when:

- `python -m pip install -e ".[dev]"` works without corporate access in all
  three repositories;
- `python -m pytest` passes on Python 3.10 and 3.12;
- Autobench and Robocop release extras resolve the pinned core `v1.0.0` tag
  after that tag is available on GitHub;
- release preflight rejects dirty, divergent, stale, incorrectly configured,
  untested, or unauditable states before mutation;
- a successful release preserves the SHA invariant and creates matching tags;
- incomplete releases are resumable and block unrelated releases;
- redacted audit bundles append only to Bitbucket `release-log`;
- no public GitHub ref contains audit data;
- repository documentation contains no stale or contradictory workflow.

GitHub creation succeeded for `pedrochagasmaster/edge-deploy-core`, but its
initial push is an explicitly deferred external operation while GitHub push
transport returns HTTP 503. Core tagging and consumer installation verification
remain blocked until that push succeeds.
