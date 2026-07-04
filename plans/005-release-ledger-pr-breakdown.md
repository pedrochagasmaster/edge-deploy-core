# Plan 005: PR-by-PR breakdown of the release-ledger architecture (Plan 004)

> **Executor instructions**: This document decomposes Plan 004 into 20
> independently mergeable PRs. Each PR section is self-contained: an
> implementer must read §0 (Shared definitions) plus their own PR section, and
> nothing else. Follow the section exactly — where it says an error message
> string, produce that exact string; where it says a function signature,
> produce that exact signature. Do not add features, flags, or refactors that
> are not listed. Every PR must leave `py -m pytest -n 4 --dist loadfile` and
> `py -m ruff check .` green from the repo root.
>
> **Drift check (run before starting any PR)**: confirm the symbols named in
> your PR's "Anchors" list still exist via `rg`. If an anchor is missing or
> renamed, STOP and report — do not improvise.

## Status

- **Priority**: P0
- **Planned at**: commit `ff3393f`, 2026-07-03
- **Supersedes**: the milestone grouping in `plans/004-release-ledger-architecture.md`
  (the architecture rationale there still applies; read its "Why" section once)

---

## Dependency graph and parallel execution

### DAG

```
Wave 1            Wave 2           Wave 3            Wave 4              Wave 5           Wave 6            Wave 7
──────            ──────           ──────            ──────              ──────           ──────            ──────
PR-01 ledger ───► PR-05 resume ──► PR-07 phases ──┬► PR-10 verify ────┐
                                   scaffold       ├► PR-11 publish ───┤
PR-02 posture ────────────────────►(needs 02+05)  ├► PR-12 deploy ────┼► PR-15 release ─► PR-17 status ──┐
                                                  └► PR-13 tag ───────┘   +rollback                      │
PR-04 heartbeat ──────────────────────────────────────────────────────────────────────► PR-18 auth ────┤
                                                      (PR-18 also needs PR-12)             broker        ├► PR-20 docs
PR-03 delete ───► PR-06 verified ┬► PR-08 pipe-pane                                                      │   + sweep
     multiplex        transfer   └► PR-09 runner ───► PR-14 deps-via ─► PR-16 rollout ─► PR-19 legacy ──┘
                                       bootstrap        runner            via-runner        shim
```

### Edge list (authoritative; the drawing is a projection)

| PR | Depends on (merged first) | Why |
|----|--------------------------|-----|
| PR-01 | — | new module |
| PR-02 | — | new module |
| PR-03 | — | deletion only |
| PR-04 | — | progress.py only |
| PR-05 | PR-01 | imports ledger |
| PR-06 | PR-03 | same file (tmux_driver.py), builds on pane-only transport |
| PR-07 | PR-02, PR-05 | phase gate uses posture + ledger |
| PR-08 | PR-06 | same file (tmux_driver.py) |
| PR-09 | PR-06 | uses verified transfer primitive |
| PR-10 | PR-07 | phase module |
| PR-11 | PR-07 | phase module |
| PR-12 | PR-07 | phase module |
| PR-13 | PR-07 | phase module |
| PR-14 | PR-09 | runner step API |
| PR-15 | PR-10, PR-11, PR-12, PR-13 | chains all phases |
| PR-16 | PR-14 | runner conversion continues |
| PR-17 | PR-15 | status needs final phase set |
| PR-18 | PR-04, PR-12 | wires heartbeat into deploy auth |
| PR-19 | PR-16 | runner owns install |
| PR-20 | all | docs describe the finished system |

### Wave schedule (maximum safe parallelism)

Same-file contention is already encoded in the edges (`cli.py` is only touched
by PR-05→07→…→15→17→18 in dependency order; `tmux_driver.py` only by
PR-03→06→08/09; PR-08 and PR-09 both touch `tmux_driver.py` — PR-08 adds one
method, PR-09 adds a new module and one import; treat PR-08 → PR-09 as
sequential if the same agent pool is tight, else accept a trivial rebase).

| Wave | PRs in parallel | Width |
|------|-----------------|-------|
| 1 | PR-01, PR-02, PR-03, PR-04 | 4 |
| 2 | PR-05, PR-06 | 2 |
| 3 | PR-07, PR-08, PR-09 | 3 |
| 4 | PR-10, PR-11, PR-12, PR-13, PR-14 | 5 |
| 5 | PR-15, PR-16 | 2 |
| 6 | PR-17, PR-18, PR-19 | 3 |
| 7 | PR-20 | 1 |

- **Critical path** (7 long): PR-01 → PR-05 → PR-07 → PR-12 → PR-15 → PR-17/18 → PR-20.
- **Peak parallelism**: 5 agents (Wave 4). Average ~3.
- **Run as a workflow**: one agent per PR in an isolated git worktree, wave =
  barrier. Wave N+1 agents branch from `main` after all Wave N PRs are merged.
  The four Wave-4 phase PRs each add one line to the same registry list
  (`PHASE_REGISTRY` in `edge_deploy/phases/__init__.py`); on merge conflict,
  keep **all** entries sorted by `order`.

### Branch and PR conventions (all PRs)

- Branch: `codex/pr-NN-<slug>` (slug given per PR). Base: current `main`.
- Commit style: single focused commit, message given per PR.
- PR body: summary bullets + `## Test plan` listing the verification commands
  actually run and their results.
- Never touch: `vendor/`, operator config, anything under `edge-deploy/`
  (generated), other tools' repos.

---

## §0 Shared definitions (read this first, always)

### D1. Runs root and run directory

- Runs root: `<tool repo root>/edge-deploy/runs/` (sibling of the existing
  `edge-deploy/reports/`).
- Run id format: `run-<UTC %Y%m%dT%H%M%SZ>-<source_sha[:7]>`
  e.g. `run-20260703T120000Z-aa6d9a5`.
- Run directory: `<runs root>/<run_id>/` containing `state.json`,
  `events.jsonl`, `run.lock` (while locked), and all report artifacts that
  previously went to `edge-deploy/reports/release-*/`.

### D2. `state.json` schema (`edge-deploy/run/1`)

