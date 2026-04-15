# coding-workflows

Centralized reusable GitHub Actions workflows for AI-powered issue-to-PR automation.

## Overview

This repository contains reusable `workflow_call` workflows that power the full AI development pipeline:

1. **Clarify** — Determines if an issue has enough information, asks clarification questions if needed
2. **Plan** — Generates a structured implementation plan from clarified issues
3. **Implement** — Executes the approved plan and creates a pull request
4. **Review & Autofix** — Multi-model PR review with automated fix application
5. **Issue PR Status** — Syncs issue labels when PRs are merged/closed
6. **Cancel on PR Close** — Cancels orphaned workflow runs when PRs close
7. **Memory Maintenance** — Monthly compaction and archival of AI memory records
8. **Validate** — Runtime harness generation + local Docker smoke validation with machine-readable results
9. **Update Workflows** — Automatically updates existing and creates new workflow wrappers in consumer repos when upstream templates change

### Memory System

All active pipeline phases (clarify, plan, implement, review, orchestrate, validate) now integrate with the AI memory subsystem.  Workflows persist decisions, implementation plans, review findings, and validation results as candidate records to a dedicated `ai-memory` git branch.  Before constructing each LLM prompt, relevant prior context is retrieved from memory and injected between the static prompt prefix and the dynamic issue/PR content — preserving provider-side prompt-prefix caching while giving the model awareness of previous runs.

Key behaviors:

- **Run events** are recorded at the start and end of every phase (fail-open: a memory error never fails the workflow).
- **Candidate records** capture decisions, plans, code summaries, review findings, and validation outcomes.
- **Processed-command idempotency** (`/answer`, `/approved`) prevents duplicate plan or implement runs caused by rapid re-triggering.
- **Task lineage** tracks the full issue-to-PR lifecycle (open → in_progress → merged/closed) and is finalized when a PR closes or merges.
- **Kill switch:** set the `AI_MEMORY_ENABLED` repository variable to `false` to disable all memory operations without any other code change.

Memory operations are implemented in `scripts/memory_helpers.sh` (shared helper wrappers) and `scripts/ai_memory.py` (CLI). The `ai-memory` branch is created automatically on the first write.

## Quickstart

Get AI-powered issue-to-PR automation running in your repository in a few minutes.

### 1. Add secrets and variables

In your consumer repository, go to **Settings → Secrets and variables → Actions** and configure:

#### Secrets

