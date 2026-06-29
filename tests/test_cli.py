"""Thin CLI surface: argparse wiring, config resolution, and command dispatch.

Network and tmux are faked, so ``rollout`` / ``drift`` / ``preflight`` run end to end with
no nodes. A subprocess smoke test exercises the real ``python -m edge_deploy`` entry point.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_ROOT = Path(__file__).resolve().parents[2]

OPERATOR_CONFIG = """\
operator_email: operator@example.com
nodes:
  node03:
    host: "user@edge.example"
    ssh_options: "-p 2222"
tools:
  autobench: {autobench_path}
"""


def _ok_addrinfo(host: str, port: int, *, type: int):  # noqa: A002 - mirrors socket.getaddrinfo
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port))]


def _raise_timeout(address: tuple[str, int], timeout: float) -> object:
    raise TimeoutError("timed out")


def _write_operator_config(tmp_path: Path) -> Path:
    autobench_path = (PROJECTS_ROOT / "autobench").as_posix()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(OPERATOR_CONFIG.format(autobench_path=autobench_path), encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_parses_rollout_args() -> None:
    args = cli.build_parser().parse_args(
        ["rollout", "--tool", "autobench", "--node", "node03", "--commit", "abc123"]
    )

    assert args.command == "rollout"
    assert args.tool == "autobench"
    assert args.node == "node03"
    assert args.commit == "abc123"
    assert args.install == "auto"


def test_parser_parses_drift_and_preflight() -> None:
    drift_args = cli.build_parser().parse_args(["drift", "--tool", "t", "--node", "n", "--commit", "c"])
    preflight_args = cli.build_parser().parse_args(["preflight", "--node", "n"])

    assert drift_args.command == "drift"
    assert preflight_args.command == "preflight"


def test_parser_help_lists_all_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "rollout" in help_text
    assert "drift" in help_text
    assert "preflight" in help_text


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


# ---------------------------------------------------------------------------
# main() error handling
# ---------------------------------------------------------------------------


def test_main_missing_config_returns_2(tmp_path, capsys) -> None:
    rc = cli.main(["--config", str(tmp_path / "nope.yaml"), "preflight", "--node", "node03"])

    assert rc == 2
    assert "Operator config not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# preflight command (no real network)
# ---------------------------------------------------------------------------


def test_preflight_command_reports_failure(tmp_path, capsys, monkeypatch) -> None:
    config_path = _write_operator_config(tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)
    monkeypatch.setattr(socket, "create_connection", _raise_timeout)

    rc = cli.main(["--config", str(config_path), "preflight", "--node", "node03", "--timeout", "3"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "Endpoint: edge.example:2222" in out
    assert "Resolved addresses: 10.0.0.5" in out


def test_preflight_command_writes_json_report(tmp_path, monkeypatch) -> None:
    config_path = _write_operator_config(tmp_path)
    report_path = tmp_path / "reports" / "preflight.json"
    monkeypatch.setattr(socket, "getaddrinfo", _ok_addrinfo)
    monkeypatch.setattr(socket, "create_connection", _raise_timeout)

    rc = cli.main(
        [
            "--config",
            str(config_path),
            "preflight",
            "--node",
            "node03",
            "--timeout",
            "3",
            "--json-report",
            str(report_path),
        ]
    )

    assert rc == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["operation"] == "preflight"
    assert report["status"] == "blocked"
    # The node name comes from the operator-config key (NodeConfig.name), not the host.
    assert report["node"] == "node03"
    assert report["endpoint"] == "edge.example:2222"
    assert report["connected"] is False


# ---------------------------------------------------------------------------
# rollout command end to end (fake authenticated pane)
# ---------------------------------------------------------------------------


def _patch_driver_factory(monkeypatch, fake) -> None:
    monkeypatch.setattr(
        cli, "TmuxDriver", SimpleNamespace(from_node_and_profile=lambda node, profile, **kwargs: fake)
    )


def test_rollout_command_rolls_out_with_fake_pane(tmp_path, fake_tmux, monkeypatch) -> None:
    if not (PROJECTS_ROOT / "autobench" / "edge_deploy.yaml").exists():
        pytest.skip("autobench profile not available")

    config_path = _write_operator_config(tmp_path)
    commit = "d" * 40
    fake = fake_tmux(head_commits=["0" * 40, commit], changed_paths=["benchmark.py"])
    _patch_driver_factory(monkeypatch, fake)

    rc = cli.main(
        ["--config", str(config_path), "rollout", "--tool", "autobench", "--node", "node03", "--commit", commit]
    )

    assert rc == 0
    assert fake.ran("./update.sh")


def test_rollout_command_refused_returns_1(tmp_path, fake_tmux, monkeypatch) -> None:
    if not (PROJECTS_ROOT / "autobench" / "edge_deploy.yaml").exists():
        pytest.skip("autobench profile not available")

    config_path = _write_operator_config(tmp_path)
    fake = fake_tmux(head_commits=["0" * 40], changed_paths=["requirements.txt"])
    _patch_driver_factory(monkeypatch, fake)

    rc = cli.main(
        ["--config", str(config_path), "rollout", "--tool", "autobench", "--node", "node03", "--commit", "d" * 40]
    )

    assert rc == 1
    assert not fake.ran("./update.sh")


# ---------------------------------------------------------------------------
# python -m edge_deploy entry point
# ---------------------------------------------------------------------------


def test_dunder_main_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "edge_deploy", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "rollout" in result.stdout


def test_dunder_main_requires_subcommand() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "edge_deploy"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
