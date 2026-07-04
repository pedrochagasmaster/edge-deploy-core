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

- Add two clearly marked, untracked scratch files:
  - `prototype_paramiko_transport.py`: PEP 723 entry script pinning
    `paramiko==5.0.0`.
  - `edge_deploy/_prototype_paramiko_transport.py`: reusable transport
    experiment.
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

## Acceptance and Lifecycle

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
