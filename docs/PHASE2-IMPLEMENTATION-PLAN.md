# edge-deploy-core — Phase 2 Implementation Plan

This is a file-level plan built directly on the Phase-1 APIs. Two findings change
the scope from the original DESIGN sketch:

- **ADR-0004 is already done.** Both `autobench/update.sh` and `robocop/update.sh`
  already resolve `EDGE_DEPLOY_REMOTE/BRANCH` with the old names as fallbacks, and
  `tests/test_scripts.py` already proves precedence. So Phase 2's only script work
  is the *deferred alias removal* (T9, note-only).
- **`run_rollout` already emits `rolled_out|failed|refused`** and computes
  `sensitive_changed`/`changed_paths`, but never `skipped` and never runs
  drift/smoke. `skipped` and the verify (drift+smoke) layer are the orchestrator's
  job.

Everything below adds the umbrella layer on top of the untouched Phase-1 engine.

---

## 0. New/changed files at a glance

```
edge_deploy/
  publish.py     # NEW  BB_TOKEN snapshot publish (gate + reparent), returns Snapshot SHA
  verify.py      # NEW  drift + per-tool smoke (standard/deep) over the pane
  release.py     # NEW  umbrella orchestrator: fan-out, getpass auth seam, consolidation
  auth.py        # NEW  (small) RSA/Kerberos getpass seam helpers (or fold into release.py)
  reporting.py   # EDIT add ReleaseReport + write_release_report (release/1 schema)
  cli.py         # EDIT add `release` (+ optional standalone `publish`) subcommands
tests/
  test_publish.py    # NEW (temp git repo + fake git runner)
  test_verify.py     # NEW
  test_release.py    # NEW (fan-out matrix, partial failure, fail-fast)
  test_auth_seam.py  # NEW (getpass forward, re-prompt, kinit-only-deep, redaction)
  conftest.py        # EDIT extend FakeTmuxDriver (auth seam + screen scripting)
  test_cli.py        # EDIT `release` parsing/dispatch
autobench/ , robocop/ # EDIT thin prod_tui shims delegating to edge_deploy (T7, deferred)
docs/DESIGN.md        # EDIT close §10 open items (report keys; tui_exit already wired)
```

A dedicated tiny `auth.py` is recommended so `TmuxDriver` stays transport-only and
the secret-handling seam is unit-testable in isolation; folding into `release.py`
is equivalent.

---

## 1. Module-by-module plan

### 1.1 `publish.py` — BB_TOKEN snapshot publish

**Responsibility:** Create exactly one Snapshot (a commit whose tree is the
reviewed source and whose parent is current `bitbucket/main`), push it via a
Bearer token, and return its SHA. The only step that talks to Bitbucket.

**Converging the two PS1s** — the references diverge on three axes; Phase 2 picks
one of each:

| Axis | autobench PS1 | robocop PS1 | Converged Phase-2 choice |
|---|---|---|---|
| Auth | `BB_TOKEN` Bearer (`-c http.extraHeader`) | ambient/interactive | **BB_TOKEN Bearer** |
| Gate | clean tree + remote-URL match only | clean tree + `local_check.ps1` reviewed-commit gate | **clean tree + on `release_branch` + `local_check.ps1` green** |
| Reparent | detach HEAD -> `reset --soft` -> commit -> restore | temp branch -> `reset --soft` -> commit -> restore | **`git commit-tree` (no working-tree mutation)** |
| Message | `Deploy snapshot: autobench <ref> <short> (<ts>)` | `Deploy snapshot: Dispatch from robocop <sha> (<isoTs>)` | `Deploy snapshot: <tool> <source-short> on <branch> (<YYYY-MM-DD HH:mm>) [edge-deploy]` |

**Recommendation 1 — reimplement in Python, do not shell out to the PS1s.** The
orchestrator already runs Python and needs the Snapshot SHA back *as a value*; the
PS1s diverge and only print it. A Python reimplementation gives one converged,
cross-platform, unit-testable path with a `subprocess` git seam.

**Recommendation 2 — reparent with `git commit-tree`, not detach/soft-reset/restore.**
`commit-tree` builds the snapshot commit object directly from the source tree and
the remote parent without touching the working tree, HEAD, or the current branch —
no detached-HEAD risk and no `finally`-restore (both PS1s carry a fragile restore
block). It is fully reentrant and safe to run while other work is in flight:

