from pathlib import Path

from edge_deploy.cli import build_parser, main


def test_onboard_parser_accepts_dispatch_alias() -> None:
    args = build_parser().parse_args(
        [
            "onboard",
            "--config",
            "C:/secure/operator.yaml",
            "--tool",
            "dispatch",
            "--tool",
            "autobench",
        ]
    )
    assert args.command == "onboard"
    assert args.tool == ["dispatch", "autobench"]


def test_yes_requires_restart() -> None:
    # Validated in main() after parse (not by argparse choices).
    code = main(["onboard", "--config", "x.yaml", "--yes"])
    assert code == 2


def test_onboard_does_not_require_operator_config_load(monkeypatch, tmp_path: Path) -> None:
    called = {"load": False, "run": False}

    def fake_load(path):
        called["load"] = True
        raise AssertionError("OperatorConfig.load must not run for onboard")

    def fake_run(args):
        called["run"] = True
        assert args.config.endswith("private.yaml")
        return 0

    monkeypatch.setattr("edge_deploy.cli.OperatorConfig.load", fake_load)
    monkeypatch.setattr("edge_deploy.cli.run_onboarding", fake_run)
    private = tmp_path / "private.yaml"
    private.write_text("operator_email: a@b.com\nnodes: {}\n", encoding="utf-8")
    code = main(["onboard", "--config", str(private), "--tool", "autobench"])
    assert code == 0
    assert called["run"] is True
    assert called["load"] is False
