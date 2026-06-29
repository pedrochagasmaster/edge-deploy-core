"""Local DNS/TCP preflight for a NodeConfig (default SSH port 2222), no real network."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from edge_deploy import preflight
from edge_deploy.config import NodeConfig
from edge_deploy.preflight import build_preflight_report, endpoint_from_node, run_tcp_preflight


def _ok_addrinfo(host: str, port: int, *, type: int):  # noqa: A002 - mirrors socket.getaddrinfo
    assert type == socket.SOCK_STREAM
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port))]


@pytest.mark.parametrize(
    "ssh_options, expected_port",
    [
        ("-p 2222 -o StrictHostKeyChecking=no", 2222),
        ("-p2200", 2200),
        ("-o Port=2022", 2022),
        ("", 2222),  # default SSH port
    ],
)
def test_endpoint_from_node_parses_port(ssh_options, expected_port) -> None:
    node = NodeConfig(host="user@hde2stl020003.mastercard.int", ssh_options=ssh_options)

    endpoint = endpoint_from_node(node)

    assert endpoint.user_host == "user@hde2stl020003.mastercard.int"
    assert endpoint.hostname == "hde2stl020003.mastercard.int"
    assert endpoint.port == expected_port


def test_tcp_preflight_success_without_real_network(monkeypatch) -> None:
    node = NodeConfig(host="user@edge.example", ssh_options="-p 2222")
    connections: list[tuple[tuple[str, int], float]] = []

    def fake_connector(address: tuple[str, int], timeout: float) -> SimpleNamespace:
        connections.append((address, timeout))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)

    result = run_tcp_preflight(node, timeout=3, connector=fake_connector)

    assert result.connected is True
    assert result.resolved_addresses == ("10.0.0.5",)
    assert connections == [(("edge.example", 2222), 3.0)]


def test_tcp_preflight_reports_timeout(monkeypatch) -> None:
    node = NodeConfig(host="user@edge.example", ssh_options="-p 2222")
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)

    def fake_connector(address: tuple[str, int], timeout: float) -> object:
        raise TimeoutError("timed out")

    result = run_tcp_preflight(node, timeout=3, connector=fake_connector)

    assert result.connected is False
    assert result.error == "timed out"
    assert result.resolved_addresses == ("10.0.0.5",)


def test_tcp_preflight_reports_blank_timeout_as_timed_out(monkeypatch) -> None:
    node = NodeConfig(host="user@edge.example", ssh_options="-p 2222")
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)

    def fake_connector(address: tuple[str, int], timeout: float) -> object:
        raise TimeoutError()

    result = run_tcp_preflight(node, timeout=3, connector=fake_connector)

    assert result.connected is False
    assert result.error == "timed out"


def test_tcp_preflight_reports_dns_failure(monkeypatch) -> None:
    node = NodeConfig(host="user@edge.example", ssh_options="-p 2222")

    def boom(host: str, port: int, *, type: int):  # noqa: A002
        raise OSError("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", boom)

    result = run_tcp_preflight(node, connector=lambda a, t: SimpleNamespace(close=lambda: None))

    assert result.connected is False
    assert result.resolved_addresses == ()
    assert "DNS lookup failed" in result.error


def test_build_preflight_report_passed(monkeypatch) -> None:
    node = NodeConfig(host="user@edge.example", ssh_options="-p 2222")
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)
    result = run_tcp_preflight(node, timeout=1, connector=lambda a, t: SimpleNamespace(close=lambda: None))

    payload = build_preflight_report(node, result, repo_path="/ads_storage/x").to_payload()

    assert payload["operation"] == "preflight"
    assert payload["status"] == "passed"
    assert payload["node"] == "edge"
    assert payload["host"] == "user@edge.example"
    assert payload["repo_path"] == "/ads_storage/x"
    assert payload["endpoint"] == "edge.example:2222"
    assert payload["resolved_addresses"] == ["10.0.0.5"]
    assert payload["connected"] is True
    assert payload["checks"][0]["name"] == "tcp_connectivity"
    assert payload["checks"][0]["passed"] is True


def test_build_preflight_report_blocked(monkeypatch) -> None:
    node = NodeConfig(host="user@edge.example", ssh_options="-p 2222")
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)

    def fake_connector(address: tuple[str, int], timeout: float) -> object:
        raise TimeoutError("timed out")

    result = run_tcp_preflight(node, timeout=1, connector=fake_connector)
    payload = build_preflight_report(node, result).to_payload()

    assert payload["status"] == "blocked"
    assert payload["repo_path"] == "not_applicable"
    assert payload["connected"] is False
    assert payload["error"] == "timed out"


def test_preflight_module_default_port_constant() -> None:
    assert preflight.DEFAULT_SSH_PORT == 2222
