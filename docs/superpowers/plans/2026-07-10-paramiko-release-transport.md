# Paramiko Release Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace tmux/base64 as the default Edge Node release transport with one persistent, strictly verified Paramiko SSH connection that carries commands, SFTP, PTY dialogue, keepalives, and binary dependency delivery.

**Architecture:** Introduce a small `RemoteTransport` protocol and two adapters: production-default `ParamikoSshTransport` and explicit-recovery `TmuxDriver`. Centralize release-owned paths under `~/.edge-deploy`, stream verified uploads to adjacent part files, expose byte progress through the existing tracker, and map transport failures into durable per-node rollout failures.

**Tech Stack:** Python 3.10/3.12, Paramiko 5.x, PyYAML, pytest/pytest-xdist, Ruff, Windows PowerShell 5.1, Linux SSH/SFTP on Edge Nodes.

---

## Execution Rules

- Execute directly on local `main` per Release Operator instruction. Do not create milestone branches or pull requests.
- Use incremental commits exactly where this plan says `Commit` so main can be inspected or recovered between slices.
- Do not push `main`, install this checkout as the production engine, create tags, mirror to Bitbucket, or update Tool pins without a separate Release Operator instruction.
- The current approved design is `docs/superpowers/specs/2026-07-10-paramiko-release-transport-design.md` at commit `a40c21c`.
- Prototype input is immutable commit `8e50cdd` on branch `explore/paramiko-ssh-transport`. Reuse its hardened algorithms; do not merge its generated `docs/architecture.html` or disposable launch script.
- Every edit under `edge_deploy/*.py` changes Engine Identity. Finish or abandon all open Tool runs before installing the modified package.
- Use `py`, never bare `python`, on the Windows controller.

## File Map

**Create**

- `edge_deploy/transport.py`: transport protocol, progress value, error hierarchy, and transport factory.
- `edge_deploy/remote_paths.py`: canonical logical paths and safe shell/home resolution.
- `edge_deploy/ssh_transport.py`: persistent Paramiko adapter.
- `edge_deploy/transport_smoke.py`: productized live command/transfer/PTY/keepalive probe.
- `tests/test_transport.py`: protocol and factory tests.
- `tests/test_remote_paths.py`: path validation and rendering regressions.
- `tests/test_ssh_transport.py`: fake-Paramiko auth, command, transfer, PTY, and teardown tests.
- `tests/test_transport_smoke.py`: smoke orchestration tests without a network.
- `tests/test_edge_console.py`: progress-file collection tests.
- `docs/adr/0014-paramiko-release-transport.md`: durable architecture decision.

**Modify**

- `pyproject.toml`, `edge_deploy/__init__.py`: required Paramiko dependency and version 1.5.0.
- `edge_deploy/tmux_driver.py`: shared errors/path rendering, protocol-compatible upload progress.
- `edge_deploy/config.py`, `config.example.yaml`: `transport: ssh|pane`, default `ssh`.
- `edge_deploy/auth.py`: type against the protocol and support the Paramiko auth/PTY state machine without changing prompt ownership.
- `edge_deploy/release.py`: factory wiring, transfer callbacks, transport error mapping, generic teardown.
- `edge_deploy/dependencies.py`: canonical paths and transfer callback threading.
- `edge_deploy/runner.py`, `edge_deploy/rollout.py`, `edge_deploy/drift.py`, `edge_deploy/verify.py`: protocol annotations and canonical path helpers.
- `edge_deploy/progress.py`, `edge_console.py`: structured byte progress and non-stalling activity updates.
- `edge_deploy/cli.py`: generic construction/authentication and `transport-smoke` command.
- `tests/conftest.py` and existing focused test modules: update fake transport signatures and regression expectations.
- `README.md`, `docs/DESIGN.md`, `docs/release-workflow.md`, `docs/adr/0011-pane-safe-remote-transport.md`: operator and architecture documentation.

## Task 0: Close Live Runs and Reconfirm Main

**Files:** None.

- [ ] **Step 1: Confirm the existing Autobench release is terminal**

Run from `D:\Projects\autobench`:

```powershell
py -m edge_deploy status --run run-20260708T213720Z-e99dc03
```

Expected: both deploy nodes and both tag phases are `passed`, and the run is complete. If either tag is pending, finish it with the exact next command printed by `status` before continuing.

- [ ] **Step 2: Confirm no other Tool run is open**

```powershell
Set-Location D:\Projects\autobench
py -m edge_deploy status

Set-Location D:\Projects\robocop
py -m edge_deploy status
```

Expected: `no open runs` in both repositories. If an open run exists, finish or abandon it with its current engine before installing modified code.

- [ ] **Step 3: Confirm direct-main safety**

```powershell
Set-Location D:\Projects\edge-deploy-core
git fetch origin main
git status --short --branch
git merge-base --is-ancestor origin/main HEAD
if ($LASTEXITCODE -ne 0) { throw "origin/main is not an ancestor of local main" }
```

Expected: clean `main`, with only the approved local design commit ahead of `origin/main`.

- [ ] **Step 4: Capture the baseline**

```powershell
py -m ruff check .
py -m pytest -n 4 --dist loadfile
```

Expected: Ruff clean and `376 passed` before implementation.

## Task 1: Define the RemoteTransport Interface

**Files:**

- Create: `edge_deploy/transport.py`
- Create: `tests/test_transport.py`
- Modify: `edge_deploy/tmux_driver.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Write failing protocol and progress tests**

Create `tests/test_transport.py`:

```python
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
```

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

```powershell
py -m pytest tests\test_transport.py -q
```

Expected: collection fails because `edge_deploy.transport` does not exist.

- [ ] **Step 3: Create the transport contract and error hierarchy**

Create `edge_deploy/transport.py` with this public surface:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


class TransportError(RuntimeError):
    """Redacted failure owned by a remote transport."""


class TransportUnavailable(TransportError):
    """The selected transport cannot be constructed."""


class HostKeyError(TransportError):
    """The server key is missing or does not match known_hosts."""


class AuthenticationError(TransportError):
    """Keyboard-interactive authentication was rejected or timed out."""


class ConnectionLostError(TransportError):
    """An authenticated transport became unusable."""


class RemoteCommandTimeout(TransportError):
    """A remote channel exceeded its monotonic deadline."""


class TransferError(TransportError):
    """A binary transfer or its verification failed."""


class InteractiveChannelError(TransportError):
    """A PTY dialogue failed."""


@dataclass(frozen=True)
class TransferProgress:
    bytes_sent: int
    total_bytes: int
    elapsed_s: float

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 100.0
        return min(100.0, 100.0 * self.bytes_sent / self.total_bytes)

    @property
    def bytes_per_second(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        return self.bytes_sent / self.elapsed_s


TransferProgressCallback = Callable[[TransferProgress], None]


@runtime_checkable
class RemoteTransport(Protocol):
    session: str

    def start_session(
        self,
        *,
        connect_timeout: float | None = None,
        passcode: str | None = None,
    ) -> bool: ...

    def session_exists(self) -> bool: ...
    def submit_secret(self, secret: str) -> None: ...

    def await_authenticated(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
    ) -> None: ...

    def at_shell_prompt(self, screen: str | None = None) -> bool: ...

    def run_remote(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        ensure_shell: bool = True,
    ) -> tuple[str, int]: ...

    def upload_file(
        self,
        source: str | Path,
        remote_path: str,
        *,
        progress: TransferProgressCallback | None = None,
    ) -> str: ...

    def send_text(self, text: str) -> None: ...

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> str: ...

    def stop_session(self) -> None: ...
```

