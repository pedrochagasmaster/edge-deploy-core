"""edge_console: ledger reading, divergence verdicts, and demo-shape guards."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import edge_console as edge_console_mod  # noqa: E402
from edge_console import (  # noqa: E402
    PAGE,
    SCHEMA,
    PostureProber,
    ToolsProber,
    aggregate_github_write,
    build_arg_parser,
    build_demo_checkouts,
    collect_runs,
    collect_runs_multi,
    probe_divergence,
    probe_github_write,
    resolve_console_roots,
)
from edge_console.actions import (  # noqa: E402
    ACTION_SPECS,
    CAPABILITIES,
    ActionError,
    ActionRegistry,
    ActionRunner,
    display_command,
)
from edge_console.demo import DEMO_ENGINE_PATH  # noqa: E402
from edge_console.probes import _tool_name  # noqa: E402

_SCHEMA = SCHEMA
_SPEC_PARAMS = {
    "run_id": "run-20260710T000000Z-aaaaaaa",
    "nodes": ["node03"],
    "node": "node03",
    "reason": "because",
}

_FORBIDDEN_PRODUCTION_COMMANDS = (
    "py -m edge_deploy verify",
    "py -m edge_deploy publish-phase",
    "py -m edge_deploy deploy",
    "py -m edge_deploy tag-github",
    "py -m edge_deploy tag-bitbucket",
    "py -m edge_deploy release",
    "py -m edge_deploy abandon",
    "py -m edge_deploy publish",
    "release --run",
    "abandon --run",
    "tag-github --run",
    "tag-bitbucket --run",
)


def _pending_phases(nodes: list[str] | None = None) -> dict:
    nodes = nodes or ["node03"]
    pending = {"state": "pending", "updated_at": None, "evidence": {}}
    return {
        "verify": dict(pending),
        "publish": dict(pending),
        "deploy": {name: dict(pending) for name in nodes},
        "tag_bitbucket": dict(pending),
        "tag_github": dict(pending),
    }


def _write_state(
    run_dir: Path,
    run_id: str,
    *,
    tool: str = "autobench",
    status: str = "open",
    source_sha: str = "a" * 40,
    created_at: str = "2026-07-10T00:00:00+00:00",
    kind: str = "release",
    training: bool | None = None,
    phases: dict | None = None,
    operator: str = "pedro.chagas",
) -> None:
    run_dir.mkdir(parents=True)
    state = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "tool": tool,
        "source_sha": source_sha,
        "operator": operator,
        "created_at": created_at,
        "kind": kind,
        "rollback_tag": None,
        "engine": {"version": "1.4.0", "package_dir": "(test)", "content_sha256": "b" * 64},
        "nodes": ["node03"],
        "status": status,
        "abandon_reason": None,
        "phases": phases if phases is not None else {},
    }
    if training is not None:
        state["training"] = training
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _serve(
    roots: list[Path],
    *,
    write_roots: list[Path] | None = None,
    read_only: bool = False,
    registry: ActionRegistry | None = None,
    token: str = "test-token",
) -> tuple[object, int]:
    """Start a real ConsoleHandler on a loopback port; caller shuts it down."""
    handler = edge_console_mod.ConsoleHandler
    handler.roots = roots
    handler.prober = PostureProber(demo=False, roots=write_roots or roots)
    handler.tools_prober = ToolsProber(roots, demo=False)
    handler.registry = registry
    handler.demo = False
    handler.read_only = read_only
    handler.token = token
    server = edge_console_mod.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _request(port: int, method: str, path: str, body: dict | None = None, **headers) -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload, headers={"Content-Type": "application/json", **headers})
    response = conn.getresponse()
    raw = response.read().decode()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, {"raw": raw}


def _await(predicate, *, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _script_runner(script: str):
    """An argv builder that runs a throwaway python script instead of the engine."""

    def build(spec, args, cwd):
        del spec, args, cwd
        return [sys.executable, "-u", "-c", script]

    return build


def _page_script_through_run_html() -> str:
    script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    marker = "/* ---------- posture panel ---------- */"
    assert marker in script, "PAGE script lost the posture-panel marker used to extract runHtml"
    return script.split(marker, 1)[0]


def _render_run_html(run: dict, *, tcp_caps: dict | None = None) -> str:
    """Evaluate PAGE's runHtml() in Node so tests assert real card output."""
    script = _page_script_through_run_html()
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as fh:
        fh.write(script)
        if tcp_caps is not None:
            fh.write(f"\ntcpCaps = {json.dumps(tcp_caps)};\n")
        fh.write("\nprocess.stdout.write(runHtml(")
        fh.write(json.dumps(run))
        fh.write("));\n")
        path = Path(fh.name)
    try:
        completed = subprocess.run(
            ["node", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        path.unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _phases_through(last_pending: str | None) -> dict:
    """Build phase map with all phases before last_pending passed; rest pending.

    last_pending=None means every phase passed (open skew / complete).
    """
    order = ["verify", "publish", "deploy", "tag_bitbucket", "tag_github"]
    pending = {"state": "pending", "updated_at": None, "evidence": {}}
    passed = {"state": "passed", "updated_at": None, "evidence": {}}
    phases: dict = {}
    reached = False
    for name in order:
        if last_pending is not None and name == last_pending:
            reached = True
        if last_pending is None:
            entry = dict(passed)
        elif reached:
            entry = dict(pending)
        else:
            entry = dict(passed)
        if name == "deploy":
            phases[name] = {"node03": dict(entry)}
        else:
            phases[name] = entry
    return phases


def _sample_run(
    *,
    kind: str = "release",
    training: bool | None = None,
    status: str = "open",
    run_id: str = "run-20260724T000000Z-train01",
    operator: str = "trainee",
    phases: dict | None = None,
) -> dict:
    state: dict = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "tool": "autobench",
        "source_sha": "a" * 40,
        "operator": operator,
        "created_at": "2026-07-24T00:00:00+00:00",
        "kind": kind,
        "rollback_tag": None,
        "engine": {"version": "1.4.0", "package_dir": "(test)", "content_sha256": "b" * 64},
        "nodes": ["node03"],
        "status": status,
        "abandon_reason": None,
        "phases": phases if phases is not None else _pending_phases(),
    }
    if training is not None:
        state["training"] = training
    return {
        "state": state,
        "events": [],
        "lock": None,
        "progress": None,
        "root": "/tmp/training/autobench",
    }


def _fake_git(
    head: str | None = None,
    origin: str | None = None,
    ahead: str | None = None,
    behind_origin: str | None = None,
    ahead_of_origin: str | None = None,
):
    def git(root: Path, *args: str, timeout: float | None = None) -> str | None:
        del root, timeout
        if args[0] == "rev-parse":
            return head
        if args[0] == "ls-remote":
            return f"{origin}\trefs/heads/main" if origin else None
        if args[0] == "rev-list":
            if args[2] == f"HEAD..{origin}":
                return behind_origin
            if args[2] == f"{origin}..HEAD":
                return ahead_of_origin
            return ahead  # the deployed..HEAD count
        return None

    return git


# ---------------------------------------------------------------------------
# collect_runs / collect_runs_multi
# ---------------------------------------------------------------------------


def test_collect_runs_includes_progress_file_unchanged(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260710T000000Z-aaaaaaa"
    _write_state(run_dir, run_dir.name)
    progress_payload = {
        "schema": "edge-deploy/release-progress/1",
        "updated_at": "2026-07-10T00:00:05+00:00",
        "elapsed_s": 5.0,
        "active": {
            "phase": "rollout",
            "label": "rollout autobench/node03",
            "tool": "autobench",
            "node": "node03",
            "tmux_session": None,
            "last_meaningful_output_at": "2026-07-10T00:00:05+00:00",
            "waiting_on": None,
            "transfer": {
                "artifact": "autobench dependency bundle",
                "bytes_sent": 25,
                "total_bytes": 100,
                "percent": 25.0,
                "bytes_per_second": 12.5,
                "updated_at": "2026-07-10T00:00:05+00:00",
            },
        },
        "inactive_s": 0.0,
    }
    (run_dir / "release-progress.json").write_text(json.dumps(progress_payload), encoding="utf-8")

    runs = collect_runs(runs_root)

    assert len(runs) == 1
    assert runs[0]["progress"] == progress_payload


def test_collect_runs_returns_none_progress_when_file_absent(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260710T000100Z-bbbbbbb"
    _write_state(run_dir, run_dir.name)

    runs = collect_runs(runs_root)

    assert len(runs) == 1
    assert runs[0]["progress"] is None


def test_collect_runs_multi_tags_roots_and_puts_open_first(tmp_path) -> None:
    root_a = tmp_path / "autobench"
    root_b = tmp_path / "robocop"
    _write_state(
        root_a / "edge-deploy" / "runs" / "run-20260711T000000Z-ccccccc",
        "run-20260711T000000Z-ccccccc",
        status="complete",
        created_at="2026-07-11T00:00:00+00:00",
    )
    _write_state(
        root_b / "edge-deploy" / "runs" / "run-20260710T000000Z-ddddddd",
        "run-20260710T000000Z-ddddddd",
        tool="robocop",
        status="open",
        created_at="2026-07-10T00:00:00+00:00",
    )

    runs = collect_runs_multi([root_a, root_b])

    assert [r["state"]["status"] for r in runs] == ["open", "complete"]
    assert runs[0]["root"] == str(root_b)
    assert runs[1]["root"] == str(root_a)


# ---------------------------------------------------------------------------
# Divergence: deployed SHA vs checkout HEAD vs GitHub main
# ---------------------------------------------------------------------------


def _runs_with_complete(tmp_path: Path, sha: str) -> list[dict]:
    runs_root = tmp_path / "edge-deploy" / "runs"
    _write_state(
        runs_root / "run-20260709T000000Z-deploy1",
        "run-20260709T000000Z-deploy1",
        status="complete",
        source_sha=sha,
        created_at="2026-07-09T00:00:00+00:00",
    )
    return collect_runs(runs_root)


def test_divergence_up_to_date(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head=deployed, origin=deployed))
    assert result["verdict"] == "up_to_date"
    assert result["stale"] is False


def test_divergence_diverged_counts_undeployed_commits(tmp_path) -> None:
    deployed = "d" * 40
    head = "e" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head=head, origin=head, ahead="3"))
    assert result["verdict"] == "diverged"
    assert result["ahead"] == 3
    assert result["ahead_exact"] is True  # live ls-remote matches HEAD: count is exact
    assert result["stale"] is False
    assert result["deployed"]["sha"] == deployed


