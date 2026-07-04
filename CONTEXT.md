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

**Snapshot** is the exact source tree a Release or Rollback delivers to Edge
Nodes. Every check that judges deployed state — including Drift — is judged
against the Snapshot's tree, never against the operator's working tree, which
may have moved on (or behind) since the Snapshot.

**Posture** is the workstation's exclusive firewall state. At any moment at
most one of GitHub or Bitbucket-plus-Edge is writable; no posture allows
writing to both. Switching posture is a human act outside the engine's
control, and a switch is not instantaneous: endpoints can fail transiently
while the new posture propagates.

**Run** is one durable release attempt, from creation to `complete` or
`abandoned`. At most one Run may be open per Tool, and an open Run must be
explicitly resumed or abandoned before a new one starts.

**Run Ledger** is the durable, append-audited record of a Run: its source SHA,
per-phase and per-node state, and the Engine Identity that created it.

**Engine Identity** is the exact content of the installed release engine. A
Run is bound to the Engine Identity that created it; any engine code change
orphans open Runs, which must be abandoned and recreated.

**Guided Release** is a Release in which the engine, in a single invocation,
walks the Release Operator through every posture switch and RSA prompt instead
of exiting at each posture boundary.

**Authenticated Pane** is the local tmux/psmux session holding an authenticated
SSH connection to one Edge Node. The pane is a lossy, screen-scraped text
channel — not a byte pipe — so everything crossing it must be pane-safe.

**Pane-Safe** describes a remote command or payload that survives the
Authenticated Pane's known corruptions: single-line only, quote-stripping on
whitespace-free arguments, line-length limits, and screen-wrap of output.
Arbitrary content crosses the pane base64-encoded in bounded chunks, and
results are read back between unique sentinels with digest verification.

**Drift** is a difference between runtime-critical files in the released commit
and files present on an Edge Node.

GitHub `main`, Bitbucket `main`, successful release tags, and deployed Edge state
must identify the same full source SHA.