```python
tree = git("rev-parse", f"{source}^{{tree}}")
parent = git("rev-parse", f"{remote}/{branch}")        # after authed fetch
snap = git("commit-tree", tree, "-p", parent, "-m", message)  # author/committer from git config
git("-c", f"http.extraHeader=Authorization: Bearer {token}", "push", remote, f"{snap}:refs/heads/{branch}")
```

The detach + `reset --soft` path stays documented as the PS1-faithful fallback if a
hook must see a checked-out tree.

**Recommendation 3 — invoke each repo's committed `local_check.ps1` via PowerShell;
don't reimplement the checks in Python.** The checks are deliberately tool-specific
(autobench: compileall+ruff+mypy+gate+pytest; robocop: compileall+pytest+help-smoke)
and already maintained in-repo. The engine only needs the gate's **exit code**.
Resolve `pwsh` (cross-platform) then `powershell` (Windows):

```bash
pwsh -NoProfile -ExecutionPolicy Bypass -File tools/dev/local_check.ps1
```

If neither shell exists, fail the gate with an actionable message rather than
silently skipping it.

**Key signatures:**

```python
class PublishError(RuntimeError): ...

@dataclass(frozen=True)
class PublishResult:
    tool: str
    status: str                 # "published"
    snapshot: str               # new Snapshot SHA (now HEAD of bitbucket/main)
    source_commit: str
    source_short: str
    branch: str
    previous_remote_commit: str
    message: str
    gate: dict[str, bool]       # {"clean_tree":..., "on_release_branch":..., "local_check":...}

GitRunner = Callable[[Sequence[str]], str]  # raises PublishError on nonzero; subprocess seam

def build_snapshot_message(tool: str, source_short: str, branch: str, when: datetime) -> str:
    return (f"Deploy snapshot: {tool} {source_short} on {branch} "
            f"({when:%Y-%m-%d %H:%M}) [edge-deploy]")

def publish_snapshot(
    profile: ToolProfile,
    *,
    repo_root: str | Path,                 # OperatorConfig.tool_path(tool)
    remote: str = "bitbucket",
    commit: str | None = None,             # --commit override (choose source)
    token_env: str = "BB_TOKEN",
    run_local_check: bool = True,
    clock: Callable[[], datetime] = _utc_now,        # injectable -> deterministic message tests
    git_runner: GitRunner | None = None,             # injectable -> no real git in tests
    local_check_runner: Callable[[Path], int] = run_local_check_ps1,
) -> PublishResult: ...

def run_local_check_ps1(repo_root: Path) -> int: ...   # pwsh|powershell resolver
def reparent_snapshot(git: GitRunner, *, source: str, remote: str,
                      branch: str, message: str, token: str) -> str: ...   # commit-tree
```

**Gate semantics:**
- **Default** (`commit=None`): require working tree clean **and** `HEAD` on
  `profile.release_branch` **and** `local_check.ps1` exit 0. Source = `HEAD`.
- **`--commit <sha>`**: source = `<sha>`; this *relaxes* the on-branch/clean-tree
  requirement (operator is explicitly naming a reviewed commit) but **still runs
  `local_check`** unless `--no-local-check`. `local_check` runs against the working
  tree, so for a `--commit` that differs from HEAD the check validates the *current
  checkout*, not the named commit (flagged in Risks).

> **Keep distinct from release's `--snapshot`:** `publish --commit <sha>` *creates a
> new Snapshot from source `<sha>`*; `release --snapshot <sha>` *skips Publish
> entirely* and rolls out an existing Snapshot. Same-looking SHAs, opposite meanings.

**Token redaction:** the Bearer token is passed as a `-c http.extraHeader=` arg. The
`git_runner` must never echo argv into reports; `redact()` already masks `token=`.

### 1.2 `verify.py` — drift + per-tool smoke

**Responsibility:** the DESIGN §6 VERIFY step beyond what `run_rollout` already does
(it already does `permissions`). Adds Drift==0 over `runtime_paths` (reuse
`check_drift`) and the profile's smoke commands.

