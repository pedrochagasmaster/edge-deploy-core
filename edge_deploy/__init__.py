"""edge-deploy-core: the shared deploy engine for the Edge Node Tools (autobench, robocop).

Everything runs on the Operator's machine: local git and SSH. Remote work goes over
a persistent Paramiko connection by default (``transport: ssh``), with the local
tmux/psmux pane kept as an explicit per-node recovery override (``transport: pane``),
not a universal channel. Per-tool differences live in data (the :class:`ToolProfile`
``edge_deploy.yaml`` committed in each Tool repo) rather than in branches in code.
"""

from __future__ import annotations

from edge_deploy.auth import AuthBroker, ensure_kerberos
from edge_deploy.config import (
    DEFAULT_OPERATOR_CONFIG_PATH,
    DependencyBundleConfig,
    NodeConfig,
    OperatorConfig,
    SmokeCommands,
    ToolProfile,
    load_operator_config,
    load_tool_profile,
)
from edge_deploy.publish import PublishError, PublishResult, publish_snapshot
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
from edge_deploy.ssh_transport import ParamikoSshTransport
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver
from edge_deploy.transport import RemoteTransport, TransferProgress, TransportError
from edge_deploy.verify import run_smoke, verify_after_rollout

__version__ = "1.5.3"

__all__ = [
    "DEFAULT_OPERATOR_CONFIG_PATH",
    "DependencyBundleConfig",
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
    "RemoteTransport",
    "ParamikoSshTransport",
    "TransportError",
    "TransferProgress",
    "AuthBroker",
    "ensure_kerberos",
    "PublishError",
    "PublishResult",
    "publish_snapshot",
    "run_smoke",
    "verify_after_rollout",
    "ReleaseSelection",
    "run_release",
    "__version__",
]