def test_divergence_stale_count_is_a_lower_bound(tmp_path) -> None:
    """Remote-only commits are uncountable without a fetch: when GitHub main
    has moved past the checkout, ahead stays the vs-HEAD count (what a release
    would ship right now) and is flagged as not exact."""
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path, runs, git=_fake_git(head="e" * 40, origin="f" * 40, ahead="2")
    )
    assert result["verdict"] == "diverged"
    assert result["ahead"] == 2
    assert result["ahead_exact"] is False
    assert result["stale"] is True
    assert result["stale_direction"] is None  # origin object not fetched: can't tell


def test_divergence_stale_direction_local_behind(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path,
        runs,
        git=_fake_git(
            head="e" * 40, origin="f" * 40, ahead="2",
            behind_origin="4", ahead_of_origin="0",
        ),
    )
    assert result["stale_direction"] == "local_behind"
    assert result["behind_origin"] == 4


def test_divergence_stale_direction_local_ahead_unpushed(tmp_path) -> None:
    """GitHub main is an ancestor of HEAD: local commits are unpushed, the
    vs-HEAD count is complete, and the fix is push/PR — not pull."""
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path,
        runs,
        git=_fake_git(
            head="e" * 40, origin="f" * 40, ahead="11",
            behind_origin="0", ahead_of_origin="11",
        ),
    )
    assert result["verdict"] == "diverged"
    assert result["stale_direction"] == "local_ahead"
    assert result["ahead_of_origin"] == 11


def test_divergence_stale_direction_forked(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path,
        runs,
        git=_fake_git(
            head="e" * 40, origin="f" * 40, ahead="2",
            behind_origin="3", ahead_of_origin="2",
        ),
    )
    assert result["stale_direction"] == "forked"
    assert result["behind_origin"] == 3
    assert result["ahead_of_origin"] == 2


def test_divergence_count_not_exact_when_github_unreachable(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head="e" * 40, ahead="2"))
    assert result["verdict"] == "diverged"
    assert result["ahead"] == 2
    assert result["ahead_exact"] is False  # no live proof without ls-remote
    assert result["stale"] is False


def test_divergence_checkout_stale_when_only_github_moved(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head=deployed, origin="f" * 40))
    assert result["verdict"] == "checkout_stale"
    assert result["stale"] is True


def test_divergence_never_released_without_complete_run(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    _write_state(
        runs_root / "run-20260710T000000Z-eeeeeee",
        "run-20260710T000000Z-eeeeeee",
        status="abandoned",
    )
    runs = collect_runs(runs_root)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head="e" * 40, origin="e" * 40))
    assert result["verdict"] == "never_released"
    assert result["deployed"] is None


def test_divergence_unknown_when_git_unavailable(tmp_path) -> None:
    runs = _runs_with_complete(tmp_path, "d" * 40)
    result = probe_divergence(tmp_path, runs, git=_fake_git())
    assert result["verdict"] == "unknown"
    assert result["head"] is None


def test_tool_name_prefers_profile_then_ledger_then_dirname(tmp_path) -> None:
    root = tmp_path / "some-checkout"
    runs_root = root / "edge-deploy" / "runs"
    _write_state(runs_root / "run-20260710T000000Z-fffffff", "run-20260710T000000Z-fffffff",
                 tool="robocop")
    runs = collect_runs(runs_root)

    assert _tool_name(root, runs) == "robocop"  # ledger, no profile yet

    (root / "edge_deploy.yaml").write_text('tool: "autobench"\nnodes: []\n', encoding="utf-8")
    assert _tool_name(root, runs) == "autobench"  # committed profile wins

    assert _tool_name(root, []) == "autobench"
    (root / "edge_deploy.yaml").unlink()
    assert _tool_name(root, []) == "some-checkout"  # nothing left but the directory


# ---------------------------------------------------------------------------
# Demo checkouts: must stay shaped like real ledgers and real tool cards
# ---------------------------------------------------------------------------


def test_demo_ledger_matches_engine_conventions() -> None:
    """The fabricated demo must stay shaped like a real ledger.

    Guards the drift this console already suffered once: deploy keys are
    operator-config node names, deploy evidence is the compact rollout report,
    and only event names the engine actually records appear in events.jsonl.
    """
    from edge_deploy import __version__
    from edge_deploy.ledger import _VALID_PHASE_STATES, _VALID_STATUSES

    engine_events = {
        "run_created",
        "phase_entered",
        "phase_skipped",
        "lock_stolen",
        "run_abandoned",
        "run_completed",
    }
    roots = build_demo_checkouts()
    runs = collect_runs_multi(roots)
    assert runs, "demo checkouts produced no readable runs"
    current_engine_runs = 0
    for run in runs:
        state = run["state"]
        assert state["status"] in _VALID_STATUSES
        current_engine_runs += state["engine"]["version"] == __version__
        deploy = state["phases"]["deploy"]
        assert sorted(deploy) == sorted(state["nodes"])
        for name, node_phase in deploy.items():
            assert name.startswith("node")
            assert node_phase["state"] in _VALID_PHASE_STATES
            if node_phase["state"] in ("passed", "failed"):
                evidence = node_phase["evidence"]
                assert evidence["node"] == name
                assert {"status", "state_left", "deployment_commit", "drift", "smoke"} <= set(evidence)
        for event in run["events"]:
            assert event["event"] in engine_events, event
    assert current_engine_runs >= len(runs) - 1  # one run demos an engine-identity mismatch


