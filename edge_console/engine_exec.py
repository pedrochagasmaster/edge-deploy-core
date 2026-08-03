"""Bind console child processes to the ``edge_deploy`` source the console loaded.

The console imports ``edge_deploy`` from whatever source is on its own path.
Action children used to run ``python -m edge_deploy`` with a tool checkout as
``cwd``, so Python could rediscover a different installed package. Every engine
child argv is built here with an explicit source root inserted ahead of module
execution; the tool checkout stays the working directory for profiles, git, and
ledgers.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

# Allowed import: used only to locate the already-loaded package via __file__.
from edge_deploy.config import load_operator_config

# Runs as ``python -c <bootstrap> <source-root> <edge-deploy-args...>``.
# ``sys.argv.pop(1)`` removes the source root before ``run_module`` so the CLI
# sees the same arguments ``python -m edge_deploy …`` would.
_ENGINE_BOOTSTRAP = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('edge_deploy',run_name='__main__')"
)

# Same source bind for the identity probe: ask the child that buttons will run.
_IDENTITY_BOOTSTRAP = (
    "import json,importlib,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "print(json.dumps(importlib.import_module('edge_deploy.ledger').engine_identity()))"
)


class EngineSourceError(RuntimeError):
    """The loaded ``edge_deploy`` package location could not be established."""


@dataclass(frozen=True)
class EngineExecContext:
    """Python executable + the source root that owns the loaded engine package."""

    python: str
    source_root: str


def resolve_engine_source_root() -> Path:
    """Directory that contains the ``edge_deploy`` package the console imported.

    Derived from the loaded module file, not from the process cwd or an assumed
    checkout path. Fails closed rather than guessing another install.
    """
    package_dir = Path(inspect.getfile(load_operator_config)).resolve().parent
    if package_dir.name != "edge_deploy":
        raise EngineSourceError(
            f"loaded edge_deploy.config is not under an edge_deploy package "
            f"directory ({package_dir})"
        )
    source_root = package_dir.parent
    init_py = source_root / "edge_deploy" / "__init__.py"
    if not init_py.is_file():
        raise EngineSourceError(
            f"edge_deploy package is incomplete at {source_root}: missing {init_py.name}"
        )
    return source_root


def build_engine_exec_context(*, python: str | None = None) -> EngineExecContext:
    """Capture the interpreter and the loaded engine source for every child."""
    return EngineExecContext(
        python=python or sys.executable,
        source_root=str(resolve_engine_source_root()),
    )


def engine_module_argv(ctx: EngineExecContext, args: list[str]) -> list[str]:
    """Argv that runs ``edge_deploy`` *args* bound to ``ctx.source_root``.

    ``shell=False`` list form only. Displayed operator commands stay
    ``py -m edge_deploy …`` via :func:`edge_console.actions.display_command`.
    """
    return [ctx.python, "-c", _ENGINE_BOOTSTRAP, ctx.source_root, *args]


def engine_identity_argv(ctx: EngineExecContext) -> list[str]:
    """Argv that prints ``engine_identity()`` from the same bound source."""
    return [ctx.python, "-c", _IDENTITY_BOOTSTRAP, ctx.source_root]
