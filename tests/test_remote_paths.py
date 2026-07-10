import pytest

from edge_deploy.remote_paths import edge_deploy_path, resolve_home_path, shell_remote_path


def test_edge_deploy_path_is_logical_and_variable_free() -> None:
    path = edge_deploy_path("bundles", "autobench", ".incoming", "abc.zip")
    assert path == "~/.edge-deploy/bundles/autobench/.incoming/abc.zip"
    assert "$USER" not in path
    assert "$HOME" not in path


@pytest.mark.parametrize("part", ["../escape", "/absolute", "a/../../escape", ""])
def test_edge_deploy_path_rejects_unsafe_parts(part: str) -> None:
    with pytest.raises(ValueError, match="safe relative POSIX path"):
        edge_deploy_path(part)


def test_shell_remote_path_expands_home_but_quotes_remainder() -> None:
    rendered = shell_remote_path("~/.edge-deploy/a path/file")
    assert rendered == "$HOME/'.edge-deploy/a path/file'"


def test_resolve_home_path_returns_concrete_path() -> None:
    assert resolve_home_path("~/.edge-deploy/a", "/ads_storage/operator") == (
        "/ads_storage/operator/.edge-deploy/a"
    )