def test_demo_tools_show_guide_and_inflight_states() -> None:
    """The demo must exercise both tool-card states: a release in flight
    (autobench) and a diverged tool with no open run, where the console
    suggests a release (robocop)."""
    roots = build_demo_checkouts()
    snapshot = ToolsProber(roots, demo=True).snapshot()
    by_tool = {entry["tool"]: entry for entry in snapshot["tools"]}

    assert set(by_tool) == {"autobench", "robocop"}
    for entry in by_tool.values():
        assert entry["deployed"] is not None
        assert entry["nodes"], "guide needs a node name for preflight/transport-smoke"

    autobench = by_tool["autobench"]
    assert autobench["open_run_id"], "autobench demos the release-in-flight state"

    robocop = by_tool["robocop"]
    assert robocop["open_run_id"] is None, "robocop demos the start-a-release guide"
    assert robocop["verdict"] == "diverged"
    assert robocop["ahead"] == 3
    assert robocop["ahead_exact"] is True
    assert robocop["stale"] is False


# ---------------------------------------------------------------------------
# Displayed commands must match what the console actually runs
# ---------------------------------------------------------------------------


def test_next_command_has_no_cd_because_the_console_sets_the_working_directory() -> None:
    """The console runs each command with cwd set to the run's own checkout
    (ActionRegistry.resolve_root -> ActionRunner(cwd=root)), so the command it
    shows must be the command it runs: a bare 'py -m edge_deploy ...' with no
    'cd' the operator never typed. The checkout is still carried, in the action
    payload and on the card."""
    assert "function nextCommand(run, phase){" in PAGE
    body = PAGE.split("function nextCommand(run, phase){", 1)[1].split("\n}\n", 1)[0]
    assert "cd " not in body
    assert "py -m edge_deploy verify --run ${id}" in body


def test_run_action_payloads_carry_the_runs_own_checkout() -> None:
    """Every runnable action names the root it belongs to; the server refuses
    any root it does not watch, so a multi-root console can never run one
    tool's command inside another tool's checkout."""
    script = _page_script_through_run_html()
    body = script.split("function runActions(run){", 1)[1].split("\n}\n", 1)[0]
    assert "const id = st.run_id, root = run.root" in body
    for payload in ("action:\"release\", root, run_id:id", "action:\"abandon\", root, run_id:id"):
        assert payload in body, payload


def test_page_mentions_github_write_probe_not_tcp_authority() -> None:
    assert "git-receive-pack" in PAGE
    assert "git push --dry-run" not in PAGE
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


def test_probe_github_write_ok_fail_unknown(tmp_path) -> None:
    root = tmp_path / "autobench"
    root.mkdir()
    (root / ".git").mkdir()

    assert probe_github_write(root, runner=lambda cwd: 0)["status"] == "ok"
    assert probe_github_write(root, runner=lambda cwd: 128)["status"] == "fail"

    missing = tmp_path / "missing"
    assert probe_github_write(missing, runner=lambda cwd: 0)["status"] == "unknown"


def test_probe_github_write_timeout_is_unknown(tmp_path) -> None:
    root = tmp_path / "robocop"
    root.mkdir()
    (root / ".git").mkdir()

    def timed_out(cwd) -> int:
        del cwd
        return -1

    result = probe_github_write(root, runner=timed_out)
    assert result["status"] == "unknown"
    assert result["tool"] == "robocop"


def test_probe_github_write_sends_empty_authenticated_receive_pack_post(
    tmp_path, monkeypatch
) -> None:
    """Regression: a dry-run GET must not masquerade as GitHub write access."""
    root = tmp_path / "autobench"
    root.mkdir()
    (root / ".git").mkdir()
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"0000"

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    def fake_run(command, **kwargs):
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never"
        assert kwargs["timeout"] == 20.0
        if command[-3:] == ["remote", "get-url", "origin"]:
            assert Path(kwargs["cwd"]) == root
            return SimpleNamespace(
                returncode=0,
                stdout=b"https://github.com/example/autobench.git\n",
            )
        assert command == ["git", "credential", "fill"]
        assert kwargs["input"] == b"protocol=https\nhost=github.com\n\n"
        return SimpleNamespace(
            returncode=0,
            stdout=b"protocol=https\nhost=github.com\nusername=user\npassword=secret\n",
        )

    monkeypatch.setattr("edge_console.probes.subprocess.run", fake_run)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = probe_github_write(root, runner=None)

    assert result["status"] == "ok"
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.method == "POST"
    assert request.data == b"0000"
    assert request.full_url.endswith("/git-receive-pack")
    assert timeout == 20.0


def test_aggregate_github_write_rules() -> None:
    assert aggregate_github_write([]) == "unknown"
    assert aggregate_github_write([{"status": "ok"}, {"status": "ok"}]) == "ok"
    assert aggregate_github_write([{"status": "ok"}, {"status": "fail"}]) == "fail"
    assert aggregate_github_write([{"status": "ok"}, {"status": "unknown"}]) == "unknown"
    assert aggregate_github_write([{"status": "fail"}, {"status": "unknown"}]) == "fail"


def test_posture_prober_github_uses_write_probes_not_tcp(tmp_path, monkeypatch) -> None:
    auto = tmp_path / "autobench"
    robo = tmp_path / "robocop"
    for root in (auto, robo):
        root.mkdir()
        (root / ".git").mkdir()

    monkeypatch.setattr(
        "edge_console.probes.probe_github_write",
        lambda root, runner=None, timeout=20.0: {
            "tool": Path(root).name,
            "root": str(root),
            "status": "ok" if Path(root).name == "autobench" else "fail",
            "detail": "test",
        },
    )
    prober = PostureProber(demo=False, roots=[auto, robo])
    # Force TCP groups empty/fast: stub _probe_one True for bitbucket only path by
    # replacing snapshot internals via monkeypatch on ThreadPoolExecutor path.
    monkeypatch.setattr("edge_console.probes._edge_endpoints", lambda: [])
    monkeypatch.setattr("edge_console.probes._probe_one", lambda host, port: True)
    snap = prober.snapshot()
    assert snap["groups"]["github"]["aggregate"] == "fail"
    by_tool = {row["tool"]: row["status"] for row in snap["groups"]["github"]["tools"]}
    assert by_tool == {"autobench": "ok", "robocop": "fail"}
    assert isinstance(snap["groups"]["bitbucket"], list)


def test_posture_prober_github_unknown_without_roots(monkeypatch) -> None:
    monkeypatch.setattr("edge_console.probes._edge_endpoints", lambda: [])
    monkeypatch.setattr("edge_console.probes._probe_one", lambda host, port: False)
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


# ---------------------------------------------------------------------------
# Training ledger visibility (read-only; either marker)
# ---------------------------------------------------------------------------