```python
def run_smoke(driver: TmuxDriver, profile: ToolProfile, *, level: str) -> list[ReportCheck]:
    cmds = profile.smoke.deep if level == "deep" else profile.smoke.standard
    checks = []
    for cmd in cmds:
        screen, code = driver.run_remote(f"cd {profile.repo_path} && {cmd}", timeout=120)
        checks.append(ReportCheck(f"smoke:{cmd}", code == 0, f"exit {code}"))
    return checks

def verify_after_rollout(
    driver: TmuxDriver, profile: ToolProfile, node, *,
    commit: str, local_root: str | Path, smoke_level: str,
) -> list[ReportCheck]:
    drift_report = check_drift(driver, profile, node, commit=commit, local_root=local_root)
    checks = list(drift_report.checks)        # "runtime_drift"
    checks.extend(run_smoke(driver, profile, level=smoke_level))
    return checks
```

The orchestrator **merges these checks into the pair's rollout `OperationReport`** so
there is one detailed file (and one pointer) per (tool x node). `autobench`'s
`smoke.deep` is `[]`, so deep is a no-op there and needs no Kerberos — the kinit
seam is only paid when a selected tool actually has deep commands.

### 1.3 `auth.py` — the getpass seam (ADR-0002)

**Responsibility:** turn `TmuxDriver.start_session() -> False` (the documented RSA
seam) into an authenticated pane, and handle Kerberos only for deep smoke. Holds
secrets transiently; never routes them through `run_remote` (logged) — always via
`send_keys(..., literal=True)`.

**Recommendation:** add one tiny public method `TmuxDriver.submit_secret(secret)`
(`send_keys(secret, literal=True); send_key("Enter")`) so `auth.py` never calls
private helpers, and promote `await_authenticated()` as a public alias of
`_await_auth_result`. Then:

```python
def authenticate_node(
    driver: TmuxDriver, label: str, *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    max_attempts: int = 3, connect_timeout: float | None = None,
) -> None:
    if driver.start_session(connect_timeout=connect_timeout):   # passcode=None -> False at prompt
        return                                                   # already authenticated (rare)
    for attempt in range(1, max_attempts + 1):
        code = getpass_fn(f"[{label}] Enter RSA PASSCODE: ")     # transient, never stored
        driver.submit_secret(code)
        try:
            driver.await_authenticated(timeout=connect_timeout or driver.ssh_connect_timeout)
            return
        except AuthenticationError:
            if attempt == max_attempts:
                raise
            # sshd re-displayed PASSCODE: loop re-prompts for a *fresh* single-use code

def ensure_kerberos(
    driver: TmuxDriver, label: str, *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    principal: str | None = None, max_attempts: int = 2,
) -> ReportCheck:
    _, code = driver.run_remote("klist -s")        # 0 => valid ticket already
    if code == 0:
        return ReportCheck("kerberos", True, "Existing Kerberos ticket")
    for attempt in range(1, max_attempts + 1):
        driver.send_text(f"kinit {principal}".strip())
        driver.wait_for(r"[Pp]assword.*:", timeout=15)
        driver.submit_secret(getpass_fn(f"[{label}] Kerberos password: "))
        _, code = driver.run_remote("klist -s")
        if code == 0:
            return ReportCheck("kerberos", True, "Kerberos ticket acquired")
    return ReportCheck("kerberos", False, "Could not acquire a Kerberos ticket")
```

**Control flow (serial per node):**
1. One `TmuxDriver` per **node** (not per tool), reused across tools — RSA paid once per node.
2. `authenticate_node` runs the RSA getpass seam, re-prompting on stale/rejected codes.
3. `ensure_kerberos` runs **only when `--smoke deep` and at least one selected tool has `smoke.deep`**.
4. Secrets live only in locals; redaction (`passcode=|password=|token=`) is enforced by `write_report`/`redact`.

### 1.4 `release.py` — umbrella orchestrator

**Responsibility:** fan-out over Tools x Edge Nodes, one Authenticated Pane per node
reused across tools, partial-failure policy (ADR-0003), consolidation.

**Pane-reuse nuance:** the per-node pane is shared across tools but the engine `cd`s
explicitly in every `run_remote`, so the landing dir is cosmetic. **Decision:** build
the per-node driver from the node + the *first selected tool's* profile (for
chrome/`tui_exit`), and rely on each engine call's explicit `cd`. Document that
chrome/`tui_exit` is taken from the first tool; fine because `return_to_shell` is
only used between commands and both tools' chrome regexes are matched defensively.

