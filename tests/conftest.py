"""Shared fixtures and test doubles for the edge-deploy-core suite.

The central piece is :class:`FakeTmuxDriver`, a stand-in for
:class:`edge_deploy.tmux_driver.TmuxDriver` that records every ``run_remote`` call and
returns canned ``(screen, exit_code)`` tuples. It lets the rollout/drift engines run end
to end with no tmux, no SSH and no Edge Node, while still exercising the real sentinel
parsing (``__RC_<nonce>_<code>__``) and the base64 ``DRIFT_PAYLOAD`` / ``PERMISSION_PAYLOAD``
round-trips.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from edge_deploy.config import NodeConfig, ToolProfile

# Sibling Tool repos live next to edge-deploy-core (…/Projects/{autobench,robocop}).
PROJECTS_ROOT = Path(__file__).resolve().parents[2]

# A permission payload where every gate passes (used as the rollout default).
OK_PERMISSIONS: dict[str, Any] = {
    "root_traversable": True,
    "update_executable": True,
    "install_executable": True,
    "runtime_files_checked": 5,
    "unreadable_runtime_files": [],
}


def _decode_remote_python(command: str) -> str:
    """Recover the inline Python a ``_remote_python`` command would run on the node.

    ``_remote_python`` builds ``printf %s <base64> | base64 -d | <py> -``; decoding the
    base64 lets the fake tell a drift scan apart from a permission probe by the marker the
    script prints, instead of guessing from call order.
    """
    match = re.search(r"printf %s (\S+) \| base64 -d", command)
    if not match:
        return ""
    try:
        return base64.b64decode(match.group(1)).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return ""


class FakeTmuxDriver:
    """Record-and-replay double for ``TmuxDriver`` used by the rollout/drift tests.

    Responses are routed by command content (not call order), so a test only configures
    the data it cares about:

    * ``head_commits`` — values returned by successive ``git rev-parse --verify HEAD``
      calls (rollout reads HEAD before and after ``update.sh``); the last value repeats.
    * ``changed_paths`` — the ``git diff --name-only`` result.
    * ``update_code`` / ``install_code`` — exit codes for ``update.sh`` / ``install.sh``.
    * ``permissions`` — the rollout permission-probe payload.
    * ``remote_runtime`` — the drift remote runtime map.
    """

    def __init__(
        self,
        *,
        head_commits: list[str] | None = None,
        changed_paths: list[str] | None = None,
        update_code: int = 0,
        install_code: int = 0,
        permissions: dict[str, Any] | None = None,
        remote_runtime: dict[str, str] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.call_log: list[dict[str, Any]] = []
        self._head_commits = list(head_commits) if head_commits else []
        self._changed_paths = list(changed_paths) if changed_paths else []
        self.update_code = update_code
        self.install_code = install_code
        self._permissions = permissions if permissions is not None else dict(OK_PERMISSIONS)
        self._remote_runtime = dict(remote_runtime) if remote_runtime else {}
        # Attributes the CLI/engine read directly off a driver.
        self.session = "fake-session"
        self.host = "user@edge.example"
        self.repo_path = ""

    # -- TmuxDriver surface the engine and CLI touch -----------------------------------
    def session_exists(self) -> bool:
        return True

    def start_session(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def stop_session(self) -> None:
        return None

    def run_remote(self, command: str, *, timeout: float = 30.0, ensure_shell: bool = True) -> tuple[str, int]:
        self.commands.append(command)
        self.call_log.append({"command": command, "timeout": timeout, "ensure_shell": ensure_shell})
        return self._respond(command)

    # -- assertions helpers ------------------------------------------------------------
    def ran(self, needle: str) -> bool:
        """True if any captured command contains ``needle``."""
        return any(needle in command for command in self.commands)

    # -- internals ---------------------------------------------------------------------
    def _next_head(self) -> str:
        if not self._head_commits:
            return ""
        value = self._head_commits[0]
        if len(self._head_commits) > 1:
            self._head_commits.pop(0)
        return value

    @staticmethod
    def _sentinel(body: str = "", code: int = 0) -> tuple[str, int]:
        marker = f"__RC_fake_{code}__"
        screen = f"{body}\n{marker}\n" if body else f"{marker}\n"
        return screen, code

    def _respond(self, command: str) -> tuple[str, int]:
        if "base64 -d" in command:
            script = _decode_remote_python(command)
            if "PERMISSION_PAYLOAD_START" in script:
                body = (
                    "PERMISSION_PAYLOAD_START\n"
                    + json.dumps(self._permissions, sort_keys=True)
                    + "\nPERMISSION_PAYLOAD_END"
                )
                return self._sentinel(body, 0)
            if "DRIFT_PAYLOAD_START" in script:
                body = (
                    "DRIFT_PAYLOAD_START\n"
                    + json.dumps(self._remote_runtime, sort_keys=True)
                    + "\nDRIFT_PAYLOAD_END"
                )
                return self._sentinel(body, 0)
            return self._sentinel("", 0)
        if "git rev-parse --verify HEAD" in command:
            return self._sentinel(self._next_head(), 0)
        if "git diff --name-only" in command:
            return self._sentinel("\n".join(self._changed_paths), 0)
        if "git rev-parse --verify" in command:
            return self._sentinel(self._head_commits[-1] if self._head_commits else "f" * 40, 0)
        if "./update.sh" in command:
            return self._sentinel(f"update.sh exit {self.update_code}", self.update_code)
        if "./install.sh" in command:
            return self._sentinel(f"install.sh exit {self.install_code}", self.install_code)
        return self._sentinel("", 0)


def _load_real_profile(tool: str) -> ToolProfile:
    """Load a committed Tool Profile, skipping the test if the sibling repo is absent."""
    profile_path = PROJECTS_ROOT / tool / "edge_deploy.yaml"
    if not profile_path.exists():
        pytest.skip(f"real Tool Profile not found: {profile_path}")
    return ToolProfile.load(profile_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_tmux() -> type[FakeTmuxDriver]:
    """Return the :class:`FakeTmuxDriver` class so tests can build configured instances."""
    return FakeTmuxDriver


@pytest.fixture
def load_profile():
    """Return a loader that yields a real Tool Profile by name (autobench / robocop)."""
    return _load_real_profile


@pytest.fixture
def autobench_profile() -> ToolProfile:
    return _load_real_profile("autobench")


@pytest.fixture
def robocop_profile() -> ToolProfile:
    return _load_real_profile("robocop")


@pytest.fixture(params=["autobench", "robocop"])
def real_profile(request: pytest.FixtureRequest) -> ToolProfile:
    """Parametrized across both committed Tool Profiles to prove engine generality."""
    return _load_real_profile(request.param)


@pytest.fixture
def sample_node() -> NodeConfig:
    return NodeConfig.from_mapping("node03", {"host": "user@hde2stl020003.mastercard.int"})
