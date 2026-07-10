# Paramiko Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the seven defects found in the Paramiko release transport review before live release gates begin.

**Architecture:** Preserve the approved `RemoteTransport` boundary and Paramiko connection model. Tighten behavior inside the SSH adapter, transport smoke diagnostic, and node-level release orchestration, with regression tests at each public seam.

**Tech Stack:** Python 3.10+, Paramiko, pytest, Ruff

---

### Task 1: Make Large Transfers Deadline-Safe

**Files:**
- Modify: `edge_deploy/ssh_transport.py`
- Test: `tests/test_ssh_transport.py`

- [ ] Add a regression test that advances a fake clock beyond 120 seconds during an otherwise healthy archive upload and expects completion.
- [ ] Replace the fixed 120-second transfer deadline with a configurable `transfer_timeout_s` setting whose default safely exceeds the documented three-minute Autobench transfer.
- [ ] Keep one monotonic deadline across remote-home resolution, upload, verification, and atomic replacement.
- [ ] Run `py -m pytest tests\test_ssh_transport.py -q` and confirm it passes.

### Task 2: Make Smoke Probes Prove Their Claims

**Files:**
- Modify: `edge_deploy/transport_smoke.py`
- Test: `tests/test_transport_smoke.py`

- [ ] Add a PTY regression test where no remote success marker is returned and assert the probe cannot pass.
- [ ] Make the PTY command emit a readiness marker, read a generated secret without echoing it, compare its SHA-256 remotely, and emit a success marker that `wait_for()` must observe.
- [ ] Add a keepalive regression test that distinguishes a command completed before the wait from one spanning the wait.
- [ ] Run one remote command containing the wait and final marker so the SSH connection must remain healthy across the keepalive intervals.
- [ ] Move scratch cleanup into `finally`, retain cleanup as a reported check, and add a regression test where an earlier probe raises.
- [ ] Run `py -m pytest tests\test_transport_smoke.py -q` and confirm it passes.

### Task 3: Persist Construction Failures

**Files:**
- Modify: `edge_deploy/release.py`
- Test: `tests/test_release.py`
- Test: `tests/test_phases_deploy.py`

- [ ] Add a release regression test whose driver factory raises `TransportUnavailable` and assert a failed rollout is returned for that node while later nodes continue.
- [ ] Move transport construction inside the node failure boundary and convert construction errors into the same durable redacted reports used for authentication failures.
- [ ] Add a deploy-phase regression test and assert the node ledger state becomes `failed`, not `pending`.
- [ ] Run `py -m pytest tests\test_release.py tests\test_phases_deploy.py -q` and confirm it passes.

### Task 4: Verify Temporary-File Security Metadata

**Files:**
- Modify: `edge_deploy/ssh_transport.py`
- Test: `tests/test_ssh_transport.py`

- [ ] Add tests rejecting a non-regular part path, wrong owner, and mode other than `0600` while preserving the existing final file.
- [ ] Replace size-only `stat` with one metadata query that verifies regular-file type, effective-user ownership, mode `600`, and size before digest verification.
- [ ] Preserve best-effort part cleanup for every rejected metadata state.
- [ ] Run the focused upload tests and confirm they pass.

### Task 5: Remove Endpoints From Durable Evidence

**Files:**
- Modify: `edge_deploy/release.py`
- Test: `tests/test_release.py`

- [ ] Strengthen the durable-failure test so the configured node host itself is the private endpoint and assert it is absent from the serialized report and report file.
- [ ] Store the node label, not `node.host`, in synthetic transport-failure reports while preserving ordinary successful rollout reporting.
- [ ] Run the focused release tests and confirm they pass.

### Task 6: Full Verification

**Files:**
- Verify: all changed files

- [ ] Run `py -m ruff check .`.
- [ ] Run `py -m pytest -n 4 --dist loadfile`.
- [ ] Run `git diff --check`.
- [ ] Confirm no endpoint, secret, generated run evidence, or debug instrumentation was added.
- [ ] Record that Python 3.10 remains a CI gate if it is still unavailable locally.