- [ ] **Step 4: Make TmuxDriver and FakeTmuxDriver satisfy the signature**

Move `AuthenticationError` imports to `edge_deploy.transport` and re-export it from `tmux_driver.py` by importing it there. Make `SessionGoneError` subclass `ConnectionLostError`. Extend both upload methods:

```python
def upload_file(
    self,
    source: str | Path,
    remote_path: str,
    *,
    progress: TransferProgressCallback | None = None,
) -> str:
```

For the pane adapter, emit an initial snapshot and source-byte snapshots after each completed base64 chunk; always emit the exact final byte count. Update `tests/conftest.py::FakeTmuxDriver.upload_file` to emit start/final progress when a callback is supplied.

- [ ] **Step 5: Run focused and full tests**

```powershell
py -m pytest tests\test_transport.py tests\test_tmux_driver.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
```

Expected: all tests pass and Ruff is clean.

- [ ] **Step 6: Commit**

```powershell
git add edge_deploy/transport.py edge_deploy/tmux_driver.py tests/test_transport.py tests/conftest.py tests/test_tmux_driver.py
git commit -m "refactor: define remote transport interface"
```

## Task 2: Canonicalize Release-Owned Remote Paths

**Files:**

- Create: `edge_deploy/remote_paths.py`
- Create: `tests/test_remote_paths.py`
- Modify: `edge_deploy/tmux_driver.py`
- Modify: `edge_deploy/dependencies.py`
- Modify: `edge_deploy/rollout.py`
- Modify: `tests/test_dependencies.py`
- Modify: `tests/test_rollout.py`
- Modify: `tests/test_tmux_driver.py`

- [ ] **Step 1: Write the path regression tests**

Create `tests/test_remote_paths.py`:

```python
import pytest

from edge_deploy.remote_paths import edge_deploy_path, resolve_home_path, shell_remote_path


def test_edge_deploy_path_is_logical_and_variable_free() -> None:
    path = edge_deploy_path("bundles", "autobench", ".incoming", "abc.zip")
    assert path == "~/.edge-deploy/bundles/autobench/.incoming/abc.zip"
    assert "$USER" not in path
    assert "$HOME" not in path


@pytest.mark.parametrize("part", ["../escape", "/absolute", "a/../../escape", ""])
def test_edge_deploy_path_rejects_unsafe_parts(part: str) -> None:
    with pytest.raises(ValueError, match="safe relative POSIX path"):
        edge_deploy_path(part)


def test_shell_remote_path_expands_home_but_quotes_remainder() -> None:
    rendered = shell_remote_path("~/.edge-deploy/a path/file")
    assert rendered == "$HOME/'.edge-deploy/a path/file'"


def test_resolve_home_path_returns_concrete_path() -> None:
    assert resolve_home_path("~/.edge-deploy/a", "/ads_storage/operator") == (
        "/ads_storage/operator/.edge-deploy/a"
    )
```

Add a focused dependency test asserting every command and uploaded path is free of `/ads_storage/$USER` and that the stage script uses `Path(remote_archive).expanduser()`.

- [ ] **Step 2: Run the tests and confirm failure**

```powershell
py -m pytest tests\test_remote_paths.py tests\test_dependencies.py -q
```

Expected: missing module and current `$USER` expectations fail.

- [ ] **Step 3: Implement path helpers**

Create `edge_deploy/remote_paths.py`:

```python
from __future__ import annotations

import posixpath
import shlex
from pathlib import PurePosixPath

EDGE_DEPLOY_ROOT = "~/.edge-deploy"


def _safe_part(part: str) -> str:
    candidate = PurePosixPath(part)
    if not part or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"expected a safe relative POSIX path, got {part!r}")
    return candidate.as_posix()


def edge_deploy_path(*parts: str) -> str:
    cleaned = [_safe_part(part) for part in parts]
    return posixpath.join(EDGE_DEPLOY_ROOT, *cleaned)


def shell_remote_path(remote_path: str) -> str:
    if remote_path == "~":
        return "$HOME"
    if remote_path.startswith("~/"):
        return f"$HOME/{shlex.quote(remote_path[2:])}"
    return shlex.quote(remote_path)


def resolve_home_path(remote_path: str, home: str) -> str:
    if not home.startswith("/") or "\n" in home or "\r" in home:
        raise ValueError("remote home must be an absolute single-line POSIX path")
    if remote_path == "~":
        return home
    if remote_path.startswith("~/"):
        return posixpath.join(home, remote_path[2:])
    if remote_path.startswith("/"):
        return remote_path
    raise ValueError(
        f"remote path must be absolute or use the current user's home, got {remote_path!r}"
    )
```

- [ ] **Step 4: Replace path construction at every dependency seam**

In `dependencies.py`, construct:

```python
incoming = edge_deploy_path("bundles", profile.tool, ".incoming")
remote_archive = f"{incoming}/{bundle.digest}.zip"
stage_script_remote = f"{incoming}/stage-{bundle.digest}.py"
```

Render shell commands with `shell_remote_path()`. In the stage script, replace `os.path.expandvars()` with `os.path.expanduser()` and define the final root from `edge_deploy_path("bundles", bundle.tool)` rather than `/ads_storage/$USER`.

In `rollout.py`, replace both `current_bundle` and activation-link paths with `edge_deploy_path("bundles", profile.tool, "current")`. Keep runner/evidence paths already rooted at `~/.edge-deploy`.

In `tmux_driver.py`, replace the private `_shell_remote_path` implementation with an import of `shell_remote_path` and update call sites.

- [ ] **Step 5: Prove the production package has no `$USER` path**

