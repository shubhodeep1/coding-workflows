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

The memory schema set also includes the cross-run cache document used by workflow analysis tooling: `workflow_log_analysis_cache.v1.json`.

**Telemetry:** Every memory operation emits a structured `AI_MEMORY_TELEMETRY: {...}` line to workflow logs (stderr from Python; shell wrappers use stdout unless stdout is reserved for machine-readable JSON, in which case telemetry is sent to stderr). These lines are picked up by the workflow log analysis pipeline and surfaced in the **AI Memory Health** section of optimization reports. Key fields: `op` (operation name), `ok`, `records_selected`, `estimated_tokens`, `keyword_method` (`llm`/`plain`/`none`), `fail_open`, `did_push`.

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
| `ALLOW_WORKFLOW_EDITS` | No | `true` | review_autofix, implement, update_workflows, orchestrate_poll | Allow AI edits to `.github/workflows` files and automatic wrapper updates. Set to `false` to opt out of auto-updates. Orchestrator conflict-dispatch (`_dispatch_review_for_conflicts`) forwards this value to the dispatched review workflow via `-f allow_workflow_edits=`. |
| `ENABLE_AUTO_MERGE` | No | `true` | review_autofix, orchestrate_poll | Auto-merge PRs (squash) when review passes. Requires "Allow auto-merge" in repo settings. |
| `MAX_AUTOFIX_ITERATIONS` | No | `3` | review_autofix | Maximum consecutive autofix rounds before the review loop stops and marks the PR `ai:review-blocked`. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | No | (built-in catalog in script) | review_autofix | Optional path to a custom floor-rule keyword catalog consumed by `scripts/review_floor_rules.sh`. When unset, missing, or unreadable, the script falls back to its built-in keywords and logs a warning. |
| `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` | No | `8` | review_autofix | Seconds the post-commit and editor-changes-lost retrigger steps wait before checking for an already-queued peer review run on the same PR branch. If a peer is found the retrigger skips its own `workflow_dispatch` to avoid creating a redundant queued run (and extra API/UI noise) in the `pr-autofix-${PR}` concurrency group (see [Autofix retrigger dedup](#autofix-retrigger-dedup)). Must be an integer in `0..60`; invalid values clamp to `8`. |
| `AUTOFIX_SKIP_SELF_TRIGGERED` | No | `true` | review_autofix | Skip the full reviewer/editor cycle on `pull_request.synchronize` events whose HEAD commit is a `[ai-autofix]` commit pushed by the configured bot account (GitHub-attributed identity, see `AUTOFIX_BOT_LOGIN`). These synchronize events are self-triggered by the prior autofix commit and otherwise cost a second full review pass (5 reviewers + consensus + editor) per fix round — roughly 2× LLM spend per autofix iteration. The gate job in `review_autofix.yml` queries the HEAD commit via one `GET /repos/{repo}/commits/{sha}` call and extracts `(commit.message first line, author.login, committer.login)` — `.author.login` / `.committer.login` are GitHub-resolved from the push credentials and are not user-controlled (unlike `.commit.author.email`, which git will accept from any local config). The gate sets `should_run=false` only when the subject starts with `[ai-autofix]` AND at least one of `.author.login` / `.committer.login` equals `AUTOFIX_BOT_LOGIN` (default `codex`); fails open on API error or when both logins are empty. The post-commit `workflow_dispatch` retrigger step applies a mirror guard; when `AUTOFIX_CONTINUATION_ENABLED=true` (default) the mirror skips only ledger-only commits (§20.2) and the legacy opt-out case, so productive `[ai-autofix]` commits immediately dispatch the next iteration via `workflow_dispatch` (see `AUTOFIX_CONTINUATION_ENABLED` and agents.md §20.4). `[ai-merge-resolve]` / conflict-resolved pushes also dispatch a follow-up verification pass for post-conflict-resolution safety. `workflow_dispatch`, `opened`, `reopened`, and `ready_for_review` events always run regardless of this flag. Set to `false` to opt out and restore the legacy "every commit re-verifies" behaviour. Safety net for orchestrator-tracked PRs: the orchestrator stall cron (`internal-orchestrate-poll.yml`, `*/30 * * * *`) re-kicks autofix via `workflow_dispatch` (which bypasses the skip) if a phase-timer threshold trips; continuation closes the same gap in-run for non-orchestrator PRs. Audit via `AUTOFIX_GATE_SKIP reason=self_triggered_autofix` / `AUTOFIX_GATE_NO_SKIP_IDENTITY` / `AUTOFIX_GATE_SKIP_QUERY_FAILED` / `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix` log lines (see [Autofix retrigger dedup](#autofix-retrigger-dedup)). |
| `AUTOFIX_BOT_LOGIN` | No | `codex` | review_autofix | GitHub login that the gate job accepts as the authoritative bot identity for the self-triggered autofix skip. Compared against `.author.login` / `.committer.login` on the HEAD commit API response — both are GitHub-attributed (resolved server-side from push credentials), not user-controlled git metadata. Override if you run the workflow under a fork of codex that pushes as a different bot account (e.g. `codex-bot`, `my-org-codex`). Unset or empty falls back to the default `codex` (via shell `${AUTOFIX_BOT_LOGIN:-codex}` expansion) — to disable the skip entirely, set `AUTOFIX_SKIP_SELF_TRIGGERED=false` instead. |
| `AUTOFIX_CONTINUATION_ENABLED` | No | `true` | review_autofix | When `true` (the default), the `Re-trigger review via workflow_dispatch` step in `review_autofix.yml` proceeds to dispatch the next autofix iteration via `workflow_dispatch` after a **productive** `[ai-autofix]` commit (`DID_COMMIT=true` AND `LEDGER_ONLY_COMMIT!=true` AND `CONFLICT_RESOLVED!=true`). This closes the ~0–120 min idle window where an `[ai-autofix]` push would otherwise wait for the orchestrator stall cron (which does not scan non-orchestrator PRs at all). Ledger-only commits (§20.2) still route to the clean-review tail in the same run — no continuation dispatch is issued. Conflict-resolved commits keep their pre-continuation dispatch path. Set to `false` to restore the pre-continuation behaviour where `AUTOFIX_SKIP_SELF_TRIGGERED` alone gated productive autofixes out of the dispatch step. `workflow_dispatch` bypasses the gate job's self-triggered skip by design — continuation is a first-class successor run, not a redundant verification. Pre-dispatch guard: settle delay (`AUTOFIX_CONTINUATION_SETTLE_SECS`). Iteration-cap handling remains in the dispatched run's `retrigger_guard` path (which gates reviewers/editor and routes exhaustion to the review-blocked judge). Alerts: the continuation path is silent (no Telegram); stall-cron `Stall recovery: re-triggered review …` alerts are unchanged and still fire only for genuine orchestrator-tracked stalls. Audit via `AUTOFIX_CONTINUATION_DISPATCH_ISSUED` / `AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected ... continuation=true` / `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix continuation_enabled=<val>` log lines. See agents.md §20.4 for the contract. |
| `AUTOFIX_CONTINUATION_SETTLE_SECS` | No | `10` | review_autofix | Seconds the continuation path `sleep`s between the push and the `workflow_dispatch` call, to let GitHub's internal indices catch up before the dispatched run checks out the new HEAD SHA. Integer in `1..60`; invalid or out-of-range values clamp to `10`. Not applied to the conflict-resolved dispatch path (that keeps its existing `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` peer-wait). |
| `ENABLE_REVIEWER_TWO_PASS` | No | `true` | review_autofix | When true, reviewers run two passes per iteration: pass 1 at `medium` reasoning (broad sweep), then pass 2 at the scheduled reasoning level with a cross-pollination summary of pass 1 findings. Set to `false` to use a single pass at the scheduled reasoning level. |
| `XPOLL_SUMMARISER_MODEL` | No | `openai/gpt-5.4-mini` | review_autofix | Model slug (resolved through codex-cli's OpenRouter provider) used by `scripts/summarize_reviewer_consensus.sh`. After each review pass finishes, this model consolidates every reviewer's output into one ledger: a `=== CONSENSUS FINDINGS ===` block with cross-reviewer dedup (entries carry `flagged_by: [slug, ...]`) followed by per-reviewer sections. The pass-1 ledger feeds pass-2 reviewers; the pass-2 ledger is written to `REVIEWER_CONSENSUS_FILE` and feeds the editor + memory-record step. |
| `XPOLL_SUMMARISER_REASONING` | No | `xhigh` | review_autofix | Reasoning effort (`xhigh` / `high` / `medium` / `low`) applied to the summariser model via its isolated `CODEX_HOME` config.toml. Keeps dedup quality high. Isolated config guarantees the override cannot leak into the editor's codex-cli invocation. |
| `XPOLL_SUMMARISER_LINES_PER_REVIEWER` | No | `160` | review_autofix | Target max per-reviewer section lines; summariser is told to collapse related findings (`(N related items)` suffix) rather than drop them when over-budget. Overall ledger target is this value × reviewer count + 120. |
| `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS` | No | `600` | review_autofix | Per-attempt wall-time timeout for a single codex-cli summariser invocation. On timeout / non-zero exit / empty stdout the summariser retries up to 3 times, then hard-fails the workflow (the job-level "Telegram failure" step surfaces the incident). |
| `XPOLL_SUMMARISER_MAX_INPUT_LINES` | No | `3000` | review_autofix | Pre-truncation ceiling per reviewer output before concatenation into the summariser prompt. Prevents a pathological reviewer output from blowing the summariser's context budget. |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | No | `true` | review_autofix | When true, non-orchestrator PRs that exhaust autofix iterations invoke a judge (LLM) to decide: merge as-is, push a fix commit, or close and reissue. Orchestrator-managed PRs are skipped (handled by the poller). PRs without linked issues use the PR title/body as requirement context. |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | No | `xhigh` | review_autofix | Reasoning effort for the review-blocked judge in non-orchestrator PRs (`xhigh`, `high`, `medium`, `low`). |
| `MAX_REVIEW_BLOCKED_RETRIES` | No | `2` | review_autofix, orchestrate_poll | Maximum judge retries for review-blocked PRs before forcing a final decision (merge or close+reissue). Used by both the review_autofix judge (counts `[judge-fix]` commits) and the orchestrator poller. |
| `ENABLE_VALIDATION` | No | `true` | orchestrate_poll | When true, a `complete` judge verdict transitions the tracking issue into runtime validation (`ai:validating`) and completion occurs only after validation passes. |
| `MAX_VALIDATE_CYCLES` | No | `3` | orchestrate_poll | Maximum runtime validation cycles (initial run + fix/revalidate loops) before forcing `ai:validation-failed`. |
| `MAX_SELF_HEAL_ATTEMPTS` | No | `2` | validate | Maximum in-process self-heal attempts per `validate_process.sh` invocation. Self-heal patches one of the four validation prompt files locally and re-execs the pipeline, and does NOT increment `MAX_VALIDATE_CYCLES`. Set to `0` to disable. See [Validation self-healing](#validation-self-healing). |
| `VALIDATE_PREFLIGHT_PYFLAKES_ENABLED` | No | `true` | validate | When `true`, the preflight phase of `scripts/validate_process.sh` runs `pyflakes` and `ruff check --select $VALIDATE_PREFLIGHT_PYFLAKES_RULES` against every quoted `python3 - <<'PY' ... PY` heredoc body under `validation/**/*.sh`. Catches undefined-name (F821) / unused-import / redefinition bugs that `ast.parse` alone cannot see and that runtime tests miss when the bug lives in an unexercised conditional branch (observed as `unknown_error:NameError` in consumer-repo autobet finalize logs). Missing tools are auto-installed via `python3 -m pip install --user`; install failure fails open with a `::warning::` and skips the check. Invalid values are coerced to `true`. |
| `VALIDATE_PREFLIGHT_PYFLAKES_RULES` | No | `F` | validate | Ruff rule selector passed to `ruff check --select`. Default `F` covers all pyflakes-equivalent rules (F401 unused import, F811 redefinition, F821 undefined name, F823 local-before-assign, F841 unused local, etc.). Must match `^[A-Z0-9,]+$`; invalid values fall back to `F`. Narrow to `F821` if operator wants only the NameError bug class to block. |
| `VALIDATE_WORKFLOW_NAME` | No | `ai-validate.yml` | orchestrate_poll | Workflow filename to dispatch for runtime validation. Override to `internal-validate.yml` for repos using the internal naming convention. Falls back to `internal-validate.yml` automatically if the primary name fails. |
| `MAX_JUDGE_CYCLES` | No | `25` | orchestrate_poll | Maximum judge evaluation cycles per project before forcing failure. Prevents infinite fix-up loops when the judge repeatedly returns `in_progress`. |
| `ENABLE_CLEAN_WAVE_JUDGE_SKIP` | No | `true` | orchestrate_poll | When true, a completed clean wave (no failures, not stuck-wave) advances mechanically without invoking the judge. Also skips the judge on clean project completions (all waves merged, no failures, no review-blocked issues) — the verdict is deterministic (`complete`). Set to `false` to force judge execution on every wave completion and project finalization. |
| `ORCHESTRATOR_MAX_CLARIFY_CYCLES` | No | `3` | orchestrate_clarify_respond | Maximum orchestrator clarification auto-answer cycles per issue. When the limit is exceeded, or when a clarify hash repeats, `orchestrate_clarify_respond` stops posting auto-answers and escalates the issue to `ai:blocked` for explicit human intervention. A backup comment-count guard counts existing `/answer [auto-answered-by-orchestrator]` comments on the issue thread (0 extra API calls) and blocks when the count reaches this limit, even when the memory-based guard fails open. |
| `STALL_THRESHOLD_MINUTES` | No | `120` | orchestrate_poll | Fallback minutes an issue can remain in the same pipeline phase before auto-recovery. Used when no per-phase override is set. |
| `STALL_THRESHOLD_NO_LABELS_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for issues with no AI pipeline labels (pre-pipeline). |
| `STALL_THRESHOLD_CLARIFICATION_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:clarification` phase. |
| `STALL_THRESHOLD_PLANNING_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:planning` phase. |
| `STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:awaiting-approval` phase. |
| `STALL_THRESHOLD_IMPLEMENTING_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:implementing` phase. |
| `STALL_THRESHOLD_DONE_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:done` phase (review/autofix). |
| `STALL_THRESHOLD_READY_TO_MERGE_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:ready-to-merge` phase. |
| `MAX_STALL_RECOVERIES_PER_ISSUE` | No | `5` | orchestrate_poll | Maximum stall recovery attempts per individual issue. Recovery selection uses `stall_recovery_count` against `STALL_RECOVERY_ACTIONS` (clamped to the last action), with optional escalation to `run_stall_judge` when enabled and the trigger count is reached. After exhausting this limit the issue is skipped (`ai:closed`) so the wave can advance; the judge evaluates the gap at wave completion. |
| `STALL_JUDGE_TRIGGER_COUNT` | No | `2` | orchestrate_poll | Stall recovery attempt threshold at which the poller escalates from declarative ladder actions to `run_stall_judge` for deeper diagnostics and action selection. |
| `ENABLE_STALL_JUDGE` | No | `true` | orchestrate_poll | Enables/disables stall-judge escalation (`run_stall_judge`) in orchestrator-managed and standalone stall recovery paths. |
| `ENABLE_STALL_HUMAN_TERMINALIZATION` | No | `false` | orchestrate_poll | Allow terminal `escalate_human` stall actions; when `false`, both declarative and judged stall actions downgrade `escalate_human` to the nearest prior non-human phase action. |
| `ENABLE_STANDALONE_STALL_RECOVERY` | No | `true` | orchestrate_poll | Enable stall detection and auto-recovery for standalone AI issues (issues not managed by an active orchestrator tracking state). |
| `ENABLE_CLOSE_MERGED_ISSUES` | No | `true` | orchestrate_poll | Enable the per-cycle sweep that closes any open GitHub issue carrying `ai:merged` once at least one cross-referenced PR is verified merged via the issue timeline helper (`gh_issue_timeline_with_cross_refs`, GraphQL-first with fail-open REST fallback). Applies to both orchestrator-managed child issues and standalone (non-orchestrator) issues. Tracking issues (`ai:orchestrator-tracking`) are intentionally skipped — they are closed by the orchestrator project completion path. If an issue has `ai:merged` but no merged PR can be verified on its timeline, the sweep leaves it open and sends a Telegram `WARNING` alert instead of guessing. |
| `ENABLE_STALL_MERGED_PR_GUARD` | No | `true` | orchestrate_poll | Before firing an early-phase stall recovery command (`retrigger_pipeline`, `auto_respond_clarify`, `retrigger_plan`, `auto_approve`, `retrigger_implement`), double-check the issue's linked pull request state. If the most recent linked PR is `MERGED`, the command is **not** posted: the issue is tagged `ai:merged` (so `close_merged_issues_sweep` closes it on the next cycle), a healing note is added, and a Telegram `WARNING` is sent. Applies to both the orchestrator-managed stall loop and the standalone stall watchdog. In the steady-state path, linked-PR state is prefetched in batched GraphQL calls (`_fetch_linked_pr_status_graphql` for managed, extended `_fetch_candidate_issue_details_graphql` for standalone) — the managed-path prefetch runs **unconditionally** whenever there are stalled issues, so the pre-existing open-PR sub-guard also benefits from the batched cache regardless of this flag. On cache/prefetch miss both paths fall back to a per-issue REST probe (timeline + PR payload) for the merged-PR sub-guard, and the managed path's open-PR sub-guard also falls back to the legacy per-issue REST lookup — so this is not a strict "0 extra per-issue API calls" path when GraphQL is unavailable. Introduced to prevent the `/reclarify` loop on issues whose phase label got stripped after merge (see GH issue #1074). Set to `false` to disable only the merged-PR short-circuit (the open-PR sub-guard and the batched prefetch still run). |
| `MAX_RECOVERY_ATTEMPTS` | No | `3` | orchestrate_poll | Maximum project-level recovery cycles when the judge declares failure. Replaces the previous single-shot `recovery_attempted` boolean with a configurable counter. |
| `MAX_VALIDATION_RECOVERY_ATTEMPTS` | No | `2` | orchestrate_poll | Maximum times the poller transitions a validation-failed project back to the judge for re-evaluation before marking it as terminally failed. Set to `0` to disable (immediate terminal failure on first validation failure, matching pre-recovery behavior). |
| `MAX_FINAL_MERGE_ATTEMPTS` | No | `3` | orchestrate_poll | Bounded retry budget for the post-validation final integration→default squash merge inside `mark_validation_complete`. Poll ticks that hit *blocking* final-merge failures increment `final_merge_attempt_count`; transient "not ready yet" conditions (for example mergeability still computing or required checks still pending) defer without consuming this budget. On success the counter is reset. After the budget is exhausted the project is escalated to `ai:blocked` (tracking issue label + state `failed` + CRITICAL Telegram alert) instead of being silently advanced to `status=complete`. Must be a positive integer; invalid values fall back to `3`. |
| `MAX_VALIDATION_FIX_BATCH_CYCLES` | No | `30` | orchestrate_poll | Maximum poll cycles a single validation fix-up batch (the set of issue numbers extracted from the most recent `## 🧪 Runtime validation found fixable issues` tracking comment) can sit in "still in progress" before the poller escalates via `mark_validation_failed` — which still honours `MAX_VALIDATION_RECOVERY_ATTEMPTS` for judge re-evaluation. Counter resets when a new fix-issues comment arrives, when the batch completes (all issues merged), or when `mark_validation_failed` clears the active list. Each fix-up issue is now also inspected for its live GitHub `state`/`state_reason`, so a fix-up issue closed without the `ai:closed` label is detected in the same poll cycle instead of stalling until this ceiling trips. |
| `MAX_IMPL_NOOP_REISSUES` | No | `2` | orchestrate_poll | Maximum automatic re-issues for an `ai:implementation-failed` issue before the poller closes it as likely already implemented and defers final verification to the wave-completion judge. Must be a positive integer; invalid values fallback to `2`. A belt-and-braces `count_noop_ancestors` walk of the `Re-issued from #N` chain (same cap) runs in parallel with the state-based counter in all three poller re-issue paths (`execute_stall_recovery_action close_and_reissue`, `run_standalone_stall_recovery close_and_reissue`, and the `no-op-implementation` branch of the `ai:implementation-failed` sweep); either signal trips closure. This catches the failure mode where the state-based counter is stale — e.g. the tracking-issue state comment was truncated or the wave iterator never refreshed `get_impl_noop_count` — which caused tracking issue #1292 to spawn 30+ duplicate sub-issues in ~5 hours. API cost: up to `2 * MAX_IMPL_NOOP_REISSUES` calls per invocation, fail-open on any API error. |
| `IMPL_NOOP_ANCESTRY_THRESHOLD` | No | `2` | implement | Ancestor-chain no-op cap enforced inside `.github/workflows/implement.yml`'s "Handle no-op implementation" step. When a commit produces zero changes, the step walks up `Re-issued from #N` markers up to this many hops and counts how many ancestors posted the `produced no repository changes` warning comment. At or above the threshold the issue is closed with `ai:closed` and the wave-completion judge is deferred to, rather than labeling `ai:implementation-failed` and letting the poller spawn another re-issue. Must be a positive integer; invalid values fall back to `2`. Complements — does not replace — the poller-side `MAX_IMPL_NOOP_REISSUES` cap. |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | No | `1` | implement | Maximum in-job post-Codex syntax-repair attempts after `Validate syntax of changed files` fails. Must be a non-negative integer (`0` disables in-job repair); invalid values fallback to `1`. |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | No | `900` | orchestrate_poll | Minimum seconds between consecutive review/autofix dispatches against the same orchestrator integration-branch final PR. Prevents the self-healing loop from re-dispatching the resolver every poll tick while a previous run is still in flight. |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | No | `3` | orchestrate_poll | Circuit-breaker budget for automated integration-branch conflict resolution. The self-healing path attempts the `main -> integration_branch` sync via GitHub's merges API; on an HTTP 409 conflict, the poller dispatches `_dispatch_review_for_conflicts` for the final integration PR. After this many consecutive unresolved ticks, the orchestrator escalates to the judge with full PR context; if the judge escalation itself fails the project is marked terminally failed. Applies to **non**-`orchestrator/project-*` integration branches; sync conflicts on orchestrator-owned integration branches use the tighter `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` instead. |
| `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` | No | `1` | orchestrate_poll | Tighter circuit-breaker budget applied **only** when the integration branch head ref matches `orchestrator/project-*`. The first-line conflict resolver (`prompts/conflict-resolver.txt`) lacks built-in awareness of merged sub-issue intent, so the safest default is to escalate to the integration judge after a single resolver shot rather than burn three dispatches that may "succeed" textually while silently dropping a merged sub-issue's work. Set to a higher value to give the resolver more attempts; set to `0` to skip the first-line resolver entirely and escalate to the judge immediately. Non-orchestrator integration branches continue to honour `INTEGRATION_CONFLICT_MAX_RETRIES`. See "Integration-sync intent fingerprints" below. |
| `FINGERPRINT_PER_FILE_CAP` | No | `12` | orchestrate_poll | Maximum number of `must_contain` / `must_not_contain` regex patterns the orchestrator captures per file per direction when a sub-issue PR merges into an integration branch. Higher values give the integration-sync conflict verifier finer-grained intent coverage at the cost of larger state-comment payloads and longer verification runs. |
| `FINGERPRINT_MIN_PATTERN_CHARS` | No | `12` | orchestrate_poll | Minimum trimmed-line length for a fingerprint pattern. Lines shorter than this are skipped during capture (too generic to fingerprint reliably). |
| `REVIEW_BLOCKED_AUTO_UNSTICK` | No | `true` | orchestrate_poll | Before invoking the review-blocked judge, the poller inspects each `ai:review-blocked` PR. If the PR is `mergeable=false` it dispatches `review_autofix.yml` (via `_dispatch_review_for_conflicts`) so the in-workflow Codex resolver gets a fresh shot at the conflict, and skips the judge for this tick. If the PR head commit was authored by an **external** identity (anything other than `codex`, `codex-bot`, `github-actions`, or `github-actions[bot]`), the poller also dispatches the review workflow AND clears `ai:review-blocked`, re-entering the normal phase loop — this bridges the GitHub platform rule that suppresses `pull_request.synchronize` events on commits pushed with the default `GITHUB_TOKEN` (Claude Code on the web, custom wrapper actions) and matches the "push a new commit to re-trigger the review workflow" contract printed in the workflow-failure comment. Set to `false` to disable both paths and force the judge-first flow. Dispatch is always gated by the existing `_dispatch_review_for_conflicts` cycle-local dedup and active-run detection, so repeat calls are cheap no-ops. |
| `TG_ADMIN_CHAT_ID` | No | — | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Telegram chat ID for notifications (pair with `TG_BOT_SECRET`) |
| `ALERT_MSG_LEVEL` | No | `DEBUG` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, issue_pr_status, update_workflows, test-and-mark-stable | Minimum Telegram alert level to send. Alerts below this threshold are suppressed. Valid values: `DEBUG`, `WARNING`, `ERROR`, `CRITICAL`. Each alert is prefixed with an icon and level (e.g. `🔍 DEBUG:`, `⚠️ WARNING:`, `❌ ERROR:`, `🚨 CRITICAL:`). New alerts default to `CRITICAL` until explicitly recategorised. |
| `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` | No | `3600` | all (any workflow or script that sources `scripts/gh_helpers.sh`) | Minimum seconds between consecutive admin Telegram alerts when a GitHub API rate limit is hit. The alert (`⚠️ WARNING: GitHub API rate limit hit …`) is fired from inside the rate-limit branch of `gh_retry` / `gh_retry_to_file` / `gh_api_json_to_file` / `curl_gh_api`, and is throttled globally via a Telegram pinned message in the admin chat (marker `<!-- gh_rl_ts:EPOCH -->`). This deliberately avoids any GitHub API call for dedup state so the throttle keeps working while the GitHub API itself is the resource being limited. Fail-closed: on Telegram pin failure the sent message is rolled back so the "≤ 1 alert per window" invariant holds. Set to `0` has no suppression effect (any non-numeric or empty value is coerced to `3600`). No-op when `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID` are unset. |
| `SERENA_VERSION` | No | `main` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Version/branch of the Serena MCP server |
| `SERENA_LANGUAGES` | No | `""` (empty) | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Languages for Serena symbol analysis |
| `SERENA_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Disable the Serena MCP server |
| `CONTEXT7_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate | Disable the optional Context7 MCP server |
| `GIT_MCP_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate | Disable the optional Git MCP server setup (preloaded diff artifacts remain the fallback). |
| `OPENROUTER_PROMPT_CACHE_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, workflow-log-analysis | Kill switch for OpenRouter prompt-cache instrumentation. `false` enables cache-friendly prompt ordering and cache telemetry logging; `true` disables explicit cache breakpoints and related instrumentation. |
| `WORKFLOW_ORCHESTRATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | orchestrate, orchestrate_poll | Model override for orchestrator decomposer and judge |
| `ORCHESTRATE_POLL_INTERVAL` | No | `30` | orchestrate | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_SHORTCIRCUIT_MAX_CHARS` | No | `1200` | _(deprecated — no longer consumed)_ | Formerly controlled the pre-LLM short-circuit. Removed in #1163; every orchestrator run now goes through full decomposition. |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | No | `ai-orchestrate-poll.yml` | orchestrate_poll | Filename of the caller wrapper workflow to retrigger for continuous polling. The poller dispatches this workflow via `workflow_dispatch` at the end of each run when active tracking issues exist, so the next cycle starts immediately instead of waiting for the cron schedule. Self-retrigger is suppressed when a GitHub API rate limit was hit during the run (circuit breaker). Set to empty string to disable self-retrigger entirely. |
| `EDITOR_IDLE_TIMEOUT` | No | `1200` | review_autofix, implement | Editor watchdog idle timeout in seconds. The editor is killed if it produces no output for this long and has no active network connections. |
| `EDITOR_MAX_WALL` | No | `3300` | review_autofix, implement | Maximum wall-clock seconds per editor attempt. Budget-aware: auto-capped to remaining job time minus a 2-min buffer. |
| `EDITOR_MIN_ATTEMPT_SECS` | No | `300` | review_autofix | Minimum remaining job budget (seconds) required to start an editor attempt. Prevents futile retries near the job deadline. |
| `REVIEW_PR_STATE_POLL_INTERVAL_SECS` | No | `10` | review_autofix | Sleep interval in seconds for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; the GitHub PR-state API check runs every 9 polls (default ~90s). Must be an integer in `10..3600`; invalid or out-of-range values emit `rate_limit_audit_fallback` warning and fail open to `10`. |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | No | `1` | implement | Maximum in-job post-Codex syntax-repair attempts after `Validate syntax of changed files` fails. Must be a non-negative integer (`0` disables in-job repair); invalid values fallback to `1`. The repair loop runs only for syntax-validator failures, enforces an allow-list scope guard, and then falls back to the existing diagnose/fix-up path when attempts are exhausted. |
| `BULK_DELETE_THRESHOLD` | No | `3` | implement | Maximum number of file deletions allowed in a single AI implementation commit before the destructive-commit guard blocks it. Set higher for legitimate large refactors, or bypass on a per-run basis via `ALLOW_BULK_DELETE=true`. See "Destructive-commit guard" below. |
| `ALLOW_BULK_DELETE` | No | `false` | implement | When `true`, the destructive-commit guard ignores the `BULK_DELETE_THRESHOLD` rejection path. Canonical workflow-source file deletions are still blocked unless `ALLOW_WORKFLOW_EDITS=true`. Use for legitimate large refactors approved by a human. |
| `BATCH_API_DISABLED` | No | `false` | workflow-log-analysis, memory_maintenance | Kill switch for async batch mode. When `true`, workflow log analysis always uses synchronous inference. Memory maintenance emits compatibility/no-op batch logs only. |
| `BATCH_API_PROVIDER` | No | `auto` | workflow-log-analysis, memory_maintenance | Batch provider routing hint for OpenRouter Responses API capability checks/submission (`auto`, `openai`, `anthropic`). Unsupported hints fall back to sync with structured warnings. |
| `BATCH_API_POLL_TIMEOUT_HOURS` | No | `24` | workflow-log-analysis, memory_maintenance | Maximum pending batch age before workflow-log-analysis falls back to synchronous generation. |

**Thinking levels** — control the model's reasoning effort per phase. Valid values: `xhigh`, `high`, `medium`, `low`. All phases default to `xhigh` (maximum reasoning depth). No cycle-based downgrades are applied — every phase uses the configured reasoning effort for all cycles. **E2E smoke test exception:** when an issue or PR title contains `[E2E Smoke Test]`, all phases force `low` reasoning to keep smoke runs cheap and fast.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `THINKING_LEVEL_CLARIFY` | `xhigh` | clarify | Reasoning effort for the clarification phase |
| `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` | `xhigh` | clarify | Reasoning effort used only when clarify runs Codex for `ai:orchestrator-managed` issues on forced human `/reclarify` |
| `THINKING_LEVEL_PLAN` | `xhigh` | plan | Reasoning effort for the planning phase |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | implement | Reasoning effort for the implementation phase |
| `THINKING_LEVEL_ANALYSIS` | `xhigh` | workflow-log-analysis | Reasoning effort for the workflow log analysis report generation. |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | review_autofix | Reasoning effort for the reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `xhigh` | review_autofix | Reasoning effort for the editor model (applying fixes) |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `xhigh` | review_autofix | Reasoning effort for the review-blocked judge (non-orchestrator PRs) |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | orchestrate | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | orchestrate_poll | Reasoning effort for judge evaluation |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `xhigh` | orchestrate_clarify_respond | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `xhigh` | validate | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `xhigh` | orchestrate_poll | Reasoning effort for the orchestrator's Codex-based merge conflict resolver |
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

<a id="autofix-retrigger-dedup"></a>
> **Autofix retrigger dedup** — After a successful autofix or merge-resolve
> commit, `review_autofix.yml` pushes to the PR branch and then fires a
> `workflow_dispatch` retrigger (for the case where the `pull_request.synchronize`
> event's merge ref is unbuildable). Both entry points land in the same
> `pr-autofix-${PR}` concurrency group with `cancel-in-progress: false`, so new
> runs queue behind the running peer. GitHub keeps only the most recent pending
> run in the group (older pending runs are cancelled), but that newest queued
> run still appears in the Actions UI and consumes a `workflow_dispatch` API
> call. To avoid this waste, the retrigger steps now wait
> `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` (default `8`) for the synchronize-event
> run to materialize, then query the Actions API for in-flight peers on the
> same head branch (matching workflow file paths `review_autofix.yml`,
> `internal-review.yml`, or `ai-review.yml`) via
> `autofix_retrigger_has_inflight_peer` in `scripts/gh_helpers.sh`. If a peer
> is found the retrigger emits `AUTOFIX_DISPATCH_SKIPPED reason=...
> pr=<n> current_run=<r>` and exits without dispatching. The probe fails open
> (one `GET /repos/{repo}/actions/runs` call per retrigger, wrapped in
> `gh_retry`): any API error falls through to the original unconditional
> dispatch so the cycle is never silently broken. Look for
> `AUTOFIX_PEER_CHECK` / `AUTOFIX_DISPATCH_SKIPPED` / `AUTOFIX_DISPATCH_ISSUED`
> lines when auditing collision behaviour in Actions logs.
>
> **Self-triggered autofix skip** — Peer-dedup only collapses parallel runs; it
> does not stop the serial "every autofix commit re-runs the full reviewer/editor
> cycle" pattern that otherwise doubles LLM spend per fix round (seen as a
> `not-edited` comment immediately followed by an `edited` comment on every
> iteration). When `AUTOFIX_SKIP_SELF_TRIGGERED` is left at its default (`true`),
> the `gate` job in `review_autofix.yml` inspects the HEAD commit on
> `pull_request.synchronize` events via `GET /repos/{repo}/commits/{sha}` and
> sets `should_run=false` when the subject begins with `[ai-autofix]` *and*
> at least one of `.author.login` / `.committer.login` equals
> `AUTOFIX_BOT_LOGIN` (default `codex`). These `login` fields are
> GitHub-attributed — resolved server-side from the push credentials — so they
> cannot be set by a user crafting a local commit with a spoofed
> `git config user.email`. The gate emits
> `AUTOFIX_GATE_SKIP reason=self_triggered_autofix pr=<n> head_sha=<sha>
> head_prefix=[ai-autofix] author_login=<login> committer_login=<login>
> bot_login=<expected>` for the skip decision,
> `AUTOFIX_GATE_NO_SKIP_IDENTITY ...` when the subject matches but neither
> login does (a third-party commit with the `[ai-autofix]` prefix — runs
> normally), or
> `AUTOFIX_GATE_SKIP_QUERY_FAILED pr=<n> head_sha=<sha> reason=api_error` and
> falls open (runs the full cycle) if the commit lookup fails. The post-commit
> `workflow_dispatch` retrigger step mirrors the guard and emits
> `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix pr=<n>
> current_run=<r> source=post_commit_retrigger` only when `DID_COMMIT=true`
> AND `CONFLICT_RESOLVED!=true`; that path relies on local workflow state
> (the step just executed the push), not commit identity, so no API
> identity check is required there. `[ai-merge-resolve]` commits still fire a
> follow-up verification pass for post-conflict-resolution safety.
> `workflow_dispatch`, `opened`, `reopened`, and `ready_for_review`
> events are never skipped, and the orchestrator stall cron
> (`internal-orchestrate-poll.yml`, `*/30 * * * *`) re-dispatches via
> `workflow_dispatch` — which bypasses the skip — so the worst-case delay
> between a missed verification and automatic recovery is ~30 min. Log
> prefixes `AUTOFIX_GATE_SKIP`, `AUTOFIX_GATE_NO_SKIP_IDENTITY`,
> `AUTOFIX_GATE_SKIP_QUERY_FAILED`, and
> `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix` are stable audit
> handles. Set `vars.AUTOFIX_SKIP_SELF_TRIGGERED=false` to opt out, or set
> `vars.AUTOFIX_BOT_LOGIN` to override the expected bot login.
>
> **Mid-run external-push gates** — The self-triggered skip above catches
> *post-run* autofix events. The companion *mid-run* gates (`agents.md §20.2`)
> catch the case where a non-autofix push lands on the PR branch **while** the
> reviewer/editor cycle is mid-flight (~15-30 min). Two evaluation points in
> `jobs.codex-agent`, both backed by `scripts/check_external_branch_advance.sh`:
> the **pre-editor gate** runs between reviewer consensus and the editor
> invocation and skips the expensive editor call when the branch advanced with
> an external commit; the **pre-push gate** runs at the top of the
> "Push all pending commits" step and soft-exits before the push if the branch
> advanced during the editor run. Both set `AUTOFIX_STALE_BASE_SKIP=true`
> (env) which every downstream editor/commit/push/clean-review step gates on.
> The synchronize event from the advancing push drives a fresh cycle. Log
> prefixes `AUTOFIX_PRE_EDITOR_STALE_BASE`, `AUTOFIX_PRE_EDITOR_SELF_ADVANCE`,
> `AUTOFIX_PRE_EDITOR_BASE_FRESH`, `AUTOFIX_PRE_EDITOR_UNKNOWN` and the
> corresponding `AUTOFIX_PRE_PUSH_*` variants are stable audit handles.
>
> **Ledger-only commit auto-merge** — `scripts/review_issue_ledger.sh`
> updates `REVIEW_LEDGER_PATH` (default `.ai/review_issue_ledger.txt`) on
> every review pass, including passes where the editor reports
> `Change status: not-edited`. This ledger-only commit scenario applies
> only when `REVIEW_LEDGER_PATH` is Git-tracked (or explicitly
> force-added); with the default `.ai/review_issue_ledger.txt`
> runtime-artifact path in repos that leave it gitignored, the ledger is
> still updated locally for the run but is not part of the commit/push
> and this bug cannot manifest. When the resulting `[ai-autofix]` commit
> contains **only** the ledger, the `commit_changes` step sets
> `LEDGER_ONLY_COMMIT=true` (and the `ledger_only_commit` step output) in
> addition to `DID_COMMIT=true`. Five downstream gates OR this signal into
> their original `did_commit != 'true'` condition:
> `Detect editor-claimed-but-uncommitted changes`,
> `Validate editor no-op disposition`, `Mark linked issues ready to merge`,
> `Enable auto-merge on PR`, and `Telegram success`. The two safety gates
> still run to validate the editor's no-op claim, and the three
> clean-review gates fire in the same run — required because any
> `[ai-autofix]` ledger push in tracked-ledger configurations triggers a
> `synchronize` event whose gate job is skipped by
> `AUTOFIX_SKIP_SELF_TRIGGERED`, so auto-merge cannot be deferred to the
> next run. In tracked-ledger configurations the push step
> (`DID_COMMIT=true`) still fires so the ledger lands on the PR branch
> and cross-iteration ledger continuity is preserved there as well.

### Local replay helper for review artifacts

Use `scripts/dev/replay_review_pipeline.sh` to replay the review artifact chain locally without dispatching any GitHub workflow.

- Stage order is fixed to `review_floor_rules.sh` -> `review_consolidate.sh` -> `review_parse_consolidator.sh` -> `review_issue_ledger.sh`.
- The helper is non-invasive: it only runs local scripts against a provided runtime bundle and prints an artifact summary.
- `--runtime-dir` is required and must contain `reviewer_bundle.txt`.

Examples:

```bash
scripts/dev/replay_review_pipeline.sh --runtime-dir /tmp/review-runtime
scripts/dev/replay_review_pipeline.sh --runtime-dir /tmp/review-runtime --disable-consolidator
```

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
>
> **Note on `@main` vs `@stable` inside this repo.** The `internal-*.yml`
> wrappers here pin `uses:` to
> `shubhodeep1/coding-workflows/.github/workflows/<wf>.yml@main` rather than
> `@stable` (consumer templates in [`workflow-templates/`](workflow-templates/)
> keep `@stable`). This split is intentional:
>
> 1. **Branch-drift immunity.** When the orchestrator opens a feature PR, any
>    `pull_request`-triggered wrapper (`internal-review.yml`,
>    `internal-cancel-on-pr-close.yml`, `internal-issue-pr-status.yml`) runs
>    from the PR branch's copy of the wrapper file. Pinning to `@main` makes
>    GitHub fetch the reusable workflow body from `main` on this repo,
>    bypassing whatever potentially stale copy the feature branch carries.
>    This is the fix for orchestrator runs that used to stall because the
>    feature branch carried an outdated reusable workflow and was hard to
>    update mid-run.
> 2. **Fast recovery.** If a bad reusable workflow lands on `main`, pushing a
>    fix to `main` takes effect on the next wrapper invocation immediately —
>    no need to re-run the full `test-and-mark-stable.yml` gate first. The
>    trade-off is that a broken merge to `main` immediately breaks in-flight
>    orchestrator runs, which is accepted as the cost of fast recovery in
>    the source-of-truth repo.
> 3. **`test-and-mark-stable.yml` still validates main HEAD.** The E2E smoke
>    test job in [`test-and-mark-stable.yml`](.github/workflows/test-and-mark-stable.yml)
>    creates a real issue on this repo, and the `issues:[opened]` event fires
>    the default-branch wrapper (`internal-clarify.yml@main`), which then
>    fetches `clarify.yml@main` — i.e. the candidate code about to be tagged
>    stable. So the release gate continues to exercise main HEAD rather than
>    the already-stable tag.
> 4. **Consumer repos are unaffected.** Consumer repos install the
>    `workflow-templates/ai-*.yml` copies pinned `@stable` and get the
>    conservative, release-gated channel.
>
> **Dogfood lint gate.** Because `@main` wrappers run whatever is on `main`,
> a bad merge can cascade. To catch YAML/schema regressions on PRs before
> they land on `main`, [`ci.yml`](.github/workflows/ci.yml) runs `yamllint`
> and `actionlint` over every file in `.github/workflows/` **and**
> `workflow-templates/` on `pull_request` against `main`. Broken reusable
> workflow bodies or template schemas fail CI before merge.
>
> **Recovery procedure for a broken `main` reusable.** Push a fix directly
> to `main` (or merge a hotfix PR). The next triggered wrapper run picks
> up the fix immediately — no `test-and-mark-stable.yml` re-run required.
> Run `test-and-mark-stable.yml` separately when you are ready to promote
> the fix to the `@stable` channel for consumer repos.

### 3. Open an issue

Create a new issue describing a feature or bug fix. The pipeline kicks off automatically:

1. **Clarify** evaluates whether the issue has enough detail. If not, it comments with clarification questions. If required input is external and non-synthesizable (for example branch/SHA/credential/external URL), it emits a `BLOCKED: <reason>` handoff that labels the issue `ai:blocked` and pauses auto-answer loops until a human supplies the missing input.
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
| `orchestrate_poll.yml` | `schedule` (every ~30 min) + self-retrigger | Orchestrator progress poller + judge + auto-recovery. Self-retriggers via `workflow_dispatch` when active tracking issues exist for near-immediate next cycles; cron acts as fallback. A rate-limit circuit breaker suppresses self-retrigger when a GitHub API rate limit was hit during the run. |
| `update_workflows.yml` | `schedule` (daily), `repository_dispatch`, `workflow_dispatch` | Auto-updates existing and creates new workflow wrappers from upstream templates |

## Workflow Log Analysis And Improvement

This repository includes [`.github/workflows/comprehensive-test-and-release.yml`](.github/workflows/comprehensive-test-and-release.yml) for chaining workflow-log analysis into an orchestrator-driven improvement follow-up. Release dispatch (`test-and-mark-stable.yml`) is no longer invoked from this workflow; it remains available as a standalone workflow for marking stable releases.

### How to run

Run **Actions -> Workflow Log Analysis And Improvement -> Run workflow**.

`workflow_dispatch` inputs:

| Input | Default | Description |
|---|---|---|
| `phase_timeout` | `30` | Per-phase inactivity timeout (minutes) for dispatch-monitor loops. |
| `lookback_days_fallback` | `7` | Workflow-log-analysis window used when the saved timestamp cursor is missing or invalid. |

### Phase behavior

1. **Phase 2 (`phase2-collect-and-analyze-logs`)** dispatches `workflow-log-analysis.yml` with `codex_mode=true`, waits for completion, and resolves the analysis window from `analysis/last_collection_timestamp.txt`:
   - if the file contains a valid UTC ISO timestamp (`YYYY-MM-DDTHH:MM:SSZ`), that value is passed as `since`.
   - otherwise the workflow falls back to `lookback_days_fallback`.
   - after a successful run, the workflow writes the current UTC timestamp back to `analysis/last_collection_timestamp.txt` and commits/pushes it when changed.
2. **Phase 3 (`phase3-dispatch-orchestrator`)** dispatches `internal-orchestrate.yml` with a project description that links to the analysis report, then waits for the orchestrator to open a tracking issue and emits the tracking issue number as a job output.

Job identifiers retain their `phase2-*` / `phase3-*` names for backward compatibility with any external references; there is no `phase1-*` job in this workflow.

## Workflow Log Analysis

This repository includes [`.github/workflows/workflow-log-analysis.yml`](.github/workflows/workflow-log-analysis.yml) to collect AI workflow telemetry and generate a markdown optimization report.

### How to run

Run **Actions -> Workflow Log Analysis -> Run workflow**.

Triggers:

- `workflow_dispatch` (manual).

`workflow_dispatch` inputs:

| Input | Default | Description |
|---|---|---|
| `since` | `""` | Optional ISO-8601 timestamp. When set, collector runs with `scripts/collect_workflow_logs.py --since <timestamp>`. |
| `lookback_days` | `"7"` | Days of workflow runs to collect when `since` is empty. Passed to `scripts/collect_workflow_logs.py --lookback-days`. |
| `codex_mode` | `true` | Codex-first analysis mode. When `true`, workflow runs analyzer preprocessing (`--codex-mode`) and then `codex exec` in the same run. When `false`, workflow uses the legacy analyzer inference/batch path (including deferred polling via batch-state artifact). |
| `batch_api_disabled` | `""` | Optional `true`/`false` override for analyzer batch API behavior in non-codex mode only. Non-empty values are validated in all runs; invalid values fail the workflow before mode branching. Empty keeps `BATCH_API_DISABLED` env default. |
| `repos_override` | `""` | Optional comma-separated `owner/repo` list. Each item is validated with `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`; invalid values fail the run. |
| `tracking_issue` | `"0"` | Optional tracking issue number used for terminal Codex/analyzer failure labeling (`ai:log-analysis-failed`) and `AI_PHASE_FAILURE_V1` marker comments. `0`/empty keeps fail-open warning behavior without issue mutation. |

Mode behavior:

- `codex_mode=true` (default): analyzer writes `analysis_context.json` (`--codex-mode`) and Codex generates the markdown report directly. Codex attempts are bounded by `MAX_CODEX_ATTEMPTS` (default `3`) with exponential backoff base `CODEX_RETRY_BACKOFF_BASE_SECS` (default `10`).
- `codex_mode=false`: workflow restores latest non-expired `workflow-log-analysis-batch-state` artifact when available, runs analyzer in legacy mode, and may return pending (`exit 3`) until a later manual rerun completes polling.
- `batch_api_disabled` input is validated whenever a non-empty value is provided, but only affects analyzer behavior in `codex_mode=false` runs.
- Terminal Codex/analyzer failures are issue-context aware: when `tracking_issue` is set to a positive integer the workflow emits `AI_PHASE_FAILURE_V1` and applies `ai:log-analysis-failed`; otherwise it emits a fail-open warning and exits without issue mutation.

Repository selection behavior:

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
  - `--log-output-dir` (optional categorized full-log export directory)
  - `--per-page` (default `100`)
  - `--max-pages` (default `10`)
  - `--max-runs` (default `0`)
  - `--max-log-runs` (default `15`)
  - `--success-sample-rate` (default `0.07` = ~7%) — fraction of successful runs randomly sampled for log analysis
- Token handling in `main`: uses `GH_TOKEN` with `GITHUB_TOKEN` fallback.
- All workflow families are collected (no family filter). The `workflow_families` field in the report is derived from observed runs rather than a static list.
- Workflow family normalization covers pipeline families (`clarify`, `plan`, `implement`, `review_autofix`, `validate`, `orchestrate`, `orchestrate_poll`, `orchestrate_clarify_respond`, `issue_pr_status`, `cancel_on_pr_close`, `memory_maintenance`) and keeps fallback buckets (for example `ci`, `workflow_log_analysis`, and sanitized filename-derived families) so non-pipeline runs remain observable.
- For notable runs (failed, retries > 0, top 10 slowest per repository, and ~7% randomly sampled successful runs), the collector also downloads raw run logs from `repos/{repo}/actions/runs/{run_id}/logs`, extracts ZIP contents in memory, and stores truncated per-step excerpts. Random sampling uses a deterministic seed derived from the collection window for reproducibility.

When `--log-output-dir` is set, collector additionally writes:

- `<log-output-dir>/summary.json` (same schema payload as `--output`)
- `<log-output-dir>/errors/<repo_slug>/<family>/<run_id>/metadata.json` and full `step-*.log`
- `<log-output-dir>/slow/<repo_slug>/<family>/<run_id>/metadata.json` and full `step-*.log`
- `<log-output-dir>/recent/<repo_slug>/<family>/<run_id>/metadata.json` and full `step-*.log`

Full-log downloads for disk export are restricted to the selected `errors`/`slow`/`recent` runs and deduplicated per `(repository, run_id)` across overlapping categories.

Generated JSON report (`workflow_log_report.json`) includes:

- `schema_version` (`workflow_log_collector.v2`)
- `generated_at`
- `scope` (`repositories`, `workflow_families` (observed, not static), `source`, `success_sample_rate`)
- `runs` (per-run metrics including `workflow_family`, `duration_seconds`, `retries`, `failure_point`, optional `log_excerpts` as `{step_name, excerpt}` entries for notable runs, optional `_success_sampled: true` flag for randomly sampled successful runs)
- `summary` (`total_runs`, success/failure/cancelled/other counts, `avg_duration_seconds`, `p50_duration_seconds`, `p95_duration_seconds`, `sampled_success_runs`)
- `errors` (includes `scope: "logs"` entries when run log download/extraction fails; collection continues)

### Analyzer input/output contract

Analyzer script: [`scripts/analyze_workflow_logs.py`](scripts/analyze_workflow_logs.py)

- Codex-first workflow path (`codex_mode=true`):
  1. `python3 scripts/analyze_workflow_logs.py --input workflow_log_report.json --output <report.md> --codex-mode` (writes `analysis_context.json` and prints its path)
  2. `codex exec --model <WORKFLOW_EDITOR_MODEL> --full-auto` with `prompts/mode-workflow-analysis.txt` + generated analysis context, writing the final markdown report file.
- Legacy workflow path (`codex_mode=false`): `python3 scripts/analyze_workflow_logs.py --input workflow_log_report.json --batch-state-file workflow_log_analysis_batch_state.json`
- `--max-output-tokens` default is `100000`. The workflow auto-caps this to `60000` when the resolved `WORKFLOW_EDITOR_MODEL` contains `gemini` (Gemini 3.1 Pro Preview's max output is 65536).
- Model resolution for this workflow only: the `Run workflow log analysis` step defaults `WORKFLOW_EDITOR_MODEL` to `openai/gpt-5.3-codex` and allows override via repo variable `WORKFLOW_LOG_ANALYSIS_MODEL`. This override is scoped to this workflow and does not affect the global `WORKFLOW_EDITOR_MODEL` used by `clarify`/`plan`/`implement`/`review_autofix`/`validate`/`orchestrate`.
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
- Deferred artifact contract (non-codex mode): pending batch metadata is uploaded as artifact `workflow-log-analysis-batch-state` containing `workflow_log_analysis_batch_state.json`; later manual dispatch runs fetch latest non-expired artifact and continue polling.
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

<!-- anchor:required-variables-table -->
<!-- Parallel orchestrator sub-issues: append new env vars to the BOTTOM
     of this table directly under this anchor. Do NOT reorder existing
     rows or reflow the table — parallel sub-issues inserting rows in
     the middle is a classic merge-conflict generator. -->
| Variable | Default | Description |
|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` | Model for code editing tasks |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `true` | Allow AI edits to workflow files and automatic wrapper updates |
| `ENABLE_AUTO_MERGE` | `true` | Auto-merge PRs (squash) when review passes and checks are green |
| `MAX_AUTOFIX_ITERATIONS` | `3` | Maximum consecutive autofix rounds before marking `ai:review-blocked` |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | `true` | Enable review-blocked judge for non-orchestrator PRs |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `xhigh` | Reasoning effort for review-blocked judge |
| `MAX_REVIEW_BLOCKED_RETRIES` | `2` | Maximum judge retries for review-blocked PRs (both review_autofix and orchestrate_poll) |
| `ENABLE_VALIDATION` | `true` | Enable post-judge runtime validation gate in orchestrator poller |
| `MAX_VALIDATE_CYCLES` | `3` | Maximum runtime validation cycles before terminal validation failure |
| `MAX_SELF_HEAL_ATTEMPTS` | `2` | Maximum in-process self-heal attempts per `validate_process.sh` invocation (self-heal re-execs do not burn cycles) |
| `ENABLE_CLEAN_WAVE_JUDGE_SKIP` | `true` | Skip judge on clean completed waves (no failures) and on clean project completions; advance mechanically |
| `ORCHESTRATOR_MAX_CLARIFY_CYCLES` | `3` | Maximum orchestrator clarify auto-answer cycles before the auto-answer loop is halted and the issue is escalated to `ai:blocked` for human input |
| `STALL_THRESHOLD_MINUTES` | `120` | Fallback minutes before a stalled issue triggers auto-recovery |
| `STALL_THRESHOLD_NO_LABELS_MINUTES` | `60` | Stall threshold for pre-pipeline (no labels) phase |
| `STALL_THRESHOLD_CLARIFICATION_MINUTES` | `60` | Stall threshold for clarification phase |
| `STALL_THRESHOLD_PLANNING_MINUTES` | `60` | Stall threshold for planning phase |
| `STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES` | `60` | Stall threshold for plan approval phase |
| `STALL_THRESHOLD_IMPLEMENTING_MINUTES` | `120` | Stall threshold for implementation phase |
| `STALL_THRESHOLD_DONE_MINUTES` | `120` | Stall threshold for review/autofix phase |
| `STALL_THRESHOLD_READY_TO_MERGE_MINUTES` | `60` | Stall threshold for ready-to-merge phase |
| `MAX_STALL_RECOVERIES_PER_ISSUE` | `5` | Max stall recovery attempts per issue before skipping (declarative `STALL_RECOVERY_ACTIONS` + optional `run_stall_judge` escalation) |
| `STALL_JUDGE_TRIGGER_COUNT` | `2` | Recovery-attempt threshold to invoke stall judge escalation (`run_stall_judge`) |
| `ENABLE_STALL_JUDGE` | `true` | Enable/disable stall-judge escalation in orchestrator and standalone stall recovery |
| `ENABLE_STALL_HUMAN_TERMINALIZATION` | `false` | Allow terminal `escalate_human` stall actions; when `false`, both declarative and judged stall actions downgrade `escalate_human` to the nearest prior non-human phase action |
| `ENABLE_STANDALONE_STALL_RECOVERY` | `true` | Enable standalone AI issue stall recovery in the poller |
| `ENABLE_STALL_MERGED_PR_GUARD` | `true` | Double-check the issue's linked PR state before firing early-phase stall recovery commands; if the PR is merged, tag `ai:merged` and skip instead of posting `/reclarify` (etc). Batched GraphQL prefetch — 0 extra per-issue calls on successful prefetch; cache misses may fall back to a per-issue REST lookup. |
| `MAX_RECOVERY_ATTEMPTS` | `3` | Max project-level recovery cycles (judge failure → auto-fix) |
| `MAX_VALIDATION_RECOVERY_ATTEMPTS` | `2` | Max validation-failure → judge re-evaluation cycles before terminal failure |
| `MAX_VALIDATION_FIX_BATCH_CYCLES` | `30` | Max poll cycles a single validation fix-up batch can sit "in progress" before the poller escalates through `mark_validation_failed` |
| `MAX_IMPL_NOOP_REISSUES` | `2` | Max automatic re-issues for `ai:implementation-failed` before closing as likely already implemented and deferring to judge verification. Enforced by both the state-based counter and the issue-local `count_noop_ancestors` walk of the `Re-issued from #N` chain (belt-and-braces); either signal trips closure |
| `IMPL_NOOP_ANCESTRY_THRESHOLD` | `2` | Ancestor-chain no-op cap enforced in `.github/workflows/implement.yml`'s "Handle no-op implementation" step; closes the issue with `ai:closed` when the `Re-issued from #N` chain already has this many no-op ancestors, rather than labeling `ai:implementation-failed` for another poller re-issue |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | `1` | Max in-job post-Codex syntax-repair attempts in `implement`; must be non-negative integer (`0` disables repair; invalid values fallback to `1`) |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | `900` | Min seconds between consecutive resolver dispatches against an integration-branch final PR |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | `3` | Max consecutive unresolved conflict ticks before judge escalation, after `_dispatch_review_for_conflicts` healing attempts (non-orchestrator integration branches) |
| `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` | `1` | Tighter ticks-before-judge budget applied only when head ref matches `orchestrator/project-*` (integration-sync conflicts) |
| `FINGERPRINT_PER_FILE_CAP` | `12` | Cap on `must_contain`/`must_not_contain` patterns captured per file per merged sub-issue |
| `FINGERPRINT_MIN_PATTERN_CHARS` | `12` | Minimum trimmed-line length for a captured fingerprint pattern |
| `ACTIONS_RUNS_CACHE_TTL_SECONDS` | `60` | Cross-tick cache TTL (seconds) for `GET /actions/runs` snapshots persisted on the `ai-memory` branch and reused by orchestrator poll run-state readers |
| `CONTEXT7_DISABLED` | `false` | Disable the optional Context7 MCP server setup in workflows |
| `GIT_MCP_DISABLED` | `false` | Disable the optional Git MCP server setup in workflows (preloaded diff artifacts remain fallback) |
| `AI_MEMORY_BRANCH` | `ai-memory` | Branch used for persistent AI memory |
| `AI_MEMORY_ROOT` | `ai-memory` | Memory root path used by workflows |
| `AI_MEMORY_RETRIEVAL_PROFILES` | `ai-memory/config/retrieval_profiles.v1.json` | Retrieval role config |
| `AI_MEMORY_ENABLED` | `true` | Enable/disable memory operations |
| `AI_MEMORY_KEYWORD_MODEL` | `openai/gpt-5-mini` | Model for semantic keyword extraction during retrieval |
| `AI_MEMORY_KEYWORD_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL for keyword model |
| `AI_MEMORY_TOKEN_BUDGET_<ROLE>` | _(from profile)_ | Per-role token budget override (e.g. `AI_MEMORY_TOKEN_BUDGET_IMPLEMENTATION=3200`) |
| `THINKING_LEVEL_CLARIFY` | `xhigh` | Reasoning effort for clarification (`xhigh`, `high`, `medium`, `low`) |
| `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` | `xhigh` | Clarify-only override for forced human `/reclarify` on `ai:orchestrator-managed` issues (normal clarify path auto-posts `/answer [auto-answered-by-orchestrator]` without Codex) |
| `THINKING_LEVEL_PLAN` | `xhigh` | Reasoning effort for planning |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | Reasoning effort for implementation |
| `THINKING_LEVEL_ANALYSIS` | `xhigh` | Reasoning effort for workflow log analysis report generation |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | Reasoning effort for reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `xhigh` | Reasoning effort for editor model (applying fixes) |
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | Tool call budget for clarification |
| `TOOL_CALL_BUDGET_PLAN` | `40` | Tool call budget for planning |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | Tool call budget for implementation |
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | Token warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | Token warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | Token warning threshold for implementation |
| `WORKFLOW_ORCHESTRATE_MODEL` | (falls back to `WORKFLOW_EDITOR_MODEL`) | Model override for orchestrator/judge |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | Reasoning effort for judge evaluation |
| `ORCHESTRATE_POLL_INTERVAL` | `30` | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_SHORTCIRCUIT_MAX_CHARS` | `1200` | _(deprecated — no longer consumed; short-circuit paths removed in #1163)_ |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | `ai-orchestrate-poll.yml` | Caller workflow filename for self-retrigger; empty string disables |
| `EDITOR_IDLE_TIMEOUT` | `1200` | Editor watchdog idle timeout (seconds); killed if no output and no active network connections |
| `EDITOR_MAX_WALL` | `3300` | Max wall-clock seconds per editor attempt; auto-capped to remaining job budget |
| `EDITOR_MIN_ATTEMPT_SECS` | `300` | Minimum job budget (seconds) required to start an editor attempt |
| `REVIEW_PR_STATE_POLL_INTERVAL_SECS` | `10` | Sleep interval (seconds) for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; GitHub PR-state API checks run every 9 polls (default ~90s); must be integer `10..3600`, else warn (`rate_limit_audit_fallback`) and fall back to `10` |
| `BATCH_API_DISABLED` | `false` | Kill switch for async batch mode in workflow-log-analysis (`true` forces sync fallback) |
| `BATCH_API_PROVIDER` | `auto` | Batch provider hint (`auto`, `openai`, `anthropic`) for OpenRouter responses routing checks |
| `BATCH_API_POLL_TIMEOUT_HOURS` | `24` | Maximum pending batch age before synchronous fallback |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | Tool call budget for decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | Tool call budget for judge (needs deep repo inspection) |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | Token warning threshold for orchestration |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `xhigh` | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `xhigh` | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `xhigh` | Reasoning effort for the orchestrator's Codex-based merge conflict resolver |
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
| `MAX_MERGE_DEFERRALS` | `5` | Max consecutive poll cycles a single sub-PR may be deferred by the pre-merge sibling-conflict probe (`probe_sibling_merge_conflicts` in `scripts/orchestrate_poll_process.sh`). The probe runs `git merge-tree --write-tree --name-only` locally against every other open sub-PR targeting the same integration branch before invoking `gh pr merge --squash`. When a textual conflict is detected, the candidate PR is skipped for the cycle and the deferral counter on its wave entry is incremented. Exceeding `MAX_MERGE_DEFERRALS` triggers a Telegram WARNING for human review but does not mark the PR failed — the probe is a merge-ordering nudge, not a gate. Set lower for more aggressive human escalation or higher to give auto-serialization more room. Every detected conflict also emits a telemetry event to `ai-memory/orchestrator/merge_conflicts.jsonl` on the `ai-memory` branch (git protocol only, zero GH API calls) so the next orchestrator run can auto-learn hot files without any manual seed file. |
| `ORCHESTRATOR_HOT_FILE_WINDOW_DAYS` | `90` | Lookback window for the auto-learned hot-file set computed at plan time from `ai-memory/orchestrator/merge_conflicts.jsonl`. A path is promoted to "hot" when it appears in at least `ORCHESTRATOR_HOT_FILE_MIN_EVENTS` distinct conflict events across at least `ORCHESTRATOR_HOT_FILE_MIN_PROJECTS` distinct orchestrator projects within this window. Older events drop out automatically — no persistent "demotion" state is kept. |
| `ORCHESTRATOR_HOT_FILE_MIN_EVENTS` | `3` | Minimum distinct conflict events required to promote a path to the learned hot-file set. Lower for faster reaction, higher for less noise. |
| `ORCHESTRATOR_HOT_FILE_MIN_PROJECTS` | `2` | Minimum distinct orchestrator projects required to promote a path. Prevents a single runaway project from skewing the set. |
| `REVIEW_LEDGER_ENABLED` | `1` | Enable (`1`) or disable (`0`) review-issue ledger lifecycle tracking in `scripts/review_issue_ledger.sh`; when disabled, `ledger_status.txt` is emitted empty and no ledger file is updated. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | `2` | Persist-count threshold for transitioning a still-present issue to `accepted-residual` after increment (>= threshold). |
| `REVIEW_LEDGER_PATH` | `.ai/review_issue_ledger.txt` | Runtime ledger file path used by `scripts/review_issue_ledger.sh`; malformed prior ledgers fail-open with `ledger_reset=1` and state reset semantics. |
| `MAX_CODEX_ATTEMPTS` | `3` | Shared Codex retry cap for validate/workflow-log-analysis Codex execution paths. Must be a positive integer; invalid values fail open to `3` with a warning. |
| `CODEX_RETRY_BACKOFF_BASE_SECS` | `10` | Exponential retry backoff base (seconds) used with `MAX_CODEX_ATTEMPTS` (`base * 2^(attempt-1)`) for validate/workflow-log-analysis Codex execution paths. Must be a positive integer; invalid values fail open to `10` with a warning. |
| `ENABLE_PHASE_FAILURE_COMMENTS` | `true` | Contract-defined gate for `AI_PHASE_FAILURE_V1` issue comments. Current branch status: reserved (not consumed yet); validate/workflow-log-analysis still emit marker comments when tracking issue context exists. |
| `ENABLE_LABEL_REPAIR_SWEEP` | `true` | Contract-defined gate for poller label-repair sweep. Current branch status: reserved (not consumed yet); `reconcile_managed_issue_labels` runs every poll cycle for current-wave managed issues. |
| `LABEL_REPAIR_DRY_RUN` | `false` | Contract-defined dry-run mode for label repair. Current branch status: reserved (not consumed yet); label diffs are applied live when detected. |
| `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE` | `50` | Contract-defined cap for per-cycle label-repair mutations. Current branch status: reserved (not consumed yet); effective scope is the current-wave issue set. |

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
    → Judge (LLM, full repo checkout): evaluates after each wave
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

<!-- anchor:orchestrator-pipeline-steps -->
<!-- Parallel orchestrator sub-issues: when you need to document a new
     pipeline step or behavior here, insert new prose directly under this
     anchor with an append-only `Na.` / `Nb.` suffixed bullet. Do NOT
     renumber existing steps and do NOT reflow the paragraphs below —
     multiple siblings editing this list in parallel is a known conflict
     generator, and the partition guard will serialize waves that touch
     the same anchor. See prompts/mode-orchestrate.txt. -->
1. **Decomposition:** The LLM reads your repo and breaks the project into scoped issues with a dependency graph. A tracking issue (labeled `ai:orchestrator-tracking`) and integration branch are always created, even for single-issue decompositions — this ensures every orchestrator-managed task goes through the full pipeline including post-merge validation and fixups.
2. **Wave dispatch:** Wave 1 issues (no dependencies) are created immediately and enter the existing clarify → plan → implement → review pipeline automatically. If clarification questions are raised, the `orchestrate_clarify_respond` workflow answers them automatically using an LLM. A **data-provision guard** (`scripts/clarify_data_provision_guard.py`) post-processes the LLM's answers before posting: if the selected option requires the respondent to provide concrete external data (PR URLs, commit SHAs, branch names) that the auto-responder cannot supply, the guard overrides the answer with the most conservative fallback option from the same question. This prevents circular clarification loops where the auto-responder repeatedly selects a "provide the URL" option without providing one. When `plan.yml` emits structured `Q<ID>` clarification blocks with single-letter `(RECOMMENDED)` options for every question, `plan.yml` now posts a synthesized `/answer Q1: A, ... [auto-answered-by-orchestrator]`; if parsing fails or any recommendation is non-single-letter (for example `A+C`), it does not auto-answer and keeps the human `/answer` loop.
3. **Auto-merge:** The poller automatically merges PRs via squash merge when they reach `ai:ready-to-merge`. If a PR has merge conflicts (e.g. `main` advanced since the PR was created), the poller automatically updates the PR branch via the GitHub API before retrying the merge. This requires either (a) no branch protection rules, or (b) branch protection with "Require status checks" that have already passed. See [Enabling auto-merge](#enabling-auto-merge) below.
4. **In-progress conflict resolution:** When the base branch advances and creates merge conflicts on open PRs whose tracking issue is in the `in_progress` or `done` wave status (still going through the review/autofix cycle, or sitting in `ai:done` awaiting promotion to `ai:ready-to-merge`), the poller detects the conflict (`mergeable == false`). It first tries a GitHub API branch update; if that fails (real conflicts), it dispatches the review workflow via `workflow_dispatch`. The review workflow's built-in Codex conflict resolver then handles the resolution on a dedicated runner with a clean environment.
5. **Polling:** Every 30 minutes (cron fallback), the poller checks if the current wave's issues have reached `ai:merged`. When all are merged, it runs the judge. Between cron ticks the poller self-retriggers for near-immediate cycles, unless a GitHub API rate limit was hit during the run (circuit breaker).
6. **Judge:** Full repo checkout + tool access (Serena, shell, file reads). Compares merged code against the project spec. Decides: complete, in_progress (next wave or fix-ups), or failed.
7a. **Clean-wave skip (flagged):** When `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`, and the completed wave has no failed issues, project is not complete, and it is not a stuck-wave invocation, the poller advances `current_wave` and increments `judge_cycle` without calling Codex judge. `judge_stall_cycles` is unchanged.
7b. **Clean project-completion skip (flagged):** When `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`, the final wave is complete with all issues merged, no failures, no review-blocked issues, and no stuck-wave invocation, the poller emits a synthetic `complete` verdict without calling the Codex judge. The outcome is deterministic in this case — the LLM judge cannot add value and risks empty-output failures.
8. **Next wave:** When the judge approves, the poller creates the next wave's issues (deferred creation — they don't exist until their dependencies are met). This triggers `clarify.yml` via `issues.opened`.
9. **Review-blocked resolution:** When a PR exhausts its autofix iterations (`ai:review-blocked`), the poller invokes a dedicated review-blocked judge (xhigh thinking, full PR context). The judge makes autonomous architectural and security trade-off decisions — it does not defer to humans. It can: (a) merge the PR as-is if remaining issues are cosmetic or low-risk, (b) push an `[orchestrator-fix]` commit with targeted fixes (resets the autofix counter, re-triggers review), or (c) close the PR and create a replacement issue with refined guidance. After `MAX_REVIEW_BLOCKED_RETRIES` (default 2), the judge must choose merge or close+reissue — no further fix attempts.
10. **Implementation-failed recovery:** When the implementation phase reaches the post-Codex pre-commit path with no committable file changes despite an approved plan (e.g. workflow edits stripped without `ALLOW_WORKFLOW_EDITS=true`, or model failure), `implement.yml` labels the source issue `ai:implementation-failed`. The poller automatically closes that issue and creates a replacement with additional diagnostic guidance, so the pipeline retries without manual intervention. For no-op implementation failures this behavior is unchanged; retries are bounded by `MAX_IMPL_NOOP_REISSUES`.
10a. **Post-Codex syntax repair (in-job):** If `Validate syntax of changed files` fails, `implement.yml` runs an in-job recovery loop before commit/push. The loop is capped by `MAX_POST_CODEX_REPAIR_ATTEMPTS` (default `1`; must be a non-negative integer, where `0` disables in-job repair and invalid values fall back to `1`), invokes Codex with `prompts/mode-implement-repair.txt` plus captured diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), and re-runs syntax validation via `scripts/validate_changed_files_syntax.sh` after each attempt. Repair edits are scope-guarded to the initial post-Codex changed-file set, intersected with captured-file entries when present. Any out-of-scope tracked edits are rolled back and out-of-scope untracked files are deleted; the attempt is counted as failed.
10b. **Post-Codex diagnose + fix-up issue creation:** For targeted post-Codex implementation failures, `implement.yml` captures diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), runs a non-fatal syntax check step first, then enforces a separate fatal syntax gate step so repair opportunities can run before final failure. When syntax repair is exhausted/unsuccessful (or for other targeted post-Codex failures with captured diagnostics), it runs the diagnose pass (`prompts/mode-implement-diagnose.txt`) and creates orchestrator-compatible fix-up issue(s). Each created fix-up now receives both `ai:clarification` (pipeline-entry) and `ai:implement-fix-up` (ops marker) labels. The source issue summary comment includes machine-readable blocker metadata (`IMPLEMENT_FIXUP_BLOCKERS_V1`) with `fixup_issue_numbers` and `blocks_source_issue`, which the poller persists additively into orchestrator state for implementation-failed reissue handling. If diagnosis/parsing fails, it creates a deterministic fallback fix-up issue with raw captured diagnostics so failures are never swallowed. This path applies `ai:implementation-failed` and suppresses the generic failure relabel/comment path (preventing re-add of `ai:awaiting-approval`). Out-of-scope failures (missing/empty capture file) continue using the existing generic failure behavior unchanged.
10c. **Implementation-failed blocker gating:** If an `ai:implementation-failed` source issue has post-Codex failure context and linked fix-up blocker issues, the poller defers close/reissue while any blocker issue is still `open` (or when blocker status lookup is unknown). During deferral, it logs and sends Telegram context including mode (`post-codex-validation`), blocker list/statuses, and the defer reason. Reissue resumes only after blockers are no longer open; reissued guidance text is mode-specific (no-op guidance for no-op failures, syntax/blocker-sequencing guidance for post-Codex validation failures). Blocker dependency metadata is persisted additively on the wave issue entry (`depends_on` when already present, otherwise `reissue_depends_on`) for backward compatibility.
10d. **Destructive-commit guard (`ai:destructive-blocked`):** Before creating the AI implementation commit, `implement.yml` inspects the staged deletion set. The commit is refused — and the workflow run fails — on either of two conditions: (a) any deletion touches the canonical workflow-source list (`agents.md`, `ai_pipeline.md`, `codex_system_instructions.md`, `unattended_llm_system_instructions.md`, `prompts/**`, `scripts/**`, `.github/ai/**`) and `ALLOW_WORKFLOW_EDITS` is not `true`, or (b) the total staged deletions exceed `BULK_DELETE_THRESHOLD` (default `3`) and `ALLOW_BULK_DELETE` is not `true`. On rejection the issue is labeled `ai:destructive-blocked`, a visible comment is posted listing the blocked deletions, and a CRITICAL Telegram alert is sent so a human can intervene. The `Validate approval phase label` step at the top of every subsequent `implement.yml` run refuses to redispatch any issue carrying `ai:destructive-blocked` until a human removes the label after auditing the earlier rejection — the orchestrator's judge-cycle may still regenerate the same task under a fresh issue number, so the TG alert is the intended human-in-the-loop signal. This guard exists because PRs #917/#931 saw a test harness that set `GITHUB_REPOSITORY=owner/repo` trigger a consumer-repo cleanup block in `scripts/orchestrate_poll_process.sh` from within the real coding-workflows checkout, causing the AI implementation commit to silently delete ~10,700 lines across 28 tracked source files. The gate in the poller/review_rb_judge scripts has since been switched from the env var to a git-remote-URL check; the destructive-commit guard in `implement.yml` is the defense-in-depth layer that catches any future destructive path regardless of its trigger.
10e. **Targeted vs legacy post-Codex failure flow:** Targeted post-Codex failures with captured diagnostics follow 10a/10b (syntax repair first; if unresolved, diagnose + fix-up issue creation, then label source issue `ai:implementation-failed`) plus blocker-aware reissue gating in 10c. The no-op pre-commit path in 10 remains the close/re-issue retry lane. Other implement workflow failures (for example, missing/empty capture artifacts) remain on the legacy path (`failure()`/`cancelled()` handling in `implement.yml`) with failure comments/alerts.
10f. **Success-no-op short-circuit (Guard 0, `ai:closed`):** The "Run Codex implementation" step in `.github/workflows/implement.yml` snapshots the worktree with `git status --porcelain -uall` into `${RUNTIME_DIR}/codex_pre_baseline.txt` BEFORE the retry loop. Detection, retry-nudge, and success checks all diff against this baseline via `grep -vxFf` so runtime support checkouts (`.codex-workflow-src`, `.codex-workflow-src-main`, `.serena`, `ai-memory/schemas`) don't register as Codex-produced changes. When the baseline-relative delta is empty AND Codex stdout matches `/no file changes were made|nothing to change|already (aligned|implemented|up[- ]to[- ]date|done|exists|present|complete)|no changes needed/i`, the step writes `${RUNTIME_DIR}/codex_success_noop.flag` and breaks with success. The "Handle no-op implementation" step's Guard 0 sees this flag first (before Guard 1's pathspec hard-fail and Guard 2's ancestor-chain cap), closes the issue with `ai:closed` + an ✅ "Already implemented" comment, and exits with `0`. This prevents the orchestrator re-issue loop from spawning duplicate sub-issues when Codex correctly reports the requested work is already on the integration branch (observed failure: issue #141 after `npm run audit:ci` was already exit-0 from a sibling sub-task). Fail-open: missing flag/`RUNTIME_DIR` or a failed flag write falls through to Guards 1/2 as before.
11. **Auto-recovery:** On failure, the judge can revert problematic PRs and create fix-up issues. Those fix-up issues include the standard orchestrator metadata block (`Tracking issue`, `Integration branch`, `Local ID`, `Managed by`) in the issue body. Recovery is attempted up to `MAX_RECOVERY_ATTEMPTS` (default 3) times; if all attempts fail, the project stops and the operator is notified via Telegram.
12. **Validation-failure recovery:** When runtime validation fails, the poller transitions the project back to the judge for re-evaluation (labeled `ai:validation-recovery`) up to `MAX_VALIDATION_RECOVERY_ATTEMPTS` (default 2) times. The judge sees the validation diagnosis in tracking issue comments, can issue fix-up work (with orchestrator metadata), and then re-validates. After exhausting the recovery budget, the project goes to terminal `ai:validation-failed`.
12a. **Integration branch delivery:** Orchestrator projects now create a per-project integration branch (`orchestrator/project-<tracking_issue>`). All orchestrator child issues include `Integration branch` metadata so implementation PRs target the integration branch instead of `main`. Branch resolution order is strict: child issue metadata footer first, then tracking issue metadata, and default-branch fallback only when no integration metadata exists. If metadata exists but the branch is invalid/missing, the poller fails safe instead of silently falling back to default branch. The poller periodically syncs default branch changes into this branch via the merge API.
12b. **Sync conflict handling and superseded detection:** Before sync merge attempts, the poller checks whether the integration branch is effectively superseded by the default branch (tracked child PRs are terminal and affected-path deltas are already represented on the default branch). Superseded projects persist `sync.status = superseded-by-main`, post one final tracking comment, and skip future sync attempts without recurring Telegram warnings. Real unresolved conflicts include parsed conflict paths, a deduped fingerprint to prevent repeated spam, and a rebuild runbook link: [docs/orchestrator-integration-branch-rebuild-runbook.md](docs/orchestrator-integration-branch-rebuild-runbook.md).
12c. **Integration self-healing:** If a periodic `main` → integration-branch sync returns HTTP 409 (real conflict), the poller routes recovery through `heal_integration_branch_conflict`: it (a) ensures/creates the final integration→default PR (eagerly, if it does not yet exist), (b) dispatches the review/autofix workflow through `_dispatch_review_for_conflicts` against that PR to run the existing Codex conflict resolver on a clean runner, and (c) records the attempt in new tracking-state fields (`integration_sync_status`, `integration_sync_last_error`, `integration_conflict_dispatch_count`, `integration_conflict_dispatch_ts`, `integration_conflict_unresolved_ticks`). Dispatches are throttled by `CONFLICT_DISPATCH_COOLDOWN_SECS` (default 900s). The retry budget is **branch-aware**: head refs matching `orchestrator/project-*` honour `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` (default `1` — single resolver shot, then judge), while non-orchestrator integration branches honour `INTEGRATION_CONFLICT_MAX_RETRIES` (default `3`). After the effective budget is exhausted the orchestrator escalates by invoking the judge with full PR context via `codex exec`. Only after both the automated resolver *and* the judge escalation fail is the project marked terminally `failed`. The same healing flow is triggered from `finalize_integration_merge_if_needed` whenever the final PR is observed with `mergeable=false`, so the project no longer halts on first conflict.

12c-i. **Integration-sync intent fingerprints:** When a sub-issue PR merges into an orchestrator integration branch, the poller captures `must_contain` / `must_not_contain` regex fingerprints from the merged diff and persists them under `merged_issue_fingerprints[<issue_num>]` in the orchestrator state comment. The `review_autofix.yml` resolver step uses two affordances on top of these fingerprints when the PR head ref matches `orchestrator/project-*`:
- **Intent injection into the resolver prompt.** The conflict resolver prompt is rendered from `prompts/integration-sync-conflict-resolver.txt` (instead of the generic `prompts/conflict-resolver.txt`) and includes the tracking-issue title/body, the list of merged sub-issues already on this integration branch, and the full `merged_issue_fingerprints` JSON. The template instructs the model to treat each fingerprint as a hard test case and to **synthesize** a new hunk when the conflict is between two independent rewrites of the same code rather than picking side A or side B verbatim.
- **Fingerprint verification gate.** Before the `[ai-merge-resolve]` commit lands, `scripts/verify_integration_fingerprints.py` walks every captured pattern against the post-resolve working tree. A `must_contain` pattern that no longer matches, or a `must_not_contain` pattern that reappears, is treated as a silent intent regression and HARD-fails the resolver step (the merge state is left intact so the next poll tick re-enters healing and — by default — escalates immediately to the integration judge). A silent-regression detector additionally logs a warning whenever the post-resolve tree contains strictly fewer total `must_contain` matches than were captured.

Capture is **going-forward only**: sub-issues merged before fingerprinting was enabled have no entries in `merged_issue_fingerprints` and are silently skipped by the verifier (fail-open). Capture and verification are tunable via `FINGERPRINT_PER_FILE_CAP` / `FINGERPRINT_MIN_PATTERN_CHARS`. The verifier script is in `OPTIONAL_BOOTSTRAP_SCRIPTS` so consumer-repo runs whose pinned `script_ref` predates the script bootstrap cleanly with a fail-open warning rather than a hard error. Operationally: a verification rejection always surfaces in the workflow run log with `::error::Aborting [ai-merge-resolve] commit: integration fingerprint verification rejected the resolver output.`, the resolver step exits 1, the conflict resolver workflow lands without a commit, and the orchestrator's next poll tick takes the judge path because `unresolved_ticks` was already incremented by the dispatch.
12d. **Atomic final merge:** When a project is complete (or validated), the poller creates/reuses a final PR from integration branch to default branch and squash-merges it.
12e. **Phase-agnostic feature-PR drift sweep:** On every poll tick the orchestrator enumerates all open PRs whose head branch matches `ai/issue-*` and calls the GitHub update-branch endpoint for any whose `mergeStateStatus` is `behind`. This fast-forwards clean-mergeable branches before they accumulate enough drift to become conflicted, regardless of the issue's current pipeline phase. Real conflicts (`dirty`) are left for the existing in-progress conflict loop to handle via the resolver dispatch path.
12f. **Comprehensive release callback (poller-owned; currently inert):** Tracking issues labeled `ai:comprehensive-test-pending` are handled by `handle_comprehensive_release_callback_if_needed` in the poller, not by a separate workflow phase. On `complete`, the poller dispatches `test-and-mark-stable.yml` with `dry_run=false` using validated `version_tag`/`test_repo` extracted from tracking comments when present. On `failed` or `validation-failed`, it sends an abort notification and skips dispatch. Successful completion and abort paths record `comprehensive_release_callback.{handled,status,handled_at}` and remove `ai:comprehensive-test-pending` best-effort; dispatch failures in the `complete` path leave callback state/label untouched so a later poll cycle can retry. **Note:** `.github/workflows/comprehensive-test-and-release.yml` no longer applies `ai:comprehensive-test-pending`, so this callback path is currently inert. The poller handler and label definition are retained so the callback can be revived by any future workflow that sets the gating label.
13. **Stall detection and self-healing:** Every poll cycle, the poller tracks how long each issue has been in its current pipeline phase. Stall thresholds are **adaptive per phase**: lightweight phases (clarification, planning, approval, merge) default to 60 minutes, while heavy phases (implementation, review/autofix) default to 120 minutes. Each threshold is independently configurable via `STALL_THRESHOLD_<PHASE>_MINUTES` env vars, with `STALL_THRESHOLD_MINUTES` as the global fallback. Before stall checks, the poller reconciles managed-issue labels and state truth (labels + issue open/closed + linked PR merge state), repairs missing/conflicting phase labels, and persists reconciled statuses every cycle. Closed/terminal issues are hard-guarded out of retrigger paths; stale `no_labels` on closed issues is healed (label/state repair) instead of retriggered. Early-phase recovery actions (`retrigger_pipeline`, `auto_respond_clarify`, `retrigger_plan`, `auto_approve`, `retrigger_implement`) are additionally guarded against issues that already have an open linked PR. The guard is **state-aware**: (a) if the PR has merge conflicts (`mergeable: false`), the guard dispatches the conflict resolver workflow instead of skipping entirely; (b) if the PR has `CHANGES_REQUESTED` reviews, the guard dispatches the review/autofix workflow; (c) if the PR is clean and progressing, the guard skips the recovery action as before. This prevents re-triggering earlier pipeline phases on issues whose implementation PR already exists, while still routing stuck PRs to the appropriate corrective action. The guard uses the batched GraphQL prefetch cache first (0 extra API calls on hit), with a per-issue REST fallback that reuses the PR JSON already fetched for the merged-PR sub-guard. The review-state check adds at most 1 REST call per stalled issue with an open PR. When an issue exceeds its phase threshold, the poller first selects a declarative action from `STALL_RECOVERY_ACTIONS` by `stall_recovery_count` (per issue): `no_labels` → `retrigger_pipeline`, `ai:clarification` → `auto_respond_clarify`, `ai:planning` → `retrigger_plan`, `ai:awaiting-approval` → `auto_approve`, `ai:implementing` → `retrigger_implement`, `ai:done` → `retrigger_review`, `ai:ready-to-merge` → `attempt_merge`, with each phase ladder ending in `escalate_human`. Backward-compatible default behavior keeps human terminalization disabled: with `ENABLE_STALL_HUMAN_TERMINALIZATION=false` (default), any terminal `escalate_human` result (declarative or judged) is downgraded to the nearest prior non-human phase action. Set `ENABLE_STALL_HUMAN_TERMINALIZATION=true` to allow terminal human escalation. If `ENABLE_STALL_JUDGE=true` and `stall_recovery_count >= STALL_JUDGE_TRIGGER_COUNT` (while still below `MAX_STALL_RECOVERIES_PER_ISSUE`), the recovery action switches to `run_stall_judge` for diagnostics-driven action selection. The stall judge may choose targeted actions including `resolve_merge_conflict`; that path attempts GitHub `update-branch` for the target PR and then dispatches `_dispatch_review_for_conflicts`. If stall-judge execution fails, output parsing fails, or the returned action is unsupported, the poller fail-opens to the same declarative ladder action for that phase/recovery count. If `stall_recovery_count` exceeds the phase ladder length, the final declarative action is repeated until the max budget is hit. After `MAX_STALL_RECOVERIES_PER_ISSUE` (default 5) attempts, the issue is skipped (`ai:closed`) so the wave can advance; the judge evaluates the gap at wave completion and decides whether to reissue, accept, or fail. When `ENABLE_STALL_JUDGE=false`, or when `STALL_JUDGE_TRIGGER_COUNT` is effectively unreachable within the configured recovery budget, recovery remains on the declarative `STALL_RECOVERY_ACTIONS` ladder without judge escalation. All stall recoveries trigger Telegram notifications. Standalone AI issues (not linked to any active orchestrator tracking state) also use the same stall recovery engine and the same human-terminalization gate when `ENABLE_STANDALONE_STALL_RECOVERY=true`; standalone recovery state is persisted per issue in a hidden marker comment. Additionally, all orchestrator-created issues (Wave 1, deferred waves, reissues, and judge fix-ups) now receive the `ai:clarification` label at creation time, ensuring they enter the pipeline immediately without relying solely on the `issues.opened` event trigger.
13a. **Missing state recovery:** If the orchestrate.yml workflow creates issues but fails before posting the initial state comment (e.g. due to a transient API error or timeout), the poller automatically reconstructs the state. It parses the tracking issue body to extract the wave structure and dependency graph, searches for child issues that reference the tracking issue, and builds a new state object. The reconstructed state is posted as a comment so subsequent poll cycles operate normally. This prevents projects from being permanently stuck when the initial orchestration run fails mid-execution.
13b. **Phase-failure labels and marker contract:** Runtime semantic validation failures continue to use `ai:validation-failed`; validate-workflow Codex/terminal failures use `ai:validate-failed`; workflow-log-analysis terminal Codex/analyzer failures use `ai:log-analysis-failed` when `tracking_issue > 0`. `scripts/validate_process.sh` and `.github/workflows/workflow-log-analysis.yml` emit `AI_PHASE_FAILURE_V1` marker comments with `schema_version`, `phase`, `failure_mode`, `failed_step_name`, `workflow_run_id`, `workflow_run_attempt`, `workflow_name`, `workflow_file`, `workflow_run_url`, `repository`, `tracking_issue`, `attempt_count`, `recommended_resume_action`, and `timestamp`. Additional failure labels in `.github/ai/label_contract.v1.json` (`ai:clarify-failed`, `ai:clarify-respond-failed`, `ai:plan-failed`, `ai:implement-diagnose-failed`, `ai:review-autofix-failed`, `ai:integration-judge-failed`, `ai:memory-maintenance-failed`) are contract-defined/reserved on this branch; they are recognized by repair/status helpers but are not yet actively emitted by their phase workflows.
13c. **Managed label-repair sweep behavior and contradiction policy:** Poller reconciliation currently runs through `reconcile_managed_issue_labels` for current-wave managed issues on every cycle. It enforces label-contract exclusivity, applies forced terminal truth (`ai:merged` when linked PR is merged; otherwise `ai:closed` for closed issues without merged evidence), logs `LABEL_REPAIR`/`LABEL_REPAIR_DIFF`, and records a healing note. The contradiction-evidence helper path in `scripts/orchestrate_lib.py` (`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`, `resolve_label_repair_evidence`) defines precedence for mixed evidence (linked PR state > stale markers), but this branch does not yet wire that helper into the active poller loop; treat it as contract-defined/reserved behavior until wiring lands. Operator env knobs `ENABLE_LABEL_REPAIR_SWEEP`, `LABEL_REPAIR_DRY_RUN`, and `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE` are likewise reserved/not consumed on this branch.
13d. **Manual sweep and recovery runbook (operator):**
1. Run the poller wrapper manually from Actions (`Internal: AI Orchestrate Poller` / `.github/workflows/internal-orchestrate-poll.yml`) or trigger the reusable `orchestrate_poll.yml` caller in consumer repos.
2. In run logs, grep `LABEL_REPAIR` and `LABEL_REPAIR_DIFF` to verify which labels were added/removed per issue.
3. For validate/log-analysis terminal failures, locate the latest `AI_PHASE_FAILURE_V1` marker comment on the tracking issue, read `recommended_resume_action`, and confirm `workflow_run_id`/`attempt_count` before retriggering.
4. If a failure label conflicts with newer PR evidence, trust linked PR truth (`open` PR implies in-flight review/implement; merged PR implies terminal merge) and treat older marker comments as stale evidence.
14. **Validation gate:** When the judge says "complete" and `ENABLE_VALIDATION=true`, the poller dispatches `ai-validate.yml` on the integration branch (`--ref <integration_branch>`), marks the tracking issue `ai:validating`, and only transitions to complete after `ai:validated` plus successful final squash merge. If the final squash merge cannot land due to a *blocking* failure (for example final-PR creation/lookup failure or hard merge rejection after mergeability/checks gates pass), `mark_validation_complete` increments `final_merge_attempt_count` and defers to the next poll tick. Transient not-ready states (mergeability still computing, required checks still pending) and merge-conflict self-healing paths defer without spending this budget. After `MAX_FINAL_MERGE_ATTEMPTS` (default 3) consecutive budget-eligible failures the project is escalated to `ai:blocked` with `status=failed`, a CRITICAL Telegram alert, and a tracking comment describing the final PR number, merge status, and last recorded error — the project is *not* silently advanced to `status=complete` while the integration branch is unmerged.
15. **Completion:** When validation is disabled, completion remains judge-driven and immediate.

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

### Stall recovery: fresh-push suppression

When the orchestrator-managed loop (`recover_stalled_issue`) and the standalone loop (`run_standalone_stall_recovery`) both detect a stall, they consult `_check_fresh_push_guard` immediately after the existing `issue_has_active_workflow` check. If the stalled issue's phase is `ai:done` or `ai:ready-to-merge` **and** the linked PR's head commit was pushed within the last **30 minutes** (hardcoded, not tunable), the recovery dispatch is suppressed for that cycle and a stable `STALL_SKIP issue=<n> reason=fresh_push pr=<p> pushed_age_secs=<s> phase=<phase> action=<action>` line is emitted. The stall counter is not incremented; the next poll tick re-evaluates.

Rationale: `issue_has_active_workflow` only matches the moment a queued/in-progress workflow run is visible on the PR branch. Between an autofix push and the `pull_request.synchronize`-driven next run materialising (or while the autofix-retrigger dedup is swapping runs per the [Autofix retrigger dedup](#autofix-retrigger-dedup) section), no run is briefly visible to the regex in `build_active_issue_set`, so the phase-age stall timer can fire even though fresh work has landed. The `pushedDate` signal is a more reliable "work landed recently" guard for the short gap.

Data source: both stall paths already fetch the linked PR via batched GraphQL — `_fetch_linked_pr_status_graphql` (orchestrator-managed, reused via `STALL_MANAGED_LINKED_PR_CACHE`) and `_fetch_candidate_issue_details_graphql` (standalone, via `_candidate_details_json`). Both helpers were extended to request `commits(last: 1) { nodes { commit { pushedDate committedDate } } }` on the cross-referenced PullRequest, adding a `headPushedAt` field to each `linked_pr` entry (coalesced: `pushedDate` first, `committedDate` as fallback when push metadata is null — e.g. squashed commits). Zero additional API calls in the steady-state path.

Fail-open: `_check_fresh_push_guard` returns "not fresh" (i.e. lets the existing stall flow proceed) when the phase is outside `{ai:done, ai:ready-to-merge}`, when the linked-PR cache entry is missing or `null`, when `headPushedAt` is missing or unparseable, or when the computed push-age is negative (clock skew). The guard can never cause a stall recovery to fire that otherwise would not have fired; it only suppresses dispatches within the 30-minute fresh-push window.

Log prefix `STALL_SKIP issue=... reason=fresh_push pr=... pushed_age_secs=... phase=... action=...` is a public contract (CLAUDE.md §6 Naming Immutability) — downstream log analysis and dashboards pivot on it; renames require the alongside-old-name shim documented in §6.

### Stall recovery: merge-conflict pre-dispatch override

The standalone stall loop (`run_standalone_stall_recovery`) reroutes the `retrigger_review` recovery action to the conflict resolver (`_dispatch_review_for_conflicts`) whenever the latest linked PR is known to be in a merge-conflict state. Without this override, the retrigger path pushes an empty commit to the PR head branch to re-kick Review Autofix — but autofix operates on the branch as-is and cannot resolve a merge conflict with base, so the next stall cycle repeats the same no-op dispatch until `MAX_STALL_RECOVERIES_PER_ISSUE` is reached.

Detection (`_check_open_pr_conflict_guard`) fires when the cached linked-PR entry shows `state=OPEN` AND (`mergeable ∈ {CONFLICTING,false}` OR `mergeStateStatus/mergeable_state == DIRTY`) — matching the same signal the rebase-bot already uses. Primary data source is `_candidate_details_json`, extended in `_fetch_candidate_issue_details_graphql` to include `headRefName`, `mergeable`, and `mergeStateStatus` on the cross-referenced PR node (zero additional API calls on cache hit). When the cache is missing **or** returns `UNKNOWN` mergeability (GitHub computes mergeability asynchronously — a push kicks off a background job and the API briefly returns `mergeable=null`/`mergeable_state=unknown` per GitHub REST docs), the guard falls back to a REST `GET /pulls/{n}` retry loop of up to **5 attempts** with sleeps **5 s → 10 s → 15 s → 20 s** between retries (50 s worst case per conflicting-unknown PR). The first request kicks off GitHub's recomputation; subsequent retries typically return the definitive state. The loop breaks early when state is settled: either `mergeStateStatus/mergeable_state == DIRTY` (conflict already known even if `mergeable` is still unknown) **or** `mergeable ∈ {true,false}` with `mergeable_state ≠ unknown`. On all-attempts-still-unknown the guard fails open and the legacy retrigger_review dispatch runs. API hygiene (CLAUDE.md §15): the retry loop's final PR JSON is stashed in an iteration-local cache (`_STD_ITER_PR_JSON_CACHED`) so the legacy retrigger_review case reuses it instead of issuing a redundant `gh api` fetch for the same PR in the fail-open path.

On a hit the poller logs `STALL_RECOVERY issue=<n> reason=open_pr_merge_conflict pr=<p> phase=<phase> action=dispatch_conflict_resolver override_from=retrigger_review`, emits a Telegram WARNING, and `continue`s the loop. The `stall_recovery_count` counter is **not** incremented — conflict resolution has its own budget and does not consume the retrigger-style recovery allowance. Duplicate same-cycle dispatches are suppressed via `_CONFLICT_DISPATCH_TRACKER` (return code 2 → `STALL_SKIP reason=open_pr_merge_conflict_dispatch_skipped`); dispatch failures (rc≠0 and ≠2) log `STALL_RECOVERY reason=open_pr_merge_conflict_dispatch_failed` and skip this cycle without burning the counter.

A belt-and-braces check is also wired into `execute_stall_recovery_action retrigger_review`: if the pre-dispatch guard was bypassed (cache empty, managed-path entry, etc.) the action-level check fetches the PR JSON once (reusing the head_ref lookup), detects the conflict, and recursively dispatches `resolve_merge_conflict` with `STALL_RECOVERY_SHOULD_INCREMENT` forced to `false` so the override remains budget-neutral.

Fail-open: when neither cache nor REST fallback can confirm a conflict state, the legacy `retrigger_review` empty-commit push runs as before. The guard can never cause an action that otherwise would not have fired; it only redirects `retrigger_review` → `resolve_merge_conflict` within the conflict window.

Log prefixes `STALL_RECOVERY issue=... reason=open_pr_merge_conflict ...` / `STALL_SKIP issue=... reason=open_pr_merge_conflict_dispatch_skipped ...` are public contracts (CLAUDE.md §6 Naming Immutability).

### Stall recovery: linked-PR closure and re-issue Gap-2 surfacing

When stall recovery closes a stuck issue and opens a replacement, two contracts are enforced by `scripts/orchestrate_poll_process.sh`:

**Linked-PR closure (`close_linked_pr`).** Every stall-recovery `close_and_reissue` invocation (main and standalone) enumerates every open PR linked to the stalled issue via three independent lookups and closes each one:

1. **Timeline cross-reference events** (`_issue_cross_ref_pr_numbers_unique`) — the historical primary source; returns every PR whose creation registered a cross-reference event on the issue timeline.
2. **Head branch name** (`_linked_prs_by_branch_name`) — `gh pr list --head ai/issue-<n> --state open`; catches PRs the orchestrator opened on its conventional branch even when the timeline cross-ref event was missed (observed in prod for PR #2568 / issue #2552, where the timeline API silently omitted the event).
3. **Body-parse** (`_linked_prs_by_body_reference`) — `gh pr list --search "#<n> in:body"` narrowed to open PRs, post-filtered by a case-insensitive regex for the GitHub close keywords `close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved` followed by `#<n>` and a non-digit boundary (so `#25528` does not match for issue #2552).

Results are deduped (`sort -u`) so a PR surfaced by multiple strategies is only acted on once. Already-closed and merged PRs are skipped. Each call emits one of these diagnostic lines to the workflow log, grep-able for audit:

- `close_linked_pr: closing linked PR #<pr> for issue #<n> (state=open).`
- `close_linked_pr: skipping PR #<pr> for issue #<n> (state=closed|merged|unknown).`
- `close_linked_pr: issue=#<n> scanned=<k> closed=<k>`
- `close_linked_pr: no linked PRs found for issue #<n> (timeline/branch/body lookups all empty).`

This enforces the documented `ai:closed` label semantic ("Linked PR closed without merge") that the single-path timeline lookup could silently violate.

**Re-issue Gap-2 surfacing (`surface_reissue_closed_without_pr`).** When stall recovery is about to close a task whose body carries the `Re-issued from #<parent>` marker AND `_find_all_linked_prs` returned nothing — i.e. a re-issue that never produced a PR before stall recovery exhausted (observed in prod for re-issue #2591 of #2552) — four stable signals are emitted before the close:

- **Log prefix** (public contract, do not rename): `REISSUE_CLOSED_WITHOUT_PR issue=<n> parent=<p> phase=<label> stall_minutes=<m> recovery_count=<c> source=<main|standalone>`
- **GHA annotation**: `::warning title=Re-issue closed without PR::...`
- **Issue comment** on the re-issue (before it is closed) summarising phase, stall duration, recovery attempt count, and the parent issue link.
- **ai-memory ledger event** via `memory_record_run_event --event-type reissue_closed_without_pr`, gated on `memory_helpers.sh` being loaded.

Per design (Q3=A), the surfacing is **informational**: it does not block the subsequent `close_and_reissue`, so forward progress through the re-issue chain continues. Downstream alerting should grep the log prefix or watch the ai-memory event type to trigger human review of the parent issue and re-issue chain.

**Pathspec hard-fail escalation (`.codex-workflow-src*`).** The "Handle no-op implementation" step in `.github/workflows/implement.yml` inspects the `remaining_changes` output from the commit step. When that listing contains `.codex-workflow-src` or `.codex-workflow-src-main`, Codex wrote changes into the runtime-fetched support checkout and the commit pathspec exclusions (around `add_u_excludes` / `add_o_excludes` in the same workflow) silently stripped them. Treating that as a normal no-op produced an infinite re-issue loop — observed in tracking issue #1292 for `local_id=validation-render-self-heal` (30+ duplicate sub-issues in ~5 hours). The step now:

- Labels the issue `ai:needs-human` (ensures the label exists, colour `b60205`).
- Posts an explanatory issue comment listing the filtered worktree paths.
- Fires a CRITICAL Telegram alert via inline `curl https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage` (bypasses `tg_helpers.sh` because the support checkout may have been stripped in consumer-repo runs).
- `exit 1`s the job so the failure surfaces as a red X in the Actions UI and downstream steps (Push branch, Create PR) are skipped.

No auto-retry is attempted. A human must review the pathspec exclusions before automation resumes.

**Ancestor-chain no-op cap (belt-and-braces for `MAX_IMPL_NOOP_REISSUES`).** Both `.github/workflows/implement.yml` (via `IMPL_NOOP_ANCESTRY_THRESHOLD`) and `scripts/orchestrate_poll_process.sh` (via the shell helper `count_noop_ancestors`) walk the `Re-issued from #N` chain up to the cap and close the issue with `ai:closed` when the ancestor-count reaches the threshold. The poller wires the check into **all three** re-issue paths — main stall (`execute_stall_recovery_action close_and_reissue`), standalone stall (`run_standalone_stall_recovery close_and_reissue`), and the `no-op-implementation` branch of the `ai:implementation-failed` sweep — so the cap trips whether the state-based `get_impl_noop_count` counter is fresh, stale, or missing. Fail-open: any `gh api` / `_safe_gh_jq` / parse error returns `0` and the caller falls through to the legacy re-issue flow. API cost is bounded at `2 * MAX_IMPL_NOOP_REISSUES` calls per invocation (one `GET /issues/{n}` + one `GET /issues/{n}/comments` per hop, stops early on first non-no-op ancestor).

### Telegram Notifications & Cleanup

Telegram notifications fall into three categories based on their lifecycle:

**Persistent alerts (never deleted):**
- **Release results** — success/failure from `test-and-mark-stable.yml`
- **PR merged** — sent by `issue_pr_status.yml` for non-orchestrator issues
- **Orchestrator project completion** — sent by the poller after all tracked messages are cleaned up

**Phase-tracked alerts (deleted when the phase completes):**
For non-orchestrator issues, human-intervention alerts are cleaned up automatically when the next phase begins:
- **Clarification required** — sent by `clarify.yml` and `plan.yml` for non-orchestrator issues only, deleted when `plan.yml` runs (stored as `<!-- tg_phase:clarify:id -->`). Orchestrator-managed issues skip this alert because clarify uses a label-based fast path (`ai:orchestrator-managed`) that auto-posts `/answer [auto-answered-by-orchestrator]` unless a human forces `/reclarify`; if `plan.yml` cannot auto-parse recommended clarification answers it sends a general tracked `WARNING` and waits for a human `/answer`.
- **Plan awaiting approval** — sent by `plan.yml` (when `AUTO_IMPLEMENT_ON_CLEAR_PLAN` is not true), deleted when `implement.yml` runs (stored as `<!-- tg_phase:plan:id -->`)

**General tracked alerts (deleted at terminal state):**
- Orchestrator-managed issue alerts use general tracking (`<!-- tg_cleanup:id1,id2,... -->`), cleaned up when the tracking issue reaches a terminal state (complete or failed) via the poller.
- Any remaining tracked messages (general or phase) are cleaned up when a PR is closed/merged by `issue_pr_status.yml`.

**Requirements:**
- `TG_BOT_SECRET` must be set (same secret used for sending).
- The bot must have permission to delete messages in the target chat (this is automatic for messages the bot itself sent, within 48 hours).
- No additional configuration is needed — cleanup is enabled automatically when `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID` are set.

**Note:** Messages older than 48 hours cannot be deleted by the Telegram Bot API. For long-running orchestrated projects, intermediate messages sent more than 48 hours before completion will remain in the chat.

### GitHub API rate-limit admin alert

Any workflow or script that routes GitHub API calls through `scripts/gh_helpers.sh` (`gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`, `curl_gh_api`) will fire a single admin Telegram alert the first time a rate limit is detected in a cooldown window. The alert body is of the form:

```
⚠️ WARNING: GitHub API rate limit hit — workflow=<name> repo=<owner/repo> run=<runs-url>
```

**Throttling:** alerts are globally throttled to at most one per `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` (default `3600` s = 1 h) across **all** workflow runs. The cooldown state is kept in a Telegram **pinned message** in the admin chat via an embedded marker `<!-- gh_rl_ts:EPOCH -->`, read with `getChat`. This deliberately avoids any GitHub API call for dedup state so the throttle still works while the GitHub API itself is the resource being limited. Previous pinned alerts are unpinned best-effort after a new alert is pinned.

**Fail-closed semantics:** if `pinChatMessage` fails after the alert was sent, the sent message is rolled back via `deleteMessage` so the "≤ 1 alert per cooldown window" invariant is preserved even under transient Telegram failures.

**Requirements:**
- `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID` must be set.
- The bot must have permission to pin / unpin / delete messages in the target chat.
- `jq` must be available (already a baseline on all repo runners).
- The caller script must source `scripts/gh_helpers.sh`. All four `review_*.sh` helpers and `orchestrate_poll_process.sh` route GitHub API calls through `gh_retry` / `curl_gh_api` and therefore participate in the alert.

**Interaction with `ALERT_MSG_LEVEL`:** the rate-limit alert is emitted at `WARNING` level and honours the global `ALERT_MSG_LEVEL` threshold the same way `scripts/tg_helpers.sh::tg_send_msg` does. If an operator configures `ALERT_MSG_LEVEL=ERROR` or `ALERT_MSG_LEVEL=CRITICAL`, the rate-limit alert is suppressed entirely (no send, no pin update, no cooldown advance). The cooldown state is only touched when the alert would actually fire, so tightening `ALERT_MSG_LEVEL` does not strand a stale pinned marker.

**Disabling:** unset `TG_BOT_SECRET` or `TG_ADMIN_CHAT_ID` — the helper no-ops silently. You can also set `ALERT_MSG_LEVEL=ERROR` (or higher) to suppress the rate-limit alert while keeping other ERROR/CRITICAL Telegram notifications. There is no way to disable the feature per-caller; if you need to skip alerting for a specific bootstrap probe (e.g. a `gh api /labels/<name>` existence check where 404 is the expected normal case), call `gh` directly instead of via `gh_retry`. See `ensure_label_exists` in `scripts/orchestrate_poll_process.sh` for an example.

### GitHub API rate-limit circuit breaker

When any `gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`, or `curl_gh_api` call in `scripts/gh_helpers.sh` detects a GitHub API rate limit (403/429), it touches a flag file (`/tmp/.gh_rate_limit_circuit_breaker`, overridable via `GH_RATE_LIMIT_BREAKER_FILE`). The poller's inline retry helper in the "Find active tracking issues" step also writes this flag on rate-limit detection.

At the end of each `orchestrate_poll.yml` run, a "Check rate-limit circuit breaker" step reads this flag. If tripped, both the cooldown sleep and the self-retrigger dispatch are skipped — the poller exits immediately and waits for the next cron-scheduled run (every 30 minutes) instead of chaining another immediate cycle.

This prevents a rate-limited poller from burning Actions minutes and GH API quota on back-to-back runs that will hit the same limit. The current run always completes normally; only the _next_ self-triggered cycle is suppressed.

The `gh_rate_limit_breaker_tripped` shell function is exported by `gh_helpers.sh` for use by other scripts that may want to query the flag.

### H3 Timeline GraphQL Shim

- `scripts/gh_helpers.sh` now exposes `gh_issue_timeline_with_cross_refs owner repo issue_number`.
- The helper issues one GraphQL query per issue (`timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT, CLOSED_EVENT])`) and reshapes nodes into the legacy timeline JSON contract used by existing jq pipelines.
- Fail-open fallback is mandatory and implemented: if the GraphQL call fails, payload shape is invalid, transform fails, or `pageInfo.hasNextPage=true`, the helper falls back to the legacy REST timeline pagination + per-PR enrichment path.
- Every fail-open path emits a structured warning with stable key: `::warning::rate_limit_audit_fallback ...`.
- Manual (non-CI) parity check:
  - Script: `scripts/compare_issue_timeline_parity.sh`
  - Fixtures: `scripts/fixtures/issue-timeline/rest_timeline_fixture.json` and `scripts/fixtures/issue-timeline/graphql_timeline_fixture.json`
  - The script compares jq-normalized parity for merged-evidence detection, cross-reference URL/number extraction, and latest-linked-PR (`| last`) selection.

### H4 PR Comments GraphQL Shim

- `scripts/gh_helpers.sh` now exposes `gh_pr_with_all_comments owner repo pr_number`.
- The helper fetches PR metadata + issue comments + review comments in one GraphQL call (`pullRequest` + `comments(first:100)` + `reviews(first:50)` + nested review `comments(first:100)`), then reshapes to the legacy contract consumed by existing jq filters:
  - `meta`: `{title, body, head_ref, base_ref, head_sha}`
  - `comments`: `[{author, body, created_at}]`
  - `review_comments`: `[{author, path, line, body}]`
- Fail-open fallback is mandatory and implemented: on GraphQL request/parse/transform failure, or if any `hasNextPage=true` (PR comments, reviews, or nested review comments), the helper falls back to the legacy REST pagination path.
- Every fail-open path emits a structured warning with stable key: `::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments ...`.
- `scripts/orchestrate_poll_process.sh` and `scripts/review_rb_judge.sh` now use this helper for comment-context hydration while preserving downstream prompt JSON semantics.
- Manual parity check (`scripts/compare_issue_timeline_parity.sh`) now also validates PR context parity using:
  - `scripts/fixtures/issue-timeline/rest_pr_with_comments_fixture.json`
  - `scripts/fixtures/issue-timeline/graphql_pr_with_comments_fixture.json`

### H6 API Hygiene Inventory/Reporting Notes

- GitHub API hygiene is operationally enforced by preferring existing batched calls (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) and cycle-local caches (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, `_candidate_details_json`) before introducing any new per-issue call sites.
- Label-repair and stall diagnostics are intentionally inventory-friendly: `LABEL_REPAIR`, `LABEL_REPAIR_DIFF`, `STALL_SKIP`, `AUTOFIX_PEER_CHECK`, `AUTOFIX_DISPATCH_SKIPPED`, and `AUTOFIX_DISPATCH_ISSUED` are stable log prefixes consumed by workflow-log analysis/reporting.
- `workflow-log-analysis.yml` is the reporting path for API hygiene drift. It already aggregates workflow families and emits markdown reports from `workflow_log_report.json`; include those stable prefixes in log-analysis queries when auditing API call regressions or contradictory repair behavior.
- Current branch status for requested resilience knobs: `ENABLE_PHASE_FAILURE_COMMENTS`, `ENABLE_LABEL_REPAIR_SWEEP`, `LABEL_REPAIR_DRY_RUN`, and `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE` are contract-defined/reserved in docs, but runtime gating for those knobs is not wired yet.

### H5 Actions Runs Cross-Tick Cache

- `scripts/orchestrate_poll_process.sh` now centralizes poller run-state reads behind `_load_actions_runs_cached`, so `build_active_issue_set`, `cancel_zombie_runs_for_issue`, and `invoke_stall_judge` filter one shared actions-runs blob per tick instead of issuing separate `GET /actions/runs` requests.
- Cache payloads are stored per repository on the `ai-memory` branch and validated against `ai-memory/schemas/actions_runs_cache.v1.json`.
- New CLI in `scripts/ai_memory.py`:
  - `actions-runs-cache get --repo <owner/repo>`
  - `actions-runs-cache put --repo <owner/repo> --runs-file <path> --etag <string>`
- New env var `ACTIONS_RUNS_CACHE_TTL_SECONDS` (default `60`) controls freshness; invalid values fail-open to default with a warning.
- Cache corruption/schema mismatch is fail-open and treated as cache miss with structured warning key `rate_limit_audit_fallback`.

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
- Terminal failure labels are split by failure class: runtime semantic failures continue to use `ai:validation-failed`, while validate-workflow Codex/terminal execution failures use `ai:validate-failed` with `raw_status=codex_failure` and `AI_PHASE_FAILURE_V1` marker comments when tracking issue context exists.
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

This does **not** reset the total `judge_cycle` counter (which is informational only — it tracks wave-advance/judge-cycle progression, including clean-wave skips where the judge is intentionally not invoked). Only the stall and recovery counters that gate the failure limits are reset.

Use this after manual intervention (e.g. fixing a problematic issue, merging a stuck PR, or adjusting `MAX_JUDGE_CYCLES`/`MAX_RECOVERY_ATTEMPTS` variables). There is no limit on how many times `/judge_resume` can be used.

> **Note:** `/judge_resume` only applies to judge/recovery failures. For validation failures (`ai:validation-failed`), use `/revalidate` instead.

### Validation Controls

| Variable | Default | Behavior |
|---|---|---|
| `ENABLE_VALIDATION` | `true` | Truthy values (`1/true/yes/on`, case-insensitive) enable the validation gate. Any other value disables it, so judge `complete` closes immediately without runtime validation. |
| `MAX_VALIDATE_CYCLES` | `3` | Maximum cycles across initial validation plus fix/revalidate loops. Must be a positive integer; invalid values are coerced to `3`. Exceeding the limit forces `ai:validation-failed`. |
| `MAX_SELF_HEAL_ATTEMPTS` | `2` | Maximum in-process self-heal attempts per validate_process.sh invocation. Self-heal attempts patch one of the four validation prompts locally and re-exec the validation pipeline; they do NOT increment `MAX_VALIDATE_CYCLES`. Set to `0` to disable self-healing entirely. See [Validation self-healing](#validation-self-healing). |
| `VALIDATION_USE_TEMPLATES` | `false` | Truthy values (`1/true/yes/on`, case-insensitive) switch `scripts/validate_process.sh` Phase 1 to template renderer mode (`scripts/render_validation_templates.py`) instead of freehand Codex harness generation. Missing manifest/renderer/schema/template assets fail with `raw_status=harness_error`; there is no silent fallback when opt-in is enabled. |
| `MAX_CODEX_ATTEMPTS` | `3` | Codex retry cap for validate and workflow-log-analysis Codex execution paths. Must be a positive integer; invalid values fail open to `3` with a warning. |
| `CODEX_RETRY_BACKOFF_BASE_SECS` | `10` | Exponential retry backoff base (seconds) used with `MAX_CODEX_ATTEMPTS` (`base * 2^(attempt-1)`) for validate and workflow-log-analysis Codex execution paths. Must be a positive integer; invalid values fail open to `10` with a warning. |
| `VALIDATE_PREFLIGHT_PYFLAKES_ENABLED` | `true` | Preflight F-code lint gate for embedded Python heredocs in `validation/**/*.sh`. Runs `pyflakes` + `ruff check --select $VALIDATE_PREFLIGHT_PYFLAKES_RULES` against each quoted `python3 - <<'PY' ... PY` body. Catches undefined-name (F821) and related bugs that `ast.parse` cannot see and runtime tests skip in unexercised conditional branches (the class that produced `unknown_error:NameError` in consumer autobet finalize logs). Missing tools are auto-installed via `python3 -m pip install --user`; install failure fails open with `::warning::`. Set `false` as a break-glass. |
| `VALIDATE_PREFLIGHT_PYFLAKES_RULES` | `F` | Ruff `--select` value for the preflight F-code lint. Default `F` matches all pyflakes-equivalent rules. Must match `^[A-Z0-9,]+$`; invalid values fall back to `F`. Narrow to `F821` if only the undefined-name class should block. |

Validate/workflow-log-analysis failure marker contract:

- `scripts/validate_process.sh` emits terminal `AI_PHASE_FAILURE_V1` only for validate-workflow Codex/execution failures and sets `ai:validate-failed` when tracking issue context exists; runtime semantic failures keep existing `ai:validation-failed` behavior.
- `workflow-log-analysis.yml` emits `AI_PHASE_FAILURE_V1` + `ai:log-analysis-failed` on terminal Codex/analyzer failures only when `tracking_issue > 0`; otherwise it logs explicit fail-open warnings and exits without issue mutation.

### Validation self-healing

Validation can self-heal transient prompt-wording defects in the four
validation prompts (`mode-validate-discover.txt`, `mode-validate-generate.txt`,
`mode-validate-fix-harness.txt`, `mode-validate-diagnose.txt`) without
burning a validation cycle. This is useful when a prompt defect causes the
LLM to emit malformed JSON, the wrong harness shape, or an incorrect
classification — failures that a human would normally have to fix by
editing the prompt and re-running.

**How it works**

1. When any phase of `scripts/validate_process.sh` is about to fail hard
   (generate parse failure, preflight failure, template render-recovery
   failure, canary failure, diagnose decision point), it invokes
   `scripts/self_heal_validation.sh`. In template mode, preflight lint/syntax
   failures first trigger one deterministic rerender + relint attempt in
   `validate_process.sh` before terminalizing.
2. The helper renders `prompts/mode-validate-self-heal.txt` with the full
   failure context and the current text of the four validation prompts,
   and asks the LLM to propose a minimal unified diff against exactly one
   of those four files — or an empty patch if the failure is a real app
   bug, real infrastructure bug, or otherwise not prompt-attributable.
3. If a patch is proposed, it is validated (allow-list of target files,
   clean apply) and applied to the runtime copy of `prompts/`. The helper
   appends the patch to `${RUNTIME_DIR}/self_heal_patches.jsonl` and
   `validate_process.sh` re-execs itself with `SELF_HEAL_ATTEMPT`
   incremented — the validation cycle counter is not touched.
4. Self-heal attempts are capped at `MAX_SELF_HEAL_ATTEMPTS` (default 2)
   per `validate_process.sh` invocation. After that, the original failure
   falls through to the normal hard-fail path and burns a validation
   cycle as today.
5. If the pipeline eventually passes after one or more successful self-
   heal attempts, `validate_process.sh` sends a `repository_dispatch`
   event of type `validation-prompt-self-heal` to
   `shubhodeep1/coding-workflows` carrying the accumulated patches and
   run metadata.

**Intake on coding-workflows**

`.github/workflows/validation-improvements-intake.yml` receives the
dispatch and:

1. Applies each patch to the allow-listed prompts on a new branch
   `validation-improvements/<consumer-slug>-<run-id>`.
2. Appends a dated entry to [`docs/validation-improvements.md`](docs/validation-improvements.md)
   with the run metadata, rationale, and the exact diff(s) applied.
3. Opens a **draft** pull request against `main` with a `[skip ai]`
   title token and the `ai:needs-prompt-review` label.
4. Sends a Telegram notification to the admin via
   `tg_send_msg` (severity `WARNING`).

**Why draft PRs?**

Draft PRs are already opted out of the auto-review/auto-autofix/
auto-merge pipeline at `.github/workflows/review_autofix.yml` — the
review gate short-circuits on `pr_is_draft == true`. This means the
self-heal patches cannot be merged without explicit human action.

**Unlock procedure (admin)**

1. Review the draft PR. Confirm the patch is additive, does not rename
   or remove any identifier, does not change any declared JSON schema
   field name, and respects every hard constraint in the target prompt.
2. If you are satisfied, click **"Ready for review"** in the GitHub UI.
   This fires a `pull_request.ready_for_review` event which the AI
   review wrapper ([`workflow-templates/ai-review.yml`](workflow-templates/ai-review.yml))
   now listens for, so the normal `review_autofix` flow engages.
3. If you are not satisfied, close the PR. The consumer's next
   validation cycle will re-dispatch a fresh patch if the same defect
   still reproduces.

**Prerequisites**

- The `GH_PAT` secret used by the consumer's validation workflow must
  have `repo` scope on `shubhodeep1/coding-workflows` in order for the
  `repository_dispatch` call to succeed. If it does not, the self-heal
  still works locally for the current run — the pipeline will pass —
  but the improvement will not be propagated upstream. The patches are
  preserved in the consumer run's `ai-validation-*` artifact
  (`self_heal_patches.jsonl`) for manual forwarding.
- If `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID` are not set on
  `shubhodeep1/coding-workflows`, the admin Telegram alert is skipped
  silently but the PR is still opened.

**Known limitations**

- Self-heal patches are only dispatched upstream when the pipeline
  reaches a final `pass` in the same `validate_process.sh` invocation.
  If self-heal fixes a prompt but validation still exits with
  `needs_fixes` (legitimate app bugs), the patches are not dispatched —
  they are only retained in the run artifact.
- Self-heal never patches files other than the four allow-listed
  validation prompts. Harness shell scripts, workflow YAML, and other
  prompts are out of scope by design.

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
- Optional template mode (`VALIDATION_USE_TEMPLATES=true`) renders harness assets from `.ai/validate.yml` via `scripts/render_validation_templates.py` + `workflow-templates/validation-harness/` and skips freehand generation.
- `validation/validate.sh` is generated as a thin wrapper that delegates to checked-in `scripts/validate_driver.sh`.
- Canonical runtime harness behavior now lives in `scripts/validate_driver.sh` (pre-flight, compose startup/logging, health polling, canary gating, TAP-safe counting, result emission/finalization).
- `scripts/validate_driver.sh` loads optional `validation/validate.env` and applies conservative defaults for supported knobs (including `APP_SERVICE`, `APP_URL`, `HEALTH_TIMEOUT`, `PHASE`). `APP_URL` is opt-in: the host-side HTTP probe is only performed when the consumer explicitly sets `APP_URL` (via environment or `validation/validate.env`). When unset, the health gate relies solely on Docker container state (Running + Health in {healthy, none}), so library-type consumers with no real HTTP service do not time out on a stale default probe URL. The fallback default (`http://localhost:8080/health`) is retained for documentation/inspection only.
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
- **Source-of-truth channel**: `@main` — used by this repo's own
  `internal-*.yml` wrappers. See the wrapper-pinning note in
  "Create wrapper workflows" above for why internal wrappers track `@main`
  rather than `@stable`.

Consumer repos pin to `@stable` for automatic updates or exact tags for reproducibility. This repo's own `internal-*.yml` wrappers pin `@main`.

## Contributing

1. Make changes in a feature branch
2. Test via canary channel on pilot repos
3. Promote to stable after validation

See `docs/release-policy.md` for the full release process.
