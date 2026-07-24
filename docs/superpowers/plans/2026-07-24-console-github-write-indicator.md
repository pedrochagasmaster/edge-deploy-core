# Console GitHub Write Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Edge Console GitHub capability light mean non-mutating write access for every watched tool, not TCP reachability or GitHub read.

**Architecture:** Keep `edge_console.py` standalone and read-only for ledgers. Add a per-checkout GitHub write probe that runs the exact argv from `edge_deploy.posture.git_probe_command("origin", "write")` with credential prompts disabled, expose per-tool `ok`/`fail`/`unknown` plus an aggregate status on `/api/posture`, and leave divergence `ls-remote` probes untouched.

**Tech Stack:** Python 3.10+, stdlib `subprocess`/`concurrent.futures`, existing `edge_console.py` HTTP UI, pytest, Ruff. Top-level `from edge_deploy.posture import git_probe_command` (no fallback argv duplication).

## Global Constraints

- Console stays outside the `edge_deploy` package (Engine Identity hashes package `*.py` only).
- No ledger writes; no remote ref updates; the only GitHub write-path call is `git push --dry-run`.
- Existing divergence read probes (`rev-parse`, `rev-list`, `ls-remote origin refs/heads/main`) stay unchanged.
- Write probe argv must match `git_probe_command("origin", "write")` from `edge_deploy/posture.py`.
- Disable interactive credential prompts via `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`.
- Aggregate GitHub indicator is green only when every watched tool probe returns `ok`.
- Aggregate is red when at least one watched tool returns definitive `fail`.
- Aggregate is `unknown` when any required probe cannot run, times out, or lacks a valid checkout and no definitive failure exists (including zero watched roots).
- Bitbucket and Edge TCP probes remain capability-specific TCP indicators.
- Canonical internal tool id remains `robocop`; display may say Dispatch later — this plan does not rename tools.
- All Python imports remain at module scope; do not add inline imports.
- Test and lint on this Linux Cloud VM with `.venv/bin/python` and `.venv/bin/ruff`.
- Do not edit `docs/superpowers/specs/2026-07-24-release-operator-onboarding-design.md`.

---

## File Map

**Modify**

- `edge_console.py`: write-probe helpers, `PostureProber` GitHub group, demo snapshot, UI copy/JS for write semantics.
- `tests/test_edge_console.py`: write-probe, aggregation, API/demo guards; keep existing divergence tests green.

**Reference only (do not change for this plan)**

- `edge_deploy/posture.py`: `git_probe_command`, `_PROBE_REF`, `_GIT_PROBE_TIMEOUT`, prompt-disabling env.
- `docs/adr/0012-posture-aware-release-flow.md`, `docs/adr/0013-five-posture-capability-model.md`.

---

### Task 1: Per-tool GitHub write probe and aggregation

**Files:**
- Modify: `edge_console.py`
- Test: `tests/test_edge_console.py`

**Interfaces:**
- Consumes: top-level `from edge_deploy.posture import git_probe_command` in `edge_console.py` (no try/except ImportError, no duplicated argv constant).
- Produces:
  - `GITHUB_WRITE_STATUSES = frozenset({"ok", "fail", "unknown"})`
  - `github_write_command() -> list[str]`
  - `probe_github_write(root: Path, *, runner: Callable[[list[str], Path], int] | None = None, timeout: float = 20.0) -> dict`
  - `aggregate_github_write(tool_results: list[dict]) -> str`  # `"ok" | "fail" | "unknown"`

- [ ] **Step 1: Write the failing tests**

In `tests/test_edge_console.py`, extend the existing **module-top** import block (do not add imports inside functions or below helpers):

```python
from edge_console import (  # noqa: E402
    PAGE,
    ToolsProber,
    _SCHEMA,
    _tool_name,
    aggregate_github_write,
    build_demo_checkouts,
    collect_runs,
    collect_runs_multi,
    github_write_command,
    probe_divergence,
    probe_github_write,
)
from edge_deploy.posture import git_probe_command  # noqa: E402
```

Then append these tests (no additional imports):

