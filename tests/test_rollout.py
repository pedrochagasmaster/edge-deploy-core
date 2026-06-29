"""Rollout engine, parametrized across both committed Tool Profiles.

The :class:`~conftest.FakeTmuxDriver` stands in for a live, authenticated pane: no tmux,
no SSH, no Edge Node is touched. Covers the ADR-0005 dependency refuse gate, the
install-decision derived from ``install_trigger_paths``, the non-blocking
``sensitive_changed`` flag, and the ``rolled_out | failed | refused`` status outcomes.
"""

from __future__ import annotations

import pytest

from edge_deploy.rollout import (
    ROLLOUT_STATUSES,
    build_install_command,
    build_update_command,
    decide_install_action,
    matching_paths,
    run_rollout,
)

PREVIOUS = "0" * 40
TARGET = "d" * 40
MISMATCH = "f" * 40

# (tool, changed_paths, expected install action, expected sensitive_changed)
SUCCESS_CASES = [
    pytest.param("autobench", ["benchmark.py"], "skip", [], id="autobench-runtime"),
    pytest.param("autobench", ["pyproject.toml"], "run", [], id="autobench-trigger"),
    pytest.param("robocop", ["dispatch/app.py"], "run", [], id="robocop-trigger"),
    pytest.param(
        "robocop",
        ["scr/Query_Impala_Parametrized.py"],
        "skip",
        ["scr/Query_Impala_Parametrized.py"],
        id="robocop-sensitive",
    ),
]

# (tool, changed_paths, expected refused_paths)
REFUSE_CASES = [
    pytest.param("autobench", ["requirements.txt"], ["requirements.txt"], id="autobench-requirements"),
    pytest.param("autobench", ["constraints.txt", "benchmark.py"], ["constraints.txt"], id="autobench-constraints"),
    pytest.param("robocop", ["requirements.txt", "dispatch/app.py"], ["requirements.txt"], id="robocop-requirements"),
]

# A changed path that is neither a dependency nor refused, per Tool (keeps a rollout green).
SAFE_CHANGE = [
    pytest.param("autobench", ["benchmark.py"], id="autobench"),
    pytest.param("robocop", ["dispatch/app.py"], id="robocop"),
]


def test_rollout_statuses_constant() -> None:
    assert ROLLOUT_STATUSES == ("rolled_out", "failed", "skipped", "refused")


# ---------------------------------------------------------------------------
# Path matching / install decision
# ---------------------------------------------------------------------------


def test_matching_paths_supports_exact_prefix_and_globs() -> None:
    patterns = ["requirements.txt", "scr/", "dispatch/**", "core/**/*.py"]
    changed = ["requirements.txt", "scr/q.py", "dispatch/a/b.py", "dispatch", "core/x/y.py", "README.md"]

    assert set(matching_paths(changed, patterns)) == {
        "requirements.txt",
        "scr/q.py",
        "dispatch/a/b.py",
        "dispatch",
        "core/x/y.py",
    }


@pytest.mark.parametrize(
    "tool, trigger_path",
    [("autobench", "requirements.txt"), ("robocop", "dispatch/widgets/table.py")],
)
def test_decide_install_auto_runs_on_trigger(load_profile, tool, trigger_path) -> None:
    profile = load_profile(tool)

    decision = decide_install_action(profile, mode="auto", changed_paths=[trigger_path, "docs/readme.md"])

    assert decision.action == "run"
    assert trigger_path in decision.reason


def test_decide_install_auto_skips_docs_only(real_profile) -> None:
    decision = decide_install_action(real_profile, mode="auto", changed_paths=["docs/guide.md", "README.md"])

    assert decision.action == "skip"
    assert "No install-trigger" in decision.reason


def test_decide_install_forced_modes(real_profile) -> None:
    assert decide_install_action(real_profile, mode="always", changed_paths=[]).action == "run"
    assert decide_install_action(real_profile, mode="never", changed_paths=["requirements.txt"]).action == "skip"


# ---------------------------------------------------------------------------
# Command construction (ADR-0004 EDGE_DEPLOY_* interface)
# ---------------------------------------------------------------------------


