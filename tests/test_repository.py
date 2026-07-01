from pathlib import Path

import pytest

from edge_deploy.repository import (
    RepositoryError,
    RepositoryState,
    inspect_repository,
    require_successful_github_ci,
)


class FakeRunner:
    def __init__(self, values):
        self.values = values

    def __call__(self, args):
        key = tuple(args)
        value = self.values.get(key, "")
        if isinstance(value, Exception):
            raise value
        return value


class SequenceRunner:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, args):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def values():
    return {
        ("git", "branch", "--show-current"): "main\n",
        ("git", "status", "--porcelain", "--untracked-files=all"): "",
        ("git", "rev-parse", "HEAD"): "a" * 40 + "\n",
        ("git", "rev-parse", "refs/remotes/origin/main"): "a" * 40 + "\n",
        ("git", "remote", "get-url", "origin"): "https://github.com/pedrochagasmaster/autobench.git\n",
        ("git", "remote", "get-url", "bitbucket"): "https://scm.example/autobench.git\n",
    }


def test_inspect_repository_accepts_exact_clean_main(tmp_path):
    state = inspect_repository(
        tmp_path,
        tool="autobench",
        expected_origin="https://github.com/pedrochagasmaster/autobench",
        expected_bitbucket="https://scm.example/autobench",
        runner=FakeRunner(values()),
    )
    assert state.commit == "a" * 40


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (("git", "branch", "--show-current"), "feature\n", "branch 'main'"),
        (("git", "status", "--porcelain", "--untracked-files=all"), " M x\n", "clean working tree"),
        (("git", "rev-parse", "HEAD"), "b" * 40, "origin/main"),
        (("git", "remote", "get-url", "origin"), "https://wrong", "unexpected repository"),
    ],
)
def test_inspect_repository_rejects_invalid_state(tmp_path, key, value, message):
    data = values()
    data[key] = value
    with pytest.raises(RepositoryError, match=message):
        inspect_repository(
            tmp_path,
            tool="autobench",
            expected_origin="https://github.com/pedrochagasmaster/autobench",
            expected_bitbucket="https://scm.example/autobench",
            runner=FakeRunner(data),
        )


def test_inspect_repository_ignores_generated_release_reports(tmp_path):
    data = values()
    data[("git", "status", "--porcelain", "--untracked-files=all")] = (
        "?? edge-deploy/reports/release-20260701T194538Z/release.json\n"
        "?? edge-deploy/reports/release-20260701T194538Z/release.log\n"
    )

    state = inspect_repository(
        tmp_path,
        tool="autobench",
        expected_origin="https://github.com/pedrochagasmaster/autobench",
        expected_bitbucket="https://scm.example/autobench",
        runner=FakeRunner(data),
    )

    assert state.commit == "a" * 40


def test_inspect_repository_rejects_other_edge_deploy_files(tmp_path):
    data = values()
    data[("git", "status", "--porcelain", "--untracked-files=all")] = "?? edge-deploy/config.yaml\n"

    with pytest.raises(RepositoryError, match="clean working tree"):
        inspect_repository(
            tmp_path,
            tool="autobench",
            expected_origin="https://github.com/pedrochagasmaster/autobench",
            expected_bitbucket="https://scm.example/autobench",
            runner=FakeRunner(data),
        )


def test_require_successful_github_ci_accepts_exact_sha(tmp_path):
    state = RepositoryState(tmp_path, "autobench", "a" * 40, "origin", "bitbucket")
    require_successful_github_ci(state, runner=lambda args: '[{"conclusion":"success"}]')


def test_require_successful_github_ci_rejects_missing_success(tmp_path):
    state = RepositoryState(tmp_path, "autobench", "a" * 40, "origin", "bitbucket")
    with pytest.raises(RepositoryError, match="no successful"):
        require_successful_github_ci(state, runner=lambda args: '[{"conclusion":"failure"}]')


def test_require_successful_github_ci_retries_transient_gh_eof(tmp_path):
    state = RepositoryState(tmp_path, "autobench", "a" * 40, "origin", "bitbucket")
    runner = SequenceRunner(
        [
            RepositoryError(
                'gh failed: couldn\'t fetch workflows for pedrochagasmaster/autobench: '
                'Get "https://api.github.com/repos/pedrochagasmaster/autobench/actions/workflows": unexpected EOF'
            ),
            '[{"conclusion":"success"}]',
        ]
    )

    require_successful_github_ci(state, runner=runner, retry_delay_seconds=0)

    assert runner.calls == 2


def test_require_successful_github_ci_does_not_retry_missing_success(tmp_path):
    state = RepositoryState(tmp_path, "autobench", "a" * 40, "origin", "bitbucket")
    runner = SequenceRunner(['[{"conclusion":"failure"}]'])

    with pytest.raises(RepositoryError, match="no successful"):
        require_successful_github_ci(state, runner=runner, retry_delay_seconds=0)

    assert runner.calls == 1
