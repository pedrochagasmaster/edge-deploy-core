# Pane-safe remote transport protocol

## Context

All remote work on Edge Nodes crosses the Authenticated Pane: a psmux (Windows
tmux port) pane holding an RSA-authenticated SSH session. Kerberos/RSA policy
rules out scp, sftp, and OpenSSH multiplexing; the pane is the only channel.

The 2026-07-03 rollback validation surfaced, one layer at a time, that the pane
is a lossy screen-scraped text channel, not a byte pipe:

- psmux `send-keys` truncates multi-line input at the first newline, so
  heredocs silently lose their bodies.
- psmux strips quotes from whitespace-free arguments: `encoding='ascii'`
  arrives as `encoding=ascii` and turns a string literal into a Python builtin.
- Long single lines exceed Windows `CreateProcess` command-line limits.
- Output is read by capturing the screen, where lines wrap at pane width and
  interleave with prompts and echo.
- `python3` is not necessarily on `PATH`; nodes resolve interpreters
  differently.
- Scripts staged from Windows through text-mode temp files pick up CRLF line
  endings, which kill `set -eu` shell scripts on Linux before any evidence is
  written — and digest verification cannot catch it, because both sides hash
  the same CRLF bytes.

Each of these produced a production failure that masqueraded as a later-stage
error (timeouts, "missing digest marker", digest mismatches), because the
corruption happened silently in transit.

## Decision

Every byte crossing the pane follows one protocol, implemented in
`tmux_driver.py` and `runner.py`:

1. **Commands are single-line.** Multi-line commands are sent line-by-line;
   command payloads (step commands, scripts) are base64-encoded and decoded on
   the node, never sent as raw source.
2. **File content moves as chunked base64** (bounded line length), appended to
   a remote staging file, then decoded by a Python one-liner that is *itself*
   base64-piped to the interpreter — so no quoting survives or matters.
3. **The node-side interpreter is resolved, not assumed**:
   `REMOTE_PYTHON_EXPR` tries `python3.11`, `python3.10`, `python3`, then the
   known sys_apps path.
4. **Results are read between unique sentinels with digest verification.**
   Remote reads emit `__EDGE_RESULT_START__` / SHA-256 / `__EDGE_RESULT_END__`
   markers; whitespace (including screen wrap) is stripped from the base64
   before decode; the digest is matched bottom-up as an exact 64-hex line.
5. **Anything staged locally for upload is written LF-only** (`newline="\n"`),
   and runner invocations check the exit code immediately, so a broken script
   fails at the invocation with the screen attached rather than two calls
   later at the evidence read.

## Considered options

- **scp/sftp for file transfer.** Rejected: not permitted through the
  RSA-authenticated interactive channel; the pane is the only allowed path.
- **OpenSSH ControlMaster multiplexing.** Rejected: not functional on the
  Windows OpenSSH stack in use.
- **Fixing individual symptoms as found.** Rejected after three iterations:
  each fix moved the failure one layer deeper. Treating the pane as a hostile
  channel with one uniform encode/verify protocol is the durable stance.

## Consequences

- Remote interaction is slower (base64 inflation, chunking) but every transfer
  is digest-verified end-to-end.
- New remote operations must use `run_remote` / `upload_file` /
  `read_remote_*`; ad-hoc `send_keys` with raw quotes, newlines, or long lines
  is a defect.
- Transport failures surface at the failing operation with the pane screen
  attached, not as downstream protocol errors.
- Local simulation is possible without an Edge Node (psmux pane + Git bash +
  interpreter shim), and caught the CRLF defect that node-side inspection
  could not explain.
