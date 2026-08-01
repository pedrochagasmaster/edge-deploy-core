"""Offline stand-in for ``py -m edge_deploy``, used only by ``--demo``.

It writes the same ledger files the real engine writes (into the throwaway
checkouts :mod:`edge_console.demo` fabricates), prints the same shapes of
progress, and stops at the same operator boundaries — the RSA passcode prompt
and the guided posture acknowledgement — so the console's orchestration,
streaming, and prompt relay can be exercised with no Bitbucket, Edge, SSH,
Kerberos, or RSA access.

Nothing here is a release engine. It never touches a real ledger, a network, or
a node.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Fabricated git answers for the demo checkouts, keyed by directory name:
# autobench's HEAD is the open run's source (five commits past the June
# rollback); robocop has three reviewed commits merged since its release.
# They live here, not in edge_console.demo, because this module is executed as
# a plain script from inside a demo checkout, where the package is not on the
# path — exactly like the real engine, which the console also invokes by path.
DEMO_HEAD_BY_TOOL = {
    "autobench": "9c4f2ae8d1b06f3a7c5e2d4b8a1f0c9e6d3b7a52",
    "robocop": "7fa9e21c3d5b8a0f2e4c6d8b1a3f5c7e9d2b4a6c",
}
DEMO_AHEAD_BY_TOOL = {"autobench": "5", "robocop": "3"}

PHASE_ORDER = ("verify", "publish", "deploy", "tag_bitbucket", "tag_github")
# ADR-0013, shrunk to what the simulation needs.
PHASE_CAPS = {
    "verify": frozenset({"github-read"}),
    "publish": frozenset({"bitbucket"}),
    "deploy": frozenset({"bitbucket", "edge"}),
    "tag_bitbucket": frozenset({"bitbucket"}),
    "tag_github": frozenset({"github-write"}),
}
POSTURE_CAPS = {
    "both-vpns": frozenset({"github-read", "bitbucket", "edge"}),
    "firewall-off": frozenset({"github-read", "github-write"}),
}
PHASE_POSTURE = {
    "verify": "any",
    "publish": "bitbucket-vpn or both-vpns",
    "deploy": "both-vpns",
    "tag_bitbucket": "bitbucket-vpn or both-vpns",
    "tag_github": "firewall-off",
}
DEMO_NODES = ("node03", "node04", "node05")
# Tests drive the simulator with EDGE_CONSOLE_DEMO_SPEED=0 so nothing sleeps.
SPEED = float(os.environ.get("EDGE_CONSOLE_DEMO_SPEED", "1"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _beat(seconds: float) -> None:
    time.sleep(seconds * SPEED)


def _say(message: str) -> None:
    print(message, flush=True)


def _runs_root() -> Path:
    return Path.cwd() / "edge-deploy" / "runs"


def _load(run_id: str) -> dict:
    path = _runs_root() / run_id / "state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _save(state: dict) -> None:
    path = _runs_root() / state["run_id"] / "state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _event(run_id: str, event: str, **extra: object) -> None:
    entry = {"ts": _now(), "event": event, "phase": None, "node": None, **extra}
    with (_runs_root() / run_id / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _set_phase(state: dict, phase: str, value: str, *, node: str | None = None, evidence: dict | None = None) -> None:
    record = {"state": value, "updated_at": _now(), "evidence": evidence or {}}
    if node:
        state["phases"]["deploy"][node] = record
    else:
        state["phases"][phase] = record
    _save(state)


def _progress(run_id: str, active: dict | None) -> None:
    payload = {
        "schema": "edge-deploy/release-progress/1",
        "updated_at": _now(),
        "elapsed_s": 0.0,
        "active": active,
    }
    if active is not None:
        payload["inactive_s"] = 0.0
    path = _runs_root() / run_id / "release-progress.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _lock(run_id: str, held: bool) -> None:
    path = _runs_root() / run_id / "run.lock"
    if held:
        path.write_text(
            json.dumps({"pid": 4242, "hostname": "DEMO-CONSOLE", "acquired_at": _now()}),
            encoding="utf-8",
        )
    else:
        path.unlink(missing_ok=True)


def _tool() -> str:
    try:
        text = (Path.cwd() / "edge_deploy.yaml").read_text(encoding="utf-8")
        return text.split("tool:", 1)[1].strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return Path.cwd().name


def _phase_done(state: dict, phase: str) -> bool:
    if phase == "deploy":
        return all(node["state"] == "passed" for node in state["phases"]["deploy"].values())
    return state["phases"][phase]["state"] in ("passed", "skipped")


def _open_run() -> dict | None:
    root = _runs_root()
    if not root.is_dir():
        return None
    states = []
    for entry in sorted(root.iterdir()):
        path = entry / "state.json"
        if path.is_file():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("status") == "open":
                states.append(state)
    return states[0] if states else None


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _run_verify(state: dict) -> None:
    _event(state["run_id"], "phase_entered", phase="verify")
    sha = state["source_sha"]
    _say(f"verify: checkout HEAD {sha[:7]} matches the run source")
    _beat(0.6)
    _say("verify: github main is at the same commit (github read works in every posture)")
    _beat(0.6)
    _say("verify: github CI conclusion success")
    _beat(0.7)
    _say("verify: running the committed tool gate tools/dev/local_check.ps1 (ADR-0016)")
    _beat(1.4)
    _say("verify: local_check passed — 412 tests, 0 failures")
    _set_phase(
        state,
        "verify",
        "passed",
        evidence={"commit": sha, "ci": "success", "tests": "passed", "verified_at": _now()},
    )
    _say("verify: passed")


def _run_publish(state: dict) -> None:
    _event(state["run_id"], "phase_entered", phase="publish")
    sha = state["source_sha"]
    _say("publish: reusing the run's source-bound verification evidence (ADR-0015)")
    _beat(0.7)
    _say("publish: fetching bitbucket main")
    _beat(0.9)
    _say(f"publish: fast-forwarding bitbucket main to {sha[:7]}")
    _beat(1.1)
    _set_phase(
        state,
        "publish",
        "passed",
        evidence={"snapshot_sha": sha, "source_commit": sha, "previous_remote_commit": "5f01d77"},
    )
    _say(f"publish: snapshot {sha[:7]} published")


def _authenticate(node: str, run_id: str) -> None:
    _progress(run_id, {"phase": "auth", "label": f"auth {node}", "node": node, "waiting_on": None})
    _say(f"starting: auth {node}")
    _beat(0.8)
    _say(f"[{node}] keyboard-interactive authentication requested by sshd")
    _progress(
        run_id,
        {"phase": "auth", "label": f"auth {node}", "node": node, "waiting_on": "operator"},
    )
    sys.stdout.write(f"[{node}] Enter RSA PASSCODE: ")
    sys.stdout.flush()
    code = sys.stdin.readline()
    _progress(run_id, {"phase": "auth", "label": f"auth {node}", "node": node, "waiting_on": None})
    if not code.strip():
        _say(f"[{node}] empty passcode — authentication abandoned")
        raise SystemExit(2)
    _beat(1.2)
    _say(f"completed: auth {node}")


def _transfer(run_id: str, node: str, tool: str) -> None:
    total = 44_040_192
    artifact = f"{tool} dependency bundle"
    sent = 0
    while sent < total:
        sent = min(total, sent + random.randint(3_500_000, 7_000_000))
        percent = round(sent * 100 / total, 1)
        rate = random.uniform(380_000, 520_000)
        _progress(
            run_id,
            {
                "phase": "rollout",
                "label": f"rollout {tool}/{node}",
                "tool": tool,
                "node": node,
                "waiting_on": None,
                "transfer": {
                    "artifact": artifact,
                    "bytes_sent": sent,
                    "total_bytes": total,
                    "percent": percent,
                    "bytes_per_second": round(rate, 1),
                    "updated_at": _now(),
                },
            },
        )
        _say(
            f"{artifact}: {sent / 1048576:.1f}/{total / 1048576:.1f} MiB "
            f"({percent:.1f}%) at {rate / 1048576:.2f} MiB/s"
        )
        _beat(0.45)


def _run_deploy(state: dict, nodes: list[str]) -> None:
    run_id = state["run_id"]
    tool = state["tool"]
    _event(run_id, "phase_entered", phase="deploy")
    pending = [
        node
        for node in state["phases"]["deploy"]
        if state["phases"]["deploy"][node]["state"] != "passed"
        and (not nodes or node in nodes)
    ]
    if not pending:
        _say("deploy: every requested node already passed")
        return
    _say(f"deploy: rolling {', '.join(pending)} to snapshot {state['source_sha'][:7]}")
    for node in pending:
        _authenticate(node, run_id)
        _say(f"starting: rollout {tool}/{node}")
        _beat(0.6)
        _say(f"[{node}] remote git preflight: fetch ok, target commit present")
        _beat(0.6)
        _transfer(run_id, node, tool)
        _say(f"[{node}] bundle verified on node, checking out {state['source_sha'][:7]}")
        _beat(0.9)
        _say(f"[{node}] drift passed")
        _beat(0.5)
        _say(f"[{node}] smoke passed")
        _set_phase(
            state,
            "deploy",
            "passed",
            node=node,
            evidence={
                "tool": tool,
                "node": node,
                "status": "rolled_out",
                "state_left": "",
                "deployment_commit": state["source_sha"],
                "previous_remote_commit": "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e",
                "sensitive_changed": [],
                "drift": "passed",
                "smoke": "passed",
                "report_path": f"(demo)/rollout-{tool}-{node}.json",
                "dependency": None,
            },
        )
        _say(f"completed: rollout {tool}/{node}")
    _progress(run_id, None)


def _release_tag(state: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"release-{stamp}-{state['source_sha'][:7]}"


def _run_tag(state: dict, phase: str) -> None:
    _event(state["run_id"], "phase_entered", phase=phase)
    where = "bitbucket" if phase == "tag_bitbucket" else "github"
    tag = _release_tag(state)
    _say(f"{phase}: creating annotated tag {tag}")
    _beat(0.8)
    _say(f"{phase}: pushing {tag} to {where}")
    _beat(1.0)
    _set_phase(state, phase, "passed", evidence={"tag": tag, "pushed_sha": state["source_sha"]})
    _say(f"{phase}: pushed")


def _finish_if_complete(state: dict) -> None:
    if all(_phase_done(state, phase) for phase in PHASE_ORDER):
        state["status"] = "complete"
        _save(state)
        _event(state["run_id"], "run_completed")
        _progress(state["run_id"], None)
        _say(f"release complete: {state['run_id']}")


# ---------------------------------------------------------------------------
# Guided posture boundary — the one thing that stays manual
# ---------------------------------------------------------------------------

def _posture_gate(phase: str, *, held: str) -> None:
    if PHASE_CAPS[phase] <= POSTURE_CAPS.get(held, frozenset()):
        return
    keys = PHASE_POSTURE[phase]
    _say(f"Phase '{phase}' requires posture [{keys}].")
    _say("Unreachable: github.com:443 (git-receive-pack).")
    try:
        input(f"Switch firewall posture to [{keys}], then press Enter to continue...")
    except EOFError:
        raise SystemExit(1) from None
    print("Verifying posture", end="", flush=True)
    for _ in range(3):
        _beat(0.6)
        print(".", end="", flush=True)
    print(" ok", flush=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _create_run(tool: str) -> dict:
    sha = DEMO_HEAD_BY_TOOL.get(tool, "0" * 40)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"run-{stamp}-{sha[:7]}"
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": "edge-deploy/run/1",
        "run_id": run_id,
        "tool": tool,
        "source_sha": sha,
        "operator": "pedro.chagas@mastercard.com",
        "created_at": _now(),
        "kind": "release",
        "rollback_tag": None,
        "engine": {"version": "demo", "package_dir": "(demo)", "content_sha256": "d3m0" + "0" * 60},
        "nodes": list(DEMO_NODES[:2]),
        "status": "open",
        "abandon_reason": None,
        "phases": {
            "verify": {"state": "pending", "updated_at": None, "evidence": {}},
            "publish": {"state": "pending", "updated_at": None, "evidence": {}},
            "deploy": {
                node: {"state": "pending", "updated_at": None, "evidence": {}}
                for node in DEMO_NODES[:2]
            },
            "tag_bitbucket": {"state": "pending", "updated_at": None, "evidence": {}},
            "tag_github": {"state": "pending", "updated_at": None, "evidence": {}},
        },
    }
    _save(state)
    _event(run_id, "run_created")
    _say(f"release: created run {run_id} for {tool} at {sha[:7]}")
    return state


def _cmd_release(args: dict) -> int:
    run_id = args.get("run")
    if run_id:
        state = _load(run_id)
        _say(f"release: resuming {run_id}")
    else:
        existing = _open_run()
        if existing is not None:
            state = existing
            _say(f"release: continuing the open run {state['run_id']}")
        else:
            state = _create_run(_tool())
    _lock(state["run_id"], True)
    try:
        held = "both-vpns"
        for phase in PHASE_ORDER:
            state = _load(state["run_id"])
            if _phase_done(state, phase):
                _say(f"release: {phase} already satisfied — skipping")
                continue
            _posture_gate(phase, held=held)
            if phase == "tag_github":
                held = "firewall-off"
            if phase == "verify":
                _run_verify(state)
            elif phase == "publish":
                _run_publish(state)
            elif phase == "deploy":
                _run_deploy(state, [])
            else:
                _run_tag(state, phase)
        _finish_if_complete(_load(state["run_id"]))
    finally:
        _lock(state["run_id"], False)
        _progress(state["run_id"], None)
    return 0


def _cmd_phase(command: str, args: dict) -> int:
    run_id = args["run"]
    state = _load(run_id)
    _lock(run_id, True)
    try:
        if command == "verify":
            _run_verify(state)
        elif command == "publish-phase":
            _run_publish(state)
        elif command == "deploy":
            nodes = [n for n in (args.get("nodes") or "").split(",") if n]
            _run_deploy(state, nodes)
        elif command == "tag-bitbucket":
            _run_tag(state, "tag_bitbucket")
        elif command == "tag-github":
            _run_tag(state, "tag_github")
        _finish_if_complete(_load(run_id))
    finally:
        _lock(run_id, False)
    return 0


def _cmd_abandon(args: dict) -> int:
    state = _load(args["run"])
    state["status"] = "abandoned"
    state["abandon_reason"] = args.get("reason", "no reason recorded")
    _save(state)
    _event(state["run_id"], "run_abandoned", reason=state["abandon_reason"])
    _progress(state["run_id"], None)
    _lock(state["run_id"], False)
    _say(f"abandoned {state['run_id']}: {state['abandon_reason']}")
    return 0


def _cmd_status() -> int:
    root = _runs_root()
    found = False
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        path = entry / "state.json"
        if not path.is_file():
            continue
        state = json.loads(path.read_text(encoding="utf-8"))
        found = True
        _say(
            f"run {state['run_id']}  tool={state['tool']}  kind={state['kind']}  "
            f"source={state['source_sha'][:7]}  status={state['status']}"
        )
        for phase in PHASE_ORDER:
            if phase == "deploy":
                nodes = " ".join(
                    f"{name}={info['state']}" for name, info in sorted(state["phases"]["deploy"].items())
                )
                _say(f"  deploy:        {nodes}")
            else:
                _say(f"  {phase + ':':<15}{state['phases'][phase]['state']}")
    if not found:
        _say("no runs under this checkout")
    return 0


def _cmd_preflight(node: str) -> int:
    _say(f"preflight {node}: resolving hdpedge{node[-2:]}.mastercard.int")
    _beat(0.7)
    _say(f"preflight {node}: DNS ok")
    _beat(0.5)
    _say(f"preflight {node}: tcp 2222 reachable")
    _say("status: passed")
    return 0


def _cmd_transport_smoke(node: str) -> int:
    _say(f"transport-smoke {node}: opening a Paramiko session")
    _beat(0.6)
    sys.stdout.write(f"[{node}] Enter RSA PASSCODE: ")
    sys.stdout.flush()
    if not sys.stdin.readline().strip():
        _say(f"[{node}] empty passcode — smoke abandoned")
        return 2
    _beat(1.0)
    for check in ("command", "transfer", "pty", "keepalive"):
        _say(f"[PASS] {check}: ok")
        _beat(0.4)
    _say("status: passed")
    return 0


def _cmd_git(args: list[str]) -> int:
    _say(f"$ git {' '.join(args)}")
    _beat(0.8)
    if args[:1] == ["push"]:
        _say("Everything up-to-date")
    else:
        _say("Already up to date.")
    _say("(demo: no repository was touched)")
    return 0


def _parse(args: list[str]) -> dict:
    parsed: dict = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            key = token[2:]
            if index + 1 < len(args) and not args[index + 1].startswith("--"):
                parsed[key] = args[index + 1]
                index += 2
                continue
            parsed[key] = True
        index += 1
    return parsed


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        return 2
    kind, rest = argv[0], argv[1:]
    if kind == "git":
        return _cmd_git(rest)
    if not rest:
        return 2
    command, args = rest[0], _parse(rest[1:])
    _say("[demo engine] simulated release engine — no network, node, or real ledger is touched")
    if command == "release":
        return _cmd_release(args)
    if command in ("verify", "publish-phase", "deploy", "tag-bitbucket", "tag-github"):
        return _cmd_phase(command, args)
    if command == "abandon":
        return _cmd_abandon(args)
    if command == "status":
        return _cmd_status()
    if command == "preflight":
        return _cmd_preflight(str(args.get("node", "node03")))
    if command == "transport-smoke":
        return _cmd_transport_smoke(str(args.get("node", "node03")))
    _say(f"[demo engine] no simulation for {command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