| Secret | Required | Used By | Description |
|---|---|---|---|
| `GH_PAT` | **Yes** | All workflows | GitHub Personal Access Token with `repo` scope |
| `OPENROUTER_API_KEY` | **Yes** | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, memory_maintenance | [OpenRouter](https://openrouter.ai) API key for LLM access and AI memory keyword extraction |
| `TG_BOT_SECRET` | No | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, issue_pr_status | Telegram bot token for notifications and message cleanup |

#### Variables

| Variable | Required | Default | Used By | Description |
|---|---|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | No | `openai/gpt-5.3-codex` | clarify, plan, implement, review_autofix | Model for code editing tasks |
| `WORKFLOW_VALIDATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | validate | Model override for validation harness generation/diagnosis |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | No | `true` | plan | Auto-trigger implementation when plan is clear |
| `ALLOW_WORKFLOW_EDITS` | No | `true` | review_autofix, implement, update_workflows | Allow AI edits to `.github/workflows` files and automatic wrapper updates. Set to `false` to opt out of auto-updates. |
| `ENABLE_AUTO_MERGE` | No | `true` | review_autofix, orchestrate_poll | Auto-merge PRs (squash) when review passes. Requires "Allow auto-merge" in repo settings. |
| `MAX_AUTOFIX_ITERATIONS` | No | `3` | review_autofix | Maximum consecutive autofix rounds before the review loop stops and marks the PR `ai:review-blocked`. |
| `REVIEW_REASONING_SCHEDULE` | No | `xhigh,high,medium` | review_autofix | Reviewer-only cycle schedule for autofix rounds (cycle 1 uses first entry, cycle 2 second, cycle 3+ last). Accepted values: `xhigh`, `high`, `medium`, `low` (comma-separated). |
| `REVIEW_AUTODOWNGRADE_DISABLED` | No | `false` | review_autofix | Kill switch for reviewer cycle schedule. When `true`, reviewer reasoning stays fixed at `THINKING_LEVEL_REVIEWER`. |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | No | `true` | review_autofix | When true, non-orchestrator PRs that exhaust autofix iterations invoke a judge (LLM) to decide: merge as-is, push a fix commit, or close and reissue. Orchestrator-managed PRs are skipped (handled by the poller). PRs without linked issues use the PR title/body as requirement context. |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | No | `xhigh` | review_autofix | Reasoning effort for the review-blocked judge in non-orchestrator PRs (`xhigh`, `high`, `medium`, `low`). |
| `MAX_REVIEW_BLOCKED_RETRIES` | No | `2` | review_autofix, orchestrate_poll | Maximum judge retries for review-blocked PRs before forcing a final decision (merge or close+reissue). Used by both the review_autofix judge (counts `[judge-fix]` commits) and the orchestrator poller. |
| `ENABLE_VALIDATION` | No | `true` | orchestrate_poll | When true, a `complete` judge verdict transitions the tracking issue into runtime validation (`ai:validating`) and completion occurs only after validation passes. |
| `MAX_VALIDATE_CYCLES` | No | `3` | orchestrate_poll | Maximum runtime validation cycles (initial run + fix/revalidate loops) before forcing `ai:validation-failed`. |
| `VALIDATE_WORKFLOW_NAME` | No | `ai-validate.yml` | orchestrate_poll | Workflow filename to dispatch for runtime validation. Override to `internal-validate.yml` for repos using the internal naming convention. Falls back to `internal-validate.yml` automatically if the primary name fails. |
| `MAX_JUDGE_CYCLES` | No | `25` | orchestrate_poll | Maximum judge evaluation cycles per project before forcing failure. Prevents infinite fix-up loops when the judge repeatedly returns `in_progress`. |
| `STALL_THRESHOLD_MINUTES` | No | `120` | orchestrate_poll | Fallback minutes an issue can remain in the same pipeline phase before auto-recovery. Used when no per-phase override is set. |
| `STALL_THRESHOLD_NO_LABELS_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for issues with no AI pipeline labels (pre-pipeline). |
| `STALL_THRESHOLD_CLARIFICATION_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:clarification` phase. |
| `STALL_THRESHOLD_PLANNING_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:planning` phase. |
| `STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:awaiting-approval` phase. |
| `STALL_THRESHOLD_IMPLEMENTING_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:implementing` phase. |
| `STALL_THRESHOLD_DONE_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:done` phase (review/autofix). |
| `STALL_THRESHOLD_READY_TO_MERGE_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:ready-to-merge` phase. |
| `MAX_STALL_RECOVERIES_PER_ISSUE` | No | `5` | orchestrate_poll | Maximum stall recovery attempts per individual issue. Declarative recovery uses `STALL_RECOVERY_ACTIONS` by `stall_recovery_count` (index clamped to the last action; current ladders are phase action, phase action, then `escalate_human`). When `run_stall_judge` is active, judge failures/invalid output/unsupported actions fail-open to that same declarative action. At this limit, the next action becomes `skip` (`ai:closed`) so the wave can advance; the judge evaluates the gap at wave completion. |
| `STALL_JUDGE_TRIGGER_COUNT` | No | `2` | orchestrate_poll | Stall recovery attempt threshold at which the poller overrides declarative ladder actions with `run_stall_judge` (when `ENABLE_STALL_JUDGE=true` and recovery count is still below `MAX_STALL_RECOVERIES_PER_ISSUE`). |
| `ENABLE_STALL_JUDGE` | No | `true` | orchestrate_poll | Enables/disables stall-judge escalation (`run_stall_judge`) in orchestrator-managed and standalone stall recovery paths. |
| `ENABLE_STALL_HUMAN_TERMINALIZATION` | No | `false` | orchestrate_poll | Legacy compatibility gate for terminal stall escalation policy. When `false` (default), `escalate_human` is suppressed and converted to the same non-human declarative fallback action for the issue phase/recovery count; when `true`, human terminalization is allowed. |
| `ENABLE_STANDALONE_STALL_RECOVERY` | No | `true` | orchestrate_poll | Enable stall detection and auto-recovery for standalone AI issues (issues not managed by an active orchestrator tracking state). |
| `ENABLE_CLOSE_MERGED_ISSUES` | No | `true` | orchestrate_poll | Enable the per-cycle sweep that closes any open GitHub issue carrying `ai:merged` once at least one cross-referenced PR is verified merged via the GitHub REST API. Applies to both orchestrator-managed child issues and standalone (non-orchestrator) issues. Tracking issues (`ai:orchestrator-tracking`) are intentionally skipped — they are closed by the orchestrator project completion path. If an issue has `ai:merged` but no merged PR can be verified on its timeline, the sweep leaves it open and sends a Telegram `WARNING` alert instead of guessing. |
| `MAX_RECOVERY_ATTEMPTS` | No | `3` | orchestrate_poll | Maximum project-level recovery cycles when the judge declares failure. Replaces the previous single-shot `recovery_attempted` boolean with a configurable counter. |
| `MAX_VALIDATION_RECOVERY_ATTEMPTS` | No | `2` | orchestrate_poll | Maximum times the poller transitions a validation-failed project back to the judge for re-evaluation before marking it as terminally failed. Set to `0` to disable (immediate terminal failure on first validation failure, matching pre-recovery behavior). |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | No | `900` | orchestrate_poll | Minimum seconds between consecutive review/autofix dispatches against the same orchestrator integration-branch final PR. Prevents the self-healing loop from re-dispatching the resolver every poll tick while a previous run is still in flight. |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | No | `3` | orchestrate_poll | Circuit-breaker budget for automated integration-branch conflict resolution. The self-healing path attempts the `main -> integration_branch` sync via GitHub's merges API; on an HTTP 409 conflict, the poller dispatches `_dispatch_review_for_conflicts` for the final integration PR. After this many consecutive unresolved ticks, the orchestrator escalates to the judge with full PR context; if the judge escalation itself fails the project is marked terminally failed. |
| `REVIEW_BLOCKED_AUTO_UNSTICK` | No | `true` | orchestrate_poll | Before invoking the review-blocked judge, the poller inspects each `ai:review-blocked` PR. If the PR is `mergeable=false` it dispatches `review_autofix.yml` (via `_dispatch_review_for_conflicts`) so the in-workflow Codex resolver gets a fresh shot at the conflict, and skips the judge for this tick. If the PR head commit was authored by an **external** identity (anything other than `codex`, `codex-bot`, `github-actions`, or `github-actions[bot]`), the poller also dispatches the review workflow AND clears `ai:review-blocked`, re-entering the normal phase loop — this bridges the GitHub platform rule that suppresses `pull_request.synchronize` events on commits pushed with the default `GITHUB_TOKEN` (Claude Code on the web, custom wrapper actions) and matches the "push a new commit to re-trigger the review workflow" contract printed in the workflow-failure comment. Set to `false` to disable both paths and force the judge-first flow. Dispatch is always gated by the existing `_dispatch_review_for_conflicts` cycle-local dedup and active-run detection, so repeat calls are cheap no-ops. |
| `TG_ADMIN_CHAT_ID` | No | — | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Telegram chat ID for notifications (pair with `TG_BOT_SECRET`) |
| `ALERT_MSG_LEVEL` | No | `DEBUG` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, issue_pr_status, update_workflows, test-and-mark-stable | Minimum Telegram alert level to send. Alerts below this threshold are suppressed. Valid values: `DEBUG`, `WARNING`, `ERROR`, `CRITICAL`. Each alert is prefixed with an icon and level (e.g. `🔍 DEBUG:`, `⚠️ WARNING:`, `❌ ERROR:`, `🚨 CRITICAL:`). New alerts default to `CRITICAL` until explicitly recategorised. |
| `SERENA_VERSION` | No | `main` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Version/branch of the Serena MCP server |
| `SERENA_LANGUAGES` | No | `""` (empty) | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Languages for Serena symbol analysis |
| `SERENA_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Disable the Serena MCP server |
| `CONTEXT7_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate | Disable the optional Context7 MCP server |
| `GIT_MCP_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate | Disable the optional Git MCP server setup (preloaded diff artifacts remain the fallback). |
| `OPENROUTER_PROMPT_CACHE_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, workflow-log-analysis | Kill switch for OpenRouter prompt-cache instrumentation. `false` enables cache-friendly prompt ordering and cache telemetry logging; `true` disables explicit cache breakpoints and related instrumentation. |
| `WORKFLOW_ORCHESTRATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | orchestrate, orchestrate_poll | Model override for orchestrator decomposer and judge |
| `ORCHESTRATE_POLL_INTERVAL` | No | `5` | orchestrate | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | No | `ai-orchestrate-poll.yml` | orchestrate_poll | Filename of the caller wrapper workflow to retrigger for continuous polling. The poller dispatches this workflow via `workflow_dispatch` at the end of each run when active tracking issues exist, so the next cycle starts immediately instead of waiting for the cron schedule. Set to empty string to disable self-retrigger. |
| `EDITOR_IDLE_TIMEOUT` | No | `1200` | review_autofix, implement | Editor watchdog idle timeout in seconds. The editor is killed if it produces no output for this long and has no active network connections. |
| `EDITOR_MAX_WALL` | No | `3300` | review_autofix, implement | Maximum wall-clock seconds per editor attempt. Budget-aware: auto-capped to remaining job time minus a 2-min buffer. |
| `EDITOR_MIN_ATTEMPT_SECS` | No | `300` | review_autofix | Minimum remaining job budget (seconds) required to start an editor attempt. Prevents futile retries near the job deadline. |
| `BULK_DELETE_THRESHOLD` | No | `3` | implement | Maximum number of file deletions allowed in a single AI implementation commit before the destructive-commit guard blocks it. Set higher for legitimate large refactors, or bypass on a per-run basis via `ALLOW_BULK_DELETE=true`. See "Destructive-commit guard" below. |
| `ALLOW_BULK_DELETE` | No | `false` | implement | When `true`, the destructive-commit guard ignores the `BULK_DELETE_THRESHOLD` rejection path. Canonical workflow-source file deletions are still blocked unless `ALLOW_WORKFLOW_EDITS=true`. Use for legitimate large refactors approved by a human. |
| `BATCH_API_DISABLED` | No | `false` | workflow-log-analysis, memory_maintenance | Kill switch for async batch mode. When `true`, workflow log analysis always uses synchronous inference. Memory maintenance emits compatibility/no-op batch logs only. |
| `BATCH_API_PROVIDER` | No | `auto` | workflow-log-analysis, memory_maintenance | Batch provider routing hint for OpenRouter Responses API capability checks/submission (`auto`, `openai`, `anthropic`). Unsupported hints fall back to sync with structured warnings. |
| `BATCH_API_POLL_TIMEOUT_HOURS` | No | `24` | workflow-log-analysis, memory_maintenance | Maximum pending batch age before workflow-log-analysis falls back to synchronous generation. |

**Thinking levels** — control the model's reasoning effort per phase. Valid values: `xhigh`, `high`, `medium`, `low`. Defaults are tuned per phase: `medium` for clarify (gap analysis doesn't need deep reasoning), `xhigh` for plan (architectural decisions benefit from maximum reasoning), `high` for implement (follows an existing plan), and `xhigh` for review (last line of defense for catching bugs). Judge runs use adaptive effort: cycles 1-3 keep `xhigh`, and cycles 4+ automatically downgrade to `high` to reduce cost on incremental rechecks. In `review_autofix`, reviewer effort also auto-schedules by autofix cycle via `REVIEW_REASONING_SCHEDULE` (default: cycle 1 `xhigh`, cycle 2 `high`, cycle 3+ `medium`) unless `REVIEW_AUTODOWNGRADE_DISABLED=true`. **E2E smoke test override has highest precedence:** when an issue title contains `[E2E Smoke Test]`, review/edit still force `low` reasoning regardless of schedule/kill-switch so release smoke runs stay cheap and fast.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `THINKING_LEVEL_CLARIFY` | `medium` | clarify | Reasoning effort for the clarification phase |
| `THINKING_LEVEL_PLAN` | `xhigh` | plan | Reasoning effort for the planning phase |
| `THINKING_LEVEL_IMPLEMENT` | `high` | implement | Reasoning effort for the implementation phase |
| `THINKING_LEVEL_ANALYSIS` | `medium` | workflow-log-analysis | Reasoning effort for the workflow log analysis report generation. |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | review_autofix | Reasoning effort for the reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `high` | review_autofix | Reasoning effort for the editor model (applying fixes) |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `xhigh` | review_autofix | Reasoning effort for the review-blocked judge (non-orchestrator PRs) |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | orchestrate | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | orchestrate_poll | Reasoning effort for judge evaluation (`xhigh` for cycles 1-3, automatically `high` from cycle 4 onward) |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `low` | orchestrate_clarify_respond | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `high` | validate | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `medium` | orchestrate_poll | Reasoning effort for the orchestrator's Codex-based merge conflict resolver |
**Tool call budgets** — soft limits on the number of MCP + shell tool calls per phase. The LLM treats these as guidelines; it may exceed them for large refactors that span many files.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | clarify | Tool call budget for the clarification phase |
| `TOOL_CALL_BUDGET_PLAN` | `40` | plan | Tool call budget for the planning phase |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | implement | Tool call budget for the implementation phase |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | orchestrate | Tool call budget for the decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | orchestrate_poll | Tool call budget for the judge (needs deep repo inspection) |
| `TOOL_CALL_BUDGET_CLARIFY_RESPOND` | `15` | orchestrate_clarify_respond | Tool call budget for auto-answering clarification questions |
| `TOOL_CALL_BUDGET_VALIDATE` | `60` | validate | Tool call budget for runtime validation harness generation and diagnosis |

**Token warning thresholds** — when a phase exceeds this many tokens, a warning appears in the GitHub Actions run summary. Raise these for large repos where deeper exploration is expected.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | clarify | Token usage warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | plan | Token usage warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | implement | Token usage warning threshold for implementation |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | orchestrate | Token usage warning threshold for orchestration |
| `TOKEN_WARN_THRESHOLD_CLARIFY_RESPOND` | `80000` | orchestrate_clarify_respond | Token usage warning threshold for auto-answering clarification questions |

**Serena adoption warning thresholds** — when Serena efficiency falls below the threshold (and at least 5 total code operations are detected), workflows emit a non-blocking `::warning::` alert. `review_autofix` automatically forces the threshold to `0` when the PR closed/merged mid-run, because in that case reviewers are short-circuited before doing meaningful semantic work and the adoption counters are not representative.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `SERENA_WARN_THRESHOLD_IMPLEMENT` | `50` | implement | Minimum Serena efficiency (%) before emitting low-adoption warning |
| `SERENA_WARN_THRESHOLD_REVIEW` | `50` | review_autofix | Minimum Serena efficiency (%) before emitting low-adoption warning |

### 2. Create wrapper workflows

Copy the ready-to-use templates from [`workflow-templates/`](workflow-templates/) into your repo's `.github/workflows/` directory. Reference implementations also live in [`.github/workflows/internal-*.yml`](.github/workflows/) in this repository.

At minimum, create these three core wrappers:

**`.github/workflows/ai-clarify.yml`** — Triages new issues automatically
```yaml
name: AI Clarify
on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  clarify:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-plan.yml`** — Generates an implementation plan when you comment `/answer`
```yaml
name: AI Plan
on:
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  plan:
    uses: shubhodeep1/coding-workflows/.github/workflows/plan.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-implement.yml`** — Executes the plan and opens a PR when you comment `/approved`
```yaml
name: AI Implement
on:
  issue_comment:
    types: [created]
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  implement:
    uses: shubhodeep1/coding-workflows/.github/workflows/implement.yml@stable
    secrets: inherit
```

#### Optional wrappers

**`.github/workflows/ai-review.yml`** — Multi-model PR review with automated fixes
```yaml
name: AI Review
on:
  pull_request:
    types: [opened, synchronize, reopened]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number to review (used by autofix re-trigger)"
        required: true
        type: string
      allow_workflow_edits:
        description: "Allow AI/editor changes to .github/workflows files"
        required: false
        default: false
        type: boolean
permissions:
  contents: write
  pull-requests: write
  issues: write
jobs:
  review:
    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable
    with:
      pr_number: ${{ github.event.inputs.pr_number || '' }}
      pr_is_draft: >-
        ${{ github.event_name != 'workflow_dispatch' && github.event.pull_request.draft || false }}
      pr_title: >-
        ${{ github.event_name != 'workflow_dispatch' && format('{0}', github.event.pull_request.title) || '' }}
      pr_body: >-
        ${{ github.event_name != 'workflow_dispatch' && format('{0}', github.event.pull_request.body) || '' }}
      # Optional fast-path skip signal. Keep false if your wrapper does not precompute it.
      pr_skip_ai: false
      allow_workflow_edits: ${{ (github.event_name == 'workflow_dispatch' && github.event.inputs.allow_workflow_edits == 'true') || (github.event_name != 'workflow_dispatch' && vars.ALLOW_WORKFLOW_EDITS != 'false') }}
    secrets: inherit
```

> **Merge-ref fallback re-trigger** — After pushing autofix or
> conflict-resolution commits, GitHub fires a `pull_request` `synchronize`
> event. However, GitHub resolves reusable workflow refs from the merge ref
> (`refs/pull/N/merge`), which can be unbuildable when the base branch has
> advanced and introduced new conflicts. When this happens the review
> workflow is silently skipped. The `workflow_dispatch` trigger and
> `pr_number` input above enable a fallback: the reusable workflow
> dispatches the caller workflow explicitly after pushing. The concurrency
> group deduplicates when both the `synchronize` event and the dispatch
> fire successfully. Because this fallback uses `gh workflow run` and the
> Actions workflow-dispatch API, `GH_PAT` must be allowed to dispatch
> workflows (classic PAT: include `workflow` scope with `repo`; fine-grained
> PAT: grant Actions read/write permission).

> The reusable workflow handles autofix iteration counting internally. It
> counts consecutive `[ai-autofix]` commits and stops after
> `MAX_AUTOFIX_ITERATIONS` (default `3`). When `ENABLE_REVIEW_BLOCKED_JUDGE`
> is `true` (the default), a judge LLM evaluates the PR and decides to:
> merge as-is, push a `[judge-fix]` commit (re-triggers review with reset
> counter), or close the PR and create a replacement issue. The judge
> respects `MAX_REVIEW_BLOCKED_RETRIES` (default `2`) by counting
> `[judge-fix]` commits in the branch history. Orchestrator-managed PRs
> are skipped (handled by the orchestrate_poll workflow instead). If the
> judge is disabled or fails, the PR is labeled `ai:review-blocked` and
> requires human intervention. When review passes with no fixes needed,
> it labels linked issues `ai:ready-to-merge` and enables auto-merge if
> configured.

> **Warning — do NOT add a top-level `concurrency` block to this wrapper.**
> The reusable workflow already manages concurrency at the job level. Adding a
> workflow-level `concurrency` group with the same key (e.g.
> `pr-autofix-${{ github.event.pull_request.number }}`) causes a deadlock:
> the caller holds the lock while the called job waits for it, and GitHub
> Actions cancels the run. If you need to customize the concurrency group,
> do so only inside the reusable workflow, not in the caller.

> **Bootstrap fail-fast + resolver hallucination guard** — The
> `review_autofix.yml` script-bootstrap loop classifies helpers as
> `REQUIRED_BOOTSTRAP_SCRIPTS` (missing from both `${script_ref}` and
> `main` is a hard error with an actionable `::error::` message) vs
> `OPTIONAL_BOOTSTRAP_SCRIPTS` (missing emits a `::warning::` and
> continues). Keep the optional list empty unless a genuinely optional
> helper is added — this catches stale references introduced by
> hallucinated `[ai-merge-resolve]` commits before they can cascade
> into "unbound variable" errors in later cleanup steps. As a second
> layer, the `Resolve merge conflicts with Codex` step captures the
> set of unmerged paths from the merge replay into
> `RESOLVER_ALLOWLIST_FILE` and — after Codex exec returns — rejects
> the commit with a hard `::error::` if any `.github/workflows/*.y(a)ml`
> file was touched outside that allowlist. This allowlist-enforcement
> path currently runs on the workflow source repository path
> (`IS_WORKFLOW_SOURCE_REPO=true`). Non-workflow out-of-allowlist edits
> emit a warning only. Both guards are automatic and have no
> configuration surface.

**`.github/workflows/ai-issue-pr-status.yml`** — Syncs issue labels when PRs are merged/closed
```yaml
name: AI Issue PR Status
on:
  pull_request:
    types: [closed]
permissions:
  issues: write
jobs:
  status:
    uses: shubhodeep1/coding-workflows/.github/workflows/issue_pr_status.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-cancel-on-pr-close.yml`** — Cancels orphaned workflow runs when PRs close
```yaml
name: AI Cancel on PR Close
on:
  pull_request:
    types: [closed]
permissions:
  actions: write
jobs:
  cancel:
    uses: shubhodeep1/coding-workflows/.github/workflows/cancel_on_pr_close.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-memory-maintenance.yml`** — Monthly compaction and archival of AI memory
```yaml
name: AI Memory Maintenance
on:
  schedule:
    - cron: '0 3 1 * *'
permissions:
  contents: write
jobs:
  maintenance:
    uses: shubhodeep1/coding-workflows/.github/workflows/memory_maintenance.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-orchestrate.yml`** — Decomposes a project description into issues with a dependency DAG
```yaml
name: AI Orchestrate
on:
  workflow_dispatch:
    inputs:
      project_description:
        description: >
          Full project description. The orchestrator will decompose it into
          issues with a dependency DAG and dispatch them through the AI pipeline.
        required: true
        type: string
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  orchestrate:
    uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate.yml@stable
    with:
      project_description: ${{ inputs.project_description }}
    secrets: inherit
```

**`.github/workflows/ai-orchestrate-clarify-respond.yml`** — Auto-answers clarification questions on orchestrator-managed issues
```yaml
name: AI Orchestrate Clarify Respond
on:
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  respond:
    uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate_clarify_respond.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-orchestrate-poll.yml`** — Polls orchestrator progress, runs judge, dispatches next waves
```yaml
name: AI Orchestrate Poller
on:
  schedule:
    - cron: '*/5 * * * *'
permissions:
  contents: write
  issues: write
  pull-requests: write
  actions: write
jobs:
  poll:
    uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate_poll.yml@stable
    secrets: inherit
```

> **Standalone PR conflict sweep** — After processing orchestrator-managed
> tracking issues, the poller scans all eligible open PRs for merge conflicts
> (`mergeable_state=dirty`). When a conflict is detected it attempts a GitHub API branch
> update; if that fails (real conflicts), the poller dispatches the review
> workflow via `workflow_dispatch` so its built-in Codex conflict resolver can
> handle resolution on a dedicated runner with a clean environment. This
> ensures standalone (non-orchestrator) PRs are not permanently blocked by
> base-branch drift conflicts.

**`.github/workflows/ai-validate.yml`** — Runs runtime validation (generate harness -> execute -> structured artifacts)
```yaml
name: AI Validate
on:
  workflow_dispatch:
    inputs:
      tracking_issue:
        description: Tracking issue number
        required: false
        type: string
        default: "0"
      compose_file:
        description: Compose file path fallback
        required: false
        type: string
        default: "docker-compose.yml"
      validation_timeout:
        description: Validation idle timeout in minutes (no output = killed)
        required: false
        type: string
        default: "15"
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  validate:
    uses: shubhodeep1/coding-workflows/.github/workflows/validate.yml@stable
    with:
      tracking_issue: ${{ inputs.tracking_issue || '0' }}
      compose_file: ${{ inputs.compose_file || 'docker-compose.yml' }}
      validation_timeout: ${{ inputs.validation_timeout || '15' }}
    secrets: inherit
```

**`.github/workflows/ai-update-workflows.yml`** — Automatically updates workflow wrappers when upstream templates change
```yaml
# This workflow automatically updates AI workflow wrappers in this repo
# when new versions are published to coding-workflows@stable.
#
# Opting out:
#   Set the ALLOW_WORKFLOW_EDITS repository variable to 'false' to prevent
#   automatic updates. The workflow will still run but skip all changes.
#
# IMPORTANT: This file is managed by coding-workflows and will be overwritten
# by the update process. Do not add custom logic here.
name: AI Update Workflows
on:
  schedule:
    - cron: '0 4 * * *'
  repository_dispatch:
    types: [coding-workflows-stable-released]
  workflow_dispatch: {}
permissions:
  contents: write
jobs:
  update:
    uses: shubhodeep1/coding-workflows/.github/workflows/update_workflows.yml@stable
    with:
      allow_workflow_edits: ${{ vars.ALLOW_WORKFLOW_EDITS != 'false' }}
    secrets: inherit
```

> **How auto-updates work:** The update workflow runs daily and also triggers
> immediately when a new `@stable` release is tagged (via `repository_dispatch`
> from this repo). It fetches the latest templates from
> `coding-workflows@stable`, compares them against your local wrappers, and
> overwrites any that have changed. **New upstream templates are also created
> automatically** — you no longer need to manually copy new workflow files.
> The only exception is `ai-update-workflows.yml` itself, which must be
> bootstrapped manually (it's the workflow that runs this process). A Telegram
> alert lists which files were updated or created. To opt out, set
> `ALLOW_WORKFLOW_EDITS` to `false`. If you have customized a wrapper and want
> to keep your changes, either opt out or maintain your customizations after
> each update.

> All internal wrapper reference implementations can be found in [`.github/workflows/internal-*.yml`](.github/workflows/).

### 3. Open an issue

Create a new issue describing a feature or bug fix. The pipeline kicks off automatically:

1. **Clarify** evaluates whether the issue has enough detail. If not, it comments with clarification questions.
2. Once the issue is clear, comment `/answer` to trigger **Plan** generation.
3. Review the plan, then comment `/approved` to start **Implementation** — a PR is created for you.

## Usage

Consumer repositories use thin wrapper workflows that call these reusable workflows:

```yaml
# .github/workflows/ai-clarify.yml
name: AI Clarify

on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]

