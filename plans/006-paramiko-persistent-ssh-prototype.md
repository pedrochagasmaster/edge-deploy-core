# Paramiko Persistent SSH Prototype

## Summary

Build an uncommitted, disposable terminal prototype proving that Paramiko can
replace the tmux/Base64 transport. Paramiko supports keyboard-interactive
authentication, multiple channels, and SFTP over one connection. See the
[official Paramiko documentation](https://docs.paramiko.org/en/stable/).

Run with:

```powershell
uv run prototype_paramiko_transport.py --node node03
```

## Implementation

- Add two clearly marked, untracked scratch files, both **outside**
  `edge_deploy/`:
  - `prototype_paramiko_transport.py`: PEP 723 entry script pinning
    `paramiko==5.0.0`.
  - `prototype_paramiko_transport_lib.py`: reusable transport experiment.
  - Never place prototype `.py` files inside `edge_deploy/`: Engine Identity
    (`ledger._content_sha256`) hashes every `*.py` under the package
    directory, tracked or not, so creating the file would orphan any open
    Run and deleting it would orphan any Run created while it existed
    (ADR-0008 identity skew is refused at every phase entry).
- Load the existing private operator configuration and parse `user@host`, port,
  and keepalive settings.
- Verify the server key strictly against `~/.ssh/known_hosts`; never auto-accept
  unknown keys.
- Authenticate through Paramiko's keyboard-interactive callback using a hidden
  controller-terminal passcode prompt.
- Maintain one authenticated transport per node and open stateless channels for:
  - commands with separate stdout, stderr, and exit status;
  - SFTP upload/download;
  - PTY-backed interactive input;
  - keepalive validation.
- Present an interactive checklist showing each probe as pending, passed, or
  failed. Provide `[a] run all` and individual probe actions.
- Hold passcodes only in callback scope. Do not log or persist secrets,
  configuration, hostnames, or generated evidence.
- Create remote scratch files under `~/.edge-deploy/`, verify them, and remove
  them in `finally`.
- Do not modify production modules, `pyproject.toml`, tests, or Git history.

## Live Probe Scenarios

- Authenticate once and prove every later action reuses the same SSH transport.
- Execute a command that emits exact stdout and stderr and exits with code `7`.
- Upload and download an 8 MiB random payload through SFTP; require identical
  SHA-256 hashes and report throughput.
- If SFTP is unavailable, report that separately and try a binary exec-channel
  fallback so transport viability remains distinguishable from subsystem
  availability.
- Send a generated dummy secret through a PTY channel and validate it remotely
  without using a real Kerberos password.
- Set a five-second keepalive and complete a command lasting across two
  keepalive intervals.
- Close the connection explicitly and confirm remote scratch cleanup.

## Risks and Open Questions

- **Policy vs. tooling.** ADR-0011 rejected scp/sftp as "not permitted through
  the RSA-authenticated interactive channel" and ControlMaster as broken on the
  Windows OpenSSH stack. Paramiko bypasses the tooling problem entirely, but a
  fully green probe run proves *capability*, not *permission*: if policy
  mandates human-interactive sessions only, a programmatic transport may be a
  compliance problem no probe detects. Confirm with the policy owner in
  parallel before designing the production transport.
- **Key negotiation is the likely first failure.** Strict `known_hosts`
  verification is required, but the operator's file was written by Windows
  OpenSSH; Paramiko can be picky about key types (rsa-sha2 negotiation, hashed
  entries). An auth-probe failure here means "config work", not "hypothesis
  dead" — record it as such.
- **Dependency surface.** Production adoption adds `paramiko` (and its binary
  `cryptography` wheel) to the operator `[release]` extra on a corporate-managed
  Windows workstation — an approval question, not a technical one.
- **What survives regardless of outcome:** `remote_python.py` interpreter
  resolution (node heterogeneity is transport-independent), LF-only staging
  (SFTP preserves bytes, so CRLF risk moves back to file-creation time on
  Windows), and ADR-0009 file evidence (its scrolled-screen rationale
  evaporates, but files still buy durability across disconnects and post-hoc
  audit — keep the contract, read the files over the new channel).

## Acceptance and Lifecycle

- Before the live run, confirm no open Runs exist for the target tool (or
  explicitly accept abandoning them) — belt-and-braces even with the scratch
  files outside the package.
- The hypothesis passes if node03 authenticates through one prompt, all channel
  probes preserve exact data, the transfer hash matches, and the connection
  survives the keepalive probe.
- Performance is measured but not claimed superior without a tmux baseline;
  structural evidence will record one streaming channel versus Base64 expansion
  and hundreds of pane round trips.
- Run node04 only if node03 succeeds and a second-node compatibility check is
  useful.
- No automated tests are added because this is throwaway prototype code.
- After the live run, capture the verdict in the conversation, then either
  delete the scratch files or use the findings to design the production
  transport.
- If the hypothesis passes, the production design introduces a `Transport`
  seam behind `runner.py` with the pane retained as fallback, keeps the
  AuthBroker boundary (ADR-0002) as the keyboard-interactive callback, and is
  recorded as ADR-0013 superseding the transport-mechanics half of ADR-0011
  while keeping its lesson: treat whatever channel you are given as hostile
  until proven byte-clean. The connection is persistent per phase invocation,
  not per Run — phases are separate short processes, so RSA re-auth per deploy
  command remains, matching current behavior.
