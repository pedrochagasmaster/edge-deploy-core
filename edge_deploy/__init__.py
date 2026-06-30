"""edge-deploy-core: the shared deploy engine for the Edge Node Tools (autobench, robocop).

Everything runs on the Operator's machine: local git, a local tmux/psmux pane, and SSH.
Per-tool differences live in data (the :class:`ToolProfile` ``edge_deploy.yaml`` committed
in each Tool repo) rather than in branches in code.
"""

from __future__ import annotations

from edge_deploy.auth import authenticate_node, ensure_kerberos
from edge_deploy.config import (
    DEFAULT_OPERATOR_CONFIG_PATH,
    NodeConfig,
    OperatorConfig,
    SmokeCommands,
    ToolProfile,
    load_operator_config,
    load_tool_profile,
)
from edge_deploy.publish import PublishError, PublishResult, build_snapshot_message, publish_snapshot
from edge_deploy.release import ReleaseSelection, run_release
from edge_deploy.reporting import (
    RELEASE_SCHEMA,
    OperationReport,
    ReleaseReport,
    ReportCheck,
    redact,
    write_release_report,
    write_report,
)
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver
from edge_deploy.verify import run_smoke, verify_after_rollout

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_OPERATOR_CONFIG_PATH",
    "NodeConfig",
    "OperatorConfig",
    "SmokeCommands",
    "ToolProfile",
    "load_operator_config",
    "load_tool_profile",
    "OperationReport",
    "ReportCheck",
    "ReleaseReport",
    "RELEASE_SCHEMA",
    "redact",
    "write_report",
    "write_release_report",
    "AuthenticationError",
    "SessionGoneError",
    "TmuxDriver",
    "authenticate_node",
    "ensure_kerberos",
    "PublishError",
    "PublishResult",
    "publish_snapshot",
    "build_snapshot_message",
    "run_smoke",
    "verify_after_rollout",
    "ReleaseSelection",
    "run_release",
    "__version__",
]
