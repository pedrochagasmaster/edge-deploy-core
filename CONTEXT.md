# Edge Deploy Language

**Contributor** develops locally or through Cursor and opens a GitHub pull
request.

**Maintainer** reviews and squash-merges a pull request after CI and human
approval.

**Release Operator** explicitly releases merged GitHub `main`.

**Tool** is one deployable product: Autobench or Robocop/Dispatch.

**Tool Profile** is the tool’s committed `edge_deploy.yaml`. It contains
node-independent release metadata and no operator paths or credentials.

**Publish** fast-forwards the exact reviewed GitHub commit to Bitbucket.

**Deploy** applies that published commit to each selected Edge Node and verifies
the final commit, drift, permissions, and smoke checks.

**Release** is one operator-controlled Publish-and-Deploy run for the Tool in
the current checkout.

**Rollback** explicitly restores a previously recorded successful release tag.
It never rewinds Bitbucket `main`.

**Authenticated Pane** is the local tmux/psmux session holding an authenticated
SSH connection to one Edge Node.

**Drift** is a difference between runtime-critical files in the released commit
and files present on an Edge Node.

GitHub `main`, Bitbucket `main`, successful release tags, and deployed Edge state
must identify the same full source SHA.