```python
def test_github_write_command_matches_posture_write_argv() -> None:
    assert github_write_command() == git_probe_command("origin", "write")
    assert github_write_command() == [
        "git",
        "push",
        "--dry-run",
        "--force",
        "origin",
        "HEAD:refs/edge-deploy/posture-probe",
    ]


def test_probe_github_write_ok_fail_unknown(tmp_path) -> None:
    root = tmp_path / "autobench"
    root.mkdir()
    (root / ".git").mkdir()

    assert probe_github_write(root, runner=lambda cmd, cwd: 0)["status"] == "ok"
    assert probe_github_write(root, runner=lambda cmd, cwd: 128)["status"] == "fail"

    missing = tmp_path / "missing"
    assert probe_github_write(missing, runner=lambda cmd, cwd: 0)["status"] == "unknown"


def test_probe_github_write_timeout_is_unknown(tmp_path) -> None:
    root = tmp_path / "robocop"
    root.mkdir()
    (root / ".git").mkdir()

    def timed_out(command: list[str], cwd) -> int:
        del command, cwd
        return -1

    result = probe_github_write(root, runner=timed_out)
    assert result["status"] == "unknown"
    assert result["tool"] == "robocop"


def test_probe_github_write_disables_prompts_and_uses_write_argv(tmp_path, monkeypatch) -> None:
    root = tmp_path / "autobench"
    root.mkdir()
    (root / ".git").mkdir()
    seen: dict = {}

    def fake_run(command, cwd=None, capture_output=None, timeout=None, env=None, **kwargs):
        seen["command"] = list(command)
        seen["cwd"] = Path(cwd)
        seen["env"] = dict(env)
        seen["timeout"] = timeout

        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr("edge_console.subprocess.run", fake_run)
    result = probe_github_write(root, runner=None)
    assert result["status"] == "ok"
    assert seen["command"] == github_write_command()
    assert seen["cwd"] == root
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GCM_INTERACTIVE"] == "never"
    assert seen["timeout"] == 20.0


def test_aggregate_github_write_rules() -> None:
    assert aggregate_github_write([]) == "unknown"
    assert aggregate_github_write([{"status": "ok"}, {"status": "ok"}]) == "ok"
    assert aggregate_github_write([{"status": "ok"}, {"status": "fail"}]) == "fail"
    assert aggregate_github_write([{"status": "ok"}, {"status": "unknown"}]) == "unknown"
    assert aggregate_github_write([{"status": "fail"}, {"status": "unknown"}]) == "fail"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py::test_github_write_command_matches_posture_write_argv tests/test_edge_console.py::test_probe_github_write_ok_fail_unknown tests/test_edge_console.py::test_probe_github_write_timeout_is_unknown tests/test_edge_console.py::test_probe_github_write_disables_prompts_and_uses_write_argv tests/test_edge_console.py::test_aggregate_github_write_rules -v`

Expected: FAIL with `ImportError` / `cannot import name 'probe_github_write'` (or equivalent missing symbol).

- [ ] **Step 3: Implement probe helpers in `edge_console.py`**

At the **module top** of `edge_console.py` (with the other imports, not inside a function), add:

```python
from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, load_operator_config
from edge_deploy.posture import git_probe_command
from edge_deploy.preflight import endpoint_from_node
```

Use those top-level names inside `_edge_endpoints` (remove its existing inline imports):

```python
def _edge_endpoints() -> list[tuple[str, int]]:
    try:
        operator = load_operator_config(DEFAULT_OPERATOR_CONFIG_PATH)
        endpoints = []
        for name in sorted(operator.nodes):
            resolved = endpoint_from_node(operator.nodes[name])
            endpoints.append((resolved.hostname, resolved.port))
        return endpoints
    except Exception:
        return []
```

Do not keep a local probe-ref / duplicated argv list and do not wrap `git_probe_command` in `try`/`except`.

Add near the TCP posture section (after `_PROBE_CACHE_SECONDS`, before `_edge_endpoints`):

