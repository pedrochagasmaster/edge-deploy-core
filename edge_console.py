"""edge_console — read-only posture console for edge-deploy runs.

A single-file, zero-dependency local web UI over one or more run ledgers
(``<checkout>/edge-deploy/runs/``). It renders each Run as a rail through the
five workstation postures (ADR-0013): stations are tinted by the capability
they need (bitbucket, edge, github write), VPN joins are soft boundaries, and
the one hard wall — dropping both VPNs for firewall-off before ``tag_github``
— is drawn as a hazard gate. Per-node deploy state (each node's compact
rollout report: drift, smoke, and the state a failure left behind), the live
operation from ``release-progress.json`` (auth waits, rollouts, and verified
binary transfers over the Paramiko transport — ADR-0014), the exact next
command, and the tail of the run's event log sit under each rail.

Above the runs, one card per watched tool checkout answers "should I
release?": it compares the last completed run's source SHA (what the Edge
Nodes are believed to hold) against the checkout's HEAD and against GitHub
``main`` (``git ls-remote`` — GitHub read works in every posture), flags
undeployed commits and stale checkouts, and — when no Run is open — walks the
operator through starting one (posture to hold, optional ``preflight`` /
``transport-smoke``, then ``py -m edge_deploy release --guided``).

Deliberately standalone: Engine Identity (ADR-0008) hashes every ``*.py``
inside the ``edge_deploy`` package, so UI code must live outside the engine
or every open Run would be orphaned. This file never writes to the ledger.

Usage:

    py edge_console.py                              # watch the cwd checkout
    py edge_console.py --root D:\\ab --root D:\\rc    # watch several checkouts
    py edge_console.py --demo                       # fabricated checkouts, no probes

Posture probes: Bitbucket and Edge remain TCP-only and labelled as such. The
GitHub capability light is a per-watched-tool ``git push --dry-run`` write
probe (same argv as posture gating); it never updates a remote ref. Divergence
facts still use read-only git (``ls-remote`` for GitHub main).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, load_operator_config
from edge_deploy.posture import git_probe_command
from edge_deploy.preflight import endpoint_from_node

_SCHEMA = "edge-deploy/run/1"
_EVENT_TAIL = 60
_PROBE_TIMEOUT = 1.5
_PROBE_CACHE_SECONDS = 10.0
GITHUB_WRITE_STATUSES = frozenset({"ok", "fail", "unknown"})

_STATIC_GROUPS: dict[str, list[tuple[str, int]]] = {
    "bitbucket": [("scm.mastercard.int", 443)],
}


# ---------------------------------------------------------------------------
# Ledger reading (raw JSON — no engine import, no writes)
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail_events(run_dir: Path) -> list[dict]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-_EVENT_TAIL:]:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def collect_runs(runs_root: Path) -> list[dict]:
    if not runs_root.is_dir():
        return []
    runs: list[dict] = []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        state = _read_json(entry / "state.json")
        if not state or state.get("schema") != _SCHEMA:
            continue
        lock = _read_json(entry / "run.lock") if (entry / "run.lock").is_file() else None
        progress = _read_json(entry / "release-progress.json")
        runs.append(
            {"state": state, "events": _tail_events(entry), "lock": lock, "progress": progress}
        )
    # Open runs first, then newest first within each group.
    runs.sort(
        key=lambda r: (
            r["state"].get("status") != "open",
            r["state"].get("created_at", ""),
        ),
    )
    open_runs = [r for r in runs if r["state"].get("status") == "open"]
    closed = [r for r in runs if r["state"].get("status") != "open"]
    closed.sort(key=lambda r: r["state"].get("created_at", ""), reverse=True)
    return open_runs + closed[:20]


def collect_runs_multi(roots: list[Path]) -> list[dict]:
    """Merge runs from several tool checkouts, tagging each run with its root."""
    merged: list[dict] = []
    for root in roots:
        for run in collect_runs(root / "edge-deploy" / "runs"):
            run["root"] = str(root)
            merged.append(run)
    open_runs = [r for r in merged if r["state"].get("status") == "open"]
    open_runs.sort(key=lambda r: r["state"].get("created_at", ""))
    closed = [r for r in merged if r["state"].get("status") != "open"]
    closed.sort(key=lambda r: r["state"].get("created_at", ""), reverse=True)
    return open_runs + closed[:20]


# ---------------------------------------------------------------------------
# TCP posture probes (informational only — see ADR-0012)
# ---------------------------------------------------------------------------

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


def _edge_endpoints() -> list[tuple[str, int]]:
    """Edge node endpoints from the operator config; empty if config cannot be loaded."""
    try:
        operator = load_operator_config(DEFAULT_OPERATOR_CONFIG_PATH)
        endpoints = []
        for name in sorted(operator.nodes):
            resolved = endpoint_from_node(operator.nodes[name])
            endpoints.append((resolved.hostname, resolved.port))
        return endpoints
    except Exception:
        return []


def _probe_one(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


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
        groups: dict[str, list[tuple[str, int]]] = dict(_STATIC_GROUPS)
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


# ---------------------------------------------------------------------------
# Demo checkouts (fabricated runs so the console renders without a real release)
# ---------------------------------------------------------------------------

def _demo_phase(state: str, at: str, evidence: dict | None = None) -> dict:
    return {"state": state, "updated_at": at, "evidence": evidence or {}}


# Fabricated git answers for the demo checkouts, keyed by directory name:
# autobench's HEAD is the open run's source (five commits past the June
# rollback); robocop has three reviewed commits merged since its release.
_DEMO_HEAD_BY_TOOL = {
    "autobench": "9c4f2ae8d1b06f3a7c5e2d4b8a1f0c9e6d3b7a52",
    "robocop": "7fa9e21c3d5b8a0f2e4c6d8b1a3f5c7e9d2b4a6c",
}
_DEMO_AHEAD_BY_TOOL = {"autobench": "5", "robocop": "3"}


def _demo_git(root: Path, *args: str, timeout: float | None = None) -> str | None:
    del timeout
    head = _DEMO_HEAD_BY_TOOL.get(root.name)
    if head is None or not args:
        return None
    if args[0] == "rev-parse":
        return head
    if args[0] == "ls-remote":
        return f"{head}\trefs/heads/main"
    if args[0] == "rev-list":
        return _DEMO_AHEAD_BY_TOOL.get(root.name)
    return None


def build_demo_checkouts() -> list[Path]:
    """Two fabricated tool checkouts (autobench, robocop), each with its own ledger."""
    base = Path(tempfile.mkdtemp(prefix="edge-console-demo-"))
    try:  # standalone by design (ADR-0008); demo just mirrors the installed version
        from edge_deploy import __version__ as engine_version
    except Exception:
        engine_version = "unknown"
    engine = {"version": engine_version, "package_dir": "(demo)", "content_sha256": "d3m0" + "0" * 60}

    checkouts: dict[str, Path] = {}
    for tool in ("autobench", "robocop"):
        checkout = base / tool
        (checkout / "edge-deploy" / "runs").mkdir(parents=True)
        (checkout / "edge_deploy.yaml").write_text(f"tool: {tool}\n", encoding="utf-8")
        checkouts[tool] = checkout

    def write(
        run: dict,
        events: list[dict],
        *,
        lock: dict | None = None,
        progress: dict | None = None,
    ) -> None:
        run_dir = checkouts[run["tool"]] / "edge-deploy" / "runs" / run["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
        lines = "".join(json.dumps(e) + "\n" for e in events)
        (run_dir / "events.jsonl").write_text(lines, encoding="utf-8")
        if lock:
            (run_dir / "run.lock").write_text(json.dumps(lock), encoding="utf-8")
        if progress:
            (run_dir / "release-progress.json").write_text(
                json.dumps(progress, indent=2), encoding="utf-8"
            )

    def rollout_evidence(tool: str, node: str, status: str, sha: str, **overrides: object) -> dict:
        # Mirrors release._compact_rollout: the deploy phase stores each
        # node's compact rollout report verbatim as ledger evidence.
        evidence: dict = {
            "tool": tool,
            "node": node,
            "status": status,
            "state_left": "",
            "deployment_commit": sha,
            "previous_remote_commit": "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e",
            "sensitive_changed": [],
            "drift": "passed",
            "smoke": "passed",
            "report_path": f"(demo)/rollout-{tool}-{node}.json",
            "dependency": None,
        }
        evidence.update(overrides)
        return evidence

    # 1) Open autobench release, mid-deploy: one node rolled out but failed
    #    verification, one is mid-rollout with a dependency bundle streaming
    #    over SFTP (ADR-0014) — so the lock is held and progress is live.
    sha_a = "9c4f2ae8d1b06f3a7c5e2d4b8a1f0c9e6d3b7a52"
    snap_a = sha_a
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260707T131512Z-9c4f2ae",
            "tool": "autobench",
            "source_sha": sha_a,
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-07-07T13:15:12+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": engine,
            "nodes": ["node03", "node04", "node05"],
            "status": "open",
            "abandon_reason": None,
            "phases": {
                "verify": _demo_phase(
                    "passed",
                    "2026-07-07T13:16:02+00:00",
                    {"commit": sha_a, "ci": "success", "tests": "passed",
                     "verified_at": "2026-07-07T13:16:02+00:00"},
                ),
                "publish": _demo_phase(
                    "passed",
                    "2026-07-07T13:31:44+00:00",
                    {
                        "snapshot_sha": snap_a,
                        "source_commit": sha_a,
                        "previous_remote_commit": "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e",
                    },
                ),
                "deploy": {
                    "node03": _demo_phase(
                        "passed",
                        "2026-07-07T13:40:19+00:00",
                        rollout_evidence("autobench", "node03", "rolled_out", snap_a),
                    ),
                    "node04": _demo_phase(
                        "failed",
                        "2026-07-07T13:44:51+00:00",
                        rollout_evidence(
                            "autobench", "node04", "failed", snap_a,
                            state_left="rolled out but verification failed: smoke:hive_connectivity",
                            smoke="failed",
                        ),
                    ),
                    "node05": _demo_phase("pending", None),
                },
                "tag_bitbucket": _demo_phase("pending", None),
                "tag_github": _demo_phase("pending", None),
            },
        },
        # The engine only ledgers run_created / phase_entered / phase_skipped /
        # lock_stolen / run_abandoned / run_completed; outcomes live in
        # state.json phases, not the event log.
        [
            {"ts": "2026-07-07T13:15:12+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-07-07T13:15:40+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-07-07T13:29:10+00:00", "event": "phase_entered", "phase": "publish", "node": None},
            {"ts": "2026-07-07T13:33:05+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
            {"ts": "2026-07-07T13:45:58+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
        ],
        lock={"pid": 21440, "hostname": "MC-L-OPERATOR", "acquired_at": "2026-07-07T13:45:58+00:00"},
        progress={
            "schema": "edge-deploy/release-progress/1",
            "updated_at": "2026-07-07T13:47:21+00:00",
            "elapsed_s": 83.0,
            "active": {
                "phase": "rollout",
                "label": "rollout autobench/node05",
                "tool": "autobench",
                "node": "node05",
                "tmux_session": None,
                "last_meaningful_output_at": "2026-07-07T13:47:21+00:00",
                "waiting_on": None,
                "transfer": {
                    "artifact": "autobench dependency bundle",
                    "bytes_sent": 27262976,
                    "total_bytes": 44040192,
                    "percent": 61.9,
                    "bytes_per_second": 419430.4,
                    "updated_at": "2026-07-07T13:47:21+00:00",
                },
            },
            "inactive_s": 1.2,
        },
    )

    # 2) Completed robocop release: the deployed baseline that robocop's
    #    divergence card is judged against (the fabricated git answers say
    #    three reviewed commits have merged since, so with no open run the
    #    console suggests a release and shows the start-a-release guide).
    sha_b = "41d9b0c7e2f5a8d1b4c7e0f3a6d9b2c5e8f1a4d7"
    tag_b = "release-20260707T153012Z-41d9b0c"
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260707T140233Z-41d9b0c",
            "tool": "robocop",
            "source_sha": sha_b,
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-07-07T14:02:33+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": engine,
            "nodes": ["node03", "node04"],
            "status": "complete",
            "abandon_reason": None,
            "phases": {
                "verify": _demo_phase(
                    "passed",
                    "2026-07-07T14:03:20+00:00",
                    {"commit": sha_b, "ci": "success", "tests": "passed",
                     "verified_at": "2026-07-07T14:03:20+00:00"},
                ),
                "publish": _demo_phase(
                    "passed",
                    "2026-07-07T14:21:08+00:00",
                    {
                        "snapshot_sha": sha_b,
                        "source_commit": sha_b,
                        "previous_remote_commit": "b82c1f04a7d3e6b9c2f5a8d1e4b7c0f3a6d9b2c5",
                    },
                ),
                "deploy": {
                    "node03": _demo_phase(
                        "passed", "2026-07-07T14:39:47+00:00",
                        rollout_evidence("robocop", "node03", "rolled_out", sha_b),
                    ),
                    "node04": _demo_phase(
                        "passed", "2026-07-07T14:52:30+00:00",
                        rollout_evidence("robocop", "node04", "rolled_out", sha_b),
                    ),
                },
                "tag_bitbucket": _demo_phase(
                    "passed", "2026-07-07T14:58:11+00:00", {"tag": tag_b, "pushed_sha": sha_b}
                ),
                "tag_github": _demo_phase(
                    "passed", "2026-07-07T15:30:12+00:00", {"tag": tag_b, "pushed_sha": sha_b}
                ),
            },
        },
        [
            {"ts": "2026-07-07T14:02:33+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-07-07T14:02:41+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-07-07T14:19:52+00:00", "event": "phase_entered", "phase": "publish", "node": None},
            {"ts": "2026-07-07T14:23:31+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
            {"ts": "2026-07-07T14:57:48+00:00", "event": "phase_entered", "phase": "tag_bitbucket", "node": None},
            {"ts": "2026-07-07T15:29:40+00:00", "event": "phase_entered", "phase": "tag_github", "node": None},
            {"ts": "2026-07-07T15:30:12+00:00", "event": "run_completed", "phase": None, "node": None},
        ],
    )

    # 3) Completed rollback. Publish is seeded "passed" at run creation (the
    #    rollback tag supplies the snapshot); verify is entered, then skipped.
    sha_c = "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e"
    rollback_tag = "release-20260622T110402Z-5f01d77"
    minted_tag = "release-20260630T094001Z-5f01d77"
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260630T091501Z-5f01d77",
            "tool": "autobench",
            "source_sha": sha_c,
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-06-30T09:15:01+00:00",
            "kind": "rollback",
            "rollback_tag": rollback_tag,
            "engine": engine,
            "nodes": ["node03", "node04", "node05"],
            "status": "complete",
            "abandon_reason": None,
            "phases": {
                "verify": _demo_phase(
                    "skipped",
                    "2026-06-30T09:15:20+00:00",
                    {
                        "reason": "rollback tag provides reviewed source and snapshot SHA",
                        "rollback_tag": rollback_tag,
                        "source_sha": sha_c,
                    },
                ),
                "publish": _demo_phase(
                    "passed", "2026-06-30T09:15:01+00:00", {"snapshot_sha": sha_c, "source_commit": sha_c}
                ),
                "deploy": {
                    "node03": _demo_phase(
                        "passed", "2026-06-30T09:30:05+00:00",
                        rollout_evidence("autobench", "node03", "rolled_out", sha_c),
                    ),
                    "node04": _demo_phase(
                        "passed", "2026-06-30T09:33:41+00:00",
                        rollout_evidence("autobench", "node04", "rolled_out", sha_c),
                    ),
                    "node05": _demo_phase(
                        "passed", "2026-06-30T09:37:12+00:00",
                        rollout_evidence("autobench", "node05", "rolled_out", sha_c),
                    ),
                },
                "tag_bitbucket": _demo_phase(
                    "passed", "2026-06-30T09:40:02+00:00", {"tag": minted_tag, "pushed_sha": sha_c}
                ),
                "tag_github": _demo_phase(
                    "passed", "2026-06-30T09:55:47+00:00", {"tag": minted_tag, "pushed_sha": sha_c}
                ),
            },
        },
        [
            {"ts": "2026-06-30T09:15:01+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-06-30T09:15:12+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-06-30T09:15:20+00:00", "event": "phase_skipped", "phase": "verify", "node": None},
            {"ts": "2026-06-30T09:21:44+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
            {"ts": "2026-06-30T09:39:30+00:00", "event": "phase_entered", "phase": "tag_bitbucket", "node": None},
            {"ts": "2026-06-30T09:55:02+00:00", "event": "phase_entered", "phase": "tag_github", "node": None},
            {"ts": "2026-06-30T09:55:47+00:00", "event": "run_completed", "phase": None, "node": None},
        ],
    )

    # 4) Abandoned run (engine identity changed under it).
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260629T160248Z-b82c1f0",
            "tool": "robocop",
            "source_sha": "b82c1f04a7d3e6b9c2f5a8d1e4b7c0f3a6d9b2c5",
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-06-29T16:02:48+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": {**engine, "version": "1.4.0"},
            "nodes": ["node03", "node04"],
            "status": "abandoned",
            "abandon_reason": f"engine identity changed (1.4.0 -> {engine_version}); recreate the run",
            "phases": {
                "verify": _demo_phase("passed", "2026-06-29T16:03:30+00:00"),
                "publish": _demo_phase("pending", None),
                "deploy": {
                    "node03": _demo_phase("pending", None),
                    "node04": _demo_phase("pending", None),
                },
                "tag_bitbucket": _demo_phase("pending", None),
                "tag_github": _demo_phase("pending", None),
            },
        },
        [
            {"ts": "2026-06-29T16:02:48+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-06-29T16:03:02+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-06-30T08:58:12+00:00", "event": "run_abandoned", "phase": None, "node": None,
             "reason": f"engine identity changed (1.4.0 -> {engine_version})"},
        ],
    )
    return [checkouts["autobench"], checkouts["robocop"]]


# ---------------------------------------------------------------------------
# Release guidance: which tools have drifted from the deployed state.
# Read-only git, matching the console's no-writes contract: rev-parse and
# rev-list stay local; ls-remote asks GitHub main, which is a github-read
# action and so works in every posture (ADR-0013).
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 10.0
_TOOLS_CACHE_SECONDS = 30.0
_TOOL_NAME_RE = re.compile(r"^tool:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)


def _git(root: Path, *args: str, timeout: float = _GIT_TIMEOUT) -> str | None:
    """Run one read-only git command; None on any failure (never raises)."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on a credential prompt
    env.setdefault("GCM_INTERACTIVE", "never")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _tool_name(root: Path, runs: list[dict]) -> str:
    """The checkout's tool: its committed profile, else the ledger, else the directory."""
    try:
        match = _TOOL_NAME_RE.search((root / "edge_deploy.yaml").read_text(encoding="utf-8"))
    except OSError:
        match = None
    if match:
        return match.group(1)
    dated = sorted(runs, key=lambda r: r["state"].get("created_at", ""), reverse=True)
    for run in dated:
        tool = run["state"].get("tool")
        if tool:
            return str(tool)
    return root.name


