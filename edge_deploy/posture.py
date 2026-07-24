"""Workstation postures and the network probes that gate release phases.

Five postures (ADR-0013). GitHub *read* (pull/fetch) works in every posture;
GitHub *write* requires the firewall off, which drops both VPNs. The
Bitbucket and Edge VPNs are independent and may be held together:

===============  ===========  ============  =========  ====
Posture          github read  github write  bitbucket  edge
===============  ===========  ============  =========  ====
baseline         yes          no            no         no
edge-vpn         yes          no            no         yes
bitbucket-vpn    yes          no            yes        no
both-vpns        yes          no            yes        yes
firewall-off     yes          yes           no         no
===============  ===========  ============  =========  ====

Phases declare required *capabilities*; the satisfying postures are derived.
Probing stays capability-level (protocol git probes per ADR-0012, TCP for
Edge SSH) because no probe can name the posture directly — the corporate
proxy accepts TCP connects in every posture.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from edge_deploy.config import OperatorConfig
from edge_deploy.preflight import endpoint_from_node

# Capability names: "github-read", "github-write", "bitbucket", "edge".
# Bitbucket and Edge need no read/write split: a posture grants all their
# actions or none.
POSTURES: dict[str, frozenset[str]] = {
    "baseline": frozenset({"github-read"}),
    "edge-vpn": frozenset({"github-read", "edge"}),
    "bitbucket-vpn": frozenset({"github-read", "bitbucket"}),
    "both-vpns": frozenset({"github-read", "bitbucket", "edge"}),
    "firewall-off": frozenset({"github-read", "github-write"}),
}

PHASE_CAPABILITIES: dict[str, frozenset[str]] = {
    "verify": frozenset({"github-read"}),
    "publish": frozenset({"bitbucket"}),
    "deploy": frozenset({"bitbucket", "edge"}),
    "tag_github": frozenset({"github-write"}),
    "tag_bitbucket": frozenset({"bitbucket"}),
}


def postures_satisfying(phase: str) -> tuple[str, ...]:
    """Posture names (in POSTURES order) whose capabilities cover ``phase``."""
    required = PHASE_CAPABILITIES[phase]
    return tuple(name for name, granted in POSTURES.items() if required <= granted)


def describe_phase_posture(phase: str) -> str:
    """Human posture requirement for ``phase``: 'any', one name, or 'a or b'."""
    names = postures_satisfying(phase)
    if len(names) == len(POSTURES):
        return "any"
    return " or ".join(names)


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

# Protocol-level git probes (ADR-0012). The corporate proxy accepts TCP
# connects in every posture and only fails at the HTTP layer, so a TCP probe
# cannot see posture at all for the git endpoints. Each git endpoint is probed
# with the protocol and access direction the phase actually uses:
# phase -> endpoint key -> (git remote name, "read" | "write").
PHASE_GIT_PROBES: dict[str, dict[str, tuple[str, str]]] = {
    "verify": {"github-api": ("origin", "read")},
    "publish": {"bitbucket": ("bitbucket", "write")},
    "deploy": {"bitbucket": ("bitbucket", "read")},
    "tag_github": {"github": ("origin", "write")},
    "tag_bitbucket": {"bitbucket": ("bitbucket", "write")},
}

_PROBE_REF = "refs/edge-deploy/posture-probe"
_GIT_PROBE_TIMEOUT = 20.0

SocketConnector = Callable[[tuple[str, int], float], object]
GitProbeRunner = Callable[[list[str], Path], int]


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


def git_probe_command(remote: str, access: str) -> list[str]:
    if access == "write":
        # A dry-run force push negotiates git-receive-pack (the real write
        # path, which requires auth and write-side reachability) without
        # sending a pack or updating any ref. --force plus an explicit
        # destination keeps local branch state (detached HEAD, stale main)
        # from masquerading as a posture failure.
        return ["git", "push", "--dry-run", "--force", remote, f"HEAD:{_PROBE_REF}"]
    return ["git", "ls-remote", remote, "HEAD"]


def _default_git_probe_runner(command: list[str], repo_root: Path) -> int:
    env = dict(os.environ)
    # A probe must never block on an interactive credential prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            timeout=_GIT_PROBE_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1
    return completed.returncode


def git_probe_failures(
    phase: str,
    repo_root: Path | str | None,
    *,
    runner: GitProbeRunner | None = None,
) -> list[str]:
    """Run the protocol-level git probes for ``phase``; describe each failure.

    Returns an empty list when ``repo_root`` is None (no checkout to run git
    in) — callers then rely on TCP probing alone, as before ADR-0012.
    """
    if repo_root is None:
        return []
    run = runner or _default_git_probe_runner
    failures: list[str] = []
    for key, (remote, access) in PHASE_GIT_PROBES.get(phase, {}).items():
        command = git_probe_command(remote, access)
        code = run(command, Path(repo_root))
        if code != 0:
            failures.append(f"{key} ({' '.join(command)} exited {code})")
    return failures


def posture_failures(
    phase: str,
    operator: OperatorConfig | None,
    *,
    connect: SocketConnector = socket.create_connection,
    repo_root: Path | str | None = None,
    git_runner: GitProbeRunner | None = None,
) -> list[str]:
    """All posture failures for ``phase``: TCP for edge/unprobed endpoints,
    protocol-level git probes for git endpoints when a checkout is available."""
    git_keys = set(PHASE_GIT_PROBES.get(phase, {})) if repo_root is not None else set()
    tcp_endpoints = [
        endpoint
        for endpoint in endpoints_for(phase, operator)
        if endpoint.key not in git_keys
    ]
    failures = [
        f"{endpoint.host}:{endpoint.port}"
        for endpoint in probe(tcp_endpoints, connect=connect)
    ]
    failures.extend(git_probe_failures(phase, repo_root, runner=git_runner))
    return failures


def require_posture(
    phase: str,
    operator: OperatorConfig | None,
    *,
    next_command: str,
    connect: SocketConnector = socket.create_connection,
    repo_root: Path | str | None = None,
    git_runner: GitProbeRunner | None = None,
) -> None:
    failures = posture_failures(
        phase,
        operator,
        connect=connect,
        repo_root=repo_root,
        git_runner=git_runner,
    )
    if not failures:
        return
    raise PostureError(
        f"phase '{phase}' requires posture [{describe_phase_posture(phase)}]; "
        f"unreachable: {', '.join(failures)}.\n"
        f"Switch the firewall posture, then re-run: {next_command}"
    )
