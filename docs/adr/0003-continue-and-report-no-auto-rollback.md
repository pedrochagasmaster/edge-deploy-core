# Releases continue on failure and never auto-rollback

A **Release** fans out across independent **Edge Nodes** with manual, serial
auth, so true all-or-nothing atomicity is impossible. On a failed **Publish** we
skip that **Tool's** **Rollouts** but continue other tools; on a failed Rollout
we record it, leave the node in whatever state it reached, and continue the
remaining Rollouts. The run exits non-zero and the consolidated report flags any
node left mid-state. Rollback stays an explicit Operator action
(`--rollback-from`), never automatic, because the Operator often wants the new
tree left on for debugging. `--fail-fast` is available for operators who prefer
to stop on the first failure.

Remote Git preflight may perform one bounded repair before rollout mutation:
when fetch identifies the expected remote-tracking ref as unresolvable or a bad
object, it deletes only that ref and its reflog, then retries fetch once. The
rollout report and release log record the attempt and result. Unknown Git
failures still stop for Operator investigation.

## Consequences

- Maximum progress and full visibility over atomicity.
- The report schema must distinguish "rolled out", "failed (state left = …)",
  and "skipped".
- Known local tracking-ref corruption is self-healing and auditable; repair is
  restricted to the exact expected ref and never changes the working tree.
- Recovery is a deliberate follow-up Release (see resume semantics), not an
  implicit unwind.