def _last_deployed(runs: list[dict]) -> dict | None:
    """The newest complete run: what the Edge Nodes are believed to hold.

    Rollbacks count — a completed rollback's source_sha *is* the deployed state.
    """
    complete = [r["state"] for r in runs if r["state"].get("status") == "complete"]
    if not complete:
        return None
    last = max(complete, key=lambda s: s.get("created_at", ""))
    return {
        "sha": last.get("source_sha", ""),
        "run_id": last.get("run_id", ""),
        "kind": last.get("kind", "release"),
        "created_at": last.get("created_at", ""),
    }


def probe_divergence(root: Path, runs: list[dict], *, git=_git) -> dict:
    """Compare the last-deployed source SHA to checkout HEAD and GitHub main.

    Two independent comparisons: deployed-vs-HEAD says whether a release would
    ship something new; HEAD-vs-origin/main says whether the checkout and
    GitHub disagree. Any git failure degrades that fact to None — the UI shows
    what it cannot know rather than guessing.

    ``ahead`` counts locally (rev-list), so it is exact only when the live
    ls-remote proves the checkout holds all of GitHub main (``ahead_exact``);
    when the checkout is stale the count is a lower bound — still exactly what
    a release would ship right now, since a release ships checkout HEAD.

    When stale, ``stale_direction`` says which side is ahead — but only when
    GitHub main's commit object exists locally (a prior fetch), because both
    range counts need it: "local_behind" (pull), "local_ahead" (unpushed work:
    push/PR first — verify needs green GitHub CI on HEAD), "forked" (both
    moved), or None when git cannot tell.
    """
    deployed = _last_deployed(runs)
    head = git(root, "rev-parse", "HEAD", timeout=5.0)
    ls = git(root, "ls-remote", "origin", "refs/heads/main")
    origin_main = ls.split()[0] if ls else None
    ahead = None
    if deployed and deployed["sha"] and head:
        count = git(root, "rev-list", "--count", f"{deployed['sha']}..HEAD", timeout=5.0)
        if count and count.isdigit():
            ahead = int(count)
    stale = bool(head and origin_main and head != origin_main)
    ahead_exact = bool(head and origin_main and head == origin_main)
    stale_direction = None
    behind_origin = None
    ahead_of_origin = None
    if stale:
        behind_raw = git(root, "rev-list", "--count", f"HEAD..{origin_main}", timeout=5.0)
        ahead_raw = git(root, "rev-list", "--count", f"{origin_main}..HEAD", timeout=5.0)
        if behind_raw and behind_raw.isdigit() and ahead_raw and ahead_raw.isdigit():
            behind_origin = int(behind_raw)
            ahead_of_origin = int(ahead_raw)
            if behind_origin and not ahead_of_origin:
                stale_direction = "local_behind"
            elif ahead_of_origin and not behind_origin:
                stale_direction = "local_ahead"
            elif behind_origin and ahead_of_origin:
                stale_direction = "forked"
    if head is None:
        verdict = "unknown"
    elif deployed is None:
        verdict = "never_released"
    elif head != deployed["sha"]:
        verdict = "diverged"
    elif stale:
        verdict = "checkout_stale"
    else:
        verdict = "up_to_date"
    return {
        "verdict": verdict,
        "deployed": deployed,
        "head": head,
        "origin_main": origin_main,
        "ahead": ahead,
        "ahead_exact": ahead_exact,
        "stale": stale,
        "stale_direction": stale_direction,
        "behind_origin": behind_origin,
        "ahead_of_origin": ahead_of_origin,
    }