```powershell
rg -n '\$USER|/ads_storage/\$USER' edge_deploy tests
```

Expected: no production path construction; only the regression-test description may mention the old defect.

- [ ] **Step 6: Run focused and full tests**

```powershell
py -m pytest tests\test_remote_paths.py tests\test_dependencies.py tests\test_rollout.py tests\test_tmux_driver.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
```

- [ ] **Step 7: Commit**

```powershell
git add edge_deploy/remote_paths.py edge_deploy/tmux_driver.py edge_deploy/dependencies.py edge_deploy/rollout.py tests/test_remote_paths.py tests/test_dependencies.py tests/test_rollout.py tests/test_tmux_driver.py
git commit -m "fix: canonicalize remote release paths"
```

## Task 3: Retype Release Modules Against the Transport Seam

**Files:**

- Modify: `edge_deploy/auth.py`
- Modify: `edge_deploy/dependencies.py`
- Modify: `edge_deploy/drift.py`
- Modify: `edge_deploy/release.py`
- Modify: `edge_deploy/rollout.py`
- Modify: `edge_deploy/runner.py`
- Modify: `edge_deploy/verify.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add a static structural grep test**

Extend `tests/test_transport.py`:

```python
from pathlib import Path


def test_engine_modules_do_not_annotate_tmx_driver_directly() -> None:
    package = Path(__file__).parents[1] / "edge_deploy"
    allowed = {"tmux_driver.py", "transport.py", "cli.py"}
    offenders = []
    for path in package.glob("*.py"):
        if path.name not in allowed and "TmuxDriver" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []
```

- [ ] **Step 2: Run the structural test and observe current direct annotations**

```powershell
py -m pytest tests\test_transport.py::test_engine_modules_do_not_annotate_tmx_driver_directly -q
```

Expected: failure listing current callers.

- [ ] **Step 3: Replace concrete annotations**

Import `RemoteTransport` under `TYPE_CHECKING` where possible and change every driver parameter and factory return annotation to the protocol. In `auth.py`, import `AuthenticationError` from `transport.py`. In `release.py`, change `_safe_stop`, `_driver_factory_with_pane_log`, and `driver_factory` annotations to `RemoteTransport` while leaving construction unchanged until Task 7.

Update comments and docstrings from “authenticated pane” to “authenticated transport” where the statement applies to both adapters. Preserve “Authenticated Pane” only for tmux-specific behavior.

- [ ] **Step 4: Run tests and commit**

```powershell
py -m pytest tests\test_transport.py tests\test_auth_seam.py tests\test_release.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
git add edge_deploy tests/conftest.py tests/test_transport.py
git commit -m "refactor: depend on remote transport protocol"
```

## Task 4: Build Paramiko Connection and Authentication

**Files:**

- Create: `edge_deploy/ssh_transport.py`
- Create: `tests/test_ssh_transport.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add Paramiko as a required dependency**

Change project dependencies to:

```toml
dependencies = ["PyYAML>=6.0", "paramiko>=5.0,<6"]
```

Install the development environment only after Task 0 confirms no open runs:

```powershell
py -m pip install -e ".[dev]"
py -c "import paramiko; print(paramiko.__version__)"
```

Expected: a Paramiko 5.x version.

- [ ] **Step 2: Write fake-Paramiko authentication tests**

Create `tests/test_ssh_transport.py` with injected socket, transport, host-key, channel, and SFTP fakes. The first tests must cover:

```python
def test_start_session_exposes_keyboard_interactive_prompt(ssh_transport) -> None:
    assert ssh_transport.start_session(connect_timeout=1.0) is False
    assert ssh_transport.session_exists() is True


def test_submit_secret_completes_auth_without_persisting_secret(ssh_transport) -> None:
    assert ssh_transport.start_session(connect_timeout=1.0) is False
    ssh_transport.submit_secret("one-time-code")
    ssh_transport.await_authenticated(timeout=1.0)
    assert ssh_transport.at_shell_prompt() is True
    assert "one-time-code" not in repr(ssh_transport)


def test_rejected_code_prepares_fresh_auth_attempt(reject_then_accept_transport) -> None:
    reject_then_accept_transport.start_session(connect_timeout=1.0)
    reject_then_accept_transport.submit_secret("stale-code")
    with pytest.raises(AuthenticationError):
        reject_then_accept_transport.await_authenticated(timeout=1.0)
    reject_then_accept_transport.submit_secret("fresh-code")
    reject_then_accept_transport.await_authenticated(timeout=1.0)


def test_unknown_host_key_fails_before_prompt(unknown_host_transport) -> None:
    with pytest.raises(HostKeyError, match="not present"):
        unknown_host_transport.start_session(connect_timeout=1.0)
    assert unknown_host_transport.secret_requests == 0
```

Also test changed key, echoed keyboard prompt, multiple unexpected prompts, auth timeout, and teardown after failure.

- [ ] **Step 3: Run the tests and confirm the adapter is missing**

```powershell
py -m pytest tests\test_ssh_transport.py -q
```

Expected: import failure for `edge_deploy.ssh_transport`.

- [ ] **Step 4: Create production connection settings and strict host verification**

Create `ssh_transport.py` using `endpoint_from_node()` for host/port and parsing only supported options: `ServerAliveInterval`, `ConnectTimeout`, and `UserKnownHostsFile`. Reject `StrictHostKeyChecking=no` for the Paramiko adapter with `TransportUnavailable` rather than weakening verification.

The host lookup must use:

```python
def _known_host_lookup_name(hostname: str, port: int) -> str:
    return hostname if port == 22 else f"[{hostname}]:{port}"
```

Load the configured known-hosts file with `paramiko.HostKeys`, require an exact key-type and key-value match, and raise redacted `HostKeyError` messages that do not include private endpoints.

Define construction explicitly so Task 7's factory has a stable target:

```python
@dataclass(frozen=True)
class SshSettings:
    username: str
    hostname: str
    port: int
    connect_timeout_s: float
    keepalive_s: int
    known_hosts_path: Path


class ParamikoSshTransport:
    def __init__(
        self,
        settings: SshSettings,
        *,
        session: str,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        transport_factory: Callable[[socket.socket], paramiko.Transport] = paramiko.Transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self.session = session
        self._socket_factory = socket_factory
        self._transport_factory = transport_factory
        self._clock = clock
        self._socket: socket.socket | None = None
        self._transport: paramiko.Transport | None = None
        self._remote_home: PurePosixPath | None = None
        self._interactive_channel: paramiko.Channel | None = None
        self._poisoned = False
        self._closed = False

    @classmethod
    def from_node_and_profile(
        cls,
        node: object,
        profile: object,
        *,
        retries: int = 2,
    ) -> "ParamikoSshTransport":
        settings = settings_from_node(node)
        return cls(settings, session=getattr(node, "session", ""))
```

