"""Local network preflight: DNS + TCP reachability for one Edge Node.

Generalized from robocop's ``preflight.py`` to operate on a :class:`NodeConfig` (SSH port
parsed from ``ssh_options``, defaulting to 2222) instead of the Dispatch harness config.
"""

from __future__ import annotations

import shlex
import socket
from dataclasses import dataclass
from typing import Callable

from edge_deploy.config import NodeConfig
from edge_deploy.reporting import OperationReport, ReportCheck, report_node_name, utc_iso_timestamp

DEFAULT_SSH_PORT = 2222
DEFAULT_TIMEOUT_SECONDS = 15.0

SocketConnector = Callable[[tuple[str, int], float], object]


@dataclass(frozen=True)
class EdgeEndpoint:
    """Resolved SSH endpoint extracted from a NodeConfig."""

    user_host: str
    hostname: str
    port: int


@dataclass(frozen=True)
class TcpPreflightResult:
    """Outcome of one local TCP preflight."""

    endpoint: EdgeEndpoint
    resolved_addresses: tuple[str, ...]
    connected: bool
    error: str = ""


def endpoint_from_node(node: NodeConfig) -> EdgeEndpoint:
    """Return the SSH hostname and port selected by a node's ``host`` / ``ssh_options``."""
    hostname = node.host.rsplit("@", 1)[-1]
    port = DEFAULT_SSH_PORT
    option_parts = shlex.split(node.ssh_options or "")
    for index, part in enumerate(option_parts):
        if part == "-p" and index + 1 < len(option_parts):
            port = int(option_parts[index + 1])
        elif part.startswith("-p") and len(part) > 2:
            port = int(part[2:])
        elif part.startswith("Port="):
            port = int(part.split("=", 1)[1])
    return EdgeEndpoint(user_host=node.host, hostname=hostname, port=port)


def run_tcp_preflight(
    node: NodeConfig,
    *,
    timeout: float | None = None,
    connector: SocketConnector | None = None,
) -> TcpPreflightResult:
    endpoint = endpoint_from_node(node)
    effective_timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS)
    try:
        infos = socket.getaddrinfo(endpoint.hostname, endpoint.port, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    except OSError as exc:
        return TcpPreflightResult(endpoint, (), False, f"DNS lookup failed: {exc}")

    try:
        active_connector = connector or socket.create_connection
        connection = active_connector((endpoint.hostname, endpoint.port), effective_timeout)
    except TimeoutError as exc:
        return TcpPreflightResult(endpoint, addresses, False, str(exc) or "timed out")
    except OSError as exc:
        return TcpPreflightResult(endpoint, addresses, False, str(exc) or "connection failed")

    close = getattr(connection, "close", None)
    if callable(close):
        close()
    return TcpPreflightResult(endpoint, addresses, True)


def build_preflight_report(
    node: NodeConfig,
    result: TcpPreflightResult,
    *,
    repo_path: str = "",
) -> OperationReport:
    return OperationReport(
        operation="preflight",
        status="passed" if result.connected else "blocked",
        node=report_node_name(node),
        host=result.endpoint.user_host,
        repo_path=repo_path or "not_applicable",
        deployment_commit="not_applicable",
        install_decision="not_applicable",
        checks=[
            ReportCheck(
                name="tcp_connectivity",
                passed=result.connected,
                message=result.error or f"TCP {result.endpoint.port} reachable",
                evidence={
                    "endpoint": f"{result.endpoint.hostname}:{result.endpoint.port}",
                    "resolved_addresses": list(result.resolved_addresses),
                },
            )
        ],
        extra={
            "generated_at": utc_iso_timestamp(),
            "endpoint": f"{result.endpoint.hostname}:{result.endpoint.port}",
            "resolved_addresses": list(result.resolved_addresses),
            "connected": result.connected,
            "error": result.error,
        },
    )