```json
{
  "schema": "edge-deploy/run/1",
  "run_id": "run-20260703T120000Z-aa6d9a5",
  "tool": "autobench",
  "source_sha": "<40-hex reviewed GitHub commit>",
  "operator": "e176097@mastercard.com",
  "created_at": "2026-07-03T12:00:00+00:00",
  "kind": "release",
  "rollback_tag": null,
  "engine": {
    "version": "1.1.0",
    "package_dir": "D:\\Projects\\edge-deploy-core\\edge_deploy",
    "content_sha256": "<see D3>"
  },
  "nodes": ["node03", "node04"],
  "status": "open",
  "abandon_reason": null,
  "phases": {
    "verify":        {"state": "pending", "updated_at": null, "evidence": {}},
    "publish":       {"state": "pending", "updated_at": null, "evidence": {}},
    "deploy":        {"node03": {"state": "pending", "updated_at": null, "evidence": {}},
                      "node04": {"state": "pending", "updated_at": null, "evidence": {}}},
    "tag_github":    {"state": "pending", "updated_at": null, "evidence": {}},
    "tag_bitbucket": {"state": "pending", "updated_at": null, "evidence": {}}
  }
}
```

- `kind` is `"release"` or `"rollback"`; `rollback_tag` is the `release-*` tag
  for rollbacks, else `null`.
- Legal `state` values: `pending | passed | failed | skipped`.
- `status` values: `open | complete | abandoned`.
- Writes are atomic: write `state.json.tmp`, then `os.replace` onto
  `state.json`.

### D3. Engine identity

`engine.content_sha256` = sha256 of the UTF-8 concatenation of
`f"{relpath}\n{file_sha256}\n"` for every `*.py` file directly inside the
installed `edge_deploy` package directory, sorted by `relpath` (POSIX
separators, relative to the package dir). Deterministic across editable
installs, wheels, and `PYTHONPATH` — that is the point.

### D4. `events.jsonl`

One JSON object per line, appended (never rewritten):
`{"ts": "<ISO8601 UTC>", "event": "<snake_case>", "phase": <str|null>, "node": <str|null>, ...extra}`.
Minimum event vocabulary: `run_created`, `phase_entered`, `phase_passed`,
`phase_failed`, `phase_skipped`, `lock_stolen`, `run_abandoned`,
`run_completed`.

### D5. Lock file

`run.lock` = JSON `{"pid": <int>, "hostname": "<socket.gethostname()>", "acquired_at": "<ISO8601 UTC>"}`.
If present when acquiring, raise `RunLockError` with exactly:
`run <run_id> is locked by PID <pid> on <hostname> (acquired <acquired_at>); if that process is dead, re-run with --force-lock`.
`--force-lock` deletes the lock and records a `lock_stolen` event.

### D6. Posture endpoint names

| Key | Host:port probed |
|-----|------------------|
| `github` | `github.com:443` |
| `github-api` | `api.github.com:443` |
| `bitbucket` | `scm.mastercard.int:443` |
| `edge` | every configured node's `hostname:port` from `OperatorConfig` |

Phase → required endpoints: `verify` → `["github-api"]`; `publish` →
`["bitbucket"]`; `deploy` → `["bitbucket", "edge"]`; `tag_github` →
`["github"]`; `tag_bitbucket` → `["bitbucket"]`.

### D7. Posture error message (exact format)

```
phase '<phase>' requires posture [<keys, comma-joined>]; unreachable: <host:port, comma-joined>.
Switch the firewall posture, then re-run: <next command>
```

### D8. Wrap-immune remote read protocol (used by PR-09/14/16)

To read a small remote file `F` through the pane without screen-wrap
corruption:

1. Send one command:
   `printf '\n__EDGE_RESULT_START__\n'; base64 -w0 <F>; printf '\n__EDGE_RESULT_SHA_%s__\n' "$(sha256sum <F> | cut -d' ' -f1)"; printf '__EDGE_RESULT_END__\n'`
2. Capture the screen (existing `capture_screen`, generous history).
3. Extract the span between `__EDGE_RESULT_START__` and
   `__EDGE_RESULT_SHA_`; **delete every whitespace character** (pane wrapping
   inserts newlines anywhere) to recover the base64 string.
4. Extract `<hex>` from `__EDGE_RESULT_SHA_<hex>__`.
5. base64-decode; verify sha256 of the decoded bytes equals `<hex>`; on
   mismatch raise `RunnerProtocolError("remote result digest mismatch for <F>")`.
6. Parse JSON.

This is the ONLY sanctioned way to move structured data node→controller.
`capture_screen` text must never be parsed for data by any other means.

---

## PR-01 — `edge_deploy/ledger.py` (run ledger core)

- **Branch**: `codex/pr-01-run-ledger` — **Commit**: `feat: add run ledger module`
- **Files**: create `edge_deploy/ledger.py`, `tests/test_ledger.py`. No other file changes.
- **Anchors**: `edge_deploy/__init__.py::__version__` exists.

Implement in `edge_deploy/ledger.py`:

```python
class LedgerError(RuntimeError): ...
class RunLockError(LedgerError): ...

def engine_identity() -> dict:  # {"version", "package_dir", "content_sha256"} per D3

@dataclass
class RunLedger:
    run_dir: Path
    state: dict  # the D2 document, kept in sync with disk

    @classmethod
    def create(cls, runs_root: Path, *, tool: str, source_sha: str,
               nodes: list[str], operator: str, kind: str = "release",
               rollback_tag: str | None = None) -> "RunLedger"
    @classmethod
    def load(cls, run_dir: Path) -> "RunLedger"          # LedgerError if missing/invalid schema
    @classmethod
    def find_open(cls, runs_root: Path) -> list["RunLedger"]   # status == "open", sorted by created_at

    def record_event(self, event: str, *, phase: str | None = None,
                     node: str | None = None, **extra) -> None   # D4 append
    def set_phase(self, phase: str, state: str, *, node: str | None = None,
                  evidence: dict | None = None) -> None          # atomic write per D2
    def phase_state(self, phase: str, *, node: str | None = None) -> str
    def abandon(self, reason: str) -> None    # status="abandoned", abandon_reason, event
    def complete(self) -> None                # status="complete", event

    def acquire_lock(self, *, force: bool = False) -> None   # D5; force deletes + lock_stolen event
    def release_lock(self) -> None
    @contextmanager
    def locked(self, *, force: bool = False): ...
```