permissions:
  contents: read
  issues: write

jobs:
  clarify:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
    secrets: inherit
```

See [`workflow-templates/`](workflow-templates/) in this repository for ready-to-copy caller wrappers.

## Reusable Workflows

| Workflow | Trigger (in consumer) | Description |
|---|---|---|
| `clarify.yml` | `issues.opened`, `issue_comment.created` | Issue clarity detection |
| `plan.yml` | `issue_comment.created` (`/answer`) | Implementation plan generation |
| `implement.yml` | `issue_comment.created` (`/approved`) | Plan execution + PR creation |
| `review_autofix.yml` | `pull_request.*` | Multi-model review + autofix |
| `validate.yml` | `workflow_dispatch` or explicit call from orchestrator/poller | Runtime validation harness generation + Docker smoke execution |
| `issue_pr_status.yml` | `pull_request.closed` | Label/state sync + final lineage closure |
| `cancel_on_pr_close.yml` | `pull_request.closed` | Active-run cancellation |
| `memory_maintenance.yml` | `schedule` (monthly) | Memory compaction/archival |
| `orchestrate.yml` | `workflow_dispatch` | Project decomposition + multi-issue orchestration |
| `orchestrate_clarify_respond.yml` | `issue_comment.created` | Auto-answers clarification questions on orchestrator issues |
| `orchestrate_poll.yml` | `schedule` (every ~5 min) + self-retrigger | Orchestrator progress poller + judge + auto-recovery. Self-retriggers via `workflow_dispatch` when active tracking issues exist for near-immediate next cycles; cron acts as fallback. |
| `update_workflows.yml` | `schedule` (daily), `repository_dispatch`, `workflow_dispatch` | Auto-updates existing and creates new workflow wrappers from upstream templates |

## Workflow Log Analysis

This repository includes [`.github/workflows/workflow-log-analysis.yml`](.github/workflows/workflow-log-analysis.yml) to collect AI workflow telemetry and generate a markdown optimization report.

### How to run

Run **Actions -> Workflow Log Analysis -> Run workflow**, or let the built-in schedule fire automatically.

Triggers:

- `workflow_dispatch` (manual).
- `schedule`: `cron: "0 6 */2 * *"` — runs every other day (1, 3, 5, ...) at 06:00 UTC, approximating an every-48h cadence. Day-of-month `*/2` drifts at month boundaries (occasional 1-day or 2-day gap).

`workflow_dispatch` inputs:

| Input | Default | Description |
|---|---|---|
| `lookback_days` | `"7"` | Days of workflow runs to collect. Passed to `scripts/collect_workflow_logs.py --lookback-days`. On scheduled runs this falls back to `"2"` to match the 48h cadence; manual dispatch keeps the `"7"` default unless overridden. |
| `repos_override` | `""` | Optional comma-separated `owner/repo` list. Each item is validated with `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`; invalid values fail the run. |

Repository selection behavior (applies to both manual and scheduled runs):

1. If `repos_override` is set, only those repositories are used.
2. Otherwise, the workflow reads `.github/ai/consumer_repos.json` (if present) and also includes `${GITHUB_REPOSITORY}`.
3. Duplicates and empty entries are removed.

### Auth and configuration

- `GH_PAT` is preferred for GitHub API/push operations, with `github.token` fallback in workflow steps.
- `OPENROUTER_API_KEY` is required for `scripts/analyze_workflow_logs.py`.
- Telegram notification is optional. If either `TG_BOT_SECRET` or `TG_ADMIN_CHAT_ID` is missing, notification is skipped.

### Collector input/output contract

Collector script: [`scripts/collect_workflow_logs.py`](scripts/collect_workflow_logs.py)

- Primary CLI used by the workflow: `--lookback-days <N> --output workflow_log_report.json --repo <owner/repo>...`
- Full CLI contract from `build_parser`:
  - `--repo` (repeatable)
  - window selector (exactly one): `--lookback-days` or `--since`
  - `--output` (default `workflow_log_report.json`)
  - `--per-page`, `--max-pages`, `--max-runs`, `--max-log-runs` (default `15`)
- Token handling in `main`: uses `GH_TOKEN` with `GITHUB_TOKEN` fallback.
- For notable runs (failed, retries > 0, and top 10 slowest per repository), the collector also downloads raw run logs from `repos/{repo}/actions/runs/{run_id}/logs`, extracts ZIP contents in memory, and stores truncated per-step excerpts.

Generated JSON report (`workflow_log_report.json`) includes:

- `schema_version`
- `generated_at`
- `scope` (`repositories`, `workflow_families`, `source`)
- `runs` (per-run metrics including `workflow_family`, `duration_seconds`, `retries`, `failure_point`, and optional `log_excerpts` as `{step_name, excerpt}` entries for notable runs)
- `summary` (`total_runs`, success/failure/cancelled/other counts, `avg_duration_seconds`, `p50_duration_seconds`, `p95_duration_seconds`)
- `errors` (includes `scope: "logs"` entries when run log download/extraction fails; collection continues)

### Analyzer input/output contract

Analyzer script: [`scripts/analyze_workflow_logs.py`](scripts/analyze_workflow_logs.py)

- Workflow invocation: `python3 scripts/analyze_workflow_logs.py --input workflow_log_report.json`
- `--max-output-tokens` default is `100000`. The workflow auto-caps this to `60000` when the resolved `WORKFLOW_EDITOR_MODEL` contains `gemini` (Gemini 3.1 Pro Preview's max output is 65536).
- Model resolution for this workflow only: the `Run workflow log analysis` step pins `WORKFLOW_EDITOR_MODEL` to `google/gemini-3.1-pro-preview` by default (pilot). Override via repo variable `WORKFLOW_LOG_ANALYSIS_MODEL` (e.g. `openai/gpt-5.3-codex`) to revert. This override is scoped to the analysis step env and does not affect the global `WORKFLOW_EDITOR_MODEL` used by `clarify`/`plan`/`implement`/`review_autofix`/`validate`/`orchestrate`.
- `load_input_data` accepts either:
  - `--input` with a collector report (`runs` list; `runs[].log_excerpts` are flattened into `deep_dive_logs` as `{name: <repo>/<run_id>/<step_name>, excerpt}`), a combined bundle object (`run_metrics`, `summary_stats`, optional `deep_dive_logs`), or a JSON array of run metrics
  - `--data-dir` containing `workflow_log_report.json` or `run_metrics.json` + `summary_stats.json` (optionally `run_logs/`)
- Output path behavior from `resolve_dated_output_path`:
  - default: `analysis/workflow-optimization-YYYY-MM-DD.md`
  - same-day collisions: `analysis/workflow-optimization-YYYY-MM-DD-2.md`, `-3.md`, etc.
- `main` prints the final report path on stdout and exits non-zero on API/write/input errors.
- Batch mode uses OpenRouter Responses API with deferred polling and state file support:
  - `--batch-mode` (`auto|submit|poll|sync`)
  - `--batch-state-file` path for persisted batch metadata
  - `--batch-provider` (`auto|openai|anthropic`) provider hint
  - `--batch-api-disabled` kill switch
  - `--batch-poll-timeout-hours` timeout before sync fallback
- Analyzer exits with code `3` when batch remains pending; workflow treats this as success and defers completion to future runs.

### Workflow outputs

- Artifact upload: `workflow-log-report` containing `workflow_log_report.json` (retention 7 days).
- Repository commit: generated markdown report is committed/pushed to `${{ github.ref_name }}`.
- No-op behavior: if the report file has no diff, commit/push is skipped (`No report changes to commit.`).
- Telegram summary: when configured, sends either a pending-batch message or a completion message with report URL and workflow run URL.
- Deferred artifact contract: pending batch metadata is uploaded as artifact `workflow-log-analysis-batch-state` containing `workflow_log_analysis_batch_state.json`; later runs fetch latest non-expired artifact and continue polling.
- Structured logs are emitted for batch decisions and lifecycle (`batch_submit`, `batch_poll`, `batch_complete`, `batch_fallback`).
- `memory_maintenance.yml` remains functionally unchanged (no LLM path in current repo) and now emits structured `batch_noop` compatibility logging with batch env values.
- Low-data windows are valid: the analyzer still writes a report when input data is sparse.

## Required Secrets

| Secret | Used By | Description |
|---|---|---|
| `GH_PAT` | All workflows | GitHub PAT with repo access |
| `OPENROUTER_API_KEY` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, memory_maintenance | OpenRouter API key for LLM access and AI memory keyword extraction |
| `TG_BOT_SECRET` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, issue_pr_status | Telegram bot token (optional; also used for message cleanup) |

## Required Variables

| Variable | Default | Description |
|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` | Model for code editing tasks |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `true` | Allow AI edits to workflow files and automatic wrapper updates |
| `ENABLE_AUTO_MERGE` | `true` | Auto-merge PRs (squash) when review passes and checks are green |
| `MAX_AUTOFIX_ITERATIONS` | `3` | Maximum consecutive autofix rounds before marking `ai:review-blocked` |
| `REVIEW_REASONING_SCHEDULE` | `xhigh,high,medium` | Reviewer autofix-cycle reasoning schedule (`cycle1,cycle2,cycle3+`) |
| `REVIEW_AUTODOWNGRADE_DISABLED` | `false` | Disable reviewer cycle schedule and keep fixed `THINKING_LEVEL_REVIEWER` |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | `true` | Enable review-blocked judge for non-orchestrator PRs |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `xhigh` | Reasoning effort for review-blocked judge |
| `MAX_REVIEW_BLOCKED_RETRIES` | `2` | Maximum judge retries for review-blocked PRs (both review_autofix and orchestrate_poll) |
| `ENABLE_VALIDATION` | `true` | Enable post-judge runtime validation gate in orchestrator poller |
| `MAX_VALIDATE_CYCLES` | `3` | Maximum runtime validation cycles before terminal validation failure |
| `STALL_THRESHOLD_MINUTES` | `120` | Fallback minutes before a stalled issue triggers auto-recovery |
| `STALL_THRESHOLD_NO_LABELS_MINUTES` | `60` | Stall threshold for pre-pipeline (no labels) phase |
| `STALL_THRESHOLD_CLARIFICATION_MINUTES` | `60` | Stall threshold for clarification phase |
| `STALL_THRESHOLD_PLANNING_MINUTES` | `60` | Stall threshold for planning phase |
| `STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES` | `60` | Stall threshold for plan approval phase |
| `STALL_THRESHOLD_IMPLEMENTING_MINUTES` | `120` | Stall threshold for implementation phase |
| `STALL_THRESHOLD_DONE_MINUTES` | `120` | Stall threshold for review/autofix phase |
| `STALL_THRESHOLD_READY_TO_MERGE_MINUTES` | `60` | Stall threshold for ready-to-merge phase |
| `MAX_STALL_RECOVERIES_PER_ISSUE` | `5` | Max stall recovery attempts per issue; declarative ladder is clamped by `stall_recovery_count` (current ladders end in `escalate_human`), judge path fail-opens to same declarative action, then next action is `skip` (`ai:closed`) |
| `STALL_JUDGE_TRIGGER_COUNT` | `2` | Recovery-attempt threshold to override declarative ladder action with `run_stall_judge` (when `ENABLE_STALL_JUDGE=true` and still below max recoveries) |
| `ENABLE_STALL_JUDGE` | `true` | Enable/disable stall-judge escalation in orchestrator and standalone stall recovery |
| `ENABLE_STALL_HUMAN_TERMINALIZATION` | `false` | Legacy compatibility gate for terminal stall escalation policy: allow (`true`) or suppress (`false`) `escalate_human` terminalization |
| `ENABLE_STANDALONE_STALL_RECOVERY` | `true` | Enable standalone AI issue stall recovery in the poller |
| `MAX_RECOVERY_ATTEMPTS` | `3` | Max project-level recovery cycles (judge failure → auto-fix) |
| `MAX_VALIDATION_RECOVERY_ATTEMPTS` | `2` | Max validation-failure → judge re-evaluation cycles before terminal failure |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | `900` | Min seconds between consecutive resolver dispatches against an integration-branch final PR |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | `3` | Max consecutive unresolved conflict ticks before judge escalation, after `_dispatch_review_for_conflicts` healing attempts |
| `CONTEXT7_DISABLED` | `false` | Disable the optional Context7 MCP server setup in workflows |
| `GIT_MCP_DISABLED` | `false` | Disable the optional Git MCP server setup in workflows (preloaded diff artifacts remain fallback) |
| `AI_MEMORY_BRANCH` | `ai-memory` | Branch used for persistent AI memory |
| `AI_MEMORY_ROOT` | `ai-memory` | Memory root path used by workflows |
| `AI_MEMORY_RETRIEVAL_PROFILES` | `ai-memory/config/retrieval_profiles.v1.json` | Retrieval role config |
| `AI_MEMORY_ENABLED` | `true` | Enable/disable memory operations |
| `AI_MEMORY_KEYWORD_MODEL` | `openai/gpt-5-mini` | Model for semantic keyword extraction during retrieval |
| `AI_MEMORY_KEYWORD_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL for keyword model |
| `AI_MEMORY_TOKEN_BUDGET_<ROLE>` | _(from profile)_ | Per-role token budget override (e.g. `AI_MEMORY_TOKEN_BUDGET_IMPLEMENTATION=3200`) |
| `THINKING_LEVEL_CLARIFY` | `medium` | Reasoning effort for clarification (`xhigh`, `high`, `medium`, `low`) |
| `THINKING_LEVEL_PLAN` | `xhigh` | Reasoning effort for planning |
| `THINKING_LEVEL_IMPLEMENT` | `high` | Reasoning effort for implementation |
| `THINKING_LEVEL_ANALYSIS` | `medium` | Reasoning effort for workflow log analysis report generation |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | Reasoning effort for reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `high` | Reasoning effort for editor model (applying fixes) |
| `REVIEW_REASONING_SCHEDULE` | `xhigh,high,medium` | Reviewer autofix-cycle schedule override (`xhigh|high|medium|low`, comma-separated) |
| `REVIEW_AUTODOWNGRADE_DISABLED` | `false` | Reviewer schedule kill switch (`true` keeps fixed reviewer effort) |
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | Tool call budget for clarification |
| `TOOL_CALL_BUDGET_PLAN` | `40` | Tool call budget for planning |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | Tool call budget for implementation |
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | Token warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | Token warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | Token warning threshold for implementation |
| `WORKFLOW_ORCHESTRATE_MODEL` | (falls back to `WORKFLOW_EDITOR_MODEL`) | Model override for orchestrator/judge |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | Reasoning effort for judge evaluation (`xhigh` for cycles 1-3, automatically `high` from cycle 4 onward) |
| `ORCHESTRATE_POLL_INTERVAL` | `5` | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | `ai-orchestrate-poll.yml` | Caller workflow filename for self-retrigger; empty string disables |
| `EDITOR_IDLE_TIMEOUT` | `1200` | Editor watchdog idle timeout (seconds); killed if no output and no active network connections |
| `EDITOR_MAX_WALL` | `3300` | Max wall-clock seconds per editor attempt; auto-capped to remaining job budget |
| `EDITOR_MIN_ATTEMPT_SECS` | `300` | Minimum job budget (seconds) required to start an editor attempt |
| `BATCH_API_DISABLED` | `false` | Kill switch for async batch mode in workflow-log-analysis (`true` forces sync fallback) |
| `BATCH_API_PROVIDER` | `auto` | Batch provider hint (`auto`, `openai`, `anthropic`) for OpenRouter responses routing checks |
| `BATCH_API_POLL_TIMEOUT_HOURS` | `24` | Maximum pending batch age before synchronous fallback |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | Tool call budget for decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | Tool call budget for judge (needs deep repo inspection) |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | Token warning threshold for orchestration |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `low` | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `high` | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `medium` | Reasoning effort for the orchestrator's Codex-based merge conflict resolver |
| `TOOL_CALL_BUDGET_CLARIFY_RESPOND` | `15` | Tool call budget for auto-answering clarification questions |
| `TOKEN_WARN_THRESHOLD_CLARIFY_RESPOND` | `80000` | Token warning threshold for auto-answering clarification questions |
| `SEMANTIC_CACHE_BACKEND` | `none` | Semantic cache backend selector for clarification workloads: `none`, `redis`, `sqlite-vec` |
| `SEMANTIC_CACHE_TTL_DAYS` | `14` | Cache TTL (days) for semantic cache entries |
| `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` | `0.92` | Minimum cosine similarity to treat a semantic cache lookup as a hit |
| `SEMANTIC_CACHE_SQLITE_PATH` | `/tmp/semantic_cache.sqlite3` | SQLite cache file path when `SEMANTIC_CACHE_BACKEND=sqlite-vec` |
| `SEMANTIC_CACHE_REDIS_URL` | _(empty)_ | Redis connection URL when `SEMANTIC_CACHE_BACKEND=redis` |
| `SEMANTIC_CACHE_REDIS_KEY_NAMESPACE` | _(empty)_ | Redis key namespace for cross-repo isolation; defaults to `GITHUB_REPOSITORY` (sanitized + stable hash suffix) on GitHub runners, else empty |
| `SEMANTIC_CACHE_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | OpenRouter embedding model used for semantic cache keys |
| `SEMANTIC_CACHE_EMBEDDING_BASE_URL` | `https://openrouter.ai/api/v1` | Base URL for embedding API requests |
| `SEMANTIC_CACHE_MAX_CANONICAL_CHARS` | `50000` | Maximum canonical input length for cache key generation (longer inputs skip cache lookup/store) |
| `SERENA_WARN_THRESHOLD_IMPLEMENT` | `50` | Minimum Serena efficiency (%) before implement emits low-adoption warning |
| `SERENA_WARN_THRESHOLD_REVIEW` | `50` | Minimum Serena efficiency (%) before review_autofix emits low-adoption warning |

