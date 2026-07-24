from __future__ import annotations

from dataclasses import dataclass

import edge_deploy

CANONICAL_TOOLS: tuple[str, ...] = ("autobench", "robocop")
TOOL_ALIASES: dict[str, str] = {
    "autobench": "autobench",
    "robocop": "robocop",
    "dispatch": "robocop",
}
DISPLAY_NAMES: dict[str, str] = {
    "autobench": "Autobench",
    "robocop": "Dispatch",
}
CORE_GITHUB_URL = "https://github.com/pedrochagasmaster/edge-deploy-core.git"
ONBOARDING_STAGES: tuple[str, ...] = (
    "prerequisites",
    "config",
    "repositories",
    "readiness",
    "practice",
    "complete",
)


@dataclass(frozen=True)
class ToolManifest:
    tool_id: str
    display_name: str
    github_url: str
    default_dirname: str
    profile_filename: str = "edge_deploy.yaml"
    local_check_relative: str = "tools/dev/local_check.ps1"


TOOL_MANIFESTS: dict[str, ToolManifest] = {
    "autobench": ToolManifest(
        tool_id="autobench",
        display_name="Autobench",
        github_url="https://github.com/pedrochagasmaster/autobench.git",
        default_dirname="autobench",
    ),
    "robocop": ToolManifest(
        tool_id="robocop",
        display_name="Dispatch",
        github_url="https://github.com/pedrochagasmaster/robocop.git",
        default_dirname="robocop",
    ),
}


def normalize_tool_id(raw: str) -> str:
    key = str(raw).strip().lower()
    try:
        return TOOL_ALIASES[key]
    except KeyError as exc:
        supported = ", ".join((*CANONICAL_TOOLS, "dispatch"))
        raise ValueError(f"unknown tool {raw!r}; supported: {supported}") from exc


def approved_engine_tag() -> str:
    return f"v{edge_deploy.__version__}"