`retries` is accepted for factory compatibility but connection retry policy
remains in release orchestration; do not silently reconnect an authenticated
operation.

- [ ] **Step 5: Implement the AuthBroker-compatible state machine**

Implement `ParamikoSshTransport.start_session`, `submit_secret`, and `await_authenticated` with:

```python
self._secret_queue: queue.Queue[str] = queue.Queue(maxsize=1)
self._prompt_ready = threading.Event()
self._auth_done = threading.Event()
self._auth_error: BaseException | None = None
```

`start_session()` creates the TCP socket and Paramiko transport, starts the SSH client, verifies the key, then starts `auth_interactive()` in a daemon worker. The handler requires exactly one non-echo prompt, sets `_prompt_ready`, blocks on the private queue, returns the code, and drops its local reference in `finally`.

On rejection, `await_authenticated()` starts a new keyboard-interactive attempt on the same active SSH transport before raising `AuthenticationError`, preserving the existing AuthBroker retry loop. On timeout, close the transport, mark it poisoned, and raise `AuthenticationError`.

`session_exists()` means the TCP/SSH transport is active. `at_shell_prompt()` means it is authenticated; the optional `screen` argument is ignored.

- [ ] **Step 6: Run auth tests and existing AuthBroker tests**

```powershell
py -m pytest tests\test_ssh_transport.py -k "auth or host or session" -q
py -m pytest tests\test_auth_seam.py -q
py -m ruff check edge_deploy\ssh_transport.py tests\test_ssh_transport.py
```

- [ ] **Step 7: Commit**

```powershell
git add pyproject.toml edge_deploy/ssh_transport.py tests/test_ssh_transport.py
git commit -m "feat: add persistent Paramiko authentication"
```

## Task 5: Implement Exec, PTY, Keepalive, and Cleanup

**Files:**

- Modify: `edge_deploy/ssh_transport.py`
- Modify: `tests/test_ssh_transport.py`
- Modify: `edge_deploy/auth.py`

- [ ] **Step 1: Add failing command-channel tests**

Add tests for exact exit status, observed transcript order, deadlock-free simultaneous stdout/stderr, command timeout, connection loss, and keepalive:

```python
def test_run_remote_returns_observed_transcript_and_real_exit_status(authenticated_transport) -> None:
    authenticated_transport.channel_script = [
        ("stdout", b"one\n"),
        ("stderr", b"two\n"),
        ("stdout", b"three\n"),
        ("exit", 7),
    ]
    text, code = authenticated_transport.driver.run_remote("probe", timeout=1.0)
    assert text == "one\ntwo\nthree\n"
    assert code == 7


def test_command_timeout_poisons_connection(timeout_transport) -> None:
    with pytest.raises(RemoteCommandTimeout):
        timeout_transport.run_remote("sleep forever", timeout=0.01)
    assert timeout_transport.session_exists() is False
```

- [ ] **Step 2: Port and adapt the prototype channel algorithms**

Use these exact prototype methods as reviewed input:

```powershell
git show 8e50cdd:edge_deploy/_prototype_paramiko_transport.py | Select-String -Pattern "def _open_session|def _channel_call_with_deadline|def _execute|def _drain_channel" -Context 0,80
```

Production changes:

- `_execute()` returns an internal `CommandResult(stdout, stderr, transcript, exit_status)`.
- `_drain_channel()` polls both `recv_ready()` and `recv_stderr_ready()` before checking exit readiness and appends each received block to `transcript` in polling order.
- `run_remote()` decodes `transcript` with UTF-8 replacement and returns `(text, exit_status)`.
- All monotonic deadline failures raise `RemoteCommandTimeout` and poison the transport.
- An inactive transport raises `ConnectionLostError`.

- [ ] **Step 3: Add failing PTY dialogue tests**

```python
def test_pty_dialogue_sends_secret_without_command_logging(authenticated_transport) -> None:
    driver = authenticated_transport.driver
    driver.send_text("kinit")
    assert "Password:" in driver.wait_for(r"[Pp]assword.*:", timeout=1.0)
    driver.submit_secret("kerberos-secret")
    assert authenticated_transport.pty_input.endswith(b"kerberos-secret\n")
    assert "kerberos-secret" not in repr(driver)
```

- [ ] **Step 4: Implement one persistent PTY dialogue channel**

Open the PTY lazily on the first `send_text()`. `wait_for()` drains into a private bounded transcript until the regex matches or raises `InteractiveChannelError`. When authenticated and a PTY dialogue is active, `submit_secret()` writes the secret plus newline directly to the PTY; before authentication it writes only to the auth queue. Close and clear the PTY after Kerberos completion or transport teardown.

Configure `transport.set_keepalive(server_alive_interval)` after authentication. `stop_session()` closes PTY, SFTP, transport, and socket once and clears all references.

- [ ] **Step 5: Run focused tests and commit**

```powershell
py -m pytest tests\test_ssh_transport.py tests\test_auth_seam.py -q
py -m ruff check .
git add edge_deploy/ssh_transport.py edge_deploy/auth.py tests/test_ssh_transport.py
git commit -m "feat: run remote operations over Paramiko channels"
```

## Task 6: Implement Verified Binary Transfer

**Files:**

- Modify: `edge_deploy/ssh_transport.py`
- Modify: `tests/test_ssh_transport.py`

- [ ] **Step 1: Add failing SFTP lifecycle tests**

Cover reuse, upload, verification, atomic replacement, cleanup, and fallback:

```python
def test_upload_reuses_matching_remote_digest(authenticated_transport, tmp_path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"already-present")
    authenticated_transport.remote_file(source.name, source.read_bytes())
    digest = authenticated_transport.driver.upload_file(source, f"~/{source.name}")
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()
    assert authenticated_transport.sftp_put_calls == []


def test_upload_verifies_part_then_atomically_replaces(authenticated_transport, tmp_path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"new-bundle")
    snapshots: list[TransferProgress] = []
    digest = authenticated_transport.driver.upload_file(
        source,
        "~/.edge-deploy/bundle.zip",
        progress=snapshots.append,
    )
    assert digest == hashlib.sha256(b"new-bundle").hexdigest()
    assert snapshots[0].bytes_sent == 0
    assert snapshots[-1].bytes_sent == len(b"new-bundle")
    assert authenticated_transport.atomic_renames == 1
    assert authenticated_transport.part_files == []


def test_digest_mismatch_removes_part_and_preserves_final(mismatch_transport, tmp_path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"corrupt-in-transit")
    with pytest.raises(TransferError, match="digest verification failed"):
        mismatch_transport.upload_file(source, "~/.edge-deploy/bundle.zip")
    assert mismatch_transport.final_bytes == b"previous-verified-bundle"
    assert mismatch_transport.part_files == []


def test_sftp_unavailable_uses_binary_exec_channel(no_sftp_transport, tmp_path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"binary\x00payload")
    no_sftp_transport.upload_file(source, "~/.edge-deploy/bundle.zip")
    assert no_sftp_transport.binary_stream_uploads == 1
    assert no_sftp_transport.base64_commands == []
```