```python
_GITHUB_WRITE_TIMEOUT = 20.0


def github_write_command() -> list[str]:
    """Exact write-path argv used by posture gating (ADR-0012)."""
    return list(git_probe_command("origin", "write"))


def _default_github_write_runner(command: list[str], repo_root: Path, *, timeout: float) -> int:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1
    except OSError:
        return -1
    return completed.returncode


def probe_github_write(
    root: Path,
    *,
    runner=None,
    timeout: float = _GITHUB_WRITE_TIMEOUT,
) -> dict:
    """Non-mutating GitHub write probe for one watched checkout.

    Status is ``ok`` (exit 0), ``fail`` (definitive non-zero), or ``unknown``
    (missing checkout, timeout, or runner cannot execute). Never guesses the
    failure cause (posture vs credentials vs authz).
    """
    root = Path(root)
    tool = root.name
    if not (root.is_dir() and (root / ".git").exists()):
        return {
            "tool": tool,
            "root": str(root),
            "status": "unknown",
            "detail": "checkout missing or not a git repository",
        }
    command = github_write_command()
    if runner is None:
        code = _default_github_write_runner(command, root, timeout=timeout)
    else:
        code = runner(command, root)
    if code == 0:
        status = "ok"
        detail = "git push --dry-run write probe passed"
    elif code < 0:
        status = "unknown"
        detail = f"write probe timed out or could not run (code {code})"
    else:
        status = "fail"
        detail = f"write probe exited {code}"
    return {
        "tool": tool,
        "root": str(root),
        "status": status,
        "detail": detail,
        "command": command,
    }


def aggregate_github_write(tool_results: list[dict]) -> str:
    if not tool_results:
        return "unknown"
    statuses = [str(item.get("status") or "unknown") for item in tool_results]
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status != "ok" for status in statuses):
        return "unknown"
    return "ok"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py::test_github_write_command_matches_posture_write_argv tests/test_edge_console.py::test_probe_github_write_ok_fail_unknown tests/test_edge_console.py::test_probe_github_write_timeout_is_unknown tests/test_edge_console.py::test_probe_github_write_disables_prompts_and_uses_write_argv tests/test_edge_console.py::test_aggregate_github_write_rules -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_console.py tests/test_edge_console.py
git commit -m "feat(console): add per-tool GitHub write probe helpers"
```

---

### Task 2: Wire write probes into `/api/posture` without changing divergence

**Files:**
- Modify: `edge_console.py` (`PostureProber`, `main`, module docstring)
- Modify: `tests/test_edge_console.py`

**Interfaces:**
- Consumes: `probe_github_write`, `aggregate_github_write`, watched roots from `ConsoleHandler.roots`
- Produces: `/api/posture` JSON where `groups.github` is:

```python
{
    "aggregate": "ok",  # or "fail" or "unknown"
    "tools": [
        {
            "tool": "autobench",
            "root": "D:/edge-deploy/autobench",
            "status": "ok",  # or "fail" or "unknown"
            "detail": "git push --dry-run write probe passed",
        },
        {
            "tool": "robocop",
            "root": "D:/edge-deploy/robocop",
            "status": "fail",
            "detail": "write probe exited 128",
        },
    ],
}
```

Bitbucket/edge groups remain `list[{"endpoint": str, "reachable": bool}]`.

- [ ] **Step 1: Write the failing tests**

Extend the module-top imports in `tests/test_edge_console.py` with `PostureProber` (keep all imports at file top). Append:

```python
def test_posture_prober_github_uses_write_probes_not_tcp(tmp_path, monkeypatch) -> None:
    auto = tmp_path / "autobench"
    robo = tmp_path / "robocop"
    for root in (auto, robo):
        root.mkdir()
        (root / ".git").mkdir()

    def runner(command: list[str], cwd):
        assert command == github_write_command()
        return 0 if Path(cwd).name == "autobench" else 128

    monkeypatch.setattr(
        "edge_console.probe_github_write",
        lambda root, runner=None, timeout=20.0: {
            "tool": Path(root).name,
            "root": str(root),
            "status": "ok" if Path(root).name == "autobench" else "fail",
            "detail": "test",
            "command": github_write_command(),
        },
    )
    prober = PostureProber(demo=False, roots=[auto, robo])
    # Force TCP groups empty/fast: stub _probe_one True for bitbucket only path by
    # replacing snapshot internals via monkeypatch on ThreadPoolExecutor path.
    monkeypatch.setattr("edge_console._edge_endpoints", lambda: [])
    monkeypatch.setattr("edge_console._probe_one", lambda host, port: True)
    snap = prober.snapshot()
    assert snap["groups"]["github"]["aggregate"] == "fail"
    by_tool = {row["tool"]: row["status"] for row in snap["groups"]["github"]["tools"]}
    assert by_tool == {"autobench": "ok", "robocop": "fail"}
    assert isinstance(snap["groups"]["bitbucket"], list)


def test_posture_prober_github_unknown_without_roots(monkeypatch) -> None:
    monkeypatch.setattr("edge_console._edge_endpoints", lambda: [])
    monkeypatch.setattr("edge_console._probe_one", lambda host, port: False)
    snap = PostureProber(demo=False, roots=[]).snapshot()
    assert snap["groups"]["github"]["aggregate"] == "unknown"
    assert snap["groups"]["github"]["tools"] == []


def test_divergence_still_uses_ls_remote_only(tmp_path) -> None:
    """Regression: write probes must not replace divergence read probes."""
    deployed = "d" * 40
    runs_root = tmp_path / "edge-deploy" / "runs"
    _write_state(
        runs_root / "run-20260709T000000Z-deploy1",
        "run-20260709T000000Z-deploy1",
        status="complete",
        source_sha=deployed,
        created_at="2026-07-09T00:00:00+00:00",
    )
    runs = collect_runs(runs_root)
    calls: list[tuple] = []

    def git(root, *args, timeout=None):
        calls.append(args)
        return _fake_git(head=deployed, origin=deployed)(root, *args, timeout=timeout)

    result = probe_divergence(tmp_path, runs, git=git)
    assert result["verdict"] == "up_to_date"
    assert any(args[:2] == ("ls-remote", "origin") for args in calls)
    assert not any(args and args[0] == "push" for args in calls)


def test_demo_posture_github_is_write_shaped() -> None:
    snap = PostureProber(demo=True, roots=[]).snapshot()
    github = snap["groups"]["github"]
    assert github["aggregate"] in {"ok", "fail", "unknown"}
    assert {row["tool"] for row in github["tools"]} == {"autobench", "robocop"}
    assert all(row["status"] in {"ok", "fail", "unknown"} for row in github["tools"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py::test_posture_prober_github_uses_write_probes_not_tcp tests/test_edge_console.py::test_posture_prober_github_unknown_without_roots tests/test_edge_console.py::test_divergence_still_uses_ls_remote_only tests/test_edge_console.py::test_demo_posture_github_is_write_shaped -v`

Expected: FAIL because `PostureProber.__init__` does not accept `roots` and `groups["github"]` is still a TCP endpoint list.

- [ ] **Step 3: Wire `PostureProber` and `main`**

Update `PostureProber`:

```python
class PostureProber:
    def __init__(self, demo: bool, roots: list[Path] | None = None) -> None:
        self._demo = demo
        self._roots = list(roots or [])
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def snapshot(self) -> dict:
        if self._demo:
            tools = [
                {
                    "tool": "autobench",
                    "root": "(demo)/autobench",
                    "status": "fail",
                    "detail": "demo: GitHub write unavailable outside firewall-off",
                },
                {
                    "tool": "robocop",
                    "root": "(demo)/robocop",
                    "status": "fail",
                    "detail": "demo: GitHub write unavailable outside firewall-off",
                },
            ]
            return {
                "probed_at": time.strftime("%H:%M:%SZ", time.gmtime()),
                "groups": {
                    "github": {
                        "aggregate": aggregate_github_write(tools),
                        "tools": tools,
                    },
                    "bitbucket": [
                        {"endpoint": "scm.mastercard.int:443", "reachable": True},
                    ],
                    "edge": [
                        {"endpoint": "hdpedge03.mastercard.int:2222", "reachable": False},
                        {"endpoint": "hdpedge04.mastercard.int:2222", "reachable": False},
                    ],
                },
            }
        with self._lock:
            if self._cached and time.monotonic() - self._cached_at < _PROBE_CACHE_SECONDS:
                return self._cached
        groups: dict[str, list[tuple[str, int]]] = {
            key: value for key, value in _STATIC_GROUPS.items() if key != "github"
        }
        edge = _edge_endpoints()
        if edge:
            groups["edge"] = edge
        flat = [(name, host, port) for name, eps in groups.items() for host, port in eps]
        with ThreadPoolExecutor(max_workers=max(1, len(flat) or 1)) as pool:
            reachable = list(pool.map(lambda item: _probe_one(item[1], item[2]), flat)) if flat else []
        result: dict = {
            "probed_at": time.strftime("%H:%M:%SZ", time.gmtime()),
            "groups": {},
        }
        for (name, host, port), ok in zip(flat, reachable):
            result["groups"].setdefault(name, []).append(
                {"endpoint": f"{host}:{port}", "reachable": ok}
            )
        with ThreadPoolExecutor(max_workers=max(1, len(self._roots) or 1)) as pool:
            write_tools = list(pool.map(probe_github_write, self._roots)) if self._roots else []
        # Strip command from API payload (argv is an implementation detail).
        api_tools = [
            {
                "tool": row["tool"],
                "root": row["root"],
                "status": row["status"],
                "detail": row["detail"],
            }
            for row in write_tools
        ]
        result["groups"]["github"] = {
            "aggregate": aggregate_github_write(api_tools),
            "tools": api_tools,
        }
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result
```

