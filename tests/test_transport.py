from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from edge_deploy.tmux_driver import TmuxDriver
from edge_deploy.transport import (
    RemoteTransport,
    TransferProgress,
    TransportUnavailable,
    transport_for_node,
)


def test_tmux_driver_satisfies_remote_transport() -> None:
    driver = TmuxDriver("operator@edge", "edge-node03", "/ads_storage/tool")
    assert isinstance(driver, RemoteTransport)


def test_transfer_progress_percent_and_rate() -> None:
    progress = TransferProgress(bytes_sent=25, total_bytes=100, elapsed_s=2.0)
    assert progress.percent == 25.0
    assert progress.bytes_per_second == 12.5


def test_transfer_progress_handles_empty_file() -> None:
    progress = TransferProgress(bytes_sent=0, total_bytes=0, elapsed_s=0.0)
    assert progress.percent == 100.0
    assert progress.bytes_per_second == 0.0


def test_engine_modules_do_not_annotate_tmx_driver_directly() -> None:
    package = Path(__file__).parents[1] / "edge_deploy"
    # __init__.py retains a legitimate TmuxDriver compatibility re-export.
    # release.py's own reference to TmuxDriver is gone as of Task 7 (construction
    # is now routed through transport_for_node).
    allowed = {"tmux_driver.py", "transport.py", "cli.py", "__init__.py"}
    offenders = []
    for path in package.glob("*.py"):
        if path.name not in allowed and "TmuxDriver" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []


def test_transport_for_node_selects_ssh_by_default() -> None:
    node = SimpleNamespace(transport="ssh", name="node03")
    profile = SimpleNamespace()
    sentinel = object()
    with patch(
        "edge_deploy.ssh_transport.ParamikoSshTransport.from_node_and_profile",
        return_value=sentinel,
    ) as ctor:
        result = transport_for_node(node, profile)
    ctor.assert_called_once_with(node, profile, retries=2)
    assert result is sentinel


def test_transport_for_node_selects_pane_when_configured() -> None:
    node = SimpleNamespace(transport="pane", name="node03")
    profile = SimpleNamespace()
    sentinel = object()
    with patch.object(
        TmuxDriver, "from_node_and_profile", return_value=sentinel
    ) as ctor:
        result = transport_for_node(node, profile, pane_log_path=Path("pane.log"))
    ctor.assert_called_once_with(
        node, profile, retries=2, pane_log_path=Path("pane.log")
    )
    assert result is sentinel


def test_transport_for_node_rejects_unknown_transport() -> None:
    node = SimpleNamespace(transport="magic", name="node03")
    profile = SimpleNamespace()
    with pytest.raises(TransportUnavailable, match="unsupported transport"):
        transport_for_node(node, profile)
