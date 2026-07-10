"""edge_console — read-only posture console for edge-deploy runs.

A single-file, zero-dependency local web UI over the run ledger
(``edge-deploy/runs/``). It renders each Run as a rail through the five
workstation postures (ADR-0013): stations are tinted by the capability they
need (bitbucket, edge, github write), VPN joins are soft boundaries, and the
one hard wall — dropping both VPNs for firewall-off before ``tag_github`` —
is drawn as a hazard gate. Per-node deploy state, the exact next command,
and the tail of the run's event log sit under each rail.

Deliberately standalone: Engine Identity (ADR-0008) hashes every ``*.py``
inside the ``edge_deploy`` package, so UI code must live outside the engine
or every open Run would be orphaned. This file never writes to the ledger.

Usage (from the tool checkout being released, where runs live):

    python edge_console.py                 # serve http://127.0.0.1:7643
    python edge_console.py --root D:\\path  # ledger root elsewhere
    python edge_console.py --demo          # fabricated runs, no probes

Posture probes are TCP-only and labelled as such in the UI: the corporate
proxy accepts TCP connects in every posture, and TCP cannot see GitHub write
(baseline and firewall-off look identical), so only the phases' own
git-protocol probes are authoritative (ADR-0012/0013).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SCHEMA = "edge-deploy/run/1"
_EVENT_TAIL = 60
_PROBE_TIMEOUT = 1.5
_PROBE_CACHE_SECONDS = 10.0

_STATIC_GROUPS: dict[str, list[tuple[str, int]]] = {
    "github": [("github.com", 443), ("api.github.com", 443)],
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
        runs.append({"state": state, "events": _tail_events(entry), "lock": lock})
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


# ---------------------------------------------------------------------------
# TCP posture probes (informational only — see ADR-0012)
# ---------------------------------------------------------------------------

def _edge_endpoints() -> list[tuple[str, int]]:
    """Edge node endpoints from the operator config, if the engine is importable."""
    try:
        from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, load_operator_config
        from edge_deploy.preflight import endpoint_from_node

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
    def __init__(self, demo: bool) -> None:
        self._demo = demo
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def snapshot(self) -> dict:
        if self._demo:
            return {
                "probed_at": time.strftime("%H:%M:%SZ", time.gmtime()),
                "groups": {
                    "github": [
                        {"endpoint": "github.com:443", "reachable": True},
                        {"endpoint": "api.github.com:443", "reachable": True},
                    ],
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
        with ThreadPoolExecutor(max_workers=max(1, len(flat))) as pool:
            reachable = list(pool.map(lambda item: _probe_one(item[1], item[2]), flat))
        result: dict = {
            "probed_at": time.strftime("%H:%M:%SZ", time.gmtime()),
            "groups": {},
        }
        for (name, host, port), ok in zip(flat, reachable):
            result["groups"].setdefault(name, []).append(
                {"endpoint": f"{host}:{port}", "reachable": ok}
            )
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result


# ---------------------------------------------------------------------------
# Demo ledger (fabricated runs so the console renders without a real release)
# ---------------------------------------------------------------------------

def _demo_phase(state: str, at: str, evidence: dict | None = None) -> dict:
    return {"state": state, "updated_at": at, "evidence": evidence or {}}


def build_demo_ledger() -> Path:
    root = Path(tempfile.mkdtemp(prefix="edge-console-demo-")) / "edge-deploy" / "runs"
    engine = {"version": "1.4.0", "package_dir": "(demo)", "content_sha256": "d3m0" + "0" * 60}

    def write(run: dict, events: list[dict]) -> None:
        run_dir = root / run["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
        lines = "".join(json.dumps(e) + "\n" for e in events)
        (run_dir / "events.jsonl").write_text(lines, encoding="utf-8")

    # 1) Open autobench release, mid-deploy: one node failed smoke.
    sha_a = "9c4f2ae8d1b06f3a7c5e2d4b8a1f0c9e6d3b7a52"
    snap_a = "9c4f2ae8d1b06f3a7c5e2d4b8a1f0c9e6d3b7a52"
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260707T131512Z-9c4f2ae",
            "tool": "autobench",
            "source_sha": sha_a,
            "operator": "pedro.chagas",
            "created_at": "2026-07-07T13:15:12+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": engine,
            "nodes": ["03", "04", "05"],
            "status": "open",
            "abandon_reason": None,
            "phases": {
                "verify": _demo_phase("passed", "2026-07-07T13:16:02+00:00"),
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
                    "03": _demo_phase(
                        "passed",
                        "2026-07-07T13:40:19+00:00",
                        {"final_commit": snap_a, "drift": "clean", "smoke": "passed"},
                    ),
                    "04": _demo_phase(
                        "failed",
                        "2026-07-07T13:44:51+00:00",
                        {"step": "smoke", "detail": "standard smoke exited 2 (hive connectivity)"},
                    ),
                    "05": _demo_phase("pending", None),
                },
                "tag_bitbucket": _demo_phase("pending", None),
                "tag_github": _demo_phase("pending", None),
            },
        },
        [
            {"ts": "2026-07-07T13:15:12+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-07-07T13:16:02+00:00", "event": "phase_passed", "phase": "verify", "node": None},
            {"ts": "2026-07-07T13:29:10+00:00", "event": "posture_ok", "phase": "publish", "node": None},
            {"ts": "2026-07-07T13:31:44+00:00", "event": "phase_passed", "phase": "publish", "node": None},
            {"ts": "2026-07-07T13:40:19+00:00", "event": "phase_passed", "phase": "deploy", "node": "03"},
            {"ts": "2026-07-07T13:44:51+00:00", "event": "phase_failed", "phase": "deploy", "node": "04",
             "detail": "smoke exited 2"},
        ],
    )

    # 2) Open dispatch release, waiting at the first wall crossing.
    sha_b = "41d9b0c7e2f5a8d1b4c7e0f3a6d9b2c5e8f1a4d7"
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260707T140233Z-41d9b0c",
            "tool": "dispatch",
            "source_sha": sha_b,
            "operator": "pedro.chagas",
            "created_at": "2026-07-07T14:02:33+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": engine,
            "nodes": ["03", "04"],
            "status": "open",
            "abandon_reason": None,
            "phases": {
                "verify": _demo_phase("passed", "2026-07-07T14:03:20+00:00"),
                "publish": _demo_phase("pending", None),
                "deploy": {
                    "03": _demo_phase("pending", None),
                    "04": _demo_phase("pending", None),
                },
                "tag_bitbucket": _demo_phase("pending", None),
                "tag_github": _demo_phase("pending", None),
            },
        },
        [
            {"ts": "2026-07-07T14:02:33+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-07-07T14:03:20+00:00", "event": "phase_passed", "phase": "verify", "node": None},
            {"ts": "2026-07-07T14:05:41+00:00", "event": "posture_blocked", "phase": "publish", "node": None,
             "detail": "bitbucket unreachable; awaiting posture switch"},
        ],
    )

    # 3) Completed rollback.
    sha_c = "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e"
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260630T091501Z-5f01d77",
            "tool": "autobench",
            "source_sha": sha_c,
            "operator": "pedro.chagas",
            "created_at": "2026-06-30T09:15:01+00:00",
            "kind": "rollback",
            "rollback_tag": "release-20260622T110402Z-5f01d77",
            "engine": engine,
            "nodes": ["03", "04", "05"],
            "status": "complete",
            "abandon_reason": None,
            "phases": {
                "verify": _demo_phase("skipped", "2026-06-30T09:15:20+00:00"),
                "publish": _demo_phase(
                    "passed", "2026-06-30T09:22:10+00:00", {"snapshot_sha": sha_c, "source_commit": sha_c}
                ),
                "deploy": {
                    "03": _demo_phase("passed", "2026-06-30T09:30:05+00:00", {"final_commit": sha_c}),
                    "04": _demo_phase("passed", "2026-06-30T09:33:41+00:00", {"final_commit": sha_c}),
                    "05": _demo_phase("passed", "2026-06-30T09:37:12+00:00", {"final_commit": sha_c}),
                },
                "tag_bitbucket": _demo_phase("passed", "2026-06-30T09:40:02+00:00"),
                "tag_github": _demo_phase("passed", "2026-06-30T09:55:47+00:00"),
            },
        },
        [
            {"ts": "2026-06-30T09:15:01+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-06-30T09:55:47+00:00", "event": "run_completed", "phase": None, "node": None},
        ],
    )

    # 4) Abandoned run (engine identity changed under it).
    write(
        {
            "schema": _SCHEMA,
            "run_id": "run-20260629T160248Z-b82c1f0",
            "tool": "dispatch",
            "source_sha": "b82c1f04a7d3e6b9c2f5a8d1e4b7c0f3a6d9b2c5",
            "operator": "pedro.chagas",
            "created_at": "2026-06-29T16:02:48+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": {**engine, "version": "1.3.0"},
            "nodes": ["03", "04"],
            "status": "abandoned",
            "abandon_reason": "engine identity changed (1.3.0 -> 1.4.0); recreate the run",
            "phases": {
                "verify": _demo_phase("passed", "2026-06-29T16:03:30+00:00"),
                "publish": _demo_phase("pending", None),
                "deploy": {
                    "03": _demo_phase("pending", None),
                    "04": _demo_phase("pending", None),
                },
                "tag_bitbucket": _demo_phase("pending", None),
                "tag_github": _demo_phase("pending", None),
            },
        },
        [
            {"ts": "2026-06-29T16:02:48+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-06-30T08:58:12+00:00", "event": "run_abandoned", "phase": None, "node": None,
             "reason": "engine identity changed"},
        ],
    )
    return root


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "edge-console"
    runs_root: Path
    prober: PostureProber
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
                    "root": str(self.runs_root),
                    "demo": self.demo,
                    "runs": collect_runs(self.runs_root),
                }
            )
        elif self.path == "/api/posture":
            self._send_json(self.prober.snapshot())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only posture console for edge-deploy runs")
    parser.add_argument("--root", default=".", help="Checkout containing edge-deploy/runs (default: cwd)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7643)))
    parser.add_argument("--demo", action="store_true", help="Serve fabricated runs; no network probes")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.demo:
        runs_root = build_demo_ledger()
    else:
        runs_root = Path(args.root).resolve() / "edge-deploy" / "runs"

    ConsoleHandler.runs_root = runs_root
    ConsoleHandler.prober = PostureProber(demo=args.demo)
    ConsoleHandler.demo = args.demo

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ConsoleHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"edge-console: watching {runs_root}")
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
.endpoint.up{color:var(--ink)}
/* five-posture strip: the inferred current posture(s) lit, the rest dim */
.pstrip{max-width:1060px;margin:0 auto;padding:0 20px 10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pchip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;border:1px solid var(--line);border-radius:4px;padding:2.5px 9px;color:var(--faint)}
.pchip.on{color:var(--ink);border-color:var(--pass);box-shadow:inset 0 0 0 1px var(--pass)}
.pchip.maybe{color:var(--dim);border-style:dashed;border-color:var(--dim)}
.pnote{max-width:1060px;margin:0 auto;padding:0 20px 12px;font-size:11.5px;color:var(--dim)}
.pnote b{color:var(--ink);font-weight:600}

/* ---------- run cards ---------- */
.rootline{font-family:var(--mono);font-size:11px;color:var(--faint);padding:16px 0 4px}
.demo-flag{color:var(--corp);border:1px solid var(--corp);border-radius:3px;padding:0 6px;margin-left:8px;font-size:10px;letter-spacing:.1em}
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
.chip.lock{color:var(--corp);border-color:var(--corp)}
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

/* ---------- next command / details ---------- */
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
  <div id="runs"></div>
  <footer>TCP reachability is informational: the corporate proxy accepts connects in every
  posture, and TCP cannot see GitHub write (baseline vs firewall-off) — only each phase's
  git-protocol probe is authoritative (ADR-0012/0013). This console never writes to the ledger.</footer>
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
  if(phase === "verify")  return `python -m edge_deploy verify --run ${id}`;
  if(phase === "publish") return `python -m edge_deploy publish-phase --run ${id}`;
  if(phase === "deploy"){
    const pending = Object.entries(run.state.phases.deploy)
      .filter(([,n]) => n.state !== "passed").map(([name]) => name).sort();
    return `python -m edge_deploy deploy --run ${id} --nodes ${pending.join(",")}`;
  }
  if(phase === "tag_bitbucket") return `python -m edge_deploy tag-bitbucket --run ${id}`;
  if(phase === "tag_github")    return `python -m edge_deploy tag-github --run ${id}`;
  return "";
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
    const nodes = run.state.phases.deploy;
    body = `<div class="nodes">` + Object.keys(nodes).sort().map(name => {
      const n = nodes[name];
      const detail = n.state === "failed" && n.evidence && n.evidence.detail
        ? ` title="${esc(n.evidence.detail)}"` : "";
      return `<span class="state ${esc(n.state)}"${detail}><span class="dot"></span>node ${esc(name)} · ${esc(n.state)}</span>`;
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
    const cls = /failed|refused|blocked|abandoned/.test(e.event) ? "ev-failed"
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
    ${tail}
    ${eventsHtml(run)}
  </article>`;
}

/* ---------- posture panel ---------- */
const POSTURE_NAMES = ["baseline","edge-vpn","bitbucket-vpn","both-vpns","firewall-off"];

function postureHtml(p){
  const order = ["github","bitbucket","edge"];
  const cls = {github:"gh", bitbucket:"bb", edge:"edge"};
  return order.filter(g => p.groups[g]).map(g => {
    const rows = p.groups[g].map(e =>
      `<div class="endpoint ${e.reachable ? "up" : "down"}"><span class="dot"></span>${esc(e.endpoint)}</div>`
    ).join("");
    return `<div class="pgroup ${cls[g]}"><h3>${esc(g)}</h3>${rows}</div>`;
  }).join("");
}

// TCP sees the VPNs (bitbucket, edge) but not GitHub write, so baseline and
// firewall-off are indistinguishable here (ADR-0013).
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
    read = "<b>Both VPNs up</b> — publish, deploy, and tag-bitbucket can run; tag-github still needs the firewall off.";
  else if(inf.bb)
    read = "<b>Bitbucket VPN up</b> — publish and tag-bitbucket can run; deploy also needs the Edge VPN.";
  else if(inf.edge)
    read = "<b>Edge VPN up</b> — join the Bitbucket VPN before publish or deploy.";
  else
    read = "<b>No VPNs reachable</b> — baseline or firewall-off; TCP cannot see GitHub write. GitHub read works in every posture.";
  return `${read} <span style="color:var(--faint)">probed ${esc(p.probed_at)} · TCP only</span>`;
}

// Small honesty marker next to the next command: can the current posture
// (as far as TCP can see) run it?
function readinessHtml(req){
  if(!tcpCaps) return "";
  if(req === "any") return `<span class="readiness ok">runs in any posture</span>`;
  if(req === "gh")  return `<span class="readiness">tcp can't see github write</span>`;
  const ok = req === "bb" ? tcpCaps.bb : (tcpCaps.bb && tcpCaps.edge);
  return ok ? `<span class="readiness ok">posture ok (tcp)</span>`
            : `<span class="readiness blocked">switch needed</span>`;
}

/* ---------- polling ---------- */
let lastRuns = "";
async function pollRuns(){
  try{
    const res = await fetch("/api/runs");
    const data = await res.json();
    const raw = JSON.stringify(data);
    document.getElementById("rootline").innerHTML =
      `watching ${esc(data.root)}` + (data.demo ? `<span class="demo-flag">demo data</span>` : "");
    if(raw === lastRuns) return;
    lastRuns = raw;
    const openLogs = new Set([...document.querySelectorAll("details.log[open]")].map(d => d.dataset.key));
    const el = document.getElementById("runs");
    if(!data.runs.length){
      el.innerHTML = `<div class="empty">No runs under <code>${esc(data.root)}</code>.<br><br>
        Start one from the tool checkout: <code>python -m edge_deploy release --tool autobench</code></div>`;
      return;
    }
    el.innerHTML = data.runs.map(runHtml).join("");
    for(const d of el.querySelectorAll("details.log")) if(openLogs.has(d.dataset.key)) d.open = true;
    for(const b of el.querySelectorAll("button.copy")){
      b.addEventListener("click", async () => {
        try{ await navigator.clipboard.writeText(b.dataset.cmd); }
        catch(_e){
          const t = document.createElement("textarea");
          t.value = b.dataset.cmd; document.body.appendChild(t); t.select();
          document.execCommand("copy"); t.remove();
        }
        b.textContent = "copied"; setTimeout(() => { b.textContent = "copy"; }, 1400);
      });
    }
  }catch(_e){ /* server briefly gone; keep last render */ }
}

async function pollPosture(){
  try{
    const res = await fetch("/api/posture");
    const p = await res.json();
    const inf = inferPostures(p);
    const capsChanged = !tcpCaps || tcpCaps.bb !== inf.bb || tcpCaps.edge !== inf.edge;
    tcpCaps = {bb: inf.bb, edge: inf.edge};
    document.getElementById("posture").innerHTML = postureHtml(p);
    document.getElementById("pstrip").innerHTML = pstripHtml(inf);
    document.getElementById("pnote").innerHTML = postureNote(p, inf);
    if(capsChanged){ lastRuns = ""; pollRuns(); }  // refresh readiness markers
  }catch(_e){ /* ignore */ }
}

pollRuns(); pollPosture();
setInterval(pollRuns, 3000);
setInterval(pollPosture, 20000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