def test_collect_runs_includes_training_ledger(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260724T000000Z-train01"
    _write_state(
        run_dir,
        run_dir.name,
        kind="training",
        status="open",
    )
    state_path = run_dir / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["training"] = True
    state_path.write_text(json.dumps(data), encoding="utf-8")
    runs = collect_runs(runs_root)
    assert len(runs) == 1
    assert runs[0]["state"]["kind"] == "training"
    assert runs[0]["state"]["training"] is True


def test_page_has_training_chip_styling() -> None:
    assert "training" in PAGE
    assert ".chip.training" in PAGE
    assert "isTrainingRun" in PAGE
    assert 'aria-label="TRAINING"' in PAGE or "aria-label='TRAINING'" in PAGE


@pytest.mark.parametrize(
    "kind,training",
    [
        ("training", True),
        ("training", None),  # kind-only legacy
        ("release", True),  # flag-only legacy
    ],
)
def test_training_card_open_labels_and_blocks_production_commands(kind, training) -> None:
    html = _render_run_html(_sample_run(kind=kind, training=training, status="open"))
    assert 'class="chip training"' in html
    assert 'aria-label="TRAINING"' in html
    assert "TRAINING ONLY" in html
    assert "TRAINING ONLY (not a production command)" in html
    for forbidden in _FORBIDDEN_PRODUCTION_COMMANDS:
        assert forbidden not in html, forbidden
    # Copy target must also be training-only when present.
    if "data-cmd=" in html:
        assert 'data-cmd="TRAINING ONLY (not a production command)"' in html
        for forbidden in _FORBIDDEN_PRODUCTION_COMMANDS:
            assert forbidden not in html


@pytest.mark.parametrize(
    "kind,training",
    [
        ("training", True),
        ("training", None),
        ("release", True),
    ],
)
def test_training_card_complete_labels_without_production_commands(kind, training) -> None:
    html = _render_run_html(
        _sample_run(
            kind=kind,
            training=training,
            status="complete",
            phases={
                "verify": {"state": "passed", "updated_at": None, "evidence": {}},
                "publish": {"state": "passed", "updated_at": None, "evidence": {}},
                "deploy": {
                    "node03": {"state": "passed", "updated_at": None, "evidence": {}}
                },
                "tag_bitbucket": {"state": "passed", "updated_at": None, "evidence": {}},
                "tag_github": {"state": "passed", "updated_at": None, "evidence": {}},
            },
        )
    )
    assert 'class="chip training"' in html
    assert 'aria-label="TRAINING"' in html
    assert "TRAINING ONLY" in html
    assert "release-tagged on GitHub and Bitbucket" not in html
    for forbidden in _FORBIDDEN_PRODUCTION_COMMANDS:
        assert forbidden not in html, forbidden


def test_training_marker_malformed_and_negative_combinations() -> None:
    # Strict true / exact kind — malformed truthy strings are not training.
    not_training = _render_run_html(_sample_run(kind="release", training=False, status="open"))
    assert 'class="chip training"' not in not_training
    assert "TRAINING ONLY" not in not_training
    assert "py -m edge_deploy verify" in not_training

    malformed = _sample_run(kind="release", training=None, status="open")
    malformed["state"]["training"] = "yes"
    html = _render_run_html(malformed)
    assert 'class="chip training"' not in html
    assert "TRAINING ONLY" not in html

    kind_only_wrong_case = _sample_run(kind="Training", training=None, status="open")
    html = _render_run_html(kind_only_wrong_case)
    assert 'class="chip training"' not in html


def test_training_card_escapes_operator_and_run_id_html() -> None:
    nasty = '<img src=x onerror=alert(1)>'
    html = _render_run_html(
        _sample_run(
            kind="training",
            training=True,
            status="open",
            run_id=f"run-{nasty}",
            operator=f"op{nasty}",
        )
    )
    assert "<img src" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert 'class="chip training"' in html


def test_real_release_card_unchanged_without_training_markers() -> None:
    html = _render_run_html(_sample_run(kind="release", training=None, status="open"))
    assert 'class="chip training"' not in html
    assert "TRAINING ONLY" not in html
    assert "py -m edge_deploy verify" in html
    # Real cards get runnable buttons, each naming the checkout the console
    # will run it in.
    assert 'class="run primary big"' in html
    assert "&quot;root&quot;:&quot;/tmp/training/autobench&quot;" in html
    assert "release-tagged on GitHub and Bitbucket" not in html  # still open

    complete = _render_run_html(
        _sample_run(
            kind="release",
            training=None,
            status="complete",
            phases={
                "verify": {"state": "passed", "updated_at": None, "evidence": {}},
                "publish": {"state": "passed", "updated_at": None, "evidence": {}},
                "deploy": {
                    "node03": {"state": "passed", "updated_at": None, "evidence": {}}
                },
                "tag_bitbucket": {"state": "passed", "updated_at": None, "evidence": {}},
                "tag_github": {"state": "passed", "updated_at": None, "evidence": {}},
            },
        )
    )
    assert "TRAINING ONLY" not in complete
    assert "release-tagged on GitHub and Bitbucket" in complete


def test_phase_timestamps_are_escaped_like_everything_else_off_disk() -> None:
    """Regression: shortTs returns its input unchanged when the timestamp is
    not the shape it expects, so an unescaped phase updated_at was a script
    injection into a page that holds the console's action token."""
    nasty = "<img src=x onerror=alert(1)>"
    phases = _pending_phases()
    phases["verify"] = {"state": "passed", "updated_at": nasty, "evidence": {}}
    html = _render_run_html(
        _sample_run(kind="release", training=None, status="open", phases=phases)
    )
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_release_cta_is_blocked_whenever_the_checkout_is_not_github_main() -> None:
    """inspect_repository refuses unless HEAD == origin/main, before a run is
    even created — so every stale direction blocks, not only the two that also
    lack CI."""
    script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    body = script.split("function releaseBlocker(t){", 1)[1].split("\n}\n", 1)[0]
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as fh:
        fh.write(f"function releaseBlocker(t){{{body}\n}}\n")
        fh.write(
            "const cases = ["
            '  {verdict:"diverged", stale:true, stale_direction:"local_behind"},'
            '  {verdict:"checkout_stale", stale:true, stale_direction:"local_behind"},'
            '  {verdict:"diverged", stale:true, stale_direction:"local_ahead", ahead_of_origin:2},'
            '  {verdict:"diverged", stale:true, stale_direction:"forked"},'
            '  {verdict:"checkout_stale", stale:true, stale_direction:null},'
            '  {verdict:"up_to_date", stale:false},'
            '  {verdict:"unknown", stale:false},'
            "];\n"
            "const open = {verdict:'diverged', stale:false, stale_direction:null};\n"
            "process.stdout.write(JSON.stringify({"
            "blocked: cases.map(c => releaseBlocker(c) !== null),"
            "open: releaseBlocker(open)}));\n"
        )
        path = Path(fh.name)
    try:
        completed = subprocess.run(["node", str(path)], check=False, capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert all(result["blocked"]), result["blocked"]
    # A clean checkout that is genuinely ahead of the nodes must still release.
    assert result["open"] is None


def test_console_never_writes_a_ledger_or_bypasses_the_engine() -> None:
    """ADR-0018: the console orchestrates by running engine commands. It must
    never edit a ledger itself, and must never reach into edge_deploy's release
    internals — only edge_deploy.config/preflight, for probe endpoints."""
    package = Path(__file__).resolve().parents[1] / "edge_console"
    # demo.py and demo_engine.py are the offline simulator: they fabricate and
    # then advance throwaway ledgers in a temp directory, which is the point.
    simulator = {"demo.py", "demo_engine.py"}
    engine_imports: set[str] = set()
    for source_file in sorted(package.rglob("*.py")):
        source = source_file.read_text(encoding="utf-8")
        if source_file.name not in simulator:
            for forbidden in ("state.json", "events.jsonl", "run.lock"):
                for line in source.splitlines():
                    if forbidden in line:
                        assert "write" not in line and "open(" not in line, (
                            f"{source_file.name} looks like it writes {forbidden}: {line.strip()}"
                        )
        engine_imports.update(re.findall(r"from (edge_deploy[.\w]*) import", source))
        engine_imports.update(re.findall(r"^import (edge_deploy[.\w]*)", source, re.M))
    assert engine_imports <= {
        "edge_deploy",
        "edge_deploy.config",
        "edge_deploy.preflight",
    }, engine_imports


def test_every_console_action_is_an_allowlisted_engine_or_git_command() -> None:
    """The only mutation surface is ACTION_SPECS: each entry becomes a fixed
    argv list, never a shell string, and never a command of the console's own
    invention."""
    params = {
        "run_id": "run-20260710T000000Z-aaaaaaa",
        "nodes": ["node03"],
        "node": "node03",
        "reason": "because",
    }
    for spec in ACTION_SPECS.values():
        args = spec.args(params)
        assert isinstance(args, list) and all(isinstance(a, str) for a in args)
        shown = display_command(spec, args)
        if spec.kind == "git":
            assert shown.startswith("git ")
            assert args[0] in {"pull", "push"}
        else:
            assert shown.startswith("py -m edge_deploy ")
            assert args[0] in {
                "verify",
                "publish-phase",
                "deploy",
                "tag-bitbucket",
                "tag-github",
                "release",
                "abandon",
                "status",
                "preflight",
                "transport-smoke",
            }
        assert spec.cap in CAPABILITIES


def test_page_and_allowlist_do_not_drift_apart() -> None:
    """The buttons are written in the page and the commands in ACTION_SPECS.
    Nothing else stops a renamed action from shipping with a dead button, or a
    button from POSTing an id the server refuses."""
    posted = set(re.findall(r'action:"([a-z_]+)"', PAGE))
    posted.update(re.findall(r'\{action:"([a-z_]+)"', PAGE))
    assert posted, "no action payloads found in the page"
    assert posted <= set(ACTION_SPECS), posted - set(ACTION_SPECS)
    # Every capability an action declares must have page wording for it.
    postures = set(re.findall(r"^\s*(\w+):\s*\"[^\"]+\",?$", PAGE.split("const REQ_POSTURE = {", 1)[1]
                              .split("};", 1)[0], re.M))
    assert {spec.cap for spec in ACTION_SPECS.values()} <= postures


def test_action_capabilities_match_the_engines_phase_posture_map() -> None:
    """A console that mislabels the posture a command needs sends the operator
    to change the firewall for no reason, or lets them think they are ready."""
    from edge_deploy.posture import PHASE_CAPABILITIES

    cap_for = {
        frozenset({"github-read"}): "any",
        frozenset({"bitbucket"}): "bb",
        frozenset({"bitbucket", "edge"}): "both",
        frozenset({"github-write"}): "gh",
    }
    for phase, action in (
        ("verify", "verify"),
        ("publish", "publish"),
        ("deploy", "deploy"),
        ("tag_bitbucket", "tag_bitbucket"),
        ("tag_github", "tag_github"),
    ):
        assert ACTION_SPECS[action].cap == cap_for[PHASE_CAPABILITIES[phase]], action
    # preflight/transport-smoke are not posture-gated by the engine and only
    # talk to the node, so they need the Edge VPN — not Bitbucket as well.
    assert ACTION_SPECS["preflight"].cap == "edge"
    assert ACTION_SPECS["transport_smoke"].cap == "edge"


def test_no_action_can_switch_the_workstation_posture() -> None:
    """ADR-0013: posture stays manual. The console may only wait for the
    operator to confirm a switch it did not make."""
    for spec in ACTION_SPECS.values():
        rendered = " ".join(spec.args(_SPEC_PARAMS)).lower()
        assert "vpn" not in rendered
        assert "firewall" not in rendered
        assert "netsh" not in rendered and "rasdial" not in rendered


def test_training_rail_is_simulated_without_live_hot_or_firewall_now_cues() -> None:
    """Educational rail stays, but never cues a live posture switch."""
    # Waiting on tag_github would normally hot the firewall-off gate.
    at_github = _render_run_html(
        _sample_run(
            kind="training",
            training=True,
            status="open",
            phases=_phases_through("tag_github"),
        ),
        tcp_caps={"bb": False, "edge": False},
    )
    assert 'class="rail simulated"' in at_github
    assert "TRAINING ONLY" in at_github
    assert "simulated" in at_github.lower()
    assert "firewall off" in at_github  # educational rail label remains
    assert " hot" not in at_github
    assert 'class="gate hot"' not in at_github
    assert 'class="sep hot"' not in at_github
    assert 'class="station next"' not in at_github
    assert "switch needed" not in at_github
    # Live do-it-now firewall aria copy must not appear on training rails.
    assert "drop VPNs, firewall off" not in at_github
    assert "needs firewall-off" not in at_github

    # Waiting on deploy would normally hot the + edge vpn sep when edge is down.
    at_deploy = _render_run_html(
        _sample_run(
            kind="training",
            training=True,
            status="open",
            phases=_phases_through("deploy"),
        ),
        tcp_caps={"bb": True, "edge": False},
    )
    assert 'class="rail simulated"' in at_deploy
    assert " hot" not in at_deploy
    assert 'class="station next"' not in at_deploy
    assert "join + edge vpn" not in at_deploy  # live join aria suppressed

    # Real release at the same point still gets the live firewall-off hot cue.
    real = _render_run_html(
        _sample_run(
            kind="release",
            training=None,
            status="open",
            phases=_phases_through("tag_github"),
        ),
        tcp_caps={"bb": False, "edge": False},
    )
    assert 'class="gate hot"' in real
    assert 'class="station next"' in real
    assert 'class="rail simulated"' not in real
    assert "drop VPNs, firewall off" in real


def test_open_training_all_phases_passed_awaits_completion_not_finished() -> None:
    html = _render_run_html(
        _sample_run(
            kind="training",
            training=True,
            status="open",
            phases=_phases_through(None),
        )
    )
    assert 'class="chip training"' in html
    assert "awaits completion" in html.lower()
    assert "practice finished" not in html.lower()
    assert "release-tagged on GitHub and Bitbucket" not in html
    # Must not claim the run is complete while status is still open.
    assert "complete —" not in html
    assert "TRAINING ONLY" in html
    for forbidden in _FORBIDDEN_PRODUCTION_COMMANDS:
        assert forbidden not in html, forbidden


# ---------------------------------------------------------------------------
# Final-review: --github-write-root separation
# ---------------------------------------------------------------------------


def test_build_arg_parser_accepts_github_write_root() -> None:
    args = build_arg_parser().parse_args(
        ["--root", "/training/ab", "--github-write-root", "/real/ab"]
    )
    assert args.root == ["/training/ab"]
    assert args.github_write_root == ["/real/ab"]


def test_resolve_console_roots_defaults_write_roots_to_ledger_roots(tmp_path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    args = build_arg_parser().parse_args(["--root", str(ledger)])
    ledger_roots, write_roots = resolve_console_roots(args)
    assert ledger_roots == [ledger.resolve()]
    assert write_roots == [ledger.resolve()]


def test_resolve_console_roots_separates_write_roots(tmp_path) -> None:
    training = tmp_path / "training"
    real = tmp_path / "real"
    training.mkdir()
    real.mkdir()
    args = build_arg_parser().parse_args(
        ["--root", str(training), "--github-write-root", str(real)]
    )
    ledger_roots, write_roots = resolve_console_roots(args)
    assert ledger_roots == [training.resolve()]
    assert write_roots == [real.resolve()]


def test_posture_prober_uses_write_roots_not_ledger_roots(tmp_path, monkeypatch) -> None:
    training = tmp_path / "training" / "autobench"
    real = tmp_path / "autobench"
    training.mkdir(parents=True)
    real.mkdir()
    (real / ".git").mkdir()
    # Training has no .git — intentional divergence unknown if probed.
    del training
    probed: list[str] = []

    def fake_probe(root, runner=None, timeout=20.0):
        probed.append(str(root))
        return {
            "tool": Path(root).name,
            "root": str(root),
            "status": "fail",
            "detail": "probe",
        }

    monkeypatch.setattr("edge_console.probes.probe_github_write", fake_probe)
    monkeypatch.setattr("edge_console.probes._edge_endpoints", lambda: [])
    monkeypatch.setattr("edge_console.probes._probe_one", lambda host, port: True)
    snap = PostureProber(demo=False, roots=[real]).snapshot()
    assert probed == [str(real)]
    assert snap["groups"]["github"]["aggregate"] == "fail"


def test_api_training_ledger_with_real_write_roots(tmp_path, monkeypatch) -> None:
    """Training --root can render runs while write aggregate uses real roots."""
    training = tmp_path / "training" / "autobench"
    real = tmp_path / "autobench"
    runs = training / "edge-deploy" / "runs" / "run-train"
    _write_state(runs, "run-train", kind="training", training=True, status="open")
    real.mkdir()
    (real / ".git").mkdir()

    monkeypatch.setattr(
        "edge_console.probes.probe_github_write",
        lambda root, runner=None, timeout=20.0: {
            "tool": Path(root).name,
            "root": str(root),
            "status": "fail",
            "detail": "real write fail",
        },
    )
    monkeypatch.setattr("edge_console.probes._edge_endpoints", lambda: [])
    monkeypatch.setattr("edge_console.probes._probe_one", lambda host, port: False)

    server, port = _serve([training], write_roots=[real])
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/runs")
        runs_payload = json.loads(conn.getresponse().read().decode())
        assert runs_payload["runs"]
        assert runs_payload["runs"][0]["state"]["kind"] == "training"

        conn.request("GET", "/api/posture")
        posture = json.loads(conn.getresponse().read().decode())
        assert posture["groups"]["github"]["aggregate"] == "fail"
        assert posture["groups"]["github"]["tools"][0]["status"] == "fail"
        # fail beats unknown: mix fail+unknown → fail
        assert (
            aggregate_github_write([{"status": "fail"}, {"status": "unknown"}]) == "fail"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_module_doc_does_not_claim_zero_dependency() -> None:
    doc = (edge_console_mod.__doc__ or "").lower()
    assert "zero-dependency" not in doc
    assert "zero-external-dependency" not in doc


# ---------------------------------------------------------------------------
# ADR-0018: the console runs the commands it used to ask the operator to copy
# ---------------------------------------------------------------------------


def _checkout(tmp_path: Path, tool: str = "autobench") -> Path:
    root = tmp_path / tool
    (root / "edge-deploy" / "runs").mkdir(parents=True)
    (root / ".git").mkdir()
    return root


def _registry(roots: list[Path], script: str | None = None) -> ActionRegistry:
    return ActionRegistry(
        roots=roots,
        argv_builder=_script_runner(script) if script else None,
    )


def test_registry_refuses_unknown_actions_and_unwatched_roots(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry([root])

    with pytest.raises(ActionError) as unknown:
        registry.start({"action": "rm -rf /", "root": str(root)})
    assert unknown.value.status == 404

    with pytest.raises(ActionError) as elsewhere:
        registry.start({"action": "status", "root": str(tmp_path)})
    assert elsewhere.value.status == 403


def test_registry_refuses_run_ids_that_are_not_runs(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry([root])

    for bogus in ("../../etc/passwd", "run; rm -rf /", "run id", ""):
        with pytest.raises(ActionError):
            registry.start({"action": "verify", "root": str(root), "run_id": bogus})

    # Well-formed but absent is a 404, not a command.
    with pytest.raises(ActionError) as missing:
        registry.start(
            {"action": "verify", "root": str(root), "run_id": "run-20260710T000000Z-aaaaaaa"}
        )
    assert missing.value.status == 404


def test_registry_refuses_production_commands_against_a_training_ledger(tmp_path) -> None:
    """ADR-0017: training ledgers are practice-only. Refusing in the UI is not
    enough — the server refuses too."""
    root = _checkout(tmp_path)
    _write_state(
        root / "edge-deploy" / "runs" / "run-train",
        "run-train",
        kind="training",
        training=True,
        phases=_pending_phases(),
    )
    registry = _registry([root])
    with pytest.raises(ActionError) as refused:
        registry.start({"action": "verify", "root": str(root), "run_id": "run-train"})
    assert refused.value.status == 403
    assert "training" in str(refused.value)


def test_registry_validates_node_names_and_abandon_reasons(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry([root])
    with pytest.raises(ActionError):
        registry.start({"action": "preflight", "root": str(root), "node": "node03; rm -rf /"})
    _write_state(
        root / "edge-deploy" / "runs" / "run-20260710T000000Z-aaaaaaa",
        "run-20260710T000000Z-aaaaaaa",
        phases=_pending_phases(),
    )
    with pytest.raises(ActionError):
        registry.start(
            {
                "action": "abandon",
                "root": str(root),
                "run_id": "run-20260710T000000Z-aaaaaaa",
                "reason": "   ",
            }
        )


def test_registry_runs_one_command_per_checkout_at_a_time(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry([root], script="import time; time.sleep(5)")
    first = registry.start({"action": "status", "root": str(root)})
    try:
        with pytest.raises(ActionError) as busy:
            registry.start({"action": "status", "root": str(root)})
        assert busy.value.status == 409
    finally:
        first.cancel()


def test_runner_streams_output_and_reports_the_exit_code(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry([root], script="print('hello from the engine'); raise SystemExit(3)")
    runner = registry.start({"action": "status", "root": str(root)})
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    payload = runner.output(0)
    assert "hello from the engine" in payload["text"]
    assert "py -m edge_deploy status" in payload["text"]  # the command is shown verbatim
    assert payload["exit_code"] == 3


def test_runner_surfaces_the_rsa_prompt_and_never_records_the_passcode(tmp_path) -> None:
    """The engine's RSA prompt (edge_deploy.auth) is written without a newline;
    the console must notice it, relay the answer to stdin, and keep the code out
    of the transcript."""
    root = _checkout(tmp_path)
    script = (
        "import sys\n"
        "sys.stdout.write('[node05] Enter RSA PASSCODE: '); sys.stdout.flush()\n"
        "code = sys.stdin.readline().strip()\n"
        "print('authenticated' if code == '8675309' else 'rejected')\n"
    )
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})

    prompt = _await(lambda: runner.snapshot()["prompt"], what="the RSA prompt")
    assert prompt["kind"] == "secret"
    assert prompt["name"] == "rsa"
    assert "node05" in prompt["title"]

    runner.answer(prompt["id"], "8675309")
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    transcript = runner.output(0)["text"]
    assert "authenticated" in transcript
    assert "8675309" not in transcript
    assert "********" in transcript


def test_runner_surfaces_the_guided_posture_boundary_as_an_acknowledgement(tmp_path) -> None:
    """Posture switching stays manual: the console shows the boundary and only
    forwards the operator's confirmation."""
    root = _checkout(tmp_path)
    script = (
        "input('Switch firewall posture to [firewall-off], then press Enter to continue...')\n"
        "print('posture confirmed')\n"
    )
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})

    prompt = _await(lambda: runner.snapshot()["prompt"], what="the posture prompt")
    assert prompt["kind"] == "ack"
    assert prompt["name"] == "posture"
    assert "firewall-off" in prompt["title"]

    runner.answer(prompt["id"], "")
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    assert "posture confirmed" in runner.output(0)["text"]


def test_runner_offers_an_answer_box_for_a_prompt_it_does_not_recognise(tmp_path) -> None:
    """An unrecognised question must never silently hang the run."""
    root = _checkout(tmp_path)
    script = "import sys; sys.stdout.write('Which node should I skip? '); sys.stdout.flush(); sys.stdin.readline()"
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})

    def poll():
        runner.tick()
        return runner.snapshot()["prompt"]

    prompt = _await(poll, timeout=25.0, what="the generic prompt")
    assert prompt["kind"] == "text"
    assert prompt["name"] == "unknown"
    assert "Which node should I skip?" in prompt["title"]
    runner.answer(prompt["id"], "node04")
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")


def test_an_empty_secret_is_not_transcribed_as_a_passcode(tmp_path) -> None:
    """The engine reads an empty answer as "no code given". The transcript must
    not show asterisks as though one was typed, and the page keeps Send off
    until the field has something in it."""
    root = _checkout(tmp_path)
    script = (
        "import sys\n"
        "sys.stdout.write('[node03] Enter RSA PASSCODE: '); sys.stdout.flush()\n"
        "print('got: ' + repr(sys.stdin.readline()))\n"
    )
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})
    prompt = _await(lambda: runner.snapshot()["prompt"], what="the RSA prompt")
    runner.answer(prompt["id"], "")
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    assert "********" not in runner.output(0)["text"]

    assert 'data-answer="secret" disabled' in PAGE
    assert 'data-answer="text" disabled' in PAGE


def test_page_keeps_a_half_typed_answer_focused_across_re_renders() -> None:
    """The stage re-renders whenever release-progress.json moves, which is
    exactly while a passcode is being typed. Re-parenting the terminal must not
    silently drop the caret out of the field."""
    body = PAGE.split("function placeTerminals(){", 1)[1].split("\n}\n", 1)[0]
    assert "document.activeElement" in body
    assert "selectionStart" in body
    assert "setSelectionRange" in body
    assert "scrollTop" in body


def test_a_pending_prompt_does_not_satisfy_every_long_poll(tmp_path) -> None:
    """Regression: the poll used to return instantly for as long as a prompt
    was open, so the page spun through the whole time the operator spent
    finding their RSA token. A prompt the caller has already seen must not wake
    the poll; one it has not seen still must."""
    root = _checkout(tmp_path)
    script = (
        "import sys\n"
        "sys.stdout.write('[node05] Enter RSA PASSCODE: '); sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
    )
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})
    prompt = _await(lambda: runner.snapshot()["prompt"], what="the RSA prompt")
    cursor = runner.output(0)["cursor"]

    unseen = time.monotonic()
    runner.output(cursor, wait=5.0)
    assert time.monotonic() - unseen < 1.0, "an unseen prompt must return at once"

    seen = time.monotonic()
    runner.output(cursor, wait=1.0, seen_prompt=prompt["id"])
    assert time.monotonic() - seen >= 0.9, "a seen prompt must not wake the poll"
    runner.cancel()


