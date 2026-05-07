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
| `WORKFLOW_EDITOR_MODEL` | No | `openai/gpt-5.4` (every phase: clarify, plan, orchestrate, orchestrate_poll judge, orchestrate_clarify_respond, validate, workflow-log-analysis, implement, review_autofix editor, orchestrate_poll conflict resolver) | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate, workflow-log-analysis | Model for code editing / reasoning tasks. The pipeline standardises on `gpt-5.4` (unified reasoning + coding) so a single setting changes every phase; the previous split that kept patch-heavy phases on `gpt-5.3-codex` was retired after the model's announce-without-emit regression ([openai/codex#11151](https://github.com/openai/codex/issues/11151)) drove repeat no-edit failures. Setting this var overrides the default; use per-workflow vars (`WORKFLOW_ORCHESTRATE_MODEL`, `WORKFLOW_VALIDATE_MODEL`, `WORKFLOW_LOG_ANALYSIS_MODEL`) for finer control. `gpt-5.3-codex` is still listed in `scripts/codex_model_catalog.json` for explicit opt-in. |
| `WORKFLOW_VALIDATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | validate | Model override for validation harness generation/diagnosis |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | No | `true` | plan | Auto-trigger implementation when plan is clear |
| `ALLOW_WORKFLOW_EDITS` | No | `true` | review_autofix, implement, update_workflows, orchestrate_poll | Allow AI edits to `.github/workflows` files and automatic wrapper updates. Set to `false` to opt out of auto-updates. Orchestrator conflict-dispatch (`_dispatch_review_for_conflicts`) forwards this value to the dispatched review workflow via `-f allow_workflow_edits=`. |
| `ENABLE_AUTO_MERGE` | No | `true` | review_autofix, orchestrate_poll | Auto-merge PRs (squash) when review passes. Requires "Allow auto-merge" in repo settings. |
| `MAX_AUTOFIX_ITERATIONS` | No | `3` | review_autofix | Maximum consecutive autofix rounds before the review loop stops and hands control to the per-PR review-blocked judge. The judge then decides `merge`, `fix` (push a `[judge-fix]` commit which resets the autofix counter — capped at `MAX_REVIEW_BLOCKED_RETRIES`), or `close_and_reissue`. If the judge step is skipped or fails to handle the PR (`judge_handled != 'true'`), the linked issues are labelled `ai:review-blocked` and a review-blocked comment is posted on the PR. Applies uniformly to every PR mode (orchestrator intermediate, orchestrator final, non-orchestrator). The retrigger guard's PR mode classifier (`orch_intermediate` / `orch_final` / `other`, gated by `ORCH_PR_AUTOFIX_FLOW_ENABLED`) is now used only for observability and the orchestrator-level judge cap bypass on `orch_final`; it no longer overrides the per-PR autofix cap. See [Orchestrator PR autofix flow](#orchestrator-pr-autofix-flow). |
| `ORCH_PR_AUTOFIX_FLOW_ENABLED` | No | `true` | review_autofix, orchestrate_poll | Master switch for the orchestrator-aware PR autofix flow. When `true`, `review_autofix.yml`'s retrigger guard classifies the PR (`orch_intermediate` / `orch_final` / `other`) by base/head branch (used for observability and the orchestrator-side cap bypass on `orch_final`); `orchestrate_poll_process.sh` bypasses the `MAX_JUDGE_CYCLES` cap while the integration→default-branch final PR is open and pending merge so the final PR can run unlimited 3-autofix→judge cycles until mergeable. The per-PR autofix loop itself uses `MAX_AUTOFIX_ITERATIONS` uniformly across every mode. Set to `false` to force `orch_pr_mode` to stay at `other` for every PR (head/base never inspected) and disable the orchestrator-side cap bypass (`MAX_JUDGE_CYCLES=25` then applies to the final-PR loop too). See [Orchestrator PR autofix flow](#orchestrator-pr-autofix-flow). |
| `ORCH_INTEGRATION_BRANCH_PATTERN` | No | `^orchestrator/project-` | review_autofix | POSIX-extended regex used by the retrigger guard to identify orchestrator integration branches (head ref) and orchestrator-targeted bases. Defaults match the orchestrator's conventional branch naming (`orchestrator/project-<TRACKING_ISSUE_NUMBER>` set by `.github/workflows/orchestrate.yml`). Override only if you have customised the orchestrator branch naming. |
| `CHECK_RUNS_AUTOFIX_ENABLED` | No | `true` | review_autofix | When `true` (default), the workflow snapshots failed and incomplete GitHub check-runs on the PR head SHA into `${PR_CHECK_RUNS_CONTEXT_FILE}` and feeds it to reviewers + editor so CI / lint failures are detected and fixed on every run. The "Collect PR check-run failures" step in `.github/workflows/review_autofix.yml` calls `gh_retry gh api --paginate --slurp "repos/{repo}/commits/{sha}/check-runs?per_page=100"` once per poll iteration; this is one *logical* snapshot attempt, but it may consume multiple underlying GitHub API requests (one per pagination page, plus up to `GH_RETRY_MAX_ATTEMPTS` retries on transient failures), so operators sizing rate-limit budgets should treat the per-iteration cost as ≥1 requests rather than exactly one. Reviewers see the file as a numbered context section, and the editor prompt elevates failed entries to the top of the WILL_FIX priority order (see `scripts/review_apply_fixes.sh` "CI / LINT CHECK-RUN FAILURES" block). Fail-open: an unrecoverable API failure writes a sentinel file (`collection_status: api_error`) and the autofix pipeline continues — reviewers/editor are explicitly told to treat the absence-of-failures signal as unknown rather than confirmed-passing. Set to `false` to disable check-run collection entirely (the file still gets written with `collection_status: disabled` and zero counts so preflight always passes). |
| `CHECK_RUNS_WAIT_TIMEOUT_SECS` | No | `1200` | review_autofix | Target/nominal maximum seconds the "Collect PR check-run failures" step waits for in-progress / queued check-runs to complete before snapshotting. The wait excludes the current workflow run's own check-runs from the in-flight count — entries whose `details_url` contains `/actions/runs/${{ github.run_id }}/job/` are filtered out — so the `codex-agent` job's own `in_progress` check-run cannot keep the count perpetually above zero and self-wait until this timeout expires; sibling workflow runs on the same SHA (e.g. the implement run's `Agent` job, lint/test workflows) are still waited on. The poll request itself runs under `gh_retry`, so retry/backoff sleep (including waiting for GitHub rate-limit reset) can push actual wall-clock elapsed time past this configured value; treat it as the collector's wait budget, not a hard cap. When the timeout trips, the snapshot proceeds with whatever data exists and emits a `::warning::CHECK_RUNS_WAIT_TIMEOUT` log line. Integer in `0..3600`; invalid or out-of-range values clamp to `1200`. Set to `0` to skip waiting entirely (snapshot whatever is currently completed). |
| `CHECK_RUNS_POLL_INTERVAL_SECS` | No | `20` | review_autofix | Target sleep interval between check-run status poll attempts in the wait loop. Integer in `5..300`; invalid values clamp to `20`. Each poll iteration runs one `gh_retry gh api --paginate --slurp "/repos/{repo}/commits/{sha}/check-runs?per_page=100"` call (≥1 underlying GitHub API requests once pagination + `gh_retry` are accounted for), so a given iteration's wall-clock duration can exceed this interval when retries/backoff apply. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | No | (built-in catalog in script) | review_autofix | Optional path to a custom floor-rule keyword catalog consumed by `scripts/review_floor_rules.sh`. When unset, missing, or unreadable, the script falls back to its built-in keywords and logs a warning. |
| `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` | No | `8` | review_autofix | Seconds the post-commit and editor-changes-lost retrigger steps wait before checking for an already-queued peer review run on the same PR branch. If a peer is found the retrigger skips its own `workflow_dispatch` to avoid creating a redundant queued run (and extra API/UI noise) in the `pr-autofix-${PR}` concurrency group (see [Autofix retrigger dedup](#autofix-retrigger-dedup)). Must be an integer in `0..60`; invalid values clamp to `8`. |
| `AUTOFIX_SKIP_SELF_TRIGGERED` | No | `true` | review_autofix | Skip the full reviewer/editor cycle on `pull_request.synchronize` events whose HEAD commit is a `[ai-autofix]` commit pushed by the configured bot account (GitHub-attributed identity, see `AUTOFIX_BOT_LOGIN`). These synchronize events are self-triggered by the prior autofix commit and otherwise cost a second full review pass (5 reviewers + consensus + editor) per fix round — roughly 2× LLM spend per autofix iteration. The gate job in `review_autofix.yml` queries the HEAD commit via one `GET /repos/{repo}/commits/{sha}` call and extracts `(commit.message first line, author.login, committer.login)` — `.author.login` / `.committer.login` are GitHub-resolved from the push credentials and are not user-controlled (unlike `.commit.author.email`, which git will accept from any local config). The gate sets `should_run=false` only when the subject starts with `[ai-autofix]` AND at least one of `.author.login` / `.committer.login` equals `AUTOFIX_BOT_LOGIN` (default `codex`); fails open on API error or when both logins are empty. The post-commit `workflow_dispatch` retrigger step applies a mirror guard; when `AUTOFIX_CONTINUATION_ENABLED=true` (default) the mirror skips only ledger-only commits (§20.3) and the legacy opt-out case, so productive `[ai-autofix]` commits immediately dispatch the next iteration via `workflow_dispatch` (see `AUTOFIX_CONTINUATION_ENABLED` and probably_unnecessary_but_read_if_stuck.md §20.4). `[ai-merge-resolve]` / conflict-resolved pushes also dispatch a follow-up verification pass for post-conflict-resolution safety. `workflow_dispatch`, `opened`, `reopened`, and `ready_for_review` events always run regardless of this flag. Set to `false` to opt out and restore the legacy "every commit re-verifies" behaviour. Safety net for orchestrator-tracked PRs: the orchestrator stall cron (`internal-orchestrate-poll.yml`, `*/5 * * * *`) re-kicks autofix via `workflow_dispatch` (which bypasses the skip) if a phase-timer threshold trips; continuation closes the same gap in-run for non-orchestrator PRs. Audit via `AUTOFIX_GATE_SKIP reason=self_triggered_autofix` / `AUTOFIX_GATE_NO_SKIP_IDENTITY` / `AUTOFIX_GATE_SKIP_QUERY_FAILED` / `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix` log lines (see [Autofix retrigger dedup](#autofix-retrigger-dedup)). |
| `AUTOFIX_BOT_LOGIN` | No | `codex` | review_autofix | GitHub login that the gate job accepts as the authoritative bot identity for the self-triggered autofix skip. Compared against `.author.login` / `.committer.login` on the HEAD commit API response — both are GitHub-attributed (resolved server-side from push credentials), not user-controlled git metadata. Override if you run the workflow under a fork of codex that pushes as a different bot account (e.g. `codex-bot`, `my-org-codex`). Unset or empty falls back to the default `codex` (via shell `${AUTOFIX_BOT_LOGIN:-codex}` expansion) — to disable the skip entirely, set `AUTOFIX_SKIP_SELF_TRIGGERED=false` instead. |
| `AUTOFIX_SKIP_DOC_ONLY` | No | `true` | review_autofix | Deterministic pre-review skip — doc-only branch. When `true`, the gate job skips the reviewer panel + editor cycle if every changed file in the PR matches the doc-only glob set: `*.md`, `*.txt`, `*.rst` (case-insensitive suffix on basename), `LICENSE*`, `CHANGELOG*` (case-insensitive prefix on basename — matches GitHub's own LICENSE/CHANGELOG detection, which recognises `license.txt`, `Changelog.md`, `LICENCE`, etc.), or `docs/**` (case-insensitive, depth-agnostic but rooted: `docs/x/y.md` matches; `src/docs/x.py` does NOT). When the gate fires, the new sibling job `deterministic-skip-merge` adds `ai:review-skipped` to the PR, sets `ai:ready-to-merge` on every linked issue (resolved via GraphQL `closingIssuesReferences` only — no body/title regex fallback, because the doc-only skip path is the most likely place for incidental issue references in prose to false-match; orchestrator-managed PRs use explicit `Fixes #N` keywords which GraphQL resolves correctly), and enables auto-merge (squash) — mirroring the tail of the normal codex-agent path so the orchestrator phase machine still advances. Merge-conflict resolver, summarizer, and review-blocked judge are all skipped on this path; if a doc-only PR happens to have a conflict, auto-merge blocks and the orchestrator stall cron re-dispatches per existing recovery contracts. Set to `false` to disable the doc-only branch (the size-threshold branch via `AUTOFIX_SKIP_MAX_ADDITIONS` / `AUTOFIX_SKIP_MAX_DELETIONS` still applies). Per-PR override: title contains `[force-review]` OR PR carries the `force-review` label — skip is bypassed and full review runs. The doc-only `/files` lookup is skipped entirely when the size-threshold branch already qualifies (cheaper path runs first), so most small-and-doc-only PRs cost zero extra API calls beyond the existing PR-state fetch. Paginated `/files` output is merged via `jq -s 'add // []'` so the doc-only check stays correct on PRs with > 1 page of files. Audit via `AUTOFIX_GATE_DET_SKIP_OVERRIDE` / `AUTOFIX_GATE_DET_SKIP_EVAL pr=<n> files=<k\|skipped> additions=<a> deletions=<d> max_add=<x> max_del=<y> doc_only=<bool> small_diff=<bool> skip=<bool> reason=<docs_only\|small_diff\|empty>` / `AUTOFIX_GATE_DET_SKIP_FILES_UNAVAILABLE` log lines. Fails open: any `/files` lookup failure leaves the doc-only check inconclusive and a non-small PR runs full review. |
| `AUTOFIX_SKIP_MAX_ADDITIONS` | No | `10` | review_autofix | Deterministic pre-review skip — size-threshold branch (additions). Together with `AUTOFIX_SKIP_MAX_DELETIONS`, defines the max diff size that auto-qualifies for the skip path **regardless of file types**. The gate skips reviewer panel + editor when total additions ≤ this value AND total deletions ≤ `AUTOFIX_SKIP_MAX_DELETIONS` (both bounds simultaneously). Totals are read from the `additions` and `deletions` fields on the existing `GET /repos/{repo}/pulls/{n}` response — no separate `/files` call is made on this branch. The size-threshold branch is OR-ed with the doc-only branch (`AUTOFIX_SKIP_DOC_ONLY`) — a small-but-code change still skips, accepting that risk in exchange for cycle-time savings on trivial fixes. Setting this value to `0` does **not** fully disable the branch — `additions ≤ 0` still matches a PR with zero additions (and `0/0` diffs do occur in edge cases like metadata-only renames or whitespace-only no-op pushes). To effectively suppress the size-threshold branch, set both this and `AUTOFIX_SKIP_MAX_DELETIONS` to `-1` so no non-negative addition/deletion count can satisfy the bound; alternatively rely on the per-PR `force-review` override. Per-PR override: `[force-review]` title marker or `force-review` label. Must be an integer; on parse failure the bash arithmetic `[ X -le Y ]` test fails which evaluates as "not small" (full review runs — fails open). |
| `AUTOFIX_SKIP_MAX_DELETIONS` | No | `10` | review_autofix | Deterministic pre-review skip — size-threshold branch (deletions). See `AUTOFIX_SKIP_MAX_ADDITIONS`; both bounds must be satisfied for the size-threshold branch to fire. Setting this value to `0` does **not** fully disable the branch — `deletions ≤ 0` still matches a 0-deletion PR. Set to `-1` (together with `AUTOFIX_SKIP_MAX_ADDITIONS=-1`) to suppress the size-threshold branch entirely. Per-PR override: `[force-review]` title marker or `force-review` label. Must be an integer; fails open to "not small" on parse failure. |
| `AUTOFIX_CONTINUATION_ENABLED` | No | `true` | review_autofix | When `true` (the default), the `Re-trigger review via workflow_dispatch` step in `review_autofix.yml` proceeds to dispatch the next autofix iteration via `workflow_dispatch` after a **productive** `[ai-autofix]` commit (`DID_COMMIT=true` AND `LEDGER_ONLY_COMMIT!=true` AND `CONFLICT_RESOLVED!=true`). This closes the ~0–120 min idle window where an `[ai-autofix]` push would otherwise wait for the orchestrator stall cron (which does not scan non-orchestrator PRs at all). Ledger-only commits (§20.3) still route to the clean-review tail in the same run — no continuation dispatch is issued. Conflict-resolved commits keep their pre-continuation dispatch path. Set to `false` to restore the pre-continuation behaviour where `AUTOFIX_SKIP_SELF_TRIGGERED` alone gated productive autofixes out of the dispatch step. `workflow_dispatch` bypasses the gate job's self-triggered skip by design — continuation is a first-class successor run, not a redundant verification. Pre-dispatch guard: settle delay (`AUTOFIX_CONTINUATION_SETTLE_SECS`). Iteration-cap handling remains in the dispatched run's `retrigger_guard` path (which gates reviewers/editor and routes exhaustion to the review-blocked judge). Alerts: the continuation path is silent (no Telegram); stall-cron `Stall recovery: re-triggered review …` alerts are unchanged and still fire only for genuine orchestrator-tracked stalls. Continuation dispatches **bypass** the post-commit peer-dedup (`autofix_retrigger_has_inflight_peer`) because the only same-branch peer is the gate-skipped self-triggered synchronize run, which cannot be a successor — leaving dedup enabled for continuation would stall non-orchestrator PRs that the stall cron does not scan. Legacy non-continuation dispatches and the `editor-changes-lost` retrigger retain peer-dedup. Audit via `AUTOFIX_CONTINUATION_DISPATCH_ISSUED` / `AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected ... continuation=true` / `AUTOFIX_PEER_CHECK_BYPASSED reason=continuation_dispatch` / `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix continuation_enabled=<val>` log lines. See probably_unnecessary_but_read_if_stuck.md §20.4 for the contract. |
| `AUTOFIX_CONTINUATION_SETTLE_SECS` | No | `10` | review_autofix | Seconds the continuation path `sleep`s between the push and the `workflow_dispatch` call, to let GitHub's internal indices catch up before the dispatched run checks out the new HEAD SHA. Integer in `1..60`; invalid or out-of-range values clamp to `10`. Not applied to the conflict-resolved dispatch path (that keeps its existing `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` peer-wait). |
| `ENABLE_REVIEWER_TWO_PASS` | No | `true` | review_autofix | When true, reviewers run two passes per iteration: pass 1 at `medium` reasoning (broad sweep), then pass 2 at the scheduled reasoning level with a cross-pollination summary of pass 1 findings. Set to `false` to use a single pass at the scheduled reasoning level. |
| `XPOLL_SUMMARISER_MODEL` | No | `openai/gpt-5.4-mini` | review_autofix | Model slug (resolved through codex-cli's OpenRouter provider) used by `scripts/summarize_reviewer_consensus.sh`. After each review pass finishes, this model consolidates every reviewer's output into one ledger: a `=== CONSENSUS FINDINGS ===` block with cross-reviewer dedup (entries carry `flagged_by: [slug, ...]`) followed by per-reviewer sections. The pass-1 ledger feeds pass-2 reviewers; the pass-2 ledger is written to `REVIEWER_CONSENSUS_FILE` and feeds the editor + memory-record step. |
| `XPOLL_SUMMARISER_REASONING` | No | `none` | review_autofix | Reasoning effort (`xhigh` / `high` / `medium` / `none`) applied to the summariser model via its isolated `CODEX_HOME` config.toml. Default is `none` — summarisation is an execution task and higher efforts on `gpt-5.4-mini` have been observed to produce rc=0/empty-stdout responses that exhaust the retry budget and the 180-min job timeout. Isolated config guarantees the override cannot leak into the editor's codex-cli invocation. |
| `XPOLL_SUMMARISER_LINES_PER_REVIEWER` | No | `160` | review_autofix | Target max per-reviewer section lines; summariser is told to collapse related findings (`(N related items)` suffix) rather than drop them when over-budget. Overall ledger target is this value × reviewer count + 120. |
| `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS` | No | `2400` | review_autofix | Per-attempt wall-time timeout for a single codex-cli summariser invocation. Raised from `1200` after observing repeat 20-min timeouts on `xhigh`-reasoning pass-1 calls over ~24 KB prompts burning ≥40 min of runner time per run before finally succeeding on attempt 3. On timeout / non-zero exit / empty stdout the summariser retries up to 10 times with exponential backoff (5s, 10s, 20s, 40s, 80s, 160s, 320s, 640s, 1280s between attempts; no cap), then hard-fails the workflow (the job-level "Telegram failure" step surfaces the incident). The PR-closed sentinel is polled every 2s during each backoff so a mid-retry PR close exits cleanly without waiting out the remaining delay. |
| `XPOLL_SUMMARISER_MAX_INPUT_LINES` | No | `3000` | review_autofix | Pre-truncation ceiling per reviewer output before concatenation into the summariser prompt. Prevents a pathological reviewer output from blowing the summariser's context budget. |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | No | `true` | review_autofix | When true, non-orchestrator PRs that exhaust autofix iterations invoke a judge (LLM) to decide: merge as-is, push a fix commit, or close and reissue. Orchestrator-managed PRs are skipped (handled by the poller). PRs without linked issues use the PR title/body as requirement context. |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | No | `medium` | review_autofix | Reasoning effort for the review-blocked judge in non-orchestrator PRs (`xhigh`, `high`, `medium`, `none`). |
| `MAX_REVIEW_BLOCKED_RETRIES` | No | `2` | review_autofix, orchestrate_poll | Maximum judge retries for review-blocked PRs before forcing a final decision (merge or close+reissue). Used by both the review_autofix judge (counts `[judge-fix]` commits) and the orchestrator poller. |
| `ENABLE_VALIDATION` | No | `true` | orchestrate_poll | When true, a `complete` judge verdict transitions the tracking issue into runtime validation (`ai:validating`) and completion occurs only after validation passes. |
| `MAX_VALIDATE_CYCLES` | No | `3` | orchestrate_poll | Maximum runtime validation cycles (initial run + fix/revalidate loops) before forcing `ai:validation-failed`. |
| `MAX_SELF_HEAL_ATTEMPTS` | No | `2` | validate | Maximum in-process self-heal attempts per `validate_process.sh` invocation. Self-heal patches one of the four validation prompt files locally and re-execs the pipeline, and does NOT increment `MAX_VALIDATE_CYCLES`. Set to `0` to disable. See [Validation self-healing](#validation-self-healing). |
| `VALIDATE_PREFLIGHT_PYFLAKES_ENABLED` | No | `true` | validate | When `true`, the preflight phase of `scripts/validate_process.sh` runs `pyflakes` and `ruff check --select $VALIDATE_PREFLIGHT_PYFLAKES_RULES` against every quoted `python3 - <<'PY' ... PY` heredoc body under `validation/**/*.sh`. Catches undefined-name (F821) / unused-import / redefinition bugs that `ast.parse` alone cannot see and that runtime tests miss when the bug lives in an unexercised conditional branch (observed as `unknown_error:NameError` in consumer-repo autobet finalize logs). Missing tools are auto-installed via `python3 -m pip install --user`; install failure fails open with a `::warning::` and skips the check. Invalid values are coerced to `true`. |
| `VALIDATE_PREFLIGHT_PYFLAKES_RULES` | No | `F` | validate | Ruff rule selector passed to `ruff check --select`. Default `F` covers all pyflakes-equivalent rules (F401 unused import, F811 redefinition, F821 undefined name, F823 local-before-assign, F841 unused local, etc.). Must match `^[A-Z0-9,]+$`; invalid values fall back to `F`. Narrow to `F821` if operator wants only the NameError bug class to block. |
| `VALIDATE_WORKFLOW_NAME` | No | `ai-validate.yml` | orchestrate_poll | Workflow filename to dispatch for runtime validation. Override to `internal-validate.yml` for repos using the internal naming convention. Falls back to `internal-validate.yml` automatically if the primary name fails. |
| `MAX_JUDGE_CYCLES` | No | `25` | orchestrate_poll | Maximum judge evaluation cycles per project before forcing failure. Prevents infinite fix-up loops when the judge repeatedly returns `in_progress`. **Orchestrator final-PR bypass:** when `ORCH_PR_AUTOFIX_FLOW_ENABLED=true` (default) and the integration→default-branch final PR is open with `final_merge_status=pending`, this cap is bypassed for the final-PR loop only — the loop runs unlimited 3-autofix→judge cycles until the PR is mergeable. The cap remains in force for sub-issue stalls, recovery loops, and the intermediate-PR phase (per-sub-issue judge runs are governed by `MAX_REVIEW_BLOCKED_RETRIES` inside `review_autofix.yml`, not by this orchestrator-level counter). The bypass emits a `[final-merge] judge cap bypassed` log line each time it fires. See [Orchestrator PR autofix flow](#orchestrator-pr-autofix-flow). |
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
| `STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:review-blocked` phase. Past this threshold the poller dispatches `review_rb_judge_dispatch.yml` (which runs `review_autofix.yml` with `force_rb_judge=true`) so `scripts/review_rb_judge.sh` decides merge, fix, or close-and-reissue. Review-blocked was previously a dedicated-handler phase with no standalone trigger; issues stamped `ai:review-blocked` by a review/autofix workflow failure that never reached the inline judge had no autonomous escape path. Matches the `ai:done` threshold so genuinely long in-flight autofix runs are not double-dispatched. |
| `MAX_STALL_RECOVERIES_PER_ISSUE` | No | `5` | orchestrate_poll | Maximum stall recovery attempts per individual issue. Recovery selection uses `stall_recovery_count` against `STALL_RECOVERY_ACTIONS` (clamped to the last action), with optional escalation to `run_stall_judge` when enabled and the trigger count is reached. After exhausting this limit the issue is skipped (`ai:closed`) so the wave can advance; the judge evaluates the gap at wave completion. |
| `STALL_JUDGE_TRIGGER_COUNT` | No | `2` | orchestrate_poll | Stall recovery attempt threshold at which the poller escalates from declarative ladder actions to `run_stall_judge` for deeper diagnostics and action selection. |
| `ENABLE_STALL_JUDGE` | No | `true` | orchestrate_poll | Enables/disables stall-judge escalation (`run_stall_judge`) in orchestrator-managed and standalone stall recovery paths. |
| `ENABLE_STALL_HUMAN_TERMINALIZATION` | No | `false` | orchestrate_poll | Allow terminal `escalate_human` stall actions; when `false`, both declarative and judged stall actions downgrade `escalate_human` to the nearest prior non-human phase action. |
| `ENABLE_STANDALONE_STALL_RECOVERY` | No | `true` | orchestrate_poll | Enable stall detection and auto-recovery for standalone AI issues (issues not managed by an active orchestrator tracking state). |
| `ENABLE_CLOSE_MERGED_ISSUES` | No | `true` | orchestrate_poll | Enable the per-cycle sweep that closes any open GitHub issue carrying `ai:merged` OR `ai:ready-to-merge` once at least one cross-referenced PR is verified merged via the issue timeline helper (`gh_issue_timeline_with_cross_refs`, GraphQL-first with fail-open REST fallback). Applies to both orchestrator-managed child issues and standalone (non-orchestrator) issues. Tracking issues (`ai:orchestrator-tracking`) are intentionally skipped — they are closed by the orchestrator project completion path. The two label-origin classes have different alerting policies (renames are breaking per §6 — log prefixes embed `origin=merged_label` / `origin=ready_label`): **`merged_label` origin** — if no merged PR can be verified on the timeline, the sweep leaves the issue open and sends a Telegram `WARNING` alert (the `ai:merged` label is a strong signal something is wrong if the merged-PR claim cannot be substantiated). **`ready_label` origin** (added 2026-04-27 as the defensive backstop for prior-wave children that the orchestrator's `reconcile_managed_issue_labels` never promoted because the wave moved past them) — if no merged PR is found, this is the normal pending state of an in-flight auto-merge, so the sweep exits silently with NO alert; if a merged PR IS verified, the sweep backfills `ai:merged` (and strips `ai:ready-to-merge`) BEFORE close, so concurrent readers (wave-status resolver, validation fix-up loop, lineage finalizer) see the same terminal label that the merged_label-origin path always produced. API hygiene (§15): up to 2 `gh issue list` calls per sweep (one per label class) regardless of N issues; per-issue cost is unchanged for the merged_label-origin branch and adds one `gh issue edit` to backfill `ai:merged` for the ready_label-origin closure path. |
| `ENABLE_STALL_MERGED_PR_GUARD` | No | `true` | orchestrate_poll | Before firing an early-phase stall recovery command (`retrigger_pipeline`, `auto_respond_clarify`, `retrigger_plan`, `auto_approve`, `retrigger_implement`), double-check the issue's linked pull request state. If the most recent linked PR is `MERGED`, the command is **not** posted: the issue is tagged `ai:merged` (so `close_merged_issues_sweep` closes it on the next cycle), a healing note is added, and a Telegram `WARNING` is sent. Applies to both the orchestrator-managed stall loop and the standalone stall watchdog. In the steady-state path, linked-PR state is prefetched in batched GraphQL calls (`_fetch_linked_pr_status_graphql` for managed, extended `_fetch_candidate_issue_details_graphql` for standalone) — the managed-path prefetch runs **unconditionally** whenever there are stalled issues, so the pre-existing open-PR sub-guard also benefits from the batched cache regardless of this flag. On cache/prefetch miss both paths fall back to a per-issue REST probe (timeline + PR payload) for the merged-PR sub-guard, and the managed path's open-PR sub-guard also falls back to the legacy per-issue REST lookup — so this is not a strict "0 extra per-issue API calls" path when GraphQL is unavailable. Introduced to prevent the `/reclarify` loop on issues whose phase label got stripped after merge (see GH issue #1074). Set to `false` to disable only the merged-PR short-circuit (the open-PR sub-guard and the batched prefetch still run). |
| `MAX_RECOVERY_ATTEMPTS` | No | `3` | orchestrate_poll | Maximum project-level recovery cycles when the judge declares failure. Replaces the previous single-shot `recovery_attempted` boolean with a configurable counter. |
| `JUDGE_REPEAT_FINGERPRINT_MAX` | No | `2` | orchestrate_poll | Circuit-breaker cap on consecutive identical normalized judge-failure fingerprints. When the same normalized failure repeats more than this threshold, the poller escalates to `ai:blocked` and stops spawning additional judge-driven recovery cycles for that project. Operates independently of `MAX_RECOVERY_ATTEMPTS`. |
| `MAX_VALIDATION_RECOVERY_ATTEMPTS` | No | `2` | orchestrate_poll | Maximum times the poller transitions a validation-failed project back to the judge for re-evaluation before marking it as terminally failed. Set to `0` to disable (immediate terminal failure on first validation failure, matching pre-recovery behavior). |
| `MAX_FINAL_MERGE_ATTEMPTS` | No | `3` | orchestrate_poll | Bounded retry budget for the post-validation final integration→default squash merge inside `mark_validation_complete`. Poll ticks that hit *blocking* final-merge failures increment `final_merge_attempt_count`; transient "not ready yet" conditions (for example mergeability still computing or required checks still pending) defer without consuming this budget. On success the counter is reset. After the budget is exhausted the project is escalated to `ai:blocked` (tracking issue label + state `failed` + CRITICAL Telegram alert) instead of being silently advanced to `status=complete`. Must be a positive integer; invalid values fall back to `3`. |
| `MAX_VALIDATION_FIX_BATCH_CYCLES` | No | `30` | orchestrate_poll | Maximum poll cycles a single validation fix-up batch (the set of issue numbers extracted from the most recent `## 🧪 Runtime validation found fixable issues` tracking comment) can sit in "still in progress" before the poller escalates via `mark_validation_failed` — which still honours `MAX_VALIDATION_RECOVERY_ATTEMPTS` for judge re-evaluation. Counter resets when a new fix-issues comment arrives, when the batch completes (all issues merged), or when `mark_validation_failed` clears the active list. Each fix-up issue is now also inspected for its live GitHub `state`/`state_reason`, so a fix-up issue closed without the `ai:closed` label is detected in the same poll cycle instead of stalling until this ceiling trips. Open fix-up issues at `ai:ready-to-merge` are additionally inspected for a merged linked PR via the same timeline-cross-reference helper used for the closed-issue backfill (`validation_fix_issue_has_merged_pr_evidence`). When found, `backfill_validation_fix_issue_merged_label` flips the issue to `ai:merged` in the same iteration, and the per-tracking-issue cycle's `close_merged_issues_sweep` closes the issue at the tail of the same poll. This eliminates the up-to-`STALL_THRESHOLD_MINUTES` delay between auto-merge firing on the linked PR and the orchestrator-managed sub-issue advancing — previously the consumer-side `pull_request.closed` handler (`.github/workflows/issue_pr_status.yml:253–323`) skipped orchestrator-managed children to preserve the anti-#1469 guard, leaving stall recovery as the only path to `ai:merged`. The proactive check fires only when the fix-up issue carries `ai:ready-to-merge`; every other open phase continues to short-circuit with no API round-trip. Fail-open on any timeline-lookup or label-edit transient failure (next cycle retries). |
| `MAX_IMPL_NOOP_REISSUES` | No | `2` | orchestrate_poll | Maximum automatic re-issues for an `ai:implementation-failed` issue before the poller closes it as likely already implemented and defers final verification to the wave-completion judge. Must be a positive integer; invalid values fallback to `2`. A belt-and-braces `count_noop_ancestors` walk of the `Re-issued from #N` chain (same cap) runs in parallel with the state-based counter in all three poller re-issue paths (`execute_stall_recovery_action close_and_reissue`, `run_standalone_stall_recovery close_and_reissue`, and the `no-op-implementation` branch of the `ai:implementation-failed` sweep); either signal trips closure. This catches the failure mode where the state-based counter is stale — e.g. the tracking-issue state comment was truncated or the wave iterator never refreshed `get_impl_noop_count` — which caused tracking issue #1292 to spawn 30+ duplicate sub-issues in ~5 hours. API cost: up to `2 * MAX_IMPL_NOOP_REISSUES` calls per invocation, fail-open on any API error. |
| `IMPL_NOOP_ANCESTRY_THRESHOLD` | No | `2` | implement | Ancestor-chain no-op cap enforced inside `.github/workflows/implement.yml`'s "Handle no-op implementation" step. When a commit produces zero changes, the step walks up `Re-issued from #N` markers up to this many hops and counts how many ancestors posted the `produced no repository changes` warning comment. At or above the threshold the issue is closed with `ai:closed` and the wave-completion judge is deferred to, rather than labeling `ai:implementation-failed` and letting the poller spawn another re-issue. Must be a positive integer; invalid values fall back to `2`. Complements — does not replace — the poller-side `MAX_IMPL_NOOP_REISSUES` cap. |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | No | `3` | implement | Maximum in-job post-Codex syntax-repair attempts after `Validate syntax of changed files` fails. Must be a non-negative integer (`0` disables in-job repair); invalid values fallback to `3`. |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | No | `900` | orchestrate_poll | Minimum seconds between consecutive review/autofix dispatches against the same orchestrator integration-branch final PR. Prevents the self-healing loop from re-dispatching the resolver every poll tick while a previous run is still in flight. |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | No | `3` | orchestrate_poll | Circuit-breaker budget for automated integration-branch conflict resolution. The self-healing path attempts the `main -> integration_branch` sync via GitHub's merges API; on an HTTP 409 conflict, the poller dispatches `_dispatch_review_for_conflicts` for the final integration PR. After this many consecutive unresolved ticks, the orchestrator escalates to the judge with full PR context; if the judge escalation itself fails the project is marked terminally failed. Applies to **non**-`orchestrator/project-*` integration branches; sync conflicts on orchestrator-owned integration branches use the tighter `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` instead. |
| `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` | No | `1` | orchestrate_poll | Tighter circuit-breaker budget applied **only** when the integration branch head ref matches `orchestrator/project-*`. The first-line conflict resolver (`prompts/conflict-resolver.txt`) lacks built-in awareness of merged sub-issue intent, so the safest default is to escalate to the integration judge after a single resolver shot rather than burn three dispatches that may "succeed" textually while silently dropping a merged sub-issue's work. Set to a higher value to give the resolver more attempts; set to `0` to skip the first-line resolver entirely and escalate to the judge immediately. Non-orchestrator integration branches continue to honour `INTEGRATION_CONFLICT_MAX_RETRIES`. See "Integration-sync intent fingerprints" below. |
| `INTEGRATION_CONFLICT_LIFETIME_MAX` | No | `10` | orchestrate_poll | Cumulative cap on the **total** number of resolver+judge dispatches per integration branch across all retry episodes. Unlike the per-burst counters above (which reset to `0` after each judge escalation in `heal_integration_branch_conflict`), this counter is additive across the lifetime of the tracking-issue state and only zeros when that state is rebuilt. When `integration_conflict_total_dispatches >= INTEGRATION_CONFLICT_LIFETIME_MAX`, the heal function flips `status=failed` + `final_merge_status=failed` + `integration_sync_status=failed`, posts a `❌ Integration self-healing capped` tracking-issue comment, fires a CRITICAL Telegram alert, and stops dispatching. Catches the alternating resolver/judge loop where each judge invocation resets `unresolved_ticks=0` but the merge stays dirty as `main` keeps moving (observed on `orchestrator/project-1479` PR #1533, 2026-04-25, 8 fingerprint-FAILED annotations across a single 2h47m run + many such runs). Must be a positive integer; invalid values fall back to `10`. |
| `FINGERPRINT_PER_FILE_CAP` | No | `12` | orchestrate_poll | Maximum number of `must_contain` / `must_not_contain` regex patterns the orchestrator captures per file per direction when a sub-issue PR merges into an integration branch. Higher values give the integration-sync conflict verifier finer-grained intent coverage at the cost of larger state-comment payloads and longer verification runs. |
| `FINGERPRINT_MIN_PATTERN_CHARS` | No | `12` | orchestrate_poll | Minimum trimmed-line length for a fingerprint pattern. Lines shorter than this are skipped during capture (too generic to fingerprint reliably). |
| `REVIEW_BLOCKED_AUTO_UNSTICK` | No | `true` | orchestrate_poll | Before invoking the review-blocked judge, the poller inspects each `ai:review-blocked` PR. If the PR is `mergeable=false` it dispatches `review_autofix.yml` (via `_dispatch_review_for_conflicts`) so the in-workflow Codex resolver gets a fresh shot at the conflict, and skips the judge for this tick. If the PR head commit was authored by an **external** identity (anything other than `codex`, `codex-bot`, `github-actions`, or `github-actions[bot]`), the poller also dispatches the review workflow AND clears `ai:review-blocked`, re-entering the normal phase loop — this bridges the GitHub platform rule that suppresses `pull_request.synchronize` events on commits pushed with the default `GITHUB_TOKEN` (Claude Code on the web, custom wrapper actions) and matches the "push a new commit to re-trigger the review workflow" contract printed in the workflow-failure comment. Set to `false` to disable both paths and force the judge-first flow. Dispatch is always gated by the existing `_dispatch_review_for_conflicts` cycle-local dedup and active-run detection, so repeat calls are cheap no-ops. |
| `TG_ADMIN_CHAT_ID` | No | — | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Telegram chat ID for notifications (pair with `TG_BOT_SECRET`) |
| `ALERT_MSG_LEVEL` | No | `DEBUG` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, issue_pr_status, update_workflows, test-and-mark-stable | Minimum Telegram alert level to send. Alerts below this threshold are suppressed. Valid values: `DEBUG`, `WARNING`, `ERROR`, `CRITICAL`. Each alert is prefixed with an icon and level (e.g. `🔍 DEBUG:`, `⚠️ WARNING:`, `❌ ERROR:`, `🚨 CRITICAL:`). New alerts default to `CRITICAL` until explicitly recategorised. |
| `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` | No | `3600` | all (any workflow or script that sources `scripts/gh_helpers.sh`) | Minimum seconds between consecutive admin Telegram alerts when a GitHub API rate limit is hit. The alert (`⚠️ WARNING: GitHub API rate limit hit …`) is fired from inside the rate-limit branch of `gh_retry` / `gh_retry_to_file` / `gh_api_json_to_file` / `curl_gh_api`, and is throttled globally via a Telegram pinned message in the admin chat (marker `<!-- gh_rl_ts:EPOCH -->`). This deliberately avoids any GitHub API call for dedup state so the throttle keeps working while the GitHub API itself is the resource being limited. Fail-closed: on Telegram pin failure the sent message is rolled back so the "≤ 1 alert per window" invariant holds. Set to `0` has no suppression effect (any non-numeric or empty value is coerced to `3600`). No-op when `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID` are unset. |
| `OPENROUTER_PROMPT_CACHE_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate | Kill switch for OpenRouter prompt-cache instrumentation. `false` enables cache-friendly prompt ordering and cache telemetry logging; `true` disables explicit cache breakpoints and related instrumentation. (No longer consumed by `workflow-log-analysis`, which is Codex-only.) |
| `WORKFLOW_ORCHESTRATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | orchestrate, orchestrate_poll | Model override for orchestrator decomposer and judge |
| `ORCHESTRATE_POLL_INTERVAL` | No | `30` | orchestrate | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_SHORTCIRCUIT_MAX_CHARS` | No | `1200` | _(deprecated — no longer consumed)_ | Formerly controlled the pre-LLM short-circuit. Removed in #1163; every orchestrator run now goes through full decomposition. |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | No | `ai-orchestrate-poll.yml` | orchestrate_poll | **Deprecated no-op.** Previously the filename of the caller wrapper workflow the poller would self-dispatch at the end of each run. The self-retrigger path (and its associated cooldown sleep and rate-limit circuit breaker) was removed; polling is now driven exclusively by the wrapper workflow's cron schedule. The variable and the matching `caller_workflow` input on `orchestrate_poll.yml` are retained for backward compatibility with existing wrappers and are ignored at runtime. |
| `ORCHESTRATE_POLL_WORKFLOW_FILE` | No | `internal-orchestrate-poll.yml` | review_autofix (resolver-bail dispatch) | Filename of the orchestrator-poller workflow the resolver script's EXIT trap (`_dispatch_integration_judge_now` in `scripts/review_conflict_resolve.sh`) targets via `gh workflow run` when an integration-sync resolver attempt fails. Default matches the workflow-source repo (`coding-workflows`). Consumer repos that ship the poller under a different filename should set this so the immediate-judge dispatch resolves correctly. Fail-open: any dispatch failure logs `::warning::` and falls through to the `*/5` cron tick (≤5 min lag), so the variable being misset on a consumer repo never blocks unattended escalation. |
| `EDITOR_IDLE_TIMEOUT` | No | `1200` | review_autofix, implement | Editor watchdog idle timeout in seconds. The editor is killed if it produces no output for this long and has no active network connections. |
| `EDITOR_MAX_WALL` | No | `7800` | review_autofix, implement | Maximum wall-clock seconds per editor attempt (~130 min). Budget-aware: auto-capped to remaining job time minus a 2-min buffer. The 3-hour job cap typically allows about one full-length attempt; retries are only possible when earlier attempts finish well under the wall cap. A watchdog kill near the 130-min limit consumes most of the remaining job budget, so undersizing per-issue scope (the orchestrator's 60-minute target) is mandatory. |
| `EDITOR_MIN_ATTEMPT_SECS` | No | `300` | review_autofix | Minimum remaining job budget (seconds) required to start an editor attempt. Prevents futile retries near the job deadline. |
| `REVIEW_PR_STATE_POLL_INTERVAL_SECS` | No | `10` | review_autofix | Sleep interval in seconds for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; the GitHub PR-state API check runs every 9 polls (default ~90s). Must be an integer in `10..3600`; invalid or out-of-range values emit `rate_limit_audit_fallback` warning and fail open to `10`. |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | No | `3` | implement | Maximum in-job post-Codex syntax-repair attempts after `Validate syntax of changed files` fails. Must be a non-negative integer (`0` disables in-job repair); invalid values fallback to `3`. The repair loop runs only for syntax-validator failures, enforces an allow-list scope guard, and then falls back to the existing diagnose/fix-up path when attempts are exhausted. |
| `BULK_DELETE_THRESHOLD` | No | `3` | implement | Maximum number of file deletions allowed in a single AI implementation commit before the destructive-commit guard blocks it. Set higher for legitimate large refactors, or bypass on a per-run basis via `ALLOW_BULK_DELETE=true`. See "Destructive-commit guard" below. |
| `ALLOW_BULK_DELETE` | No | `false` | implement | When `true`, the destructive-commit guard ignores the `BULK_DELETE_THRESHOLD` rejection path. Canonical workflow-source file deletions are still blocked unless `ALLOW_WORKFLOW_EDITS=true`. Use for legitimate large refactors approved by a human. |
| `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS` | No | `30` | workflow-log-analysis | Age (in days) above which dated `analysis/workflow-optimization-<date>.md` reports are git-removed in the same commit as a new report. Filename date stamps are authoritative; the just-written report is always preserved. Invalid values fail open to `30` with a warning. |
| `BATCH_API_DISABLED` | No | `false` | memory_maintenance | Deprecated compatibility variable. The active workflow-log-analysis batch path was removed (the workflow is now Codex-only). `memory_maintenance.yml` still reads this var and echoes it in a single `batch_noop` log line so external log scrapers that grep for `batch_*` events keep working; the value does not change any current behaviour. |
| `BATCH_API_PROVIDER` | No | `auto` | memory_maintenance | Deprecated compatibility variable. Same status as `BATCH_API_DISABLED` — only surfaced in `memory_maintenance.yml`'s `batch_noop` log line for backward-compatible telemetry. |
| `BATCH_API_POLL_TIMEOUT_HOURS` | No | `24` | memory_maintenance | Deprecated compatibility variable. Same status as `BATCH_API_DISABLED` — only surfaced in `memory_maintenance.yml`'s `batch_noop` log line for backward-compatible telemetry. |
| `ALT_EDITOR_MODEL` | No | `openai/gpt-5.3-codex` | test-and-mark-stable (`e2e-alt-model-test` job) | Model used by the release-gate's alternate-model happy-path run. Pinned to a different model variant than the production `WORKFLOW_EDITOR_MODEL` default (`openai/gpt-5.4` for `implement.yml`) so model-coupling regressions surface before stable is tagged. The legacy `gpt-5.3-codex` is the natural alternate (still listed in the catalog with `priority: 3`) — flipping editor and alt swaps which model is treated as the canary. The override is propagated to the implement run by parsing it out of the smoke issue body in `implement.yml`'s "Detect smoke test" step (gated on the `[E2E Smoke Test alt-model]` title sub-tag). Override per release if you need to test against a specific candidate; the chosen model is recommended to appear in `scripts/codex_model_catalog.json` so codex can register `apply_patch_tool_type` for it (the dispatcher only warns when it doesn't — codex may still resolve metadata via bundled `models.json` or remote provider metadata, but reliability degrades on the openai/gpt-5.3-codex slug per `openai/codex#11151`). Has no effect outside the release gate. |
| `E2E_ALT_MODEL_ENABLED` | No | `true` | test-and-mark-stable (`e2e-alt-model-test` job) | External-dependency opt-out. Set to `false` to skip the alt-model job when the upstream OpenRouter model (selected via `ALT_EDITOR_MODEL`) is temporarily unavailable, deprecated, or your API key lacks access. The validate gate accepts `skipped` as a pass for this job, so flipping the flag unblocks releases when the orthogonal alt-model dependency is the only thing failing. Re-enable once the upstream is healthy. |
| `LOG_ANALYZER_MODEL` | No | `openai/gpt-5.4-mini` | test-and-mark-stable (Phase 8 soft-error analyser) | Lightweight model used by the release-gate post-run log analyser (`scripts/analyze_soft_errors.py`) to summarise soft failures (rate-limit recoveries, codex fallbacks, summariser hard-fails, editor no-ops) into the Telegram release notification. Non-blocking; analyser failures fall back to a stub report rather than failing the gate. The script collects logs from every phase run (clarify, plan, implement, review_autofix, orchestrate_poll, cancel_on_pr_close), filters to soft-error candidates, truncates per-run to 40K chars, and emits a markdown report whose first line carries a parseable status code (`ok` / `no_runs` / `api_skipped` / `call_failed` / `analyser_empty`). The full report is uploaded as the `soft-error-report-${run_id}` workflow artifact and a truncated copy is appended to the Telegram release message. |
| `LOG_ANALYZER_REASONING` | No | `none` | test-and-mark-stable (Phase 8 soft-error analyser) | Reasoning effort for `LOG_ANALYZER_MODEL`. `none` is the default — log summarisation is an execution task. Set to `medium` for deeper triage or `high` for maximum signal; values must match what the chosen model accepts. |

**Thinking levels** — control the model's reasoning effort per phase. Valid values: `xhigh`, `high`, `medium`, `none`. Phases default to `medium` (research/analysis tasks) or `none` (execution tasks) — see the table below. No cycle-based downgrades are applied — every phase uses the configured reasoning effort for all cycles. **E2E smoke test exception:** when an issue or PR title contains `[E2E Smoke Test]`, the clarify, plan, reviewer, and editor phases force `none` reasoning to keep smoke runs cheap and fast. The implement phase already defaults to `none`. The review-blocked judge is not overridden and retains its configured reasoning level.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `THINKING_LEVEL_CLARIFY` | `medium` | clarify | Reasoning effort for the clarification phase |
| `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` | `medium` | clarify | Reasoning effort used only when clarify runs Codex for `ai:orchestrator-managed` issues on forced human `/reclarify` |
| `THINKING_LEVEL_PLAN` | `medium` | plan | Reasoning effort for the planning phase |
| `THINKING_LEVEL_IMPLEMENT` | `medium` | implement | Reasoning effort for the implementation phase |
| `THINKING_LEVEL_ANALYSIS` | `high` | workflow-log-analysis | Reasoning effort for the API-redundancy Codex pass (passed via Codex `model_reasoning_effort`). The deep-audit pass no longer reads this var — its reasoning is hardcoded at `high` in the workflow YAML to keep it inside the per-job timeout budget; edit the hardcoded value in `workflow-log-analysis.yml` if you need to override it. |
| `THINKING_LEVEL_REVIEWER` | `medium` | review_autofix | Reasoning effort for the reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `medium` | review_autofix | Reasoning effort for the editor model (applying fixes) |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `medium` | review_autofix | Reasoning effort for the review-blocked judge (non-orchestrator PRs) |
| `THINKING_LEVEL_ORCHESTRATE` | `medium` | orchestrate | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `medium` | orchestrate_poll | Reasoning effort for judge evaluation |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `medium` | orchestrate_clarify_respond | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `medium` | validate | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `medium` | orchestrate_poll, review_autofix | Reasoning effort for the Codex-based merge conflict resolver (orchestrator integration-sync runs and review_autofix's post-editor resolver step) |
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
> (`internal-orchestrate-poll.yml`, `*/5 * * * *`) re-dispatches via
> `workflow_dispatch` — which bypasses the skip — so the worst-case delay
> between a missed verification and automatic recovery is ~5 min. Log
> prefixes `AUTOFIX_GATE_SKIP`, `AUTOFIX_GATE_NO_SKIP_IDENTITY`,
> `AUTOFIX_GATE_SKIP_QUERY_FAILED`, and
> `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix` are stable audit
> handles. Set `vars.AUTOFIX_SKIP_SELF_TRIGGERED=false` to opt out, or set
> `vars.AUTOFIX_BOT_LOGIN` to override the expected bot login.
>
> **Mid-run external-push gates** — The self-triggered skip above catches
> *post-run* autofix events. The companion *mid-run* gates (`probably_unnecessary_but_read_if_stuck.md §20.2`)
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
> updates `REVIEW_LEDGER_PATH` (default
> `.ai/review_issue_ledger/pr-<PR_NUMBER>.txt`) on every review pass,
> including passes where the editor reports
> `Change status: not-edited`. In the default configuration the per-PR
> ledger path is gitignored and persisted across autofix iterations via
> `actions/cache` in `review_autofix.yml` — the ledger is updated locally
> for the run but never part of the commit/push, so this bug cannot
> manifest. The ledger-only commit scenario only applies when
> `REVIEW_LEDGER_PATH` is explicitly overridden to a Git-tracked (or
> force-added) path. When the resulting `[ai-autofix]` commit
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

**`.github/workflows/review_rb_judge_dispatch.yml`** — Stall-recovery dispatch wrapper for `ai:review-blocked` PRs. Filename is load-bearing: the orchestrator poller dispatches it by exact name (`gh workflow run review_rb_judge_dispatch.yml`) when an issue stalls past `STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES` with no active autofix run, so the `ai-*` prefix is intentionally omitted. Calls `review_autofix.yml` with `force_rb_judge=true`; the reviewer/editor/commit path is short-circuited and only `scripts/review_rb_judge.sh` runs (decides merge / fix / close-and-reissue). Auto-deployed by `ai-update-workflows.yml`; consumer repos that lack this wrapper will see the poller log `::warning::Could not dispatch review_rb_judge_dispatch.yml` and the `ai:review-blocked` phase will have no autonomous escape path.
```yaml
name: AI Review-Blocked Judge Dispatch
on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number to run the review-blocked judge against"
        required: true
        type: string
      allow_workflow_edits:
        description: "Allow AI/editor changes to .github/workflows files (default false for judge-only dispatch)"
        required: false
        default: false
        type: boolean
permissions:
  contents: write
  pull-requests: write
  issues: write
jobs:
  judge:
    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable
    with:
      pr_number: ${{ github.event.inputs.pr_number }}
      pr_is_draft: false
      pr_title: ""
      pr_body: ""
      pr_skip_ai: false
      allow_workflow_edits: ${{ github.event.inputs.allow_workflow_edits == 'true' }}
      force_rb_judge: true
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

> **Canonical audit-gate delivery contract:** `update_workflows.yml` applies
> `workflow-templates/audit-gate/contract.json` atomically and idempotently.
> The contract requires `package_script` and `managed_files`. When
> `package.json` exists and `scripts.audit:ci` is missing (or already
> canonical), the updater sets `scripts.audit:ci = node
> scripts/security/check-npm-audit.js` and syncs managed files
> (`scripts/security/check-npm-audit.js`,
> `security/dependency-audit-allowlist.json`). Repositories with a custom
> `scripts.audit:ci` value are preserved unchanged, and repositories without
> `package.json` are skipped (`no_package_json`).

> **Audit identity and regeneration:**
> `scripts/security/check-npm-audit.js` matches findings on
> `severity|package|advisoryId` (`advisoryId` prefers GHSA, then CVE).
> `viaPackages` is retained for legacy carry-forward/diagnostics but is not
> part of the current identity key. Use `npm run audit:ci -- --write` to
> regenerate the allowlist; regeneration preserves curated metadata fields
> (`reason`, `owner`, `expiresOn`) and exits with no file changes when already
> aligned.

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
| `orchestrate_poll.yml` | `schedule` (every ~5 min) | Orchestrator progress poller + judge + auto-recovery. Polling cadence is driven entirely by the wrapper workflow's cron schedule; the legacy self-retrigger path (cooldown sleep + `workflow_dispatch` at end-of-run) and its rate-limit circuit-breaker gate have been removed. |
| `update_workflows.yml` | `schedule` (daily), `repository_dispatch`, `workflow_dispatch` | Auto-updates existing and creates new workflow wrappers from upstream templates |

<!-- §Workflow Log Analysis And Improvement and §Workflow Log Analysis moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need workflow-log-analysis pipeline runbook details (collector/analyzer contracts, phase behavior, env vars). -->

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
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.4` (every phase) | Model for code editing / reasoning tasks. The split that previously routed patch-heavy phases (implement, review_autofix editor, conflict resolver) to `gpt-5.3-codex` was retired after openai/codex#11151. See main table above. |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `true` | Allow AI edits to workflow files and automatic wrapper updates |
| `ENABLE_AUTO_MERGE` | `true` | Auto-merge PRs (squash) when review passes and checks are green |
| `MAX_AUTOFIX_ITERATIONS` | `3` | Maximum consecutive autofix rounds before marking `ai:review-blocked` |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | `true` | Enable review-blocked judge for non-orchestrator PRs |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `medium` | Reasoning effort for review-blocked judge |
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
| `STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES` | `120` | Stall threshold for `ai:review-blocked` phase; past threshold the poller dispatches `review_rb_judge_dispatch.yml` to run `review_rb_judge.sh` against the linked PR |
| `MAX_STALL_RECOVERIES_PER_ISSUE` | `5` | Max stall recovery attempts per issue before skipping (declarative `STALL_RECOVERY_ACTIONS` + optional `run_stall_judge` escalation) |
| `STALL_JUDGE_TRIGGER_COUNT` | `2` | Recovery-attempt threshold to invoke stall judge escalation (`run_stall_judge`) |
| `ENABLE_STALL_JUDGE` | `true` | Enable/disable stall-judge escalation in orchestrator and standalone stall recovery |
| `ENABLE_STALL_HUMAN_TERMINALIZATION` | `false` | Allow terminal `escalate_human` stall actions; when `false`, both declarative and judged stall actions downgrade `escalate_human` to the nearest prior non-human phase action |
| `ENABLE_STANDALONE_STALL_RECOVERY` | `true` | Enable standalone AI issue stall recovery in the poller |
| `ENABLE_STALL_MERGED_PR_GUARD` | `true` | Double-check the issue's linked PR state before firing early-phase stall recovery commands; if the PR is merged, tag `ai:merged` and skip instead of posting `/reclarify` (etc). Batched GraphQL prefetch — 0 extra per-issue calls on successful prefetch; cache misses may fall back to a per-issue REST lookup. |
| `MAX_RECOVERY_ATTEMPTS` | `3` | Max project-level recovery cycles (judge failure → auto-fix) |
| `JUDGE_REPEAT_FINGERPRINT_MAX` | `2` | Circuit-breaker cap on consecutive identical normalized judge-failure fingerprints (same normalized justification). Exceedance sets `ai:blocked`, posts a breaker comment (fingerprint + normalized justification), emits a CRITICAL alert, and requires manual intervention instead of additional judge-driven auto-recovery |
| `MAX_VALIDATION_RECOVERY_ATTEMPTS` | `2` | Max validation-failure → judge re-evaluation cycles before terminal failure |
| `MAX_VALIDATION_FIX_BATCH_CYCLES` | `30` | Max poll cycles a single validation fix-up batch can sit "in progress" before the poller escalates through `mark_validation_failed` |
| `MAX_IMPL_NOOP_REISSUES` | `2` | Max automatic re-issues for `ai:implementation-failed` before closing as likely already implemented and deferring to judge verification. Enforced by both the state-based counter and the issue-local `count_noop_ancestors` walk of the `Re-issued from #N` chain (belt-and-braces); either signal trips closure |
| `IMPL_NOOP_ANCESTRY_THRESHOLD` | `2` | Ancestor-chain no-op cap enforced in `.github/workflows/implement.yml`'s "Handle no-op implementation" step; closes the issue with `ai:closed` when the `Re-issued from #N` chain already has this many no-op ancestors, rather than labeling `ai:implementation-failed` for another poller re-issue |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | `3` | Max in-job post-Codex syntax-repair attempts in `implement`; must be non-negative integer (`0` disables repair; invalid values fallback to `3`) |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | `900` | Min seconds between consecutive resolver dispatches against an integration-branch final PR |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | `3` | Max consecutive unresolved conflict ticks before judge escalation, after `_dispatch_review_for_conflicts` healing attempts (non-orchestrator integration branches) |
| `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` | `1` | Tighter ticks-before-judge budget applied only when head ref matches `orchestrator/project-*` (integration-sync conflicts) |
| `INTEGRATION_CONFLICT_LIFETIME_MAX` | `10` | Cumulative cap on resolver+judge dispatches per integration branch across all retry episodes; once reached, `heal_integration_branch_conflict` flips `status=failed` and stops dispatching. Catches the resolver/judge alternation loop where per-burst counters reset on each judge escalation but the merge stays dirty as main keeps moving |
| `FINGERPRINT_PER_FILE_CAP` | `12` | Cap on `must_contain`/`must_not_contain` patterns captured per file per merged sub-issue |
| `FINGERPRINT_MIN_PATTERN_CHARS` | `12` | Minimum trimmed-line length for a captured fingerprint pattern |
| `ACTIONS_RUNS_CACHE_TTL_SECONDS` | `60` | Cross-tick cache TTL (seconds) for `GET /actions/runs` snapshots persisted on the `ai-memory` branch and reused by orchestrator poll run-state readers |
| `AI_MEMORY_BRANCH` | `ai-memory` | Branch used for persistent AI memory |
| `AI_MEMORY_ROOT` | `ai-memory` | Memory root path used by workflows |
| `AI_MEMORY_RETRIEVAL_PROFILES` | `ai-memory/config/retrieval_profiles.v1.json` | Retrieval role config |
| `AI_MEMORY_ENABLED` | `true` | Enable/disable memory operations |
| `AI_MEMORY_KEYWORD_MODEL` | `openai/gpt-5.4-nano` | Model for semantic keyword extraction during retrieval |
| `AI_MEMORY_KEYWORD_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL for keyword model |
| `AI_MEMORY_TOKEN_BUDGET_<ROLE>` | _(from profile)_ | Per-role token budget override (e.g. `AI_MEMORY_TOKEN_BUDGET_IMPLEMENTATION=3200`) |
| `THINKING_LEVEL_CLARIFY` | `medium` | Reasoning effort for clarification (`xhigh`, `high`, `medium`, `none`) |
| `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` | `medium` | Clarify-only override for forced human `/reclarify` on `ai:orchestrator-managed` issues (normal clarify path auto-posts `/answer [auto-answered-by-orchestrator]` without Codex) |
| `THINKING_LEVEL_PLAN` | `medium` | Reasoning effort for planning |
| `THINKING_LEVEL_IMPLEMENT` | `medium` | Reasoning effort for implementation |
| `THINKING_LEVEL_ANALYSIS` | `high` | Reasoning effort for the workflow-log-analysis API-redundancy pass (deep-audit is hardcoded at `high` in the workflow YAML; edit there to override) |
| `THINKING_LEVEL_REVIEWER` | `medium` | Reasoning effort for reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `medium` | Reasoning effort for editor model (applying fixes) |
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | Tool call budget for clarification |
| `TOOL_CALL_BUDGET_PLAN` | `40` | Tool call budget for planning |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | Tool call budget for implementation |
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | Token warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | Token warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | Token warning threshold for implementation |
| `WORKFLOW_ORCHESTRATE_MODEL` | (falls back to `WORKFLOW_EDITOR_MODEL`) | Model override for orchestrator/judge |
| `THINKING_LEVEL_ORCHESTRATE` | `medium` | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `medium` | Reasoning effort for judge evaluation |
| `ORCHESTRATE_POLL_INTERVAL` | `30` | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_SHORTCIRCUIT_MAX_CHARS` | `1200` | _(deprecated — no longer consumed; short-circuit paths removed in #1163)_ |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | `ai-orchestrate-poll.yml` | _(deprecated no-op — self-retrigger removed; value is ignored, retained for backward compatibility)_ |
| `EDITOR_IDLE_TIMEOUT` | `1200` | Editor watchdog idle timeout (seconds); killed if no output and no active network connections |
| `EDITOR_MAX_WALL` | `7800` | Max wall-clock seconds (~130 min) per editor attempt; auto-capped to remaining job budget |
| `EDITOR_MIN_ATTEMPT_SECS` | `300` | Minimum job budget (seconds) required to start an editor attempt |
| `REVIEW_PR_STATE_POLL_INTERVAL_SECS` | `10` | Sleep interval (seconds) for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; GitHub PR-state API checks run every 9 polls (default ~90s); must be integer `10..3600`, else warn (`rate_limit_audit_fallback`) and fall back to `10` |
| `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS` | `30` | Age (days) above which sibling `analysis/workflow-optimization-<date>.md` reports are git-removed in the same commit as a new report. Filename date is authoritative; the new report is always preserved. |
| `BATCH_API_DISABLED` | `false` | _(deprecated compat)_ Active batch path removed; only echoed in `memory_maintenance.yml`'s `batch_noop` telemetry line for log-scraper backward compatibility |
| `BATCH_API_PROVIDER` | `auto` | _(deprecated compat)_ Same status as `BATCH_API_DISABLED` — surfaced only in `memory_maintenance.yml` `batch_noop` log line |
| `BATCH_API_POLL_TIMEOUT_HOURS` | `24` | _(deprecated compat)_ Same status as `BATCH_API_DISABLED` — surfaced only in `memory_maintenance.yml` `batch_noop` log line |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | Tool call budget for decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | Tool call budget for judge (needs deep repo inspection) |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | Token warning threshold for orchestration |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `medium` | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `medium` | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `medium` | Reasoning effort for the Codex-based merge conflict resolver (used by orchestrate_poll integration-sync and review_autofix's post-editor resolver step) |
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
| `MAX_MERGE_DEFERRALS` | `5` | Max consecutive poll cycles a single sub-PR may be deferred by the pre-merge sibling-conflict probe (`probe_sibling_merge_conflicts` in `scripts/orchestrate_poll_process.sh`). The probe runs `git merge-tree --write-tree --name-only` locally against every other open sub-PR targeting the same integration branch before invoking `gh pr merge --squash`. When a textual conflict is detected, the candidate PR is skipped for the cycle and the deferral counter on its wave entry is incremented. Exceeding `MAX_MERGE_DEFERRALS` triggers a Telegram WARNING for human review but does not mark the PR failed — the probe is a merge-ordering nudge, not a gate. Set lower for more aggressive human escalation or higher to give auto-serialization more room. Every detected conflict also emits a telemetry event to `ai-memory/orchestrator/merge_conflicts.jsonl` on the `ai-memory` branch (git protocol only, zero GH API calls) so the next orchestrator run can auto-learn hot files without any manual seed file. |
| `ORCHESTRATOR_HOT_FILE_WINDOW_DAYS` | `90` | Lookback window for the auto-learned hot-file set computed at plan time from `ai-memory/orchestrator/merge_conflicts.jsonl`. A path is promoted to "hot" when it appears in at least `ORCHESTRATOR_HOT_FILE_MIN_EVENTS` distinct conflict events across at least `ORCHESTRATOR_HOT_FILE_MIN_PROJECTS` distinct orchestrator projects within this window. Older events drop out automatically — no persistent "demotion" state is kept. |
| `ORCHESTRATOR_HOT_FILE_MIN_EVENTS` | `3` | Minimum distinct conflict events required to promote a path to the learned hot-file set. Lower for faster reaction, higher for less noise. |
| `ORCHESTRATOR_HOT_FILE_MIN_PROJECTS` | `2` | Minimum distinct orchestrator projects required to promote a path. Prevents a single runaway project from skewing the set. |
| `REVIEW_LEDGER_ENABLED` | `1` | Enable (`1`) or disable (`0`) review-issue ledger lifecycle tracking in `scripts/review_issue_ledger.sh`; when disabled, `ledger_status.txt` is emitted empty and no ledger file is updated. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | `2` | Persist-count threshold for transitioning a still-present issue to `accepted-residual` after increment (>= threshold). |
| `REVIEW_LEDGER_PATH` | `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` | Runtime ledger file path used by `scripts/review_issue_ledger.sh`. Per-PR filename isolates concurrent PRs so they never share a file (no cross-PR merge conflicts on main). Gitignored by default; cross-iteration persistence is provided by `actions/cache` restore/save steps in `review_autofix.yml` keyed on `review-ledger-<repo>-pr-<N>-`. Explicit overrides are honored verbatim (legacy single-file path still supported). Malformed prior ledgers fail-open with `ledger_reset=1` and state reset semantics. |
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

- Prompt assembly is cache-friendly in all Codex-driven phases: static prefix first (`unattended_system_instructions.md` + `agents.md` + phase template), dynamic context second (memory context, issue/PR body, comments/diffs).
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

- **Observed support (route-dependent):** `openai/gpt-5.4` and `openai/gpt-5.3-codex` via OpenRouter Responses API can benefit from provider-managed prefix caching, but availability/reporting can vary by routed provider/model.
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
5. **Polling:** Every ~5 minutes (cron schedule), the poller checks if the current wave's issues have reached `ai:merged`. When all are merged, it runs the judge. The legacy end-of-run self-retrigger (cooldown sleep + `workflow_dispatch`) was removed — each cycle is started by the wrapper's cron entry.
6. **Judge:** Full repo checkout + tool access (shell, file reads). Compares merged code against the project spec. Decides: complete, in_progress (next wave or fix-ups), or failed.
7a. **Clean-wave skip (flagged):** When `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`, and the completed wave has no failed issues, project is not complete, and it is not a stuck-wave invocation, the poller advances `current_wave` and increments `judge_cycle` without calling Codex judge. `judge_stall_cycles` is unchanged.
7b. **Clean project-completion skip (flagged):** When `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`, the final wave is complete with all issues merged, no failures, no review-blocked issues, and no stuck-wave invocation, the poller emits a synthetic `complete` verdict without calling the Codex judge. The outcome is deterministic in this case — the LLM judge cannot add value and risks empty-output failures.
8. **Next wave:** When the judge approves, the poller creates the next wave's issues (deferred creation — they don't exist until their dependencies are met). This triggers `clarify.yml` via `issues.opened`.
9. **Review-blocked resolution:** When a PR exhausts its autofix iterations (`ai:review-blocked`), the poller invokes a dedicated review-blocked judge (medium thinking, full PR context). The judge makes autonomous architectural and security trade-off decisions — it does not defer to humans. It can: (a) merge the PR as-is if remaining issues are cosmetic or low-risk, (b) push an `[orchestrator-fix]` commit with targeted fixes (resets the autofix counter, re-triggers review), or (c) close the PR and create a replacement issue with refined guidance. After `MAX_REVIEW_BLOCKED_RETRIES` (default 2), the judge must choose merge or close+reissue — no further fix attempts.
10. **Implementation-failed recovery:** When the implementation phase reaches the post-Codex pre-commit path with no committable file changes despite an approved plan (e.g. workflow edits stripped without `ALLOW_WORKFLOW_EDITS=true`, or model failure), `implement.yml` labels the source issue `ai:implementation-failed`. The poller automatically closes that issue and creates a replacement with additional diagnostic guidance, so the pipeline retries without manual intervention. For no-op implementation failures this behavior is unchanged; retries are bounded by `MAX_IMPL_NOOP_REISSUES`.
10a. **Post-Codex syntax repair (in-job):** If `Validate syntax of changed files` fails, `implement.yml` runs an in-job recovery loop before commit/push. The loop is capped by `MAX_POST_CODEX_REPAIR_ATTEMPTS` (default `3`; must be a non-negative integer, where `0` disables in-job repair and invalid values fall back to `3`), invokes Codex with `prompts/mode-implement-repair.txt` plus captured diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), and re-runs syntax validation via `scripts/validate_changed_files_syntax.sh` after each attempt. Repair edits are scope-guarded to the initial post-Codex changed-file set, intersected with captured-file entries when present. Any out-of-scope tracked edits are rolled back and out-of-scope untracked files are deleted; the attempt is counted as failed.
10b. **Post-Codex diagnose + fix-up issue creation:** For targeted post-Codex implementation failures, `implement.yml` captures diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), runs a non-fatal syntax check step first, then enforces a separate fatal syntax gate step so repair opportunities can run before final failure. When syntax repair is exhausted/unsuccessful (or for other targeted post-Codex failures with captured diagnostics), it runs the diagnose pass (`prompts/mode-implement-diagnose.txt`) and creates orchestrator-compatible fix-up issue(s). Each created fix-up now receives both `ai:clarification` (pipeline-entry) and `ai:implement-fix-up` (ops marker) labels. The source issue summary comment includes machine-readable blocker metadata (`IMPLEMENT_FIXUP_BLOCKERS_V1`) with `fixup_issue_numbers` and `blocks_source_issue`, which the poller persists additively into orchestrator state for implementation-failed reissue handling. If diagnosis/parsing fails, it creates a deterministic fallback fix-up issue with raw captured diagnostics so failures are never swallowed. This path applies `ai:implementation-failed` and suppresses the generic failure relabel/comment path (preventing re-add of `ai:awaiting-approval`). Out-of-scope failures (missing/empty capture file) continue using the existing generic failure behavior unchanged.
10c. **Implementation-failed blocker gating:** If an `ai:implementation-failed` source issue has post-Codex failure context and linked fix-up blocker issues, the poller defers close/reissue while any blocker issue is still `open` (or when blocker status lookup is unknown). During deferral, it logs and sends Telegram context including mode (`post-codex-validation`), blocker list/statuses, and the defer reason. Reissue resumes only after blockers are no longer open; reissued guidance text is mode-specific (no-op guidance for no-op failures, syntax/blocker-sequencing guidance for post-Codex validation failures). Blocker dependency metadata is persisted additively on the wave issue entry (`depends_on` when already present, otherwise `reissue_depends_on`) for backward compatibility.
10d. **Destructive-commit guard (`ai:destructive-blocked`):** Before creating the AI implementation commit, `implement.yml` inspects the staged deletion set. The commit is refused — and the workflow run fails — on either of two conditions: (a) any deletion touches the canonical workflow-source list (`agents.md`, `ai_pipeline.md`, `unattended_system_instructions.md`, `prompts/**`, `scripts/**`, `.github/ai/**`) and `ALLOW_WORKFLOW_EDITS` is not `true`, or (b) the total staged deletions exceed `BULK_DELETE_THRESHOLD` (default `3`) and `ALLOW_BULK_DELETE` is not `true`. On rejection the issue is labeled `ai:destructive-blocked`, a visible comment is posted listing the blocked deletions, and a CRITICAL Telegram alert is sent so a human can intervene. The `Validate approval phase label` step at the top of every subsequent `implement.yml` run refuses to redispatch any issue carrying `ai:destructive-blocked` until a human removes the label after auditing the earlier rejection — the orchestrator's judge-cycle may still regenerate the same task under a fresh issue number, so the TG alert is the intended human-in-the-loop signal. This guard exists because PRs #917/#931 saw a test harness that set `GITHUB_REPOSITORY=owner/repo` trigger a consumer-repo cleanup block in `scripts/orchestrate_poll_process.sh` from within the real coding-workflows checkout, causing the AI implementation commit to silently delete ~10,700 lines across 28 tracked source files. The gate in the poller/review_rb_judge scripts has since been switched from the env var to a git-remote-URL check; the destructive-commit guard in `implement.yml` is the defense-in-depth layer that catches any future destructive path regardless of its trigger.
10e. **Targeted vs legacy post-Codex failure flow:** Targeted post-Codex failures with captured diagnostics follow 10a/10b (syntax repair first; if unresolved, diagnose + fix-up issue creation, then label source issue `ai:implementation-failed`) plus blocker-aware reissue gating in 10c. The no-op pre-commit path in 10 remains the close/re-issue retry lane. Other implement workflow failures (for example, missing/empty capture artifacts) remain on the legacy path (`failure()`/`cancelled()` handling in `implement.yml`) with failure comments/alerts.
10f. **Success-no-op short-circuit (Guard 0, `ai:closed`):** The "Run Codex implementation" step in `.github/workflows/implement.yml` snapshots the worktree with `git status --porcelain -uall` into `${RUNTIME_DIR}/codex_pre_baseline.txt` BEFORE the retry loop. Detection, retry-nudge, and success checks all diff against this baseline via `grep -vxFf` so runtime support checkouts (`.codex-workflow-src`, `.codex-workflow-src-main`, `ai-memory/schemas`) don't register as Codex-produced changes. When the baseline-relative delta is empty AND Codex stdout matches `/no file changes were made|nothing to change|already (aligned|implemented|satisfied|up[- ]to[- ]date|done|exists|present|complete)|no changes needed|no repository changes (were )?made|no file changes made|no repository changes (were )?required|no files (were )?modified|no repository changes (were )?needed|no file changes (were )?needed/i`, the step writes `${RUNTIME_DIR}/codex_success_noop.flag` and breaks with success. The "Handle no-op implementation" step's Guard 0 sees this flag first (before Guard 1's pathspec hard-fail and Guard 2's ancestor-chain cap), closes the issue with `ai:closed` + an ✅ "Already implemented" comment, and exits with `0`. This prevents the orchestrator re-issue loop from spawning duplicate sub-issues when Codex correctly reports the requested work is already on the integration branch (observed failure: issue #141 after `npm run audit:ci` was already exit-0 from a sibling sub-task). Fail-open: missing flag/`RUNTIME_DIR` or a failed flag write falls through to Guards 1/2 as before.
11. **Auto-recovery:** On failure, the judge can revert problematic PRs and create fix-up issues. Those fix-up issues include the standard orchestrator metadata block (`Tracking issue`, `Integration branch`, `Local ID`, `Managed by`) in the issue body. Recovery is attempted up to `MAX_RECOVERY_ATTEMPTS` (default 3) times; if all attempts fail, the project stops and the operator is notified via Telegram.
12. **Validation-failure recovery:** When runtime validation fails, the poller transitions the project back to the judge for re-evaluation (labeled `ai:validation-recovery`) up to `MAX_VALIDATION_RECOVERY_ATTEMPTS` (default 2) times. The judge sees the validation diagnosis in tracking issue comments, can issue fix-up work (with orchestrator metadata), and then re-validates. After exhausting the recovery budget, the project goes to terminal `ai:validation-failed`.
12a. **Integration branch delivery:** Orchestrator projects now create a per-project integration branch (`orchestrator/project-<tracking_issue>`). All orchestrator child issues include `Integration branch` metadata so implementation PRs target the integration branch instead of `main`. Branch resolution order is strict: child issue metadata footer first, then tracking issue metadata, and default-branch fallback only when no integration metadata exists. If metadata exists but the branch is invalid/missing, the poller fails safe instead of silently falling back to default branch. The poller periodically syncs default branch changes into this branch via the merge API.
12b. **Sync conflict handling and superseded detection:** Before sync merge attempts, the poller checks whether the integration branch is effectively superseded by the default branch (tracked child PRs are terminal and affected-path deltas are already represented on the default branch). Superseded projects persist `sync.status = superseded-by-main`, post one final tracking comment, and skip future sync attempts without recurring Telegram warnings. Real unresolved conflicts include parsed conflict paths, a deduped fingerprint to prevent repeated spam, and a rebuild runbook link: [docs/orchestrator-integration-branch-rebuild-runbook.md](docs/orchestrator-integration-branch-rebuild-runbook.md).
12c. **Integration self-healing:** If a periodic `main` → integration-branch sync returns HTTP 409 (real conflict), the poller routes recovery through `heal_integration_branch_conflict`: it (a) ensures/creates the final integration→default PR (eagerly, if it does not yet exist), (b) dispatches the review/autofix workflow through `_dispatch_review_for_conflicts` against that PR to run the existing Codex conflict resolver on a clean runner, and (c) records the attempt in new tracking-state fields (`integration_sync_status`, `integration_sync_last_error`, `integration_conflict_dispatch_count`, `integration_conflict_dispatch_ts`, `integration_conflict_unresolved_ticks`). Dispatches are throttled by `CONFLICT_DISPATCH_COOLDOWN_SECS` (default 900s). The retry budget is **branch-aware**: head refs matching `orchestrator/project-*` honour `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` (default `1` — single resolver shot, then judge), while non-orchestrator integration branches honour `INTEGRATION_CONFLICT_MAX_RETRIES` (default `3`). After the effective budget is exhausted the orchestrator escalates by invoking the judge with full PR context via `codex exec`. Only after both the automated resolver *and* the judge escalation fail is the project marked terminally `failed`. The same healing flow is triggered from `finalize_integration_merge_if_needed` whenever the final PR is observed with `mergeable=false`, so the project no longer halts on first conflict.

12c-i. **Integration-sync intent fingerprints:** When a sub-issue PR merges into an orchestrator integration branch, the poller captures `must_contain` / `must_not_contain` regex fingerprints from the merged diff and persists them under `merged_issue_fingerprints[<issue_num>]` in the orchestrator state comment. The `review_autofix.yml` resolver step uses three affordances on top of these fingerprints when the PR head ref matches `orchestrator/project-*`:
- **Intent injection into the resolver prompt.** The conflict resolver prompt is rendered from `prompts/integration-sync-conflict-resolver.txt` (instead of the generic `prompts/conflict-resolver.txt`) and includes the tracking-issue title/body, the list of merged sub-issues already on this integration branch, and the full `merged_issue_fingerprints` JSON. The template instructs the model to treat each fingerprint as a hard test case and to **synthesize** a new hunk when the conflict is between two independent rewrites of the same code rather than picking side A or side B verbatim. The template also contains two anti-regression hardening blocks aimed at the dominant observed failure mode ("pick default-branch side verbatim, drop HEAD sub-issue content"): (1) a "do NOT pick the default-branch side verbatim" rule tying non-empty-fingerprint files to "keep HEAD or synthesize", and (2) a per-file fingerprint pre-flight that requires the model to reconcile every `must_contain` / `must_not_contain` pattern for a file before moving on to the next file, plus a self-check that requires reporting each pattern's match status before declaring success.
- **Fingerprint verification gate.** Before the `[ai-merge-resolve]` commit lands, `scripts/verify_integration_fingerprints.py` walks every captured pattern against the post-resolve working tree. A `must_contain` pattern that no longer matches, or a `must_not_contain` pattern that reappears, is treated as a silent intent regression and HARD-fails the resolver step (the merge state is left intact so the next poll tick re-enters healing and — by default — escalates immediately to the integration judge). A silent-regression detector additionally logs a warning whenever the post-resolve tree contains strictly fewer total `must_contain` matches than were captured.
- **Retry-loop with reflexion (verify-in-loop + per-attempt reset).** Previously, the resolver step ran `codex exec` once and then evaluated the fingerprint verifier after the retry loop; the loop only retried when `codex exec` crashed or produced empty output, so a bad-but-well-formed model output terminated the whole run with zero retries consumed on the real failure class. Large integration PRs hit this reliably. `scripts/review_conflict_resolve.sh` now runs the codex resolver up to `INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS=3` times with the soft quality gates — residual Git conflict markers (`<<<<<<< ` / `>>>>>>> ` scan over every path in `resolver_unmerged_allowlist.txt`) and the full `scripts/verify_integration_fingerprints.py` pass — running **inside** the loop. On a soft failure the working tree is restored from a pre-first-attempt snapshot (every file in the allowlist, snapshotted via `cp -a` into `${RUNTIME_DIR}/resolver_attempt_base/` before the loop starts) and the next attempt is given a **reflexion prompt** built from `prompts/integration-sync-conflict-resolver-retry-prelude.txt` concatenated in front of the rendered `${CONFLICT_RESOLVER_PROMPT_FILE}`. The prelude names each file with residual markers and each fingerprint regex that regressed, so the retry fixes specific violations rather than re-rolling the whole merge blind. On intermediate attempts the verifier output is captured with annotations suppressed (no false-positive `::error::` flood in the GHA log); on exhaustion the verifier is re-run at normal verbosity so the `"Aborting [ai-merge-resolve] commit: integration fingerprint verification rejected the resolver output."` annotation lands exactly as before. Hard gates (workflow-file allowlist violation, `scripts/check_resolver_diff.sh`) still run **once** post-loop on the accepted attempt — retrying them is unsafe (a hallucinated workflow edit must never be handed back to the model as "try again, here's what went wrong"). The reflexion prompt is integration-sync only; generic (non-integration) resolver runs still benefit from verify-in-loop + per-attempt reset + marker pre-scan but retry with the original prompt verbatim. `prompts/integration-sync-conflict-resolver-retry-prelude.txt` is a soft dependency: a missing template falls open to "retry with original prompt" plus a `::warning::`, matching the handling pattern of `prompts/conflict-resolver.txt` so older consumer-repo `script_ref` pins bootstrap cleanly.
- **No-progress detection + step wall-clock cap + immediate-judge dispatch.** Three small additions close the failure mode where a structurally-impossible integration merge (sub-issues with logically contradictory `must_contain` / `must_not_contain` fingerprints) consumed an entire 180-min job budget on three resolver attempts that all reproduced the identical fingerprint violation set. (1) **No-progress detection** inside the retry loop — after each attempt's soft validation, when `IS_INTEGRATION_SYNC=true` and the current `${RUNTIME_DIR}/resolver_fp_violations.txt` is byte-identical to the previous attempt's snapshot at `${RUNTIME_DIR}/resolver_fp_violations_prev.txt` (compared via `sort` then `cmp -s` so verifier output reordering is not a false-negative trigger), the attempt counter is promoted to `INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS` so the existing exhaustion block runs immediately. Same `CONFLICT_RESOLVED=false` + `exit 1` shape as natural exhaustion, so downstream tooling (telegram alert, `ai:integration-judge-failed` transitions, immediate dispatch trap below) sees the same signal whether the loop bailed early or ran to MAX_ATTEMPTS. Restricted to integration-sync runs because only fingerprint violations carry stable per-pattern identity; residual-marker presence/absence is not a reliable progress signal. (2) **Step wall-clock cap** — the `Run Codex resolver, validate, stage, commit` step in `.github/workflows/review_autofix.yml` carries `timeout-minutes: 60`, sized so a single hung `codex exec` cannot eat the full job budget but a well-behaved 3-attempt run still completes (one observed worst-case attempt ran ~58 min). The step inherits the job's 180-min cap as the previous behaviour; the explicit step cap is the safety net. (3) **Immediate orchestrator-poll dispatch** via an EXIT trap in `scripts/review_conflict_resolve.sh` (`_dispatch_integration_judge_now`) — on any non-zero exit from the resolver script when `IS_INTEGRATION_SYNC=true`, the script fires `gh workflow run ${ORCHESTRATE_POLL_WORKFLOW_FILE:-internal-orchestrate-poll.yml}` against the current repo so the orchestrator integration judge picks up on the next concurrency slot rather than waiting up to 5 min for the `*/5` cron tick. Dedup: skipped when a poller run is already `in_progress` or `queued` (the poller's `concurrency.group: ai-orchestrate-poll-${{ github.repository }}` + `cancel-in-progress: false` already serialises runs across the repo). Idempotent within a single script invocation. Fail-open: missing `GH_PAT`/`GITHUB_REPOSITORY`, an `gh` rate-limit, or an unknown workflow filename on a consumer repo logs `::warning::` and falls through; the cron tick remains the safety net so unattendedness is preserved. Exit-0 paths (resolver succeeded in deciding no commit was needed) intentionally do not fire — no escalation is warranted. Consumer repos that ship the orchestrator poller under a non-default filename can override via the `ORCHESTRATE_POLL_WORKFLOW_FILE` env var.
- **Pre-codex working-set expansion.** The verifier is also invoked in `--list-violated-files` mode by `scripts/review_conflict_prepare.sh` after the merge replay but before the resolver prompt is rendered. Any file whose fingerprints already fail against the auto-merged tree — i.e. `git merge` resolved the textual diff cleanly but the resolution silently dropped a merged sub-issue line, or reintroduced one the sub-issue had deleted — is appended to the resolver working set: the prompt's in-scope file list (so Codex is told to inspect it), the unmerged-paths allowlist (so the workflow-file violation guard in `scripts/review_conflict_resolve.sh` permits an edit), and the conflicted-paths set (so `scripts/check_resolver_diff.sh`'s `touched ⊆ conflicted` guard permits the edit). Without this expansion the dominant fail-mode observed in practice — main carries an independent rewrite of a hunk a sub-issue also rewrote, `git merge`'s 3-way resolution picks main's side verbatim, no conflict markers are emitted, and the resolver is structurally unable to touch the file — reaches the post-codex verifier as an irrecoverable violation and wastes the entire run. Fail-open: if the verifier is not bootstrapped on the current `script_ref` or exits `2` (plumbing failure), the expansion is skipped and the run proceeds with the git-marked conflicted set only.

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

