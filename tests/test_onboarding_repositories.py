import sys
from pathlib import Path

import pytest

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.manifest import CORE_GITHUB_URL, TOOL_MANIFESTS, approved_engine_tag
from edge_deploy.onboarding.repositories import (
    assert_engine_pins_compatible,
    bootstrap_core_root,
    install_tool_dependencies,
    provision_tool_checkout,
    validate_bootstrap_core,
)


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.origin = TOOL_MANIFESTS["autobench"].github_url
        self.bitbucket = ""
        self.head_tag = approved_engine_tag()
        self.head_sha = "a" * 40
        self.tag_sha = "a" * 40
        self.get_url_errors: dict[str, Exception] = {}

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:2] == ["git", "clone"]:
            dest = Path(args[-1])
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
            (dest / "edge_deploy.yaml").write_text("tool: autobench\n", encoding="utf-8")
            (dest / "tools" / "dev").mkdir(parents=True)
            (dest / "tools" / "dev" / "local_check.ps1").write_text("# ok\n", encoding="utf-8")
            (dest / "pyproject.toml").write_text(
                f"edge-deploy-core @ git+https://example/@{approved_engine_tag()}\n",
                encoding="utf-8",
            )
            return ""
        if args == ["git", "remote"]:
            names = ["origin"]
            if self.bitbucket:
                names.append("bitbucket")
            return "\n".join(names) + "\n"
        if args[:3] == ["git", "remote", "get-url"]:
            name = args[3]
            if name in self.get_url_errors:
                raise self.get_url_errors[name]
            if name == "origin":
                return self.origin + "\n"
            if name == "bitbucket":
                if not self.bitbucket:
                    raise RuntimeError("fatal: No such remote 'bitbucket'")
                return self.bitbucket + "\n"
            raise RuntimeError(f"fatal: No such remote '{name}'")
        if args[:3] == ["git", "remote", "add"]:
            if args[3] == "bitbucket":
                self.bitbucket = args[4]
            return ""
        if args[:2] == ["git", "status"]:
            return ""
        if args[:3] == ["git", "describe", "--tags"]:
            if not self.head_tag:
                raise RuntimeError("fatal: no tag exactly matches 'HEAD'")
            return self.head_tag + "\n"
        if args[:2] == ["git", "rev-parse"]:
            target = args[2]
            if target == "HEAD":
                return self.head_sha + "\n"
            if target.endswith("^{}"):
                return self.tag_sha + "\n"
            return self.head_sha + "\n"
        return ""


def _seed_tool_checkout(dest: Path) -> None:
    dest.mkdir(parents=True)
    (dest / ".git").mkdir()
    (dest / "edge_deploy.yaml").write_text("tool: autobench\n", encoding="utf-8")
    (dest / "tools" / "dev").mkdir(parents=True)
    (dest / "tools" / "dev" / "local_check.ps1").write_text("# ok\n", encoding="utf-8")
    (dest / "pyproject.toml").write_text(
        f"edge-deploy-core @ git+https://example/@{approved_engine_tag()}\n",
        encoding="utf-8",
    )


def _tool_pyproject(*, tag: str, extras: bool = True) -> str:
    lines = [
        "[project]",
        "name = \"demo-tool\"",
        f'dependencies = ["edge-deploy-core @ git+https://example/@{tag}"]',
        "",
    ]
    if extras:
        lines.extend(
            [
                "[project.optional-dependencies]",
                'dev = ["pytest"]',
                'release = ["build"]',
                "",
            ]
        )
    return "\n".join(lines)


def test_bootstrap_core_root_is_editable_package_parent() -> None:
    expected = Path(engine_identity()["package_dir"]).resolve().parent
    assert bootstrap_core_root() == expected


def test_validate_bootstrap_core_never_clones(tmp_path: Path) -> None:
    core = tmp_path / "edge-deploy-core"
    core.mkdir()
    (core / ".git").mkdir()
    fake = FakeGit()
    fake.origin = CORE_GITHUB_URL
    result = validate_bootstrap_core(
        core,
        bitbucket_url="https://bitbucket.example/core.git",
        expected_tag=approved_engine_tag(),
        runner=fake,
    )
    assert result.action == "validated"
    assert not any(c[:2] == ["git", "clone"] for c in fake.calls)


