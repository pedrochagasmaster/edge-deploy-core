"""Verify: per-tool smoke (one check per command) + drift merge, across both profiles.

No tmux/SSH/network: the :class:`~conftest.FakeTmuxDriver` answers smoke commands (0 by
default, or via ``command_codes``), and ``drift.local_runtime_map`` is monkeypatched so the
drift compare runs without real git.
"""

from __future__ import annotations

import pytest

from edge_deploy import drift
from edge_deploy.verify import run_smoke, verify_after_rollout


@pytest.mark.parametrize("tool", ["autobench", "robocop"])
def test_run_smoke_one_check_per_standard_command(load_profile, fake_tmux, tool) -> None:
    profile = load_profile(tool)
    driver = fake_tmux()

    checks = run_smoke(driver, profile, level="standard")

    assert [check.name for check in checks] == [f"smoke:{command}" for command in profile.smoke.standard]
    assert all(check.passed for check in checks)
    # Every smoke command carries its own explicit ``cd <repo_path>`` (Risk #7).
    for command in profile.smoke.standard:
        assert driver.ran(f"cd {profile.repo_path} && {command}")


def test_run_smoke_marks_failing_command(autobench_profile, fake_tmux) -> None:
    failing = autobench_profile.smoke.standard[0]
    driver = fake_tmux(command_codes={failing: 3})

    checks = run_smoke(driver, autobench_profile, level="standard")

    failed = [check for check in checks if not check.passed]
    assert [check.name for check in failed] == [f"smoke:{failing}"]
    assert "exit 3" in failed[0].message


def test_run_smoke_deep_autobench_is_noop(autobench_profile, fake_tmux) -> None:
    driver = fake_tmux()

    # autobench's smoke.deep is [] -> deep is a no-op and needs no Kerberos.
    assert run_smoke(driver, autobench_profile, level="deep") == []
    assert driver.commands == []


def test_run_smoke_deep_robocop_runs_deep_commands(robocop_profile, fake_tmux) -> None:
    driver = fake_tmux()

    checks = run_smoke(driver, robocop_profile, level="deep")

    assert [check.name for check in checks] == [f"smoke:{command}" for command in robocop_profile.smoke.deep]


@pytest.mark.parametrize("tool", ["autobench", "robocop"])
def test_verify_after_rollout_merges_drift_then_smoke(
    load_profile, sample_node, fake_tmux, monkeypatch, tool
) -> None:
    profile = load_profile(tool)
    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: {"a.py": "1"})
    driver = fake_tmux(remote_runtime={"a.py": "1"})

    checks = verify_after_rollout(
        driver, profile, sample_node, commit="c" * 40, local_root="/local/root", smoke_level="standard"
    )

    names = [check.name for check in checks]
    assert names[0] == "runtime_drift"
    assert names[1:] == [f"smoke:{command}" for command in profile.smoke.standard]
    assert all(check.passed for check in checks)


@pytest.mark.parametrize("tool", ["autobench", "robocop"])
def test_verify_after_rollout_flags_drift(load_profile, sample_node, fake_tmux, monkeypatch, tool) -> None:
    profile = load_profile(tool)
    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: {"a.py": "1"})
    driver = fake_tmux(remote_runtime={"a.py": "DRIFTED"})

    checks = verify_after_rollout(
        driver, profile, sample_node, commit="c" * 40, local_root="/local/root", smoke_level="standard"
    )

    drift_check = next(check for check in checks if check.name == "runtime_drift")
    assert drift_check.passed is False