In `main()`:

```python
ConsoleHandler.prober = PostureProber(demo=args.demo, roots=roots)
```

Update the module docstring so it no longer claims Git is strictly read-only or that posture probes are TCP-only for GitHub. Replace the opening probe paragraph with:

```text
Posture probes: Bitbucket and Edge remain TCP-only and labelled as such. The
GitHub capability light is a per-watched-tool ``git push --dry-run`` write
probe (same argv as posture gating); it never updates a remote ref. Divergence
facts still use read-only git (``ls-remote`` for GitHub main).
```

Remove `"github"` from driving any TCP-only honesty copy that claims GitHub green means TCP up.

- [ ] **Step 4: Run targeted tests plus full console suite**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py -v`

Expected: PASS (including prior divergence tests).

- [ ] **Step 5: Commit**

```bash
git add edge_console.py tests/test_edge_console.py
git commit -m "feat(console): drive GitHub light from write probes"
```

---

### Task 3: UI — per-tool write outcomes and aggregate green rule

**Files:**
- Modify: `edge_console.py` (`PAGE` CSS/JS/footer)
- Test: `tests/test_edge_console.py`

**Interfaces:**
- Consumes: `/api/posture` `groups.github.aggregate` and `groups.github.tools[]`
- Produces: posture panel rendering where GitHub shows per-tool status dots; aggregate chip/light is green only for `aggregate === "ok"`

- [ ] **Step 1: Write the failing page-contract tests**

```python
def test_page_mentions_github_write_probe_not_tcp_authority() -> None:
    assert "git push --dry-run" in PAGE
    assert "github write" in PAGE.lower()
    # Must match the JS in Step 3: githubWriteHtml(p.groups.github)
    assert "p.groups.github" in PAGE
    assert "githubWriteHtml" in PAGE
    assert "aggregate" in PAGE
    assert "github write unavailable" in PAGE.lower()


def test_page_render_helpers_include_github_write_statuses() -> None:
    assert 'status || "unknown"' in PAGE or "status || 'unknown'" in PAGE
    assert "githubWriteAgg === \"ok\"" in PAGE or 'githubWriteAgg === "ok"' in PAGE
    assert "githubWriteAgg === \"fail\"" in PAGE or 'githubWriteAgg === "fail"' in PAGE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py::test_page_mentions_github_write_probe_not_tcp_authority tests/test_edge_console.py::test_page_render_helpers_include_github_write_statuses -v`

Expected: FAIL on missing write-probe UI copy / aggregate handling.

- [ ] **Step 3: Update `PAGE` JS/CSS/footer**

1. Extend CSS with unknown/fail/ok endpoint states for write rows:

```css
.endpoint.unknown .dot{background:transparent;border:1.5px solid var(--warn)}
.endpoint.ok .dot{background:var(--pass);box-shadow:0 0 5px rgba(121,201,143,.7)}
.endpoint.fail .dot{background:transparent;border:1.5px solid var(--fail)}
```

2. Replace `postureHtml` GitHub branch so `github` is not mapped through TCP `reachable`:

```javascript
function githubWriteHtml(g){
  const agg = g.aggregate || "unknown";
  const rows = (g.tools || []).map(t => {
    const st = t.status || "unknown";
    return `<div class="endpoint ${st}"><span class="dot"></span>${esc(t.tool)} · ${esc(st)}${t.detail ? ` · ${esc(t.detail)}` : ""}</div>`;
  }).join("") || `<div class="endpoint unknown"><span class="dot"></span>no watched tools</div>`;
  return `<div class="pgroup gh"><h3>github write · ${esc(agg)}</h3>${rows}</div>`;
}

