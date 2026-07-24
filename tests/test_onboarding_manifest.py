from pathlib import Path

import edge_deploy
import edge_deploy.onboarding.manifest as mod
from edge_deploy.onboarding.manifest import (
    CANONICAL_TOOLS,
    CORE_GITHUB_URL,
    DISPLAY_NAMES,
    TOOL_MANIFESTS,
    approved_engine_tag,
    normalize_tool_id,
)


def test_dispatch_alias_normalizes_to_robocop() -> None:
    assert normalize_tool_id("dispatch") == "robocop"
    assert normalize_tool_id("Dispatch") == "robocop"
    assert normalize_tool_id("robocop") == "robocop"
    assert normalize_tool_id("autobench") == "autobench"


def test_unknown_tool_rejected() -> None:
    try:
        normalize_tool_id("not-a-tool")
    except ValueError as exc:
        assert "not-a-tool" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_canonical_tools_and_display_names() -> None:
    assert CANONICAL_TOOLS == ("autobench", "robocop")
    assert set(TOOL_MANIFESTS) == {"autobench", "robocop"}
    assert DISPLAY_NAMES["robocop"] == "Dispatch"
    assert TOOL_MANIFESTS["autobench"].github_url.endswith("autobench.git")
    assert TOOL_MANIFESTS["robocop"].github_url.endswith("robocop.git")
    assert TOOL_MANIFESTS["robocop"].default_dirname == "robocop"
    assert CORE_GITHUB_URL.endswith("edge-deploy-core.git")


def test_approved_engine_tag_tracks_package_version() -> None:
    assert approved_engine_tag() == f"v{edge_deploy.__version__}"


def test_manifest_module_is_under_package_for_engine_identity() -> None:
    package_dir = Path(edge_deploy.__file__).resolve().parent
    assert package_dir in Path(mod.__file__).resolve().parents
