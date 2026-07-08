# Plan 010: Redact known transient secret values, not just key=value patterns

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d8ec786..HEAD -- edge_deploy/reporting.py edge_deploy/auth.py edge_deploy/publish.py tests/test_reporting.py tests/test_auth_seam.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Secret-handling rule for the executor**: never write a real credential
> value into any file, test, log, or report. All test secrets must be
> obviously fake (e.g. `"fake-passcode-123456"`).

## Status

- **Priority**: P2
- **Effort**: S–M
- **Risk**: LOW (additive redaction layer; existing regex redaction unchanged)
- **Depends on**: none
- **Category**: security (defense-in-depth for ADR-0002's redaction mandate)
- **Planned at**: commit `d8ec786`, 2026-07-07

## Why this matters

ADR-0002 mandates that RSA passcodes, Kerberos passwords, and the Bitbucket
token never reach a report or log. Today's enforcement is pattern-based: a
regex masks `passcode=`/`password=`/`token=` assignments and
`Authorization: Bearer` headers. That protects the *known formats*. But
reports embed pane screen tails and remote command output on failures
(`runner.py` includes a screen tail in `RunnerProtocolError`, rollout reports
carry `stdout_tail`), and a secret that surfaces in any *other* shape — a
password echoed raw by a misbehaving prompt, a token appearing in git output —
passes the regex untouched. The engine, uniquely, *knows the exact secret
strings* it handled this process (it read them from the operator or the
environment). Masking those exact values everywhere is strictly stronger than
guessing formats, costs a few lines, and cannot false-negative on a value it
was told about.

## Current state

- `edge_deploy/reporting.py:17-37` — the existing redaction:

  ```python
  # Secrets are forwarded transiently (RSA passcode, Kerberos password, BB token); they
  # must never reach a report or log. Match ``key=value`` up to the next whitespace/quote.
  _SECRET_RE = re.compile(r"(?i)\b(passcode|password|token)=([^\s'\"]+)")
  _BEARER_RE = re.compile(r"(?i)Authorization:\s*Bearer\s+\S+")
  _REDACTED = "***REDACTED***"


  def redact(text: str) -> str:
      """Mask secret assignments and Bearer auth headers in a string."""
      masked = _SECRET_RE.sub(rf"\1={_REDACTED}", text)
      return _BEARER_RE.sub(f"Authorization: Bearer {_REDACTED}", masked)
  ```

  `_redact_obj` (lines 30-37) recursively applies `redact` to strings in
  dicts/lists; `write_report` (line 114) and the release-report writer route
  all persisted payloads through it.

- `edge_deploy/auth.py:21-25` — where interactive secrets enter the process:

  ```python
  def _prompt_for_secret(prompt: str) -> str:
      """Read a secret from the operator terminal without hidden prompts."""
      sys.stdout.write(prompt)
      sys.stdout.flush()
      return sys.stdin.readline().rstrip("\n")
  ```

  `AuthBroker` forwards these via `TmuxDriver.submit_secret`
  (`tmux_driver.py:446`), never via `run_remote` — that part is sound and
  unchanged by this plan.

- `edge_deploy/publish.py:225` — where the Bitbucket token enters:
  `token = os.environ.get(token_env, "")`. Note `publish.py:190-195` already
  deliberately avoids echoing git argv (which carries the Bearer header) into
  errors; this plan adds the value-level backstop, it does not replace that.

- `tests/test_reporting.py` — exemplar test style (plain functions, build an
  `OperationReport`, assert on `to_payload()`/`write_report` output). Also
  note `tests/test_auth_seam.py` for AuthBroker test patterns.

- CONTEXT.md vocabulary: the redaction boundary is part of the **Run Ledger /
  reporting** evidence chain; redacted release bundles are appended to the
  Bitbucket-only `release-log` branch — i.e. redaction failures end up in a
  *pushed* branch, which is why defense-in-depth is warranted.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Install | `py -m pip install -e ".[dev]"` | exit 0 |
| Focused | `py -m pytest tests/test_reporting.py tests/test_auth_seam.py tests/test_publish.py -q` | all pass |
| Full | `py -m pytest -n 4 --dist loadfile` | all pass |
| Lint | `py -m ruff check .` | no new violations |

## Scope

**In scope**:
- `edge_deploy/reporting.py` — add the registry and wire it into `redact`
- `edge_deploy/auth.py` — register prompted secrets
- `edge_deploy/publish.py` — register the token once read (and the same in
  `edge_deploy/mirror.py` / `edge_deploy/audit.py` ONLY if they read the token
  env var themselves rather than through `publish.py`; check with
  `grep -rn "environ" edge_deploy/mirror.py edge_deploy/audit.py`)
- `tests/test_reporting.py`, `tests/test_auth_seam.py` — new tests

**Out of scope**:
- Changing or removing `_SECRET_RE` / `_BEARER_RE` — the regexes stay as the
  first layer.
- Console output paths (`print`) — this plan hardens *persisted* output
  (reports); a console-redaction sweep is a separate concern. (Exception:
  `cli.py:765` already routes one console line through `redact`; do not
  regress it.)
- Persisting registered values anywhere, in any form, including hashed.
  The registry is process-memory only.

## Git workflow

- Branch: `advisor/010-secret-value-redaction` off `main`; PR per CONTRIBUTING.md.
- Commit style: `fix: redact known transient secret values in all written reports (ADR-0002)`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the registry to reporting.py

Below the existing `_REDACTED` constant, add:

```python
# Exact secret values seen by this process (RSA passcodes, Kerberos passwords,
# BB token). Value-level masking backstops the pattern-based rules above: a
# secret that leaks in an unanticipated shape is still caught. Never persisted.
_TRANSIENT_SECRETS: set[str] = set()
_MIN_SECRET_LENGTH = 6  # refuse trivially short values; masking e.g. "1" would shred reports


def register_transient_secret(value: str) -> None:
    """Record a secret *value* so ``redact`` masks every occurrence of it."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _TRANSIENT_SECRETS.add(value)
```

Extend `redact` to mask registered values after the regex passes,
longest-first (so a value that is a substring of another is handled
correctly):

```python
def redact(text: str) -> str:
    """Mask secret assignments, Bearer auth headers, and registered secret values."""
    masked = _SECRET_RE.sub(rf"\1={_REDACTED}", text)
    masked = _BEARER_RE.sub(f"Authorization: Bearer {_REDACTED}", masked)
    for secret in sorted(_TRANSIENT_SECRETS, key=len, reverse=True):
        masked = masked.replace(secret, _REDACTED)
    return masked
```

Plain `str.replace`, not regex — values must not be interpreted as patterns.

**Verify**: `py -m pytest tests/test_reporting.py -q` → existing tests still pass.

### Step 2: Register secrets at their entry points

1. `edge_deploy/auth.py`: in `_prompt_for_secret`, after reading the line and
   before returning, call `reporting.register_transient_secret(value)`
   (import `from edge_deploy.reporting import register_transient_secret` —
   check for import cycles: `auth.py` already imports `ReportCheck` from
   `reporting`, so this is safe).
2. `edge_deploy/publish.py`: immediately after `token = os.environ.get(token_env, "")`
   (line 225) and its non-empty check, call `register_transient_secret(token)`.
3. Run `grep -rn "os.environ" edge_deploy/` — for every other site that reads
   a credential env var (at planning time: check `mirror.py` and `audit.py`),
   add the same one-line registration. Sites that read non-secret env vars
   (paths, flags) must NOT be registered.

**Verify**: `grep -rn "register_transient_secret" edge_deploy/` → one
definition in `reporting.py`, one call in `auth.py`, one per token-reading
site in publish/mirror/audit as found.

### Step 3: Tests

In `tests/test_reporting.py` (model on the existing plain-function style):

1. `test_registered_secret_value_is_masked_everywhere` — register
   `"fake-passcode-123456"`, build an `OperationReport` whose check message
   embeds it raw (no `key=` prefix, e.g. `"screen tail: fake-passcode-123456 rejected"`),
   `write_report` to `tmp_path`, read the JSON back, assert the value is
   absent and `***REDACTED***` present.
2. `test_short_or_empty_values_are_not_registered` — register `""` and
   `"abc"`; assert a message containing `"abc"` survives unmasked (no
   report-shredding on trivial values).
3. `test_longest_registered_value_wins` — register `"fake-secret"` and
   `"fake-secret-extended"`; a message containing the longer one is masked as
   one unit (no `***REDACTED***-extended` remainder... assert the *longer*
   value's remnant `-extended` does not appear... i.e. the output contains
   `***REDACTED***` and not `extended`).
4. Registry isolation: because `_TRANSIENT_SECRETS` is module-global, add an
   autouse-free explicit cleanup in each new test
   (`monkeypatch.setattr(reporting, "_TRANSIENT_SECRETS", set())` at the top)
   so tests don't leak registrations into each other or into unrelated tests.

In `tests/test_auth_seam.py`: one test asserting that after the auth seam
prompts for a code (drive it with the existing scripted-prompt pattern in that
file), the prompted fake value is masked by `reporting.redact` — proving the
entry-point wiring, not just the registry.

**Verify**: `py -m pytest tests/test_reporting.py tests/test_auth_seam.py -q` → all pass.

### Step 4: Full suite

**Verify**: `py -m pytest -n 4 --dist loadfile` → all pass;
`py -m ruff check .` → no new violations in touched files.

## Test plan

Steps 3.1–3.4 plus the auth-seam wiring test — five new tests. Pattern
exemplars: `tests/test_reporting.py:33`
(`test_operation_report_to_payload_full_contract`) for report construction;
`tests/test_auth_seam.py` scripted prompts for the seam test.

## Done criteria

- [ ] `py -m pytest -n 4 --dist loadfile` exits 0
- [ ] `grep -n "register_transient_secret" edge_deploy/reporting.py edge_deploy/auth.py edge_deploy/publish.py` → definition + ≥ 2 call sites
- [ ] New tests present: `grep -c "register_transient_secret" tests/test_reporting.py` ≥ 3
- [ ] No real-looking secret literals added anywhere: `grep -rniE "(passcode|password|token)" tests/ | grep -v fake` shows only pre-existing lines (manual eyeball)
- [ ] No files outside the in-scope list modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- Importing `reporting` from `auth.py` or `publish.py` creates a circular
  import (planning-time check says it won't — `auth.py:17` already imports
  from `reporting` — but if it does, report rather than restructuring modules).
- Any existing test fails because production code was *depending* on a secret
  value appearing in output (that would be a real leak — report it as a
  security finding, with file:line only, never the value).
- You find an entry point handling secrets that this plan didn't list
  (e.g. Kerberos password read somewhere other than `auth.py`) — add the
  registration only if it's the same one-line pattern; otherwise report.

## Maintenance notes

- Anyone adding a new credential source (a new env var, a new prompt) must
  call `register_transient_secret` at the read site — reviewers should ask
  this question on any PR that touches auth or adds an env var read. Consider
  noting it in AGENTS.md's learned facts on a future docs pass.
- The registry is intentionally process-lifetime; guided releases hold
  secrets in memory for the run anyway (ADR-0010), so no clearing API is
  needed now. If a clearing API is added later, it must not log what it clears.
- This does not change what is *sent over the pane* (submit_secret path is
  already non-echoed); it hardens what is *written to disk and pushed to the
  release-log branch*.