- [ ] **Step 2: Resolve and cache the authenticated remote home**

After authentication, execute `printf '%s' "$HOME"`, require one absolute UTF-8 path, store it as `PurePosixPath`, and resolve every upload destination with `resolve_home_path()`. Never pass `~` to SFTP.

- [ ] **Step 3: Implement SFTP upload with adjacent part files**

Use a unique target such as `<final>.edge-deploy-<uuid>.part`. The algorithm is:

```python
local_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
if self._remote_digest(final_path) == local_digest:
    emit_complete_without_upload()
    return local_digest
try:
    sftp.putfo(source_handle, part_path, file_size=total, callback=on_progress)
    sftp.chmod(part_path, 0o600)
    verify_regular_owner_mode_size_and_digest(part_path)
    run_atomic_mv(part_path, final_path)
except BaseException as exc:
    remove_part_best_effort()
    raise TransferError(redacted_reason) from exc
```

Do not read large files twice: hash in 1 MiB chunks, rewind, then upload. Rate-limit callbacks to at most one per second plus mandatory start/final snapshots.

- [ ] **Step 4: Implement binary exec-channel fallback**

If opening the SFTP subsystem raises `SftpUnavailable`, open an exec channel running secure `cat > <part>`, stream raw chunks with `sendall()`, call `shutdown_write()`, drain the exit status, then run the same size/digest/atomic-rename checks. Other SFTP errors fail immediately; they do not trigger fallback.

- [ ] **Step 5: Run transfer tests and the full suite**

```powershell
py -m pytest tests\test_ssh_transport.py -k "upload or sftp or transfer or digest or progress" -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
```

- [ ] **Step 6: Commit**

```powershell
git add edge_deploy/ssh_transport.py tests/test_ssh_transport.py
git commit -m "feat: stream verified dependency bundles over SFTP"
```

## Task 7: Make Paramiko the Configured Default

**Files:**

- Modify: `edge_deploy/config.py`
- Modify: `config.example.yaml`
- Modify: `edge_deploy/transport.py`
- Modify: `edge_deploy/release.py`
- Modify: `edge_deploy/cli.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_transport.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_release.py`

- [ ] **Step 1: Write failing config and factory tests**

Add:

```python
def test_node_config_defaults_to_ssh_transport() -> None:
    node = OperatorConfig.from_mapping(
        {"nodes": {"node03": {"host": "operator@edge"}}}
    ).node("node03")
    assert node.transport == "ssh"


def test_node_config_accepts_explicit_pane_transport() -> None:
    node = OperatorConfig.from_mapping(
        {"nodes": {"node03": {"host": "operator@edge", "transport": "pane"}}}
    ).node("node03")
    assert node.transport == "pane"


def test_node_config_rejects_unknown_transport() -> None:
    with pytest.raises(ValueError, match="transport must be one of: pane, ssh"):
        OperatorConfig.from_mapping(
            {"nodes": {"node03": {"host": "operator@edge", "transport": "magic"}}}
        )
```

In `tests/test_transport.py`, patch both adapter constructors and assert `transport_for_node()` selects SSH by default and pane only when configured.

- [ ] **Step 2: Implement validated NodeConfig.transport**

Add:

```python
VALID_TRANSPORTS = ("pane", "ssh")
DEFAULT_TRANSPORT = "ssh"
```

Add `transport: str = DEFAULT_TRANSPORT` to `NodeConfig`. Validate in `from_mapping()` and raise a node-labeled but endpoint-free `ValueError` for unknown values. Add `transport: ssh` to both nodes in `config.example.yaml`, with a comment that `pane` is an explicit recovery override.

- [ ] **Step 3: Add the centralized factory**

Append to `transport.py`:

```python
def transport_for_node(
    node: object,
    profile: object,
    *,
    retries: int = 2,
    pane_log_path: Path | None = None,
) -> RemoteTransport:
    selected = getattr(node, "transport", "ssh")
    if selected == "ssh":
        from edge_deploy.ssh_transport import ParamikoSshTransport

        return ParamikoSshTransport.from_node_and_profile(node, profile, retries=retries)
    if selected == "pane":
        from edge_deploy.tmux_driver import TmuxDriver

        return TmuxDriver.from_node_and_profile(
            node,
            profile,
            retries=retries,
            pane_log_path=pane_log_path,
        )
    raise TransportUnavailable(f"unsupported transport {selected!r}")
```

- [ ] **Step 4: Route release construction through the factory**

Set the `run_release()` default exactly as:

```python
driver_factory: Callable[..., RemoteTransport] = transport_for_node,
```

Make `_driver_factory_with_pane_log` pass `pane_log_path` through; the factory ignores it for SSH and uses it for pane. Keep injected factories in tests unchanged.

Update `_safe_stop()` and exception handling to use `RemoteTransport` and `TransportError`.

- [ ] **Step 5: Make standalone rollout and drift authenticate generically**

Replace direct `TmuxDriver.from_node_and_profile` in `_cmd_rollout` and `_cmd_drift` with `transport_for_node`. Keep `--reuse-session` legal only for `transport: pane`; return a clear CLI error for SSH because a standalone process cannot reuse another process's Paramiko connection.

For a new SSH connection, construct an in-memory auth progress adapter:

```python
class _CliAuthProgress:
    def emit(self, message: str, **_kwargs: object) -> None:
        print(redact(message))

    def set_waiting(self, _waiting_on: str | None) -> None:
        return None
```

Retype `AuthBroker` to an `AuthProgress` protocol containing only `emit()` and `set_waiting()`, then use `AuthBroker(_CliAuthProgress(), "prompt", 300.0, 3)` before standalone operations. Always close the transport in `finally`.

- [ ] **Step 6: Run config/factory/release tests and commit**

