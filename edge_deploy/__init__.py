"""edge-deploy-core: the shared deploy engine for the Edge Node Tools (autobench, robocop).

Everything runs on the Operator's machine: local git, a local tmux/psmux pane, and SSH.
Per-tool differences live in data (the :class:`ToolProfile` ``edge_deploy.yaml`` committed
in each Tool repo) rather than in branches in code.
"""

from __future__ import annotations

from edge_deploy.config import (
    DEFAULT_OPERATOR_CONFIG_PATH,
    NodeConfig,
    OperatorConfig,
    SmokeCommands,
    ToolProfile,
    load_operator_config,
    load_tool_profile,
)
from edge_deploy.reporting import OperationReport, ReportCheck, redact, write_report
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver

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
    "redact",
    "write_report",
    "AuthenticationError",
    "SessionGoneError",
    "TmuxDriver",
    "__version__",
]
