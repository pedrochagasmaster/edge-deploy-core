"""Config layer: OperatorConfig + ToolProfile dataclasses and their YAML loaders.

Covers both loader backends — PyYAML (primary) and the dependency-free
``_fallback_yaml_load`` (exercised by simulating PyYAML-absent) — on nested profiles.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path

import pytest

from edge_deploy import config
from edge_deploy.config import (
    DEFAULT_TUI_EXIT,
    VALID_TUI_EXIT,
    NodeConfig,
    OperatorConfig,
    SmokeCommands,
    ToolProfile,
)

# Sibling Tool repos live next to edge-deploy-core (…/Projects/{autobench,robocop}).
PROJECTS_ROOT = Path(__file__).resolve().parents[2]

# A nested profile that uses block mappings, block lists, flow lists, comments and quoted
# scalars — and deliberately no YAML escape sequences, so both backends must agree.
SAMPLE_PROFILE_YAML = """\
# sample tool profile
tool: sample
repo_path: /opt/sample
bitbucket_url: https://example.int/scm/sample.git
release_branch: release
runtime_paths: [app.py, "core/**/*.py"]   # flow list with a glob
compile_targets: "app core"
version_files:
  - VERSION
  - pyproject.toml
install_trigger_paths: [requirements.txt, VERSION]
dependency_paths: [requirements.txt]
smoke:
  standard:
    - "./run.sh check"
    - "./run.sh --help"
  deep: []
sensitive_paths: [secrets/]
tui_chrome_regex: "App Title|Main Screen"
tui_exit: none
"""

OPERATOR_YAML = """\
operator_email: operator@example.com
nodes:
  node03:
    host: "user@hde2stl020003.mastercard.int"
    ssh_options: "-p 2222 -o StrictHostKeyChecking=no"
    session: dispatch-prod
  node04:
    host: "user@hde2stl020004.mastercard.int"
tools:
  autobench: /ads_storage/autobench
  robocop: /ads_storage/dispatch
