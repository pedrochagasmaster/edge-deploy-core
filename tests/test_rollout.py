"""Rollout engine, parametrized across both committed Tool Profiles.

The :class:`~conftest.FakeTmuxDriver` stands in for a live, authenticated pane: no tmux,
no SSH, no Edge Node is touched. Covers the ADR-0005 dependency refuse gate, the
install-decision derived from ``install_trigger_paths``, the non-blocking
``sensitive_changed`` flag, and the ``rolled_out | failed | refused`` status outcomes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import encode_text_result

from edge_deploy.config import DependencyBundleConfig
from edge_deploy.dependencies import create_dependency_bundle
from edge_deploy.rollout import (
    ROLLOUT_STATUSES,
    RemoteGitPreflightError,
    build_install_command,
    build_update_command,
    decide_install_action,
    matching_paths,
    remote_changed_paths,
    run_rollout,
)
from edge_deploy.runner import read_remote_text

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

RUN_ID = "test-run"


class LocalRunnerGitDriver:
    """Execute real git commands while emulating the on-node runner + D8 reads."""

    def __init__(self, repo_root: Path, *, run_id: str = "edge-deploy") -> None:
        self.repo_root = repo_root
        self.run_id = run_id
        self.steps_root = repo_root / ".edge-deploy" / "runs" / run_id / "steps"
        self.steps_root.mkdir(parents=True, exist_ok=True)
        self.commands: list[str] = []

    def upload_file(self, source: Path | str, remote_path: str) -> str:
        return hashlib.sha256(Path(source).read_bytes()).hexdigest()

    def _bash_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.repo_root)
        return env

    def _repo_posix(self) -> str:
        return self.repo_root.as_posix()

    def _run_bash(self, payload: str) -> tuple[str, int]:
        result = subprocess.run(
            ["bash", "-lc", payload],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            env=self._bash_env(),
            check=False,
        )
        return result.stdout + result.stderr, result.returncode

    def _expand_remote_path(self, remote_path: str) -> Path:
        if remote_path.startswith("~/"):
            return self.repo_root / remote_path[2:]
        if remote_path.startswith("$HOME/"):
            return self.repo_root / remote_path[len("$HOME/") :]
        return Path(remote_path)

    def run_remote(
        self, command: str, *, timeout: float = 30.0, ensure_shell: bool = True
    ) -> tuple[str, int]:
        del timeout, ensure_shell
        self.commands.append(command)
        if "__EDGE_RESULT_START__" in command and "base64 -w0" in command:
            path_match = re.search(r"base64 -w0 ([^;\s]+)", command)
            remote_path = path_match.group(1) if path_match else ""
            content = self._expand_remote_path(remote_path).read_text(encoding="utf-8")
            return encode_text_result(content), 0
        runner_match = re.search(r"sh (\S*runner-\S+\.sh) (\S+) (\S+) (\S+)", command)
        if runner_match:
            _runner_path, run_id, step_name, encoded = runner_match.groups()
            inner = base64.b64decode(encoded).decode("utf-8")
            screen, code = self._run_bash(inner)
            out_file = self.steps_root / f"{step_name}.out"
            out_file.write_text(screen, encoding="utf-8")
            payload = {
                "schema": "edge-deploy/step/1",
                "step": step_name,
                "exit_code": code,
                "started_at": "2026-07-03T12:00:00Z",
                "finished_at": "2026-07-03T12:00:01Z",
                "stdout_tail": "\n".join(screen.splitlines()[-40:]),
            }
            (self.steps_root / f"{step_name}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return "", 0
        if " && " in command:
            payload = command.split(" && ", 1)[1]
            return self._run_bash(f"cd {self._repo_posix()} && {payload}")
        return self._run_bash(command)


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


def test_build_install_command_prefers_dswpython310_alias_then_python310(real_profile) -> None:
    command = build_install_command(real_profile, operator_email="op@example.com")

    assert "command -v dswpython310" in command
    assert "alias dswpython310=" in command
    assert "/sys_apps_01/python/python310/bin/python3.10" in command
    assert "command -v python3.11" in command


# ---------------------------------------------------------------------------
# run_rollout — success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, changed, expect_install, expect_sensitive", SUCCESS_CASES)
def test_run_rollout_success_contract(
    load_profile, sample_node, fake_tmux, tool, changed, expect_install, expect_sensitive
) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert report.status == "rolled_out"
    assert report.install_decision == expect_install
    assert sorted(report.sensitive_changed) == sorted(expect_sensitive)

    # update.sh always runs on a non-refused rollout; install.sh only when triggered.
    assert any(step == "update" for _, _, step in driver.runner_step_commands)
    assert any(step == "install" for _, _, step in driver.runner_step_commands) is (
        expect_install == "run"
    )

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
    expected_checks = {
        "remote_git_preflight",
        "update",
        "final_commit",
        "install",
        "permissions",
    }
    if expect_install == "run":
        expected_checks.add("install_preflight")
    assert {check["name"] for check in payload["checks"]} == expected_checks


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_records_authenticated_pane_calls(load_profile, sample_node, fake_tmux, tool, changed) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed)

    run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert sum(step == "git-rev-parse" for _, _, step in driver.runner_step_commands) == 2
    assert any(step == "git-diff" for _, _, step in driver.runner_step_commands)


# ---------------------------------------------------------------------------
# run_rollout — ADR-0005 dependency refuse gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, changed, expect_refused", REFUSE_CASES)
def test_run_rollout_refuses_dependency_change_without_running_update(
    load_profile, sample_node, fake_tmux, tool, changed, expect_refused
) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS], changed_paths=changed)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert report.status == "refused"
    assert report.install_decision == "not_applicable"
    # A lower-level rollout without reviewed-source provenance remains fail-closed.
    assert not any(step == "update" for _, _, step in driver.runner_step_commands)
    assert not any(step == "install" for _, _, step in driver.runner_step_commands)

    payload = report.to_payload()
    assert payload["dependency_paths"] == expect_refused
    assert payload["changed_paths"] == changed
    assert [check["name"] for check in payload["checks"]] == [
        "remote_git_preflight",
        "dependency_bundle_unavailable",
    ]
    assert payload["checks"][1]["passed"] is False


def test_run_rollout_delivers_dependency_bundle_before_update(
    tmp_path, load_profile, sample_node, fake_tmux
) -> None:
    profile = replace(load_profile("autobench"), dependency_bundle=DependencyBundleConfig())
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    bundle = create_dependency_bundle(
        tool="autobench",
        source_sha="a" * 40,
        dependency_files={"requirements.txt": b"demo==1.0\n"},
        wheels=[wheel],
        config=profile.dependency_bundle,
        output_dir=tmp_path / "bundle",
    )
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=["requirements.txt"])

    report = run_rollout(
        driver,
        profile,
        sample_node,
        target_commit=TARGET,
        dependency_bundle=bundle,
        run_id=RUN_ID,
    )

    assert report.status == "rolled_out"
    names = [check.name for check in report.checks]
    assert names.index("dependency_delivery") < names.index("update")
    assert driver.uploads
    assert any(step == "install" for _, _, step in driver.runner_step_commands)


# ---------------------------------------------------------------------------
# run_rollout — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_failed_when_update_errors(load_profile, sample_node, fake_tmux, tool, changed) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed, update_code=1)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert report.status == "failed"
    update_check = next(check for check in report.checks if check.name == "update")
    assert update_check.passed is False


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_records_install_output_tail_on_failure(
    load_profile, sample_node, fake_tmux, tool, changed
) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed, install_code=1)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, install_mode="always")

    install_check = next(check for check in report.checks if check.name == "install")
    assert install_check.passed is False
    assert install_check.evidence is not None
    assert install_check.evidence["exit_code"] == 1
    assert "install.sh exit 1" in install_check.evidence["output_tail"]


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_preflights_offline_install_before_running_install(
    load_profile, sample_node, fake_tmux, tool, changed
) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=changed, install_preflight_code=1)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, install_mode="always")

    assert report.status == "failed"
    preflight_check = next(check for check in report.checks if check.name == "install_preflight")
    assert preflight_check.passed is False
    assert preflight_check.evidence is not None
    assert preflight_check.evidence["exit_code"] == 1
    assert "install preflight exit 1" in preflight_check.evidence["output_tail"]
    assert not any(step == "install" for _, _, step in driver.runner_step_commands)


@pytest.mark.parametrize("tool, changed", SAFE_CHANGE)
def test_run_rollout_failed_on_commit_mismatch(load_profile, sample_node, fake_tmux, tool, changed) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(head_commits=[PREVIOUS, MISMATCH], changed_paths=changed)

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert report.status == "failed"
    final_check = next(check for check in report.checks if check.name == "final_commit")
    assert final_check.passed is False
    assert final_check.evidence == {"expected_commit": TARGET}


# ---------------------------------------------------------------------------
# Remote git preflight (verify / fetch / diff)
# ---------------------------------------------------------------------------


def test_remote_changed_paths_runs_verify_fetch_diff_separately(load_profile, sample_node, fake_tmux) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(changed_paths=["benchmark.py"])

    paths = remote_changed_paths(driver, profile.repo_path, PREVIOUS, TARGET, run_id=RUN_ID)

    assert paths == ["benchmark.py"]
    assert any(step == "git-verify" for _, _, step in driver.runner_step_commands)
    assert any(step == "git-fetch" for _, _, step in driver.runner_step_commands)
    assert any(step == "git-diff" for _, _, step in driver.runner_step_commands)


def test_remote_changed_paths_repairs_corrupt_tracking_ref(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    node = tmp_path / "node"

    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True
    )
    (seed / "README.md").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "before"], check=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True)
    subprocess.run(["git", "clone", "--branch", "main", str(remote), str(node)], check=True)
    previous = subprocess.check_output(
        ["git", "-C", str(node), "rev-parse", "HEAD"], text=True
    ).strip()

    (seed / "README.md").write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "after"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True)
    target = subprocess.check_output(
        ["git", "-C", str(seed), "rev-parse", "HEAD"], text=True
    ).strip()
    remote_ref = node / ".git" / "refs" / "remotes" / "origin" / "main"
    remote_ref.parent.mkdir(parents=True, exist_ok=True)
    remote_ref.write_text("", encoding="utf-8")

    class LocalGitDriver(LocalRunnerGitDriver):
        pass

    paths = remote_changed_paths(
        LocalGitDriver(node),
        node.as_posix(),
        previous,
        target,
        remote="origin",
        branch="main",
    )

    assert paths == ["README.md"]
    tracking_ref = subprocess.check_output(
        ["git", "-C", str(node), "rev-parse", "refs/remotes/origin/main"], text=True
    ).strip()
    assert tracking_ref == target


def test_remote_fetch_retries_once_on_transient_failure(load_profile, sample_node, fake_tmux) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(
        changed_paths=["benchmark.py"],
        fetch_script=[
            (128, "fatal: unable to access 'https://example/repo/': connection reset"),
            (0, ""),
        ],
    )

    paths = remote_changed_paths(driver, profile.repo_path, PREVIOUS, TARGET, run_id=RUN_ID)

    assert paths == ["benchmark.py"]
    fetch_steps = [step for _, _, step in driver.runner_step_commands if step == "git-fetch"]
    assert len(fetch_steps) == 2


def test_remote_fetch_repairs_corrupt_tracking_ref_once(load_profile, sample_node, fake_tmux) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(
        changed_paths=["README.md"],
        fetch_script=[
            (
                1,
                "error: cannot lock ref 'refs/remotes/bitbucket/main': "
                "unable to resolve reference 'refs/remotes/bitbucket/main'",
            ),
            (0, ""),
        ],
    )

    paths = remote_changed_paths(driver, profile.repo_path, PREVIOUS, TARGET, run_id=RUN_ID)

    assert paths == ["README.md"]
    assert driver.ran("git update-ref -d refs/remotes/bitbucket/main")
    fetch_steps = [step for _, _, step in driver.runner_step_commands if step == "git-fetch"]
    assert len(fetch_steps) == 2


def test_run_rollout_reports_successful_tracking_ref_repair(
    load_profile, sample_node, fake_tmux
) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(
        head_commits=[PREVIOUS, TARGET],
        changed_paths=["README.md"],
        fetch_script=[
            (
                1,
                "error: cannot lock ref 'refs/remotes/bitbucket/main': "
                "unable to resolve reference 'refs/remotes/bitbucket/main'",
            ),
            (0, ""),
        ],
    )

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    preflight = next(check for check in report.checks if check.name == "remote_git_preflight")
    assert preflight.passed is True
    assert preflight.evidence == {
        "fetch_attempts": 2,
        "repair_attempted": True,
        "repair_succeeded": True,
    }


def test_remote_fetch_does_not_repair_unrelated_bad_object(
    load_profile, sample_node, fake_tmux
) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(fetch_script=[(128, "fatal: bad object deadbeef")])

    with pytest.raises(RemoteGitPreflightError):
        remote_changed_paths(driver, profile.repo_path, PREVIOUS, TARGET)

    assert not driver.ran("git update-ref -d")


def test_run_rollout_preflight_failure_preserves_evidence(load_profile, sample_node, fake_tmux) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(
        head_commits=[PREVIOUS],
        changed_paths=["benchmark.py"],
        fetch_script=[(128, "fatal: not a git repository: '/bad/path'")],
    )

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert report.status == "failed"
    assert not any(step == "update" for _, _, step in driver.runner_step_commands)
    check = next(check for check in report.checks if check.name == "remote_git_preflight")
    evidence = check.evidence or {}
    assert evidence["step"] == "fetch"
    assert "git fetch --prune" in evidence["command"]
    assert evidence["exit_code"] == 128
    assert evidence["output_tail"]
    assert evidence["transient"] is False
    assert evidence["attempts"] == 1
    assert evidence["suggested_action"]


# ---------------------------------------------------------------------------
# Runner file protocol (PR-16)
# ---------------------------------------------------------------------------


def test_read_remote_text_survives_wrapped_changed_paths(fake_tmux) -> None:
    driver = fake_tmux(changed_paths=["benchmark.py", "pyproject.toml"])
    driver.runner_step_results["git-diff"] = {
        "schema": "edge-deploy/step/1",
        "step": "git-diff",
        "exit_code": 0,
        "started_at": "2026-07-03T12:00:00Z",
        "finished_at": "2026-07-03T12:00:01Z",
        "stdout_tail": "",
    }

    text = read_remote_text(driver, f"~/.edge-deploy/runs/{RUN_ID}/steps/git-diff-data.txt")

    assert text.splitlines() == ["benchmark.py", "pyproject.toml"]


def test_run_rollout_happy_path_uses_runner_steps(load_profile, sample_node, fake_tmux) -> None:
    profile = load_profile("autobench")
    driver = fake_tmux(head_commits=[PREVIOUS, TARGET], changed_paths=["benchmark.py"])

    report = run_rollout(driver, profile, sample_node, target_commit=TARGET, run_id=RUN_ID)

    assert report.status == "rolled_out"
    step_names = [step for _, _, step in driver.runner_step_commands]
    assert "git-verify" in step_names
    assert "git-fetch" in step_names
    assert "git-diff" in step_names
    assert "update" in step_names
    assert "permission-check" in step_names
    assert driver.uploads


def test_run_rollout_exit_code_only_commands_still_use_run_remote(
    load_profile, sample_node, fake_tmux
) -> None:
    profile = replace(load_profile("autobench"), dependency_bundle=DependencyBundleConfig())
    driver = fake_tmux(
        head_commits=[PREVIOUS, TARGET],
        changed_paths=["benchmark.py"],
        install_code=0,
    )

    run_rollout(driver, profile, sample_node, target_commit=TARGET, install_mode="always", run_id=RUN_ID)

    assert driver.ran("test -f /ads_storage/$USER/.edge-deploy/bundles/autobench/current/manifest.json")
    assert any(step == "install" for _, _, step in driver.runner_step_commands)
    assert not any("ln -sfn" in command for command in driver.commands)