def test_an_unrecognised_prompts_answer_is_masked_like_a_secret(tmp_path) -> None:
    """The generic answer box exists because the console cannot know every
    prompt — which means it cannot know the answer is not a password."""
    root = _checkout(tmp_path)
    script = "import sys; sys.stdout.write('vault passphrase: '); sys.stdout.flush(); sys.stdin.readline()"
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})

    def poll():
        runner.tick()
        return runner.snapshot()["prompt"]

    prompt = _await(poll, timeout=25.0, what="the generic prompt")
    assert prompt["kind"] == "text"
    runner.answer(prompt["id"], "correct-horse-battery")
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    assert "correct-horse-battery" not in runner.output(0)["text"]


def test_short_secrets_are_masked_too(tmp_path) -> None:
    root = _checkout(tmp_path)
    script = (
        "import sys\n"
        "sys.stdout.write('[node03] Enter RSA PASSCODE: '); sys.stdout.flush()\n"
        "code = sys.stdin.readline().strip()\n"
        "print('sshd echoed ' + code)\n"
    )
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})
    prompt = _await(lambda: runner.snapshot()["prompt"], what="the RSA prompt")
    runner.answer(prompt["id"], "42")
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    transcript = runner.output(0)["text"]
    assert "sshd echoed ********" in transcript
    assert "sshd echoed 42" not in transcript