Rules: `create` builds the full D2 document (deploy sub-dict keyed by the
given nodes), writes `state.json` atomically, appends `run_created`, and
returns the instance. `set_phase` on `phase="deploy"` requires `node`.
Unknown phase or state value → `LedgerError`.

- **Tests** (`tests/test_ledger.py`): create→load round-trip preserves the
  document; `set_phase` persists atomically (read file directly); deploy
  requires node; `find_open` excludes abandoned/complete; lock collision
  raises `RunLockError` with the exact D5 message; `locked(force=True)` over a
  stale lock appends `lock_stolen`; `engine_identity()["content_sha256"]` is
  64 hex chars and changes when a temp copy of the package gains a file
  (compute over a copied tree in `tmp_path` via a helper that accepts a
  package dir argument — expose `_content_sha256(package_dir: Path) -> str`
  for this).
- **Verify**: `py -m pytest tests/test_ledger.py -q` then full suite + ruff.
- **Out of scope**: any CLI change, any use of the ledger by other modules.

## PR-02 — `edge_deploy/posture.py` (network posture probes)

- **Branch**: `codex/pr-02-posture` — **Commit**: `feat: add network posture probes`
- **Files**: create `edge_deploy/posture.py`, `tests/test_posture.py`.
- **Anchors**: `edge_deploy/config.py::OperatorConfig` with `.nodes` mapping;
  `edge_deploy/preflight.py` shows the existing endpoint-parsing pattern
  (`NodeConfig.host` like `user@host`, `ssh_options` carrying `-p 2222`).

Implement:

```python
@dataclass(frozen=True)
class Endpoint:
    key: str      # posture key (D6)
    host: str
    port: int

PHASE_ENDPOINTS: dict[str, tuple[str, ...]] = {  # exactly D6's table
    "verify": ("github-api",), "publish": ("bitbucket",),
    "deploy": ("bitbucket", "edge"), "tag_github": ("github",),
    "tag_bitbucket": ("bitbucket",),
}

class PostureError(RuntimeError): ...  # message per D7

def endpoints_for(phase: str, operator: OperatorConfig | None) -> list[Endpoint]
    # expands "edge" into one Endpoint per configured node, reusing the
    # hostname/port parsing already used by edge_deploy.preflight
def probe(endpoints: list[Endpoint], *, timeout: float = 2.0,
          connect=socket.create_connection) -> list[Endpoint]   # returns UNREACHABLE endpoints
def require_posture(phase: str, operator: OperatorConfig | None,
                    *, next_command: str, connect=socket.create_connection) -> None
    # raises PostureError (D7 exact format) listing unreachable host:port pairs
```

- **Tests**: injected fake `connect` — all reachable → no raise; one edge node
  unreachable → message matches D7 exactly (assert full string); `"edge"`
  expansion produces one endpoint per configured node with the right port
  (2222 from `ssh_options`); unknown phase → `KeyError`.
- **Out of scope**: wiring into any command; no real network calls in tests.

## PR-03 — delete the SSH-multiplex transport

- **Branch**: `codex/pr-03-delete-multiplex` — **Commit**: `refactor: single pane transport, drop ControlMaster`
- **Files**: `edge_deploy/tmux_driver.py`, `tests/test_tmux_driver.py`, `edge_deploy/dependencies.py` (only if it references multiplex), docs mentions.
- **Anchors**: `tmux_driver.py::_ssh_multiplex_enabled` (line ~40),
  `TmuxDriver.upload_file` scp branch (~380), `use_ssh_multiplex` attribute,
  `control_path` attribute, `EDGE_DEPLOY_SSH_MULTIPLEX` env var.

Do exactly:
1. Delete `_ssh_multiplex_enabled` and every read of `EDGE_DEPLOY_SSH_MULTIPLEX`.
2. Delete the scp branch of `upload_file`; `upload_file` body becomes the
   current `_upload_file_via_pane` (rename it; keep the public name
   `upload_file`).
3. Remove `use_ssh_multiplex` and all `ControlMaster`/`ControlPath`/
   `ControlPersist` option construction from `_build_pane_command`. Keep
   `control_path` only if `stop_session` still unlinks it — instead delete the
   attribute and the unlink line.
4. Update/delete tests that assert multiplex behavior; keep the test proving
   the pane command contains no `ControlMaster`.
5. `rg -n "multiplex|ControlMaster|ControlPath|EDGE_DEPLOY_SSH_MULTIPLEX" edge_deploy tests docs` must return zero code hits (docs may keep one historical ADR mention).
- **Out of scope**: changing the pane-upload algorithm (that is PR-06).

## PR-04 — `waiting_on` operator heartbeat state

- **Branch**: `codex/pr-04-waiting-heartbeat` — **Commit**: `feat: distinguish operator-wait from running in progress heartbeat`
- **Files**: `edge_deploy/progress.py`, `tests/test_progress.py`.
- **Anchors**: `progress.py::ActiveOperation` (~19), `ReleaseProgressTracker.start` (~76), `_write_progress_json` (~174).

1. Add field `waiting_on: str | None = None` to `ActiveOperation`.
2. `ReleaseProgressTracker.start(...)` gains keyword `waiting_on: str | None = None`.
3. Add method `set_waiting(self, waiting_on: str | None) -> None` that mutates
   the active operation and immediately rewrites the progress JSON.
4. Progress JSON `active` object gains `"waiting_on"` key (null or string).
5. Heartbeat line format: when `waiting_on == "operator"`, emit
   `>>> WAITING FOR OPERATOR - <label> (<elapsed>s) <<<` instead of the
   current `still running: <label> (<n>s elapsed)` line.
- **Tests**: progress JSON contains `waiting_on`; heartbeat text switches
  format when waiting; `set_waiting(None)` reverts.
- **Out of scope**: any caller change (PR-18 wires it).

## PR-05 — ledger wiring, explicit resume, `abandon`

- **Branch**: `codex/pr-05-explicit-resume` — **Commit**: `feat: ledger-backed runs with explicit resume only`
- **Files**: `edge_deploy/cli.py`, `tests/test_cli.py`.
- **Anchors**: `cli.py::_cmd_release`, `_cmd_rollback`, `_default_report_dir`,
  `_load_resume_provenance`, release parser `--resume` argument.