def probe_tool(root: Path, *, git=_git) -> dict:
    """Everything the tool card needs: identity, open run, nodes, divergence."""
    runs = collect_runs(root / "edge-deploy" / "runs")
    open_run = next(
        (r["state"]["run_id"] for r in runs if r["state"].get("status") == "open"), None
    )
    dated = sorted(runs, key=lambda r: r["state"].get("created_at", ""), reverse=True)
    nodes = list(dated[0]["state"].get("nodes") or []) if dated else []
    entry = {
        "root": str(root),
        "tool": _tool_name(root, runs),
        "open_run_id": open_run,
        "nodes": nodes,
    }
    entry.update(probe_divergence(root, runs, git=git))
    return entry


class ToolsProber:
    """Cached per-checkout divergence probe (ls-remote hits the network)."""

    def __init__(self, roots: list[Path], demo: bool) -> None:
        self._roots = roots
        self._git = _demo_git if demo else _git
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            if self._cached and time.monotonic() - self._cached_at < _TOOLS_CACHE_SECONDS:
                return self._cached
        with ThreadPoolExecutor(max_workers=max(1, len(self._roots))) as pool:
            tools = list(pool.map(lambda root: probe_tool(root, git=self._git), self._roots))
        result = {"probed_at": time.strftime("%H:%M:%SZ", time.gmtime()), "tools": tools}
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "edge-console"
    roots: list[Path]
    prober: PostureProber
    tools_prober: ToolsProber
    demo: bool

    def log_message(self, *args: object) -> None:  # keep the terminal quiet
        del args

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict) -> None:
        self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif self.path == "/api/runs":
            self._send_json(
                {
                    "roots": [str(root) for root in self.roots],
                    "demo": self.demo,
                    "runs": collect_runs_multi(self.roots),
                }
            )
        elif self.path == "/api/tools":
            self._send_json({"demo": self.demo, **self.tools_prober.snapshot()})
        elif self.path == "/api/posture":
            self._send_json(self.prober.snapshot())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only posture console for edge-deploy runs")
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="Tool checkout containing edge-deploy/runs; repeat to watch several (default: cwd)",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7643)))
    parser.add_argument("--demo", action="store_true", help="Serve fabricated checkouts; no network probes")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.demo:
        roots = build_demo_checkouts()
    else:
        roots = [Path(raw).resolve() for raw in (args.root or ["."])]

    ConsoleHandler.roots = roots
    ConsoleHandler.prober = PostureProber(demo=args.demo, roots=roots)
    ConsoleHandler.tools_prober = ToolsProber(roots, demo=args.demo)
    ConsoleHandler.demo = args.demo

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ConsoleHandler)
    url = f"http://127.0.0.1:{args.port}/"
    for root in roots:
        print(f"edge-console: watching {root / 'edge-deploy' / 'runs'}")
    print(f"edge-console: {url}  (read-only; Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# The page (inline everything: posture means the internet is never guaranteed)
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<link rel="icon" href="data:,">
<title>edge-deploy · posture console</title>
<style>
:root{
  --void:#141a21;
  --panel:#1a222b;
  --line:#2a3440;
  --ink:#dde4ec;
  --dim:#8b96a3;
  --faint:#5b6672;
  --gh:#7fb4e0;        /* github write (firewall-off) */
  --gh-band:#152535;
  --bb:#e8933f;        /* bitbucket vpn */
  --bb-band:#291d10;
  --edge:#58c0a8;      /* edge vpn */
  --edge-band:#12251f;
  --pass:#79c98f;
  --fail:#e2685c;
  --pend:#77828f;
  --warn:#d8a35a;
  --hazard-a:#43371f;
  --hazard-b:#1b1712;
  --mono:"Cascadia Code","Cascadia Mono",Consolas,"SF Mono",ui-monospace,monospace;
  --sans:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--void);color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.45}