## Semantic Cache (Clarification Only)

An embedding-based semantic cache is available only for high-repetition clarification workloads.

- Cached phases:
  - `clarify`
  - `orchestrate_clarify_respond`
- Explicitly not cached:
  - `implement`
  - `review_autofix`
  - `validate`
  - `plan`
  - `orchestrate`

Cache key input is a canonical text built from:

- issue body
- issue thread history (chronological comments)

Operational behavior:

- `SEMANTIC_CACHE_BACKEND=none` keeps full passthrough behavior (default).
- `SEMANTIC_CACHE_BACKEND=redis` requires the Python `redis` package on runner hosts (installed automatically in built-in clarify workflows).
- Redis cache keys are namespaced by `SEMANTIC_CACHE_REDIS_KEY_NAMESPACE` (defaults to `GITHUB_REPOSITORY` on GitHub runners, sanitized + stable hash suffix) to prevent cross-repo collisions from normalization conflicts.
- SQLite cache is persisted across workflow runs via GitHub Actions `actions/cache` (for `sqlite-vec` backend).
- Cache entries are embedding-model scoped; changing `SEMANTIC_CACHE_EMBEDDING_MODEL` isolates old entries automatically.
- Inputs exceeding `SEMANTIC_CACHE_MAX_CANONICAL_CHARS` are treated as cache misses and are not stored.
- Any cache-layer error is fail-open: the workflows log a warning and continue with the normal OpenRouter/Codex path.
- On cache hit, workflows emit structured audit fields in log output: `phase`, `similarity`, `cached_at`, `original_issue_id`.