```python
TOOLS_BOTH = ("autobench", "robocop")

@dataclass(frozen=True)
class ReleaseSelection:
    tools: list[str]        # resolved from --tool {autobench|robocop|both}
    nodes: list[str]        # resolved from --nodes (default: all configured)
    snapshot: str | None    # --snapshot <sha> -> skip Publish
    smoke: str              # "standard" | "deep"
    fail_fast: bool

def run_release(
    operator: OperatorConfig,
    selection: ReleaseSelection,
    *,
    report_dir: Path,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    publish_fn: Callable[..., PublishResult] = publish_snapshot,
    driver_factory: Callable[..., TmuxDriver] = TmuxDriver.from_node_and_profile,
    clock: Callable[[], datetime] = _utc_now,
) -> ReleaseReport: ...
```

### 1.5 `reporting.py` additions — the `release/1` consolidated report

Reuse the unchanged `OperationReport`/`ReportCheck` for detailed per-pair files
(they already carry `sensitive_changed`, `changed_paths`, `refused_paths`). Add:

```python
RELEASE_SCHEMA = "edge-deploy/release/1"

@dataclass
class ReleaseReport:
    selection: dict[str, Any]
    publishes: list[dict[str, Any]]    # compact PublishResult dicts
    rollouts: list[dict[str, Any]]     # compact per-pair summaries + report_path pointers
    operator_email: str = ""
    timestamp: str = field(default_factory=utc_iso_timestamp)

    def summary(self) -> dict[str, Any]: ...    # counts + handoffs[] + overall
    def exit_code(self) -> int: ...             # 1 if any failed/refused/publish_failed else 0
    def to_payload(self) -> dict[str, Any]: ... # {"schema": RELEASE_SCHEMA, ...}

def write_release_report(path: str | Path, report: ReleaseReport) -> Path:
    # same redaction path as write_report (ADR-0002)
```

### 1.6 `cli.py` additions + tool-repo wiring (T7, deferred)

Add a `release` (and optionally standalone `publish`) subparser, reusing
`OperatorConfig.load` + `load_tool_profile` + the existing print helpers.