1. New helper in cli.py: `_runs_root(repo_root: Path) -> Path` returning
   `repo_root / "edge-deploy" / "runs"`.
2. In `_cmd_release`: **before** `_run_release_preflight`, resolve the run:
   - If `--run <run_id>` given: `RunLedger.load(_runs_root(...) / run_id)`;
     error message on missing: `no such run: <run_id> under <runs root>`.
   - Else: if `RunLedger.find_open(...)` is non-empty, print exactly:
     ```
     release refused: unresolved run <run_id> for <tool> (source <sha7>, created <created_at>) exists.
     Choose one:
       1. continue it:   python -m edge_deploy release --run <run_id>
       2. abandon it:    python -m edge_deploy abandon --run <run_id> --reason "<why>"
     ```
     and return exit code 2. (List each open run if several.)
   - Else create a new run via `RunLedger.create` (source_sha = current HEAD
     via `inspect_repository` — call it once, reuse for preflight).
3. `report_dir` becomes the run directory. Delete the `--resume <report dir>`
   argument, `_load_resume_provenance`, and the `ResumeProvenance` dataclass;
   resume provenance now comes from the ledger's `phases.publish.evidence`
   (for this PR, keep writing `publish-<tool>.json` as today AND mirror
   `{"snapshot_sha", "source_commit"}` into
   `set_phase("publish", "passed", evidence=...)` after `run_release` returns,
   derived from `report.publishes` — full phase split comes later).
4. Wrap the body of `_cmd_release`/`_cmd_rollback` in `ledger.locked(force=args.force_lock)`;
   add `--force-lock` (store_true) to both parsers.
5. New subcommand `abandon` with `--run` (required) and `--reason` (required):
   loads, calls `ledger.abandon(reason)`, prints `abandoned <run_id>`.
6. `_cmd_rollback` creates its run with `kind="rollback"`,
   `rollback_tag=args.tag`, and records publish evidence from
   `_write_rollback_publish_provenance` values (keep that function).
7. On `report.exit_code() == 0` mark `ledger.complete()`; on failure leave
   `open` (that IS the resume mechanism).
- **Tests** (extend `tests/test_cli.py`, using existing fakes): bare release
  with an open run prints the exact refusal text and exits 2; `--run` resumes
  into the same directory; `abandon` flips status and `find_open` no longer
  returns it; lock held → `RunLockError` surfaces as exit 2 via the existing
  exception handler (add `LedgerError` to the `except` tuple in `main`);
  successful release marks run complete.
- **Out of scope**: phase subcommands, posture, status.

## PR-06 — verified, reusable pane transfer

- **Branch**: `codex/pr-06-verified-transfer` — **Commit**: `feat: digest-verified pane file transfer with reuse`
- **Files**: `edge_deploy/tmux_driver.py`, `tests/test_tmux_driver.py`.
- **Anchors**: `TmuxDriver.upload_file` (post-PR-03: the pane implementation),
  `run_remote`, D8.

1. Compute the local file's sha256 before upload.
2. Pre-check: run
   `test -f <remote_path> && sha256sum <remote_path> | cut -d' ' -f1 || echo MISSING`
   via `run_remote`; if the captured last non-empty line equals the local
   digest, return without uploading (add `return_digest: bool` no — keep
   signature `upload_file(self, source, remote_path) -> str` returning the
   digest; update the two call sites in `dependencies.py`).
3. After the existing chunked upload + decode, verify: run
   `sha256sum <remote_path> | cut -d' ' -f1` and compare against the local
   digest reading only the last non-empty captured line (64-hex regex
   `\b[0-9a-f]{64}\b`, wrap-safe because 64 chars < pane width; if not found,
   re-capture with more history once). Mismatch → delete remote file, raise
   `RuntimeError(f"authenticated bundle transfer failed: digest mismatch for {remote_path}")`.
- **Tests**: fake driver records commands — upload skipped when pre-check
  digest matches; digest mismatch raises and issues `rm -f`; success returns
  the digest.
- **Out of scope**: chunk-level resume; runner (PR-09).

## PR-07 — phases scaffold and `enter_phase` gate

- **Branch**: `codex/pr-07-phases-scaffold` — **Commit**: `feat: phase command scaffold with ledger+posture+engine gate`
- **Files**: create `edge_deploy/phases/__init__.py`, `tests/test_phases.py`; modify `edge_deploy/cli.py` (registry hook only).
- **Anchors**: PR-01 `RunLedger`, `engine_identity`; PR-02 `require_posture`; `cli.py::build_parser`.

`edge_deploy/phases/__init__.py`:

```python
@dataclass(frozen=True)
class PhaseSpec:
    name: str            # "verify" | "publish" | "deploy" | "tag_github" | "tag_bitbucket"
    order: int           # 10, 20, 30, 40, 50
    endpoints: tuple[str, ...]   # from posture.PHASE_ENDPOINTS

# Each phase module (PR-10..13) appends a (PhaseSpec, register_fn) pair here.
# register_fn(subparsers) adds its argparse subcommand.
PHASE_REGISTRY: list[tuple[PhaseSpec, Callable]] = []

class EngineMismatchError(RuntimeError): ...

def enter_phase(spec: PhaseSpec, operator, ledger: RunLedger, *,
                next_command: str, force_lock: bool = False,
                connect=socket.create_connection) -> ExitStack:
```

`enter_phase` (in order): (1) `ledger.acquire_lock(force=force_lock)` —
returned `ExitStack` releases it; (2) engine check: if
`ledger.state["engine"]["content_sha256"] != engine_identity()["content_sha256"]`
raise `EngineMismatchError` with exactly
`engine mismatch: run <run_id> was created by engine <old8> but this process is <new8>; finish the run with the original engine or abandon it`
(first 8 hex chars each); (3) `require_posture(spec.name, operator, next_command=next_command, connect=connect)`;
(4) `ledger.record_event("phase_entered", phase=spec.name)`.

`cli.py`: in `build_parser`, after existing subparsers, iterate
`PHASE_REGISTRY` (sorted by order) calling each `register_fn(subparsers)`; in
`main`, dispatch `args.command` to a `func` attribute set via
`parser.set_defaults(func=...)` by each register_fn (existing commands keep
their if/elif chain). Add `EngineMismatchError`, `PostureError`, `LedgerError`
to the `except` tuple in `main`.