### Orchestrator PR autofix flow

Orchestrator-managed PRs use the same per-PR autofix loop as non-orchestrator PRs (reviewer/editor → up to `MAX_AUTOFIX_ITERATIONS=3` `[ai-autofix]` commits → per-PR review-blocked judge with up to `MAX_REVIEW_BLOCKED_RETRIES` retries). Two orchestrator-aware behaviours sit on top of that uniform loop, both gated by the master switch `ORCH_PR_AUTOFIX_FLOW_ENABLED` (default `true`):

1. **PR mode classification** — `review_autofix.yml`'s retrigger guard tags each PR as `orch_intermediate`, `orch_final`, or `other` for observability. The classification used to override the per-PR autofix cap (intermediate PRs were capped at 1 iteration with the judge skipped) — that override has been removed: catching blocking issues per sub-issue PR (where the diff is small and the linked-issue context is narrow) is cheaper and more reliable than letting them accumulate and surface en masse on the integration→default-branch final merge.
2. **Final-PR cap bypass** — `orchestrate_poll_process.sh` bypasses the orchestrator-level `MAX_JUDGE_CYCLES` cap while the integration→default-branch final PR is open and pending merge, so the final PR can run unlimited 3-autofix→judge cycles until mergeable.

**PR mode classification** (in `review_autofix.yml` retrigger_guard step):