```powershell
py -m pytest tests\test_config.py tests\test_transport.py tests\test_cli.py tests\test_release.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
git add edge_deploy/config.py config.example.yaml edge_deploy/transport.py edge_deploy/release.py edge_deploy/cli.py edge_deploy/auth.py tests/test_config.py tests/test_transport.py tests/test_cli.py tests/test_release.py
git commit -m "feat: make Paramiko the default release transport"
```

## Task 8: Thread Transfer Progress Through Release

**Files:**

- Modify: `edge_deploy/progress.py`
- Modify: `edge_deploy/release.py`
- Modify: `edge_deploy/rollout.py`
- Modify: `edge_deploy/dependencies.py`
- Modify: `edge_console.py`
- Modify: `tests/test_progress.py`
- Modify: `tests/test_release.py`
- Modify: `tests/test_dependencies.py`
- Create: `tests/test_edge_console.py`

- [ ] **Step 1: Write tracker progress tests**

Add to `tests/test_progress.py`:

```python
def test_transfer_progress_is_durable_and_marks_activity(tmp_path) -> None:
    clock = FakeClock()
    tracker = ReleaseProgressTracker(tmp_path, clock=clock, stall_threshold_s=3.0)
    tracker.start("rollout autobench/node03", phase="rollout", tool="autobench", node="node03")
    clock.advance(2.0)
    tracker.update_transfer(
        artifact="dependency bundle",
        progress=TransferProgress(bytes_sent=25, total_bytes=100, elapsed_s=2.0),
    )
    payload = json.loads((tmp_path / "release-progress.json").read_text(encoding="utf-8"))
    assert payload["active"]["transfer"]["percent"] == 25.0
    assert payload["active"]["transfer"]["bytes_sent"] == 25
    assert payload["inactive_s"] == 0.0
    assert "stall_warning" not in payload
```

Add a completion test asserting the final progress event is 100 percent and the console message includes MiB, percentage, and MiB/s.

- [ ] **Step 2: Implement structured progress updates**

Add `ReleaseProgressTracker.update_transfer(artifact, progress)`. Store this object under `self._active.extra["transfer"]`:

```python
{
    "artifact": artifact,
    "bytes_sent": progress.bytes_sent,
    "total_bytes": progress.total_bytes,
    "percent": round(progress.percent, 1),
    "bytes_per_second": round(progress.bytes_per_second, 1),
    "updated_at": utc_iso_timestamp(),
}
```

Call `mark_activity()`, write progress JSON, and notify the console without appending every rate-limited update to `release.log`. Protect active-state mutation and JSON replacement with an `RLock` because the heartbeat thread and SFTP callback can run concurrently. Include `ActiveOperation.extra` fields in the `active` payload.

- [ ] **Step 3: Thread the callback to the bundle upload only**

Add `transfer_progress: TransferProgressCallback | None = None` to `run_rollout()` and `deliver_dependency_bundle()`. In `release.py`, create a closure inside the tool/node loop:

```python
def on_transfer(progress: TransferProgress) -> None:
    tracker.update_transfer(artifact=f"{tool} dependency bundle", progress=progress)
```

Pass it through rollout to:

```python
driver.upload_file(
    bundle.archive_path,
    remote_archive,
    progress=transfer_progress,
)
```

Small runner and stage-script uploads keep `progress=None`.

- [ ] **Step 4: Add Edge Console collection and rendering**

Change `collect_runs()` to include:

```python
progress = _read_json(entry / "release-progress.json")
runs.append({"state": state, "events": events, "lock": lock, "progress": progress})
```

Add `transferHtml(run)` to render the optional active transfer as a compact progress bar with bytes, percentage, and rate. Render nothing for old progress files or inactive runs. Add `tests/test_edge_console.py` that imports `collect_runs`, writes a v1 progress file with a transfer object, and asserts it is returned unchanged; also assert runs without the file return `progress is None`.

- [ ] **Step 5: Run progress and integration tests**

```powershell
py -m pytest tests\test_progress.py tests\test_dependencies.py tests\test_release.py tests\test_edge_console.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
```

- [ ] **Step 6: Commit**

```powershell
git add edge_deploy/progress.py edge_deploy/release.py edge_deploy/rollout.py edge_deploy/dependencies.py edge_console.py tests/test_progress.py tests/test_release.py tests/test_dependencies.py tests/test_edge_console.py
git commit -m "feat: report binary transfer progress"
```

## Task 9: Convert Transport Failures Into Durable Node Failures

**Files:**

- Modify: `edge_deploy/release.py`
- Modify: `edge_deploy/phases/deploy.py`
- Modify: `tests/test_release.py`
- Modify: `tests/test_phases_deploy.py`
- Modify: `tests/test_reporting.py`

- [ ] **Step 1: Write failing transport-error release tests**

Add a parameterized release test covering every public `TransportError` subclass. Inject a driver that raises during auth or rollout and assert:

```python
assert report.exit_code() == 1
assert report.rollouts[0]["status"] == "failed"
assert report.rollouts[0]["node"] == "node03"
assert "Traceback" not in report.rollouts[0]["state_left"]
assert "operator@private-edge.example" not in json.dumps(report.to_payload())
```

Add a phase test that injects a `TransferError` and asserts `RunLedger.phase_state("deploy", node="node03") == "failed"`, not `pending`.

- [ ] **Step 2: Centralize redacted failure conversion**

Add a helper in `release.py`:

```python
def _transport_failure_report(
    exc: TransportError,
    node: object,
    profile: ToolProfile,
    snapshot: str,
) -> OperationReport:
    return _synthetic_report(
        "failed",
        node,
        profile.repo_path,
        deployment_commit=snapshot,
        check=ReportCheck("transport", False, f"transport failed: {exc}"),
    )
```

Catch `TransportError` at both authentication and per-tool rollout seams. Preserve `AuthenticationError` retry inside `AuthBroker`; only exhausted authentication reaches release conversion. Always call `_safe_stop()` in `finally` for each node, not only selected exception branches.

Expected transport failures must return reports and phase exit code 1 without a raw traceback. Unexpected programming errors continue to raise during development rather than being mislabeled as transport failures.

- [ ] **Step 3: Run failure tests and commit**

```powershell
py -m pytest tests\test_release.py tests\test_phases_deploy.py tests\test_reporting.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
git add edge_deploy/release.py edge_deploy/phases/deploy.py tests/test_release.py tests/test_phases_deploy.py tests/test_reporting.py
git commit -m "fix: persist transport failures in the run ledger"
```

## Task 10: Productize the Transport Smoke Command

**Files:**