"""


def _force_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import yaml`` raise ModuleNotFoundError so the fallback parser is used."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# ---------------------------------------------------------------------------
# OperatorConfig
# ---------------------------------------------------------------------------


def test_operator_config_from_mapping_builds_nodes_and_tools() -> None:
    operator = OperatorConfig.from_mapping(
        {
            "operator_email": "op@example.com",
            "nodes": {"node03": {"host": "user@edge3", "ssh_options": "-p 2222", "session": "s3"}},
            "tools": {"autobench": "/ads_storage/autobench"},
        }
    )

    assert operator.operator_email == "op@example.com"
    assert operator.node("node03") == NodeConfig(
        host="user@edge3", ssh_options="-p 2222", session="s3", name="node03"
    )
    assert operator.tool_path("autobench") == "/ads_storage/autobench"


def test_node_config_defaults_session_to_node_name() -> None:
    operator = OperatorConfig.from_mapping({"nodes": {"node04": {"host": "user@edge4"}}})

    node = operator.node("node04")
    assert node.host == "user@edge4"
    assert node.ssh_options == ""
    assert node.session == "node04"  # session defaults to the node key
    assert node.name == "node04"


def test_node_config_defaults_to_ssh_transport() -> None:
    node = OperatorConfig.from_mapping(
        {"nodes": {"node03": {"host": "operator@edge"}}}
    ).node("node03")
    assert node.transport == "ssh"


def test_node_config_accepts_explicit_pane_transport() -> None:
    node = OperatorConfig.from_mapping(
        {"nodes": {"node03": {"host": "operator@edge", "transport": "pane"}}}
    ).node("node03")
    assert node.transport == "pane"


def test_node_config_rejects_unknown_transport() -> None:
    with pytest.raises(ValueError, match="transport must be one of: pane, ssh"):
        OperatorConfig.from_mapping(
            {"nodes": {"node03": {"host": "operator@edge", "transport": "magic"}}}
        )


def test_operator_config_unknown_node_and_tool_raise_keyerror() -> None:
    operator = OperatorConfig.from_mapping({"nodes": {"node03": {"host": "h"}}, "tools": {"t": "/p"}})

    with pytest.raises(KeyError, match="Unknown node 'ghost'"):
        operator.node("ghost")
    with pytest.raises(KeyError, match="Unknown tool 'ghost'"):
        operator.tool_path("ghost")


def test_operator_config_load_reads_yaml_file(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(OPERATOR_YAML, encoding="utf-8")

    operator = OperatorConfig.load(config_path)

    assert operator.operator_email == "operator@example.com"
    assert set(operator.nodes) == {"node03", "node04"}
    assert operator.node("node03").session == "dispatch-prod"
    assert operator.node("node04").session == "node04"
    assert operator.tool_path("robocop") == "/ads_storage/dispatch"


# ---------------------------------------------------------------------------
# ToolProfile
# ---------------------------------------------------------------------------


def test_tool_profile_defaults() -> None:
    profile = ToolProfile.from_mapping({"tool": "x", "repo_path": "/p"})

    assert profile.release_branch == "main"
    assert profile.tui_exit == DEFAULT_TUI_EXIT == "ctrl_c"
    assert profile.smoke == SmokeCommands(standard=[], deep=[])
    assert profile.runtime_paths == []


def test_tool_profile_from_mapping_typed_smoke_and_lists() -> None:
    profile = ToolProfile.from_mapping(
        {
            "tool": "sample",
            "repo_path": "/opt/sample",
            "runtime_paths": ["a.py", "core/**/*.py"],
            "smoke": {"standard": ["./run.sh check"], "deep": ["./run.sh deep"]},
            "tui_exit": "dispatch_dynamic",
        }
    )

    assert profile.runtime_paths == ["a.py", "core/**/*.py"]
    assert profile.smoke.standard == ["./run.sh check"]
    assert profile.smoke.deep == ["./run.sh deep"]
    assert profile.tui_exit == "dispatch_dynamic"


def test_tool_profile_load_accepts_directory(tmp_path) -> None:
    (tmp_path / "edge_deploy.yaml").write_text(SAMPLE_PROFILE_YAML, encoding="utf-8")

    from_dir = ToolProfile.load(tmp_path)
    from_file = ToolProfile.load(tmp_path / "edge_deploy.yaml")

    assert from_dir == from_file
    assert from_dir.tool == "sample"
    assert from_dir.release_branch == "release"


def test_valid_tui_exit_values() -> None:
    assert set(VALID_TUI_EXIT) == {"ctrl_c", "dispatch_dynamic", "none"}
    assert DEFAULT_TUI_EXIT in VALID_TUI_EXIT


# ---------------------------------------------------------------------------
# Fallback YAML parser (nested-aware) and the PyYAML-absent code path
# ---------------------------------------------------------------------------


def test_fallback_yaml_load_parses_nested_profile() -> None:
    loaded = config._fallback_yaml_load(SAMPLE_PROFILE_YAML)

    assert loaded["tool"] == "sample"
    assert loaded["runtime_paths"] == ["app.py", "core/**/*.py"]
    assert loaded["version_files"] == ["VERSION", "pyproject.toml"]
    assert loaded["smoke"] == {"standard": ["./run.sh check", "./run.sh --help"], "deep": []}
    assert loaded["sensitive_paths"] == ["secrets/"]
    assert loaded["tui_chrome_regex"] == "App Title|Main Screen"


def test_load_yaml_mapping_falls_back_when_pyyaml_missing(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "edge_deploy.yaml"
    profile_path.write_text(SAMPLE_PROFILE_YAML, encoding="utf-8")

    _force_fallback(monkeypatch)

    loaded = config._load_yaml_mapping(profile_path)
    assert loaded == config._fallback_yaml_load(SAMPLE_PROFILE_YAML)


def test_tool_profile_identical_across_yaml_backends(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "edge_deploy.yaml"
    profile_path.write_text(SAMPLE_PROFILE_YAML, encoding="utf-8")

    with_pyyaml = ToolProfile.load(profile_path)
    _force_fallback(monkeypatch)
    with_fallback = ToolProfile.load(profile_path)

    assert with_pyyaml == with_fallback
    assert with_pyyaml.smoke.standard == ["./run.sh check", "./run.sh --help"]


def test_robocop_profile_agrees_across_backends_with_real_em_dash(monkeypatch) -> None:
    # robocop's ``tui_chrome_regex`` used to store its em-dash as the double-quoted escape
    # ``\u2014``. PyYAML decodes such escapes while the dependency-free fallback keeps them
    # literal, so the two backends parsed the field differently. The profile now ships a real
    # em-dash (U+2014), so both backends must agree byte-for-byte.
    profile_path = PROJECTS_ROOT / "robocop" / "edge_deploy.yaml"
    if not profile_path.exists():
        pytest.skip("robocop profile not available")

    with_pyyaml = ToolProfile.load(profile_path)
    _force_fallback(monkeypatch)
    with_fallback = ToolProfile.load(profile_path)

    assert with_pyyaml == with_fallback

    chrome = with_pyyaml.tui_chrome_regex
    assert "\u2014" in chrome  # a real em-dash (U+2014)...
    assert "\\u2014" not in chrome  # ...not the 6-char ``\u2014`` literal escape
    assert re.search(chrome, "Dispatch \u2014 Impala")  # chrome still matches the live header


# ---------------------------------------------------------------------------
# Real committed Tool Profiles
# ---------------------------------------------------------------------------


def test_real_profiles_expose_expected_contract(real_profile) -> None:
    assert real_profile.tool in {"autobench", "robocop"}
    assert real_profile.repo_path.startswith("/ads_storage/")
    assert real_profile.release_branch == "main"
    assert real_profile.tui_exit in VALID_TUI_EXIT
    assert real_profile.smoke.standard, "every Tool ships at least one standard smoke command"
    assert real_profile.runtime_paths
    assert real_profile.dependency_paths


def test_real_profile_specifics(autobench_profile, robocop_profile) -> None:
    assert autobench_profile.dependency_paths == ["requirements.txt", "constraints.txt"]
    assert autobench_profile.tui_exit == "ctrl_c"
    assert autobench_profile.sensitive_paths == []

    assert robocop_profile.dependency_paths == ["requirements.txt"]
    assert robocop_profile.tui_exit == "dispatch_dynamic"
    assert robocop_profile.sensitive_paths == ["scr/"]


def test_autobench_profile_loads_identically_across_backends(monkeypatch) -> None:
    profile_path = PROJECTS_ROOT / "autobench" / "edge_deploy.yaml"
    if not profile_path.exists():
        pytest.skip("autobench profile not available")

    with_pyyaml = ToolProfile.load(profile_path)
    _force_fallback(monkeypatch)
    with_fallback = ToolProfile.load(profile_path)

    # autobench uses no YAML escape sequences, so both backends agree completely.
    assert with_pyyaml == with_fallback