def test_an_answer_cannot_smuggle_extra_stdin_lines(tmp_path) -> None:
    """One answer is one line: a newline would pre-answer the next question."""
    root = _checkout(tmp_path)
    script = "import sys; sys.stdout.write('[node03] Enter RSA PASSCODE: '); sys.stdout.flush(); sys.stdin.readline()"
    registry = _registry([root], script=script)
    runner = registry.start({"action": "status", "root": str(root)})
    prompt = _await(lambda: runner.snapshot()["prompt"], what="the RSA prompt")
    with pytest.raises(ActionError):
        runner.answer(prompt["id"], "1234\ny")
    runner.cancel()


def test_kerberos_and_yes_no_prompts_are_recognised(tmp_path) -> None:
    root = _checkout(tmp_path)
    for script, kind, name in (
        (
            "import sys; sys.stdout.write('[node03] Kerberos password: '); "
            "sys.stdout.flush(); sys.stdin.readline()",
            "secret",
            "kerberos",
        ),
        (
            "import sys; sys.stdout.write('Discard onboarding evidence only? [y/N] '); "
            "sys.stdout.flush(); sys.stdin.readline()",
            "choice",
            "confirm",
        ),
    ):
        registry = _registry([root], script=script)
        runner = registry.start({"action": "status", "root": str(root)})
        prompt = _await(lambda: runner.snapshot()["prompt"], what=f"the {name} prompt")
        assert (prompt["kind"], prompt["name"]) == (kind, name)
        runner.answer(prompt["id"], "y" if kind == "choice" else "secret")
        _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")


