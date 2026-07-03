# Tree-equivalent Bitbucket mirroring

Bitbucket's server-side pre-receive hook only accepts commits committed by the
pushing operator. Every GitHub pull-request merge commit is committed by
`GitHub <noreply@github.com>`, so "push the same commit and tag to both remotes"
can never succeed for merged work. The v1.1.0 release exposed this as a permanent
architectural conflict, not a transient failure.

The operator workstation also cannot assume simultaneous access to GitHub and
Bitbucket/Edge. Release commands run in the Bitbucket/Edge posture so they can
publish snapshots, deploy nodes, smoke test, and write the private audit log.
GitHub tag pushes are a separate network phase.

The cross-remote release contract is therefore **tree equivalence**: GitHub remains
the review source of truth; Bitbucket receives content proven identical by tree SHA.
Commit SHAs may differ between remotes and that is expected, recorded, and verified.

- `python -m edge_deploy mirror --tag vX.Y.Z` mirrors a core release tag. It pushes
  the exact commit and tag when Bitbucket accepts them, and otherwise creates an
  operator-authored mirror commit carrying the reviewed commit's exact tree
  (parented on the Bitbucket tip, fast-forward only) plus an operator-authored
  annotated tag. Commit and tag messages record the source commit SHA and tree SHA.
- Tool publishes already create operator-authored deploy-snapshot commits; release
  tags now follow the same rule: the GitHub tag points at the reviewed source commit
  and the Bitbucket tag points at the commit actually deployed there.
- Tool release commands create the local annotated tag and write a report-side
  tag-push handoff instead of pushing remote tags inline. Operators push GitHub
  and Bitbucket tags after switching to the matching network posture.
- Rollback verifies cross-remote tags by tree SHA and rolls nodes to the
  Bitbucket-side commit, which is the only one nodes can fetch.

## Considered options

- **Policy exception for GitHub-authored commits.** Rejected: requires per-release
  human escalation to a Bitbucket administrator; the opposite of seamless.
- **Squash/rebase-only merges so the operator authors every main commit.** Rejected:
  constrains GitHub review workflow to work around a delivery-mirror limitation and
  still breaks on any historical merge commit.

## Consequences

- Mirroring never blocks on commit authorship; provenance lives in messages and the
  shared tree SHA is machine-verifiable on both remotes.
- Bitbucket `main` history is a chain of operator commits, each traceable to one
  reviewed GitHub commit.
- Tag targets differ across remotes by design; tooling must compare trees, never
  assume SHA equality.
- A blocked remote tag push is a finalization issue, not a failed node rollout.
  Operators must not rerun a completed rollout just to retry tag publication.
