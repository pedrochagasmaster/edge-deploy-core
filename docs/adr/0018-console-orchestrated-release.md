# Console-orchestrated release

## Context

The Edge Console was read-only. It answered "what is the state of this run?"
and "should I release this tool?" well, and then handed the operator a string
to copy into PowerShell. Everything after the copy happened somewhere the
console could not see.

That split cost more than convenience.

- **The guided release was opaque.** `release --guided` is the recommended
  path, but once it started, the console showed nothing until the phase landed
  in the ledger. The only visible sign of life was `release-progress.json`,
  which the tracker writes on activity — a coarse heartbeat, not the engine's
  output. The one moment the operator *had* to act, the RSA passcode prompt
  (ADR-0002), was invisible in the console: it appeared in a terminal window
  the operator had to keep watching. An operator staring at the console could
  sit through a whole `WAITING FOR OPERATOR` heartbeat without knowing what
  was being waited on.
- **Copy-paste was a correctness hazard.** With several watched checkouts, the
  copied command had to carry `cd "<root>"` because `load_run()` falls back to
  the working directory. A command pasted while standing in the wrong checkout
  fails with "no such run".
- **State was split across two surfaces.** The console knew the ledger; the
  terminal knew the output. Reconstructing what happened meant reading
  `release.log` after the fact.

Constraints that do not move:

- Engine Identity (ADR-0008) hashes every `edge_deploy/**/*.py`, so console
  code must stay outside the package or every open Run is orphaned.
- Firewall posture (ADR-0013) is a workstation-level change no local process
  may make on the operator's behalf.
- Secrets (ADR-0002) are transient, per-node, and never persisted or logged.
- Training ledgers (ADR-0017) are practice-only and must never be reachable by
  a production command.

## Decision

**The console runs the commands it used to print, and shows everything the
engine says while they run.** It gains no release logic of its own.

1. **A closed allowlist, not a command box.** Every button maps to one entry
   in `edge_console.actions.ACTION_SPECS`. Each entry builds a fixed `argv`
   list from validated parameters and is executed with `shell=False` in one of
   the watched checkouts. The allowlist covers exactly the operator's
   documented vocabulary: the five phases, `release --guided`, `abandon`,
   `status`, `preflight`, `transport-smoke`, and three read-obvious git
   commands (`pull --ff-only`, `pull --rebase`, `push`). Nothing the browser
   sends can become shell syntax, a new flag, or a different program.

2. **Validation is server-side and fails closed.** The checkout must be one
   the console was started with; a run id must match the ledger id shape *and*
   exist on disk; node names and abandon reasons are pattern-checked; a run
   marked `kind="training"` or `training=true` is refused with 403; release
   commands are refused outside a git checkout, which is what keeps training
   workspaces out. One command per checkout at a time.

3. **The engine's stdout is the console's transcript.** Commands run with
   `PYTHONUNBUFFERED=1` and pipes, and output is read incrementally rather
   than by line, so a prompt flushed without a newline is visible immediately.
   The browser long-polls from a byte cursor, so a reload — or a second tab —
   re-attaches to a running command, its full transcript, and its pending
   prompt.

4. **Prompts are first-class.** The console recognises the engine's operator
   boundaries by their exact text: the RSA passcode prompt, the Kerberos
   password prompt, the guided posture acknowledgement, and `[y/N]` gates. It
   renders each as a typed prompt — a masked field, an acknowledgement button,
   a yes/no pair — and writes the answer to the process stdin. An
   unrecognised question that stalls the stream gets a generic answer box
   after a short delay, so a prompt the console does not know can never
   silently hang a run. The raw text the engine printed is always shown next
   to the console's interpretation of it.

5. **Secrets pass through and are never recorded.** A submitted secret is
   written straight to the child process, echoed into the transcript as
   asterisks, and registered so that any later echo of the same string is
   masked. It is never written to disk by the console and never returned by
   any endpoint.

6. **Posture stays manual and is stated as such.** There is no posture action
   and no posture-changing command in the allowlist. At a boundary, the
   console shows which posture the phase needs, that the operator must switch
   it themselves, and a single button that forwards the acknowledgement the
   engine is blocked on. Readiness markers stay advisory: the engine's own
   git-protocol probe (ADR-0012) remains authoritative, so the console warns
   rather than blocks.

7. **Only this page may act.** The server binds to loopback, requires a
   loopback `Host`, and requires a per-process token embedded in the page it
   served. Read endpoints stay open; every mutating endpoint requires the
   token. `--read-only` starts the console with no action registry at all, and
   onboarding uses it.

8. **The page leads with what is happening now.** Open runs are spotlighted
   with their rail, live progress, action rows, and terminal. Checkouts with
   no run in flight get a release decision card: a one-sentence verdict, the
   three facts it rests on (what the nodes hold, what the checkout holds, what
   GitHub main holds), a precondition checklist whose fixable items carry
   their own buttons, and one primary call to action that is disabled with a
   stated reason when a release would fail. Closed runs move to a collapsed
   history section.

