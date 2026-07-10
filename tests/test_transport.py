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
