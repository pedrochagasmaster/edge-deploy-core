"""Two-layer configuration for edge-deploy-core.

Layer 1 — :class:`OperatorConfig` (``%APPDATA%/edge-deploy/config.yaml``):
    Operator identity, Edge Node inventory, and the private audit checkout.

Layer 2 — :class:`ToolProfile` (``edge_deploy.yaml`` committed in each Tool repo):
    Tool-specific, node-independent deploy data (paths, smoke commands, TUI chrome,
    GitHub/Bitbucket URLs, branch, sensitive/dependency paths).

YAML is parsed with PyYAML when available, falling back to a minimal dependency-free
parser (mirroring robocop's ``_fallback_yaml_load``) so command construction and unit
tests stay usable in a stripped-down environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_OPERATOR_CONFIG_PATH = (
    Path(os.environ.get("APPDATA", Path.home() / ".config")) / "edge-deploy" / "config.yaml"
)
TOOL_PROFILE_FILENAME = "edge_deploy.yaml"

# Valid ``tui_exit`` strategies understood by :class:`~edge_deploy.tmux_driver.TmuxDriver`.
VALID_TUI_EXIT = ("ctrl_c", "dispatch_dynamic", "none")
DEFAULT_TUI_EXIT = "ctrl_c"


# ---------------------------------------------------------------------------
# Minimal YAML fallback (mirrors robocop's _fallback_yaml_load, nested-aware)
# ---------------------------------------------------------------------------


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment that is not inside quotes (YAML needs a space before it)."""
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _split_top_level(text: str) -> list[str]:
    """Split a flow collection body on commas that are not nested or quoted."""
    parts: list[str] = []
    depth = 0
    in_single = in_double = False
    current: list[str] = []
    for char in text:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if not in_single and not in_double:
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_value(raw: str) -> Any:
    stripped = raw.strip()
    if stripped == "":
        return None
    if stripped.startswith("[") and stripped.endswith("]"):
        body = stripped[1:-1].strip()
        return [_parse_scalar(item) for item in _split_top_level(body)] if body else []
    if stripped.startswith("{") and stripped.endswith("}"):
        body = stripped[1:-1].strip()
        mapping: dict[str, Any] = {}
        for entry in _split_top_level(body):
            key, sep, value = entry.partition(":")
            if sep:
                mapping[key.strip()] = _parse_scalar(value)
        return mapping
    return _parse_scalar(stripped)


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    first_text = lines[index][1]
    if first_text == "-" or first_text.startswith("- "):
        sequence: list[Any] = []
        while index < len(lines):
            cur_indent, cur_text = lines[index]
            if cur_indent != indent or not (cur_text == "-" or cur_text.startswith("- ")):
                break
            item_text = cur_text[1:].strip()
            if item_text == "":
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
                sequence.append(value)
            else:
                sequence.append(_parse_value(item_text))
                index += 1
        return sequence, index

    mapping: dict[str, Any] = {}
    while index < len(lines):
        cur_indent, cur_text = lines[index]
        if cur_indent != indent or cur_text == "-" or cur_text.startswith("- "):
            break
        key, _sep, rest = cur_text.partition(":")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest == "":
            if index < len(lines) and lines[index][0] > cur_indent:
                value, index = _parse_block(lines, index, lines[index][0])
                mapping[key] = value
            else:
                mapping[key] = None
        else:
            mapping[key] = _parse_value(rest)
    return mapping, index


def _fallback_yaml_load(text: str) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        without_comment = _strip_comment(raw_line)
        if without_comment.strip() == "":
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        lines.append((indent, without_comment.strip()))
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value if isinstance(value, dict) else {}


def _load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return _fallback_yaml_load(text)
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"YAML file must be a mapping: {config_path}")
    return loaded


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


# ---------------------------------------------------------------------------
# Operator config (layer 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeConfig:
    """One Edge Node entry from the operator config.

    Contract fields are ``host``, ``ssh_options`` and ``session``; ``name`` is an
    additive convenience carrying the operator-config key so a NodeConfig can name
    itself in reports/preflight.
    """

    host: str
    ssh_options: str = ""
    session: str = ""
    name: str = ""

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "NodeConfig":
        return cls(
            host=str(data.get("host", "")),
            ssh_options=str(data.get("ssh_options", "") or ""),
            session=str(data.get("session", name)),
            name=name,
        )