## Prompt Caching (OpenRouter + Codex)

### Current behavior

- Prompt assembly is cache-friendly in all Codex-driven phases: static prefix first (`codex_system_instructions.md` + `agents.md` + `prompts/serena-efficiency-block.txt` + phase template), dynamic context second (memory context, issue/PR body, comments/diffs).
- Explicit OpenRouter `cache_control: { "type": "ephemeral" }` breakpoints are added only in direct OpenRouter HTTP callers (`scripts/ai_memory_lib.py`, `scripts/analyze_workflow_logs.py`) when cache instrumentation is enabled.
- Gemini-family model IDs skip explicit breakpoint insertion by design.
- Fail-open safety is enforced: when a provider rejects explicit cache metadata, direct callers retry once without cache metadata instead of failing the workflow.

### Kill switch

- `OPENROUTER_PROMPT_CACHE_DISABLED=false` (default): cache behavior and telemetry are enabled.
- `OPENROUTER_PROMPT_CACHE_DISABLED=true`: explicit breakpoint insertion is disabled and workflows continue with normal execution.

### Telemetry fields

- Structured OpenRouter usage logging now includes:
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
  - `prompt_tokens`, `completion_tokens`, `total_tokens`
  - `phase`, `model`, and cache instrumentation flags when available
- Usage parsing is normalized across provider response shapes, including both:
  - `usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens`
  - `usage.prompt_tokens_details.cache_write_tokens` / `usage.prompt_tokens_details.cached_tokens`

