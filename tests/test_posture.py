"""Network posture probes with injected connect (no real network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from edge_deploy.config import NodeConfig, OperatorConfig
from edge_deploy.posture import (
    PHASE_ENDPOINTS,
    Endpoint,
    PostureError,
    endpoints_for,
    probe,
    require_posture,
)


def _operator_with_two_nodes() -> OperatorConfig:
    return OperatorConfig(
        nodes={
            "node03": NodeConfig(
                host="user@hde2stl020003.mastercard.int",
                ssh_options="-p 2222 -o StrictHostKeyChecking=no",
                session="dispatch-prod",
                name="node03",
            ),
            "node04": NodeConfig(
                host="user@hde2stl020004.mastercard.int",
                ssh_options="-p 2222",
                session="dispatch-prod",
                name="node04",
            ),
        }
    )


def test_endpoints_for_unknown_phase_raises_key_error() -> None:
    with pytest.raises(KeyError):
        endpoints_for("no_such_phase", None)


def test_endpoints_for_edge_expansion_one_per_node() -> None:
    operator = _operator_with_two_nodes()

    endpoints = endpoints_for("deploy", operator)

    edge_endpoints = [endpoint for endpoint in endpoints if endpoint.key == "edge"]
    assert len(edge_endpoints) == 2
    assert edge_endpoints[0] == Endpoint(
        key="edge",
        host="hde2stl020003.mastercard.int",
        port=2222,
    )
    assert edge_endpoints[1] == Endpoint(
        key="edge",
        host="hde2stl020004.mastercard.int",
        port=2222,
    )
    assert endpoints[0] == Endpoint(key="bitbucket", host="scm.mastercard.int", port=443)


def test_probe_returns_unreachable_endpoints() -> None:
    endpoints = [
        Endpoint(key="github-api", host="api.github.com", port=443),
        Endpoint(key="bitbucket", host="scm.mastercard.int", port=443),
    ]

    def fake_connect(address: tuple[str, int], timeout: float) -> object:
        if address[0] == "scm.mastercard.int":
            raise OSError("connection refused")
        return SimpleNamespace(close=lambda: None)

    unreachable = probe(endpoints, connect=fake_connect)

    assert unreachable == [endpoints[1]]


def test_require_posture_all_reachable_does_not_raise() -> None:
    def fake_connect(address: tuple[str, int], timeout: float) -> object:
        return SimpleNamespace(close=lambda: None)

    require_posture(
        "verify",
        None,
        next_command="python -m edge_deploy verify --run run-1",
        connect=fake_connect,
    )


def test_require_posture_unreachable_edge_node_matches_d7_message() -> None:
    operator = _operator_with_two_nodes()
    next_command = "python -m edge_deploy deploy --run run-1 --nodes node04"

    def fake_connect(address: tuple[str, int], timeout: float) -> object:
        if address == ("hde2stl020004.mastercard.int", 2222):
            raise TimeoutError("timed out")
        return SimpleNamespace(close=lambda: None)

    with pytest.raises(PostureError) as exc_info:
        require_posture(
            "deploy",
            operator,
            next_command=next_command,
            connect=fake_connect,
        )

    expected = (
        "phase 'deploy' requires posture [bitbucket, edge]; "
        "unreachable: hde2stl020004.mastercard.int:2222.\n"
        f"Switch the firewall posture, then re-run: {next_command}"
    )
    assert str(exc_info.value) == expected


def test_phase_endpoints_matches_d6() -> None:
    assert PHASE_ENDPOINTS == {
        "verify": ("github-api",),
        "publish": ("bitbucket",),
        "deploy": ("bitbucket", "edge"),
        "tag_github": ("github",),
        "tag_bitbucket": ("bitbucket",),
    }
