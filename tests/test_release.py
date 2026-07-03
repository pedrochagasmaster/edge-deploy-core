"""Release orchestrator: fan-out, partial failure (ADR-0003), --fail-fast, --snapshot.

Everything is injected — ``publish_fn``, prompt patching, and ``driver_factory`` — so the
real ``run_rollout`` / ``verify`` / auth-broker paths run end to end against the extended
:class:`~conftest.FakeTmuxDriver` with no tmux, SSH, git push or real secrets.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from edge_deploy import auth, drift, release
from edge_deploy.config import DependencyBundleConfig, NodeConfig, OperatorConfig
from edge_deploy.dependencies import create_dependency_bundle
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


def _publishing(events: list, *, fail: tuple[str, ...] = (), local_check_tail: str = ""):
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
            local_check_output_tail=local_check_tail or f"{profile.tool} local_check ok",
        )

    return publish_fn


def _patch_prompt(monkeypatch, events: list, code: str = "12345678") -> None:
    def prompt_fn(prompt: str) -> str:
        events.append(("auth", prompt))
        return code

    monkeypatch.setattr(auth, "_prompt_for_secret", prompt_fn)


@pytest.fixture(autouse=True)
def _default_prompt_auth(monkeypatch):
    monkeypatch.setattr(auth, "_prompt_for_secret", lambda prompt: "12345678")


@pytest.fixture
def patched_drift(monkeypatch):
    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: {"a.py": "1"})


# ---------------------------------------------------------------------------
# Happy path: full Tools × Nodes matrix
# ---------------------------------------------------------------------------


def test_release_full_matrix_succeeds(fake_tmux, tmp_path, patched_drift, monkeypatch) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}
    _patch_prompt(monkeypatch, events)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"]),
        report_dir=tmp_path,
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
        heartbeat_interval_s=3600.0,
        stall_threshold_s=7200.0,
    )

    assert report.exit_code() == 0
    counts = report.summary()["counts"]
    assert counts["rolled_out"] == 4
    assert counts["published"] == 2
    assert all(
        r["status"] == "rolled_out" and r["drift"] == "passed" and r["smoke"] == "passed"
        for r in report.rollouts
    )

    publish_indices = [i for i, e in enumerate(events) if e[0] == "publish"]
    auth_indices = [i for i, e in enumerate(events) if e[0] == "auth"]
    assert len(publish_indices) == 2
    assert len(auth_indices) == 2  # one auth prompt per node, reused across tools
    assert max(publish_indices) < min(auth_indices)

    assert set(drivers) == {"node03", "node04"}
    assert drivers["node03"].ran("/ads_storage/autobench")
    assert drivers["node03"].ran("/ads_storage/dispatch")

    assert (tmp_path / "rollout-autobench-node03.json").exists()
    assert (tmp_path / "rollout-robocop-node04.json").exists()
    assert (tmp_path / "publish-robocop.json").exists()
    assert (tmp_path / "release.json").exists()
    assert (tmp_path / "release.log").exists()


def test_release_default_auth_mode_is_prompt() -> None:
    signature = inspect.signature(run_release)
    assert signature.parameters["auth_mode"].default == "prompt"


# ---------------------------------------------------------------------------
# Partial failure (ADR-0003) + synthetic skipped (Risk #9)
# ---------------------------------------------------------------------------


def test_release_publish_failure_skips_only_that_tool(fake_tmux, tmp_path, patched_drift, monkeypatch) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}
    _patch_prompt(monkeypatch, events)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"]),
        report_dir=tmp_path,
        publish_fn=_publishing(events, fail=("robocop",)),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    counts = report.summary()["counts"]
    assert counts["published"] == 1
    assert counts["publish_failed"] == 1
    assert counts["rolled_out"] == 2
    assert counts["skipped"] == 2
    assert report.exit_code() == 1

    robocop = [r for r in report.rollouts if r["tool"] == "robocop"]
    assert all(r["status"] == "skipped" and r["report_path"] is None for r in robocop)
    assert all("publish failed" in r["state_left"] for r in robocop)
    assert any(h["kind"] == "publish" and h["tool"] == "robocop" for h in report.summary()["handoffs"])


def test_release_forwards_local_check_skip_to_publish(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}

    def publish_fn(profile, **kwargs):
        events.append((profile.tool, kwargs["run_local_check"]))
        return _publishing([])(profile, **kwargs)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"], run_local_check=False),
        report_dir=tmp_path,
        publish_fn=publish_fn,
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    assert report.exit_code() == 0
    assert events == [("autobench", False)]


def test_release_auth_failure_isolated_to_node(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        if name == "node03":
            kw["auth_script"] = ["reject", "reject", "reject"]

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"]),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
        max_auth_attempts=3,
    )

    counts = report.summary()["counts"]
    assert counts["failed"] == 2
    assert counts["rolled_out"] == 2
    node03 = [r for r in report.rollouts if r["node"] == "node03"]
    assert all(r["status"] == "failed" and r["state_left"].startswith("auth:") for r in node03)
    assert (tmp_path / "rollout-autobench-node03.json").exists()


def test_release_delivers_dependency_change(fake_tmux, tmp_path, patched_drift, monkeypatch) -> None:
    operator = _operator()
    drivers: dict = {}

    real_load = release.load_tool_profile

    def load_with_bundle(root):
        profile = real_load(root)
        return dataclasses.replace(
            profile, dependency_bundle=profile.dependency_bundle or DependencyBundleConfig()
        )

    monkeypatch.setattr(release, "load_tool_profile", load_with_bundle)

    def configure(name, kw):
        kw["changed_paths"] = ["requirements.txt"]

    def build(profile, *, source_sha, output_root, **_kwargs):
        wheel = output_root / "demo-1.0-py3-none-any.whl"
        wheel.parent.mkdir(parents=True, exist_ok=True)
        wheel.write_bytes(b"wheel")
        return create_dependency_bundle(
            tool=profile.tool,
            source_sha=source_sha,
            dependency_files={"requirements.txt": b"demo==1.0\n"},
            wheels=[wheel],
            config=profile.dependency_bundle or DependencyBundleConfig(),
            output_dir=output_root / "fixture",
        )

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
        dependency_builder=build,
    )

    assert report.rollouts[0]["status"] == "rolled_out"
    assert report.rollouts[0]["dependency"]["source_sha"] == "src1234abcd"
    assert report.exit_code() == 0
    assert drivers["node03"].uploads
    assert drivers["node03"].ran_step("./update.sh")


# ---------------------------------------------------------------------------
# --fail-fast
# ---------------------------------------------------------------------------


def test_release_fail_fast_halts_after_first_failure(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        if name == "node03":
            kw["update_code"] = 1

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03", "node04"], fail_fast=True),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
    )

    counts = report.summary()["counts"]
    assert counts["failed"] == 1
    assert counts["skipped"] == 3
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
    _patch_prompt(monkeypatch, events)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03"], snapshot=SNAP),
        report_dir=tmp_path,
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    assert not any(e[0] == "publish" for e in events)
    assert report.publishes == []
    assert report.selection["snapshot_override"] == SNAP
    assert report.summary()["counts"]["rolled_out"] == 2
    assert report.exit_code() == 0
    assert drivers["node03"].ran_step(f"./update.sh {SNAP}")


def test_release_tool_snapshots_resume_both_tools_without_publish(
    fake_tmux, tmp_path, monkeypatch, patched_drift
) -> None:
    operator = _operator()
    monkeypatch.setattr(release, "ensure_snapshot_available", lambda root, sha, **kw: True)
    events: list = []
    drivers: dict = {}
    autobench_snapshot = "a" * 40
    robocop_snapshot = "b" * 40
    _patch_prompt(monkeypatch, events)

    def configure(name, kw):
        kw["head_commits"] = [PREV, autobench_snapshot, PREV, robocop_snapshot]

    report = run_release(
        operator,
        ReleaseSelection(
            tools=["autobench", "robocop"],
            nodes=["node03"],
            snapshot_by_tool={"autobench": autobench_snapshot, "robocop": robocop_snapshot},
        ),
        report_dir=tmp_path,
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
    )

    assert not any(e[0] == "publish" for e in events)
    assert report.selection["snapshot_by_tool"] == {"autobench": autobench_snapshot, "robocop": robocop_snapshot}
    assert drivers["node03"].ran_step(f"./update.sh {autobench_snapshot}")
    assert drivers["node03"].ran_step(f"./update.sh {robocop_snapshot}")
    assert report.exit_code() == 0


def test_release_pane_auth_mode_does_not_prompt_for_passcode(fake_tmux, tmp_path, patched_drift, monkeypatch) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}
    progress: list[str] = []
    _patch_prompt(monkeypatch, events)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="pane",
        progress_fn=progress.append,
    )

    assert report.exit_code() == 0
    assert not any(e[0] == "auth" for e in events)
    assert any("waiting for node03 RSA" in message for message in progress)
    assert any("published autobench" in message for message in progress)


def test_release_prompt_auth_uses_auth_wait_seconds(fake_tmux, tmp_path, patched_drift, monkeypatch) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}
    _patch_prompt(monkeypatch, events)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
        auth_wait_seconds=77.0,
    )

    assert report.exit_code() == 0
    assert any(e[0] == "auth" for e in events)
    assert drivers["node03"].await_timeouts == [77.0]


def test_release_prompt_auth_toggles_waiting_on(fake_tmux, tmp_path, patched_drift, monkeypatch) -> None:
    operator = _operator()
    events: list = []
    drivers: dict = {}
    waiting_states: list[str | None] = []
    original_set_waiting = release.ReleaseProgressTracker.set_waiting

    def record_waiting(self, waiting_on: str | None) -> None:
        waiting_states.append(waiting_on)
        original_set_waiting(self, waiting_on)

    monkeypatch.setattr(release.ReleaseProgressTracker, "set_waiting", record_waiting)
    _patch_prompt(monkeypatch, events)

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        publish_fn=_publishing(events),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    assert report.exit_code() == 0
    assert "operator" in waiting_states
    assert None in waiting_states


def test_release_snapshot_unavailable_surfaces_handoff(fake_tmux, tmp_path, monkeypatch) -> None:
    operator = _operator()
    monkeypatch.setattr(release, "ensure_snapshot_available", lambda root, sha, **kw: False)
    drivers: dict = {}

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"], snapshot=SNAP),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    assert report.rollouts[0]["status"] == "failed"
    assert "not available locally" in report.rollouts[0]["state_left"]
    assert report.exit_code() == 1
    assert any(h["kind"] == "snapshot" for h in report.summary()["handoffs"])
    assert drivers == {}


# ---------------------------------------------------------------------------
# Deep smoke pays Kerberos once per node, only for tools that have deep commands
# ---------------------------------------------------------------------------


def test_release_deep_smoke_runs_kerberos_once_per_node(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        kw["klist_code"] = 0

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench", "robocop"], nodes=["node03"], smoke="deep"),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
    )

    assert report.summary()["counts"]["rolled_out"] == 2
    assert drivers["node03"].ran("klist -s")
    assert drivers["node03"].ran("<controlled Impala job>")


def test_release_retries_transient_git_preflight_once(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}
    rollout_calls: list[str] = []

    def configure(name, kw):
        kw["fetch_script"] = [
            (128, "fatal: unable to access 'https://example/repo/': connection reset"),
            (128, "fatal: unable to access 'https://example/repo/': connection reset"),
            (0, ""),
        ]

    original_run_rollout = release.run_rollout

    def counting_run_rollout(*args, **kwargs):
        profile = args[1]
        rollout_calls.append(profile.tool)
        return original_run_rollout(*args, **kwargs)

    import edge_deploy.release as release_module

    release_module.run_rollout = counting_run_rollout
    try:
        report = run_release(
            operator,
            ReleaseSelection(tools=["autobench"], nodes=["node03"]),
            report_dir=tmp_path,
            publish_fn=_publishing([]),
            driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
            auth_mode="prompt",
            heartbeat_interval_s=3600.0,
            stall_threshold_s=7200.0,
        )
    finally:
        release_module.run_rollout = original_run_rollout

    assert report.exit_code() == 0
    assert rollout_calls == ["autobench", "autobench"]
    fetch_commands = [
        command
        for _step, command in drivers["node03"].decoded_step_commands
        if "git fetch --prune" in command
    ]
    assert len(fetch_commands) == 3


def test_release_logs_successful_remote_tracking_ref_repair(
    fake_tmux, tmp_path, patched_drift
) -> None:
    operator = _operator()
    drivers: dict = {}

    def configure(name, kw):
        kw["fetch_script"] = [
            (
                1,
                "error: cannot lock ref 'refs/remotes/bitbucket/main': "
                "unable to resolve reference 'refs/remotes/bitbucket/main'",
            ),
            (0, ""),
        ]

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
        heartbeat_interval_s=3600.0,
        stall_threshold_s=7200.0,
    )

    assert report.exit_code() == 0
    log_text = (tmp_path / "release.log").read_text(encoding="utf-8")
    assert "repaired remote tracking ref for autobench/node03" in log_text


def test_release_does_not_retry_permanent_git_preflight(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}
    rollout_calls: list[str] = []

    def configure(name, kw):
        kw["fetch_script"] = [(128, "fatal: not a git repository: '/bad/path'")]

    original_run_rollout = release.run_rollout

    def counting_run_rollout(*args, **kwargs):
        profile = args[1]
        rollout_calls.append(profile.tool)
        return original_run_rollout(*args, **kwargs)

    import edge_deploy.release as release_module

    release_module.run_rollout = counting_run_rollout
    try:
        report = run_release(
            operator,
            ReleaseSelection(tools=["autobench"], nodes=["node03"]),
            report_dir=tmp_path,
            publish_fn=_publishing([]),
            driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
            auth_mode="prompt",
            heartbeat_interval_s=3600.0,
            stall_threshold_s=7200.0,
        )
    finally:
        release_module.run_rollout = original_run_rollout

    assert report.exit_code() == 1
    assert rollout_calls == ["autobench"]
    rollout = report.rollouts[0]
    assert rollout["status"] == "failed"
    assert "remote git preflight" in rollout["state_left"]
    assert (tmp_path / "release.log").exists()
    assert (tmp_path / "release-progress.json").exists()
    log_text = (tmp_path / "release.log").read_text(encoding="utf-8")
    assert "remote preflight autobench/node03" in log_text
    assert "output tail:" in log_text
    assert "not a git repository" in log_text


def test_resume_loads_publishes_into_release_json(fake_tmux, tmp_path, monkeypatch, patched_drift) -> None:
    operator = _operator()
    monkeypatch.setattr(release, "ensure_snapshot_available", lambda root, sha, **kw: True)
    autobench_snapshot = "a" * 40
    robocop_snapshot = "b" * 40
    (tmp_path / "publish-autobench.json").write_text(
        json.dumps(
            {
                "tool": "autobench",
                "status": "published",
                "deployment_commit": autobench_snapshot,
                "source_short": "src1111",
                "branch": "main",
                "previous_remote_commit": "prev1111",
                "message": "Deploy snapshot: autobench",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "publish-robocop.json").write_text(
        json.dumps(
            {
                "tool": "robocop",
                "status": "published",
                "deployment_commit": robocop_snapshot,
                "source_short": "src2222",
                "branch": "main",
                "previous_remote_commit": "prev2222",
                "message": "Deploy snapshot: robocop",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "release.log").write_text("2026-06-30T12:00:00Z [release] prior run started\n", encoding="utf-8")
    drivers: dict = {}

    def configure(name, kw):
        kw["head_commits"] = [PREV, autobench_snapshot, PREV, robocop_snapshot]

    report = run_release(
        operator,
        ReleaseSelection(
            tools=["autobench", "robocop"],
            nodes=["node03"],
            snapshot_by_tool={"autobench": autobench_snapshot, "robocop": robocop_snapshot},
        ),
        report_dir=tmp_path,
        publish_fn=_publishing([]),
        driver_factory=_make_factory(fake_tmux, drivers, configure=configure),
        auth_mode="prompt",
    )

    assert len(report.publishes) == 2
    release_json = json.loads((tmp_path / "release.json").read_text(encoding="utf-8"))
    assert len(release_json["publishes"]) == 2
    assert {entry["tool"] for entry in release_json["publishes"]} == {"autobench", "robocop"}
    assert release_json["summary"]["counts"]["published"] == 2
    log_text = (tmp_path / "release.log").read_text(encoding="utf-8")
    assert "prior run started" in log_text
    assert "rolling out autobench/node03" in log_text


def test_release_log_includes_local_check_output_tail_redacted(fake_tmux, tmp_path, patched_drift) -> None:
    operator = _operator()
    drivers: dict = {}
    secret = "token=super-secret-value"

    report = run_release(
        operator,
        ReleaseSelection(tools=["autobench"], nodes=["node03"]),
        report_dir=tmp_path,
        publish_fn=_publishing([], local_check_tail=f"local_check passed with {secret}"),
        driver_factory=_make_factory(fake_tmux, drivers),
        auth_mode="prompt",
    )

    assert report.exit_code() == 0
    log_text = (tmp_path / "release.log").read_text(encoding="utf-8")
    assert "local_check autobench output tail:" in log_text
    assert "local_check passed with" in log_text
    assert secret not in log_text
    assert "token=***REDACTED***" in log_text