### Determination (current stack)

- **Observed support (route-dependent):** `openai/gpt-5.3-codex` via OpenRouter Responses API can benefit from provider-managed prefix caching, but availability/reporting can vary by routed provider/model.
- Caching is provider-managed prefix caching (automatic when request prefixes are identical and long enough).
- In this repo, cache-friendly prompt shaping is enabled by design: a static pre-assembled prefix is placed first, and dynamic issue/PR/runtime content is appended after it.

### What Codex CLI can and cannot control

- Codex workflow config used here supports provider/network basics (for example `wire_api = "responses"`, retries, and timeouts).
- Codex config used here does **not** expose direct request-body prompt-cache controls (for example explicit `cache_control` or manual cache keys) in workflow generation.
- Operational result: cache behavior is achieved through stable prompt-prefix discipline, not per-request cache toggles.

### Operational implications

- Cache hits require identical leading content; edits near the top of prompts reduce hit rate.
- Short prompts may not cross provider cache thresholds and can show little/no savings.
- Cache reuse is best when requests are routed consistently; heavy concurrency and routing changes can reduce hit rates.
- `wire_api = "responses"` is kept across workflows/scripts for the current OpenRouter path.

### Verification recipe

1. Send two consecutive OpenRouter Responses requests with the same large static prefix and only small trailing dynamic differences.
2. Compare usage fields in the second response (for example cached-token indicators when present) against the first response.
3. Repeat a few times to smooth routing variance.
4. In this repo, also confirm generated prompts still keep `pre_assembled_static.txt` (or `judge_static.txt`) content at the top.

### Expected savings assumptions

- Savings are workload-dependent and primarily correlate with:
  - stable prefix size,
  - request repetition frequency,
  - provider routing/cache retention behavior.
- Practical expectation: repeated pipeline runs with large unchanged static prefixes should reduce effective input cost/latency versus fully dynamic prompts.

## Project Orchestrator

The orchestrator enables complex, multi-issue projects from a single prompt. It decomposes a project description into a dependency-aware DAG of GitHub issues, dispatches them through the existing AI pipeline in waves, and uses a judge to validate results between waves.

### Architecture

```
workflow_dispatch (project description)
    → Decomposer (LLM): breaks project into issues + dependency DAG
    → Creates tracking issue + child issues
    → Wave 1 issues enter pipeline (clarify → auto-answer → plan → implement → review → merge)
    → Poller (scheduled): monitors progress, dispatches next waves
    → Judge (LLM, adaptive thinking: `xhigh` cycles 1-3 then `high`, full repo checkout): evaluates after each wave
        → complete: close tracking issue
        → in_progress: create fix-up issues (added to current wave for tracking), advance to next wave
        → failed: auto-recovery (revert + re-plan, retry once), then stop
```

### Setup

**1.** Copy the three wrapper workflows from [`workflow-templates/`](workflow-templates/) into your consumer repo's `.github/workflows/` directory:

- [`ai-orchestrate.yml`](workflow-templates/ai-orchestrate.yml) — triggers decomposition via `workflow_dispatch`
- [`ai-orchestrate-clarify-respond.yml`](workflow-templates/ai-orchestrate-clarify-respond.yml) — auto-answers clarification questions on orchestrator issues
- [`ai-orchestrate-poll.yml`](workflow-templates/ai-orchestrate-poll.yml) — scheduled poller (every 5 min)