- Create: `edge_deploy/transport_smoke.py`
- Create: `tests/test_transport_smoke.py`
- Modify: `edge_deploy/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write a fake-transport smoke test**

The smoke result must test one connection across command, 8 MiB upload/download digest, PTY dummy secret, keepalive, and cleanup without persisting secrets or endpoints. Use a small payload in unit tests:

```python
def test_transport_smoke_reuses_one_connection_and_cleans_up(fake_transport, tmp_path) -> None:
    result = run_transport_smoke(
        fake_transport,
        node_label="node03",
        payload_bytes=1024,
        keepalive_wait_s=0.0,
    )
    assert result.passed is True
    assert [check.name for check in result.checks] == [
        "command", "transfer", "pty", "keepalive", "cleanup"
    ]
    assert fake_transport.start_count == 1
    assert fake_transport.stop_count == 1
```

- [ ] **Step 2: Implement a production diagnostic using public transport behavior**

`run_transport_smoke()` must not import private Paramiko internals. It uses `RemoteTransport`, creates random bytes locally, uploads them to a unique `edge_deploy_path("smoke", uuid, "payload")`, verifies remote SHA through `run_remote`, exercises a generated dummy PTY secret, runs a command across two keepalive intervals, removes the scratch directory in `finally`, and closes the transport.

Return a dataclass result with redacted checks; print throughput but do not persist a report by default.

- [ ] **Step 3: Add the CLI surface**

Register:

```text
py -m edge_deploy transport-smoke --node node03 [--payload-mib 8]
```

The command loads the node, requires `transport: ssh`, runs TCP preflight, authenticates through `AuthBroker`, executes all checks, prints PASS/FAIL lines, and exits 0 only when cleanup succeeds. It must refuse `transport: pane` because this diagnostic validates the new production adapter.

- [ ] **Step 4: Run tests and commit**

```powershell
py -m pytest tests\test_transport_smoke.py tests\test_cli.py -q
py -m pytest -n 4 --dist loadfile
py -m ruff check .
git add edge_deploy/transport_smoke.py edge_deploy/cli.py tests/test_transport_smoke.py tests/test_cli.py
git commit -m "feat: add Paramiko transport smoke command"
```

## Task 11: Update ADRs, Operator Documentation, and Version

**Files:**

- Create: `docs/adr/0014-paramiko-release-transport.md`
- Modify: `docs/adr/0011-pane-safe-remote-transport.md`
- Modify: `README.md`
- Modify: `docs/DESIGN.md`
- Modify: `docs/release-workflow.md`
- Modify: `config.example.yaml`
- Modify: `pyproject.toml`
- Modify: `edge_deploy/__init__.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write ADR-0014 from the approved design**

ADR-0014 must record:

- live node03 evidence: all probes passed, SFTP 0.4 MiB/s upload and 0.2 MiB/s download, one transport reused, cleanup confirmed;
- decision: Paramiko required/default, one connection per node deploy invocation, pane explicit fallback;
- canonical `~/.edge-deploy` paths;
- SFTP then binary-stream fallback, never automatic base64 fallback;
- strict known-hosts and AuthBroker secret ownership;
- progress and durable failure consequences; and
- relationship to ADR-0009 and ADR-0011.

Append a dated postscript to ADR-0011: its pane-safe rules remain binding only when `transport: pane` is selected; ADR-0014 supersedes the “pane is the only channel” conclusion.

- [ ] **Step 2: Update operator docs**

Document in `docs/release-workflow.md`:

```powershell
py -m edge_deploy preflight --node node03
py -m edge_deploy transport-smoke --node node03
py -m edge_deploy release --guided --tool autobench
```

Explain that SSH is default, pane is an explicit per-node recovery override, matching dependency archives are reused, new archives show byte progress, and no manual SCP/symlink workaround is part of the canonical 1.5.0 workflow.

Update `README.md` and `docs/DESIGN.md` so `transport.py` and `ssh_transport.py` are named architecture modules and “local tmux/psmux pane” is no longer described as universal.

- [ ] **Step 3: Bump the package version once**

Set both:

```toml
version = "1.5.0"
```

and:

```python
__version__ = "1.5.0"
```

Export `RemoteTransport`, `ParamikoSshTransport`, `TransportError`, and `TransferProgress` from `edge_deploy.__init__` while retaining `TmuxDriver`, `AuthenticationError`, and `SessionGoneError` compatibility exports.

- [ ] **Step 4: Run documentation/config/version checks and commit**

```powershell
py -m pytest tests\test_config.py tests\test_cli.py -q
py -c "import edge_deploy; assert edge_deploy.__version__ == '1.5.0'"
py -m ruff check .
git diff --check
git add docs README.md config.example.yaml pyproject.toml edge_deploy/__init__.py tests/test_config.py
git commit -m "docs: make Paramiko the 1.5.0 release transport"
```

## Task 12: Automated Verification and Independent Review

**Files:** All changed implementation and test files.

- [ ] **Step 1: Run exact static searches**

```powershell
rg -n '\$USER|/ads_storage/\$USER' edge_deploy
rg -n 'TmuxDriver.from_node_and_profile' edge_deploy
rg -n 'base64' edge_deploy\ssh_transport.py
```

Expected:

- no `$USER` path construction;
- `TmuxDriver.from_node_and_profile` only inside `transport_for_node`;
- no base64 use in `ssh_transport.py`.

- [ ] **Step 2: Run the complete test and lint gates**

