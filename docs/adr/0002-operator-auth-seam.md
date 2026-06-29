# Operator delivers RSA/Kerberos secrets via forwarded getpass, per node

A **Release** automates everything except interactive auth. When an
**Authenticated Pane** reaches `Enter PASSCODE:` (or `kinit` asks for a Kerberos
password), the orchestrator prompts the **Operator** with `getpass` in the
console and forwards the secret into the pane, then resumes unattended.
Authentication is serial, one **Edge Node** at a time, because RSA SecurID codes
are single-use and rotate ~every 60s. Secrets are held only transiently in
memory and are redacted from all logs and reports.

This deliberately overrides the prior per-repo guidance ("never handle PASSCODE
in chat or scripts"). Forwarding a `getpass`-read code is strictly safer than
robocop's existing `--passcode` CLI flag, which leaks the code into shell history
and the process argument list.

## Considered Options

- **Pane-direct (Operator attaches and types into the pane).** Honours the old
  no-touch rule but breaks the "automate the rest" goal: the Operator must drive
  tmux, and the orchestrator cannot deterministically resume.
- **`--passcode` on the CLI (today's robocop model).** Rejected: leaks the
  single-use code into history/argv; no better for safety and worse for UX.
- **getpass with a pane-direct fallback flag.** Acceptable later, but the
  default is getpass-forward.

## Consequences

- Kerberos is prompted only when a requested smoke level needs it; the default
  RSA-only flow asks for one code per node.
- On a rejected/stale code the orchestrator re-prompts (robocop already detects
  re-display of the prompt and `Permission denied`).
- Report redaction (`passcode=`/`password=`/`token=`) is a hard requirement, not
  best-effort.
