# Paramiko Release Transport Design

**Status:** Approved for implementation

**Date:** 2026-07-10

**Target release:** edge-deploy-core 1.5.0

## Summary

Replace the Authenticated Pane as the default release transport with one
persistent Paramiko SSH connection per Edge Node deployment conversation. The
connection authenticates once through the existing operator passcode flow and
then carries command, SFTP, PTY, keepalive, and evidence traffic over separate
SSH channels.

The pane transport remains available as an explicitly configured emergency
adapter. It is never selected automatically after an SSH failure. Large files
must not fall back to tmux/base64.

## Evidence

The disposable Paramiko prototype passed a live node03 probe on 2026-07-10:

- keyboard-interactive RSA authentication succeeded once;
- command execution preserved exact stdout, stderr, and exit status 7;
- SFTP uploaded and downloaded 8 MiB with identical SHA-256 digests;
- measured SFTP throughput was 0.4 MiB/s upload and 0.2 MiB/s download;
- generated PTY input was validated without using a real Kerberos secret;
- the connection survived a 61.8 second command across two keepalive periods;
- every probe reused transport number 1; and
- remote scratch cleanup and explicit transport close both succeeded.

This proves the required server capabilities. At the measured upload rate, the
67 MiB Autobench dependency archive should transfer in roughly three minutes,
instead of the multi-hour tmux/base64 path observed in production.

## Problem

The current `TmuxDriver` treats a psmux pane as the only authenticated channel.
This imposes three classes of failure:

1. The pane is a lossy text interface. Binary files are base64-expanded and
   divided into thousands of `send-keys` operations.
2. Shell paths have inconsistent expansion rules. A path such as
   `/ads_storage/$USER/.edge-deploy/...` is expanded by unquoted shell commands
   but becomes literal when passed through `shlex.quote()`. The upload precheck,
   decoder, and staging script can therefore address different directories.
3. Progress cannot be measured meaningfully. A live upload looks stalled until
   one complete pane command returns.

The live Autobench release demonstrated all three. A correctly pre-staged
archive was missed, the fallback attempted a roughly 93 MiB base64 payload, and
the engine reported a stall despite bytes moving slowly.

## Goals

- Make one authenticated Paramiko connection the default transport for each
  node during a deploy invocation.
- Transfer dependency archives as binary data with measurable progress.
- Preserve strict host-key verification and the current operator-owned secret
  flow.
- Use one canonical remote-path representation without shell variables.
- Preserve the run ledger, posture phases, D8 evidence files, tool-owned install
  scripts, reports, and resumability.
- Produce actionable node failures instead of transport tracebacks.
- Keep the pane implementation available for explicit recovery use.

## Non-goals

- Persist SSH connections across separate CLI processes or posture phases.
- Remove the pane driver.
- Remove D8 file evidence or change the on-node runner contract.
- Change publish, mirror, audit, tag, rollback, or GitHub/Bitbucket behavior.
- Automatically retry a failed Paramiko operation through the pane.
- Store passcodes, hostnames, private configuration, or generated release
  evidence in GitHub.

## Considered Approaches

### Persistent Paramiko transport

One keyboard-interactive SSH transport carries exec, SFTP, PTY, and keepalive
channels. This is the selected approach because it removes duplicate
authentication, makes binary transfer native, and concentrates remote-channel
complexity behind one interface.

### Paramiko for transfer and tmux for commands

This limits the initial code change but requires two authenticated connections
per node and preserves two path implementations. It was rejected because the
authentication and path split would remain part of every release.

### Automated system SCP pre-staging

This is close to the successful manual recovery, but it requires another
passcode, provides a weak programmatic interface, and leaves tmux/base64 as the
primary engine transport. It is retained only as an operator recovery method,
not as the canonical workflow.

## Architecture

### RemoteTransport interface

Add `edge_deploy/transport.py` with a runtime-checkable `RemoteTransport`
protocol. It contains only behavior used by release modules:

- `start_session()`;
- `session_exists()`;
- `submit_secret()`;
- `await_authenticated()`;
- `at_shell_prompt()`;
- `run_remote()`;
- `upload_file()`;
- `send_text()` and `wait_for()` for PTY dialogue; and
- `stop_session()`.

Callers and tests depend on this interface. `TmuxDriver` and
`ParamikoSshTransport` are adapters at the seam. Pane-only operations such as
attach, capture, resize, and raw `send-keys` do not enter the interface.

Construction is centralized in `transport_for_node()`. Release modules must not
instantiate `TmuxDriver` or `ParamikoSshTransport` directly.

### ParamikoSshTransport

Add `edge_deploy/ssh_transport.py`, productized from prototype commit
`8e50cdd`. The adapter owns:

- endpoint and supported OpenSSH-option parsing;
- strict `~/.ssh/known_hosts` verification, including non-default ports;
- TCP connection setup and keepalive configuration;
- keyboard-interactive authentication state;
- deadline-bounded exec, SFTP, and PTY channels;
- interleaved stdout/stderr draining to avoid channel deadlocks;
- binary transfer and remote digest verification;
- connection poisoning after protocol or timeout failures; and
- deterministic channel and transport cleanup.

Paramiko is a required runtime dependency constrained to the tested major line:
`paramiko>=5.0,<6`.

### Authentication bridge

Preserve the `AuthBroker` interaction:

1. `start_session()` opens TCP and starts keyboard-interactive authentication
   in a worker thread.
2. The keyboard-interactive callback blocks on a private one-slot queue when the
   server requests the RSA code.
3. `start_session()` returns `False` so `AuthBroker` prompts the operator.
4. `submit_secret()` places the code in the queue without logging it.
5. `await_authenticated()` returns on success or raises `AuthenticationError`
   on rejection so the broker can request a fresh code.

The queue slot and callback references are cleared after every outcome. Secrets
must never appear in exceptions, progress events, reports, pane logs, or debug
representations.

### Connection lifecycle

One `ParamikoSshTransport` is created per node within a `deploy` invocation. It
is reused for dependency delivery, checkout update, installation, smoke tests,
drift checks, PTY dialogue, and evidence retrieval. Every remote operation opens
a new stateless channel on that transport.

The transport closes in `finally`. It does not survive a process exit, a phase
boundary, or a later resume command. This matches the run ledger: durable state
lives in evidence files and reports, not in a network connection.

### Canonical remote paths

All release-owned state uses paths rooted at `~/.edge-deploy/`. Callers must not
construct paths containing `$USER`, `$HOME`, or a hard-coded username.

The two adapters resolve the same logical path differently behind the seam:

- Paramiko resolves `~` once from the authenticated account and passes concrete
  paths to SFTP and exec channels.
- The pane adapter renders `~/...` as an expansion-safe `$HOME/...` shell
  expression while quoting only the remainder.

Dependency staging, the runner, evidence retrieval, bundle activation, and
cleanup all use the same logical path helpers. Tests must reproduce the
`$USER` quoting defect and prove that no literal-variable directory can be
created.

### Binary upload protocol

`upload_file()` keeps the existing digest-returning interface and adds an
optional progress callback.

For Paramiko:

1. Hash the local file.
2. Resolve the canonical remote path.
3. If the final remote file has the same digest, return immediately.
4. Upload to a uniquely named adjacent `.part` file with mode `0600`.
5. Emit rate-limited progress snapshots containing bytes sent, total bytes,
   elapsed time, and current throughput.
6. Verify remote size and SHA-256.
7. Atomically rename the verified part file to the final path.
8. Remove the part file on failure without touching a previously verified final
   file.

SFTP is the primary implementation. If the authenticated server does not offer
an SFTP subsystem, use the prototype's binary exec-channel streaming on the same
connection. Do not base64-encode large payloads. A connection failure, timeout,
permission failure, or digest mismatch fails the node rather than changing
transports.

The pane adapter remains compatible for small runner and evidence files. Its
large-file behavior is available only when an operator explicitly selects
`transport: pane`.

### Command and PTY behavior

`run_remote(command, timeout=...)` preserves the current `(text, exit_code)`
shape. The Paramiko adapter drains stdout and stderr concurrently, appending
chunks to the returned transcript in observed order. Text is decoded as UTF-8
with replacement for invalid bytes. It returns the real channel exit status and
does not synthesize shell prompts, command echoes, or pane sentinels.

The interactive PTY channel is created only for dialogue such as Kerberos
authentication. `send_text()` and `wait_for()` operate on that channel and use
the same deadlines and secret-redaction rules.

### Progress and observability

Transfer progress is visible in both places operators already use:

- console updates such as `node03 dependency upload: 23.4/67.0 MiB (35%, 0.4
  MiB/s)`; and
- an optional structured transfer object in `release-progress.json` containing
  node, artifact kind, bytes sent, total bytes, percentage, rate, and update
  timestamp.

Updates are rate-limited to avoid console noise and disk churn, but the final
100 percent event is mandatory. Each event counts as meaningful activity so a
healthy transfer cannot trigger a stall warning.

The progress schema change must remain readable by the current Edge Console.
The console accepts progress files without transfer fields and displays transfer
fields when present.

## Error Model

Define transport-owned errors with stable, redacted messages:

- `TransportUnavailable`: required adapter or dependency cannot be loaded;
- `HostKeyError`: host key is unknown or changed;
- `AuthenticationError`: keyboard-interactive authentication failed;
- `ConnectionLostError`: the authenticated transport died;
- `RemoteCommandTimeout`: a channel exceeded its deadline;
- `TransferError`: binary delivery, permission, size, or digest verification
  failed; and
- `InteractiveChannelError`: PTY dialogue failed.

