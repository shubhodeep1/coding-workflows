# Plan: Validation Template-Scaffold Project

## Scope (one sentence)

Replace LLM-freehand `validation/` harness generation with **deterministic
Jinja2 template rendering** driven by a tracked `.ai/validate.yml` manifest per
consumer repo, so harnesses start correct, evolve via code review, and stop
breaking across cycles.

## Motivation

The last 10 failing validation runs analysed for this plan break down as:

| Class                    | Count | Example                                                                       |
| ------------------------ | ----: | ----------------------------------------------------------------------------- |
| LLM compliance failure   |     8 | `mongosh` asserted against `python:3.12-slim` app container (canary scope)    |
| Rule gap / real defect   |     2 | Foundry PATH lost on non-login shells; Flask `ALLOWED_HOSTS` reject on `app:` |

Eight of ten failures were the model regenerating a harness that violated a
rule **already present** in `prompts/mode-validate-generate.txt`. Strengthening
prose rules does not close this loop — the generator does not reliably obey
rules it already has. The only reliable way to enforce them is to bake them
into code that the LLM no longer touches.

## Q/A decisions (finalised)

| Q-ID  | Decision | Meaning                                                                                                             |
| ----- | -------- | ------------------------------------------------------------------------------------------------------------------- |
| Q1    | A        | Ship template-scaffold system in this repo; consumers pull templates instead of regenerating.                        |
| Q2    | A        | Manifest is `.ai/validate.yml`, committed in the consumer repo.                                                      |
| Q3    | A        | **Regenerate** on drift: refresh PR overwrites managed files; custom additions live in whitelisted escape hatches.   |
| Q4    | —        | (skipped)                                                                                                            |
| Q5    | A        | Per-stack template families (`python-mongo-flask`, `node-hardhat-solidity`); dispatch by `type` + signals.           |
| Q6    | B        | Keep the self-heal loop, but retarget it to re-render from manifest / re-run lint instead of freehand edits.         |
| Q7    | A        | **Zero human steps**: `validation-refresh.yml` opens an auto-mergeable PR on green; draft PR (needs human) on red.   |
| Q8    | A        | Auto-merge gate is pass-only; any lint or self-test regression downgrades to draft PR.                               |
| Q9    | A        | Encode ~30 hard-won rules from `prompts/mode-validate-generate.txt` into the templates and `validation_lint.py`.     |
| Q10   | A        | Ship the CANARY_TOOLS scope lint as a standalone PR first (done — see PR #1289).                                     |

## Architecture

```
consumer repo                         coding-workflows (this repo)
─────────────                         ──────────────────────────────
.ai/validate.yml  ────────────────►  workflow-templates/validation-harness/
                                       _shared/                 (Jinja2 base)
                                       python-mongo-flask/      (family)
                                       node-hardhat-solidity/   (family)

scripts/render_validation_templates.py
     │
     ├─ reads manifest
     ├─ picks family
     ├─ renders Dockerfile.app, docker-compose.test.yml,
     │   tests/00_canary.sh, tests/10_http_smoke.py, etc.
     └─ writes to consumer-repo checkout

scripts/validation_lint.py
     │
     ├─ CANARY_TOOLS scope (already live via PR #1289)
     ├─ shell-mode parity (/bin/sh -c vs -lc)
     ├─ init: true presence on long-running services
     ├─ graceful_shutdown readiness polling
     ├─ stdout/stderr bounded-tail capture
     ├─ external-tool dependency analysis (custom_tests)
     ├─ importlib dynamic-loading guard
     ├─ dependency_auditing sidecar rules
     └─ ~30 encoded rules total — one test each under tests/

.github/workflows/validation-refresh.yml
     │
     ├─ cron + workflow_dispatch
     ├─ renders templates against current manifest
     ├─ runs validation_lint.py
     ├─ runs self-test matrix (pass → auto-merge label)
     └─ opens PR (auto-merge on all-green, draft on any red)
```

## Stack families (what each template family produces)

Each family renders the full `validation/` tree for its stack. The shared
`_shared/` skeleton provides compose fragments, canary helpers, bounded-tail
log capture, and the `attempt_self_heal_and_reexec` integration points.

### `python-mongo-flask`

| File                                     | Purpose                                                                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `Dockerfile.app`                         | `python:3.12-slim` base; `apt-get install -y curl jq`; app deps via `pip install -r`; no service-side CLIs unless manifest opts in |
| `docker-compose.test.yml`                | `app`, `mongo` (per `services:`); `init: true` on every long-running service; `/bin/sh -c` healthchecks for sh parity              |
| `tests/00_canary.sh`                     | `CANARY_TOOLS` scoped to client-side only (`curl jq python3 pytest`); service-side CLIs go in a separate `svc_canary.sh`           |
| `tests/10_http_smoke.py`                 | Uses `TEST_HOST_HEADER` helper to defeat Flask `ALLOWED_HOSTS`/`Host:` checks when hitting the compose service name                |
| `_lib/import_audit.py`                   | Runs dependency_auditing via `subprocess.run([sys.executable, "-c", ...])` to isolate `sys.modules` side-effects                   |
| `_lib/graceful_shutdown.py`              | Post-SIGTERM readiness polling with bounded timeout + stdout/stderr tail capture on timeout                                        |
| `tests/90_tap_report.sh`                 | TAP-format `ok N` / `not ok N` aggregator                                                                                          |

### `node-hardhat-solidity`

| File                                     | Purpose                                                                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `Dockerfile.app`                         | `node:20-bookworm` base; `curl -L https://foundry.paradigm.xyz \| bash`; `foundryup`; **`ENV PATH=/root/.foundry/bin:$PATH`** so `forge`/`cast` resolve in non-login shells |
| `docker-compose.test.yml`                | `app`, `anvil` service; `init: true`; `validate.env` generated with **double-quoted** values to survive compose interpolation      |
| `tests/00_canary.sh`                     | `CANARY_TOOLS="curl jq node npx forge cast"` — all client-side; no `psql`/`mongosh`                                                |
| `tests/20_rpc_probe.sh`                  | `curl -s $RPC_URL --data '{...eth_blockNumber...}' \| jq -e '.result'` — probes **`.result`** not `.` (empty body must fail)       |
| `tests/30_hardhat_test.sh`               | `npx hardhat test --network localhost`; captures stdout/stderr tails on timeout                                                    |
| `_lib/graceful_shutdown.sh`              | Same pattern as python family, shell version                                                                                       |

## Encoded prompt rules (the ~30)

Each rule from `prompts/mode-validate-generate.txt` becomes either:

1. A **template invariant** (the generated file is structurally incapable of violating it), or
2. A **`validation_lint.py` check** (rendered output is scanned and rejected on violation).

| Rule (source line in `mode-validate-generate.txt`)                          | Encoded as                                                                 |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| 101–103, 491–492: service-CLI scope (`mongosh` etc. not in app CANARY_TOOLS) | Template invariant + `validation_lint.py` (already live in PR #1289)       |
| 131–137: shell-mode parity `/bin/sh -c` vs `-lc`                            | `validation_lint.py`: reject `-lc` in compose healthchecks                 |
| 255–321: PID 1 signal handling (`init: true`)                               | Template invariant: every long-running service gets `init: true`           |
| 360–424: graceful_shutdown + post-restart readiness polling                 | `_lib/graceful_shutdown.*` + lint check that tests import it               |
| 426–466: stdout/stderr capture with bounded tails                           | `_lib/` helper + lint check that failure paths invoke it                   |
| 469–504: custom-test external-tool dependency analysis                      | `validation_lint.py`: every tool in `custom_tests` must appear in Dockerfile |
| 622–756: importlib dynamic loading, dependency_auditing subprocess isolation | `_lib/import_audit.py` template invariant                                  |
| (…~22 more rules from the same prompt encoded 1:1)                          | Enumerated in `validation_lint.py` module docstring                        |

Each encoded rule ships with a dedicated test under
`tests/test_validate_lint_<rule>.py` following the pattern of
`tests/test_validate_preflight_canary_tools_scope.py`.

## Zero-human refresh workflow

`.github/workflows/validation-refresh.yml`:

```yaml
on:
  schedule: [{ cron: "17 4 * * *" }]
  workflow_dispatch: {}

jobs:
  refresh:
    steps:
      - checkout consumer repo
      - python scripts/render_validation_templates.py
      - python scripts/validation_lint.py validation/
      - run self-test matrix (docker compose up, run tests/*)
      - if all green:
          open PR with label `auto-merge:validation-refresh`
      - else:
          open DRAFT PR with failure log + link to manifest
```

The auto-merge gate (Q8=A) is **pass-only**: any lint regression, any
self-test regression, or any manifest drift downgrades the PR to draft and
pings a human via the standard `tg_notify` path.

## Milestones

M0 is intentionally **removed** — it shipped as PR #1289
(`validate_process: fail preflight when app CANARY_TOOLS reference uninstalled
service-side CLIs`) and is live on `claude/analyze-job-logs-JKNlF`.

### M1 — Scaffold + renderer (3–5 days)

- `workflow-templates/validation-harness/_shared/` Jinja2 base
- `scripts/render_validation_templates.py` (manifest → files)
- `scripts/templates/slot_manifest.schema.json` (JSON Schema for `.ai/validate.yml`)
- `VALIDATION_USE_TEMPLATES=1` opt-in flag in `validate_driver.sh`
- Unit tests: manifest parsing, family dispatch, render-to-tmp golden diffs

### M2 — `python-mongo-flask` family (2–3 days)

- All templates listed in the family table above
- `_lib/import_audit.py` subprocess isolation
- `TEST_HOST_HEADER` helper for Flask `ALLOWED_HOSTS` bypass
- Golden-output tests against the 5 python-mongo logs from the diagnosis set

### M3 — `node-hardhat-solidity` family (2–3 days)

- All templates listed in the family table above
- **`ENV PATH=/root/.foundry/bin:$PATH`** in Dockerfile (fixes log-7 regression)
- `validate.env` double-quoted generator (fixes log-9 compose interpolation)
- RPC probe uses `jq -e '.result'` not `jq -e '.'` (fixes log-8 false green)
- Golden-output tests against the 4 solidity logs from the diagnosis set

### M4 — `validation_lint.py` + `render` self-heal phase (2 days)

- `scripts/validation_lint.py` with all encoded rules
- New `attempt_self_heal_and_reexec "render"` phase: on lint fail, re-render from manifest, re-run lint, never hand to LLM freehand
- Per-rule test file under `tests/test_validate_lint_<rule>.py`

### M5 — `validation-refresh.yml` + auto-merge gate (2 days)

- Workflow file under `.github/workflows/`
- `auto-merge:validation-refresh` label handling in existing merge-bot
- Draft-PR fallback path with `tg_notify` integration
- Consumer-repo dispatch via `.github/ai/consumer_repos.json`

### M6 — Self-test coverage (2 days)

- `coding-workflows-test-python-mongo` fixture repo
- `coding-workflows-test-node-hardhat` fixture repo
- Nightly CI job that renders + validates against both fixtures
- Green-run streak counter surfaced in `README.md` badge

### M7 — Flip flag default + remove freehand path (after 2 weeks clean runs)

- `VALIDATION_USE_TEMPLATES` default flips to `1`
- Delete LLM-freehand harness generation from `validate_process.sh`
- Delete now-dead `prompts/mode-validate-generate.txt` sections superseded by templates
- Update `agents.md` + `README.md` with the new manifest-centric flow

## Risks & mitigations

| Risk                                                           | Likelihood | Impact | Mitigation                                                                                   |
| -------------------------------------------------------------- | ---------- | ------ | -------------------------------------------------------------------------------------------- |
| Consumer repo has bespoke validation not covered by any family | Medium     | Medium | Whitelisted escape hatches (`validation/custom/*.sh`) preserved across regeneration          |
| Template drift vs real-world images (e.g. new base image CVE)  | Medium     | Low    | Nightly `validation-refresh.yml` + pinned base tags + Dependabot on the template Dockerfiles |
| Lint false-positive blocks legitimate fix                      | Low        | Medium | Every lint rule has a per-rule escape (`# validation-lint: allow <rule> <reason>`)           |
| Two families are not enough to cover the fleet                 | Medium     | Medium | Family registry is pluggable; M1 renderer dispatches by `type` + signal list — add families as needed |

## Files changed (by milestone)

**M1:**
- NEW: `workflow-templates/validation-harness/_shared/**`
- NEW: `scripts/render_validation_templates.py`
- NEW: `scripts/templates/slot_manifest.schema.json`
- NEW: `tests/test_render_validation_templates.py`
- MODIFIED: `scripts/validate_driver.sh` (opt-in flag)

**M2:**
- NEW: `workflow-templates/validation-harness/python-mongo-flask/**`
- NEW: `tests/test_family_python_mongo_flask.py`

**M3:**
- NEW: `workflow-templates/validation-harness/node-hardhat-solidity/**`
- NEW: `tests/test_family_node_hardhat_solidity.py`

**M4:**
- NEW: `scripts/validation_lint.py`
- NEW: `tests/test_validate_lint_*.py` (one per encoded rule)
- MODIFIED: `scripts/validate_process.sh` (add `"render"` phase to `attempt_self_heal_and_reexec`)

**M5:**
- NEW: `.github/workflows/validation-refresh.yml`
- MODIFIED: `.github/ai/consumer_repos.json` (dispatch targets)
- MODIFIED: merge-bot config (`auto-merge:validation-refresh` label)

**M6:**
- NEW: `coding-workflows-test-python-mongo` (separate repo; tracked here via submodule or fixture fetch)
- NEW: `coding-workflows-test-node-hardhat` (ditto)
- NEW: `.github/workflows/nightly-validation-selftest.yml`

**M7:**
- DELETED: LLM-freehand sections in `scripts/validate_process.sh`
- DELETED: superseded sections of `prompts/mode-validate-generate.txt`
- MODIFIED: `README.md`, `agents.md`

## Success criteria

- Zero `needs_fixes` cycles caused by the 8 LLM-compliance failure classes
  identified in the 10-log diagnosis set.
- `validation-refresh.yml` produces auto-mergeable PRs on ≥ 95% of runs across
  both fixture repos for 14 consecutive days before M7 flips the default.
- Mean time from a new encoded rule (new entry in `validation_lint.py`) to
  consumer-repo enforcement ≤ 24h via the refresh workflow.

<!-- anchor:validation-templates-status -->
<!-- Append new status snapshots below; do not rewrite prior snapshots. -->

## Source-of-truth status snapshots

### Snapshot — 2026-04-21

| Milestone | Status | Objective evidence from repository |
| --- | --- | --- |
| M1 — Scaffold + renderer | Done | `workflow-templates/validation-harness/_shared/**`, `scripts/render_validation_templates.py`, `scripts/templates/slot_manifest.schema.json`, `tests/test_render_validation_templates.py`, template-mode wiring in `scripts/validate_process.sh`, bootstrap fetch/staging in `.github/workflows/validate.yml` |
| M2 — `python-mongo-flask` family | Done | `workflow-templates/validation-harness/python-mongo-flask/**`, `tests/test_family_python_mongo_flask.py`, `tests/fixtures/validation_harness/python_mongo_flask/**` |
| M3 — `node-hardhat-solidity` family | Done | `workflow-templates/validation-harness/node-hardhat-solidity/**`, `tests/test_render_validation_templates.py`, `tests/test_render_validation_templates_node_hardhat_regressions.py`, `tests/test_validate_harness_rpc.py` |
| M4 — `validation_lint.py` + render self-heal | Done | `scripts/validation_lint.py`, `tests/test_validate_lint_*.py`, render self-heal path `attempt_self_heal_and_reexec "render"` in `scripts/validate_process.sh`, `tests/test_validate_process_render_recovery.py` |
| M5 — Refresh workflow + auto-merge gate | Partial | Present: `.github/ai/consumer_repos.json`. Missing: `.github/workflows/validation-refresh.yml`; no merge-bot/runtime handling found for label `auto-merge:validation-refresh` outside this planning doc |
| M6 — Nightly self-test coverage | Remaining | Missing in this repository: `.github/workflows/nightly-validation-selftest.yml`; fixture-repo wiring and README streak badge are not present in tracked files |
| M7 — Flip default + remove freehand path | Remaining | `VALIDATION_USE_TEMPLATES` default remains `false` in `scripts/validate_process.sh`; freehand generation path remains in `scripts/validate_process.sh`; `prompts/mode-validate-generate.txt` remains active |

### Factual conflicts captured (plan prose vs codebase truth)

- Milestone prose cites `scripts/validate_driver.sh` for template-flag wiring; current implementation is in `scripts/validate_process.sh`.
- Milestone prose references `tests/test_family_node_hardhat_solidity.py`; that test file is absent, while coverage is present via `tests/test_render_validation_templates_node_hardhat_regressions.py` and `tests/test_validate_harness_rpc.py`.
- Milestone prose lists `.github/workflows/validation-refresh.yml` and `.github/workflows/nightly-validation-selftest.yml`; both workflow files are currently absent.

### Remaining implementation backlog (unresolved scope only)

- [ ] Add `.github/workflows/validation-refresh.yml` with render, lint, self-test, and PR-open/draft fallback flow.
- [ ] Add merge-bot handling for the `auto-merge:validation-refresh` label.
- [ ] Add nightly validation self-test workflow at `.github/workflows/nightly-validation-selftest.yml`.
- [ ] Add fixture-repo wiring for python-mongo and node-hardhat self-test targets.
- [ ] Add README surface for nightly validation green-run streak reporting.
- [ ] Flip `VALIDATION_USE_TEMPLATES` default from `false` to `true` after stability soak.
- [ ] Remove freehand harness generation path from `scripts/validate_process.sh` once template mode is default.
- [ ] Remove superseded freehand sections from `prompts/mode-validate-generate.txt` after cutover.
- [ ] Update `agents.md` and `README.md` with the new manifest-centric flow.
