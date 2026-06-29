# Edge Deploy

The shared deployment toolkit used by sibling Hadoop Edge Node tools (currently
**autobench** and **robocop**/Dispatch) to publish a reviewed build and roll it
out to the corporate Edge Nodes. This context is about *getting a build onto the
servers safely*, not about what those tools do once installed.

## Language

**Tool**:
A deployable sibling product managed by this toolkit — currently **autobench**
and **robocop** (Dispatch). The thing a **Release** acts on; selected with
`--tool autobench|robocop|both`.
_Avoid_: target (clashes with the deploy destination), project, app.

**Tool Profile**:
The small `edge_deploy.yaml` file committed in each **Tool's** repo root that
captures only that tool's deploy-specific differences (repo path, env var names,
install-trigger paths, smoke commands, TUI chrome, Bitbucket URL, nodes). The
shared engine reads it so divergence lives in data, not code.
_Avoid_: config, manifest (clashes with robocop's Job manifests), descriptor.

**Operator**:
The human running a **Release** from their Windows machine. Steps in only for
interactive auth (RSA passcode, Kerberos `kinit`); everything else is automated.
_Avoid_: user (means an end user of the tools), deployer.

**Release**:
One end-to-end run of the pipeline for one or more **Tools**: **Publish** the
**Snapshot**, then **Rollout** to every **Edge Node**, then verify. The unit an
**Operator** triggers ("release autobench to both nodes").
_Avoid_: deploy (overloaded), push, ship.

**Publish**:
Create and push a **Snapshot** to the **Tool's** Bitbucket deployment remote.
The only step that talks to Bitbucket.
_Avoid_: push, upload.

**Snapshot**:
A single deployment commit on `bitbucket/main` whose tree is the reviewed source
build and whose parent is the current `bitbucket/main`. The exact thing an Edge
Node is brought to. Identified by its SHA.
_Avoid_: build, release commit, tag.

**Rollout**:
Apply one **Snapshot** to one **Edge Node**: `update.sh <snapshot>`, then
`install.sh` only if install-sensitive files changed, then verify (final commit,
**Drift**, permissions, smoke). Renames robocop's per-node `deploy`.
_Avoid_: deploy, update (ambiguous with `update.sh`), install.

**Authenticated Pane**:
A local tmux/psmux pane holding one live, logged-in SSH session to one **Edge
Node**. Created per node, reused across **Tools** on that node, and the channel
through which all remote **Rollout** work runs (so single-use RSA is paid once
per node, not per command).
_Avoid_: session (ambiguous), connection, terminal.

**Drift**:
Divergence between a **Tool's** runtime-critical files at the intended **Snapshot**
and the files actually present on an **Edge Node**. Zero drift is the success
condition for a **Rollout**.
_Avoid_: diff, mismatch.

**Edge Node**:
The gateway server providing access to the Hadoop cluster; hosts the shared
deployed tree (e.g. `/ads_storage/<tool>`). The two standard nodes are
`hde2stl020003` and `hde2stl020004`, reached over SSH on port 2222. (Consistent
with robocop's product CONTEXT.md.)
_Avoid_: server, host, box.

## Relationships

- A **Release** fans out into one **Publish** per **Tool** and one **Rollout**
  per (**Tool** × **Edge Node**).
- A **Publish** produces exactly one **Snapshot**; a **Rollout** consumes one.
- Edge Nodes have independent filesystems: a **Rollout** to one says nothing
  about the other. Each is verified separately.