**Wiring each tool repo (the deferred "core_only -> wire" step):**
- **Canonical entry stays `py -m edge_deploy`** (ADR-0001). Nothing tool-specific vendored.
- **Stays in each repo:** the committed `edge_deploy.yaml`, `update.sh`/`install.sh`,
  `tools/dev/local_check.ps1`, and product-specific QA that is *not* deploy
  (robocop's `controlled_job.py`/`levels.py`/`smoke_test.py` Impala scenarios remain
  robocop's — out of scope per DESIGN §8).
- **Package owns:** publish/rollout/drift/verify/release engine.
- **The wire:** replace the bodies of `robocop/tools/prod_tui/deploy.py` and `drift.py`
  (and `autobench/tools/prod_tui/harness.py`'s deploy/drift paths) with thin shims
  that `import edge_deploy`, load `OperatorConfig` + the repo's `ToolProfile`, and call
  `run_rollout`/`check_drift` — preserving existing muscle-memory while deleting
  duplicated engine logic.

---

## 2. CLI surface & argument handling

```
py -m edge_deploy release
    --tool {autobench|robocop|both}     (required)
    --nodes 03,04                       (default: all nodes in operator config; accepts 03 / node03)
    --snapshot <sha>                    (optional; SKIP Publish, roll out existing Snapshot)
    --smoke {standard|deep}             (default: standard)
    --fail-fast                         (default: off -> continue, ADR-0003)
    --report-dir <path>                 (default: ./edge-deploy/reports/release-<UTC>/)
    --max-auth-attempts N               (default: 3)
    --config <operator-config>          (inherited top-level arg)

# Optional standalone (handy for resume / manual publish):
py -m edge_deploy publish
    --tool {autobench|robocop}          (required; no "both" — publish is per-tool)
    --commit <sha>                      (optional source override)
    --no-local-check                    (escape hatch; default runs the gate)
    --remote bitbucket
```

**Details:**
- `--tool both` -> `["autobench","robocop"]`; validate each against `operator.tools`.
- `--nodes` parsing: split on comma, normalize `03`->`node03` (accept full `node03`),
  look up via `operator.node(name)`.
- `--smoke` is a 2-value `choices`; map to `SmokeCommands.standard|deep`.
- `--snapshot` present => Publish skipped for **all** tools and the snapshot SHA is
  reused for every selected tool (DESIGN §7 resume).
- Exit code = `ReleaseReport.exit_code()` (non-zero if any pair failed/refused or any
  publish failed).
- `main()` keeps the existing exception envelope (exit 2) and adds `PublishError`.

---

## 3. getpass auth seam — control flow & testing

### 3.1 Runtime control flow (per node, serial)

```
for node in selection.nodes:                # SERIAL — RSA is single-use ~60s
    driver = driver_factory(node, first_tool_profile)
    try:
        authenticate_node(driver, node.name, getpass_fn=getpass_fn)   # RSA seam
    except AuthenticationError as e:
        record every (tool x node) as status="failed", state_left="auth: <e>"
        if fail_fast: break else: continue   # never block other nodes
    kerb = ReportCheck("kerberos", True, "n/a")
    if selection.smoke == "deep" and any(profile.smoke.deep for tool in tools):
        kerb = ensure_kerberos(driver, node.name, getpass_fn=getpass_fn)
    for tool in selection.tools:
        ... rollout + verify (see §4.1), attaching `kerb` when deep ...
    if not reuse: driver.stop_session()
```

### 3.2 Testing without real secrets

Extend `FakeTmuxDriver` (currently `start_session` just returns `True`) additively:

```python
class FakeTmuxDriver:
    def __init__(self, *, auth_script: list[str] | None = None,   # e.g. ["reject","accept"]
                 klist_code: int = 0, ...):
        self.sent_secrets: list[str] = []      # what submit_secret captured
        self.sent_keys: list[str] = []
        self._auth_script = list(auth_script or ["accept"])
        self._klist_code = klist_code

    def start_session(self, **kw) -> bool:
        return self._auth_script[0] == "preauthed"     # else False -> seam engaged

    def submit_secret(self, secret: str) -> None:
        self.sent_secrets.append(secret)

    def await_authenticated(self, *, timeout=None) -> None:
        outcome = self._auth_script.pop(0)
        if outcome == "reject":
            raise AuthenticationError("stale code")     # forces a re-prompt
    # run_remote already routes by content -> add "klist -s" -> self._klist_code
```

Tests (`test_auth_seam.py`): forward+success; re-prompt (`["reject","accept"]`);
exhaustion (3x reject -> `AuthenticationError`); kinit-only-for-deep; redaction.

---

## 4. Fan-out + partial-failure + consolidated report

### 4.1 Per-pair outcome logic (ADR-0003)

| Situation | pair `status` | `state_left` | detailed report |
|---|---|---|---|
| Publish for tool failed | `skipped` | `"publish failed; rollout not attempted"` | synthetic report |
| Node auth failed | `failed` | `"auth: <reason>"` | synthetic report |
| `run_rollout` -> `refused` (ADR-0005) | `refused` | `"not started; dependency change refused"` | engine refusal report |
| `run_rollout` -> `failed` | `failed` | e.g. `"update.sh exit N; node at <final>, expected <target>"` | engine report |
| `rolled_out`, then drift/smoke fail | `failed` | `"rolled out but drift/smoke failed"` | engine report + merged verify checks |
| `rolled_out` + verify clean | `rolled_out` | `""` | engine report + verify checks |

Orchestrator: call `run_rollout`; if `rolled_out`, call `verify_after_rollout` and
append its checks (re-derive status if a verify check fails); write the detailed file
with `write_report`; record the compact summary.

**`fail_fast`:** stop scheduling further pairs/nodes on the first non-success;
already-touched nodes are reported with whatever they reached.

**Loop order:** Publish per tool first (a broken build never authenticates a node),
then **nodes outer / tools inner** (one pane per node, reused).

### 4.2 Consolidated `edge-deploy/release/1` JSON

```json
{
  "schema": "edge-deploy/release/1",
  "timestamp": "2026-06-29T23:00:00Z",
  "operator_email": "e176097@mastercard.com",
  "selection": {
    "tools": ["autobench", "robocop"],
    "nodes": ["node03", "node04"],
    "smoke": "standard",
    "fail_fast": false,
    "snapshot_override": null
  },
  "publishes": [
    {
      "tool": "autobench", "status": "published",
      "snapshot": "a1b2c3d4...", "source_short": "a1b2c3d", "branch": "main",
      "previous_remote_commit": "0f0f0f0...",
      "message": "Deploy snapshot: autobench a1b2c3d on main (2026-06-29 23:00) [edge-deploy]",
      "report_path": "edge-deploy/reports/release-20260629T230000Z/publish-autobench.json"
    },
    { "tool": "robocop", "status": "failed", "snapshot": null,
      "error": "local_check.ps1 failed with exit code 1" }
  ],
  "rollouts": [
    {
      "tool": "autobench", "node": "node03", "status": "rolled_out", "state_left": "",
      "deployment_commit": "a1b2c3d4...", "previous_remote_commit": "9999...",
      "sensitive_changed": [], "drift": "passed", "smoke": "passed",
      "report_path": "edge-deploy/reports/release-20260629T230000Z/rollout-autobench-node03.json"
    },
    {
      "tool": "autobench", "node": "node04", "status": "failed",
      "state_left": "rolled out but drift detected",
      "deployment_commit": "a1b2c3d4...", "sensitive_changed": [],
      "drift": "failed", "smoke": "not_run", "report_path": ".../rollout-autobench-node04.json"
    },
    {
      "tool": "robocop", "node": "node03", "status": "skipped",
      "state_left": "publish failed; rollout not attempted",
      "drift": "not_run", "smoke": "not_run", "report_path": null
    }
  ],
  "summary": {
    "counts": {
      "rolled_out": 1, "failed": 1, "skipped": 2, "refused": 0,
      "published": 1, "publish_failed": 1
    },
    "handoffs": [
      { "kind": "publish", "tool": "robocop", "node": null,
        "message": "local_check.ps1 failed (exit 1)",
        "action": "fix local check, re-run: release --tool robocop" },
      { "kind": "mid_state", "tool": "autobench", "node": "node04",
        "message": "rolled out but drift detected on runtime files",
        "action": "investigate node04; re-run: release --tool autobench --nodes 04 --snapshot a1b2c3d4" }
    ],
    "overall": "failed"
  },
  "exit_code": 1
}
```

Embeds compact per-rollout summaries and references the detailed per-(tool x node)
`OperationReport` files via `report_path`. `summary.counts` covers ADR-0003's four
pair statuses plus publish tallies; `summary.handoffs[]` enumerates operator
follow-ups with a ready-to-paste resume command; `summary.overall` + top-level
`exit_code` drive the non-zero exit. The whole payload passes through the same
redaction path as `write_report`.

---

## 5. Test strategy (extending the FakeTmuxDriver suite)

Principles kept from Phase 1: route fake responses by command content; parametrize
across **both real profiles**; no tmux/SSH/network.

1. **`conftest.py`** — extend `FakeTmuxDriver` with the auth-seam surface (§3.2),
   `submit_secret`, `await_authenticated`, `send_keys`/`send_key` capture, `klist -s`
   code, `kinit`/`Password:` handling. Keep all current defaults so existing tests pass.
2. **`test_publish.py`** — real temporary git repo (init + local bare remote as
   `bitbucket/main`) for the happy path, plus a fake `git_runner` for unit cases:
   message format exactness (inject `clock`); `commit-tree` parent/tree correctness;
   gate failures (dirty tree, off release_branch, local_check exit 1); Bearer header in
   push argv; token never in `PublishResult` or any report.
3. **`test_verify.py`** — `run_smoke` one `ReportCheck` per command; `verify_after_rollout`
   merges `runtime_drift` + smoke; parametrized over both profiles.
4. **`test_auth_seam.py`** — forward/re-prompt/exhaustion/kinit-only-deep/redaction.
5. **`test_release.py`** — fan-out integration with injected `publish_fn`/`getpass_fn`/
   `driver_factory`: ordering (publish before auth, one auth per node, tools inside
   nodes); partial failure; `--fail-fast`; refused; report-schema keys; `--snapshot`.
6. **`test_cli.py`** — `release` parsing + dispatch into a monkeypatched `run_release`;
   keep the `python -m edge_deploy --help` subprocess smoke; add `release` to help text.
7. **Drift-source guard** — `release --snapshot <sha>` where `<sha>` isn't a local
   object -> clear error / handoff (see Risks).

---

## 6. Task breakdown, dependencies & parallelism

| # | Task | Depends on | Parallel with |
|---|---|---|---|
| **T0** | Extend `FakeTmuxDriver` (auth seam, key/secret capture, klist/kinit) | — | T1, T4 |
| **T1** | `publish.py` (gate + `commit-tree` reparent + `local_check.ps1` runner) + `test_publish.py` | config | T0, T2, T3, T4 |
| **T2** | `verify.py` (smoke + drift merge) + `test_verify.py` | config, drift, reporting | T1, T3, T4 |
| **T3** | `auth.py` seam + `TmuxDriver.submit_secret`/`await_authenticated` + `test_auth_seam.py` | tmux_driver, **T0** | T1, T2, T4 |
| **T4** | `reporting.py` `ReleaseReport` + `write_release_report` + schema tests | reporting | T1, T2, T3 |
| **T5** | `release.py` orchestrator (fan-out + partial failure) + `test_release.py` | **T1, T2, T3, T4**, rollout, drift | — (integration) |
| **T6** | `cli.py` `release` (+ `publish`) subcommands + `test_cli.py` | **T5** | T7 docs |
| **T7** | Tool-repo wiring shims (robocop `deploy.py`/`drift.py`, autobench `harness.py`) | **T6** | per-tool parallel; T8 |
| **T8** | DESIGN §10 closeout (report keys), README CLI section | T4, T6 | T7 |
| **T9** | *(later, not now)* ADR-0004 alias removal + `test_scripts.py` update + tool deploy docs | one green Release on both nodes x both tools | — |

**Parallelizable now:** T0–T4 are independent module builds (T0 the shared first
step). T5 is the integration join point and must wait for T1–T4. T7 splits per tool.
**Critical path:** T0 -> T3 -> T5 -> T6 -> T7.

---

## 7. Risks & open questions

1. **`--snapshot <sha>` needs the Snapshot's tree locally for Drift.** `check_drift`
   -> `local_runtime_map` does `git show <commit>:<path>` against the operator working
   copy. A resume against a remote-only SHA fails `rev-parse`. **Mitigation:** in
   `run_release`, when `--snapshot` is given, `git fetch` (and/or `git cat-file -e <sha>`)
   in each tool's working copy before verify, or document that the SHA must be fetched
   first. (T5)
2. **`publish --commit <sha>` vs `local_check` target.** `local_check.ps1` validates the
   *current checkout*, not an arbitrary `--commit`. Options: (a) require `--commit` == HEAD
   unless `--no-local-check`; (b) checkout `<sha>` first (reintroduces the restore dance).
   Recommend (a) + clear messaging.
3. **BB_TOKEN in git argv.** `-c http.extraHeader=Authorization: Bearer <token>` puts a
   reusable token on the child argv (visible to local `ps`). Consider
   `http.<url>.extraHeader` via `GIT_CONFIG_COUNT/KEY/VALUE` env to keep it out of argv —
   worth a small spike. (Open)
4. **getpass requires a TTY.** Under a future SDK/CI path getpass degrades or fails.
   ADR-0002 names a pane-direct fallback flag as acceptable-later; for now document that
   `release` is interactive-operator-only. (Open)
5. **Kerberos `kinit` principal/realm.** `ensure_kerberos` uses the SSH user's default
   principal (bare `kinit`). If a tool needs an explicit principal, where does it live —
   operator config or Tool Profile? Confirm bare `kinit` suffices on the Edge Nodes. (Open)
6. **`local_check.ps1` cross-platform.** Detect missing `pwsh`/`powershell` and fail the
   gate loudly rather than skip it (a silent skip would let an unverified build publish).
7. **Per-node pane landing dir under tool reuse.** Decided: build the per-node driver from
   the first selected tool's profile and rely on explicit `cd` in every engine call. Add a
   test asserting both tools' commands carry their own `cd <repo_path>`.
8. **Merging verify checks into the rollout report vs separate files.** Recommended merge
   (one pointer per pair). If reviewers prefer separate `drift-*`/`smoke-*` files,
   `report_path` becomes a list — decide before T4 freezes the schema.
9. **`run_rollout` has no `skipped` path.** `ROLLOUT_STATUSES` includes `skipped` but the
   engine never returns it; the orchestrator must synthesize `skipped` (publish-failed /
   node-excluded). Ensure consolidated counts treat synthetic `skipped` consistently.
10. **ADR-0004 alias removal (T9) is not in this phase.** The `EDGE_DEPLOY_*` interface +
    aliases already ship and are tested. Removal is gated on one green Release via the new
    interface across both tools and both nodes, then drop the alias branches and update
    each tool's deploy docs.
