# Squad Workflow Improvements Report

> **Note**: This report was generated before the workflow refactoring. Old
> workflow names (`ai-clarify.yml`, `ai-plan.yml`, `ai-implement.yml`,
> `ai-auto-review-and-edit.yml`, `ai-issue-pr-status.yml`) have been updated to
> their current names (`clarify.yml`, `plan.yml`, `implement.yml`,
> `review_autofix.yml`, `issue_pr_status.yml`).

Generated at: 2026-03-22T04:24:36Z

Analyzed external repository:
- Repo: `https://github.com/bradygaster/squad`
- Commit: `1446050f43471b111aa6210eb9825449651f2b64`
- Commit date: `2026-03-20 03:06:58 -0700`

## Scope and Methodology

Scope was restricted to workflow automation assets only.

Local assets reviewed:
- `.github/workflows/*`
- `scripts/*`
- `prompts/*`
- `ai_pipeline.md`
- `codex_system_instructions.md`

External assets reviewed:
- `/tmp/squad_repo/.github/workflows/*`
- `/tmp/squad_repo/templates/workflows/*`
- `/tmp/squad_repo/.squad/templates/workflows/*`

Method:
- Mapped workflow patterns in `squad` to this repository's AI issue pipeline.
- Classified each pattern as `adopt`, `adapt`, or `reject`.
- Prioritized only improvements that reduce operational risk or maintenance overhead in this repository.

## Current-State Baseline (This Repository)