a{color:var(--gh)}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px 64px}

/* ---------- header / posture panel ---------- */
header{border-bottom:1px solid var(--line);background:var(--panel)}
.masthead{max-width:1060px;margin:0 auto;padding:18px 20px 12px;display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end;justify-content:space-between}
.wordmark{font-family:var(--mono);font-size:17px;letter-spacing:.22em;font-weight:600}
.wordmark small{display:block;letter-spacing:.34em;font-size:10px;color:var(--dim);font-weight:400;margin-top:3px;text-transform:uppercase}
.posture{display:flex;gap:22px;align-items:flex-start;flex-wrap:wrap}
.pgroup{min-width:120px}
.pgroup h3{margin:0 0 5px;font-size:10px;letter-spacing:.18em;text-transform:uppercase;font-weight:600}
.pgroup.gh h3{color:var(--gh)}
.pgroup.bb h3{color:var(--bb)}
.pgroup.edge h3{color:var(--edge)}
.endpoint{font-family:var(--mono);font-size:11px;color:var(--dim);display:flex;gap:7px;align-items:center;padding:1px 0}
.endpoint .dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--faint)}
.endpoint.up .dot{background:var(--pass);box-shadow:0 0 5px rgba(121,201,143,.7)}
.endpoint.down .dot{background:transparent;border:1.5px solid var(--fail)}
.endpoint.unknown .dot{background:transparent;border:1.5px solid var(--warn)}
.endpoint.ok .dot{background:var(--pass);box-shadow:0 0 5px rgba(121,201,143,.7)}
.endpoint.fail .dot{background:transparent;border:1.5px solid var(--fail)}
.endpoint.up,.endpoint.ok{color:var(--ink)}
/* five-posture strip: the inferred current posture(s) lit, the rest dim */
.pstrip{max-width:1060px;margin:0 auto;padding:0 20px 10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pchip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;border:1px solid var(--line);border-radius:4px;padding:2.5px 9px;color:var(--faint)}
.pchip.on{color:var(--ink);border-color:var(--pass);box-shadow:inset 0 0 0 1px var(--pass)}
.pchip.maybe{color:var(--dim);border-style:dashed;border-color:var(--dim)}
.pnote{max-width:1060px;margin:0 auto;padding:0 20px 12px;font-size:11.5px;color:var(--dim)}
.pnote b{color:var(--ink);font-weight:600}

/* ---------- run cards ---------- */
.rootline{font-family:var(--mono);font-size:11px;color:var(--faint);padding:16px 0 4px}
.demo-flag{color:var(--warn);border:1px solid var(--warn);border-radius:3px;padding:0 6px;margin-left:8px;font-size:10px;letter-spacing:.1em}

/* ---------- run filter bar: toggle chips, all-on by default ---------- */
.filterbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:2px 0 12px}
.flabel{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin-right:2px}
.fchip{font-family:var(--mono);font-size:10.5px;letter-spacing:.03em;border:1px solid var(--line);border-radius:4px;padding:2.5px 9px;color:var(--dim);background:none;cursor:pointer;line-height:1.4}
.fchip:hover{border-color:var(--faint);color:var(--ink)}
.fchip:focus-visible{outline:2px solid var(--gh);outline-offset:1px}
.fchip.off{color:var(--faint);text-decoration:line-through;opacity:.6}
.fchip.tool.on{color:var(--ink);border-color:var(--faint)}
.fchip.status.open.on{color:var(--pass);border-color:var(--pass)}
.fchip.status.complete.on{color:var(--gh);border-color:var(--gh)}
.fchip.status.abandoned.on{color:var(--fail);border-color:var(--fail)}
.fchip.clear{color:var(--faint);border-style:dashed}
.fcount{font-family:var(--mono);font-size:10px;color:var(--faint);margin-left:2px}

/* ---------- tool cards: deployed-state divergence + start-a-release guide ---------- */
.toolcard{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-top:14px;overflow:hidden}
.toolhead{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;padding:12px 16px 4px}
.toolname{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.toolroot{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:auto}
.verdict{font-size:10px;letter-spacing:.14em;text-transform:uppercase;border:1px solid var(--line);border-radius:3px;padding:1.5px 7px;color:var(--dim)}
.verdict.ok{color:var(--pass);border-color:var(--pass)}
.verdict.warn{color:var(--warn);border-color:var(--warn)}
.verdict.dim{color:var(--faint)}
.divline{padding:2px 16px 0;font-family:var(--mono);font-size:11px;color:var(--dim)}
.divline:last-of-type{padding-bottom:12px}
.divline .warn{color:var(--warn)}
.divline .ok{color:var(--pass)}
.divline .sep2{color:var(--faint);padding:0 6px}
.inflight{padding:2px 16px 12px;font-family:var(--mono);font-size:11px;color:var(--pass)}
details.guide{border-top:1px solid var(--line)}
details.guide summary{padding:9px 16px;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);cursor:pointer;font-weight:600;list-style:none}
details.guide summary::before{content:"▸ ";color:var(--faint)}
details.guide[open] summary::before{content:"▾ "}
details.guide summary:focus-visible{outline:2px solid var(--gh);outline-offset:-2px}
.gstep{display:flex;gap:10px;align-items:center;padding:6px 16px;flex-wrap:wrap}
.gstep:last-child{padding-bottom:14px}
.gstep .gnum{font-family:var(--mono);font-size:11px;color:var(--faint);flex:none;width:14px;text-align:right}
.gstep .gtext{font-size:12px;color:var(--dim);flex:0 1 auto}
.gstep .gtext b{color:var(--ink);font-weight:600}
.gstep code{font-family:var(--mono);font-size:11.5px;background:var(--void);border:1px solid var(--line);border-radius:5px;padding:5px 9px;flex:1 1 300px;overflow-x:auto;white-space:nowrap;scrollbar-width:thin}
.gstep .opt{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);border:1px dashed var(--faint);border-radius:3px;padding:1px 5px;flex:none}
.gstep .readiness{font-family:var(--mono);font-size:10px;color:var(--faint)}
.gstep .readiness.ok{color:var(--pass)}
.gstep .readiness.blocked{color:var(--fail)}
.run{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-top:18px;overflow:hidden}
.run.closed{opacity:.62}
.run.closed:hover{opacity:1}
.runhead{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;padding:13px 16px 11px;border-bottom:1px solid var(--line)}
.runid{font-family:var(--mono);font-size:14px;font-weight:600}
.chip{font-size:10px;letter-spacing:.14em;text-transform:uppercase;border-radius:3px;padding:1.5px 7px;border:1px solid var(--line);color:var(--dim)}
.chip.tool{color:var(--ink);border-color:var(--faint)}
.chip.open{color:var(--pass);border-color:var(--pass)}
.chip.complete{color:var(--gh);border-color:var(--gh)}
.chip.abandoned{color:var(--fail);border-color:var(--fail)}
.chip.lock{color:var(--warn);border-color:var(--warn)}
.runmeta{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}

