"""edge_console — the local release console for edge-deploy runs.

A single-purpose web UI over one or more tool checkouts. It renders each Run as
a rail through the five workstation postures (ADR-0013), spotlights whatever is
open or live, and answers "should I release this tool?" for every checkout that
has no Run in flight.

Since ADR-0018 the console also *drives* the release: every command an operator
used to copy into a terminal is a button that runs the same allowlisted
``edge_deploy`` command in that checkout, streams its output, and relays the
engine's interactive prompts — the RSA passcode, a Kerberos password, the
guided posture acknowledgement — back to the process. The console owns no
release logic and never writes to a ledger; changing the workstation firewall
posture stays manual.

Deliberately outside the ``edge_deploy`` package: Engine Identity (ADR-0008)
hashes every package ``*.py``, so UI code must live here or every open Run
would be orphaned.

Usage:

    py -m edge_console                              # watch the cwd checkout
    py -m edge_console --root D:\\ab --root D:\\rc    # watch several checkouts
    py -m edge_console --root D:\\training\\ab \\
        --github-write-root D:\\ab --read-only       # training ledgers, watch only
    py -m edge_console --demo                       # fabricated checkouts, offline simulator

Posture probes: Bitbucket and Edge remain TCP-only and labelled as such. The
GitHub capability light is a per-watched-tool authenticated, empty
``git-receive-pack`` POST. It reaches the write-side HTTP path but sends no
update commands, pack, or ref mutation. Write probes use
``--github-write-root`` when set, otherwise the same paths as ``--root``.
Divergence facts still use read-only git on ``--root`` checkouts
(``ls-remote`` for GitHub main); training roots may lack git on purpose.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer

from edge_console.actions import (
    ACTION_SPECS,
    ActionError,
    ActionRegistry,
    ActionRunner,
    ActionSpec,
    catalog,
    display_command,
)
from edge_console.demo import build_demo_checkouts, demo_argv_builder, demo_git
from edge_console.engine_exec import (
    EngineExecContext,
    EngineSourceError,
    build_engine_exec_context,
    engine_identity_argv,
    engine_module_argv,
    resolve_engine_source_root,
)
from edge_console.ledger import (
    SCHEMA,
    collect_runs,
    collect_runs_multi,
    find_run_state,
    is_training_state,
)
from edge_console.page import PAGE
from edge_console.probes import (
    PostureProber,
    ToolsProber,
    aggregate_github_write,
    probe_divergence,
    probe_github_write,
    probe_tool,
)
from edge_console.readiness import (
    ReadinessProber,
    probe_bb_token,
    probe_engine_identity,
    probe_operator_config,
)
from edge_console.server import (
    ConsoleHandler,
    build_arg_parser,
    main,
    resolve_console_roots,
)

__all__ = [
    "ACTION_SPECS",
    "ActionError",
    "ActionRegistry",
    "ActionRunner",
    "ActionSpec",
    "ConsoleHandler",
    "PAGE",
    "PostureProber",
    "ReadinessProber",
    "SCHEMA",
    "ThreadingHTTPServer",
    "ToolsProber",
    "aggregate_github_write",
    "build_arg_parser",
    "build_demo_checkouts",
    "catalog",
    "collect_runs",
    "collect_runs_multi",
    "EngineExecContext",
    "EngineSourceError",
    "build_engine_exec_context",
    "demo_argv_builder",
    "demo_git",
    "display_command",
    "engine_identity_argv",
    "engine_module_argv",
    "find_run_state",
    "is_training_state",
    "main",
    "probe_bb_token",
    "probe_divergence",
    "probe_engine_identity",
    "probe_github_write",
    "probe_operator_config",
    "probe_tool",
    "resolve_console_roots",
    "resolve_engine_source_root",
]