Strengths already in place:
- End-to-end 3-phase issue pipeline with command gating and phase labels: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`.
- PR autofix and review hardening pipeline with runtime context capture: `.github/workflows/review_autofix.yml`.
- Shared prompt fragments exist for phase modes: `prompts/header.txt`, `prompts/mode-clarify.txt`, `prompts/mode-plan.txt`, `prompts/mode-implement.txt`.
- AI memory infrastructure exists for persistence and retrieval: `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`.

Gaps observed:
- AI label lifecycle is implemented ad hoc across workflows; there is no single label contract registry or automated label sync/enforcement workflow.
- Prompt logic is duplicated: workflows use large inline prompt heredocs while `prompts/*` also stores similar instructions.
- Idempotency checks are uneven across phases (for example, stale `/answer` protection in plan exists, but command processing is not tracked via a shared processed-comment ledger across all phases).
- Manual operator replay path is limited to comment commands; no dedicated `workflow_dispatch` control plane for rerunning a specific phase on demand.
- Shared workflow logic blocks are repeated across files with no template-generation discipline.

## Imported Pattern Matrix (Squad -> This Repo)

| Pattern from `squad` | Source evidence | Local analog | Disposition | Why |
| --- | --- | --- | --- | --- |
| Label catalog sync from team metadata | `sync-squad-labels.yml` | AI phase labels (`ai:*`) manipulated in multiple workflows | `adapt` | This repo should centralize AI labels to avoid missing/invalid labels during phase transitions. |
| Namespace mutual exclusivity enforcement | `squad-label-enforce.yml` | Manual per-workflow label add/remove | `adapt` | A generic phase-state enforcer reduces stuck/inconsistent issue state. |
| Scheduled orphan-state repair heartbeat | `squad-heartbeat.yml` | No periodic repair pass for inconsistent AI labels | `adapt` | Helps recover from partial failures and interrupted runs. |
| Manual rerun workflow with explicit input and status | `ci-rerun.yml` | No dedicated phase replay workflow | `adapt` | Improves operator recovery without posting synthetic issue comments. |
| Template-first workflow maintenance | `templates/workflows/*` + sync notes in active workflows | AI workflows maintained directly | `adapt` | Reduces cross-workflow drift and duplicated shell logic. |
| Branch promotion + release lane automation | `squad-promote.yml`, `squad-release.yml` | Not part of issue -> plan -> implement path | `reject` | Valuable for package release pipelines, but not directly relevant to current AI issue automation scope. |

## Prioritized Recommendation Shortlist

### 1) priority: P1
- source_pattern: Label sync + mutual-exclusivity enforcement (`sync-squad-labels.yml`, `squad-label-enforce.yml`, `squad-heartbeat.yml`)
- source_refs:
  - squad: `.github/workflows/sync-squad-labels.yml`, `.github/workflows/squad-label-enforce.yml`, `.github/workflows/squad-heartbeat.yml`
  - local: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/issue_pr_status.yml`, `.github/workflows/review_autofix.yml`
- current_state: AI labels are transitioned in multiple workflows independently (`clarify.yml`, `plan.yml`, `implement.yml`, `issue_pr_status.yml`, `review_autofix.yml`).
- proposed_adaptation: Introduce a single AI label contract (authoritative list + allowed transitions) and enforce it centrally (sync + exclusivity + orphan repair).
- expected_impact: Lower rate of invalid/stuck issue phase states; simpler incident triage.
- effort: Medium
- risk: Low
- dependencies: Decide canonical transition graph for `ai:*` states and ownership for label policy updates.
- Partial implementation only: added `.github/ai/label_contract.v1.json` and introduced `scripts/ai_labels.py`; workflow-level integration and maintenance workflows are still pending.

### 2) priority: P2
- source_pattern: Idempotent workflow behavior with explicit rerun-safe logic (seen across label-driven `squad` workflows)
- source_refs:
  - squad: `.github/workflows/squad-triage.yml`, `.github/workflows/squad-issue-assign.yml`, `.github/workflows/ci-rerun.yml`
  - local: `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`
- current_state: Plan workflow protects against stale `/answer`, but command processing is not tracked through a shared processed-comment ledger across all phases.
- proposed_adaptation: Track processed issue comment IDs for `/reclarify`, `/answer`, and `/approved` in shared memory (reuse `scripts/ai_memory.py` capabilities) and guard every phase on that ledger.
- expected_impact: Prevents duplicate phase execution and accidental re-processing after retries/race conditions.
- effort: Medium
- risk: Low
- dependencies: Extend memory schema/CLI contract for processed-command entries and retention policy.
- Partial implementation only: added `processed_command_entry.v1` schema/example, implemented processed-command check/claim/complete CLI in `scripts/ai_memory.py`, and added ledger helpers in `scripts/ai_memory_lib.py`; workflow-level idempotency wiring is still pending.

### 3) priority: P3
- source_pattern: Template/source-of-truth discipline in `squad` (`templates/workflows/*` mirrored into active workflows)
- source_refs:
  - squad: `templates/workflows/*.yml`, `.github/workflows/*.yml`, `.squad/templates/workflows/*.yml`
  - local: `prompts/header.txt`, `prompts/mode-clarify.txt`, `prompts/mode-plan.txt`, `prompts/mode-implement.txt`, `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`
- current_state: Prompt text exists both inline inside workflow heredocs and separately under `prompts/*`.
- proposed_adaptation: Use `prompts/header.txt` + `mode-*.txt` as the only prompt source; workflows should assemble prompt text from files at runtime and remove inline duplicates.
- expected_impact: Eliminates prompt drift, simplifies prompt reviews, and reduces workflow churn.
- effort: Low
- risk: Low
- dependencies: Small refactor of phase workflows to build prompt input from prompt files.

### 4) priority: P4
- source_pattern: Manual replay entrypoint (`ci-rerun.yml` with `workflow_dispatch` inputs)
- source_refs:
  - squad: `.github/workflows/ci-rerun.yml`
  - local: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`
- current_state: Recovery relies on posting issue comments to re-trigger phases.
- proposed_adaptation: Add a controlled manual replay workflow (`workflow_dispatch`) that accepts `issue_number`, `phase`, and optional `comment_id`, then runs existing context-build + guard logic.
- expected_impact: Faster, safer operator recovery for transient workflow failures.
- effort: Medium
- risk: Medium
- dependencies: Permission model and duplicate-PR guard alignment with current implementation workflow.

### 5) priority: P5
- source_pattern: Generated/install-synced workflow copies (`templates/workflows/*`, `.squad/templates/workflows/*`, `.github/workflows/*`)
- source_refs:
  - squad: `templates/workflows/*.yml`, `.squad/templates/workflows/*.yml`, `.github/workflows/*.yml`
  - local: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`
- current_state: AI workflows repeat shared shell blocks (Codex config creation, Telegram delivery patterns, common guards) with manual copy/update.
- proposed_adaptation: Establish workflow snippet/template generation for shared AI pipeline blocks and add drift checks in CI.
- expected_impact: Higher consistency for security/guardrail fixes across all phases.
- effort: Medium-High
- risk: Medium
- dependencies: Agree on generation strategy (templating tool/script and ownership).

## Adoption Sequencing

Quick wins (low risk, high leverage):
1. P3 Prompt single-source refactor.
2. P1 AI label contract definition (initial sync + enforcement).
3. P2 Processed-comment idempotency ledger.

Follow-up items:
1. P4 Manual phase replay workflow.
2. P5 Workflow template-generation and drift checks.

## Short Summary Block for Issue Comment

- Compared this repo's AI automation assets against `bradygaster/squad` at commit `1446050f43471b111aa6210eb9825449651f2b64`.
- Best near-term imports are: central AI label contract/enforcement, shared command idempotency ledger, and prompt single-source cleanup.
- Highest risk reduction comes from preventing invalid issue phase states and duplicate command processing.
- Medium-term improvements are manual phase replay (`workflow_dispatch`) and template-driven workflow generation to reduce drift.
- Release-promotion patterns from `squad` were intentionally rejected for this issue because they are outside the current AI issue pipeline scope.