/* ---------- the posture rail (signature) ---------- */
.rail{display:flex;align-items:stretch;min-height:96px}
.station{flex:1 1 0;padding:12px 12px 14px;position:relative}
.station[data-req="any"]{background:var(--panel)}
.station[data-req="bb"]{background:var(--bb-band)}
.station[data-req="both"]{background:linear-gradient(90deg,var(--bb-band) 0 55%,var(--edge-band) 100%)}
.station[data-req="gh"]{background:var(--gh-band)}
.station .posture-tag{font-size:9px;letter-spacing:.16em;text-transform:uppercase;font-weight:600;color:var(--faint)}
.station[data-req="bb"] .posture-tag,.station[data-req="both"] .posture-tag{color:var(--bb)}
.station[data-req="gh"] .posture-tag{color:var(--gh)}
.posture-tag .cap-edge{color:var(--edge)}
.station h4{margin:3px 0 8px;font-family:var(--mono);font-size:12.5px;font-weight:600;letter-spacing:.02em}
.station.next h4::after{content:"◀\00a0next";color:var(--pass);font-size:10px;margin-left:8px;letter-spacing:.08em;white-space:nowrap}
.state{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;white-space:nowrap}
.when{display:block;font-family:var(--mono);font-size:10px;color:var(--faint);margin:3px 0 0 14px}
.state .dot{width:8px;height:8px;border-radius:50%;flex:none}
.state.passed{color:var(--pass)}.state.passed .dot{background:var(--pass)}
.state.failed{color:var(--fail)}.state.failed .dot{background:var(--fail)}
.state.pending{color:var(--pend)}.state.pending .dot{background:transparent;border:1.5px solid var(--pend)}
.state.skipped{color:var(--faint)}.state.skipped .dot{background:transparent;border:1.5px dashed var(--faint)}
.nodes{margin-top:2px;display:grid;gap:3px}
/* soft boundary: joining a VPN (no wall — both sides keep github read) */
.sep{flex:0 0 24px;position:relative;border-left:2px dashed var(--line)}
.sep span{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(90deg);white-space:nowrap;font-size:8.5px;letter-spacing:.22em;text-transform:uppercase;color:var(--faint);font-weight:600}
.sep.hot{border-left-color:var(--bb)}
.sep.hot span{color:var(--bb)}
/* hard boundary: firewall off drops both VPNs (the one real wall) */
.gate{flex:0 0 30px;background:repeating-linear-gradient(135deg,var(--hazard-a) 0 7px,var(--hazard-b) 7px 14px);position:relative;border-left:1px solid var(--line);border-right:1px solid var(--line)}
.gate span{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(90deg);white-space:nowrap;font-size:8.5px;letter-spacing:.3em;text-transform:uppercase;color:#a08a58;font-weight:600}
.gate.hot{outline:2px solid var(--bb);outline-offset:-2px;animation:gatepulse 1.6s ease-in-out infinite}
.gate.hot span{color:var(--bb)}
@keyframes gatepulse{0%,100%{outline-color:var(--bb)}50%{outline-color:transparent}}
@media (prefers-reduced-motion: reduce){.gate.hot{animation:none}}

/* ---------- live operation / next command / details ---------- */
.activeop{display:flex;gap:10px;align-items:center;padding:9px 16px;border-top:1px solid var(--line);flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--dim)}
.activeop .op-dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--edge);animation:oppulse 1.6s ease-in-out infinite}
.activeop.waiting .op-dot{background:var(--warn)}
.activeop.stalled .op-dot{background:var(--fail);animation:none}
.activeop .op-label{color:var(--ink)}
.activeop .op-note{color:var(--dim)}
.activeop.waiting .op-note{color:var(--warn);letter-spacing:.1em;text-transform:uppercase;font-size:10px;font-weight:600}
.activeop.stalled .op-note{color:var(--fail)}
.activeop .op-when{margin-left:auto;color:var(--faint)}
@keyframes oppulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion: reduce){.activeop .op-dot{animation:none}}
.transfer{display:flex;gap:10px;align-items:center;padding:9px 16px;border-top:1px solid var(--line);flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--dim)}
.transfer-artifact{flex:none}
.transfer-bar{flex:1 1 160px;height:6px;background:var(--void);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.transfer-fill{height:100%;background:var(--edge)}
.transfer-stats{flex:none;color:var(--faint)}

.nextcmd{display:flex;gap:10px;align-items:center;padding:11px 16px;border-top:1px solid var(--line);flex-wrap:wrap}
.nextcmd .label{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);font-weight:600;flex:none}
.nextcmd code{font-family:var(--mono);font-size:12px;background:var(--void);border:1px solid var(--line);border-radius:5px;padding:6px 10px;flex:1 1 320px;overflow-x:auto;white-space:nowrap;scrollbar-width:thin}
.nextcmd .need{font-size:10px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;flex:none}
.need.gh{color:var(--gh)}.need.bb{color:var(--bb)}.need.both{color:var(--bb)}.need.any{color:var(--dim)}
.nextcmd .readiness{font-family:var(--mono);font-size:10px;color:var(--faint);flex:none}
.readiness.ok{color:var(--pass)}.readiness.blocked{color:var(--fail)}
button.copy{font-family:var(--mono);font-size:11px;background:none;border:1px solid var(--faint);color:var(--dim);border-radius:5px;padding:5px 12px;cursor:pointer;flex:none}
button.copy:hover{color:var(--ink);border-color:var(--ink)}
button.copy:focus-visible{outline:2px solid var(--gh);outline-offset:2px}
.done-line{padding:11px 16px;border-top:1px solid var(--line);font-family:var(--mono);font-size:12px;color:var(--dim)}
.done-line.abandoned{color:var(--fail)}
details.log{border-top:1px solid var(--line)}
details.log summary{padding:9px 16px;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);cursor:pointer;font-weight:600;list-style:none}
details.log summary::before{content:"▸ ";color:var(--faint)}
details.log[open] summary::before{content:"▾ "}
details.log summary:focus-visible{outline:2px solid var(--gh);outline-offset:-2px}
.events{max-height:220px;overflow-y:auto;padding:0 16px 12px;font-family:var(--mono);font-size:11px}
.events div{padding:1.5px 0;color:var(--dim);white-space:nowrap}
.events .ts{color:var(--faint)}
.events .ev-name{color:var(--ink)}
.events .ev-passed{color:var(--pass)}.events .ev-failed{color:var(--fail)}
.empty{border:1px dashed var(--line);border-radius:8px;margin-top:22px;padding:34px 24px;text-align:center;color:var(--dim)}
.empty code{font-family:var(--mono);color:var(--ink)}
footer{margin-top:34px;font-size:11px;color:var(--faint)}
footer code{font-family:var(--mono)}