- **Tests**: `enter_phase` order of failures (lock → engine → posture) using
  fakes; exact engine-mismatch message; registry-driven subcommand appears in
  `--help` when a dummy phase registers.
- **Out of scope**: real phase implementations.

## PR-08 — pane logging (`pipe-pane` with capture fallback)

- **Branch**: `codex/pr-08-pane-logging` — **Commit**: `feat: full-fidelity local pane logs`
- **Files**: `edge_deploy/tmux_driver.py`, `tests/test_tmux_driver.py`.
- **Anchors**: `TmuxDriver.start_session`, `_tmux`.

1. Add `TmuxDriver.enable_pane_log(self, log_path: Path) -> bool`: runs
   `tmux pipe-pane -t <session> -o "cat >> <log_path>"` with `check=False`;
   returns `returncode == 0`. Store `self.pane_log_supported: bool | None`.
2. In `start_session`, after the pane is created, if the driver was
   constructed with `pane_log_path` (new optional `__init__` kwarg, default
   `None`), call `enable_pane_log`. If unsupported (psmux may reject
   `pipe-pane`), record `self.pane_log_supported = False` and do nothing else
   — callers may then use `capture_screen` snapshots; do NOT implement a
   polling fallback in this PR.
- **Tests**: fake `_tmux` asserting the pipe-pane argv; unsupported (rc=1)
  sets the flag and does not raise.
- **Out of scope**: wiring `pane_log_path` from release code (PR-12 does it).

## PR-09 — node runner and step API

- **Branch**: `codex/pr-09-node-runner` — **Commit**: `feat: on-node runner with file-based step results`
- **Files**: create `edge_deploy/runner.py`, `tests/test_runner.py`.
- **Anchors**: PR-06 `upload_file` returning digest; `run_remote`; D8.

`edge_deploy/runner.py` contains:

1. `RUNNER_SCRIPT: str` — a POSIX-sh script (module-level constant). Contract:
   `sh runner.sh <run_id> <step_name> <b64_command>` → mkdir -p
   `~/.edge-deploy/runs/<run_id>/steps/`; decode `<b64_command>`; execute via
   `sh -c`, capturing stdout+stderr to
   `~/.edge-deploy/runs/<run_id>/steps/<step_name>.out`; write
   `~/.edge-deploy/runs/<run_id>/steps/<step_name>.json` containing
   `{"schema":"edge-deploy/step/1","step":"<step_name>","exit_code":<int>,"started_at":"<iso>","finished_at":"<iso>","stdout_tail":"<last 40 lines of .out>"}`
   (JSON written with python3 if available, else a here-doc printf — use
   python3; nodes have it); always exit 0 itself (the step's exit code lives
   in the JSON).
2. `RUNNER_VERSION = "1"` and `runner_sha256() -> str` (sha256 of
   `RUNNER_SCRIPT` bytes).
