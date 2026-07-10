# Paramiko as the default release transport

## Context

ADR-0011 made the psmux pane a hostile-but-tolerable byte channel: base64
chunking for every upload, D8 wrap-immune markers for every read, and a
uniform encode/verify protocol because scp/sftp were believed unavailable
under RSA/Kerberos policy. That belief was re-tested directly against a live
Edge Node: a Paramiko connection using the same interactive RSA
keyboard-interactive prompt authenticates cleanly, and its SFTP subsystem is
reachable. A live node03 probe recorded:

- all transport-smoke checks passed (command, transfer, PTY, keepalive,
  cleanup);
- SFTP upload at 0.4 MiB/s and download at 0.2 MiB/s;
- exactly one transport connection reused across every check; and
- cleanup confirmed (part files and scratch directories removed, session
  closed).

The pane protocol remains correct for what it is — a resilient text channel —
but it was never required by policy for file transfer, and paying its
base64/chunking/screen-scrape tax on every release is no longer necessary
where a persistent SSH connection is reachable.

Separately, path construction for release-owned remote state depended on the
literal `$USER` environment variable expanding inside remote shell commands
(`/ads_storage/$USER`), which is fragile across shells and login
environments. Canonicalizing on `~/.edge-deploy`, resolved once per session
against the authenticated remote `$HOME`, removes that dependency regardless
of which transport is in use.

## Decision

1. **`RemoteTransport` protocol** (`edge_deploy/transport.py`) is the seam
   every release-engine module (`auth.py`, `dependencies.py`, `drift.py`,
   `release.py`, `rollout.py`, `runner.py`, `verify.py`) depends on. No engine
   module names `TmuxDriver` directly except the `transport_for_node` factory
   and the package's compatibility re-export.

2. **Paramiko (`edge_deploy/ssh_transport.py`, `ParamikoSshTransport`) is the
   default transport**, selected by `NodeConfig.transport` (`"ssh"` by
   default). One persistent SSH connection is opened per node per deploy
   invocation and reused for every command, transfer, PTY dialogue, and
   keepalive for the life of that invocation, then closed exactly once in
   `finally`.

3. **`transport: pane` remains an explicit, per-node, opt-in fallback.**
   Selecting it constructs `TmuxDriver.from_node_and_profile` and every rule
   in ADR-0011 (single-line commands, base64 chunking, D8 markers, LF-only
   staging) still applies unchanged. No transport failure silently falls back
   from `ssh` to `pane`; an SSH failure is a durable node failure, not a
   trigger to retry over the pane.

4. **Authentication stays keyboard-interactive and owned by `AuthBroker`.**
   `ParamikoSshTransport` never persists an RSA passcode or Kerberos secret;
   `submit_secret` hands the value directly to the active auth attempt or PTY
   dialogue and it is never formatted into a log line, report, or exception
   message. Host keys are verified strictly against `UserKnownHostsFile`
   (exact key-type and key-value match); `AutoAddPolicy` and
   `StrictHostKeyChecking=no` are both rejected before any connection is
   attempted.

5. **Binary transfer, not base64.** `upload_file` hashes the source locally,
   short-circuits when the remote digest at the final path already matches,
   otherwise streams to a uniquely named `<final>.edge-deploy-<uuid>.part`
   file (mode `0600`) via SFTP (`SFTPClient.putfo`). If the SFTP subsystem is
   rejected, the transport falls back to a binary exec-channel stream
   (`cat >` over `sendall`, no base64 encoding at any point). The part file is
   verified by exact size and SHA-256 digest, then atomically renamed
   (`mv -f --`) onto the destination; on any failure the part is removed
   best-effort and the previously verified final archive is left untouched.
   This fallback is transfer-encoding only — it is never a trigger to switch
   the node's configured transport back to `pane`.

6. **Canonical release-owned remote paths.** `edge_deploy/remote_paths.py`
   resolves every release-owned remote path under `~/.edge-deploy`, expanding
   `~` once against the transport-resolved `$HOME` rather than the literal
   `$USER` environment variable. No production path construction references
   `/ads_storage/$USER`.

