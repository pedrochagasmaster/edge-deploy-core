"""Network posture probes with injected connect (no real network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from edge_deploy.config import NodeConfig, OperatorConfig
from edge_deploy.posture import (
    PHASE_CAPABILITIES,
    PHASE_ENDPOINTS,
    PHASE_GIT_PROBES,
    POSTURES,
    Endpoint,
    PostureError,
    describe_phase_posture,
    endpoints_for,
    git_probe_command,
    git_probe_failures,
    posture_failures,
    postures_satisfying,
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


# ---------------------------------------------------------------------------
# The five-posture capability model (ADR-0013).
# ---------------------------------------------------------------------------


def test_postures_match_the_five_firewall_states() -> None:
    assert POSTURES == {
        "baseline": {"github-read"},
        "edge-vpn": {"github-read", "edge"},
        "bitbucket-vpn": {"github-read", "bitbucket"},
        "both-vpns": {"github-read", "bitbucket", "edge"},
        "firewall-off": {"github-read", "github-write"},
    }


def test_github_read_is_available_in_every_posture() -> None:
    assert all("github-read" in granted for granted in POSTURES.values())


def test_no_posture_grants_github_write_and_any_vpn() -> None:
    for granted in POSTURES.values():
        if "github-write" in granted:
            assert "bitbucket" not in granted
            assert "edge" not in granted


def test_phase_capabilities_match_what_each_phase_does() -> None:
    assert PHASE_CAPABILITIES == {
        "verify": {"github-read"},
        "publish": {"bitbucket"},
        "deploy": {"bitbucket", "edge"},
        "tag_github": {"github-write"},
        "tag_bitbucket": {"bitbucket"},
    }


def test_postures_satisfying_each_phase() -> None:
    assert postures_satisfying("verify") == (
        "baseline",
        "edge-vpn",
        "bitbucket-vpn",
        "both-vpns",
        "firewall-off",
    )
    assert postures_satisfying("publish") == ("bitbucket-vpn", "both-vpns")
    assert postures_satisfying("deploy") == ("both-vpns",)
    assert postures_satisfying("tag_bitbucket") == ("bitbucket-vpn", "both-vpns")
    assert postures_satisfying("tag_github") == ("firewall-off",)


def test_describe_phase_posture_names_or_any() -> None:
    assert describe_phase_posture("verify") == "any"
    assert describe_phase_posture("publish") == "bitbucket-vpn or both-vpns"
    assert describe_phase_posture("deploy") == "both-vpns"
    assert describe_phase_posture("tag_bitbucket") == "bitbucket-vpn or both-vpns"
    assert describe_phase_posture("tag_github") == "firewall-off"


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
        next_command="py -m edge_deploy verify --run run-1",
        connect=fake_connect,
    )


def test_require_posture_unreachable_edge_node_matches_d7_message() -> None:
    operator = _operator_with_two_nodes()
    next_command = "py -m edge_deploy deploy --run run-1 --nodes node04"

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
        "phase 'deploy' requires posture [both-vpns]; "
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


# ---------------------------------------------------------------------------
# Protocol-level git probes (ADR-0012): TCP connects lie behind the proxy.
# ---------------------------------------------------------------------------


def test_phase_git_probes_match_each_phase_access_direction() -> None:
    assert PHASE_GIT_PROBES == {
        "verify": {"github-api": ("origin", "read")},
        "publish": {"bitbucket": ("bitbucket", "write")},
        "deploy": {"bitbucket": ("bitbucket", "read")},
        "tag_github": {"github": ("origin", "write")},
        "tag_bitbucket": {"bitbucket": ("bitbucket", "write")},
    }


def test_git_probe_command_write_is_dry_run_force_push() -> None:
    command = git_probe_command("origin", "write")

    assert command[:3] == ["git", "push", "--dry-run"]
    assert "--force" in command
    assert command[-1] == "HEAD:refs/edge-deploy/posture-probe"


def test_git_probe_command_read_is_ls_remote_head() -> None:
    assert git_probe_command("bitbucket", "read") == ["git", "ls-remote", "bitbucket", "HEAD"]


def test_git_probe_failures_describe_command_and_exit_code(tmp_path) -> None:
    ran: list[tuple[list[str], object]] = []

    def failing_runner(command: list[str], repo_root) -> int:
        ran.append((command, repo_root))
        return 128

    failures = git_probe_failures("tag_github", tmp_path, runner=failing_runner)

    assert len(ran) == 1
    assert len(failures) == 1
    assert failures[0].startswith("github (")
    assert "exited 128" in failures[0]


def test_git_probe_failures_empty_without_repo_root() -> None:
    def exploding_runner(command: list[str], repo_root) -> int:
        raise AssertionError("git probes must not run without a checkout")

    assert git_probe_failures("tag_github", None, runner=exploding_runner) == []


def test_posture_failures_skips_tcp_for_git_probed_endpoints(tmp_path) -> None:
    connected_hosts: list[str] = []

    def tracking_connect(address: tuple[str, int], timeout: float) -> object:
        connected_hosts.append(address[0])
        return SimpleNamespace(close=lambda: None)

    failures = posture_failures(
        "publish",
        None,
        connect=tracking_connect,
        repo_root=tmp_path,
        git_runner=lambda command, repo_root: 0,
    )

    assert failures == []
    # bitbucket is protocol-probed, so no TCP connect is made for it.
    assert connected_hosts == []


def test_require_posture_raises_on_git_probe_failure(tmp_path) -> None:
    def fake_connect(address: tuple[str, int], timeout: float) -> object:
        return SimpleNamespace(close=lambda: None)

    with pytest.raises(PostureError) as exc_info:
        require_posture(
            "tag_github",
            None,
            next_command="py -m edge_deploy tag-github --run run-1",
            connect=fake_connect,
            repo_root=tmp_path,
            git_runner=lambda command, repo_root: 128,
        )

    message = str(exc_info.value)
    assert "phase 'tag_github' requires posture [firewall-off]" in message
    assert "exited 128" in message