@dataclass(frozen=True)
class OperatorConfig:
    """Private operator identity, node inventory, and audit checkout."""

    operator_email: str = ""
    audit_repo: str = ""
    nodes: dict[str, NodeConfig] = field(default_factory=dict)
    # Backward-compatible only for lower-level commands. Normal release infers cwd.
    tools: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OperatorConfig":
        nodes: dict[str, NodeConfig] = {}
        for name, raw in (data.get("nodes") or {}).items():
            node_data = raw if isinstance(raw, Mapping) else {"host": raw}
            nodes[str(name)] = NodeConfig.from_mapping(str(name), node_data)
        tools: dict[str, str] = {}
        for name, raw in (data.get("tools") or {}).items():
            if isinstance(raw, Mapping):
                tools[str(name)] = str(raw.get("path", ""))
            else:
                tools[str(name)] = str(raw)
        return cls(
            operator_email=str(data.get("operator_email", "") or ""),
            audit_repo=str(data.get("audit_repo", "") or ""),
            nodes=nodes,
            tools=tools,
        )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_OPERATOR_CONFIG_PATH) -> "OperatorConfig":
        return cls.from_mapping(_load_yaml_mapping(path))

    def node(self, name: str) -> NodeConfig:
        try:
            return self.nodes[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.nodes)) or "(none)"
            raise KeyError(f"Unknown node {name!r}; configured nodes: {available}") from exc

    def tool_path(self, name: str) -> str:
        try:
            return self.tools[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.tools)) or "(none)"
            raise KeyError(f"Unknown tool {name!r}; configured tools: {available}") from exc


def load_operator_config(path: str | Path = DEFAULT_OPERATOR_CONFIG_PATH) -> OperatorConfig:
    return OperatorConfig.load(path)


# ---------------------------------------------------------------------------
# Tool Profile (layer 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmokeCommands:
    """Per-tool smoke commands, split by auth requirement."""

    standard: list[str] = field(default_factory=list)
    deep: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SmokeCommands":
        data = data or {}
        return cls(
            standard=_as_str_list(data.get("standard")),
            deep=_as_str_list(data.get("deep")),
        )


@dataclass(frozen=True)
class DependencyBundleConfig:
    """Target and input contract for a Tool's externally delivered wheel bundle."""

    requirements_file: str = "requirements.txt"
    constraints_file: str = ""
    python_version: str = "3.10"
    implementation: str = "cp"
    abi: str = "cp310"
    platform: str = "manylinux2014_x86_64"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "DependencyBundleConfig | None":
        if not data:
            return None
        return cls(
            requirements_file=str(data.get("requirements_file", "requirements.txt")),
            constraints_file=str(data.get("constraints_file", "") or ""),
            python_version=str(data.get("python_version", "3.10")),
            implementation=str(data.get("implementation", "cp")),
            abi=str(data.get("abi", "cp310")),
            platform=str(data.get("platform", "manylinux2014_x86_64")),
        )


@dataclass(frozen=True)
class ToolProfile:
    """Deploy-specific differences for one Tool, committed as ``edge_deploy.yaml``."""

    tool: str = ""
    repo_path: str = ""
    github_url: str = ""
    bitbucket_url: str = ""
    release_branch: str = "main"
    runtime_paths: list[str] = field(default_factory=list)
    compile_targets: str = ""
    version_files: list[str] = field(default_factory=list)
    install_trigger_paths: list[str] = field(default_factory=list)
    dependency_paths: list[str] = field(default_factory=list)
    dependency_bundle: DependencyBundleConfig | None = None
    smoke: SmokeCommands = field(default_factory=SmokeCommands)
    sensitive_paths: list[str] = field(default_factory=list)
    tui_chrome_regex: str = ""
    tui_exit: str = DEFAULT_TUI_EXIT

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ToolProfile":
        return cls(
            tool=str(data.get("tool", "")),
            repo_path=str(data.get("repo_path", "")),
            github_url=str(data.get("github_url", "")),
            bitbucket_url=str(data.get("bitbucket_url", "")),
            release_branch=str(data.get("release_branch", "main") or "main"),
            runtime_paths=_as_str_list(data.get("runtime_paths")),
            compile_targets=str(data.get("compile_targets", "") or ""),
            version_files=_as_str_list(data.get("version_files")),
            install_trigger_paths=_as_str_list(data.get("install_trigger_paths")),
            dependency_paths=_as_str_list(data.get("dependency_paths")),
            dependency_bundle=DependencyBundleConfig.from_mapping(data.get("dependency_bundle")),
            smoke=SmokeCommands.from_mapping(data.get("smoke")),
            sensitive_paths=_as_str_list(data.get("sensitive_paths")),
            tui_chrome_regex=str(data.get("tui_chrome_regex", "") or ""),
            tui_exit=str(data.get("tui_exit", DEFAULT_TUI_EXIT) or DEFAULT_TUI_EXIT),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ToolProfile":
        profile_path = Path(path)
        if profile_path.is_dir():
            profile_path = profile_path / TOOL_PROFILE_FILENAME
        return cls.from_mapping(_load_yaml_mapping(profile_path))


def load_tool_profile(path: str | Path) -> ToolProfile:
    """Load a Tool Profile from an ``edge_deploy.yaml`` file or the repo dir containing it."""
    return ToolProfile.load(path)