def test_a_second_command_is_refused_while_the_first_is_still_spawning(tmp_path) -> None:
    """The runner is registered before Popen returns; that window must still
    count as busy or the one-command-per-checkout rule has a hole in it."""
    root = _checkout(tmp_path)
    registry = _registry([root], script="import time; time.sleep(5)")
    first = registry.start({"action": "status", "root": str(root)})
    try:
        first.status = "starting"  # re-enter the spawn window deterministically
        with pytest.raises(ActionError) as busy:
            registry.start({"action": "status", "root": str(root)})
        assert busy.value.status == 409
    finally:
        first.status = "running"
        first.cancel()


def test_cancel_before_the_process_exists_is_not_lost(tmp_path) -> None:
    """Stop is clickable as soon as the action is listed, which is before Popen
    has returned. Dropping that cancel would leave the operator believing they
    stopped a command that is in fact still running."""
    root = _checkout(tmp_path)
    runner = ActionRunner(
        action_id="cancel-window",
        spec=ACTION_SPECS["status"],
        argv=[sys.executable, "-u", "-c", "import time; time.sleep(30)"],
        cwd=root,
        command="py -m edge_deploy status",
        root=str(root),
        run_id=None,
        tool=None,
    )
    runner.cancel()
    runner.start()
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to stop")
    assert "cancelled by operator" in runner.output(0)["text"]