The retrigger guard reads `headRefName` and `baseRefName` from `${PR_META_FILE}` and matches them against `ORCH_INTEGRATION_BRANCH_PATTERN` (default `^orchestrator/project-`):

| Mode | Detection | Per-PR autofix cap | Per-PR judge | Orchestrator-level cycle cap (project-wide) |
|---|---|---|---|---|
| `orch_intermediate` | head matches pattern AND base matches pattern (sub-issue PR → integration branch) | `MAX_AUTOFIX_ITERATIONS` (default `3`) | Runs after exhaustion (full `merge` / `fix` / `close_and_reissue` actions; per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`) | `MAX_JUDGE_CYCLES` (default `25`) — counts orchestrator-issued judge runs at wave-completion / project-evaluation events; per-sub-issue rb_judge runs do not increment this counter |
| `orch_final` | head matches pattern AND base does NOT match pattern (integration branch → default branch) | `MAX_AUTOFIX_ITERATIONS` (default `3`) | Runs after exhaustion (full `merge` / `fix` / `close_and_reissue` actions; per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`) | **Bypassed** (unlimited cycles while `final_merge_status=pending`) |
| `other` | neither head nor base matches pattern (non-orchestrator PR) | `MAX_AUTOFIX_ITERATIONS` (default `3`) | Runs after exhaustion (full `merge` / `fix` / `close_and_reissue` actions; per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`) | n/a — the PR is not part of any orchestrator project |

**Intermediate PR behavior** (`orch_intermediate`):
- Reviewer + editor run on each `pull_request.synchronize` (or `workflow_dispatch`) up to `MAX_AUTOFIX_ITERATIONS` consecutive `[ai-autofix]` commits, addressing CI / lint check-run failures via the existing `CHECK_RUNS_AUTOFIX_ENABLED=true` path along the way.
- On exhaustion the per-PR review-blocked judge runs and decides `merge` (auto-merge into the integration branch), `fix` (push a `[judge-fix]` commit, which resets the autofix counter and lets the loop continue — capped at `MAX_REVIEW_BLOCKED_RETRIES`, default `2`), or `close_and_reissue` (close the sub-issue PR and create a refined issue).
- The PR merges into the integration branch only when the existing orchestrator merge gate clears: `mergeable=true` AND `_pr_checks_completed` AND `ai:ready-to-merge` label set.
- The `force_rb_judge` stall-recovery path (dispatched by the orchestrator stall poller for issues stuck at `ai:review-blocked` past the threshold) is unchanged — it forces `max_iterations_reached=true` so the rb_judge step fires directly against the existing PR state.

**Final PR behavior** (`orch_final`):
- Inner loop matches the same `MAX_AUTOFIX_ITERATIONS=3` cycle: 3 consecutive `[ai-autofix]` commits → judge runs → judge may push `[judge-fix]` → autofix resumes → repeat.
- The orchestrator-level `MAX_JUDGE_CYCLES` cap is **bypassed** while `state.final_merge_pr` is non-empty AND `state.final_merge_status="pending"`. The final-PR loop terminates implicitly when reviewer/editor produce zero `[ai-autofix]` commits AND judge approves; the existing final-merge gate (`finalize_integration_merge_if_needed` at `scripts/orchestrate_poll_process.sh`) then merges integration → default branch only when `mergeable=true` + checks complete (mergeability conflicts hand off to `heal_integration_branch_conflict`, unchanged).
- Bypass observability: each cycle that would otherwise have failed against the cap emits `[final-merge] judge cap bypassed (final-PR loop active: PR #<n>, status=pending); JUDGE_STALL_CYCLES=<m> > MAX_JUDGE=<k>, proceeding to judge invocation.` to the orchestrator log.

**Non-orchestrator PRs** (`other`): unchanged. `MAX_AUTOFIX_ITERATIONS=3`, judge runs after exhaustion, per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`. The orchestrator-level `MAX_JUDGE_CYCLES` cap does not apply — the PR is not part of any orchestrator project.

**Failure modes**:
- **Intermediate PR judge picks `close_and_reissue`**: the sub-issue PR closes and a new issue is created with refined guidance. The orchestrator's existing closed-PR / failed-sub-issue handling resumes from there. On a small sub-issue diff this is generally safe and is preferable to merging a fundamentally flawed approach into the integration branch where it would surface (much more expensively) on the final-merge judge cycle.
- **Intermediate PR with persistent CI failure**: autofix attempts CI fixes across its `MAX_AUTOFIX_ITERATIONS` runs; if CI stays red, `_pr_checks_completed` returns false and the orchestrator does not merge. Existing stall-recovery contracts (`STALL_THRESHOLD_DONE_MINUTES`, `STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES`) recover the issue.
- **Final PR with bad judge verdict loop**: with the cap bypassed, the loop continues indefinitely as long as judge keeps producing `[judge-fix]` commits. Operator intervention path: set `ORCH_PR_AUTOFIX_FLOW_ENABLED=false` to restore the `MAX_JUDGE_CYCLES` cap, then take action against the PR.
- **Branch naming mismatch**: if your orchestrator pushes to a branch that does not match `^orchestrator/project-`, the classifier falls through to `other` and per-PR behavior is identical (the only operational difference vs `orch_final` is the orchestrator-side cap bypass). Override `ORCH_INTEGRATION_BRANCH_PATTERN` to match your naming if you want the bypass.

**Operational steps**:
- To enable (default): no action required. The flow is on with `ORCH_PR_AUTOFIX_FLOW_ENABLED=true` baked into both the workflow and the script as the default.
- To disable / roll back: set repo var `ORCH_PR_AUTOFIX_FLOW_ENABLED=false`. The orchestrator-side cap bypass on `orch_final` reverts to `MAX_JUDGE_CYCLES=25` on the next poll tick. Per-PR autofix loops are unaffected (they already use `MAX_AUTOFIX_ITERATIONS` in every mode).
- To verify on a PR: check the workflow log for `Orchestrator PR mode: <mode> (head=... base=..., flow_enabled=true)` near the start of the `Count autofix iterations` step.

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

A second belt-and-braces check in `execute_stall_recovery_action retrigger_review` covers the **failed-autofix** case: after the merge-conflict override runs (and the PR is known to be mergeable), the action queries `gh run list --workflow <wf> --branch <head_ref> --limit 1` for each of `ai-review.yml`, `internal-review.yml`, `review_autofix.yml`, and if the most recent completed run concluded `failure`, `cancelled`, or `timed_out`, it calls `_dispatch_review_for_conflicts` directly instead of pushing an empty commit. The existing cycle-local `_CONFLICT_DISPATCH_TRACKER` and `_has_active_autofix_run` guards inside that helper prevent duplicate dispatch. On dispatch success (rc=0), the action emits `STALL_RECOVERY_EFFECTIVE_ACTION=redispatch_review_autofix`, consumes one recovery attempt (`STALL_RECOVERY_SHOULD_INCREMENT=true`), and returns 0. On already-dispatched-this-cycle/active (rc=2), it emits the same effective action but returns without incrementing the recovery counter. On dispatch failure (rc=1) the legacy empty-commit push runs as the fallback. Rationale: a failed Review-Autofix run leaves the PR with no in-flight worker and no review verdict, and an empty-commit push does not re-dispatch the workflow on its own (only `pull_request.synchronize` on a real code delta does), so the PR would otherwise sit until `MAX_STALL_RECOVERIES_PER_ISSUE` is exhausted.

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

The `gh_rate_limit_breaker_tripped` shell function is exported by `gh_helpers.sh` for use by scripts that want to short-circuit further API calls within the same job when the flag is set.

Historical note: `orchestrate_poll.yml` previously carried a "Check rate-limit circuit breaker" step that gated an end-of-run self-retrigger dispatch (cooldown sleep + `workflow_dispatch`) on this flag. Both the self-retrigger and its circuit-breaker gate were removed — polling cadence now comes exclusively from the wrapper workflow's cron schedule, which naturally back-pressures rate-limited cycles without a dedicated breaker.

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
4. If validation fails with fixable findings (`needs_fixes`), `validate_process.sh` creates fix-up issues, comments them on the tracking issue, and sets `ai:validation-fixing`. Before creating a new fix-up issue, the script now runs two guards (fail-open on API error):
   - **Per-tracker dedupe**: if an open issue labelled `ai:orchestrator-managed` already contains both the `Local ID: validation-fix-cycle-<N>` marker and the `Tracking issue: #<tracker>` marker (the pair emitted in the fix-up body), the existing issue number is reused instead of creating a duplicate — the tracking comment and `ai:validation-fixing` label are still applied so downstream poller behavior is unchanged.
   - **Tracker-open guard**: if the tracking issue's GitHub `state` is not `open`, no fix-up issue is created and the run exits with `needs_fixes`/`write_result_files` + Telegram notice (no tracking comment, no label flip on a closed tracker).
5. While in `ai:validation-fixing`, the poller waits for all active validation fix-up issues to reach `ai:merged`. Open fix-up issues at `ai:ready-to-merge` whose linked PR is already merged are reconciled in-cycle (see `MAX_VALIDATION_FIX_BATCH_CYCLES` description above) — the consumer-side `pull_request.closed` handler intentionally skips orchestrator-managed children, so this in-loop reconciliation is what flips them to `ai:merged` without waiting for the stall threshold.
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

1. Preserve `judge_stall_cycles` and `recovery_count` by default.
2. Reset counters only when explicit flags are present: `--reset-stall` (stall only), `--reset-recovery` (recovery only), or `--force` (both).
3. Transition the project status from `failed` to `in_progress`.
4. Resume normal wave processing immediately.

This does **not** reset the total `judge_cycle` counter (which is informational only — it tracks wave-advance/judge-cycle progression, including clean-wave skips where the judge is intentionally not invoked).

Use this after manual intervention (e.g. fixing a problematic issue, merging a stuck PR, or adjusting `MAX_JUDGE_CYCLES`/`MAX_RECOVERY_ATTEMPTS` variables). There is no limit on how many times `/judge_resume` can be used.

> **Note:** `/judge_resume` only applies to judge/recovery failures. For validation failures (`ai:validation-failed`), use `/revalidate` instead.

### Validation Controls

| Variable | Default | Behavior |
|---|---|---|
| `ENABLE_VALIDATION` | `true` | Truthy values (`1/true/yes/on`, case-insensitive) enable the validation gate. Any other value disables it, so judge `complete` closes immediately without runtime validation. |
| `MAX_VALIDATE_CYCLES` | `3` | Maximum cycles across initial validation plus fix/revalidate loops. Must be a positive integer; invalid values are coerced to `3`. Exceeding the limit forces `ai:validation-failed`. |
| `MAX_SELF_HEAL_ATTEMPTS` | `2` | Maximum in-process self-heal attempts per validate_process.sh invocation. Self-heal attempts patch one of the four validation prompts locally and re-exec the validation pipeline; they do NOT increment `MAX_VALIDATE_CYCLES`. Set to `0` to disable self-healing entirely. See [Validation self-healing](#validation-self-healing). |
| `VALIDATION_USE_TEMPLATES` | `true` | Truthy values (`1/true/yes/on`, case-insensitive) run `scripts/validate_process.sh` Phase 1 through the template renderer (`scripts/render_validation_templates.py`). This is the default path. Setting `VALIDATION_USE_TEMPLATES=false` now returns `raw_status=harness_error` because freehand Codex harness generation was removed. Missing manifest/renderer/schema/template assets also fail with `raw_status=harness_error`; there is no fallback path. Renderer exit codes (returned by `run_template_validation_harness_renderer`, consumed by the script's main `case`): `0` success, `10` `.ai/validate.yml` missing, `11` `scripts/render_validation_templates.py` missing, `12` `scripts/templates/slot_manifest.schema.json` missing, `13` `workflow-templates/validation-harness/` missing, `14` renderer subprocess failed (dependency import, manifest validation, schema violation, template collection — last 40 lines of `${GENERATE_LOG_FILE}` are surfaced into the tracking-issue comment), `15` required `_shared/` template assets missing, `17` `python3 >= 3.9` unavailable (deterministic environment failure — the case-arm embeds an `AI_VALIDATION_FAILURE_CLASS:deterministic_python_missing` marker that `mark_validation_failed` in `scripts/orchestrate_poll_process.sh` reads to short-circuit the `MAX_VALIDATION_RECOVERY_ATTEMPTS` budget). Exit codes are public contract; renames/repurposes are breaking. |
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
- If `.ai/validate.yml` is absent, validation runs a lightweight discovery phase (or reuses a cached hints file) and materializes the result into `.ai/validate.yml` in the runner's working tree so the template renderer can consume it. The materialized file is never pushed back to the consumer repo; commit your own `.ai/validate.yml` for deterministic, no-codex behavior.
- The validation-refresh onboarding bootstrap (manifest stub + repo-check entry script) is a separate path from this runtime-only hints materialization flow.

### Validation Harness Lifecycle

- Validation renders a manifest-driven harness under `validation/` from `.ai/validate.yml` via `scripts/render_validation_templates.py` + `workflow-templates/validation-harness/`.
- `VALIDATION_USE_TEMPLATES` now defaults to `true`; setting `VALIDATION_USE_TEMPLATES=false` is a terminal guard that returns `raw_status=harness_error` because freehand generation/fix paths were removed.
- Renderer-supported template families are currently `python-mongo-flask`, `node-hardhat-solidity`, `python-repo-checks`, and `python-mongo-repo-checks`.
- Use `python-repo-checks` for workflow/script repositories that do not expose a long-running app entrypoint; set `entry` to a repo-local command/script (for example `scripts/run_validation_repo_checks.sh`) so validation executes meaningful local checks instead of forcing web-service startup. If this entry path was auto-seeded by validation refresh onboarding, replace the placeholder script and manifest values with repo-specific checks before relying on the harness as a release gate.
- Freehand hint examples such as `type: http-server` in `examples/ai-validate-hints.yml` are diagnosis hints, not template-renderer family IDs.
- `validation/validate.sh` is generated as a thin wrapper that delegates to checked-in `scripts/validate_driver.sh`.
- Canonical runtime harness behavior now lives in `scripts/validate_driver.sh` (pre-flight, compose startup/logging, health polling, canary gating, TAP-safe counting, result emission/finalization).
- `scripts/validate_driver.sh` loads optional `validation/validate.env` and applies conservative defaults for supported knobs (including `APP_SERVICE`, `APP_URL`, `HEALTH_TIMEOUT`, `PHASE`). `APP_URL` is opt-in: the host-side HTTP probe is only performed when the consumer explicitly sets `APP_URL` (via environment or `validation/validate.env`). When unset, the health gate relies solely on Docker container state (Running + Health in {healthy, none}), so library-type consumers with no real HTTP service do not time out on a stale default probe URL. The fallback default (`http://localhost:8080/health`) is retained for documentation/inspection only.
- Before execution, validation runs pre-flight checks (`docker compose config`, shell syntax, and compose build path resolution).
- Pre-flight failures are classified as terminal `harness_error` for that run.
- The first generated test must be a canary infrastructure check (`00_canary.sh` style); infra-only canary failures shortcut to `harness_error`, while app startup/crash signals continue to diagnosis.

### Validation Refresh Automation

- Authentication:
  - Uses `GH_PAT` (exported as `GH_TOKEN`) for all cross-repository clone/branch/PR operations.
- Workflow: [`.github/workflows/validation-refresh.yml`](.github/workflows/validation-refresh.yml)
- Triggers:
  - Daily cron (`17 2 * * *`)
  - Manual dispatch (`workflow_dispatch`) with optional `repos_file` and `branch_name` inputs
- Runtime:
  - Reads target repositories from `.github/ai/consumer_repos.json`
  - For each target repo, clones the repo into a temporary workspace and checks out the `ai/validation-refresh` branch locally (the local branch is never pushed back to the remote). If `.ai/validate.yml` is missing, refresh bootstraps it from `examples/validation-fixtures/python-repo-checks.yml` and ensures executable `scripts/run_validation_repo_checks.sh` exists (diagnostics include `manifest_bootstrapped_from`, `repo_check_entry_seeded`, and `repo_check_entry_preserved_existing`). It then renders validation assets from `.ai/validate.yml` using `scripts/render_validation_templates.py`, runs deterministic lint (`scripts/validation_lint.py`) and deterministic self-test (`scripts/validate_driver.sh`).
- Drift reporting only — no PRs:
  - This workflow does NOT commit, push, or open pull requests in consumer repos. Consumers are expected to render validation assets on demand inside their own validation flow, so there is no need to ship a `chore(validation): refresh validation assets` PR ahead of time.
  - When the rendered output differs from what is checked into the consumer repo, the result includes a `validation_assets_drifted_no_push` diagnostic. The outcome is `green` when render/lint/self-test all pass and `red` when any stage fails.
- Failure/no-op behavior:
  - Manifest-less repos are bootstrapped in the temp clone (not skipped), but the seeded `.ai/validate.yml` and `scripts/run_validation_repo_checks.sh` are onboarding stubs. Repo owners still need to replace placeholder checks/values with real repository-specific validation logic and commit them in their own repo.
  - Pipeline failure with no file diff: records error (`pipeline_failed_without_changes`).
  - Workflow writes machine-readable summary JSON, appends a human summary to `$GITHUB_STEP_SUMMARY`, and sends Telegram failure notification (`TG_BOT_SECRET` + `TG_ADMIN_CHAT_ID`) on workflow failure.

### Nightly Validation Self-Test Status

- Workflow: [`.github/workflows/nightly-validation-selftest.yml`](.github/workflows/nightly-validation-selftest.yml) runs on nightly cron (`15 2 * * *`) and manual `workflow_dispatch`.
- Each run uploads `artifacts/validation-selftest-summary.json` and `artifacts/validation-selftest-logs/` in artifact `validation-selftest-<run_id>-<run_attempt>`.
- The same run updates committed status file `analysis/validation-selftest-status.json` via `scripts/validation_selftest_status.py`.
- Track `consecutive_green_runs`, `latest_run.overall_status`, `latest_run.generated_at`, and `latest_run.totals.{fixtures,passed,failed}`.
- Streak semantics: a new passing run increments `consecutive_green_runs`; a failing run resets it to `0`; an identical rerun preserves the existing count.

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
