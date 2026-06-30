"""Release orchestrator: fan-out, partial failure (ADR-0003), --fail-fast, --snapshot.

Everything is injected — ``publish_fn``, ``getpass_fn`` and ``driver_factory`` — so the
real ``run_rollout`` / ``verify`` / auth-seam paths run end to end against the extended
:class:`~conftest.FakeTmuxDriver` with no tmux, SSH, git push or real secrets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edge_deploy import drift, release
from edge_deploy.config import NodeConfig, OperatorConfig
from edge_deploy.publish import PublishError, PublishResult
from edge_deploy.release import ReleaseSelection, run_release

PROJECTS_ROOT = Path(__file__).resolve().parents[2]
PREV = "0" * 40
SNAP = "5" * 40


def _operator() -> OperatorConfig:
    if not all((PROJECTS_ROOT / tool / "edge_deploy.yaml").exists() for tool in ("autobench", "robocop")):
        pytest.skip("real Tool Profiles not available")
    return OperatorConfig(
        operator_email="op@mastercard.com",
        nodes={
            "node03": NodeConfig(host="u@h3", session="s3", name="node03"),
            "node04": NodeConfig(host="u@h4", session="s4", name="node04"),
        },
        tools={"autobench": str(PROJECTS_ROOT / "autobench"), "robocop": str(PROJECTS_ROOT / "robocop")},
    )


def _make_factory(fake_tmux, drivers: dict, *, configure=None):
    """Build a ``driver_factory`` that yields one configured fake per node (reused across tools)."""

    def factory(node, profile, **kwargs):
        kw = dict(
            head_commits=[PREV, SNAP],
            changed_paths=["benchmark.py"],
            remote_runtime={"a.py": "1"},
            auth_script=["accept"],
        )
        if configure:
            configure(node.name, kw)
        driver = fake_tmux(**kw)
        drivers[node.name] = driver
        return driver

    return factory


def _publishing(events: list, *, fail: tuple[str, ...] = ()):
    def publish_fn(profile, **kwargs):
        events.append(("publish", profile.tool))
        if profile.tool in fail:
            raise PublishError("local_check.ps1 failed with exit code 1")
        return PublishResult(
            tool=profile.tool,
            status="published",
            snapshot=SNAP,
            source_commit="src1234abcd",
            source_short="src1234",
            branch="main",
            previous_remote_commit="prev1234",
            message=f"Deploy snapshot: {profile.tool} src1234 on main (2026-06-29 23:00) [edge-deploy]",
            gate={"clean_tree": True, "on_release_branch": True, "local_check": True},
        )

    return publish_fn


def _getpass(events: list, code: str = "12345678"):
    def getpass_fn(prompt: str) -> str:
        events.append(("auth", prompt))
        return code

    return getpass_fn


@pytest.fixture
def patched_drift(monkeypatch):
    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: {"a.py": "1"})


# ---------------------------------------------------------------------------
# Happy path: full Tools × Nodes matrix
# ---------------------------------------------------------------------------


def test_release_full_matrix_succeeds(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"]),
        report_dir=tmp_path,
        getpass_fn=_getpass(events),
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
    )

    assert report.exit_code() == 0
    counts = report.summary()["counts"]
    assert counts["rolled_out"] == 4
    assert counts["published"] == 2
    assert all(
        r["status"] == "rolled_out" and r["drift"] == "passed" and r["smoke"] == "passed"
        for r in report.rollouts
    )

    # Loop order: every Publish precedes any auth (a broken build never authenticates a node).
    publish_indices = [i for i, e in enumerate(events) if e[0] == "publish"]
    auth_indices = [i for i, e in enumerate(events) if e[0] == "auth"]
    assert len(publish_indices) == 2
    assert len(auth_indices) == 2  # one Authenticated Pane (one getpass) per node, reused across tools
    assert max(publish_indices) < min(auth_indices)

    # One pane per node, reused across both tools (each tool carries its own cd — Risk #7).
    assert set(drivers) == {"node03", "node04"}
    assert drivers["node03"].ran("/ads_storage/autobench")
    assert drivers["node03"].ran("/ads_storage/dispatch")

    # Detailed per-pair + per-publish files were written.
    assert (tmp_path / "rollout-autobench-node03.json").exists()
    assert (tmp_path / "rollout-robocop-node04.json").exists()
    assert (tmp_path / "publish-robocop.json").exists()


# ---------------------------------------------------------------------------
# Partial failure (ADR-0003) + synthetic skipped (Risk #9)
# ---------------------------------------------------------------------------


def test_release_publish_failure_skips_only_that_tool(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"]),
        report_dir=tmp_path,
        getpass_fn=_getpass(events),
        publish_fn=_publishing(events, fail=("robocop",)),
        driver_factory=_make_factory(fake_tmux, drivers),
    )

    counts = report.summary()["counts"]
    assert counts["published"] == 1
    assert counts["publish_failed"] == 1
    assert counts["rolled_out"] == 2  # autobench on both nodes
    assert counts["skipped"] == 2  # robocop on both nodes (engine never returns skipped — synthesized)
    assert report.exit_code() == 1

    robocop = [r for r in report.rollouts if r["tool"] == "robocop"]
    assert all(r["status"] == "skipped" and r["report_path"] is None for r in robocop)
    assert all("publish failed" in r["state_left"] for r in robocop)
    assert any(h["kind"] == "publish" and h["tool"] == "robocop" for h in report.summary()["handoffs"])


def test_release_auth_failure_isolated_to_node(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        if name == "node03":
            kw["auth_script"] = ["reject", "reject", "reject"]  # exhausts -> AuthenticationError

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"]),
        report_dir=tmp_path,
        getpass_fn=_getpass([]),
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        max_auth_attempts=3,
    )

    counts = report.summary()["counts"]
    assert counts["failed"] == 2  # node03 both tools failed at auth
    assert counts["rolled_out"] == 2  # node04 continued and succeeded (never blocked)
    node03 = [r for r in report.rollouts if r["node"] == "node03"]
    assert all(r["status"] == "failed" and r["state_left"].startswith("auth:") for r in node03)
    assert (tmp_path / "rollout-autobench-node03.json").exists()  # synthetic auth report still written


def test_release_refused_dependency_change(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        kw["changed_paths"] = ["requirements.txt"]  # ADR-0005 refuse

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        getpass_fn=_getpass([]),
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
    )

    assert report.rollouts[0]["status"] == "refused"
    assert report.summary()["counts"]["refused"] == 1
    assert report.exit_code() == 1
    assert not drivers["node03"].ran("./update.sh")  # refused before anything ran on the node
    assert any(h["kind"] == "refused" for h in report.summary()["handoffs"])


# ---------------------------------------------------------------------------
# --fail-fast
# ---------------------------------------------------------------------------


def test_release_fail_fast_halts_after_first_failure(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        if name == "node03":
            kw["update_code"] = 1  # autobench update.sh fails on node03

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"], fail_fast=True),
        report_dir=tmp_path,
        getpass_fn=_getpass([]),
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
    )

    counts = report.summary()["counts"]
    assert counts["failed"] == 1  # autobench@node03
    assert counts["skipped"] == 3  # robocop@node03 + both tools @node04 (halted)
    # node04 was never even authenticated once fail-fast tripped.
    assert "node04" not in drivers
    halted = [r for r in report.rollouts if r["node"] == "node04"]
    assert all("halted by --fail-fast" in r["state_left"] for r in halted)


# ---------------------------------------------------------------------------
# --snapshot resume (skip Publish) + Risk #1 local-availability guard
# ---------------------------------------------------------------------------


def test_release_snapshot_skips_publish_and_reuses_sha(fake_tmux, tmp_path, monkeypatch, patched_drift) -> None:
    operator = _operator()
    monkeypatch.setattr(release, "ensure_snapshot_available", lambda root, sha, **kw: True)
    events: list = []
    drivers: dict = {}

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03"], snapshot=SNAP),
        report_dir=tmp_path,
        getpass_fn=_getpass(events),
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
    )

    assert not any(e[0] == "publish" for e in events)  # Publish skipped entirely
    assert report.publishes == []
    assert report.selection["snapshot_override"] == SNAP
    assert report.summary()["counts"]["rolled_out"] == 2
    assert report.exit_code() == 0
    # The reused Snapshot SHA was the rollout target on the node.
    assert drivers["node03"].ran(f"./update.sh {SNAP}")


def test_release_snapshot_unavailable_surfaces_handoff(fake_tmux, tmp_path, monkeypatch) -> None:
    operator = _operator()
    monkeypatch.setattr(release, "ensure_snapshot_available", lambda root, sha, **kw: False)
    drivers: dict = {}

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"], snapshot=SNAP),
        report_dir=tmp_path,
        getpass_fn=_getpass([]),
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers),
    )

    assert report.rollouts[0]["status"] == "failed"
    assert "not available locally" in report.rollouts[0]["state_left"]
    assert report.exit_code() == 1
    assert any(h["kind"] == "snapshot" for h in report.summary()["handoffs"])
    # The fan-out never started: no node was authenticated.
    assert drivers == {}


# ---------------------------------------------------------------------------
# Deep smoke pays Kerberos once per node, only for tools that have deep commands
# ---------------------------------------------------------------------------


def test_release_deep_smoke_runs_kerberos_once_per_node(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        kw["klist_code"] = 0  # existing Kerberos ticket -> no password prompt

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03"], smoke="deep"),
        report_dir=tmp_path,
        getpass_fn=_getpass([]),
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
    )

    assert report.summary()["counts"]["rolled_out"] == 2
    # Kerberos was checked on the node, and robocop's deep command ran; autobench deep is [].
    assert drivers["node03"].ran("klist -s")
    assert drivers["node03"].ran("<controlled Impala job>")
