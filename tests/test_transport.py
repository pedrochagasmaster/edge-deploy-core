from pathlib import Path

from edge_deploy.tmux_driver import TmuxDriver
from edge_deploy.transport import RemoteTransport, TransferProgress


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
    # release.py and __init__.py retain legitimate TmuxDriver references (the
    # default driver_factory construction and the compatibility re-export,
    # respectively) until Task 7 rewires construction through transport_for_node.
    allowed = {"tmux_driver.py", "transport.py", "cli.py", "release.py", "__init__.py"}
    offenders = []
    for path in package.glob("*.py"):
        if path.name not in allowed and "TmuxDriver" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []
