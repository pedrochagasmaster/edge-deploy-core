# Implementation Plans

Advisor-generated plans for edge-deploy-core. Plans 001–006 predate this
index; plans 007–010 were produced by the deep audit of 2026-07-07 at commit
`d8ec786`. Execute in the order below unless dependencies say otherwise. Each
executor: read the plan fully before starting, honor its STOP conditions, and
update your row when done.

> This audit ran non-interactively: the top findings by leverage were planned
> by default (skill fallback). The maintainer may re-order, reject, or request
> plans for the deferred findings listed below.

## Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| 001 | Hermetic money-path tests in CI | P1 | M | — | DONE (absorbed by PR-16 fake-driver rework) |
| 002 | Stage-evidence linewrap | P1 | M | — | DONE (absorbed by PR-14/16; D8 protocol replaced screen parsing) |
| 003 | CI gates: ruff, parallel pytest, honest failures (+2026-07-07 addendum: pip cache, pre-fix `runner.py:44` E501) | P1 | S | — | TODO (re-verified valid 2026-07-07) |
| 004 | Release-ledger architecture (M1–M5) | P0 | XL | — | DONE (landed as ADR-0008/0012, v1.3.0 phases) |
| 005 | Release-ledger PR breakdown | P0 | — | 004 | DONE (companion to 004) |
| 006 | Paramiko persistent-SSH prototype | P2 | M | — | DONE (spike verdict recorded in the plan file; productized via 011) |
| 007 | Drift → D8 file evidence + batched local hashing | P1 | M | — | TODO |
| 008 | Coverage measurement + CI gate | P2 | S | 003 | TODO |
| 009 | Error-path test hardening (deps delivery, ledger retry, drift globs) | P2 | M | — | TODO |
| 010 | Transient-secret redaction registry | P2 | S–M | — | TODO |
| 011 | Paramiko transport productization (Transport seam, M0 gates, pane fallback) | P1 | L | 006 (verdict = M0), 007 recommended first | DONE (landed as ADR-0014, v1.5.0: RemoteTransport seam, Paramiko default, pane fallback, transport-smoke; shipped before 007) |
| 012 | Reuse durable verification and preserve publish diagnostics | P1 | M | — | DONE (landed as #34) |

Status values: TODO | IN PROGRESS | DONE | BLOCKED (with one-line reason) | REJECTED (with one-line rationale)

## Dependency notes

- 008 depends on 003 because both edit `.github/workflows/ci.yml`; 008
  contains an integration note if 003 lands first (expected) or is abandoned.
- 009 composes with 008: re-measure the coverage baseline after 009 lands.
- 007 and 003's addendum each change the engine content hash — every merge of
  engine code orphans open runs by design (CONTEXT.md "Engine Identity");
  complete or abandon open runs before upgrading the installed engine.
- 007 is unaffected by 009 and vice versa (009 documents the one overlap:
  `_glob_regex` is untouched by 007).
- 011 landed (ADR-0014, v1.5.0) with its M0 satisfied by the plan-006 live
  probe. The `explore/paramiko-ssh-transport` branch is deleted; its verdict
  and risk notes live in `006-paramiko-persistent-ssh-prototype.md`. It
  shipped ahead of 007, so drift still reads over the pane protocol on
  `transport: pane` nodes — 007 remains worthwhile on its own.

## Direction options (maintainer decision, not ranked against fixes)

Grounded suggestions from the 2026-07-07 audit, not yet planned:

1. **Productize the Paramiko transport** — planned as
   `011-paramiko-transport-productization.md` (2026-07-07) and landed as
   ADR-0014 in v1.5.0.
2. **Release-history query CLI** (`list-releases` / per-node history). The
   data already exists twice — `edge-deploy/runs/*/events.jsonl` locally and
   the redacted bundles on the Bitbucket `release-log` branch — but answering
   "what shipped to node03 last Tuesday?" is manual directory spelunking
   (`scripts/e2e-release.ps1` hand-searches `edge-deploy/runs/`). M effort.
3. **Rollback decision guidance**: `audit.py:_attempt_requires_resolution()`
   already encodes when a failed attempt changed deployed state; surfacing it
   as an `analyze-failure` command (retry / rollback / safe-to-release verdict
   with evidence) turns ADR-0003's "no auto-rollback" policy into guided
   operator judgment. M effort.

## Findings considered and rejected (do not re-audit)

- **Publish stale-parent race** (`publish.py:296-310`): the snapshot push is a
  plain (non-force) push, so a stale parent is rejected server-side as
  non-fast-forward; single-operator tool besides. No corruption can land.
- **Shell-injection via smoke commands / repo_path / operator_email**
  (`verify.py:35`, `rollout.py:161-176`): no trust boundary crossed — the
  committed Tool Profile and local operator config already fully control what
  runs on the node by design (ADR-0004); smoke commands are intentionally
  shell commands. Blanket `shlex.quote` would also fight the pane's documented
  quote-stripping (ADR-0011).
- **BB_TOKEN visible in process argv** during git calls: documented
  convention (ADR-0002 / README); error paths already avoid echoing argv
  (`publish.py:190`). Reports mask Bearer headers; plan 010 adds value-level
  masking. Credential-helper migration judged not worth the complexity now.
- **Profile-driven path traversal into git blobs** (`drift.py`): the profile
  lives in the same repo it would "exfiltrate" from; no boundary.
- **Audit-branch write confirmation**: append-only audit of every attempt is
  the point (ADR-0008); a confirmation prompt would weaken the record.
- **Engine-identity hash recomputed per phase** (`ledger.py:46-64`):
  milliseconds per phase; micro-optimization.
- **Docs phase-order drift** (`docs/release-workflow.md:82`): false report —
  the example already matches `cli.py:51` (`tag_bitbucket` before `tag_github`).
- **Committed run reports referencing missing plans**: false report —
  `edge-deploy/reports/` is gitignored; those are local artifacts.
- **`build/` / `edge_deploy_core.egg-info/` at repo root**: local build
  artifacts, not git-tracked, already gitignored.
- **cli.py god-module split** (879 lines, 36 functions): real but MED-risk L
  refactor with modest payoff while the module is stable post-1.3.0; revisit
  if cli.py churn resumes.
- **`run_release` 12-kwarg signature** (`release.py:342`): test seams doing
  their job; a config-dataclass regrouping is cosmetic. Not now.
- **Pre-commit hooks / .editorconfig**: marginal on a PowerShell 5.1-only
  controller; CI gates (plan 003) deliver the same protection where it binds.
- **EdgeDeployError operator-message hierarchy** (`cli.py:859-874` catches 11
  exception types with one generic format): legitimate DX improvement,
  deferred below the cut line; candidate for a future plan.
- **ADR-0007 missing the own-commit-hook migration scenario**
  (`publish.py:280-310` implements it): legitimate small docs gap, deferred;
  fold into the next docs pass.
- **README lacks full subcommand list**: minor; fold into the next docs pass.
