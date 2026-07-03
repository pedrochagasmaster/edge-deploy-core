"""Network posture probes for release phases (D6/D7)."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Callable

from edge_deploy.config import OperatorConfig
from edge_deploy.preflight import endpoint_from_node

_STATIC_ENDPOINTS: dict[str, tuple[str, int]] = {
    "github": ("github.com", 443),
    "github-api": ("api.github.com", 443),
    "bitbucket": ("scm.mastercard.int", 443),
}

PHASE_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "verify": ("github-api",),
    "publish": ("bitbucket",),
    "deploy": ("bitbucket", "edge"),
    "tag_github": ("github",),
    "tag_bitbucket": ("bitbucket",),
}

SocketConnector = Callable[[tuple[str, int], float], object]


@dataclass(frozen=True)
class Endpoint:
    key: str
    host: str
    port: int


class PostureError(RuntimeError):
    """Raised when required posture endpoints are unreachable."""


def _static_endpoint(key: str) -> Endpoint:
    host, port = _STATIC_ENDPOINTS[key]
    return Endpoint(key=key, host=host, port=port)


def _edge_endpoints(operator: OperatorConfig) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for name in sorted(operator.nodes):
        resolved = endpoint_from_node(operator.nodes[name])
        endpoints.append(Endpoint(key="edge", host=resolved.hostname, port=resolved.port))
    return endpoints


def endpoints_for(phase: str, operator: OperatorConfig | None) -> list[Endpoint]:
    keys = PHASE_ENDPOINTS[phase]
    result: list[Endpoint] = []
    for key in keys:
        if key == "edge":
            if operator is not None:
                result.extend(_edge_endpoints(operator))
        else:
            result.append(_static_endpoint(key))
    return result


def probe(
    endpoints: list[Endpoint],
    *,
    timeout: float = 2.0,
    connect: SocketConnector = socket.create_connection,
) -> list[Endpoint]:
    unreachable: list[Endpoint] = []
    for endpoint in endpoints:
        try:
            connection = connect((endpoint.host, endpoint.port), timeout)
        except (OSError, TimeoutError):
            unreachable.append(endpoint)
            continue
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    return unreachable


def require_posture(
    phase: str,
    operator: OperatorConfig | None,
    *,
    next_command: str,
    connect: SocketConnector = socket.create_connection,
) -> None:
    keys = PHASE_ENDPOINTS[phase]
    unreachable = probe(endpoints_for(phase, operator), connect=connect)
    if not unreachable:
        return
    posture_keys = ", ".join(keys)
    unreachable_hosts = ", ".join(f"{endpoint.host}:{endpoint.port}" for endpoint in unreachable)
    raise PostureError(
        f"phase '{phase}' requires posture [{posture_keys}]; unreachable: {unreachable_hosts}.\n"
        f"Switch the firewall posture, then re-run: {next_command}"
    )