def test_build_update_command_uses_edge_deploy_env(real_profile) -> None:
    command = build_update_command(real_profile, "abc123", remote="bitbucket")

    assert command.startswith(f"cd {real_profile.repo_path} &&")
    assert "EDGE_DEPLOY_REMOTE=bitbucket" in command
    assert f"EDGE_DEPLOY_BRANCH={real_profile.release_branch}" in command
    assert command.endswith("./update.sh abc123")


def test_build_install_command_includes_email_only_when_present(real_profile) -> None:
    with_email = build_install_command(real_profile, operator_email="op@example.com")
    without_email = build_install_command(real_profile)

    assert "EDGE_DEPLOY_EMAIL=op@example.com" in with_email
    assert "EDGE_DEPLOY_PYTHON_BIN=" in with_email
    assert with_email.endswith("./install.sh")
    assert "EDGE_DEPLOY_EMAIL=" not in without_email


# ---------------------------------------------------------------------------
# run_rollout — success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, changed, expect_install, expect_sensitive", SUCCESS_CASES)
def test_run_rollout_success_contract(
    load_profile, sample_node, fake_tmux, tool, changed, expect_install, expect_sensitive
) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET)

    assert report.status == "rolled_out"
    assert report.install_decision == expect_install
    assert sorted(report.sensitive_changed) == sorted(expect_sensitive)

    # update.sh always runs on a non-refused rollout; install.sh only when triggered.
    assert driver.ran(f"EDGE_DEPLOY_REMOTE=bitbucket EDGE_DEPLOY_BRANCH=main ./update.sh {TARGET}")
    assert driver.ran("./install.sh") is (expect_install == "run")

    payload = report.to_payload()
    assert payload["operation"] == "rollout"
    assert payload["status"] == "rolled_out"
    assert payload["node"] == "node03"
    assert payload["host"] == "user@hde2stl020003.mastercard.int"
    assert payload["repo_path"] == profile.repo_path
    assert payload["deployment_commit"] == TARGET
    assert payload["previous_remote_commit"] == PREVIOUS
    assert payload["changed_paths"] == changed
    assert "refused_paths" not in payload
    assert {check["name"] for check in payload["checks"]} == {"update", "final_commit", "install", "permissions"}


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_records_authenticated_pane_calls(load_profile, sample_node, fake_tmux, tool, changed) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed)

    run_rollout(driver, profile, sample_node, target_commit=TARGET)

    # HEAD is read before and after update; the diff is taken once.
    assert sum("git rev-parse --verify HEAD" in c for c in driver.commands) == 2
    assert sum("git diff --name-only" in c for c in driver.commands) == 1


# ---------------------------------------------------------------------------
# run_rollout — ADR-0005 dependency refuse gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, changed, expect_refused", REFUSE_CASES)
def test_run_rollout_refuses_dependency_change_without_running_update(
    load_profile, sample_node, fake_tmux, tool, changed, expect_refused
) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS], changed_paths=changed)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET)

    assert report.status == "refused"
    assert report.install_decision == "not_applicable"
    # The whole point of ADR-0005: nothing is executed on the node.
    assert not driver.ran("./update.sh")
    assert not driver.ran("./install.sh")

    payload = report.to_payload()
    assert payload["refused_paths"] == expect_refused
    assert payload["changed_paths"] == changed
    assert [check["name"] for check in payload["checks"]] == ["dependency_refusal"]
    assert payload["checks"][0]["passed"] is False


# ---------------------------------------------------------------------------
# run_rollout — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_failed_when_update_errors(load_profile, sample_node, fake_tmux, tool, changed) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed, update_code=1)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET)

    assert report.status == "failed"
    update_check = next(check for check in report.checks if check.name == "update")
    assert update_check.passed is False


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_failed_on_commit_mismatch(load_profile, sample_node, fake_tmux, tool, changed) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, MISMATCH], changed_paths=changed)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET)

    assert report.status == "failed"
    final_check = next(check for check in report.checks if check.name == "final_commit")
    assert final_check.passed is False
    assert final_check.evidence == {"expected_commit": TARGET}