Or create them manually — see the inline examples in the [Quickstart](#quickstart) section above.

**2.** Ensure your repo has the required secrets (`GH_PAT`, `OPENROUTER_API_KEY`) and optionally configure the orchestrator variables listed in [Required Variables](#required-variables).

**3.** Go to **Actions → AI Orchestrate → Run workflow**, paste your project description, and click **Run workflow**.

### How it works

1. **Decomposition:** The LLM reads your repo, breaks the project into scoped issues with a dependency graph, and creates a tracking issue (labeled `ai:orchestrator-tracking`).
2. **Wave dispatch:** Wave 1 issues (no dependencies) are created immediately and enter the existing clarify → plan → implement → review pipeline automatically. If clarification questions are raised, the `orchestrate_clarify_respond` workflow answers them automatically using an LLM, so the pipeline runs fully unattended.
3. **Auto-merge:** The poller automatically merges PRs via squash merge when they reach `ai:ready-to-merge`. If a PR has merge conflicts (e.g. `main` advanced since the PR was created), the poller automatically updates the PR branch via the GitHub API before retrying the merge. This requires either (a) no branch protection rules, or (b) branch protection with "Require status checks" that have already passed. See [Enabling auto-merge](#enabling-auto-merge) below.
4. **In-progress conflict resolution:** When the base branch advances and creates merge conflicts on open PRs whose tracking issue is in the `in_progress` or `done` wave status (still going through the review/autofix cycle, or sitting in `ai:done` awaiting promotion to `ai:ready-to-merge`), the poller detects the conflict (`mergeable == false`). It first tries a GitHub API branch update; if that fails (real conflicts), it dispatches the review workflow via `workflow_dispatch`. The review workflow's built-in Codex conflict resolver then handles the resolution on a dedicated runner with a clean environment.
5. **Polling:** Every 5 minutes, the poller checks if the current wave's issues have reached `ai:merged`. When all are merged, it runs the judge.
6. **Judge:** Full repo checkout + tool access (Serena, shell, file reads) with adaptive thinking (`xhigh` for cycles 1-3, then `high`). Compares merged code against the project spec. Decides: complete, in_progress (next wave or fix-ups), or failed.
7. **Next wave:** When the judge approves, the poller creates the next wave's issues (deferred creation — they don't exist until their dependencies are met). This triggers `clarify.yml` via `issues.opened`.
8. **Review-blocked resolution:** When a PR exhausts its autofix iterations (`ai:review-blocked`), the poller invokes a dedicated review-blocked judge (xhigh thinking, full PR context). The judge makes autonomous architectural and security trade-off decisions — it does not defer to humans. It can: (a) merge the PR as-is if remaining issues are cosmetic or low-risk, (b) push an `[orchestrator-fix]` commit with targeted fixes (resets the autofix counter, re-triggers review), or (c) close the PR and create a replacement issue with refined guidance. After `MAX_REVIEW_BLOCKED_RETRIES` (default 2), the judge must choose merge or close+reissue — no further fix attempts.
9. **Implementation-failed recovery:** When the implementation phase reaches the post-Codex pre-commit path with no committable file changes despite an approved plan (e.g. workflow edits stripped without `ALLOW_WORKFLOW_EDITS=true`, or model failure), `implement.yml` labels the source issue `ai:implementation-failed`. The poller automatically closes that issue and creates a replacement with additional diagnostic guidance, so the pipeline retries without manual intervention.
9a. **Post-Codex diagnose + fix-up issue creation:** For targeted post-Codex implementation failures, `implement.yml` now captures diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), runs a short diagnose pass (`prompts/mode-implement-diagnose.txt`), and creates orchestrator-compatible fix-up issue(s). If diagnosis/parsing fails, it creates a deterministic fallback fix-up issue with raw captured diagnostics so failures are never swallowed. This path applies `ai:implementation-failed` and suppresses the generic failure relabel/comment path (preventing re-add of `ai:awaiting-approval`). Out-of-scope failures (missing/empty capture file) continue using the existing generic failure behavior unchanged.
9b. **Destructive-commit guard (`ai:destructive-blocked`):** Before creating the AI implementation commit, `implement.yml` inspects the staged deletion set. The commit is refused — and the workflow run fails — on either of two conditions: (a) any deletion touches the canonical workflow-source list (`agents.md`, `ai_pipeline.md`, `codex_system_instructions.md`, `unattended_llm_system_instructions.md`, `prompts/**`, `scripts/**`, `.github/ai/**`) and `ALLOW_WORKFLOW_EDITS` is not `true`, or (b) the total staged deletions exceed `BULK_DELETE_THRESHOLD` (default `3`) and `ALLOW_BULK_DELETE` is not `true`. On rejection the issue is labeled `ai:destructive-blocked`, a visible comment is posted listing the blocked deletions, and a CRITICAL Telegram alert is sent so a human can intervene. The `Validate approval phase label` step at the top of every subsequent `implement.yml` run refuses to redispatch any issue carrying `ai:destructive-blocked` until a human removes the label after auditing the earlier rejection — the orchestrator's judge-cycle may still regenerate the same task under a fresh issue number, so the TG alert is the intended human-in-the-loop signal. This guard exists because PRs #917/#931 saw a test harness that set `GITHUB_REPOSITORY=owner/repo` trigger a consumer-repo cleanup block in `scripts/orchestrate_poll_process.sh` from within the real coding-workflows checkout, causing the AI implementation commit to silently delete ~10,700 lines across 28 tracked source files. The gate in the poller/review_rb_judge scripts has since been switched from the env var to a git-remote-URL check; the destructive-commit guard in `implement.yml` is the defense-in-depth layer that catches any future destructive path regardless of its trigger.
9c. **Targeted vs legacy post-Codex failure flow:** Targeted post-Codex failures with captured diagnostics follow 9a (diagnose + fix-up issue creation, then label source issue `ai:implementation-failed`). The no-op pre-commit path in 9 remains the close/re-issue retry lane. Other implement workflow failures (for example, missing/empty capture artifacts) remain on the legacy path (`failure()`/`cancelled()` handling in `implement.yml`) with failure comments/alerts.
10. **Auto-recovery:** On failure, the judge can revert problematic PRs and create fix-up issues. Those fix-up issues include the standard orchestrator metadata block (`Tracking issue`, `Integration branch`, `Local ID`, `Managed by`) in the issue body. Recovery is attempted up to `MAX_RECOVERY_ATTEMPTS` (default 3) times; if all attempts fail, the project stops and the operator is notified via Telegram.
11. **Validation-failure recovery:** When runtime validation fails, the poller transitions the project back to the judge for re-evaluation (labeled `ai:validation-recovery`) up to `MAX_VALIDATION_RECOVERY_ATTEMPTS` (default 2) times. The judge sees the validation diagnosis in tracking issue comments, can issue fix-up work (with orchestrator metadata), and then re-validates. After exhausting the recovery budget, the project goes to terminal `ai:validation-failed`.
11a. **Integration branch delivery:** Orchestrator projects now create a per-project integration branch (`orchestrator/project-<tracking_issue>`). All orchestrator child issues include `Integration branch` metadata so implementation PRs target the integration branch instead of `main`. The poller periodically syncs default branch changes into this branch via the merge API.
11b. **Sync conflict handling and superseded detection:** Before sync merge attempts, the poller checks whether the integration branch is effectively superseded by the default branch (tracked child PRs are terminal and affected-path deltas are already represented on the default branch). Superseded projects persist `sync.status = superseded-by-main`, post one final tracking comment, and skip future sync attempts without recurring Telegram warnings. Real unresolved conflicts include parsed conflict paths, a deduped fingerprint to prevent repeated spam, and a rebuild runbook link: [docs/orchestrator-integration-branch-rebuild-runbook.md](docs/orchestrator-integration-branch-rebuild-runbook.md).
11c. **Integration self-healing:** If a periodic `main` → integration-branch sync returns HTTP 409 (real conflict), the poller routes recovery through `heal_integration_branch_conflict`: it (a) ensures/creates the final integration→default PR (eagerly, if it does not yet exist), (b) dispatches the review/autofix workflow through `_dispatch_review_for_conflicts` against that PR to run the existing Codex conflict resolver on a clean runner, and (c) records the attempt in new tracking-state fields (`integration_sync_status`, `integration_sync_last_error`, `integration_conflict_dispatch_count`, `integration_conflict_dispatch_ts`, `integration_conflict_unresolved_ticks`). Dispatches are throttled by `CONFLICT_DISPATCH_COOLDOWN_SECS` (default 900s). After `INTEGRATION_CONFLICT_MAX_RETRIES` (default 3) unresolved ticks the orchestrator escalates by invoking the judge with full PR context via `codex exec`. Only after both the automated resolver *and* the judge escalation fail is the project marked terminally `failed`. The same healing flow is triggered from `finalize_integration_merge_if_needed` whenever the final PR is observed with `mergeable=false`, so the project no longer halts on first conflict.
11d. **Atomic final merge:** When a project is complete (or validated), the poller creates/reuses a final PR from integration branch to default branch and squash-merges it.
11e. **Phase-agnostic feature-PR drift sweep:** On every poll tick the orchestrator enumerates all open PRs whose head branch matches `ai/issue-*` and calls the GitHub update-branch endpoint for any whose `mergeStateStatus` is `behind`. This fast-forwards clean-mergeable branches before they accumulate enough drift to become conflicted, regardless of the issue's current pipeline phase. Real conflicts (`dirty`) are left for the existing in-progress conflict loop to handle via the resolver dispatch path.
12. **Stall detection and self-healing:** Every poll cycle, the poller tracks how long each issue has been in its current pipeline phase. Stall thresholds are **adaptive per phase**: lightweight phases (clarification, planning, approval, merge) default to 60 minutes, while heavy phases (implementation, review/autofix) default to 120 minutes. Each threshold is independently configurable via `STALL_THRESHOLD_<PHASE>_MINUTES` env vars, with `STALL_THRESHOLD_MINUTES` as the global fallback. Before stall checks, the poller reconciles managed-issue labels and state truth (labels + issue open/closed + linked PR merge state), repairs missing/conflicting phase labels, and persists reconciled statuses every cycle. Closed/terminal issues are hard-guarded out of retrigger paths; stale `no_labels` on closed issues is healed (label/state repair) instead of retriggered. When an issue exceeds its phase threshold, the poller computes a declarative fallback action from `STALL_RECOVERY_ACTIONS` by `stall_recovery_count` (index clamped to the last action). Current ladders are: `no_labels` → `retrigger_pipeline`, `ai:clarification` → `auto_respond_clarify`, `ai:planning` → `retrigger_plan`, `ai:awaiting-approval` → `auto_approve`, `ai:implementing` → `retrigger_implement`, `ai:done` → `retrigger_review`, `ai:ready-to-merge` → `attempt_merge`; each ladder currently has two same-phase retries and then `escalate_human`, with the last entry repeated once the index is out of range. If `ENABLE_STALL_JUDGE=true` and `stall_recovery_count >= STALL_JUDGE_TRIGGER_COUNT` (while still below `MAX_STALL_RECOVERIES_PER_ISSUE`), the selected action is overridden to `run_stall_judge` for diagnostics-driven action selection. The stall judge may choose targeted actions including `resolve_merge_conflict`; that path attempts GitHub `update-branch` for the target PR and then dispatches `_dispatch_review_for_conflicts`. If `ENABLE_STALL_HUMAN_TERMINALIZATION=false` (default), a stall-judge `escalate_human` result is terminalization-gated and converted to the same non-human declarative fallback action for that issue/phase/recovery count. If stall-judge execution fails, output parsing fails, or the returned action is unsupported, the poller fail-opens to that same declarative fallback action for that phase/recovery count. After `MAX_STALL_RECOVERIES_PER_ISSUE` (default 5) attempts, action selection becomes `skip` and the issue is closed (`ai:closed`) so the wave can advance; the judge evaluates the gap at wave completion and decides whether to reissue, accept, or fail. When `ENABLE_STALL_JUDGE=false`, or when `STALL_JUDGE_TRIGGER_COUNT` is effectively unreachable within the configured recovery budget, recovery remains on declarative `STALL_RECOVERY_ACTIONS` actions without judge escalation. All stall recoveries trigger Telegram notifications. Standalone AI issues (not linked to any active orchestrator tracking state) also use the same stall recovery engine when `ENABLE_STANDALONE_STALL_RECOVERY=true`; standalone recovery state is persisted per issue in a hidden marker comment. Additionally, all orchestrator-created issues (Wave 1, deferred waves, reissues, and judge fix-ups) now receive the `ai:clarification` label at creation time, ensuring they enter the pipeline immediately without relying solely on the `issues.opened` event trigger.
12a. **Missing state recovery:** If the orchestrate.yml workflow creates issues but fails before posting the initial state comment (e.g. due to a transient API error or timeout), the poller automatically reconstructs the state. It parses the tracking issue body to extract the wave structure and dependency graph, searches for child issues that reference the tracking issue, and builds a new state object. The reconstructed state is posted as a comment so subsequent poll cycles operate normally. This prevents projects from being permanently stuck when the initial orchestration run fails mid-execution.
13. **Validation gate:** When the judge says "complete" and `ENABLE_VALIDATION=true`, the poller dispatches `ai-validate.yml` on the integration branch (`--ref <integration_branch>`), marks the tracking issue `ai:validating`, and only transitions to complete after `ai:validated` plus successful final squash merge.
14. **Completion:** When validation is disabled, completion remains judge-driven and immediate.

### Enabling auto-merge

Auto-merge works at two levels: the **review workflow** (merges individual PRs right after review passes) and the **orchestrator poller** (merges PRs for orchestrator-managed issues). Both use the same `ENABLE_AUTO_MERGE` variable.

**Step 1: Enable in GitHub repo settings**
1. Go to **Settings → General → Pull Requests** and check **Allow auto-merge**.
2. If you use branch protection, ensure your required status checks are configured (the PR will auto-merge once they pass).

**Step 2: Set the variable**
In **Settings → Secrets and variables → Actions → Variables**, add:
- `ENABLE_AUTO_MERGE` = `true`

This enables `gh pr merge --squash --auto` in both `review_autofix.yml` (right after setting `ai:ready-to-merge`) and the orchestrator poller. GitHub queues the merge and executes it once all required checks pass.

**Branch protection compatibility:**

| Setup | Auto-merge behavior |
|---|---|
| No branch protection | PR merged immediately after review passes |
| Branch protection + required checks | PR queued, merged once checks pass |
| Branch protection + required human reviews | Cannot auto-merge unless bot account is added as a bypass actor |

Your `GH_PAT` must have permission to enable auto-merge (repo scope with admin or write access).

### Labels

The orchestrator uses `ai:orchestrator-tracking` for tracking issues. Child issues use the standard `ai:*` phase labels.

The label contract (`/.github/ai/label_contract.v1.json`) is the single source of truth for:
- label definitions (name/color/description),
- phase exclusivity groups,
- contract-driven phase add/remove transitions.

The poller’s managed-wave reconciliation pass repairs labels against this contract each cycle before wave-status and stall logic.

### Telegram Notifications & Cleanup

Telegram notifications fall into three categories based on their lifecycle:

**Persistent alerts (never deleted):**
- **Release results** — success/failure from `test-and-mark-stable.yml`
- **PR merged** — sent by `issue_pr_status.yml` for non-orchestrator issues
- **Orchestrator project completion** — sent by the poller after all tracked messages are cleaned up

**Phase-tracked alerts (deleted when the phase completes):**
For non-orchestrator issues, human-intervention alerts are cleaned up automatically when the next phase begins:
- **Clarification required** — sent by `clarify.yml` and `plan.yml` for non-orchestrator issues only, deleted when `plan.yml` runs (stored as `<!-- tg_phase:clarify:id -->`). Orchestrator-managed issues skip this alert since clarifications are auto-answered by `orchestrate_clarify_respond.yml`.
- **Plan awaiting approval** — sent by `plan.yml` (when `AUTO_IMPLEMENT_ON_CLEAR_PLAN` is not true), deleted when `implement.yml` runs (stored as `<!-- tg_phase:plan:id -->`)

**General tracked alerts (deleted at terminal state):**
- Orchestrator-managed issue alerts use general tracking (`<!-- tg_cleanup:id1,id2,... -->`), cleaned up when the tracking issue reaches a terminal state (complete or failed) via the poller.
- Any remaining tracked messages (general or phase) are cleaned up when a PR is closed/merged by `issue_pr_status.yml`.

**Requirements:**
- `TG_BOT_SECRET` must be set (same secret used for sending).
- The bot must have permission to delete messages in the target chat (this is automatic for messages the bot itself sent, within 48 hours).
- No additional configuration is needed — cleanup is enabled automatically when `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID` are set.

**Note:** Messages older than 48 hours cannot be deleted by the Telegram Bot API. For long-running orchestrated projects, intermediate messages sent more than 48 hours before completion will remain in the chat.

## Runtime Validation Phase

This phase starts only after the orchestrator judge returns `complete`.

### Lifecycle After Judge Approval

1. The poller transitions the tracking issue to `ai:validating` and dispatches `.github/workflows/ai-validate.yml`.
2. The wrapper workflow calls reusable `.github/workflows/validate.yml@stable`, which runs `scripts/validate_process.sh`.
3. If validation passes, `validate_process.sh` sets `ai:validated`; the poller marks the project `complete` and closes the tracking issue.
4. If validation fails with fixable findings (`needs_fixes`), `validate_process.sh` creates fix-up issues, comments them on the tracking issue, and sets `ai:validation-fixing`.
5. While in `ai:validation-fixing`, the poller waits for all active validation fix-up issues to reach `ai:merged`.
6. After all fix-up issues merge, the poller increments the validation cycle, returns to `ai:validating`, and redispatches `ai-validate.yml`.

### Pass/Fail and Stop Conditions

- Terminal success: `ai:validated`.
- Non-terminal failure: `needs_fixes` diagnosis with fix-up issues (enters the fix/revalidate loop).
- Terminal failure: validation dispatch failure, harness error, infeasible diagnosis, unknown diagnosis payload, closed fix-up issues, or cycle limit exceeded.
- Terminal failure label: `ai:validation-failed`.
- Managed artifact contract: startup checks now enforce only managed artifacts (`scripts/validate_process.sh`, optional `scripts/validate_driver.sh`) and the transient `validation/validate.sh` rule. Repos may keep unrelated consumer scripts such as `scripts/validate_local.sh` without failing validation.

### Manual Reset: `/revalidate`

When a tracking issue reaches `ai:validation-failed`, you can manually reset it by commenting `/revalidate` on the tracking issue. The next poller cycle will:

1. Reset all validation counters (`validation_cycle` → 1, `validation_recovery_count` → 0).
2. Clear the failure reason and any tracked fix-up issues.
3. Transition the label from `ai:validation-failed` to `ai:validating`.
4. Dispatch a fresh validation run (cycle 1).

This is useful after fixing the root cause manually (e.g. correcting a Docker config, adding a missing env var, or updating a dependency). There is no limit on how many times `/revalidate` can be used — the operator decides when to stop retrying.

### Manual Reset: `/judge_resume`

When a tracking issue reaches terminal `failed` status due to judge stall cycle exhaustion (`MAX_JUDGE_CYCLES`) or recovery attempt exhaustion (`MAX_RECOVERY_ATTEMPTS`), you can manually resume it by commenting `/judge_resume` on the tracking issue. The next poller cycle will:

1. Reset judge stall cycles (`judge_stall_cycles` → 0).
2. Reset recovery counter (`recovery_count` → 0).
3. Transition the project status from `failed` to `in_progress`.
4. Resume normal wave processing immediately.

This does **not** reset the total `judge_cycle` counter (which is informational only — it tracks how many times the judge has been invoked overall). Only the stall and recovery counters that gate the failure limits are reset.

Use this after manual intervention (e.g. fixing a problematic issue, merging a stuck PR, or adjusting `MAX_JUDGE_CYCLES`/`MAX_RECOVERY_ATTEMPTS` variables). There is no limit on how many times `/judge_resume` can be used.

> **Note:** `/judge_resume` only applies to judge/recovery failures. For validation failures (`ai:validation-failed`), use `/revalidate` instead.

### Validation Controls

| Variable | Default | Behavior |
|---|---|---|
| `ENABLE_VALIDATION` | `true` | Truthy values (`1/true/yes/on`, case-insensitive) enable the validation gate. Any other value disables it, so judge `complete` closes immediately without runtime validation. |
| `MAX_VALIDATE_CYCLES` | `3` | Maximum cycles across initial validation plus fix/revalidate loops. Must be a positive integer; invalid values are coerced to `3`. Exceeding the limit forces `ai:validation-failed`. |

### Wrapper Setup and Reusable Workflow Relationship

- Consumer repos must provide `.github/workflows/ai-validate.yml` so the poller can dispatch validation by workflow name.
- Use [`workflow-templates/ai-validate.yml`](workflow-templates/ai-validate.yml) as the wrapper template.
- The wrapper must call reusable [`/.github/workflows/validate.yml`](.github/workflows/validate.yml) with these inputs:
- `tracking_issue` (tracking issue number)
- `compose_file` (compose fallback path, default `docker-compose.yml`)
- `validation_timeout` (idle timeout in minutes — process is killed only after this long with no output, default `15`)
- If the wrapper is missing or dispatch permissions are insufficient, the poller marks the tracking issue `ai:validation-failed`.

### Runtime Constraints

- Validation runs on `ubuntu-latest`.
- Validation must execute against local runtime dependencies only (Docker/Compose services on the runner).
- Use synthetic/test credentials only; defaults are test-safe (`VALIDATION_TEST_USERNAME`, `VALIDATION_TEST_PASSWORD`, `VALIDATION_TEST_API_KEY`).
- Do not require external infrastructure (managed cloud databases, private VPC services, external queues, or production-only endpoints) for validation success.

### Hints Configuration (`.ai/validate.yml`)

- You can optionally add `.ai/validate.yml` in a consumer repo to guide harness generation and diagnosis.
- Baseline example: [`examples/ai-validate-hints.yml`](examples/ai-validate-hints.yml).
- If `.ai/validate.yml` is absent, validation now runs a lightweight discovery phase that generates an ephemeral runtime hints file (not committed).

### Validation Harness Lifecycle

- Cycle 1 generates a new harness under `validation/`.
- Cycle 2+ reuses and targeted-fixes the existing owned harness when `validation/` is present (for example, restored from artifacts); otherwise it safely falls back to full regeneration.
- `validation/validate.sh` is generated as a thin wrapper that delegates to checked-in `scripts/validate_driver.sh`.
- Canonical runtime harness behavior now lives in `scripts/validate_driver.sh` (pre-flight, compose startup/logging, health polling, canary gating, TAP-safe counting, result emission/finalization).
- `scripts/validate_driver.sh` loads optional `validation/validate.env` and applies conservative defaults for supported knobs (including `APP_SERVICE`, `APP_URL`, `HEALTH_TIMEOUT`, `PHASE`).
- Before execution, validation runs pre-flight checks (`docker compose config`, shell syntax, and compose build path resolution).
- Pre-flight failures are classified as terminal `harness_error` for that run.
- The first generated test must be a canary infrastructure check (`00_canary.sh` style); infra-only canary failures shortcut to `harness_error`, while app startup/crash signals continue to diagnosis.

## Repository Structure

```
coding-workflows/
  .github/
    workflows/          # Reusable workflow_call workflows
    actions/
      setup-runtime/    # Shared composite action for runtime setup
    ai/                 # AI config: label contract, orchestrate schema
  scripts/              # Helper scripts (memory, context, git, orchestrator)
  prompts/              # LLM prompt templates (clarify, plan, orchestrate, judge)
  ai-memory/            # Memory schemas, config, and examples
  netwask/              # Agent configuration
  docs/                 # Documentation
```

## Versioning

- **Immutable tags**: `v1.0.0`, `v1.0.1`, etc.
- **Stable channel**: `@stable` — moving tag, updated after canary validation
- **Canary channel**: `@canary` — pre-stable testing

Consumer repos pin to `@stable` for automatic updates or exact tags for reproducibility.

## Contributing

1. Make changes in a feature branch
2. Test via canary channel on pilot repos
3. Promote to stable after validation

See `docs/release-policy.md` for the full release process.