def test_answering_a_stale_prompt_is_refused(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry([root], script="print('done')")
    runner = registry.start({"action": "status", "root": str(root)})
    _await(lambda: runner.snapshot()["status"] == "exited", what="the command to exit")
    with pytest.raises(ActionError) as stale:
        runner.answer("nope-p1", "value")
    assert stale.value.status == 409


def test_output_long_poll_returns_new_text_from_a_cursor(tmp_path) -> None:
    root = _checkout(tmp_path)
    registry = _registry(
        [root], script="import time; print('first'); time.sleep(0.4); print('second')"
    )
    runner = registry.start({"action": "status", "root": str(root)})
    first = runner.output(0, wait=5.0)
    assert first["text"]
    second = runner.output(first["cursor"], wait=5.0)
    assert second["text"] not in ("", first["text"])
    assert second["cursor"] >= first["cursor"]


# ---------------------------------------------------------------------------
# HTTP: only this page, and only when it is allowed to act
# ---------------------------------------------------------------------------


def test_actions_require_the_page_token(tmp_path) -> None:
    root = _checkout(tmp_path)
    server, port = _serve([root], registry=_registry([root], script="print('ok')"))
    try:
        status, body = _request(port, "POST", "/api/actions", {"action": "status", "root": str(root)})
        assert status == 403
        assert "token" in body["error"]

        status, _ = _request(
            port,
            "POST",
            "/api/actions",
            {"action": "status", "root": str(root)},
            **{"X-Edge-Console-Token": "test-token"},
        )
        assert status == 202
    finally:
        server.shutdown()
        server.server_close()


def test_read_only_console_refuses_every_action(tmp_path) -> None:
    root = _checkout(tmp_path)
    server, port = _serve([root], read_only=True)
    try:
        status, payload = _request(port, "GET", "/api/runs")
        assert payload["read_only"] is True
        status, body = _request(
            port,
            "POST",
            "/api/actions",
            {"action": "status", "root": str(root)},
            **{"X-Edge-Console-Token": "test-token"},
        )
        assert status == 403
        assert "read-only" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_answer_and_cancel_also_require_the_page_token(tmp_path) -> None:
    root = _checkout(tmp_path)
    server, port = _serve([root], registry=_registry([root], script="import time; time.sleep(5)"))
    auth = {"X-Edge-Console-Token": "test-token"}
    try:
        _, started = _request(
            port, "POST", "/api/actions", {"action": "status", "root": str(root)}, **auth
        )
        for path, body in (
            (f"/api/actions/{started['id']}/answer", {"prompt_id": "x", "value": "y"}),
            (f"/api/actions/{started['id']}/cancel", {}),
        ):
            status, payload = _request(port, "POST", path, body)
            assert status == 403, path
            assert "token" in payload["error"]
        status, _ = _request(port, "POST", f"/api/actions/{started['id']}/cancel", {}, **auth)
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_non_loopback_host_is_refused(tmp_path) -> None:
    """The DNS-rebinding guard: a valid token from a foreign origin is not enough."""
    root = _checkout(tmp_path)
    server, port = _serve([root], registry=_registry([root], script="print('ok')"))
    try:
        status, payload = _request(
            port,
            "POST",
            "/api/actions",
            {"action": "status", "root": str(root)},
            **{"X-Edge-Console-Token": "test-token", "Host": "attacker.example.com"},
        )
        assert status == 403
        assert "Host" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_rejected_post_does_not_desync_the_connection(tmp_path) -> None:
    """Regression: an unread request body was parsed as the next request line,
    so the poll right after a stale-token click got a nonsense response."""
    root = _checkout(tmp_path)
    server, port = _serve([root], registry=_registry([root], script="print('ok')"))
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"action": "status", "root": str(root)}).encode()
        conn.request("POST", "/api/actions", body=body, headers={"Content-Type": "application/json"})
        rejected = conn.getresponse()
        assert rejected.status == 403
        rejected.read()
        conn.request("GET", "/api/runs")
        response = conn.getresponse()
        assert response.status == 200, "the next request on the same connection must be understood"
        assert json.loads(response.read().decode())["runs"] == []
    finally:
        server.shutdown()
        server.server_close()


def test_oversized_action_bodies_are_refused(tmp_path) -> None:
    root = _checkout(tmp_path)
    server, port = _serve([root], registry=_registry([root], script="print('ok')"))
    try:
        status, payload = _request(
            port,
            "POST",
            "/api/actions",
            {"action": "status", "root": str(root), "reason": "x" * 70_000},
            **{"X-Edge-Console-Token": "test-token"},
        )
        assert status == 413
        assert "too large" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_release_commands_are_refused_outside_a_git_checkout(tmp_path) -> None:
    """Training workspaces are deliberately not git checkouts (ADR-0017)."""
    training = tmp_path / "training" / "autobench"
    (training / "edge-deploy" / "runs").mkdir(parents=True)
    server, port = _serve([training], registry=_registry([training], script="print('ok')"))
    try:
        status, body = _request(
            port,
            "POST",
            "/api/actions",
            {"action": "release", "root": str(training)},
            **{"X-Edge-Console-Token": "test-token"},
        )
        assert status == 403
        assert "git checkout" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_http_action_round_trip_streams_output_and_relays_a_secret(tmp_path) -> None:
    root = _checkout(tmp_path)
    script = (
        "import sys\n"
        "sys.stdout.write('[node03] Enter RSA PASSCODE: '); sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
        "print('rolled out')\n"
    )
    server, port = _serve([root], registry=_registry([root], script=script))
    auth = {"X-Edge-Console-Token": "test-token"}
    try:
        status, started = _request(
            port, "POST", "/api/actions", {"action": "status", "root": str(root)}, **auth
        )
        assert status == 202
        action_id = started["id"]

        def prompted():
            _, payload = _request(port, "GET", f"/api/actions/{action_id}/output?cursor=0&wait=5")
            return payload if payload.get("prompt") else None

        payload = _await(prompted, what="the prompt over HTTP")
        assert payload["prompt"]["kind"] == "secret"
        assert "[node03] Enter RSA PASSCODE:" in payload["text"]

        status, _ = _request(
            port,
            "POST",
            f"/api/actions/{action_id}/answer",
            {"prompt_id": payload["prompt"]["id"], "value": "424242"},
            **auth,
        )
        assert status == 200

        def finished():
            _, done = _request(port, "GET", f"/api/actions/{action_id}/output?cursor=0&wait=5")
            return done if done["status"] == "exited" else None

        done = _await(finished, what="the command to exit")
        assert "rolled out" in done["text"]
        assert "424242" not in done["text"]

        _, listing = _request(port, "GET", "/api/actions")
        assert [a["id"] for a in listing["actions"]] == [action_id]
        assert {entry["id"] for entry in listing["catalog"]} == set(ACTION_SPECS)

        # A reload re-reads the whole transcript from cursor 0 without waiting,
        # which is how a second tab (or a refreshed one) catches up.
        _, replayed = _request(port, "GET", f"/api/actions/{action_id}/output?cursor=0&wait=0")
        assert replayed["text"] == done["text"]
        assert "pump(a.id, false)" in PAGE  # the page takes that path when not following
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The offline simulator behind --demo
# ---------------------------------------------------------------------------


def test_demo_simulator_completes_a_guided_release_through_both_operator_gates() -> None:
    """--demo must exercise the same shapes the real engine produces: an RSA
    prompt per node, the guided firewall-off boundary, and a completed ledger."""
    autobench, _robocop = build_demo_checkouts()
    run_id = "run-20260707T131512Z-9c4f2ae"
    completed = subprocess.run(
        [sys.executable, "-u", str(DEMO_ENGINE_PATH), "engine", "release", "--guided", "--run", run_id],
        cwd=str(autobench),
        input="1234567\n7654321\n\n",
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "EDGE_CONSOLE_DEMO_SPEED": "0", "PYTHONUNBUFFERED": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("Enter RSA PASSCODE: ") == 2
    assert "Switch firewall posture to [firewall-off]" in completed.stdout
    assert f"release complete: {run_id}" in completed.stdout

    state = json.loads(
        (autobench / "edge-deploy" / "runs" / run_id / "state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "complete"
    assert all(node["state"] == "passed" for node in state["phases"]["deploy"].values())
    assert state["phases"]["tag_github"]["state"] == "passed"
    assert not (autobench / "edge-deploy" / "runs" / run_id / "run.lock").exists()


def test_demo_simulator_never_touches_the_real_engine() -> None:
    """The simulator runs from inside a fabricated checkout, so it must import
    neither the engine nor the console — exactly like the real engine, which
    the console also invokes by path."""
    source = DEMO_ENGINE_PATH.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", source, re.M)
    assert not [name for name in imports if name.split(".")[0] in {"edge_deploy", "edge_console"}]