3. `class RunnerProtocolError(RuntimeError)`.
4. `def bootstrap_runner(driver, run_id: str) -> str` — writes RUNNER_SCRIPT
   to a local temp file, `driver.upload_file(tmp, f"~/.edge-deploy/runner-{RUNNER_VERSION}-{runner_sha256()[:8]}.sh")`
   (upload_file's digest verification is the integrity check), returns the
   remote path. Idempotent by upload_file's reuse pre-check.
5. `def run_step(driver, runner_path: str, run_id: str, step_name: str, command: str, *, timeout: float) -> dict`
   — sends `sh <runner_path> <run_id> <step_name> <base64(command)>` via
   `driver.run_remote(..., timeout=timeout)`; then reads
   `~/.edge-deploy/runs/<run_id>/steps/<step_name>.json` using **exactly the
   D8 protocol** implemented as `read_remote_json(driver, remote_path) -> dict`;
   validates `schema == "edge-deploy/step/1"` and `step == step_name`; returns
   the dict. Any protocol violation → `RunnerProtocolError`.
- **Tests**: fake driver scripted with canned screens — `read_remote_json`
  survives base64 broken across lines with arbitrary newlines/spaces (build
  the canned screen by inserting `\n` every 80 chars); digest mismatch raises;
  `run_step` composes the right command line; `bootstrap_runner` target path
  embeds version+digest.
- **Out of scope**: converting any existing call site (PR-14/16).

## PR-10 — `verify` phase

- **Branch**: `codex/pr-10-verify-phase` — **Commit**: `feat: verify phase with verified-once evidence`
- **Files**: create `edge_deploy/phases/verify.py`; modify `edge_deploy/phases/__init__.py` (registry line), `edge_deploy/cli.py` (move, not copy, logic), `tests/test_phases_verify.py`.
- **Anchors**: `cli.py::_run_release_preflight` — its `inspect_repository` +
  `require_successful_github_ci` + pytest steps. NOTE: `check_audit_remote`
  does NOT move here (it needs bitbucket; it stays where it is and PR-11
  relocates it).

1. Subcommand: `verify --run <run_id> [--reverify] [--force-lock]`.
2. Behavior: `enter_phase(VERIFY_SPEC, ...)`; if
   `ledger.phase_state("verify") == "passed"` and
   `ledger.state["source_sha"]` equals current `inspect_repository(...).commit`
   and not `--reverify`: print `verify: already passed for <sha7> (skipping)`,
   record `phase_skipped`, exit 0. Else run: `inspect_repository`,
   `require_successful_github_ci`, `pytest -n 8 --dist loadfile` (same
   subprocess call as today); on success
   `set_phase("verify","passed", evidence={"commit": sha, "ci": "success", "tests": "passed", "verified_at": iso})`,
   else `set_phase("verify","failed", ...)` and exit 1.
3. `_run_release_preflight` in cli.py: replace its CI+pytest section with a
   call into `phases.verify.ensure_verified(operator, profile, repo_root, ledger, reverify=False)`
   so `release` and `verify` share one implementation (keep
   `check_audit_remote` call in `_run_release_preflight` untouched).
- **Tests**: skip path (evidence honored, no pytest subprocess spawned — patch
  `subprocess.run`); `--reverify` forces the run; failure records
  `state=failed`.
- **Out of scope**: publish/deploy/tag phases; ruff gates.

## PR-11 — `publish` phase

- **Branch**: `codex/pr-11-publish-phase` — **Commit**: `feat: posture-gated publish phase`
- **Files**: create `edge_deploy/phases/publish.py`; registry line; `tests/test_phases_publish.py`; `edge_deploy/cli.py` (relocate audit gate).
- **Anchors**: `publish.py::publish_snapshot`, `release.py::_write_publish_report`, `audit.py::check_audit_remote`, `cli.py::_run_release_preflight`.

1. Subcommand: `publish-phase --run <run_id> [--no-local-check] [--force-lock]`
   (name avoids colliding with the existing standalone `publish`; the wrapper
   PR-15 calls this module's function, and PR-20 may rename).
2. Behavior: `enter_phase`; require `phases.verify.state == "passed"` else
   exit 2 with `publish refused: verify has not passed for run <run_id>`;
   run `check_audit_remote(...)` (moved here from `_run_release_preflight` —
   delete it there; it needs bitbucket posture which this phase guarantees);
   idempotency: if `phases.publish.state == "passed"` and
   `git ls-remote bitbucket refs/heads/<release_branch>` head equals recorded
   `snapshot_sha` → print `publish: already published <sha7> (skipping)`,
   exit 0; else call `publish_snapshot(...)`, write `publish-<tool>.json`
   into the run dir (existing helper), and
   `set_phase("publish","passed", evidence={"snapshot_sha": ..., "source_commit": ..., "previous_remote_commit": ...})`.
3. For `kind == "rollback"` runs the phase is a no-op validator: evidence
   already seeded (PR-05); verify state `pending` is acceptable — require
   instead that `rollback_tag` is set.
- **Tests**: refusal without verify; idempotent skip; evidence recorded;
  audit gate runs here and no longer in `_run_release_preflight`
  (assert by patching `check_audit_remote`).

## PR-12 — `deploy` phase

- **Branch**: `codex/pr-12-deploy-phase` — **Commit**: `feat: per-node resumable deploy phase`
- **Files**: create `edge_deploy/phases/deploy.py`; registry line; `tests/test_phases_deploy.py`; minimal edits in `edge_deploy/release.py`.
- **Anchors**: `release.py::run_release`, `ReleaseSelection`, `resolve_nodes`; PR-08 `pane_log_path`.

1. Subcommand: `deploy --run <run_id> [--nodes ...] [--smoke standard|deep] [--auth-mode prompt|pane] [--auth-wait-seconds N] [--force-lock]`.
2. Behavior: `enter_phase`; require `phases.publish.state == "passed"`;
   compute pending nodes = requested ∩ nodes whose ledger deploy state is not
   `passed` — if empty, print `deploy: all nodes already rolled out (skipping)`
   and exit 0; build `ReleaseSelection(tools=[tool], nodes=pending,
   snapshot_by_tool={tool: publish evidence snapshot_sha}, smoke=...)`; call
   `run_release(...)` with `report_dir = run_dir` and pass
   `pane_log_dir=run_dir` (add optional param to `run_release` that threads
   `pane_log_path=run_dir / f"pane-{node}.log"` into
   `TmuxDriver.from_node_and_profile` construction);
   after the report: for each rollout in `report.rollouts`, map status
   `rolled_out→passed`, `failed→failed`, `refused→failed`, `skipped→pending`
   into `set_phase("deploy", <mapped>, node=<node>, evidence=<compact rollout dict>)`.
3. Exit 0 only if every requested node maps to `passed`.
- **Tests**: reuse `conftest.FakeTmuxDriver` release-path fixtures; node
  already `passed` in ledger is not re-deployed (selection excludes it);
  ledger states written from report statuses; refusal without publish.
- **Out of scope**: auth changes (PR-18), runner conversion (PR-14/16).

## PR-13 — `tag github` / `tag bitbucket` phases

- **Branch**: `codex/pr-13-tag-phases` — **Commit**: `feat: native posture-scoped tag finalization phases`
- **Files**: create `edge_deploy/phases/tag.py`; registry lines (two specs, one module); `tests/test_phases_tag.py`; `edge_deploy/cli.py` (remove handoff-script emission from `_cmd_release` success path — the wrapper PR-15 rebuilds the success flow).
- **Anchors**: `cli.py::_tag_successful_release`, `_tag_push_handoff_lines` (the git command sequence to replicate natively), `_record_release_attempt`, `audit.py::append_audit_attempt`.

1. Subcommands: `tag-github --run <run_id>` and `tag-bitbucket --run <run_id>`
   (both accept `--force-lock`).
2. `tag-github`: `enter_phase(TAG_GITHUB_SPEC, ...)`; require all deploy nodes
   `passed`; tag name: reuse `ledger.state["phases"]["tag_github"]["evidence"].get("tag")`
   if present, else create via `_tag_successful_release` (move that function
   into `phases/tag.py`) and store it immediately in evidence (state stays
   `pending` until pushed); `git push origin refs/tags/<tag>`; verify via
   `git ls-remote --tags origin` dereferenced SHA == source_sha (reuse the
   logic from `_tag_push_handoff_lines` phase 1, as Python subprocess calls,
   not a script); `set_phase("tag_github","passed", evidence={"tag": tag, "pushed_sha": ...})`.
3. `tag-bitbucket`: same shape; replicate handoff phase 2 natively: if
   `snapshot_sha == source_sha` push the same tag, else create
   `edge-deploy-mirror/<tag>` at snapshot, push to `refs/tags/<tag>`, verify
   dereference == snapshot, delete the temp tag. Then append the audit record:
   move `_record_release_attempt` call here (release wrapper stops calling it
   at the end of deploy) and finally `ledger.complete()` when both tag phases
   are `passed`.
4. Delete `_write_tag_push_handoff` + `_tag_push_handoff_lines` and their
   tests only if the wrapper (PR-15) has NOT merged yet — otherwise leave for
   PR-15. (To keep this PR independent: keep the old functions; PR-20 sweeps.)
- **Tests**: fake `subprocess.run` command recorder — exact git argv
  sequences for the equal and mirror cases; refusal when a deploy node is not
  `passed`; idempotent re-run when already `passed` prints
  `tag-github: already pushed (skipping)`.

## PR-14 — dependency delivery via runner steps

- **Branch**: `codex/pr-14-deps-via-runner` — **Commit**: `refactor: dependency staging through runner step files`
- **Files**: `edge_deploy/dependencies.py`, `tests/test_dependencies.py`, `tests/conftest.py` (additive only).
- **Anchors**: `dependencies.py::deliver_dependency_bundle` (~400), `_stage_script` (~292), `_parse_stage_evidence` (~392); PR-09 `runner.bootstrap_runner`, `run_step`, `read_remote_json`.

1. `deliver_dependency_bundle` gains parameter `run_id: str` (thread it from
   `release.py`'s `bundle_for_tool` call site — it has the report dir; use the
   run dir name as run_id).
2. New flow inside `deliver_dependency_bundle`: `bootstrap_runner(driver, run_id)`;
   upload stage script + archive as today (`driver.upload_file`); execute the
   stage script via
   `run_step(driver, runner_path, run_id, "dependency-stage", f"python3 {stage_script_path} ...", timeout=...)`;
   the stage script is changed to WRITE its evidence JSON to
   `~/.edge-deploy/runs/<run_id>/steps/dependency-stage-evidence.json` instead
   of printing `DEPENDENCY_STAGE_START/END`; read it with
   `read_remote_json`.
3. Delete `_parse_stage_evidence` and the marker printing in `_stage_script`.
   `rg -n "DEPENDENCY_STAGE" edge_deploy tests` → only conftest fake support
   may remain, updated to serve step files instead.
4. `tests/conftest.py` (ADDITIVE): teach `FakeTmuxDriver` to (a) accept
   `runner_step_results: dict[str, dict]` and (b) when a command matches
   `sh *runner-*.sh <run_id> <step>` record it and, on the subsequent D8 read
   command for that step's json, return a canned screen built by a new helper
   `encode_step_result(payload: dict) -> str` that produces a REALISTICALLY
   WRAPPED screen (insert `\n` every 80 chars in the base64). Do not remove
   any existing fake behavior.
- **Tests**: delivery uses runner step + wrapped read; reuse path unchanged;
  evidence identical in shape to before (`remote_dir`, `reused`, digests).

## PR-15 — `release`/`rollback` wrappers chain phases

- **Branch**: `codex/pr-15-release-wrapper` — **Commit**: `refactor: release chains posture-scoped phases`
- **Files**: `edge_deploy/cli.py`, `tests/test_cli.py`.
- **Anchors**: `_cmd_release`, `_cmd_rollback`, phase modules from PR-10..13.

1. `_cmd_release` becomes: resolve/create run (PR-05 logic) → loop
   `[verify, publish, deploy, tag_github, tag_bitbucket]` in order:
   - already `passed` (deploy: all nodes passed) → continue;
   - posture probe for the phase fails → print the D7 message with
     `next command = python -m edge_deploy release --run <run_id>` and
     **exit 0** (state saved, not an error);
   - else invoke the phase's function; nonzero → exit with that code.
2. After `tag_bitbucket` passes, print `release complete: <run_id>`.
3. `_cmd_rollback`: create `kind="rollback"` run seeded per PR-05, then the
   same loop (verify auto-skips via seeding; publish validates).
4. Delete the now-dead direct `run_release` invocation path from
   `_cmd_release` (deploy phase owns it), `_write_tag_push_handoff`,
   `_tag_push_handoff_lines`, and the tag lines in the old success path.
   Keep `--nodes/--smoke/--auth-mode/--auth-wait-seconds/--fail-fast/
   --no-local-check` args and thread them into the respective phases.
- **Tests**: full chain with fakes (all postures reachable) completes and the
  ledger shows all phases passed; posture failure mid-chain exits 0 with the
  exact D7 message and leaves earlier phases `passed`; re-invocation resumes
  at the first non-passed phase (assert verify's pytest not re-spawned).

## PR-16 — rollout remote steps via runner

- **Branch**: `codex/pr-16-rollout-via-runner` — **Commit**: `refactor: rollout data steps through runner files`
- **Files**: `edge_deploy/rollout.py`, `tests/test_rollout.py`, `tests/conftest.py` (additive).
- **Anchors**: `rollout.py::run_rollout`; every `driver.run_remote(...)` call
  site in rollout.py whose screen output is parsed beyond the exit code
  (enumerate with `rg -n "run_remote" edge_deploy/rollout.py` — at planning
  time these are: the remote git preflight payload (`PERMISSION_PAYLOAD` /
  base64 blobs), the `git diff --name-only` changed-paths read, and the
  update/install/smoke output tails used in report evidence).
- **Conversion rule (apply mechanically)**: a `run_remote` call whose result
  screen is parsed for DATA becomes
  `run_step(driver, runner_path, run_id, "<step-name>", "<same shell command> > $HOME/.edge-deploy/runs/<run_id>/steps/<step-name>-data.txt 2>&1", ...)`
  … except that structured outputs are written by the command itself to a
  file and read via `read_remote_json` / a new `read_remote_text` (same D8
  protocol, skipping the JSON parse). A `run_remote` call used only for its
  exit code stays as-is.
1. `run_rollout` gains `run_id: str` param (threaded from deploy phase via
   `run_release`); bootstrap the runner once per node at rollout start.
2. Convert the data-bearing call sites per the rule. Delete the base64
   payload markers (`DRIFT_PAYLOAD`/`PERMISSION_PAYLOAD`) from rollout and
   from `drift.py` ONLY if drift shares the helper; otherwise leave drift
   untouched (record a TODO in PR-20's sweep list).
3. conftest: extend the PR-14 runner fake so rollout tests drive step results
   for preflight/diff/update/install/smoke; keep every existing test passing
   with updated fakes.
- **Tests**: wrapped-screen robustness test for the changed-paths read (the
  exact failure class of session 1's `JSONDecodeError`); a full
  `run_rollout` happy path over the fake runner; exit-code-only sites still
  use `run_remote` (assert command log).

## PR-17 — `status` command

- **Branch**: `codex/pr-17-status` — **Commit**: `feat: status shows run state and next posture-scoped command`
- **Files**: create `edge_deploy/phases/status.py`; registry line; `tests/test_phases_status.py`.
- **Anchors**: `RunLedger.find_open/load`; `posture.PHASE_ENDPOINTS`.

1. Subcommand: `status [--run <run_id>]` (default: all open runs, newest
   first; none → print `no open runs under <runs root>` and exit 0).
2. Per run print exactly this shape:
   ```
   run <run_id>  tool=<tool>  kind=<kind>  source=<sha7>  created=<created_at>
     verify:        passed
     publish:       passed (snapshot <sha7>)
     deploy:        node03=passed node04=failed
     tag_github:    pending
     tag_bitbucket: pending
   next: python -m edge_deploy deploy --run <run_id> --nodes node04   [posture: bitbucket+edge]
   ```
3. `next` resolver: first phase (order 10→50) not `passed`; deploy lists only
   non-passed nodes via `--nodes`; completed run → `next: none (complete)`.
   The posture suffix comes from `PHASE_ENDPOINTS`, `+`-joined.
- **Tests**: golden-string comparison for the block above from a synthetic
  ledger; complete and abandoned runs render correctly; `--run` filters.

## PR-18 — auth broker, delete `getpass`

- **Branch**: `codex/pr-18-auth-broker` — **Commit**: `refactor: single auth owner, no hidden prompts`
- **Files**: `edge_deploy/auth.py`, `edge_deploy/release.py`, `edge_deploy/cli.py` (arg choices), `tests/test_auth_seam.py`, `tests/test_release.py`.
- **Anchors**: `auth.py::authenticate_node` (getpass path), `authenticate_node_via_pane`; `release.py::_resolve_auth_mode` (~135), the auth block in `run_release` (~506-540); PR-04 `set_waiting`.

1. Add `class AuthBroker` in auth.py:
   `AuthBroker(tracker, auth_mode: str, wait_seconds: float, max_attempts: int)`
   with one method `ensure_authenticated(driver, node_name) -> None` that:
   reuses a live authenticated pane (`driver.session_exists()` +
   `driver.at_shell_prompt()`), else starts the session and drives EXACTLY the
   current `authenticate_node_via_pane` (mode `pane`) or the current
   prompt-forwarding path (mode `prompt`) — move those bodies in, do not
   rewrite their logic. Around any human wait it calls
   `tracker.set_waiting("operator")` / `tracker.set_waiting(None)`.
2. Delete: `_resolve_auth_mode`, mode `auto`, and every `getpass` import and
   call in the package. `rg -n "getpass" edge_deploy` → zero hits.
3. `--auth-mode` argparse choices become `("prompt", "pane")`, default
   `"prompt"`, on release/rollback/deploy parsers.
4. `run_release` replaces its inline auth block with
   `broker.ensure_authenticated(driver, node_name)`.
- **Tests**: no `getpass` importable path (grep-style test acceptable:
  assert `"getpass" not in Path(auth.__file__).read_text()` and same for
  release.py); auth requested exactly once per node per run (command log);
  `waiting_on` toggles around the prompt wait; default mode is prompt.

## PR-19 — runner-owned legacy installer shim

- **Branch**: `codex/pr-19-legacy-install-shim` — **Commit**: `refactor: runner owns install env compatibility`
- **Files**: `edge_deploy/runner.py` (RUNNER_SCRIPT + RUNNER_VERSION bump to "2"), `edge_deploy/rollout.py`, `tests/test_runner.py`, `tests/test_rollout.py`.
- **Anchors**: `rollout.py::build_install_command` (~154) and any
  `PIP_NO_INDEX` / `PIP_FIND_LINKS` / `EDGE_DEPLOY_BUNDLE_DIR` env injection
  found via `rg -n "PIP_NO_INDEX|PIP_FIND_LINKS|EDGE_DEPLOY_BUNDLE_DIR" edge_deploy`.

1. Add a dedicated runner step `install` to RUNNER_SCRIPT:
   `sh runner.sh <run_id> install <b64_command> <bundle_dir_or_dash>` — when
   the 4th arg is not `-`, export `EDGE_DEPLOY_BUNDLE_DIR=<dir>`,
   `PIP_NO_INDEX=1`, `PIP_FIND_LINKS=<dir>/wheels` before executing.
2. rollout.py: `build_install_command` stops emitting those three variables;
   the install call site passes `bundle_dir` (or `-`) as the runner arg.
3. Bump `RUNNER_VERSION = "2"`; bootstrap naturally re-uploads (path embeds
   version+digest).
- **Tests**: runner script text contains the guarded exports; install call
  site passes the bundle dir; no PIP_* injection remains in rollout.py.

## PR-20 — docs, ADRs, dead-code sweep

- **Branch**: `codex/pr-20-docs-and-sweep` — **Commit**: `docs: run-ledger release workflow; sweep superseded paths`
- **Files**: `docs/release-workflow.md`, `docs/DESIGN.md`, create `docs/adr/0008-run-ledger-and-posture-phases.md`, `docs/adr/0009-on-node-runner-file-evidence.md`, `README.md`, `plans/README.md` (status rows), deletions listed below.
- **Depends on**: every other PR merged.

1. Rewrite `docs/release-workflow.md` around: create run → `status` →
   per-posture phase commands → complete; include the exact refusal/posture
   message formats (D5/D7) so operators recognize them.
2. ADR-0008: run ledger, explicit resume, posture phases (context: the three
   failed sessions; decision; consequences). ADR-0009: runner + D8 protocol;
   mark the screen-scraping approach superseded; note ADR-0005/0006 sections
   this amends.
3. Sweep (each must be dead by now; verify with `rg` before deleting):
   old `--resume <dir>` docs mentions; `_write_tag_push_handoff` remnants;
   `DEPENDENCY_STAGE`/`DRIFT_PAYLOAD` markers left anywhere outside drift.py;
   `plans/001-*`, `plans/002-*` marked "absorbed by PR-16/PR-14" in their
   status rows (do not delete the files).
4. Full suite + ruff + a manual `python -m edge_deploy status` smoke in a
   scratch repo fixture.

---

## STOP conditions (all PRs)

- Your PR needs an edit in a file another in-flight PR owns (see the edge
  list) — stop, report the collision, do not "quickly fix" the other file.
- An anchor symbol is missing or moved — stop and report.
- Any test unrelated to your change breaks and the fix is not obvious from
  your diff — stop; do not delete or skip the test.
- You believe a message/schema in §0 is wrong — stop and propose; never ship
  a variant format.
