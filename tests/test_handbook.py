"""Pin the operator handbook to the code it documents.

``docs/edge-deploy-handbook.html`` is the offline Release Operator handbook for
the engine and the console. A command it shows that the CLI does not have, or a
console flag that does not exist, sends a tired operator down a dead end — so
every command token on the page is checked against the real parsers, and the
page's offline promise is enforced.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from edge_console.server import build_arg_parser
from edge_deploy.cli import build_parser

HANDBOOK = Path(__file__).resolve().parent.parent / "docs" / "edge-deploy-handbook.html"
TEXT = HANDBOOK.read_text(encoding="utf-8")


def _cli_subcommands() -> set[str]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("the engine CLI parser has no subcommands")


def test_every_engine_command_in_the_handbook_exists() -> None:
    shown = set(re.findall(r"py -m edge_deploy ([a-z][a-z-]*)", TEXT))
    assert shown, "the handbook shows no engine commands at all?"
    unknown = shown - _cli_subcommands()
    assert not unknown, f"handbook documents engine commands that do not exist: {sorted(unknown)}"


def test_every_console_flag_in_the_handbook_exists() -> None:
    start = TEXT.index('data-page="console"')
    end = TEXT.index("<article", start)
    console_page = TEXT[start:end]
    shown = set(re.findall(r"<code>(--[a-z][a-z-]*)</code>", console_page))
    assert shown, "the console page documents no flags at all?"
    real = {
        option
        for action in build_arg_parser()._actions
        for option in action.option_strings
    }
    unknown = shown - real
    assert not unknown, f"handbook documents console flags that do not exist: {sorted(unknown)}"


def test_the_handbook_is_fully_offline() -> None:
    """The page promises to work from a file share with no network."""
    assert "src=" not in TEXT
    assert 'href="http' not in TEXT
    assert "@import" not in TEXT
    assert "url(" not in TEXT


def test_the_handbook_covers_the_contract_surface() -> None:
    for phase_command in ("verify", "publish-phase", "deploy", "tag-bitbucket", "tag-github"):
        assert f"py -m edge_deploy {phase_command}" in TEXT, phase_command
    for posture in ("baseline", "edge-vpn", "bitbucket-vpn", "both-vpns", "firewall-off"):
        assert posture in TEXT, posture
    assert "same full source SHA" in TEXT, "the invariant must be stated"


def test_the_handbook_version_matches_the_package() -> None:
    pyproject = (HANDBOOK.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1)
    assert f"v{version}" in TEXT, (
        f"the handbook names a version other than {version}; update its pills and footer"
    )
