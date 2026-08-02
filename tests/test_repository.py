
import pytest

from edge_deploy.repository import (
    RepositoryError,
    RepositoryState,
    github_ci_conclusions_via_api,
    github_repo_path,
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


def _ci_state(tmp_path):
    return RepositoryState(tmp_path, "autobench", "a" * 40, "origin", "bitbucket")


def test_ci_fallback_never_overturns_an_answer_gh_gave(tmp_path):
    """The gate must not shop for a second opinion. When gh answers, that
    answer is final — otherwise a failing build could be released by asking
    twice."""
    asked = []

    def api_probe(root, commit, run=None):
        asked.append(commit)
        return ["success"]

    with pytest.raises(RepositoryError, match="no successful"):
        require_successful_github_ci(
            _ci_state(tmp_path),
            runner=lambda args: '[{"conclusion":"failure"}]',
            api_probe=api_probe,
        )
    assert asked == [], "the API must not be consulted after gh answered"


def test_ci_fallback_answers_when_gh_cannot(tmp_path):
    """gh missing or unauthenticated is not a reason to block a green build."""
    def no_gh(args):
        raise RepositoryError("gh could not be run: [Errno 2] No such file or directory")

    require_successful_github_ci(
        _ci_state(tmp_path),
        runner=no_gh,
        api_probe=lambda root, commit, run=None: ["success"],
        retry_delay_seconds=0,
    )

    with pytest.raises(RepositoryError, match="no successful"):
        require_successful_github_ci(
            _ci_state(tmp_path),
            runner=no_gh,
            api_probe=lambda root, commit, run=None: ["failure"],
            retry_delay_seconds=0,
        )


def test_ci_fails_closed_when_neither_source_can_answer(tmp_path):
    """An unknown must never read as a pass, and gh's diagnosis is kept."""
    def no_gh(args):
        raise RepositoryError("gh could not be run: [Errno 2] No such file or directory")

    with pytest.raises(RepositoryError, match="could not be run"):
        require_successful_github_ci(
            _ci_state(tmp_path),
            runner=no_gh,
            api_probe=lambda root, commit, run=None: None,
            retry_delay_seconds=0,
        )


def test_ci_fallback_is_used_after_transient_gh_failures_are_exhausted(tmp_path):
    runner = SequenceRunner(
        [RepositoryError("gh failed: unexpected EOF"), RepositoryError("gh failed: unexpected EOF")]
    )
    require_successful_github_ci(
        _ci_state(tmp_path),
        runner=runner,
        attempts=2,
        retry_delay_seconds=0,
        api_probe=lambda root, commit, run=None: ["success"],
    )
    assert runner.calls == 2


def test_ci_fallback_covers_unparseable_gh_output(tmp_path):
    require_successful_github_ci(
        _ci_state(tmp_path),
        runner=lambda args: "not json",
        api_probe=lambda root, commit, run=None: ["success"],
    )


def test_api_probe_reads_conclusions_for_the_exact_sha(tmp_path):
    """Mirrors `gh run list --commit <sha> --workflow CI`: only the CI workflow,
    only that head SHA, and an unfinished run is pending rather than absent."""
    requested = {}

    def fetch(url, token):
        requested["url"] = url
        return {
            "workflow_runs": [
                {"name": "CI", "status": "completed", "conclusion": "success"},
                {"name": "CI", "status": "in_progress", "conclusion": None},
                {"name": "Lint", "status": "completed", "conclusion": "failure"},
            ]
        }

    conclusions = github_ci_conclusions_via_api(
        tmp_path,
        "b" * 40,
        run=lambda args: "https://github.com/mastercard/autobench.git",
        credential=lambda root: "token",
        fetch=fetch,
    )
    assert conclusions == ["success", "pending"], "Lint is not the CI workflow"
    assert "mastercard/autobench" in requested["url"]
    assert "head_sha=" + "b" * 40 in requested["url"]


def test_api_probe_returns_unknown_rather_than_guessing(tmp_path):
    """None means 'could not ask', which callers must not read as 'no runs'."""
    # Not a GitHub remote.
    assert github_ci_conclusions_via_api(
        tmp_path, "a" * 40,
        run=lambda args: "https://scm.mastercard.int/edge/autobench.git",
        credential=lambda root: pytest.fail("must not ask for a credential"),
        fetch=lambda url, token: pytest.fail("must not reach the API"),
    ) is None
    # No remote at all.
    def no_remote(args):
        raise RepositoryError("git failed: no such remote")

    assert github_ci_conclusions_via_api(tmp_path, "a" * 40, run=no_remote) is None
    # A GitHub remote but no usable credential.
    assert github_ci_conclusions_via_api(
        tmp_path, "a" * 40,
        run=lambda args: "https://github.com/mastercard/autobench.git",
        credential=lambda root: None,
        fetch=lambda url, token: pytest.fail("must not reach the API"),
    ) is None
    # The API itself could not be reached.
    assert github_ci_conclusions_via_api(
        tmp_path, "a" * 40,
        run=lambda args: "https://github.com/mastercard/autobench.git",
        credential=lambda root: "token",
        fetch=lambda url, token: None,
    ) is None


def test_github_repo_path_accepts_both_remote_forms():
    assert github_repo_path("https://github.com/mastercard/autobench.git") == "mastercard/autobench"
    assert github_repo_path("git@github.com:mastercard/autobench.git") == "mastercard/autobench"
    assert github_repo_path("https://scm.mastercard.int/edge/autobench.git") is None
