# Partial deployments are recorded and never auto-rolled back

Edge Nodes have independent filesystems, so an all-or-nothing Deploy is not
possible. A failed node is recorded with the state it reached. Remaining nodes
may continue unless the Release Operator selected fail-fast behavior.

If Publish succeeded but any Deploy or verification failed, the Release is
unresolved for that source SHA. A retry may resume the same SHA. Releasing a
different SHA is blocked until the attempt succeeds or an explicit Rollback
restores a recorded successful release.

Rollback is never automatic because the failed state may be needed for
diagnosis. Every attempt, including failures and retries, is appended to the
private audit branch.

Remote Git preflight may repair only a known corrupt remote-tracking ref and its
reflog, then retry once. It never changes the working tree.