7. **Transfer progress is durable and visible.** `upload_file`'s progress
   callback reports `TransferProgress` (bytes sent, total bytes, elapsed
   time) into `ReleaseProgressTracker.update_transfer`, which is persisted to
   `release-progress.json` and rate-limited on the console (MiB sent/total,
   percent complete, MiB/s). `edge_console.py` renders the same data as a
   live progress bar per run.

8. **Transport failures are durable node failures, not crashes.** Every
   `TransportError` subclass raised at the per-node auth seam or the per-tool
   rollout seam inside `release.py` is caught, redacted (SSH endpoints are
   masked out of the message), and written into the node's report as
   `status: failed` — never left as an unhandled traceback, and never left as
   `pending` in the run ledger. `_safe_stop` always runs for a node's
   transport, in `finally`, whether the node succeeded, failed, or an
   unexpected exception escaped the tool loop.

9. **A single productized smoke command** (`edge_deploy transport-smoke
   --node <node>`) authenticates once, exercises command execution, verified
   transfer, PTY dialogue, and keepalive over that one connection, always
   tears the connection down, and reports pass/fail per check plus an overall
   result — the same shape an operator or CI can run before trusting a node
   for a real release.

## Considered options

- **Keep the pane as the only channel.** Rejected: the live node03 probe
  falsifies the premise that SFTP/exec-channel binary transfer is blocked;
  paying the base64/chunking tax unconditionally is no longer justified.
- **Make Paramiko available but leave `pane` the default.** Rejected: every
  node an operator has authorization to reach should get the faster,
  digest-verified binary path by default; `pane` is kept only as a named
  recovery override for nodes or environments where SSH access regresses.
- **Automatic fallback from SSH to pane on transport failure.** Rejected: a
  silent transport switch would mask the actual failure (host-key change,
  auth rejection, network partition) behind a slower path that "just works,"
  defeating the point of a durable, visible node failure (ADR-0003).
- **Automatic SFTP-to-base64 fallback on SFTP unavailability.** Rejected:
  base64 belongs to the pane protocol's screen-scrape constraints (ADR-0011),
  which do not apply to an interactive SSH channel; the binary exec-channel
  stream is the correct same-tier fallback and keeps ADR-0011's "no base64
  outside the pane" boundary intact.

## Consequences

- New engine code depends on `RemoteTransport`, never `TmuxDriver`, except
  inside `transport_for_node` itself.
- A release against an `ssh`-configured node opens exactly one connection per
  node per deploy invocation; operators authenticate once per node, not once
  per pane command batch.
- Dependency bundle delivery and other large transfers are digest-verified
  end-to-end exactly as under the pane protocol, but move at SFTP/binary-
  stream speed instead of base64-inflated speed, and report live byte
  progress instead of an opaque wait.
- Nodes recovering from an SSH regression can be pinned to `transport: pane`
  without code changes and keep every ADR-0011 guarantee.
- `transport-smoke` gives operators and CI a fast, non-destructive way to
  validate a node's transport before trusting it inside a real release.

## Relationship to ADR-0009 and ADR-0011

- **ADR-0009** (on-node runner and file evidence) is unchanged: the runner
  script, its `~/.edge-deploy/runs/<run-id>/steps/*.json` evidence contract,
  and the D8 wrap-immune read protocol for the pane remain exactly as
  specified. `RemoteTransport.upload_file`/`run_remote` are the same seam the
  runner is bootstrapped and driven through, whichever concrete transport
  implements them.
- **ADR-0011** (pane-safe remote transport) is amended, not replaced: its
  base64/chunking/D8-marker/LF-only rules remain binding whenever a node is
  configured `transport: pane`. Its context-section conclusion that "the pane
  is the only channel" is superseded by this ADR for nodes configured
  `transport: ssh` (the default): SFTP and a binary exec-channel stream are
  both reachable channels under the same RSA/Kerberos policy, verified live
  against node03.
