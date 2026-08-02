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

6. **The console is a Paramiko-transport surface.** A `transport: pane` node
   (ADR-0011) takes its RSA passcode in the tmux pane, not on the engine's
   stdout, so the console can neither see the prompt nor answer it — a deploy
   would sit on "waiting for operator" until the operator gave up. The server
   refuses `deploy`, `release`, `rollback` and `transport-smoke` for any pane
   node and says to use a terminal. `preflight` is TCP-only and still works.
   Pane remains the documented recovery path; it is simply not a console one.

7. **Posture stays manual and is stated as such.** There is no posture action
   and no posture-changing command in the allowlist. At a boundary, the
   console shows which posture the phase needs, that the operator must switch
   it themselves, and a single button that forwards the acknowledgement the
   engine is blocked on. Readiness markers stay advisory: the engine's own
   git-protocol probe (ADR-0012) remains authoritative, so the console warns
   rather than blocks.

8. **Only this page may act.** The server binds to loopback, requires a
   loopback `Host`, and requires a per-process token embedded in the page it
   served. Read endpoints stay open; every mutating endpoint requires the
   token. `--read-only` starts the console with no action registry at all, and
   onboarding uses it.

9. **The page leads with what is happening now.** Open runs are spotlighted
   with their rail, live progress, action rows, and terminal. Checkouts with
   no run in flight get a release decision card: a one-sentence verdict, the
   three facts it rests on (what the nodes hold, what the checkout holds, what
   GitHub main holds), a precondition checklist whose fixable items carry
   their own buttons, and one primary call to action that is disabled with a
   stated reason when a release would fail. Closed runs move to a collapsed
   history section.

10. **A refusal the console can predict is shown, not discovered.** The engine
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
   the button is pressed and disables the actions it would refuse, scoped to
   the phases the run has not passed yet — a missing `BB_TOKEN` does not
   disable a run whose only remaining phase is `tag_github`, which pushes to
   GitHub. `status` is never disabled: it reads local ledgers only and is the
   one command that still works when everything else does not. This matters
    most in a guided release, where a refusal the console could have predicted
   otherwise arrives several phases and a manual posture switch later.

   Taking a working button away is worse than failing to predict a refusal, so
   where the console cannot tell, it does not block.

   GitHub CI is the one condition reported rather than enforced. A conclusion
   exists only in the GitHub API — git publishes `refs/heads`, `refs/tags` and
   `refs/pull` and nothing about checks — so it cannot be read offline, and it
   can change between the answer and the click. The console runs the engine's
   own probe (`repository.github_ci_conclusions_via_api`) rather than a second
   implementation, so the prediction and the gate cannot disagree. Where verify
   refuses on an unknown, the console does not: an answer it could not get
   blocks nothing.

   What the console cannot predict cheaply and honestly is left to the
   streamed refusal: Bitbucket remote state, and anything the phase exists to
   attempt.

11. **`--demo` drives an offline simulator.** `edge_console.demo_engine`
   produces the same output shapes and the same operator gates as the real
   engine against fabricated checkouts, so the orchestration path is
   exercisable — and reviewable — with no Bitbucket, Edge, SSH, Kerberos, or
   RSA access.

## Consequences

- `gh` is no longer required to release. `require_successful_github_ci` asks it
  first and falls back to the REST API with the credential git's helper already
  holds, so a controller with git credentials but no `gh` can still verify. The
  fallback runs **only when `gh` could not answer at all** — an answer from
  `gh` is final in both directions, and if neither source can answer, verify
  refuses and keeps `gh`'s diagnosis. `api.github.com` is a different host from
  `github.com`, so a proxy may allow one and not the other; that case lands on
  the same refusal as before this change.

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
- `transport: pane` nodes are the one operator path the console refuses
  outright rather than half-supporting.
- Stealing a run lock takes a named confirmation: the console offers
  `--force-lock` only on the run that is blocked, only when the console does
  not itself hold that lock, and only behind a dialog naming the pid and host.
  Stopping a command closes the child's stdin and waits before terminating, so
  the common case never needs it at all — the engine takes its own unwind path
  and releases the lock.
- A rollback is offered from the completed run it would restore, because the
  history is exactly the list of tags worth restoring, and only when no run is
  open in that checkout (the engine refuses otherwise).
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