@media (max-width:720px){
  .rail{flex-direction:column}
  .gate{flex-basis:26px;border:0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .gate span{transform:translate(-50%,-50%)}
  .sep{flex-basis:22px;border-left:0;border-top:2px dashed var(--line)}
  .sep.hot{border-top-color:var(--bb)}
  .sep span{transform:translate(-50%,-50%)}
  .runmeta{margin-left:0;width:100%}
}
</style>
</head>
<body>
<header>
  <div class="masthead">
    <div class="wordmark">EDGE&nbsp;DEPLOY<small>posture console · read-only</small></div>
    <div class="posture" id="posture" aria-live="polite"></div>
  </div>
  <div class="pstrip" id="pstrip" aria-label="inferred workstation posture"></div>
  <div class="pnote" id="pnote"></div>
</header>

<main class="wrap">
  <div class="rootline" id="rootline"></div>
  <div id="tools"></div>
  <div class="filterbar" id="filterbar" aria-label="filter runs by tool and status"></div>
  <div id="runs"></div>
  <footer>Bitbucket/Edge lights are TCP-only. The GitHub light is a per-tool
  <code>git push --dry-run</code> write probe (no ref update); green only when every
  watched tool passes. Divergence still uses read-only <code>ls-remote</code>
  (GitHub read works in every posture). Phase git-protocol probes remain
  authoritative for release commands (ADR-0012/0013).</footer>
</main>

<script>
"use strict";

const PHASE_ORDER = ["verify","publish","deploy","tag_bitbucket","tag_github"];
// Capability requirement per phase (ADR-0013): "any" = github read, available
// in every posture; "bb" = bitbucket vpn; "both" = bitbucket + edge vpns;
// "gh" = github write (firewall off).
const PHASE_REQ = {verify:"any", publish:"bb", deploy:"both", tag_bitbucket:"bb", tag_github:"gh"};
const REQ_TAG = {
  any:  `github read`,
  bb:   `bitbucket`,
  both: `bitbucket <span class="cap-edge">+ edge</span>`,
  gh:   `github write`,
};
const REQ_POSTURE = {
  any:  "any posture",
  bb:   "bitbucket-vpn or both-vpns",
  both: "both-vpns",
  gh:   "firewall-off",
};
const PHASE_LABEL = {verify:"verify", publish:"publish", deploy:"deploy",
                     tag_bitbucket:"tag bitbucket", tag_github:"tag github"};
// The rail: five stations, two soft VPN joins, and the one hard wall —
// dropping both VPNs for firewall-off before tag_github.
const RAIL = [
  {phase:"verify"},
  {sep:"+ bitbucket vpn", before:"publish", cap:"bb"},
  {phase:"publish"},
  {sep:"+ edge vpn", before:"deploy", cap:"edge"},
  {phase:"deploy"},
  {phase:"tag_bitbucket"},
  {gate:"firewall off", before:"tag_github"},
  {phase:"tag_github"},
];
// Latest TCP inference from the posture panel ("bitbucket"/"edge" up flags);
// null until the first /api/posture answer arrives.
let tcpCaps = null;
let githubWriteAgg = null; // set when /api/posture arrives

function esc(s){
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function shortTs(iso){
  if(!iso) return "";
  const m = String(iso).match(/T(\d\d:\d\d:\d\d)/);
  return m ? m[1] + "Z" : iso;
}
function shortDate(iso){
  if(!iso) return "";
  return String(iso).replace(/T(\d\d:\d\d).*$/, " $1Z");
}

function phasePassed(run, phase){
  if(phase === "deploy"){
    const nodes = run.state.phases.deploy;
    return Object.values(nodes).every(n => n.state === "passed");
  }
  const s = run.state.phases[phase].state;
  return s === "passed" || s === "skipped";
}

function nextPhase(run){
  if(run.state.status !== "open") return null;
  for(const p of PHASE_ORDER) if(!phasePassed(run, p)) return p;
  return null;
}

function nextCommand(run, phase){
  const id = run.state.run_id;
  let cmd;
  if(phase === "verify")  cmd = `py -m edge_deploy verify --run ${id}`;
  else if(phase === "publish") cmd = `py -m edge_deploy publish-phase --run ${id}`;
  else if(phase === "deploy"){
    const pending = Object.entries(run.state.phases.deploy)
      .filter(([,n]) => n.state !== "passed").map(([name]) => name).sort();
    cmd = `py -m edge_deploy deploy --run ${id} --nodes ${pending.join(",")}`;
  }
  else if(phase === "tag_bitbucket") cmd = `py -m edge_deploy tag-bitbucket --run ${id}`;
  else if(phase === "tag_github")    cmd = `py -m edge_deploy tag-github --run ${id}`;
  else return "";
  // load_run() falls back to cwd when the run isn't under a configured
  // operator tool path (edge_deploy/phases/__init__.py); the console watches
  // several checkouts, so the copied command must not assume cwd == this
  // run's root the way a single-root console safely could.
  return run.root ? `cd "${run.root}"; ${cmd}` : cmd;
}

function stateChip(s){
  return `<span class="state ${esc(s.state)}"><span class="dot"></span>${esc(s.state)}</span>` +
         (s.updated_at ? `<span class="when">${shortTs(s.updated_at)}</span>` : "");
}

function stationHtml(run, phase, next){
  const req = PHASE_REQ[phase];
  const cls = phase === next ? "station next" : "station";
  let body;
  if(phase === "deploy"){
    // Deploy node keys are operator-config names ("node03"), and evidence is
    // the node's compact rollout report (release._compact_rollout):
    // state_left says what a failure left behind; drift/smoke are
    // "passed" | "failed" | "not_run".
    const nodes = run.state.phases.deploy;
    body = `<div class="nodes">` + Object.keys(nodes).sort().map(name => {
      const n = nodes[name];
      const ev = n.evidence || {};
      const hint = [
        ev.state_left,
        ev.drift && ev.drift !== "not_run" ? `drift ${ev.drift}` : "",
        ev.smoke && ev.smoke !== "not_run" ? `smoke ${ev.smoke}` : "",
      ].filter(Boolean).join(" · ");
      const title = hint ? ` title="${esc(hint)}"` : "";
      return `<span class="state ${esc(n.state)}"${title}><span class="dot"></span>${esc(name)} · ${esc(n.state)}</span>`;
    }).join("") + `</div>`;
  } else {
    const p = run.state.phases[phase];
    body = stateChip(p);
    if(phase === "publish" && p.evidence && p.evidence.snapshot_sha){
      body += `<div style="font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:5px">snapshot ${esc(p.evidence.snapshot_sha.slice(0,7))}</div>`;
    }
  }
  return `<div class="${cls}" data-req="${req}">
    <span class="posture-tag">${REQ_TAG[req]}</span>
    <h4>${esc(PHASE_LABEL[phase])}</h4>${body}</div>`;
}

function railHtml(run){
  const next = nextPhase(run);
  const parts = RAIL.map(item => {
    if(item.phase) return stationHtml(run, item.phase, next);
    if(item.gate){
      // The one hard wall: firewall-off drops both VPNs (ADR-0013). TCP
      // cannot see github write, so this stays hot as a position marker.
      const hot = next === item.before;
      return `<div class="gate${hot ? " hot" : ""}" role="separator" aria-label="drop VPNs, firewall off"><span>${esc(item.gate)}</span></div>`;
    }
    // A VPN join only glows when the run is waiting here AND that VPN is
    // actually down as far as TCP can see.
    const missing = !tcpCaps || !tcpCaps[item.cap];
    const hot = next === item.before && missing;
    return `<div class="sep${hot ? " hot" : ""}" role="separator" aria-label="join ${esc(item.sep)}"><span>${esc(item.sep)}</span></div>`;
  });
  return `<div class="rail">${parts.join("")}</div>`;
}

function eventsHtml(run){
  if(!run.events.length) return "";
  const rows = run.events.slice().reverse().map(e => {
    const cls = /failed|refused|blocked|abandoned|stolen/.test(e.event) ? "ev-failed"
              : /passed|completed|ok/.test(e.event) ? "ev-passed" : "";
    const where = [e.phase, e.node && `node ${e.node}`].filter(Boolean).join(" · ");
    const extra = Object.entries(e)
      .filter(([k]) => !["ts","event","phase","node"].includes(k) && e[k] != null)
      .map(([k,v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ");
    return `<div><span class="ts">${esc(shortTs(e.ts))}</span>  ` +
           `<span class="ev-name ${cls}">${esc(e.event)}</span>` +
           (where ? `  <span>${esc(where)}</span>` : "") +
           (extra ? `  <span class="ts">${esc(extra)}</span>` : "") + `</div>`;
  }).join("");
  return `<details class="log" data-key="${esc(run.state.run_id)}">
    <summary>event log · last ${run.events.length}</summary>
    <div class="events">${rows}</div></details>`;
}

// The live operation from release-progress.json (ADR-0014): the tracker
// records what the engine is doing right now (auth <node>, publish <tool>,
// rollout <tool>/<node>, verify <tool>/<node>), whether it is waiting on the
// operator (RSA prompt), a stall warning, and any in-flight verified binary
// transfer. active is null once the engine finishes; a crashed process can
// leave it stale, so the last-update time is always shown.
function progressHtml(run){
  const progress = run.progress;
  if(!progress || !progress.active || run.state.status !== "open") return "";
  const a = progress.active;
  let cls = "activeop", note = "";
  if(a.waiting_on === "operator"){
    cls += " waiting";
    note = "waiting for operator";
  } else if(progress.stall_warning){
    cls += " stalled";
    note = `stalled · no activity for ${Math.round(progress.inactive_s || 0)}s`;
  }
  let html = `<div class="${cls}">
    <span class="op-dot"></span>
    <span class="op-label">${esc(a.label || a.phase || "working")}</span>` +
    (note ? `<span class="op-note">${esc(note)}</span>` : "") +
    `<span class="op-when">updated ${esc(shortTs(progress.updated_at))}</span>
  </div>`;
  const transfer = a.transfer;
  if(transfer){
    const percent = Math.max(0, Math.min(100, transfer.percent));
    const mibSent = (transfer.bytes_sent / (1024*1024)).toFixed(1);
    const mibTotal = (transfer.total_bytes / (1024*1024)).toFixed(1);
    const rate = (transfer.bytes_per_second / (1024*1024)).toFixed(2);
    html += `<div class="transfer">
      <span class="transfer-artifact">${esc(transfer.artifact)}</span>
      <div class="transfer-bar"><div class="transfer-fill" style="width:${percent}%"></div></div>
      <span class="transfer-stats">${esc(mibSent)}/${esc(mibTotal)} MiB · ${esc(percent.toFixed(1))}% · ${esc(rate)} MiB/s</span>
    </div>`;
  }
  return html;
}

function runHtml(run){
  const st = run.state;
  const next = nextPhase(run);
  const statusChip = `<span class="chip ${esc(st.status)}">${esc(st.status)}</span>`;
  const lockChip = run.lock
    ? `<span class="chip lock" title="acquired ${esc(run.lock.acquired_at || "")}">locked · pid ${esc(run.lock.pid)} @ ${esc(run.lock.hostname)}</span>`
    : "";
  const kindChip = st.kind !== "release" ? `<span class="chip">${esc(st.kind)}</span>` : "";
  const rollback = st.rollback_tag
    ? `<span class="chip" title="restores this recorded release tag">→ ${esc(st.rollback_tag)}</span>` : "";

  let tail;
  if(st.status === "open" && next){
    const req = PHASE_REQ[next];
    const needText = req === "any" ? "any posture" : `needs ${REQ_POSTURE[req]}`;
    tail = `<div class="nextcmd">
      <span class="label">next</span>
      <span class="need ${req}">${esc(needText)}</span>
      <code>${esc(nextCommand(run, next))}</code>
      <button class="copy" data-cmd="${esc(nextCommand(run, next))}">copy</button>
      ${readinessHtml(req)}
    </div>`;
  } else if(st.status === "abandoned"){
    tail = `<div class="done-line abandoned">abandoned — ${esc(st.abandon_reason || "no reason recorded")}</div>`;
  } else {
    tail = `<div class="done-line">complete — release-tagged on GitHub and Bitbucket</div>`;
  }

  return `<article class="run ${st.status === "open" ? "" : "closed"}">
    <div class="runhead">
      <span class="runid">${esc(st.run_id)}</span>
      <span class="chip tool">${esc(st.tool)}</span>${kindChip}${statusChip}${lockChip}${rollback}
      <span class="runmeta">source ${esc(st.source_sha.slice(0,7))} · ${esc(st.operator)} · engine ${esc(st.engine && st.engine.version || "?")} · ${esc(shortDate(st.created_at))}</span>
    </div>
    ${railHtml(run)}
    ${progressHtml(run)}
    ${tail}
    ${eventsHtml(run)}
  </article>`;
}

/* ---------- posture panel ---------- */
const POSTURE_NAMES = ["baseline","edge-vpn","bitbucket-vpn","both-vpns","firewall-off"];

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

// VPN chips come from Bitbucket/Edge TCP only. GitHub write is a separate
// aggregate (ADR-0013); baseline and firewall-off stay indistinguishable via TCP.
function inferPostures(p){
  const up = g => (p.groups[g] || []).some(e => e.reachable);
  const bb = up("bitbucket"), edge = up("edge");
  if(bb && edge) return {certain:["both-vpns"], bb, edge};
  if(bb) return {certain:["bitbucket-vpn"], bb, edge};
  if(edge) return {certain:["edge-vpn"], bb, edge};
  return {certain:[], maybe:["baseline","firewall-off"], bb, edge};
}

function pstripHtml(inf){
  const maybe = inf.maybe || [];
  return POSTURE_NAMES.map(name => {
    const cls = inf.certain.includes(name) ? "pchip on" : maybe.includes(name) ? "pchip maybe" : "pchip";
    return `<span class="${cls}">${esc(name)}</span>`;
  }).join("");
}

function postureNote(p, inf){
  let read;
  if(inf.bb && inf.edge)
    read = "<b>Both VPNs up</b> — publish, deploy, and tag-bitbucket can run; tag-github still needs the firewall off. GitHub write aggregate is shown separately.";
  else if(inf.bb)
    read = "<b>Bitbucket VPN up</b> — publish and tag-bitbucket can run; deploy also needs the Edge VPN.";
  else if(inf.edge)
    read = "<b>Edge VPN up</b> — join the Bitbucket VPN before publish or deploy.";
  else
    read = "<b>No VPNs reachable</b> — baseline or firewall-off; GitHub write aggregate is shown separately. GitHub read works in every posture.";
  return `${read} <span style="color:var(--faint)">probed ${esc(p.probed_at)}</span>`;
}

// Honesty marker next to the next command: TCP for VPN phases; write aggregate for gh.
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

/* ---------- tool cards: divergence verdict + start-a-release guide ---------- */
const VERDICT_CHIP = {
  up_to_date:     ["up to date", "ok"],
  diverged:       ["release suggested", "warn"],
  checkout_stale: ["pull needed", "warn"],
  never_released: ["never released", "warn"],
  unknown:        ["no git", "dim"],
};

function sha7(sha){ return sha ? String(sha).slice(0, 7) : "?"; }

// Which way the checkout and GitHub main disagree, when git can tell.
function staleReadHtml(t){
  const commits = c => `${c} commit${c === 1 ? "" : "s"}`;
  if(t.stale_direction === "local_ahead")
    return `${commits(t.ahead_of_origin)} not on github main yet — push/PR first (verify needs green CI on HEAD)`;
  if(t.stale_direction === "local_behind")
    return `github main is ahead by ${commits(t.behind_origin)} — pull for the full picture`;
  if(t.stale_direction === "forked")
    return `checkout and github main have forked (${commits(t.ahead_of_origin)} local-only, ` +
           `${commits(t.behind_origin)} remote-only) — reconcile first`;
  return `github main differs — sync the checkout (git can't tell which side is ahead without a fetch)`;
}

function divergenceHtml(t){
  const facts = [
    t.deployed
      ? `deployed ${sha7(t.deployed.sha)} (${t.deployed.kind} · ${shortDate(t.deployed.created_at)})`
      : "deployed: no completed run",
    `checkout ${sha7(t.head)}`,
    t.origin_main ? `github main ${sha7(t.origin_main)}` : "github main unreachable",
  ];
  let read;
  if(t.verdict === "unknown")
    read = `<span>git state unavailable — is this directory a checkout?</span>`;
  else if(t.verdict === "up_to_date")
    read = `<span class="ok">deployed = checkout = github main — nothing to release</span>`;
  else if(t.verdict === "never_released")
    read = `<span class="warn">no completed release recorded — a first release would set the baseline</span>`;
  else if(t.verdict === "checkout_stale")
    read = `<span class="warn">checkout matches deployed, but github main differs — ${staleReadHtml(t)}</span>`;
  else {
    const n = t.ahead == null ? "commits" : `${t.ahead} commit${t.ahead === 1 ? "" : "s"}`;
    if(t.stale && t.stale_direction === "local_ahead"){
      // All of GitHub main is contained in HEAD, so the count is complete —
      // but part of it is unpushed, and verify needs GitHub CI on HEAD.
      read = `<span class="warn">${esc(n)} undeployed — release suggested</span>` +
             `<span class="sep2">·</span><span class="warn">${staleReadHtml(t)}</span>`;
    } else if(t.stale){
      // Lower bound: remote-only commits are uncountable without a fetch,
      // but vs-HEAD is exactly what a release would ship right now.
      read = `<span class="warn">≥ ${esc(n)} undeployed (vs checkout) — release suggested</span>` +
             `<span class="sep2">·</span><span class="warn">${staleReadHtml(t)}</span>`;
    } else {
      read = `<span class="warn">${esc(n)} undeployed — release suggested</span>`;
      if(t.ahead != null && t.ahead_exact)
        read += `<span class="sep2">·</span><span class="ok">count is live — checkout in sync with github main</span>`;
    }
  }
  return `<div class="divline">${facts.map(esc).join(`<span class="sep2">·</span>`)}</div>
          <div class="divline">${read}</div>`;
}

// The start-a-release guide (release-workflow.md distilled): shown only when
// the tool has no open run — an open run's rail already owns "what next".
function guideHtml(t){
  if(t.open_run_id)
    return `<div class="inflight">release in flight — ${esc(t.open_run_id)} (rail below)</div>`;
  const node = (t.nodes && t.nodes[0]) || "<node>";
  const suggest = ["diverged", "checkout_stale", "never_released"].includes(t.verdict);
  const steps = [];
  let n = 1;
  const step = (text, cmd, opt) => steps.push(
    `<div class="gstep"><span class="gnum">${n++}</span>` +
    (opt ? `<span class="opt">optional</span>` : "") +
    `<span class="gtext">${text}</span>` +
    (cmd ? `<code>${esc(cmd)}</code><button class="copy" data-cmd="${esc(cmd)}">copy</button>` : "") +
    `</div>`);
  if(t.stale || t.verdict === "checkout_stale"){
    if(t.stale_direction === "local_ahead")
      step(`push the local commits through review — release verify requires HEAD on github main with green CI`,
           `cd "${t.root}"; git push origin main`, false);
    else if(t.stale_direction === "forked")
      step(`reconcile — checkout and github main have forked`,
           `cd "${t.root}"; git pull --rebase origin main`, false);
    else
      step(`pull — github main ${t.stale_direction === "local_behind" ? "is ahead of" : "differs from"} the checkout`,
           `cd "${t.root}"; git pull origin main`, false);
  }
  step(`switch posture to <b>both-vpns</b> — the guided release then needs exactly one more
        switch (firewall-off, for tag-github) ${readinessHtml("both")}`, null, false);
  step(`check the node is reachable`,
       `py -m edge_deploy preflight --node ${node}`, true);
  step(`smoke the Paramiko transport before trusting the node`,
       `py -m edge_deploy transport-smoke --node ${node}`, true);
  step(`start the guided release — one command that walks every posture switch and RSA prompt`,
       `cd "${t.root}"; py -m edge_deploy release --guided`, false);
  return `<details class="guide" data-key="${esc(t.tool)}" data-suggest="${suggest ? 1 : 0}">
    <summary>start a release · ${suggest ? "suggested" : "when needed"}</summary>
    ${steps.join("")}</details>`;
}

function toolCardHtml(t){
  let [chipText, chipCls] = VERDICT_CHIP[t.verdict] || ["?", "dim"];
  if(t.verdict === "checkout_stale" && t.stale_direction === "local_ahead")
    [chipText, chipCls] = ["push needed", "warn"];
  return `<article class="toolcard">
    <div class="toolhead">
      <span class="toolname">${esc(t.tool)}</span>
      <span class="verdict ${chipCls}">${esc(chipText)}</span>
      <span class="toolroot">${esc(t.root)}</span>
    </div>
    ${divergenceHtml(t)}
    ${guideHtml(t)}
  </article>`;
}

/* ---------- run filter: tool + status toggle chips, all-on by default ---------- */
const STATUS_ORDER = ["open", "complete", "abandoned"];
const FILTER_STORAGE_KEY = "edge-console-filter-v1";

function loadFilterState(){
  try{
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return {
      excludedTools: new Set(Array.isArray(parsed.excludedTools) ? parsed.excludedTools : []),
      excludedStatuses: new Set(Array.isArray(parsed.excludedStatuses) ? parsed.excludedStatuses : []),
    };
  }catch(_e){
    return {excludedTools: new Set(), excludedStatuses: new Set()};
  }
}
function saveFilterState(){
  try{
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({
      excludedTools: [...filterState.excludedTools],
      excludedStatuses: [...filterState.excludedStatuses],
    }));
  }catch(_e){ /* private mode / storage disabled: filter still works this session */ }
}
let filterState = loadFilterState();

function passesFilter(run){
  const st = run.state;
  if(filterState.excludedStatuses.has(st.status)) return false;
  if(filterState.excludedTools.has(st.tool)) return false;
  return true;
}

function filterBarHtml(allRuns){
  if(!allRuns.length) return "";
  const tools = [...new Set(allRuns.map(r => r.state.tool))].sort();
  const countOf = (kind, value) => allRuns.filter(r =>
    kind === "tool" ? r.state.tool === value : r.state.status === value).length;
  const chip = (kind, value, label) => {
    const on = kind === "tool" ? !filterState.excludedTools.has(value) : !filterState.excludedStatuses.has(value);
    const cls = kind === "tool" ? "tool" : `status ${value}`;
    return `<button class="fchip ${cls} ${on ? "on" : "off"}" data-kind="${kind}" data-value="${esc(value)}"
      aria-pressed="${on}">${esc(label)} <span class="fcount">${countOf(kind, value)}</span></button>`;
  };
  const toolChips = tools.map(t => chip("tool", t, t)).join("");
  const statusChips = STATUS_ORDER.map(s => chip("status", s, s)).join("");
  const active = filterState.excludedTools.size || filterState.excludedStatuses.size;
  const shown = allRuns.filter(passesFilter).length;
  const clear = active
    ? `<button class="fchip clear" id="filter-clear">clear</button><span class="fcount">${shown}/${allRuns.length} shown</span>`
    : "";
  return `<span class="flabel">tool</span>${toolChips}
          <span class="flabel">status</span>${statusChips}${clear}`;
}

document.addEventListener("click", ev => {
  if(ev.target.closest("#filter-clear, #filter-clear-empty")){
    filterState.excludedTools.clear();
    filterState.excludedStatuses.clear();
    saveFilterState();
    renderRuns();
    return;
  }
  const chipBtn = ev.target.closest(".fchip[data-kind]");
  if(!chipBtn) return;
  const set = chipBtn.dataset.kind === "tool" ? filterState.excludedTools : filterState.excludedStatuses;
  if(set.has(chipBtn.dataset.value)) set.delete(chipBtn.dataset.value); else set.add(chipBtn.dataset.value);
  saveFilterState();
  renderRuns();
});

/* ---------- polling ---------- */
let runsData = null, lastRuns = "";
function renderRuns(){
  if(!runsData) return;
  const data = runsData;
  document.getElementById("rootline").innerHTML =
    `watching ${data.roots.map(esc).join(`<span style="color:var(--line)"> · </span>`)}` +
    (data.demo ? `<span class="demo-flag">demo data</span>` : "");
  document.getElementById("filterbar").innerHTML = filterBarHtml(data.runs);
  const openLogs = new Set([...document.querySelectorAll("details.log[open]")].map(d => d.dataset.key));
  const el = document.getElementById("runs");
  if(!data.runs.length){
    el.innerHTML = `<div class="empty">No runs under the watched checkouts.<br><br>
      Use a <b>start a release</b> panel above, or run
      <code>py -m edge_deploy release --guided</code> from a tool checkout.</div>`;
    return;
  }
  const filtered = data.runs.filter(passesFilter);
  if(!filtered.length){
    el.innerHTML = `<div class="empty">${data.runs.length} run${data.runs.length === 1 ? "" : "s"}
      hidden by the filter above.<br><br><button class="fchip clear" id="filter-clear-empty">clear filter</button></div>`;
    return;
  }
  el.innerHTML = filtered.map(runHtml).join("");
  for(const d of el.querySelectorAll("details.log")) if(openLogs.has(d.dataset.key)) d.open = true;
}

async function pollRuns(){
  try{
    const res = await fetch("/api/runs");
    const data = await res.json();
    const raw = JSON.stringify(data);
    runsData = data;
    if(raw === lastRuns) return;
    lastRuns = raw;
    renderRuns();
  }catch(_e){ /* server briefly gone; keep last render */ }
}

// Tool cards re-render on data change (30s cadence: git + ls-remote are
// cached server-side) and on posture change (the guide's readiness marker).
// A guide the operator opened or closed stays that way; a "suggested" guide
// opens itself only on the first render.
let toolsData = null, lastTools = "";
function renderTools(){
  const el = document.getElementById("tools");
  if(!toolsData || !toolsData.tools || !toolsData.tools.length){ el.innerHTML = ""; return; }
  const openKeys = new Set([...el.querySelectorAll("details.guide[open]")].map(d => d.dataset.key));
  const firstRender = !el.childElementCount;
  el.innerHTML = toolsData.tools.map(toolCardHtml).join("");
  for(const d of el.querySelectorAll("details.guide")){
    if(openKeys.has(d.dataset.key) || (firstRender && d.dataset.suggest === "1")) d.open = true;
  }
}
async function pollTools(){
  try{
    const res = await fetch("/api/tools");
    const data = await res.json();
    const raw = JSON.stringify(data);
    toolsData = data;
    if(raw !== lastTools){ lastTools = raw; renderTools(); }
  }catch(_e){ /* keep last render */ }
}

async function pollPosture(){
  try{
    const res = await fetch("/api/posture");
    const p = await res.json();
    const inf = inferPostures(p);
    const nextAgg = p.groups.github && p.groups.github.aggregate;
    const capsChanged = !tcpCaps || tcpCaps.bb !== inf.bb || tcpCaps.edge !== inf.edge
      || githubWriteAgg !== nextAgg;
    tcpCaps = {bb: inf.bb, edge: inf.edge};
    githubWriteAgg = nextAgg;
    document.getElementById("posture").innerHTML = postureHtml(p);
    document.getElementById("pstrip").innerHTML = pstripHtml(inf);
    document.getElementById("pnote").innerHTML = postureNote(p, inf);
    if(capsChanged){ lastRuns = ""; pollRuns(); renderTools(); }  // refresh readiness markers
  }catch(_e){ /* ignore */ }
}

// One delegated copy handler outlives every re-render.
document.addEventListener("click", async ev => {
  const b = ev.target.closest("button.copy");
  if(!b) return;
  try{ await navigator.clipboard.writeText(b.dataset.cmd); }
  catch(_e){
    const t = document.createElement("textarea");
    t.value = b.dataset.cmd; document.body.appendChild(t); t.select();
    document.execCommand("copy"); t.remove();
  }
  b.textContent = "copied"; setTimeout(() => { b.textContent = "copy"; }, 1400);
});

pollRuns(); pollPosture(); pollTools();
setInterval(pollRuns, 3000);
setInterval(pollPosture, 20000);
setInterval(pollTools, 30000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
