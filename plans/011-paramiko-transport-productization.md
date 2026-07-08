# Plan 011: Productize the Paramiko SSH transport behind a Transport seam, pane retained as fallback

> **Executor instructions**: This is an architecture plan executed as gated
> milestones M0–M4. Each of M1–M4 is one branch and one PR, following
> CONTRIBUTING.md. **M0 is a hard gate: do not start M1 until every M0 item
> has a recorded verdict.** Run every verification command and confirm the
> expected result before moving on. If anything in the "STOP conditions"
> section occurs, stop and report — do not improvise. When done (or when a
> milestone lands), update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d8ec786..HEAD -- edge_deploy/ tests/ pyproject.toml config.example.yaml`
> This plan touches many files; on any drift in `tmux_driver.py`, `auth.py`,
> `release.py`, `runner.py`, or `config.py`, re-verify the "Current state"
> excerpts before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1 (largest single reliability/speed lever identified by the 2026-07-07 audit)
- **Effort**: L (multi-week; M1 ≈ S, M2 ≈ L, M3 ≈ M, M4 ≈ M)
- **Risk**: HIGH overall, mitigated to MED by milestone gating, opt-in-per-node rollout, and the pane remaining the default until the exit criteria in M4
- **Depends on**: plans/006 (prototype — code exists on branch `explore/paramiko-ssh-transport`, commit `8e50cdd`; its **live verdict is not yet captured** — that is M0). Plan 007 (drift → D8 file evidence) should land first: it removes the last screen-scrape read, so the new transport only ever has to serve the uniform `run_remote`/`upload_file`/file-evidence surface.
- **Category**: direction / architecture
- **Planned at**: commit `d8ec786`, 2026-07-07

## Why this matters

Every remote operation today crosses the Authenticated Pane — a screen-scraped
psmux window where multi-line input truncates, quotes are stripped, long lines
die, and output wraps and interleaves (ADR-0011 catalogues six distinct
production failures). The engine survives it via base64 chunking, sentinels,
and digest verification, at the cost of speed (base64 inflation, hundreds of
round trips per deploy) and a permanent tax on every new remote feature. A
persistent Paramiko SSH connection gives real exit codes, separate
stdout/stderr, byte-clean SFTP transfer, and one authentication per phase —
while the AuthBroker seam (ADR-0002) and the D8 file-evidence contract
(ADR-0009) carry over unchanged. The prototype (plan 006) was built and
consolidated onto branch `explore/paramiko-ssh-transport`; what remains is the
live verdict, the policy/dependency gates, and the production integration —
this plan.

## Current state

### The prototype (input material, not production code)

- Branch `explore/paramiko-ssh-transport`, commit `8e50cdd`:
  - `edge_deploy/_prototype_paramiko_transport.py` (1035 lines) — working
    prototype: `NodeSettings` parsed from operator config + `ssh_options`,
    strict `~/.ssh/known_hosts` verification (`_verify_server_key`),
    keyboard-interactive auth, deadline-bounded channel calls
    (`_channel_call_with_deadline`), exec with separate streams
    (`_execute`), SFTP with exec-channel streaming fallback
    (`_sftp_transfer` / `_stream_transfer`), PTY probe, keepalive probe,
    poison-on-teardown (`_poison_and_teardown`).
  - `prototype_paramiko_transport.py` — PEP 723 entry script pinning
    `paramiko==5.0.0`, runs an interactive probe checklist.
  - The expanded `plans/006-*.md` on that branch records the production
    design decisions this plan implements (Transport seam behind `runner.py`,
    pane fallback, AuthBroker as the KI callback, ADR-0013 superseding the
    transport-mechanics half of ADR-0011, **persistent per phase invocation,
    not per Run**).
- **Warning inherited from that plan**: never place prototype/scratch `.py`
  files inside `edge_deploy/` on a working install — Engine Identity hashes
  every `*.py` under the package dir and orphans open Runs. (The prototype
  branch violated its own rule by committing under `edge_deploy/`; production
  code obviously lives there, but scratch experiments must not.)

### The surface the new transport must implement

Engine call-sites use exactly this driver surface (verified by grep at
planning time):

| Method | Callers | Notes |
|---|---|---|
| `run_remote(command, *, timeout)` → `(screen, exit_code)` | runner, rollout, drift, verify, dependencies (10 sites) | THE workhorse; pane returns captured screen text |
| `upload_file(source, remote_path)` → digest | runner, dependencies (3) | chunked-base64 today; SFTP tomorrow |
| `start_session(*, connect_timeout=None, passcode=None)` → bool | auth, release, cli (3) | False ⇒ auth prompt pending |
| `session_exists()` → bool | auth, cli (3) | |
| `await_authenticated(*, timeout=None, poll_interval=1.0)` | auth (3) | raises `AuthenticationError` on reject/timeout |
| `submit_secret(secret)` | auth (2) | literal send-keys today (`tmux_driver.py:446`) |
| `at_shell_prompt(screen=None)` → bool | auth (2) | |
| `stop_session()` | release.py:313 (1) | |
| `send_text(text)` + `wait_for(pattern, timeout)` | auth.py:136-137 (Kerberos kinit only) | interactive PTY dialogue |

tmux-only methods (`capture_screen`, `send_keys`, `attach`, `resize_window`,
`enable_pane_log`, `return_to_shell`, `type_command_confirmed`) are NOT part
of the seam — they stay on `TmuxDriver`.

### Construction sites

- `edge_deploy/cli.py:772` and `cli.py:791`:
  `driver = TmuxDriver.from_node_and_profile(node, profile, retries=2)`
- `edge_deploy/release.py:348`:
  `driver_factory: Callable[..., TmuxDriver] = TmuxDriver.from_node_and_profile`
- `edge_deploy/config.py:191-202` — `NodeConfig` frozen dataclass, fields
  `host`, `ssh_options`, `session`, `name`. Additive fields are the
  established extension pattern (`name` was added that way).

### The AuthBroker contract to preserve (ADR-0002)

`auth.py` drives auth as: `start_session()` → `False` means a passcode prompt
is pending → broker prompts the operator → `driver.submit_secret(code)` →
`driver.await_authenticated()` (raises on reject, so the broker re-prompts
with a fresh RSA code — codes are single-use, ~60 s rotation). The Paramiko
transport must present this same three-call dance, with the KI callback
internally bridged to it (design in M2 step 3).

### Vocabulary to honor (CONTEXT.md)

**Authenticated Pane** and **Pane-Safe** describe the *current* channel, not
the seam. The new ADR-0013 should introduce **Transport** as the domain term
for "the channel a node conversation crosses", keeping ADR-0011's lesson:
treat whatever channel you are given as hostile until proven byte-clean.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `py -m pip install -e ".[dev]"` | exit 0 |
| Install with ssh extra (M2+) | `py -m pip install -e ".[dev,ssh]"` | exit 0, paramiko present |
| Full tests | `py -m pytest -n 4 --dist loadfile` | all pass |
| Lint | `py -m ruff check .` | clean |
| Live e2e (M3, operator-run) | `powershell -File scripts/e2e-release.ps1` | per script output |

(Windows controller: `py` launcher; CI/Linux: `python`.)

## Scope

**In scope across milestones**:
- New: `edge_deploy/transport.py` (M1), `edge_deploy/ssh_transport.py` (M2),
  `docs/adr/0013-transport-seam-and-paramiko-ssh.md` (M1), tests for each.
- Modified: type annotations in `runner.py`, `rollout.py`, `drift.py`,
  `verify.py`, `dependencies.py`, `auth.py`, `release.py` (M1);
  `config.py` + `config.example.yaml` (M3); `cli.py:772,791` +
  `release.py:348` factory wiring (M3); `pyproject.toml` `[ssh]` extra (M2);
  `docs/release-workflow.md`, `README.md`, ADR-0011 postscript (M4).

**Out of scope (do NOT touch)**:
- `TmuxDriver` internals — the pane driver keeps working unchanged; it is the
  fallback and, until M4 exit criteria are met, the default.
- The D8 file-evidence contract (`runner.py` semantics): step results stay
  files under `~/.edge-deploy/runs/<run_id>/steps/`; only the channel that
  reads them changes. Do not "optimize away" file evidence — it buys
  durability across disconnects and post-hoc audit (expanded plan 006).
- Rollback, publish, mirror, audit — controller-side git; no transport involvement.
- Guided-release UX (ADR-0010) beyond what M3's factory requires.

## Git workflow

- One branch/PR per milestone: `advisor/011-m1-transport-seam`,
  `advisor/011-m2-ssh-transport`, `advisor/011-m3-optin-wiring`,
  `advisor/011-m4-hardening-default-criteria`.
- Commit style: conventional prefixes matching `git log` (`feat:`,
  `refactor:`, `docs:`).
- Every milestone that touches `edge_deploy/*.py` changes the engine identity
  and orphans open Runs by design — state it in each PR description.
- Do NOT push or open PRs unless the operator instructed it.

## Milestones

### M0 — Gates (no code; record verdicts before anything else)

1. **Live probe verdict.** From a checkout of
   `explore/paramiko-ssh-transport`, the Release Operator runs
   `uv run prototype_paramiko_transport.py --node node03` (Edge posture, no
   open Runs for the tool, per that plan's preconditions). Record, verbatim,
   in a new `docs/transport-probe-verdict.md`: which probes passed
   (auth/exec/SFTP-or-stream/PTY/keepalive/cleanup), SFTP availability vs
   stream fallback, observed throughput, and any known_hosts negotiation
   fixes that were needed.
2. **Policy confirmation.** ADR-0011 recorded scp/sftp as "not permitted
   through the RSA-authenticated interactive channel". The prototype plan
   flags this as *capability vs. permission*: a green probe does not prove a
   programmatic transport is allowed. Obtain and record (same file) the
   policy owner's answer: is a Paramiko keyboard-interactive SSH session with
   exec/SFTP channels permitted for this release tooling?
3. **Dependency approval.** Adding `paramiko` (+ binary `cryptography` wheel)
   to a corporate-managed Windows workstation is an approval question. Record
   the approved version floor (prototype pinned `paramiko==5.0.0`).

**Verify**: `docs/transport-probe-verdict.md` exists and answers all three.
**Gate**: if probe fails structurally (not config), policy says no, or the
dependency is refused → STOP; record the outcome in `plans/README.md` and
mark this plan BLOCKED with the reason. Everything below assumes green.

### M1 — Transport seam (pure refactor; no paramiko, no behavior change)

1. Create `edge_deploy/transport.py` defining a `RemoteTransport`
   `typing.Protocol` with exactly the 9-row surface table above (same
   signatures as `TmuxDriver` today — copy them from `tmux_driver.py` lines
   210, 225, 324, 334, 341, 442, 446, 484, 506, 585). Include
   `AuthenticationError` re-export or leave it in `tmux_driver` and import —
   decide by where `auth.py` imports it from today (`auth.py:18`), and keep
   that import path working.
2. `TmuxDriver` satisfies the protocol structurally — add a unit test
   asserting `isinstance(TmuxDriver-instance-or-fake, RemoteTransport)` via
   `@runtime_checkable`.
3. Retype the engine: everywhere a parameter is annotated `TmuxDriver`
   (`runner.py:14` TYPE_CHECKING import, `drift.py`, `verify.py`,
   `rollout.py`, `dependencies.py`, `auth.py`, `release.py`) switch the
   annotation to `RemoteTransport`. Runtime imports of
   `TmuxDriver.from_node_and_profile` at construction sites stay.
4. No test-behavior changes expected: `FakeTmuxDriver` already duck-types the
   surface.

**Verify**: `py -m pytest -n 4 --dist loadfile` → all pass, count unchanged.
`grep -rn "TmuxDriver" edge_deploy/ --include=*.py | grep -v "tmux_driver\|from_node_and_profile\|import"` → only construction/factory sites remain.

Also in M1: write `docs/adr/0013-transport-seam-and-paramiko-ssh.md` —
context (ADR-0011's constraints were partly tooling, probe verdict from M0),
decision (seam + Paramiko implementation + pane fallback + per-phase
persistence + AuthBroker as KI bridge), consequences (dependency surface,
engine-identity churn, exit criteria for making ssh the default — see M4).
State explicitly that ADR-0011's *lesson* stands and its pane protocol
remains in force whenever the pane transport is selected.

### M2 — `ParamikoSshTransport` (new module + optional dependency; not yet reachable from the CLI)

1. `pyproject.toml`: add `[project.optional-dependencies] ssh = ["paramiko>=<M0-approved floor>"]`.
   `edge_deploy/ssh_transport.py` guards the import:

   ```python
   try:
       import paramiko
   except ImportError as exc:  # pragma: no cover
       raise TransportUnavailable(
           "paramiko is not installed; install the [ssh] extra or use transport: pane"
       ) from exc
   ```

2. Port from the prototype (`git show 8e50cdd:edge_deploy/_prototype_paramiko_transport.py`),
   keeping: strict known_hosts verification (never auto-accept),
   deadline-bounded channel calls, poison-on-teardown, SFTP-with-stream-fallback
   upload. Drop: checklist UI, probe methods, PEP 723 header.
3. **Auth bridge** — the delicate part. Implement the three-call dance as a
   small state machine so `AuthBroker` works unmodified:
   - `start_session()`: open the TCP connection + transport, begin KI auth in
     a worker thread whose KI handler *blocks on an internal queue* when the
     server asks for the passcode. Return `False` (prompt pending) — or
     `True` if the server authenticated without a prompt.
   - `submit_secret(code)`: put the code on the queue (never logged; held
     only in the queue slot).
   - `await_authenticated(timeout=...)`: join the auth outcome; on server
     reject raise `AuthenticationError` (so the broker re-prompts fresh —
     preserve tmux semantics exactly; see `tests/test_auth_seam.py` reject →
     accept scripts).
   - `at_shell_prompt()` / `session_exists()`: transport-active checks
     (`transport.is_authenticated()` etc.).
4. `run_remote(command, *, timeout)` → `(text, exit_code)`: exec channel,
   merge stdout+stderr into `text` in arrival order (engine call-sites parse
   "screen" text today; exact exit codes are the win — do NOT change the
   tuple shape). `upload_file(source, remote_path)` → SFTP put (stream
   fallback per prototype), return the same SHA-256 digest string the pane
   driver returns, verified remotely exactly as today (`sha256sum` compare).
   `send_text`/`wait_for`: PTY channel dialogue for the kinit path
   (`auth.py:136-137`). `stop_session()`: close channels + transport.
5. Tests: `tests/test_ssh_transport.py` with a **fake paramiko layer**
   (no network, no real keys): fake Transport/Channel/SFTP objects injected
   via a factory parameter. Cover: KI dance (prompt → submit →
   authenticated), reject → `AuthenticationError`, exec exit codes, upload
   digest verify, `TransportUnavailable` when import fails
   (`monkeypatch.setitem(sys.modules, "paramiko", None)` pattern), timeout →
   poison-teardown. Model test style on `tests/test_auth_seam.py`.
   Mark the whole module `pytest.importorskip("paramiko")` EXCEPT the
   unavailability test, so `[dev]`-only environments (CI today) still pass.
   Optionally add an `ssh` extra to one CI matrix cell so the suite runs.

**Verify**: `py -m pytest -n 4 --dist loadfile` all pass without paramiko
installed; then `py -m pip install -e ".[dev,ssh]"` and again all pass with
the new module's tests active. `py -m ruff check .` clean.

### M3 — Opt-in wiring per node

1. `config.py`: additive `NodeConfig.transport: str = "pane"` (follow the
   `name` field's additive pattern; validate value in `{"pane", "ssh"}` at
   load with a clear error). Document in `config.example.yaml`.
2. New factory in `transport.py`:

   ```python
   def transport_for_node(node, profile, *, retries: int = 2) -> RemoteTransport:
       if getattr(node, "transport", "pane") == "ssh":
           from edge_deploy.ssh_transport import ParamikoSshTransport
           return ParamikoSshTransport.from_node_and_profile(node, profile, retries=retries)
       return TmuxDriver.from_node_and_profile(node, profile, retries=retries)
   ```

   Swap the three construction sites (`cli.py:772`, `cli.py:791`,
   `release.py:348` default) to `transport_for_node`. `release.py`'s
   `driver_factory` test seam keeps working — tests inject fakes as before.
3. Preflight: extend `preflight` output to state which transport each node is
   configured for (read-only report line; no probe change required in this
   milestone).
4. Tests: factory selection by config value; unknown value rejected at config
   load; ssh selected but extra not installed → the `TransportUnavailable`
   message reaches the operator intact.
5. **Live validation (operator, not executor)**: set `transport: ssh` for
   node03 only, run one full release via `scripts/e2e-release.ps1`
   (node04 stays on pane — mixed-transport release is itself a test).
   Capture the run's reports and pane/SSH evidence under the run directory as
   usual. Two clean releases here are the entry ticket to M4.

**Verify**: full suite passes; `grep -rn "TmuxDriver.from_node_and_profile" edge_deploy/ | grep -v transport.py | grep -v tmux_driver.py` → no direct construction outside the factory.

### M4 — Hardening and default-flip criteria (not the flip itself)

1. Port the transport-level niceties that the pane never had: keepalive
   configuration from `ssh_options` (prototype `_ssh_option_values`),
   connection-drop mid-phase → clear `SessionGoneError`-equivalent mapping so
   `release.py`'s existing exception handling (`release.py:631`) reports a
   synthetic failed check instead of a stack trace.
2. Docs: `docs/release-workflow.md` gains a short "Transports" section;
   README one line; ADR-0011 gets a dated postscript pointing to ADR-0013.
3. Record in ADR-0013 the **exit criteria for flipping the default** to ssh
   (suggested: N consecutive clean production releases across both nodes on
   `transport: ssh`, zero transport-attributed failures, policy sign-off
   unchanged). The flip itself is a future one-line config-default change —
   deliberately not in this plan.

**Verify**: full suite; docs build not applicable (plain markdown); manual
review of ADR-0013 completeness against this plan's design decisions.

## Test plan

- M1: protocol-conformance test (`TmuxDriver`/`FakeTmuxDriver` satisfy
  `RemoteTransport`); zero regression in the 369-test suite.
- M2: `tests/test_ssh_transport.py` — KI auth dance happy/reject/timeout,
  exec exit codes, upload digest, unavailability, teardown poisoning
  (~12 tests; fake paramiko, no network).
- M3: factory selection + config validation tests (~4 tests); live
  mixed-transport release evidence (operator).
- Structural pattern exemplars: `tests/test_auth_seam.py` (scripted auth
  outcomes), `tests/conftest.py:96` FakeTmuxDriver (knob-style fakes).

## Done criteria (plan-level; each milestone also has its Verify gates)

- [ ] `docs/transport-probe-verdict.md` records probe + policy + dependency verdicts
- [ ] `edge_deploy/transport.py` protocol exists; engine annotated against it, not `TmuxDriver`
- [ ] `edge_deploy/ssh_transport.py` passes its fake-paramiko suite; repo installs and tests green **both with and without** the `[ssh]` extra
- [ ] `transport: ssh` on a node routes through `ParamikoSshTransport`; default remains `pane`
- [ ] ADR-0013 committed; ADR-0011 postscript added
- [ ] Two clean e2e releases with node03 on ssh recorded under `edge-deploy/runs/`
- [ ] `py -m pytest -n 4 --dist loadfile` exits 0 at every milestone boundary
- [ ] `plans/README.md` status row updated per milestone

## STOP conditions

Stop and report back (do not improvise) if:

- Any M0 gate is not green — this plan must not proceed on capability alone.
- The KI auth bridge cannot preserve the exact `start_session → submit_secret
  → await_authenticated` semantics `auth.py` relies on (e.g. Paramiko's KI
  handler model won't suspend mid-handshake on this server) — that
  invalidates the "AuthBroker unmodified" premise; the seam design needs a
  human decision, not a workaround.
- The server rejects exec/SFTP channels at runtime despite a green probe
  (environment drift between probe and productization).
- `run_remote`'s merged-stream text breaks an engine call-site that turns out
  to parse pane-specific artifacts (prompt lines, echo) — report the call
  site; do not shim fake prompts into the SSH output.
- Any milestone's full-suite verification fails twice after a reasonable fix
  attempt.
- You are tempted to modify `TmuxDriver` or the D8 evidence contract — both
  are explicitly out of scope.

## Maintenance notes

- **Engine identity**: every milestone orphans open Runs on install; operators
  must complete/abandon open Runs before upgrading (CONTEXT.md). Sequence
  milestone releases between production releases.
- **Reviewers should scrutinize**: the KI state machine's thread/queue
  handling (secret must never outlive the handshake or reach a log/report —
  compose with plan 010's `register_transient_secret` at the prompt site,
  which already covers it since the broker prompts through `auth.py`);
  the SFTP fallback path (prototype's `_stream_transfer`) digest handling;
  that `run_remote`'s tuple shape and text semantics stayed compatible.
- **Interaction with other plans**: 007 should land first (drift on D8);
  010's redaction registry is orthogonal but compounding; plan 003/008 CI
  changes may want an `[ssh]`-extra matrix cell once M2 lands.
- **Deliberately deferred**: flipping the default transport (criteria in
  ADR-0013); per-Run persistent connections (phases are separate processes —
  revisit only if guided mode ever becomes a single long-lived process);
  deleting the pane driver (never — it is the fallback for policy or
  environment regressions).