9. **A refusal the console can predict is shown, not discovered.** The engine
   turns commands away for several reasons that are already on disk or in the
   console's own environment: the run was created by a different engine build
   (Engine Identity, ADR-0008), another process holds the run lock, the
   operator config is missing or unreadable, `BB_TOKEN` is absent from the
   environment the child will inherit, the checkout is not on `main`, is not
   clean, has drifted off the run's reviewed commit, points a remote somewhere
   the tool profile does not name, or has no committed gate script; a node in
   the ledger is no longer in the operator config; no PowerShell is on PATH to
   run that gate; `audit_repo` is unset or audit records are still queued.
   Each of those is stated before
   the button is pressed and disables exactly the actions it would refuse —
   never `status`, which reads local ledgers only and is the one command that
   still works when everything else does not. This matters most in a guided
   release, where a refusal the console could have predicted otherwise arrives
   several phases and a manual posture switch later.

   What the console cannot predict cheaply and honestly is left to the
   streamed refusal: GitHub CI state (needs `gh` and the network, and goes
   stale between the probe and the click), Bitbucket remote state, and
   anything the phase exists to attempt.

10. **`--demo` drives an offline simulator.** `edge_console.demo_engine`
   produces the same output shapes and the same operator gates as the real
   engine against fabricated checkouts, so the orchestration path is
   exercisable — and reviewable — with no Bitbucket, Edge, SSH, Kerberos, or
   RSA access.

## Consequences

- The console is no longer read-only, and the guard that asserted so is
  replaced by guards on what it *is* allowed to do: the allowlist shape, the
  refusal cases, the absence of ledger writes, and the restriction to
  `edge_deploy.config` / `edge_deploy.preflight` imports.
- `edge_console.py` becomes the `edge_console` package. It still lives beside
  `edge_deploy`, not inside it, so Engine Identity is unchanged by console
  work. Operators launch it with `py -m edge_console`.
- Displayed commands no longer carry `cd "<root>"`: the console sets the
  working directory itself, and the shown command is the command that runs.
  Commands remain copyable for operators who prefer a terminal, and
  `--read-only` keeps that the only mode.
- An operator can now hold a release from one window. The terminal remains a
  supported path — nothing about the engine, the ledger, or the phase
  contracts changed.
- The console can hold a secret in flight. It is written to one child process
  and dropped, and masked in the transcript by a private list of submitted
  values. That list is a local mechanism, not the shared redaction registry
  Plan 010 still proposes; when Plan 010 lands it should absorb it.
- `edge_deploy/onboarding/runner.py` had to change, because it launched the
  console by path. Any edit under `edge_deploy/` changes the Engine Identity
  hash, so open Runs created by the previous engine must be finished with it
  or abandoned before upgrading. This is the standing rule for engine
  changes (ADR-0008), not a new exception.
- Two operator paths still need a terminal: a deep-smoke release
  (`--smoke deep`, which is what triggers the Kerberos prompt) has no console
  action, and `transport: pane` nodes take their RSA passcode in the attached
  pane rather than on the engine's stdout, so the console can only show that
  it is waiting.
- The console will not steal a run lock: `--force-lock` is deliberately not in
  the allowlist, because a lock held by a live process should take a
  deliberate decision and a typed command. To keep that from being a trap,
  stopping a command closes the child's stdin and waits before terminating, so
  the engine takes its own unwind path and releases the lock. A lock left by a
  process that died some other way still needs a terminal, and the console
  says so.
- The child inherits the console process's environment, so `BB_TOKEN` has to
  be set in the shell that starts the console — exporting it later, or in
  another window, does not reach the buttons. The console reports its absence
  rather than letting publish discover it.
- Which engine a button runs is `--engine-python` (default: the console's own
  interpreter), resolved in the watched checkout. If that is not the engine the
  operator would get in their own terminal, runs created from one side are
  refused by the other on Engine Identity. The console reads the identity from
  the interpreter it will actually spawn and compares it to every open run.

## Considered options

**Server-sent events or a WebSocket instead of long polling.** Both are a
better fit for a stream, and both are more code than the stdlib
`ThreadingHTTPServer` wants to carry. A byte cursor over plain GET gave the
one property that mattered more than elegance: a reload, or a second tab, can
re-attach to a running command and its pending prompt by asking for everything
after byte N. That is the same reconnect story an SSE `Last-Event-ID` would
have bought, without a framing layer.

**Importing the engine and calling the phase functions in-process.** Rejected
twice over. It would put console code in the same process as the run — the
thing Engine Identity exists to keep separate — and it would make the console
a second implementation of the phase sequencing that `cli.py` already owns.
Spawning the documented command keeps exactly one implementation, and keeps
what the console shows identical to what it runs.

**A free-form command box.** Rejected: the value here is that the operator
cannot ask for anything the procedure does not already sanction. A closed
allowlist is what lets the console accept input from a browser at all.

## Relationship to prior decisions

Preserves ADR-0002 (the auth seam still owns prompting; the console only
relays), ADR-0003 (nothing is auto-rolled back), ADR-0008 (the ledger is
written by the engine alone, and Engine Identity is untouched), ADR-0012 (git
protocol probes remain authoritative for gating), ADR-0013 (five postures,
switched by hand), ADR-0014 (transfer progress still comes from
`release-progress.json`), and ADR-0017 (training ledgers are display-only,
now enforced by the server as well as the page).

Supersedes the read-only console constraint recorded in the onboarding design
spec, and only for production checkouts: onboarding still launches the console
`--read-only`.