function postureHtml(p){
  const order = ["github","bitbucket","edge"];
  const cls = {github:"gh", bitbucket:"bb", edge:"edge"};
  return order.filter(g => p.groups[g]).map(g => {
    if(g === "github") return githubWriteHtml(p.groups.github);
    const rows = p.groups[g].map(e =>
      `<div class="endpoint ${e.reachable ? "up" : "down"}"><span class="dot"></span>${esc(e.endpoint)}</div>`
    ).join("");
    return `<div class="pgroup ${cls[g]}"><h3>${esc(g)}</h3>${rows}</div>`;
  }).join("");
}
```

3. Update `inferPostures` / notes:
   - Keep Bitbucket/Edge TCP inference for VPN posture chips.
   - Stop implying GitHub TCP up means write. In `postureNote`, when both VPNs are up, keep the existing both-vpns guidance and append that GitHub write aggregate is shown separately.
   - Change `readinessHtml` for `req === "gh"` to use the write aggregate when available:

```javascript
let githubWriteAgg = null; // set when /api/posture arrives

function readinessHtml(req){
  if(!tcpCaps) return "";
  if(req === "any") return `<span class="readiness ok">runs in any posture</span>`;
  if(req === "gh"){
    if(githubWriteAgg === "ok")
      return `<span class="readiness ok">github write ok</span>`;
    if(githubWriteAgg === "fail")
      return `<span class="readiness blocked">github write unavailable</span>`;
    return `<span class="readiness">github write unknown</span>`;
  }
  const ok = req === "bb" ? tcpCaps.bb : (tcpCaps.bb && tcpCaps.edge);
  return ok ? `<span class="readiness ok">posture ok (tcp)</span>`
            : `<span class="readiness blocked">switch needed</span>`;
}
```

Where posture fetch assigns `githubWriteAgg = p.groups.github && p.groups.github.aggregate`.

4. Footer text — replace the TCP-only GitHub sentence with:

```html
<footer>Bitbucket/Edge lights are TCP-only. The GitHub light is a per-tool
<code>git push --dry-run</code> write probe (no ref update); green only when every
watched tool passes. Divergence still uses read-only <code>ls-remote</code>
(GitHub read works in every posture). Phase git-protocol probes remain
authoritative for release commands (ADR-0012/0013).</footer>
```

- [ ] **Step 4: Run console tests and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_edge_console.py -v
.venv/bin/ruff check edge_console.py tests/test_edge_console.py
```

Expected: PASS / Ruff clean.

- [ ] **Step 5: Manual offline smoke**

Run: `.venv/bin/python edge_console.py --demo --no-browser`

Expected: server starts on `http://127.0.0.1:7643`; `curl -s http://127.0.0.1:7643/api/posture` shows `groups.github.aggregate` of `fail` with per-tool statuses; `/api/tools` still returns divergence fields driven by `ls-remote` (demo git stub), not `push`.

- [ ] **Step 6: Commit**

```bash
git add edge_console.py tests/test_edge_console.py
git commit -m "feat(console): render GitHub write aggregate and per-tool status"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
| --- | --- |
| Replace GitHub TCP/read lights with write probes | Task 2 |
| Same argv as posture `git push --dry-run --force ... posture-probe` | Task 1 |
| Disable credential prompts | Task 1 |
| Per-tool ok/fail/unknown + detail | Tasks 1–3 |
| Aggregate green only if all watched tools pass | Tasks 1–3 |
| Red on definitive failure; unknown when probes cannot run | Task 1 |
| Divergence `ls-remote` unchanged | Task 2 regression |
| No remote ref created (`--dry-run`) | Task 1 command assertion |
| Bitbucket/Edge keep TCP indicators | Task 2 |
| Console remains standalone / no ledger writes | All tasks |

## Placeholder scan

No TBD/TODO placeholders. All APIs, commands, and expected outcomes are concrete.

## Type consistency

- Status vocabulary is always `"ok" | "fail" | "unknown"`.
- Aggregate field name is always `aggregate`.
- Probe helper names: `github_write_command`, `probe_github_write`, `aggregate_github_write`.