```powershell
py -m ruff check .
py -m pytest -n 4 --dist loadfile
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: Ruff clean, all tests pass on Python 3.12, no whitespace errors, and only intended direct-main commits ahead of origin.

- [ ] **Step 3: Verify Python 3.10 through CI-equivalent environment**

On a controller with Python 3.10 available:

```powershell
py -3.10 -m venv D:\Temp\edge-deploy-1.5-py310
D:\Temp\edge-deploy-1.5-py310\Scripts\python.exe -m pip install -e ".[dev]"
D:\Temp\edge-deploy-1.5-py310\Scripts\python.exe -m pytest
```

Expected: all tests pass. If Python 3.10 is unavailable locally, GitHub CI on `main` becomes the required 3.10 gate after the Release Operator authorizes pushing.

- [ ] **Step 4: Review security-sensitive invariants**

Inspect the diff and confirm:

- no passcode or password enters a string formatted into commands or reports;
- no AutoAddPolicy or disabled host-key checking exists;
- part files use mode 0600 and are cleaned on every transfer failure;
- digest mismatch preserves the previous final archive;
- no transport failure silently selects pane;
- every node transport closes in `finally`; and
- generated run/probe evidence is untracked.

- [ ] **Step 5: Commit any review-only corrections**

If review finds a concrete defect, fix it with a failing regression test, rerun Step 2, and commit:

```powershell
git add edge_deploy tests docs edge_console.py config.example.yaml pyproject.toml README.md
git commit -m "fix: harden Paramiko release transport"
```

Do not create an empty commit when no correction is needed.

## Task 13: Operator Live Gates

**Files:** Generated evidence remains under Tool run directories and must not be committed.

- [ ] **Step 1: Install the direct-main engine only after all old runs are terminal**

```powershell
Set-Location D:\Projects\edge-deploy-core
py -m pip install -e ".[dev]"
py -c "import edge_deploy; print(edge_deploy.__version__)"
```

Expected: `1.5.0` from this checkout.

- [ ] **Step 2: Run productized transport smoke on both nodes in both-vpns posture**

```powershell
py -m edge_deploy transport-smoke --node node03
py -m edge_deploy transport-smoke --node node04
```

Expected for each node: command, transfer, PTY, keepalive, and cleanup PASS; one authentication; SFTP available or binary stream explicitly reported; SHA-256 identical.

- [ ] **Step 3: Remove the temporary literal-variable compatibility links**

Only after both smoke tests pass, connect to each node and inspect:

```bash
test -L '/ads_storage/$USER' && readlink '/ads_storage/$USER'
```

If it points to the authenticated user's real `/ads_storage/<user>` root, remove only the symlink:

```bash
rm '/ads_storage/$USER'
```

Do not delete the preserved recovery directory under `~/.edge-deploy/recovery/` until the dependency-bearing release passes.

- [ ] **Step 4: Prepare and run a dependency-bearing guided Autobench release**

Use the repository's dependency-path e2e preparation from the core checkout:

```powershell
Set-Location D:\Projects\edge-deploy-core
.\scripts\e2e-release.ps1 -Kind deps -Tool autobench -ToolPath D:\Projects\autobench
```

This creates a comment-only `requirements.txt` change, verifies effective
dependency declarations are unchanged, and exercises dependency delivery. After
the generated Autobench PR passes CI and the Release Operator merges it, run from
`D:\Projects\autobench`:

```powershell
py -m edge_deploy release --guided
```

Acceptance evidence:

- each node authenticates once;
- `release-progress.json` shows increasing bytes and reaches 100 percent;
- console progress reports MiB, percent, and rate;
- archive digest verification passes;
- no `__EDGE_DEPLOY_UPLOAD_` base64 heredoc appears;
- dependency stage/install/smoke/drift pass on node03 and node04;
- tag-bitbucket and tag-github complete under their required postures; and
- final `status` reports a complete run.

- [ ] **Step 5: Verify explicit pane recovery remains usable**

Temporarily set one private operator-config node to `transport: pane`, run the productized preflight plus a focused non-dependency rollout or existing e2e no-deps release, then restore `transport: ssh`. Do not run a large dependency upload through pane.

Expected: pane auth and small runner/evidence uploads still work; no code or private config change is committed.

- [ ] **Step 6: Run final local verification after live gates**

```powershell
Set-Location D:\Projects\edge-deploy-core
py -m ruff check .
py -m pytest -n 4 --dist loadfile
git status --short --branch
```

Expected: clean implementation tree except direct-main commits ahead of origin; generated Tool run files remain outside this repository.

## Task 14: Release 1.5.0 After Explicit Operator Authorization

**Files:** No implementation changes unless a live gate found a tested defect.

- [ ] **Step 1: Push direct main only in firewall-off posture**

After the Release Operator explicitly authorizes publication:

```powershell
Set-Location D:\Projects\edge-deploy-core
git push origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Expected: local HEAD and GitHub main match. Wait for Python 3.10 and 3.12 CI to pass.

- [ ] **Step 2: Publish the immutable GitHub tag**

```powershell
git tag -a v1.5.0 -m "edge-deploy-core 1.5.0"
git push origin refs/tags/v1.5.0
git ls-remote origin refs/tags/v1.5.0 refs/tags/v1.5.0^{}
```

Expected: annotated tag and peeled commit resolve; never move this tag.

- [ ] **Step 3: Mirror the exact release in bitbucket-vpn posture**

```powershell
py -m edge_deploy mirror --tag v1.5.0
git ls-remote bitbucket refs/heads/main refs/tags/v1.5.0 refs/tags/v1.5.0^{}
```

Expected: Bitbucket main/tag point to the approved tree according to mirror evidence.

- [ ] **Step 4: Update Tool pins only after GitHub tag resolution**

In Autobench and Dispatch, change only the immutable engine URL from `@v1.4.0` to `@v1.5.0`, install each in a clean virtual environment, and run each full test suite before publication. Preserve each Tool repository's normal contribution workflow unless the Release Operator gives a separate direct-main instruction for that repository.

## Spec Coverage

- Transport seam and explicit adapters: Tasks 1, 3, and 7.
- Strict Paramiko authentication and one-connection lifecycle: Tasks 4 and 5.
- Canonical variable-free remote paths: Task 2.
- SFTP, binary fallback, digest verification, atomic rename, and cleanup: Task 6.
- Byte progress, stall prevention, JSON, and Edge Console visibility: Task 8.
- Redacted durable transport failures: Task 9.
- Productized node diagnostics: Task 10.
- Required dependency, SSH default, pane override, ADRs, and 1.5.0 migration: Tasks 7 and 11.
- Automated, security, Python-version, node03/node04, dependency-release, and pane-recovery gates: Tasks 12 and 13.
- Immutable tag, mirror, and downstream Tool-pin ordering: Task 14.

## Plan Completion Checklist

- [ ] Required Paramiko 5.x dependency installed by normal package installation.
- [ ] `RemoteTransport` is the only release-module transport type.
- [ ] Paramiko is the default; pane requires explicit configuration.
- [ ] One successful RSA code authenticates all node operations in a deploy invocation.
- [ ] All release-owned logical paths use `~/.edge-deploy`.
- [ ] SFTP or binary exec streaming transfers archives with atomic digest-verified replacement.
- [ ] Byte progress prevents false stall warnings and is visible in console and JSON.
- [ ] Expected transport failures become durable failed node states without tracebacks.
- [ ] Productized smoke passes on node03 and node04.
- [ ] A dependency-bearing guided release completes on both nodes.
- [ ] Ruff, Python 3.10 tests, and Python 3.12 tests pass.
- [ ] Version 1.5.0 docs and ADR-0014 are complete.
- [ ] No generated evidence, endpoint configuration, or secrets enter GitHub.