Unknown or changed host keys fail before a passcode is requested. Authentication
rejection retains the existing fresh-code retry behavior. A poisoned or timed
out connection is closed and never reused.

Release orchestration catches transport errors at the per-node seam and writes a
failed rollout report and ledger state with an actionable redacted reason. It
must not leave a node pending or surface a raw traceback for an expected
transport failure.

No automatic fallback to the pane is allowed. Silent fallback would reintroduce
multi-hour behavior and make the report describe the wrong transport.

## Configuration and Migration

Add `NodeConfig.transport` with accepted values `ssh` and `pane`. Missing values
default to `ssh`. The example operator configuration documents `pane` only as a
recovery override.

The change is released as `edge-deploy-core 1.5.0`. Engine Identity rules remain
unchanged: runs created by 1.4.0 must finish or be abandoned using 1.4.0 before
the installed engine is upgraded.

The literal `/ads_storage/$USER` compatibility symlinks created during the
Autobench recovery are not required by 1.5.0. They are harmless while present
and may be removed after a successful 1.5.0 dependency-bearing release on both
nodes.

Both Autobench and Dispatch continue pinning the engine by immutable Git tag.
Their pins move to `v1.5.0` only after the engine tag exists and resolves on
GitHub.

## Security

- Strict known-hosts verification is mandatory; no auto-add policy exists.
- Passcodes and Kerberos secrets remain memory-only and are never persisted.
- Remote temporary files are operator-owned, mode `0600`, and uniquely named.
- Verified final files are replaced atomically.
- Exceptions and durable reports use node labels, not endpoints or private
  configuration.
- Generated probe and release evidence stays outside GitHub.
- The binary fallback accepts only controller-provided bytes and verifies the
  same local and remote digest.

## Test Strategy

### Interface and existing adapter

- Prove `TmuxDriver` structurally satisfies `RemoteTransport`.
- Retype all release callers against the protocol without changing pane
  behavior.
- Preserve the existing pane test suite.

### Paramiko adapter

Use injected fake Paramiko transports, channels, and SFTP clients for
deterministic tests covering:

- known-host success, unknown key, and changed key;
- keyboard-interactive prompt, success, rejection, retry, and timeout;
- secret disposal after every authentication outcome;
- exact command transcript and exit status;
- concurrent stdout/stderr draining without deadlock;
- command timeout, connection loss, poisoning, and cleanup;
- SFTP precheck reuse and successful atomic upload;
- progress snapshots and mandatory completion;
- interrupted upload cleanup while preserving the old final file;
- permission, size, and digest failures;
- SFTP-unavailable binary streaming fallback;
- PTY input and pattern waiting; and
- keepalive configuration and connection reuse.

### Path regression

Add focused tests proving:

- every dependency and runner path begins with `~/.edge-deploy/`;
- pane rendering expands only `$HOME` and safely quotes the remainder;
- Paramiko resolves one concrete home directory;
- precheck, upload, staging, execution, activation, and cleanup address the same
  path; and
- no command contains `/ads_storage/$USER`.

### Release integration

Test factory selection, SSH defaulting, explicit pane selection, transfer
progress persistence, redacted node failure reports, connection cleanup, run
locking, and resume behavior. Existing verify, publish, tag, audit, rollback,
and posture tests remain unchanged.

### Live gates

Before release:

1. Run the productized transport smoke against node03 and node04.
2. Run one dependency-bearing guided release with Paramiko on both nodes.
3. Confirm the dependency archive uses SFTP or binary streaming, reaches 100
   percent progress, verifies its digest, and does not invoke tmux/base64.
4. Confirm install, smoke, drift, reports, ledger completion, and remote cleanup.
5. Confirm the pane adapter still passes a focused operator smoke when selected
   explicitly.

## Delivery

Per operator instruction, implementation occurs directly on local `main`, not
through milestone pull requests. Use incremental commits so each coherent slice
is recoverable and reviewable. Do not push remote `main`, install the modified
engine for production use, create tags, mirror, or update tool pins until the
full automated and live gates pass and the Release Operator explicitly advances
those steps.

The final automated gate is:

```powershell
py -m ruff check .
py -m pytest -n 4 --dist loadfile
```

## Acceptance Criteria

- Paramiko is the default transport and a required runtime dependency.
- One RSA authentication per node supports commands, transfer, PTY, and
  keepalive operations.
- A pre-staged matching archive is reused immediately.
- A new dependency archive transfers as binary data with visible byte progress.
- No caller-owned logical path contains shell variables or a hard-coded
  username; only the pane adapter may render `$HOME` as an internal shell
  expression.
- Digest mismatch, timeout, connection loss, and host-key failures are redacted,
  actionable node failures with durable evidence.
- No expected transport failure leaves a deploy node pending.
- Pane transport remains available only through explicit configuration.
- All automated tests and both live node smoke tests pass.
- A dependency-bearing guided release completes on node03 and node04.
- Version and documentation identify the release as 1.5.0.
