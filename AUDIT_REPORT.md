# Repository Audit Report

**Date**: 2026-03-22
**Scope**: Full dependency, structure, documentation, and runtime audit

---

## Executive Summary

The repo is **structurally sound** — all Python scripts import cleanly using only
stdlib modules, all JSON schemas/configs parse correctly, and the core workflow
YAML files reference existing paths. However, there are **3 bugs** (1 critical,
2 low), **2 inconsistencies**, and **several observations** worth addressing.

---

## Critical Issues

### 1. `memory_maintenance.yml` — Missing branch checkout + bypasses CLI (CRITICAL)

**File**: `.github/workflows/memory_maintenance.yml` (lines 31, 38-51, 66)

The workflow checks out the **default branch** (`actions/checkout@v5`), then
calls `compact_memory(Path('.github/ai-memory'), ...)` directly. However, the
`.github/ai-memory/` directory only exists on the dedicated `ai-memory` git
branch — it does not exist on the default branch. This means:

- `compact_memory()` will fail or no-op (the path doesn't exist)
- `git add .github/ai-memory/` on line 66 will add nothing
- The `ai-memory/README.md` explicitly states: *"Workflows must use CLI
  subcommands and avoid duplicated inline memory logic."*

**Fix**: Replace the inline Python with the CLI command, which handles branch
switching via `persist_memory_operation`:

```yaml
python3 scripts/ai_memory.py compact \
  --month "${ARCHIVE_MONTH}" \
  --prune true
```

And remove the manual `git add / git commit / git push` step (the CLI handles
persistence and push retries).

---

## Low-Severity Issues

### 2. `review_autofix.yml` — Ghost reference to old workflow name (LOW)

**File**: `.github/workflows/review_autofix.yml` (line 473)

```bash
cp .github/workflows/ai-auto-review-and-edit.yml "${RUNTIME_CONTEXT_DIR}/workflow_snapshot.yml" || true
```

`ai-auto-review-and-edit.yml` was likely the pre-refactoring name of
`review_autofix.yml`. The file doesn't exist. The `|| true` prevents failure,
but the intended workflow snapshot will never be created.

**Fix**: Change to `review_autofix.yml` or remove the line if the snapshot is
no longer needed.

### 3. `ai_pipeline.md` — Stale references to old workflow names (LOW)

**File**: `ai_pipeline.md` (lines ~594, ~600)

References `ai-auto-review-and-edit.yml` which no longer exists. These are
documentation references, not runtime paths, but they're misleading.

**Fix**: Update references to the current workflow names.

---

## Inconsistencies

### 4. `setup-runtime` composite action is defined but never used

**File**: `.github/actions/setup-runtime/action.yml`

This composite action installs Node.js, Python, Codex CLI, validates env vars,
and sets up the runtime workspace. However, **no workflow references it** — each
workflow independently implements its own setup steps.

**Impact**: Code duplication across workflows; the action exists for
consolidation but was never wired in. `docs/compatibility-matrix.md` (line 32)
mentions it as the installer, which is misleading.

### 5. `research/` directory contains pre-refactoring analysis

**File**: `research/squad-workflow-improvements-20260322T042436Z.md`

References old workflow filenames (`ai-clarify.yml`, `ai-plan.yml`,
`ai-implement.yml`, `ai-auto-review-and-edit.yml`, `ai-issue-pr-status.yml`)
that were the consumer-side wrappers before the refactoring into reusable
`workflow_call` workflows. This is a research artifact and not actionable, but
could confuse future contributors.

---

## What Works Well

| Area | Status | Notes |
|------|--------|-------|
| **Python scripts** | All import cleanly | stdlib-only; no external packages needed |
| **`ai_memory.py` CLI** | Fully functional | All 6 subcommands parse and dispatch correctly |
| **`ai_memory_lib.py`** | Imports OK | Schema validation, governance, branch-safe persistence |
| **`ai_context_utils.py`** | Imports OK | Envelope builders, attachment handling, SSRF protection |
| **`issue_attachment_bundle.py`** | Imports OK | Present in `scripts/` |
| **`git_ref_health_check.sh`** | Valid bash | Correct `set -euo pipefail`, handles check/repair modes |
| **JSON schemas** | All parse OK | `memory_record.v1`, `run_ledger_entry.v1`, `task_lineage.v1` |
| **Retrieval profiles** | Valid JSON | 5 roles with deterministic scoring weights |
| **Example files** | Valid JSON | All 3 sample files parse correctly |
| **`netwask/tier1-agent-suite.json`** | Valid JSON | Agent suite config |
| **Script path refs in workflows** | Correct | `scripts/` and `prompts/` paths match actual locations |
| **Prompt template files** | All present | `header.txt`, `mode-clarify.txt`, `mode-plan.txt`, `mode-implement.txt` |
| **Root instruction files** | All present | `codex_system_instructions.md`, `ai_pipeline.md`, `unattended_llm_system_instructions.md` |

---

## README Accuracy

The `README.md` is **mostly accurate**:

- Workflow table matches actual files
- Required secrets/variables are correct
- Repository structure section matches actual layout
- Versioning section aligns with `docs/release-policy.md`

**One minor inaccuracy**: The README says `See workflow-templates/ in consumer
repos for all wrapper examples` — this is contextually correct (consumer repos
would have these) but could be clearer that no such directory exists in _this_
repo.

---

## Dependency Summary

### Runtime Dependencies (Workflows)

| Dependency | Type | Pinned | Notes |
|------------|------|--------|-------|
| `@openai/codex@v0.114.0` | npm | Yes | Installed by workflows |
| Python 3.12 | runtime | Yes | stdlib only, no pip packages |
| Node.js 22 | runtime | Yes | For Codex CLI |
| `jq`, `curl`, `gh` | system | No | Installed via apt if missing |
| `actions/checkout@v5` | GH Action | Yes | |
| `actions/setup-python@v5` | GH Action | Yes | |
| `actions/setup-node@v4` | GH Action | Yes (in composite only) | |

### External Services

| Service | Required | Secret |
|---------|----------|--------|
| OpenRouter API | Yes | `OPENROUTER_API_KEY` |
| GitHub API | Yes | `GH_PAT` |
| Telegram Bot API | No | `TG_BOT_SECRET` |

### Unmet Dependencies

**None** — all Python imports are stdlib, all referenced files exist, all JSON
configs parse correctly. The only runtime dependency is `@openai/codex` which
is installed at workflow start.

---

## Missing from Repo

| Item | Impact |
|------|--------|
| **Unit tests** | No test suite for Python scripts or schema validation |
| **CI pipeline** | No GitHub Actions workflow to lint/validate this repo itself |
| **`requirements.txt` / `pyproject.toml`** | Not needed (stdlib only) but absent for documentation |
| **`.yamllint.yml`** | No YAML linting configuration |
| **CHANGELOG** | No changelog tracking releases |

---

## Recommendations (Priority Order)

1. **Fix `memory_maintenance.yml`** — Use the CLI instead of inline Python to
   get branch-safe persistence (critical bug)
2. **Fix ghost reference in `review_autofix.yml`** — Update the workflow
   snapshot path
3. **Wire in `setup-runtime` action or remove it** — Eliminate the
   defined-but-unused action
4. **Update stale references in `ai_pipeline.md`** — Replace old workflow names
5. **Add a basic CI workflow** — YAML lint + Python syntax check on PRs to this
   repo
6. **Add a CHANGELOG.md** — Track releases per `docs/release-policy.md`