def test_clone_tool_when_missing(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    fake = FakeGit()
    result = provision_tool_checkout(
        dest,
        TOOL_MANIFESTS["autobench"],
        bitbucket_url="https://bitbucket.example/ab.git",
        runner=fake,
    )
    assert result.action == "cloned"
    assert dest.is_dir()
    assert any(c[:2] == ["git", "clone"] for c in fake.calls)


def test_refuse_unexpected_existing_directory(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    dest.mkdir()
    (dest / "README.md").write_text("nope\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected"):
        provision_tool_checkout(
            dest,
            TOOL_MANIFESTS["autobench"],
            bitbucket_url="https://bitbucket.example/ab.git",
            runner=lambda args: "",
        )


def test_engine_pin_mismatch_fails_before_install(tmp_path: Path) -> None:
    a = tmp_path / "autobench"
    b = tmp_path / "robocop"
    for root, tag in ((a, "v1.5.3"), (b, "v1.4.0")):
        root.mkdir()
        (root / "pyproject.toml").write_text(
            f'dependencies = ["edge-deploy-core @ git+https://example/@{tag}"]\n',
            encoding="utf-8",
        )
    with pytest.raises(RuntimeError, match="engine pin"):
        assert_engine_pins_compatible([a, b], expected_tag="v1.5.3")


def test_pin_mismatch_causes_zero_install_calls(tmp_path: Path) -> None:
    a = tmp_path / "autobench"
    b = tmp_path / "robocop"
    for root, tag in ((a, approved_engine_tag()), (b, "v1.4.0")):
        root.mkdir()
        (root / "pyproject.toml").write_text(_tool_pyproject(tag=tag), encoding="utf-8")
    install_calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        install_calls.append(list(args))
        return ""

    with pytest.raises(RuntimeError, match="engine pin"):
        assert_engine_pins_compatible([a, b], expected_tag=approved_engine_tag())
        for root in (a, b):
            install_tool_dependencies(root, runner=runner)

    assert install_calls == []


def test_install_tool_dependencies_runs_declared_operator_extras(tmp_path: Path) -> None:
    tool = tmp_path / "autobench"
    tool.mkdir()
    (tool / "pyproject.toml").write_text(
        _tool_pyproject(tag=approved_engine_tag()),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(list(args))
        return ""

    assert_engine_pins_compatible([tool], expected_tag=approved_engine_tag())
    install_tool_dependencies(tool, runner=runner)

    assert calls == [[sys.executable, "-m", "pip", "install", "-e", ".[dev,release]"]]


def test_install_tool_dependencies_requires_declared_extras(tmp_path: Path) -> None:
    tool = tmp_path / "autobench"
    tool.mkdir()
    (tool / "pyproject.toml").write_text(
        _tool_pyproject(tag=approved_engine_tag(), extras=False),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    with pytest.raises(RuntimeError, match="optional-dependencies"):
        install_tool_dependencies(tool, runner=lambda args: calls.append(list(args)) or "")

    assert calls == []


def test_wrong_bitbucket_remote_refused_without_private_urls(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    _seed_tool_checkout(dest)
    existing = "https://bitbucket.example/wrong-existing.git"
    configured = "https://bitbucket.example/ab.git"
    fake = FakeGit()
    fake.bitbucket = existing
    with pytest.raises(RuntimeError, match="bitbucket remote") as exc_info:
        provision_tool_checkout(
            dest,
            TOOL_MANIFESTS["autobench"],
            bitbucket_url=configured,
            runner=fake,
        )
    message = str(exc_info.value)
    assert existing not in message
    assert configured not in message
    assert "bitbucket.example" not in message
    assert not any(c[:3] == ["git", "remote", "add"] for c in fake.calls)
    assert not any(c[:2] == ["git", "clone"] for c in fake.calls)


def test_reuse_correct_existing_checkout_without_clone(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    _seed_tool_checkout(dest)
    configured = "https://bitbucket.example/ab.git"
    fake = FakeGit()
    fake.bitbucket = configured
    result = provision_tool_checkout(
        dest,
        TOOL_MANIFESTS["autobench"],
        bitbucket_url=configured,
        runner=fake,
    )
    assert result.action == "reused"
    assert result.path == dest.resolve()
    assert not any(c[:2] == ["git", "clone"] for c in fake.calls)
    assert not any(c[:3] == ["git", "remote", "add"] for c in fake.calls)


def test_unrelated_get_url_error_containing_missing_is_not_swallowed(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    _seed_tool_checkout(dest)
    configured = "https://bitbucket.example/ab.git"
    fake = FakeGit()
    fake.bitbucket = configured
    fake.get_url_errors["bitbucket"] = RuntimeError("credential helper missing for bitbucket")
    with pytest.raises(RuntimeError, match="credential helper missing"):
        provision_tool_checkout(
            dest,
            TOOL_MANIFESTS["autobench"],
            bitbucket_url=configured,
            runner=fake,
        )
    assert not any(c[:3] == ["git", "remote", "add"] for c in fake.calls)
    assert any(c == ["git", "remote"] for c in fake.calls)


def test_origin_mismatch_refused(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    _seed_tool_checkout(dest)
    fake = FakeGit()
    fake.origin = "https://github.com/other/autobench.git"
    with pytest.raises(RuntimeError, match="origin points to unexpected"):
        provision_tool_checkout(
            dest,
            TOOL_MANIFESTS["autobench"],
            bitbucket_url="https://bitbucket.example/ab.git",
            runner=fake,
        )
    assert not any(c[:2] == ["git", "clone"] for c in fake.calls)


def test_bootstrap_tag_mismatch_refused(tmp_path: Path) -> None:
    core = tmp_path / "edge-deploy-core"
    core.mkdir()
    (core / ".git").mkdir()
    fake = FakeGit()
    fake.origin = CORE_GITHUB_URL
    fake.head_tag = "v0.0.1"
    fake.head_sha = "a" * 40
    fake.tag_sha = "b" * 40
    with pytest.raises(RuntimeError, match="HEAD is not exactly tag"):
        validate_bootstrap_core(
            core,
            bitbucket_url="https://bitbucket.example/core.git",
            expected_tag=approved_engine_tag(),
            runner=fake,
        )
    assert not any(c[:2] == ["git", "clone"] for c in fake.calls)
