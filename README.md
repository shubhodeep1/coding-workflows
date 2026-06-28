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
| `WORKFLOW_EDITOR_MODEL` | No | `openai/gpt-5.4` (every phase: clarify, plan, orchestrate, orchestrate_poll judge, orchestrate_clarify_respond, validate, workflow-log-analysis, implement, review_autofix editor, orchestrate_poll conflict resolver) | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate, workflow-log-analysis | Model for code editing / reasoning tasks. The pipeline standardises on `gpt-5.4` (unified reasoning + coding) so a single setting changes every phase; the previous legacy editor split (patch-heavy phases on a separate older slug) was retired after the announce-without-emit regression ([openai/codex#11151](https://github.com/openai/codex/issues/11151)) drove repeat no-edit failures, and the underlying `apply_patch_tool_type: "freeform"` interaction with the OpenRouter Responses path was identified by the 2026-05-07 ablation suite (now flipped to `function` in `scripts/codex_model_catalog.json`). Setting this var overrides the default; use per-workflow vars (`WORKFLOW_ORCHESTRATE_MODEL`, `WORKFLOW_VALIDATE_MODEL`, `WORKFLOW_LOG_ANALYSIS_MODEL`) for finer control. |
| `WORKFLOW_VALIDATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | validate | Model override for validation harness generation/diagnosis |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | No | `true` | plan | Auto-trigger implementation when plan is clear |
| `ALLOW_WORKFLOW_EDITS` | No | `true` | review_autofix, implement, update_workflows, orchestrate_poll | Allow AI edits to `.github/workflows` files and automatic wrapper updates. Set to `false` to opt out of auto-updates. Orchestrator conflict-dispatch (`_dispatch_review_for_conflicts`) forwards this value to the dispatched review workflow via `-f allow_workflow_edits=`. |
| `ENABLE_AUTO_MERGE` | No | `true` | review_autofix, orchestrate_poll | Auto-merge PRs (squash) when review passes. Requires "Allow auto-merge" in repo settings. **Orchestrator integration PRs (head ref matching `ORCH_INTEGRATION_BRANCH_PATTERN`, default `^orchestrator/project-`) are unconditionally excluded** even when this is `true`: the orchestrator's `finalize_integration_merge_if_needed` handles their merge synchronously once the project is genuinely complete (all waves merged AND the default branch contains the integration tip). Without this exception, an integration-conflict self-healing dispatch could let review_autofix ship the integration branch partway through the project — stranding subsequent wave PRs on the integration branch with no path to default. The PR-metadata fetch fails closed: a transient API error suppresses auto-merge for that cycle (next sync event retries). See shubhodeep1/binance-blessings#135 for the regression case that motivated the exclusion. **forward-merge fallback PRs (head ref matching `^auto/forward-merge-stable-`, opened by `.github/workflows/forward-merge-stable-to-main.yml` when the automated stable→main merge hits conflict or branch protection) auto-merge via a real merge commit instead of a squash** — gated by `FORWARD_MERGE_FALLBACK_AUTO_MERGE` (default `true`; see its own row). These PRs MUST land as a 2-parent merge commit so `stable`'s tip stays reachable from `main`; `gh pr merge --squash --auto` (the regular auto-merge call) silently strips that ancestry, after which `.github/workflows/promote-main-to-stable.yml`'s pre-flight `git merge-base --is-ancestor HEAD origin/main` check refuses the next promote run with the "squash/rebase strips ancestry" error (see `.github/workflows/promote-main-to-stable.yml:115-126` and the CAUTION banner injected into every fallback PR body at `.github/workflows/forward-merge-stable-to-main.yml:265-270`). So the forward-merge branch instead calls `gh pr merge --merge --auto` — the unattended equivalent of the manual "Create a merge commit", which preserves ancestry. The `^auto/forward-merge-stable-` pattern is hard-coded — the branch prefix is owned by the forward-merge workflow and never varies per repo. Both the codex-agent "Enable auto-merge on PR" step and the `deterministic-skip-merge` sibling job apply this merge-commit path, so a small forward-merge fallback that happens to fall under `AUTOFIX_SKIP_MAX_ADDITIONS` / `AUTOFIX_SKIP_MAX_DELETIONS` cannot short-circuit to a squash merge via the deterministic-skip path either. |
| `FORWARD_MERGE_FALLBACK_AUTO_MERGE` | No | `true` | review_autofix | Controls how forward-merge fallback PRs (head ref `^auto/forward-merge-stable-`, opened by `.github/workflows/forward-merge-stable-to-main.yml`) are merged when review passes with no changes needed. When `true` (default), `review_autofix.yml` enables auto-merge with a **real merge commit** (`gh pr merge --merge --auto`) so `stable`'s commits stay reachable from `main` and `.github/workflows/promote-main-to-stable.yml`'s pre-flight `git merge-base --is-ancestor HEAD origin/main` check keeps passing. Requires "Allow merge commits" **and** "Allow auto-merge" in repo settings; if either is off the enable call logs a `::warning::` and the PR is left for a manual "Create a merge commit". Applies to **both** flavours of fallback PR — conflict-resolved (body: "failed due to merge conflicts", resolved unattended by `[ai-merge-resolve]`) and branch-protection (body: "could not push directly"). Set to any non-`true` value to restore the previous behaviour of leaving every forward-merge fallback PR for a manual merge commit. Independent of `ENABLE_AUTO_MERGE`, but `ENABLE_AUTO_MERGE=false` still disables all auto-merge including this path. |
| `MAX_AUTOFIX_ITERATIONS` | No | `5` | review_autofix | Maximum consecutive autofix rounds before the review loop stops and hands control to the per-PR review-blocked judge. The judge then decides `merge`, `fix` (push a `[judge-fix]` commit which resets the autofix counter — capped at `MAX_REVIEW_BLOCKED_RETRIES`), `merge_with_followup` (merge as-is and open a follow-up issue tracking the deferred gap — preferred over `close_and_reissue` at IS_FINAL when the PR is shippable), or `close_and_reissue`. If the judge step is skipped or fails to handle the PR (`judge_handled != 'true'`), the linked issues are labelled `ai:review-blocked` and a review-blocked comment is posted on the PR. Applies uniformly to every PR mode (orchestrator intermediate, orchestrator final, non-orchestrator). The retrigger guard's PR mode classifier (`orch_intermediate` / `orch_final` / `other`, gated by `ORCH_PR_AUTOFIX_FLOW_ENABLED`) is now used only for observability and the orchestrator-level judge cap bypass on `orch_final`; it no longer overrides the per-PR autofix cap. See [Orchestrator PR autofix flow](#orchestrator-pr-autofix-flow). |
| `ORCH_PR_AUTOFIX_FLOW_ENABLED` | No | `true` | review_autofix, orchestrate_poll | Master switch for the orchestrator-aware PR autofix flow. When `true`, `review_autofix.yml`'s retrigger guard classifies the PR (`orch_intermediate` / `orch_final` / `other`) by base/head branch (used for observability and the orchestrator-side cap bypass on `orch_final`); `orchestrate_poll_process.sh` bypasses the `MAX_JUDGE_CYCLES` cap while the integration→default-branch final PR is open and pending merge so the final PR can run unlimited 5-autofix→judge cycles until mergeable. The per-PR autofix loop itself uses `MAX_AUTOFIX_ITERATIONS` uniformly across every mode. Set to `false` to force `orch_pr_mode` to stay at `other` for every PR (head/base never inspected) and disable the orchestrator-side cap bypass (`MAX_JUDGE_CYCLES=25` then applies to the final-PR loop too). See [Orchestrator PR autofix flow](#orchestrator-pr-autofix-flow). |
| `ORCH_INTEGRATION_BRANCH_PATTERN` | No | `^orchestrator/project-` | review_autofix | POSIX-extended regex used by the retrigger guard to identify orchestrator integration branches (head ref) and orchestrator-targeted bases. Defaults match the orchestrator's conventional branch naming (`orchestrator/project-<TRACKING_ISSUE_NUMBER>` set by `.github/workflows/orchestrate.yml`). Override only if you have customised the orchestrator branch naming. |
| `CHECK_RUNS_AUTOFIX_ENABLED` | No | `true` | review_autofix | When `true` (default), the workflow snapshots failed and incomplete GitHub check-runs on the PR head SHA into `${PR_CHECK_RUNS_CONTEXT_FILE}` and feeds it to reviewers + editor so CI / lint failures are detected and fixed on every run. The "Collect PR check-run failures" step in `.github/workflows/review_autofix.yml` calls `gh_retry gh api --paginate --slurp "repos/{repo}/commits/{sha}/check-runs?per_page=100"` once per poll iteration; this is one *logical* snapshot attempt, but it may consume multiple underlying GitHub API requests (one per pagination page, plus up to `GH_RETRY_MAX_ATTEMPTS` retries on transient failures), so operators sizing rate-limit budgets should treat the per-iteration cost as ≥1 requests rather than exactly one. Reviewers see the file as a numbered context section, and the editor prompt elevates failed entries to the top of the WILL_FIX priority order (see `scripts/review_apply_fixes.sh` "CI / LINT CHECK-RUN FAILURES" block). Fail-open: an unrecoverable API failure writes a sentinel file (`collection_status: api_error`) and the autofix pipeline continues — reviewers/editor are explicitly told to treat the absence-of-failures signal as unknown rather than confirmed-passing. Set to `false` to disable check-run collection entirely (the file still gets written with `collection_status: disabled` and zero counts so preflight always passes). |
| `CHECK_RUNS_WAIT_TIMEOUT_SECS` | No | `300` | review_autofix | Target/nominal maximum seconds the "Collect PR check-run failures" step waits for in-progress / queued check-runs to complete before snapshotting. The wait excludes the current workflow run's own check-runs from the in-flight count — entries whose `details_url` contains `/actions/runs/${{ github.run_id }}/job/` are filtered out — so the `codex-agent` job's own `in_progress` check-run cannot keep the count perpetually above zero and self-wait until this timeout expires; sibling workflow runs on the same SHA (e.g. the implement run's `Agent` job, lint/test workflows) are still waited on. The loop now reuses the last self-excluded in-flight snapshot signature and backs off from the base poll interval to 2× / 4× that interval when the snapshot is unchanged. The poll request itself runs under `gh_retry`, so retry/backoff sleep (including waiting for GitHub rate-limit reset) can push actual wall-clock elapsed time past this configured value; treat it as the collector's wait budget, not a hard cap. When the timeout trips, the snapshot proceeds with whatever data exists and emits a `::warning::CHECK_RUNS_WAIT_TIMEOUT` log line. Integer in `0..3600`; invalid or out-of-range values clamp to `300`. Set to `0` to skip waiting entirely (snapshot whatever is currently completed). |
| `CHECK_RUNS_POLL_INTERVAL_SECS` | No | `20` | review_autofix | Base sleep interval between check-run status poll attempts in the wait loop. Unchanged in-flight snapshots back off to 2× and 4× this base interval (capped at 120s) before the timeout path fails open with the latest available snapshot. Integer in `5..300`; invalid values clamp to `20`. Each poll iteration runs one `gh_retry gh api --paginate --slurp "/repos/{repo}/commits/{sha}/check-runs?per_page=100"` call (≥1 underlying GitHub API requests once pagination + `gh_retry` are accounted for), so a given iteration's wall-clock duration can exceed this interval when retries/backoff apply. |
| `CHECK_RUNS_LOG_TAIL_BYTES` | No | `16384` | review_autofix | Per-failed-check-run cap on the `failed[i].log_tail` field written into `${PR_CHECK_RUNS_CONTEXT_FILE}`. When `failed[i].summary` is empty or whitespace-only, the snapshot writer in `.github/workflows/review_autofix.yml` parses `run_id`/`job_id` out of the already-captured `failed[i].details_url`, calls the Actions job-logs REST endpoint to resolve a short-lived signed log URL (one GitHub API call per affected failed check-run; `actions:read` on the workflow's `GH_PAT` is required), then range-fetches only the last `CHECK_RUNS_LOG_TAIL_BYTES` bytes and appends them truncated to the last 200 lines under `failed[i].log_tail`. This is the actionable failure detail the editor falls back on when `output.summary` is empty — typical for consumer CI steps that don't emit `::error::` annotations (bare `npm test`, `pytest`, `make test`). Set to `0` to disable log_tail capture entirely; the rest of the snapshot still ships. Invalid integers fall back to `16384`; values above `131072` clamp to `131072` so the snapshot stays bounded. Operators with many concurrent failures per PR should weigh raising this against the 80 KB embed budget in the reviewer/editor prompts (`scripts/review_run_reviewers.sh`, `scripts/review_apply_fixes.sh`). Fail-open: any API error, missing `actions:read` scope, missing token, malformed `details_url`, or ranged-fetch failure writes an empty log_tail and the pipeline continues. |
| `REVIEW_FLOOR_RULES_ENABLED` | No | `1` | review_autofix | Enable floor-rule tagging before the editor runs. Matching findings are emitted to `floor_tags.txt` and treated as non-skippable floor signals. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | No | `(empty; built-in fallback)` | review_autofix | Optional path to a custom floor-rule keyword catalog consumed by `scripts/review_floor_rules.sh`. When unset, missing, or unreadable, the script falls back to its built-in keywords and logs a warning. |
| `REVIEW_CONSOLIDATOR_ENABLED` | No | `1` | review_autofix | Enable the advisory consolidator stage that writes `consolidator_raw.txt` / `review_issues.txt`. The editor still treats `reviewer_bundle.txt` as authoritative. |
| `REVIEW_CONSOLIDATOR_MODEL` | No | `openai/gpt-5.4` | review_autofix | Model used by the consolidator stage. |
| `REVIEW_CONSOLIDATOR_REASONING` | No | `xhigh` | review_autofix | Reasoning effort for the consolidator stage. |
| `REVIEW_CONSOLIDATOR_TIMEOUT_SECS` | No | `300` | review_autofix | Wall-clock timeout for the consolidator model call. |
| `REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT` | No | `16000` | review_autofix | Max output tokens requested from the consolidator model. |
| `REVIEW_PARSER_FAILOPEN` | No | `1` | review_autofix | When enabled, parser failures downgrade to empty / passthrough advisory artifacts and the editor continues from the raw reviewer bundle. |
| `REVIEW_LEDGER_ENABLED` | No | `1` | review_autofix | Enable per-PR ledger persistence and emit `ledger_status.txt` for cross-iteration issue tracking. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | No | `2` | review_autofix | Persist-count threshold for promoting still-open advisory issues to `accepted-residual`. |
| `REVIEW_LEDGER_PATH` | No | `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` | review_autofix | Default per-PR ledger path; the workflow caches it across autofix iterations by default. |
| `REVIEW_REVIEWER_CHECKLIST_ENABLED` | No | `1` | review_autofix | Append the reviewer checklist prompt block when the checklist template is available. |
| `REVIEW_REVIEWER_ITERATION_SCOPING` | No | `1` | review_autofix | On later iterations, scope reviewer prompts from last-run changed files plus actionable ledger rows; the first pass stays full-diff. |
| `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` | No | `8` | review_autofix | Seconds the post-commit and editor-changes-lost retrigger steps wait before checking for an already-queued peer review run on the same PR branch. If a peer is found the retrigger skips its own `workflow_dispatch` to avoid creating a redundant queued run (and extra API/UI noise) in the `pr-autofix-${PR}` concurrency group (see [Autofix retrigger dedup](#autofix-retrigger-dedup)). Must be an integer in `0..60`; invalid values clamp to `8`. |
| `AUTOFIX_SKIP_SELF_TRIGGERED` | No | `false` | review_autofix | Skip the full reviewer/editor cycle on `pull_request.synchronize` events whose HEAD commit is a `[ai-autofix]` commit pushed by the configured bot account (GitHub-attributed identity, see `AUTOFIX_BOT_LOGIN`). These synchronize events are self-triggered by the prior autofix commit and otherwise cost a second full review pass (5 reviewers + consensus + editor) per fix round — roughly 2× LLM spend per autofix iteration. The gate job in `review_autofix.yml` queries the HEAD commit via one `GET /repos/{repo}/commits/{sha}` call and extracts `(commit.message first line, author.login, committer.login)` — `.author.login` / `.committer.login` are GitHub-resolved from the push credentials and are not user-controlled (unlike `.commit.author.email`, which git will accept from any local config). The gate sets `should_run=false` only when the subject starts with `[ai-autofix]` AND at least one of `.author.login` / `.committer.login` equals `AUTOFIX_BOT_LOGIN` (default `codex`); fails open on API error or when both logins are empty. The post-commit `workflow_dispatch` retrigger step applies a mirror guard; when `AUTOFIX_CONTINUATION_ENABLED=true` (default) the mirror skips only ledger-only commits (§20.3) and the legacy opt-in case, so productive `[ai-autofix]` commits immediately dispatch the next iteration via `workflow_dispatch` (see `AUTOFIX_CONTINUATION_ENABLED` and probably_unnecessary_but_read_if_stuck.md §20.4). `[ai-merge-resolve]` / conflict-resolved pushes also dispatch a follow-up verification pass for post-conflict-resolution safety. `workflow_dispatch`, `opened`, `reopened`, and `ready_for_review` events always run regardless of this flag. Set to `true` to opt in and restore the prior LLM-cost-saving skip behaviour. Safety net for orchestrator-tracked PRs: the orchestrator stall cron (`internal-orchestrate-poll.yml`, `*/5 * * * *`) re-kicks autofix via `workflow_dispatch` (which bypasses the skip) if a phase-timer threshold trips; continuation closes the same gap in-run for non-orchestrator PRs. Audit via `AUTOFIX_GATE_SKIP reason=self_triggered_autofix` / `AUTOFIX_GATE_NO_SKIP_IDENTITY` / `AUTOFIX_GATE_SKIP_QUERY_FAILED` / `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix` log lines (see [Autofix retrigger dedup](#autofix-retrigger-dedup)). |
| `AUTOFIX_BOT_LOGIN` | No | `codex` | review_autofix | GitHub login that the gate job accepts as the authoritative bot identity for the self-triggered autofix skip. Compared against `.author.login` / `.committer.login` on the HEAD commit API response — both are GitHub-attributed (resolved server-side from push credentials), not user-controlled git metadata. Override if you run the workflow under a fork of codex that pushes as a different bot account (e.g. `codex-bot`, `my-org-codex`). Unset or empty falls back to the default `codex` (via shell `${AUTOFIX_BOT_LOGIN:-codex}` expansion) — to enable the skip, set `AUTOFIX_SKIP_SELF_TRIGGERED=true` instead. |
| `AUTOFIX_SKIP_DOC_ONLY` | No | `true` | review_autofix | Deterministic pre-review skip — doc-only branch. When `true`, the gate job skips the reviewer panel + editor cycle if every changed file in the PR matches the doc-only glob set: `*.md`, `*.txt`, `*.rst` (case-insensitive suffix on basename), `LICENSE*`, `CHANGELOG*` (case-insensitive prefix on basename — matches GitHub's own LICENSE/CHANGELOG detection, which recognises `license.txt`, `Changelog.md`, `LICENCE`, etc.), or `docs/**` (case-insensitive, depth-agnostic but rooted: `docs/x/y.md` matches; `src/docs/x.py` does NOT). When the gate fires, the new sibling job `deterministic-skip-merge` adds `ai:review-skipped` to the PR, sets `ai:ready-to-merge` on every linked issue (resolved via GraphQL `closingIssuesReferences` only — no body/title regex fallback, because the doc-only skip path is the most likely place for incidental issue references in prose to false-match; orchestrator-managed PRs use explicit `Fixes #N` keywords which GraphQL resolves correctly), and enables auto-merge (squash) — mirroring the tail of the normal codex-agent path so the orchestrator phase machine still advances. Merge-conflict resolver, summarizer, and review-blocked judge are all skipped on this path; if a doc-only PR happens to have a conflict, auto-merge blocks and the orchestrator stall cron re-dispatches per existing recovery contracts. Set to `false` to disable the doc-only branch (the size-threshold branch via `AUTOFIX_SKIP_MAX_ADDITIONS` / `AUTOFIX_SKIP_MAX_DELETIONS` still applies). Per-PR override: title contains `[force-review]` OR PR carries the `force-review` label — skip is bypassed and full review runs. The doc-only `/files` lookup is skipped entirely when the size-threshold branch already qualifies (cheaper path runs first), so most small-and-doc-only PRs cost zero extra API calls beyond the existing PR-state fetch. Paginated `/files` output is merged via `jq -s 'add // []'` so the doc-only check stays correct on PRs with > 1 page of files. Audit via `AUTOFIX_GATE_DET_SKIP_OVERRIDE` / `AUTOFIX_GATE_DET_SKIP_EVAL pr=<n> files=<k\|skipped> additions=<a> deletions=<d> max_add=<x> max_del=<y> doc_only=<bool> small_diff=<bool> skip=<bool> reason=<docs_only\|small_diff\|empty>` / `AUTOFIX_GATE_DET_SKIP_FILES_UNAVAILABLE` log lines. Fails open: any `/files` lookup failure leaves the doc-only check inconclusive and a non-small PR runs full review. |
| `AUTOFIX_SKIP_MAX_ADDITIONS` | No | `10` | review_autofix | Deterministic pre-review skip — size-threshold branch (additions). Together with `AUTOFIX_SKIP_MAX_DELETIONS`, defines the max diff size that auto-qualifies for the skip path **regardless of file types**. The gate skips reviewer panel + editor when total additions ≤ this value AND total deletions ≤ `AUTOFIX_SKIP_MAX_DELETIONS` (both bounds simultaneously). Totals are read from the `additions` and `deletions` fields on the existing `GET /repos/{repo}/pulls/{n}` response — no separate `/files` call is made on this branch. The size-threshold branch is OR-ed with the doc-only branch (`AUTOFIX_SKIP_DOC_ONLY`) — a small-but-code change still skips, accepting that risk in exchange for cycle-time savings on trivial fixes. Setting this value to `0` does **not** fully disable the branch — `additions ≤ 0` still matches a PR with zero additions (and `0/0` diffs do occur in edge cases like metadata-only renames or whitespace-only no-op pushes). To effectively suppress the size-threshold branch, set both this and `AUTOFIX_SKIP_MAX_DELETIONS` to `-1` so no non-negative addition/deletion count can satisfy the bound; alternatively rely on the per-PR `force-review` override. Per-PR override: `[force-review]` title marker or `force-review` label. Must be an integer; on parse failure the bash arithmetic `[ X -le Y ]` test fails which evaluates as "not small" (full review runs — fails open). |
| `AUTOFIX_SKIP_MAX_DELETIONS` | No | `10` | review_autofix | Deterministic pre-review skip — size-threshold branch (deletions). See `AUTOFIX_SKIP_MAX_ADDITIONS`; both bounds must be satisfied for the size-threshold branch to fire. Setting this value to `0` does **not** fully disable the branch — `deletions ≤ 0` still matches a 0-deletion PR. Set to `-1` (together with `AUTOFIX_SKIP_MAX_ADDITIONS=-1`) to suppress the size-threshold branch entirely. Per-PR override: `[force-review]` title marker or `force-review` label. Must be an integer; fails open to "not small" on parse failure. |
| `AUTOFIX_CONTINUATION_ENABLED` | No | `true` | review_autofix | When `true` (the default), the `Re-trigger review via workflow_dispatch` step in `review_autofix.yml` proceeds to dispatch the next autofix iteration via `workflow_dispatch` after a **productive** `[ai-autofix]` commit (`DID_COMMIT=true` AND `LEDGER_ONLY_COMMIT!=true` AND `CONFLICT_RESOLVED!=true`). This closes the ~0–120 min idle window where an `[ai-autofix]` push would otherwise wait for the orchestrator stall cron (which does not scan non-orchestrator PRs at all). Ledger-only commits (§20.3) still route to the clean-review tail in the same run — no continuation dispatch is issued. Conflict-resolved commits keep their pre-continuation dispatch path. Set to `false` to restore the pre-continuation behaviour where `AUTOFIX_SKIP_SELF_TRIGGERED` alone gated productive autofixes out of the dispatch step. `workflow_dispatch` bypasses the gate job's self-triggered skip by design — continuation is a first-class successor run, not a redundant verification. Pre-dispatch guard: settle delay (`AUTOFIX_CONTINUATION_SETTLE_SECS`). Iteration-cap handling remains in the dispatched run's `retrigger_guard` path (which gates reviewers/editor and routes exhaustion to the review-blocked judge). Alerts: the continuation path is silent (no Telegram); stall-cron `Stall recovery: re-triggered review …` alerts are unchanged and still fire only for genuine orchestrator-tracked stalls. Continuation dispatches **bypass** the post-commit peer-dedup (`autofix_retrigger_has_inflight_peer`) because the only same-branch peer is the gate-skipped self-triggered synchronize run, which cannot be a successor — leaving dedup enabled for continuation would stall non-orchestrator PRs that the stall cron does not scan. Legacy non-continuation dispatches and the `editor-changes-lost` retrigger retain peer-dedup. Audit via `AUTOFIX_CONTINUATION_DISPATCH_ISSUED` / `AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected ... continuation=true` / `AUTOFIX_PEER_CHECK_BYPASSED reason=continuation_dispatch` / `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix continuation_enabled=<val>` log lines. See probably_unnecessary_but_read_if_stuck.md §20.4 for the contract. |
| `AUTOFIX_CONTINUATION_SETTLE_SECS` | No | `10` | review_autofix | Seconds the continuation path `sleep`s between the push and the `workflow_dispatch` call, to let GitHub's internal indices catch up before the dispatched run checks out the new HEAD SHA. Integer in `1..60`; invalid or out-of-range values clamp to `10`. Not applied to the conflict-resolved dispatch path (that keeps its existing `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` peer-wait). |
| `ENABLE_REVIEWER_TWO_PASS` | No | `true` | review_autofix | When true, reviewers run two passes per iteration: pass 1 at `xhigh` reasoning (broad sweep), then pass 2 with a cross-pollination summary of pass 1 findings. By default, pass 2 uses `REVIEWER_PASS2_REASONING_SMALL=high` for diffs below `REVIEWER_PASS2_DIFF_LARGE_LOC=200` LOC and `REVIEWER_PASS2_REASONING_LARGE=xhigh` at or above that threshold; smoke / explicit `REVIEWER_REASONING_EFFORT` overrides still win. Set to `false` to use a single pass at the scheduled reasoning level. |
| `XPOLL_SUMMARISER_MODEL` | No | `openai/gpt-5.4-mini` | review_autofix | Model slug (resolved through codex-cli's OpenRouter provider) used by `scripts/summarize_reviewer_consensus.sh`. After each review pass finishes, this model consolidates every reviewer's output into one ledger: a `=== CONSENSUS FINDINGS ===` block with cross-reviewer dedup (entries carry `flagged_by: [slug, ...]`) followed by per-reviewer sections. The pass-1 ledger feeds pass-2 reviewers; the pass-2 ledger is written to `REVIEWER_CONSENSUS_FILE` and feeds the editor + memory-record step. |
| `XPOLL_SUMMARISER_REASONING` | No | `medium` | review_autofix | Reasoning effort (`xhigh` / `high` / `medium` / `low` / `none`) applied to the summariser model via its isolated `CODEX_HOME` config.toml. Default is `medium` per the OpenAI gpt-5.4 prompt guide (consolidating reviewer findings is a research/synthesis task). Earlier revisions defaulted to `none` after observing rc=0/empty-stdout responses on `gpt-5.4-mini` at higher reasoning; if that failure mode reappears, set `XPOLL_SUMMARISER_REASONING=none` per-repo to revert. Isolated config guarantees the override cannot leak into the editor's codex-cli invocation. |
| `XPOLL_SUMMARISER_LINES_PER_REVIEWER` | No | `160` | review_autofix | Target max per-reviewer section lines; summariser is told to collapse related findings (`(N related items)` suffix) rather than drop them when over-budget. Overall ledger target is this value × reviewer count + 120. |
| `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS` | No | `2400` | review_autofix | Per-attempt wall-time timeout for a single codex-cli summariser invocation. Raised from `1200` after observing repeat 20-min timeouts on `xhigh`-reasoning pass-1 calls over ~24 KB prompts burning ≥40 min of runner time per run before finally succeeding on attempt 3. On timeout / non-zero exit / empty stdout the summariser retries up to 10 times with exponential backoff (5s, 10s, 20s, 40s, 80s, 160s, 320s, 640s, 1280s between attempts; no cap), then hard-fails the workflow (the job-level "Telegram failure" step surfaces the incident). The PR-closed sentinel is polled every 2s during each backoff so a mid-retry PR close exits cleanly without waiting out the remaining delay. |
| `XPOLL_SUMMARISER_MAX_INPUT_LINES` | No | `3000` | review_autofix | Pre-truncation ceiling per reviewer output before concatenation into the summariser prompt. Prevents a pathological reviewer output from blowing the summariser's context budget. |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | No | `true` | review_autofix | When true, non-orchestrator PRs that exhaust autofix iterations invoke a judge (LLM) to decide: merge as-is, push a fix commit, merge_with_followup (merge as-is and open a follow-up issue tracking a deferred gap — only when merge is confirmed and follow-up details are provided), or close and reissue. Orchestrator-managed PRs are skipped (handled by the poller). PRs without linked issues use the PR title/body as requirement context. |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | No | `xhigh` | review_autofix | Reasoning effort for the review-blocked judge in non-orchestrator PRs (`xhigh`, `high`, `medium`, `none`). |
| `MAX_REVIEW_BLOCKED_RETRIES` | No | `2` | review_autofix, orchestrate_poll | Maximum judge `fix` retries for review-blocked PRs before forcing a final decision (`merge`, `merge_with_followup`, or `close_and_reissue` — no further `fix` attempts at IS_FINAL). Used by both the review_autofix judge (counts `[judge-fix]` commits) and the orchestrator poller. Bump to `3` to give the judge one more `fix` attempt before falling back to a terminal action. |
| `WORKSPACE_HOOK_TIMEOUT_SECONDS` | No | `600` | implement, validate | Timeout in seconds for optional workspace lifecycle hooks executed by `scripts/run_workspace_hook.sh`. Invalid or non-positive values fall back to `600`. `after_create` / `before_run` failures are fatal; `after_run` / `before_remove` failures are logged and ignored. |
| `WORKSPACE_REUSE_ENABLED` | No | `false` | implement, review_autofix, validate | Enables cache-backed per-issue workspaces under `${RUNNER_TEMP}/workspaces/<WORKSPACE_KEY>`. When `false`, the workflows keep the legacy per-run workspace layout; when `true`, exact workspace-cache restores can set `CREATED_NOW=false` so one-time setup and `after_create` hooks are skipped on reuse. |
| `CODEX_THREAD_REUSE_ENABLED` | No | `false` | implement, review_autofix, validate | Enables same-run Codex session reuse through `scripts/codex_thread_reuse.sh` plus the `mode-*-continuation.txt` prompts. Unsupported resume capability or helper failures fail open to the fresh full-prompt path. |
| `CODEX_STALL_GUARD_ENABLED` | No | `false` | implement, orchestrate_poll, review_autofix, validate | Enables event-idle Codex stall killing in `scripts/codex_stall_guard.sh`. When `false`, the helper stays observe-only and emits `codex_stall_observed` telemetry without terminating the child process. |
| `CODEX_STALL_TIMEOUT_SECONDS` | No | `600` | implement, orchestrate_poll, review_autofix, validate | Seconds of heartbeat inactivity before the stall helper records a stall and, when `CODEX_STALL_GUARD_ENABLED=true`, begins termination. Invalid values fall back to `600`. |
| `CODEX_STALL_KILL_GRACE_SECONDS` | No | `30` | implement, orchestrate_poll, review_autofix, validate | Grace period between the guard's SIGTERM and forced kill for an idle Codex child. Invalid values fall back to `30`. |
| `LEDGER_SUBSTATES_ENABLED` | No | `true` | orchestrate_poll | Poller-side gate for run-substate enrichment in state-snapshot exports. When `false`, `scripts/build_state_snapshot.py` omits `running[].substate` even if ledger entries contain `run_substate` metadata. |
| `STATE_SNAPSHOT_ARTIFACT_ENABLED` | No | `true` | orchestrate_poll | Upload the per-tick `state-snapshot` artifact built by `scripts/build_state_snapshot.py`. Set to `false` to disable both the artifact upload and the optional branch-publish step. |
| `STATE_SNAPSHOT_BRANCH_ENABLED` | No | `true` | orchestrate_poll | Publish the rolling state-snapshot history to a dedicated branch after writing the artifact. Enabled by default; set to `false` to disable branch publication (the per-tick artifact upload is unaffected). |
| `STATE_SNAPSHOT_HISTORY_DEPTH` | No | `100` | orchestrate_poll | Maximum number of tick snapshots retained on the published state-snapshot branch. Invalid values fall back to `100`. |
| `STATE_SNAPSHOT_BRANCH_NAME` | No | `state-snapshot` | orchestrate_poll | Branch name used when `STATE_SNAPSHOT_BRANCH_ENABLED=true` publishes the rolling state-snapshot history. |
| `RUNTIME_BLOCKER_CHECK_ENABLED` | No | `true` | orchestrate_poll | Enables blocker-aware runtime dispatch gating from `dependency_edges` before the poller creates or dispatches a managed issue. Truthy values (`1/true/yes/on`, case-insensitive) keep the gate on; any other value disables it and restores the legacy fail-open dispatch path with no blocker deferral. |
| `ENABLE_VALIDATION` | No | `true` | orchestrate_poll | When true, a `complete` judge verdict transitions the tracking issue into runtime validation (`ai:validating`) and completion occurs only after validation passes. |
| `MAX_VALIDATE_CYCLES` | No | `3` | orchestrate_poll | Maximum runtime validation cycles (initial run + fix/revalidate loops) before forcing `ai:validation-failed`. |
| `MAX_SELF_HEAL_ATTEMPTS` | No | `2` | validate | Maximum in-process self-heal attempts per `validate_process.sh` invocation. Self-heal patches one of the four validation prompt files locally and re-execs the pipeline, and does NOT increment `MAX_VALIDATE_CYCLES`. Set to `0` to disable. See [Validation self-healing](#validation-self-healing). |
| `VALIDATE_PREFLIGHT_PYFLAKES_ENABLED` | No | `true` | validate | When `true`, the preflight phase of `scripts/validate_process.sh` runs `pyflakes` and `ruff check --select $VALIDATE_PREFLIGHT_PYFLAKES_RULES` against every quoted `python3 - <<'PY' ... PY` heredoc body under `validation/**/*.sh`. Catches undefined-name (F821) / unused-import / redefinition bugs that `ast.parse` alone cannot see and that runtime tests miss when the bug lives in an unexercised conditional branch (observed as `unknown_error:NameError` in consumer-repo autobet finalize logs). Missing tools are auto-installed via `python3 -m pip install --user`; install failure fails open with a `::warning::` and skips the check. Invalid values are coerced to `true`. |
| `VALIDATE_PREFLIGHT_PYFLAKES_RULES` | No | `F` | validate | Ruff rule selector passed to `ruff check --select`. Default `F` covers all pyflakes-equivalent rules (F401 unused import, F811 redefinition, F821 undefined name, F823 local-before-assign, F841 unused local, etc.). Must match `^[A-Z0-9,]+$`; invalid values fall back to `F`. Narrow to `F821` if operator wants only the NameError bug class to block. |
| `VALIDATE_WORKFLOW_NAME` | No | `ai-validate.yml` | orchestrate_poll | Workflow filename to dispatch for runtime validation. Override to `internal-validate.yml` for repos using the internal naming convention. Falls back to `internal-validate.yml` automatically if the primary name fails. |
| `MAX_JUDGE_CYCLES` | No | `25` | orchestrate_poll | Maximum judge evaluation cycles per project before forcing failure. Prevents infinite fix-up loops when the judge repeatedly returns `in_progress`. **Orchestrator final-PR bypass:** when `ORCH_PR_AUTOFIX_FLOW_ENABLED=true` (default) and the integration→default-branch final PR is open with `final_merge_status=pending`, this cap is bypassed for the final-PR loop only — the loop runs unlimited 5-autofix→judge cycles until the PR is mergeable. The cap remains in force for sub-issue stalls, recovery loops, and the intermediate-PR phase (per-sub-issue judge runs are governed by `MAX_REVIEW_BLOCKED_RETRIES` inside `review_autofix.yml`, not by this orchestrator-level counter). The bypass emits a `[final-merge] judge cap bypassed` log line each time it fires. See [Orchestrator PR autofix flow](#orchestrator-pr-autofix-flow). |
| `ENABLE_CLEAN_WAVE_JUDGE_SKIP` | No | `true` | orchestrate_poll | When true, a completed clean wave (no failures, not stuck-wave) advances mechanically without invoking the judge. Also skips the judge on clean project completions (all waves merged, no failures, no review-blocked issues) — the verdict is deterministic (`complete`). Set to `false` to force judge execution on every wave completion and project finalization. |
| `ORCHESTRATOR_MAX_CLARIFY_CYCLES` | No | `3` | orchestrate_clarify_respond | Maximum orchestrator clarification auto-answer cycles per issue. When the limit is exceeded, or when a clarify hash repeats, `orchestrate_clarify_respond` stops posting auto-answers and escalates the issue to `ai:blocked` for explicit human intervention. A backup comment-count guard counts existing `/answer [auto-answered-by-orchestrator]` comments on the issue thread (0 extra API calls) and blocks when the count reaches this limit, even when the memory-based guard fails open. |
| `STALL_THRESHOLD_MINUTES` | No | `120` | orchestrate_poll | Fallback minutes an issue can remain in the same pipeline phase before auto-recovery. Used when no per-phase override is set. |
| `STALL_THRESHOLD_NO_LABELS_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for issues with no AI pipeline labels (pre-pipeline). |
| `STALL_THRESHOLD_CLARIFICATION_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:clarification` phase. |
| `STALL_THRESHOLD_PLANNING_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:planning` phase. |
| `STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES` | No | `60` | orchestrate_poll | Stall threshold for `ai:awaiting-approval` phase. |
| `STALL_THRESHOLD_IMPLEMENTING_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:implementing` phase. |
| `STALL_THRESHOLD_DONE_MINUTES` | No | `120` | orchestrate_poll | Stall threshold for `ai:done` phase (review/autofix). |
| `REVIEW_RUN_MAX_RUNTIME_MINUTES` | No | `250` | orchestrate_poll | Freshness window the in-flight / zombie guards (`build_active_issue_set`, the `retrigger_review` inline guard, and `_direct_inflight_review_run_on_branch`) apply to **review-family** runs (AI Review, Internal Review, Review Autofix), which can legitimately run past `STALL_THRESHOLD_MINUTES` up to the codex-agent job's 240-min timeout. A review still editing within this window is treated as active (not a zombie) so stall recovery does not clobber it with an empty commit. Floored at `STALL_THRESHOLD_MINUTES` in-script. |
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
| `ORCH_FINAL_MERGE_REQUIRED_CHECKS` | No | `CI,Integration PR readiness check,Lint plan-archival completeness,Lint PR body for auto-close keywords against orchestrator-tracking issues,review / gate` | orchestrate_poll, review_autofix | Comma-separated list of check-run names that the shared `_pr_checks_completed` gate (`scripts/pr_checks_lib.sh`, sourced by both `scripts/orchestrate_poll_process.sh` and `scripts/review_rb_judge.sh`) treats as blocking. As of the shared-gate consolidation this required-checks filter governs **every** orchestrator squash merge — the final integration→default merge **and** the four review-blocked merge paths (`merge` / force-merge / no-fix / `merge_with_followup`) — plus the standalone review-blocked judge's `merge_with_followup` gate. Previously only the final merge used the filter while the review-blocked paths blocked on ANY failing check-run, which deadlocked a judge-approved merge whenever a non-required/environmental check (e.g. CodeQL when code scanning is disabled at the repo level) was permanently red. Resolution inside the gate: (1) when the PR's base ref is branch-protected with a non-empty `required_status_checks.contexts` list, **that list wins** and this env var is ignored (server-side truth); (2) otherwise this env var (or the built-in default) names the blocking set; (3) any check-run whose `name` is NOT in the resolved set is treated as advisory and ignored even when failing. Sentinels: `*` restores the legacy fail-closed-on-any-failure behaviour (every check-run is blocking); explicit empty string (`ORCH_FINAL_MERGE_REQUIRED_CHECKS=`) means allow-all (no check-run blocks; rely entirely on GitHub branch protection at merge time). The default reflects the five checks the orchestrator already produces on every integration PR (`CI`, `Integration PR readiness check`, `Lint plan-archival completeness`, `Lint PR body for auto-close keywords against orchestrator-tracking issues`, `review / gate`). Override per-repo when consumers add new required checks of their own (extend the list) or when a flapping advisory third-party check (e.g. Copilot review, optional reviewer bots) is silently stalling the orchestrator (drop it from the list). Whitespace around comma-separated tokens is trimmed. |
| `ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS` | No | `6` | orchestrate_poll | Hours the orchestrator may sit in the budget-ineligible deferral path of `finalize_integration_merge_if_needed` (`FINAL_MERGE_BUDGET_ELIGIBLE=0` — required checks still pending, mergeability still computing, or conflict-resolver self-healing path) on the same final PR head SHA before firing exactly one CRITICAL Telegram alert plus a tracking-issue comment summarising the blocking check-runs. The clock resets when (a) the head SHA changes (a fresh autofix push deserves a fresh window), (b) a finalize attempt succeeds (merge lands), or (c) the project advances to `status=complete`. Set to `0` to disable the alert path. Must be a non-negative integer; invalid values fall back to `6`. Layer 2 is alert-only — it never auto-escalates to `ai:blocked` (operator decision); resolution paths are: re-run/dismiss the blocking check-runs, merge the PR manually, or set `ORCH_FINAL_MERGE_REQUIRED_CHECKS` to exclude the advisory check. |
| `ORCH_INTEGRATION_STALE_ALERT_HOURS` | No | `0` | orchestrate_poll | Minimum hours since the last successful integration-branch squash to default before the poller emits `INTEGRATION_STALE_ALERT_SENT` and a `WARNING` Telegram alert when the integration branch is still ahead of default. Uses `last_main_squash_at_utc` from the tracking state as the anchor; legacy states without that field seed it from the first ahead-of-default observation. **Set to `0` to disable the alert path entirely** (parity with `ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS`); the reusable workflow `.github/workflows/orchestrate_poll.yml` now passes `0` by default, because `main` only catches up at the single end-of-project squash, so the alert otherwise fires for the whole lifetime of every multi-wave project — healthy progress, not a stall (the jammed-final-merge case is covered by `ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS` and backpressure via `ORCH_INTEGRATION_MAX_AHEAD_COMMITS`). Set the repo variable to a positive integer such as `6` to re-enable. The script fallback remains `6` for direct callers that omit or mis-set the env. Must be a non-negative integer; invalid values fall back to `6`. |
| `ORCH_INTEGRATION_STALE_REALERT_HOURS` | No | `12` | orchestrate_poll | Minimum hours between repeated stale-integration alerts for the same tracking state while the integration branch remains ahead of default. The dedupe window is stored in `integration_stale_last_alerted_at_utc` and is cleared when the branch catches up / a squash merge lands. Must be a positive integer; invalid values fall back to `12`. |
| `ORCH_INTEGRATION_MAX_AHEAD_COMMITS` | No | `10` | orchestrate_poll | Integration-branch backpressure **floor**. The poller pauses additional sub-issue squash merges (labels the tracking issue `ai:integration-backpressure`, points operators at the eager integration PR) once the integration branch is ahead of default by at least the *effective* threshold, then auto-clears when `ahead_by` falls back below it. The effective threshold is `max(ORCH_INTEGRATION_MAX_AHEAD_COMMITS, planned_issue_count + ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN)` so a project's own planned sub-issue merges can never trip backpressure before the integration→default PR drains (which only happens at completion) — otherwise a project with more commits than this floor would self-deadlock. This value still acts as the floor for anomalous over-drift beyond a project's planned scope. Must be a positive integer; invalid values fall back to `10`. |
| `ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN` | No | `20` | orchestrate_poll | Headroom added to a project's planned sub-issue count when deriving the size-aware backpressure threshold (see `ORCH_INTEGRATION_MAX_AHEAD_COMMITS`). Absorbs the non-sub-issue commits a healthy project still accrues on its integration branch before draining — `main`→integration sync-merges, judge conflict-merges, and judge-added fix-up issues. Raised from `5` to `20` after project #2974 (10 planned issues) drifted to 22 commits ahead and self-deadlocked under the old `max(10, 10+5)=15` threshold: backpressure paused the very sub-issue merges needed to reach completion, and completion is what drains the integration branch to clear backpressure. Raise it for projects that routinely spawn many fix-ups; lower it to tighten the gate. Must be a non-negative integer; the `orchestrate_poll` workflow sets `20` by default and invalid values fall back to the script-level `5`. |
| `MAX_VALIDATION_FIX_BATCH_CYCLES` | No | `30` | orchestrate_poll | Maximum poll cycles a single validation fix-up batch (the set of issue numbers extracted from the most recent `## 🧪 Runtime validation found fixable issues` tracking comment) can sit in "still in progress" before the poller escalates via `mark_validation_failed` — which still honours `MAX_VALIDATION_RECOVERY_ATTEMPTS` for judge re-evaluation. Counter resets when a new fix-issues comment arrives, when the batch completes (all issues merged), or when `mark_validation_failed` clears the active list. Each fix-up issue is now also inspected for its live GitHub `state`/`state_reason`, so a fix-up issue closed without the `ai:closed` label is detected in the same poll cycle instead of stalling until this ceiling trips. Open fix-up issues at `ai:ready-to-merge` are additionally inspected for a merged linked PR via the same timeline-cross-reference helper used for the closed-issue backfill (`validation_fix_issue_has_merged_pr_evidence`). When found, `backfill_validation_fix_issue_merged_label` flips the issue to `ai:merged` in the same iteration, and the per-tracking-issue cycle's `close_merged_issues_sweep` closes the issue at the tail of the same poll. This eliminates the up-to-`STALL_THRESHOLD_MINUTES` delay between auto-merge firing on the linked PR and the orchestrator-managed sub-issue advancing — previously the consumer-side `pull_request.closed` handler (`.github/workflows/issue_pr_status.yml:253–323`) skipped orchestrator-managed children to preserve the anti-#1469 guard, leaving stall recovery as the only path to `ai:merged`. The proactive check fires only when the fix-up issue carries `ai:ready-to-merge`; every other open phase continues to short-circuit with no API round-trip. Fail-open on any timeline-lookup or label-edit transient failure (next cycle retries). |
| `MAX_IMPL_NOOP_REISSUES` | No | `2` | orchestrate_poll | Maximum automatic re-issues for an `ai:implementation-failed` issue before the poller closes it as likely already implemented and defers final verification to the wave-completion judge. Must be a positive integer; invalid values fallback to `2`. A belt-and-braces `count_noop_ancestors` walk of the `Re-issued from #N` chain (same cap) runs in parallel with the state-based counter in all three poller re-issue paths (`execute_stall_recovery_action close_and_reissue`, `run_standalone_stall_recovery close_and_reissue`, and the `no-op-implementation` branch of the `ai:implementation-failed` sweep); either signal trips closure. This catches the failure mode where the state-based counter is stale — e.g. the tracking-issue state comment was truncated or the wave iterator never refreshed `get_impl_noop_count` — which caused tracking issue #1292 to spawn 30+ duplicate sub-issues in ~5 hours. API cost: up to `2 * MAX_IMPL_NOOP_REISSUES` calls per invocation, fail-open on any API error. |
| `IMPL_NOOP_ANCESTRY_THRESHOLD` | No | `2` | implement | Ancestor-chain no-op cap enforced inside `.github/workflows/implement.yml`'s "Handle no-op implementation" step. When a commit produces zero changes, the step walks up `Re-issued from #N` markers up to this many hops and counts how many ancestors posted the `produced no repository changes` warning comment. At or above the threshold the issue is closed with `ai:closed` and the wave-completion judge is deferred to, rather than labeling `ai:implementation-failed` and letting the poller spawn another re-issue. Must be a positive integer; invalid values fall back to `2`. Complements — does not replace — the poller-side `MAX_IMPL_NOOP_REISSUES` cap. |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | No | `3` | implement | Maximum in-job post-Codex syntax-repair attempts after `Validate syntax of changed files` fails. Must be a non-negative integer (`0` disables in-job repair); invalid values fallback to `3`. |
| `CONFLICT_DISPATCH_COOLDOWN_SECS` | No | `900` | orchestrate_poll | Minimum seconds between consecutive review/autofix dispatches against the same orchestrator integration-branch final PR. Prevents the self-healing loop from re-dispatching the resolver every poll tick while a previous run is still in flight. |
| `INTEGRATION_CONFLICT_MAX_RETRIES` | No | `3` | orchestrate_poll | Circuit-breaker budget for automated integration-branch conflict resolution. The self-healing path attempts the `main -> integration_branch` sync via GitHub's merges API; on an HTTP 409 conflict, the poller dispatches `_dispatch_review_for_conflicts` for the final integration PR. After this many consecutive unresolved ticks, the orchestrator escalates to the judge with full PR context; if the judge escalation itself fails the project is marked terminally failed. Applies to **non**-`orchestrator/project-*` integration branches; sync conflicts on orchestrator-owned integration branches use the tighter `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` instead. |
| `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` | No | `1` | orchestrate_poll | Tighter circuit-breaker budget applied **only** when the integration branch head ref matches `orchestrator/project-*`. The first-line conflict resolver (`prompts/conflict-resolver.txt`) lacks built-in awareness of merged sub-issue intent, so the safest default is to escalate to the integration judge after a single resolver shot rather than burn three dispatches that may "succeed" textually while silently dropping a merged sub-issue's work. Set to a higher value to give the resolver more attempts; set to `0` to skip the first-line resolver entirely and escalate to the judge immediately. Non-orchestrator integration branches continue to honour `INTEGRATION_CONFLICT_MAX_RETRIES`. See "Integration-sync intent fingerprints" below. |
| `INTEGRATION_CONFLICT_LIFETIME_MAX` | No | `10` | orchestrate_poll | Cumulative cap on the **total** number of resolver+judge dispatches per integration branch across all retry episodes. Unlike the per-burst counters above (which reset to `0` after each judge escalation in `heal_integration_branch_conflict`), this counter is additive across the lifetime of the tracking-issue state and only zeros when that state is rebuilt. When `integration_conflict_total_dispatches >= INTEGRATION_CONFLICT_LIFETIME_MAX`, the heal function flips `status=failed` + `final_merge_status=failed` + `integration_sync_status=failed`, posts a `❌ Integration self-healing capped` tracking-issue comment, fires a CRITICAL Telegram alert, and stops dispatching. Catches the alternating resolver/judge loop where each judge invocation resets `unresolved_ticks=0` but the merge stays dirty as `main` keeps moving (observed on `orchestrator/project-1479` PR #1533, 2026-04-25, 8 fingerprint-FAILED annotations across a single 2h47m run + many such runs). Two race-recovery escape paths run before terminalization (added 2026-05-13 after `orchestrator/project-40` PR #223 false-positive cap trip): (1) re-query PR `mergeable` — if `true`, a late-finishing dispatch has landed its `[ai-merge-resolve]` commit between ticks, so the heal function clears state via `mark_integration_sync_clean` and returns instead of terminalizing; (2) call `_has_active_autofix_run` — if a resolver dispatch is still in flight against the integration branch, defer the cap decision one tick so the running dispatch can complete. Both checks fail-open on API error. Independently, each call to `heal_integration_branch_conflict` runs a pre-flight `git merge-tree` probe and short-circuits to `mark_integration_sync_clean` without dispatching or incrementing the counter when the merge into `default_branch` is verifiably clean (catches the case where GitHub's PR-`mergeable=false` signal lags behind the actual merge state by minutes/hours on large PRs and the workflow's runtime `MERGE_CONFLICT` recompute would fall through to autofix anyway). Must be a positive integer; invalid values fall back to `10`. |
| `FINGERPRINT_PER_FILE_CAP` | No | `12` | orchestrate_poll | Maximum number of `must_contain` / `must_not_contain` regex patterns the orchestrator captures per file per direction when a sub-issue PR merges into an integration branch. Higher values give the integration-sync conflict verifier finer-grained intent coverage at the cost of larger state-comment payloads and longer verification runs. |
| `FINGERPRINT_MIN_PATTERN_CHARS` | No | `12` | orchestrate_poll | Minimum trimmed-line length for a fingerprint pattern. Lines shorter than this are skipped during capture (too generic to fingerprint reliably). |
| `RESOLVER_ESCAPE_THRESHOLD_N` | No | `5` | review_autofix | Per-tier same-head, same-signature consecutive-failure step size for integration-sync resolver retry-state tracking in `scripts/review_conflict_resolve.sh`. Multiples of `N` advance `strict` → `ratio` → `count_only` → `warn_only` and emit `FINGERPRINT_TIER_DOWNGRADED_V1`; the next multiple (default `20` total failures) marks the final PR issue `ai:resolver-escalated` and stops first-line redispatch until the PR head changes. |
| `FINGERPRINT_QUARANTINE_RUNS_M` | No | `3` | review_autofix | Consecutive `PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged` observations required before `scripts/verify_integration_fingerprints.py` moves an `fp_key` into ai-memory quarantine, suppresses it on later runs, and emits the one-time `FINGERPRINT_QUARANTINED_V1` marker. |
| `DRIFT_AUDIT_ENABLED` | No | `false` | drift-audit | Opt-in gate for `.github/workflows/drift-audit.yml` / `scripts/drift_audit.sh`. When `true`, the daily `0 3 * * *` audit clusters recent `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` markers by `fp_key` and opens or updates tracker issues; when `false`, the workflow exits cleanly without scanning. Each enabled run also posts a Telegram run summary (gated by `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID`, no-op when unset) linking to the workflow run, and writes a GitHub Actions job summary. |
| `BRANCH_REBUILD_ENABLED` | No | `false` | orchestrate_poll | Enable the last-resort integration-branch rebuild flow for `orchestrator/project-*` branches after the resolver escape valve has already tripped on the current final PR head. Fails safe unless ai-memory audit storage and replay metadata are available. |
| `BRANCH_REBUILD_THRESHOLD_HOURS` | No | `24` | orchestrate_poll | Minimum hours the final PR's persisted `AUTOFIX_RESOLVER_RETRY_STATE_V1.escalated_at` must remain unchanged before the poller may delete/recreate the integration branch from default branch and replay merged sub-PR commits. |
| `BRANCH_REBUILD_COOLDOWN_HOURS` | No | `48` | orchestrate_poll | Minimum hours between successive branch rebuild attempts for the same integration branch, enforced from the latest persisted `BranchRebuildAuditV1` snapshot (`ai-memory/schemas/branch_rebuild_audit.v1.json`). |
| `REVIEW_BLOCKED_AUTO_UNSTICK` | No | `true` | orchestrate_poll | Before invoking the review-blocked judge, the poller inspects each `ai:review-blocked` PR. If the PR is `mergeable=false` it dispatches `review_autofix.yml` (via `_dispatch_review_for_conflicts`) so the in-workflow Codex resolver gets a fresh shot at the conflict, and skips the judge for this tick. If the PR head commit was authored by an **external** identity (anything other than `codex`, `codex-bot`, `github-actions`, or `github-actions[bot]`), the poller also dispatches the review workflow AND clears `ai:review-blocked`, re-entering the normal phase loop — this bridges the GitHub platform rule that suppresses `pull_request.synchronize` events on commits pushed with the default `GITHUB_TOKEN` (Claude Code on the web, custom wrapper actions) and matches the "push a new commit to re-trigger the review workflow" contract printed in the workflow-failure comment. Set to `false` to disable both paths and force the judge-first flow. Dispatch is always gated by the existing `_dispatch_review_for_conflicts` cycle-local dedup and active-run detection, so repeat calls are cheap no-ops. |
| `TG_ADMIN_CHAT_ID` | No | — | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, validate | Telegram chat ID for notifications (pair with `TG_BOT_SECRET`) |
| `ALERT_MSG_LEVEL` | No | `DEBUG` (except `update_workflows` → `SILENT`) | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate, issue_pr_status, update_workflows, test-and-mark-stable | Minimum Telegram alert level to send. Alerts below this threshold are suppressed. Valid values: `DEBUG`, `WARNING`, `ERROR`, `CRITICAL`. Each alert is prefixed with an icon and level (e.g. `🔍 DEBUG:`, `⚠️ WARNING:`, `❌ ERROR:`, `🚨 CRITICAL:`). New alerts default to `CRITICAL` until explicitly recategorised. **Exception:** `update_workflows.yml` defaults its in-workflow `ALERT_MSG_LEVEL` env to `SILENT` so the per-run `🔍 DEBUG: Workflow wrappers updated in <repo>…` notification is suppressed by default; consumer repos that want the alert back can set `vars.ALERT_MSG_LEVEL=DEBUG` (or pass `alert_msg_level=DEBUG` on `workflow_dispatch`). **Exception:** `review_autofix.yml`'s "PR processed: #N" notification is gated independently by the dedicated `PR_PROCESSED_ALERT_LEVEL` knob (default `SILENT`; set `vars.PR_PROCESSED_ALERT_LEVEL=DEBUG` to re-enable). The remaining `review_autofix.yml` Telegram sends continue to honour `ALERT_MSG_LEVEL`. |
| `PR_PROCESSED_ALERT_LEVEL` | No | `SILENT` | review_autofix | Step-scoped `ALERT_MSG_LEVEL` override for the `review_autofix.yml` "Telegram success" step that emits the `🔍 DEBUG: PR processed: #N` notification once per successful run. Because orchestrator-driven branches synchronize many times, the underlying DEBUG ping is noisy by default; the step's `ALERT_MSG_LEVEL` env defaults to `SILENT` so `tg_helpers.sh::_tg_should_send` suppresses the call. Valid values: `DEBUG`, `WARNING`, `ERROR`, `CRITICAL`, `SILENT`. Set `vars.PR_PROCESSED_ALERT_LEVEL=DEBUG` on a consumer repo to receive the ping on every successful iteration again; values above `DEBUG` keep the ping suppressed without affecting any other Telegram alert (those still honour `ALERT_MSG_LEVEL`). Mirrors the `update_workflows.yml` `SILENT` default pattern. |
| `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` | No | `3600` | all (any workflow or script that sources `scripts/gh_helpers.sh`) | Minimum seconds between consecutive admin Telegram alerts when a GitHub API rate limit is hit. The alert (`⚠️ WARNING: GitHub API rate limit hit …`) is fired from inside the rate-limit branch of `gh_retry` / `gh_retry_to_file` / `gh_api_json_to_file` / `curl_gh_api`, and is throttled globally via a Telegram pinned message in the admin chat (marker `<!-- gh_rl_ts:EPOCH -->`). This deliberately avoids any GitHub API call for dedup state so the throttle keeps working while the GitHub API itself is the resource being limited. Fail-closed: on Telegram pin failure the sent message is rolled back so the "≤ 1 alert per window" invariant holds. Set to `0` has no suppression effect (any non-numeric or empty value is coerced to `3600`). No-op when `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID` are unset. |
| `OPENROUTER_PROMPT_CACHE_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond, validate | Kill switch for OpenRouter prompt-cache instrumentation. `false` enables cache-friendly prompt ordering and cache telemetry logging; `true` disables explicit cache breakpoints and related instrumentation. (No longer consumed by `workflow-log-analysis`, which is Codex-only.) |
| `WORKFLOW_ORCHESTRATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | orchestrate, orchestrate_poll | Model override for orchestrator decomposer and judge |
| `ORCHESTRATE_POLL_INTERVAL` | No | `30` | orchestrate | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `ORCHESTRATE_SHORTCIRCUIT_MAX_CHARS` | No | `1200` | _(deprecated — no longer consumed)_ | Formerly controlled the pre-LLM short-circuit. Removed in #1163; every orchestrator run now goes through full decomposition. |
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | No | `ai-orchestrate-poll.yml` | orchestrate_poll | **Deprecated no-op.** Previously the filename of the caller wrapper workflow the poller would self-dispatch at the end of each run. The self-retrigger path (and its associated cooldown sleep and rate-limit circuit breaker) was removed; polling is now driven exclusively by the wrapper workflow's cron schedule. The variable and the matching `caller_workflow` input on `orchestrate_poll.yml` are retained for backward compatibility with existing wrappers and are ignored at runtime. |
| `ORCHESTRATE_POLL_WORKFLOW_FILE` | No | `internal-orchestrate-poll.yml` | review_autofix (resolver-bail dispatch) | Filename of the orchestrator-poller workflow the resolver script's EXIT trap (`_dispatch_integration_judge_now` in `scripts/review_conflict_resolve.sh`) targets via `gh workflow run` when an integration-sync resolver attempt fails. Default matches the workflow-source repo (`coding-workflows`). Consumer repos that ship the poller under a different filename should set this so the immediate-judge dispatch resolves correctly. Fail-open: any dispatch failure logs `::warning::` and falls through to the `*/5` cron tick (≤5 min lag), so the variable being misset on a consumer repo never blocks unattended escalation. |
| `EDITOR_IDLE_TIMEOUT` | No | `1200` | review_autofix, implement | Editor watchdog idle timeout in seconds. The editor is killed if it produces no output for this long and has no active network connections. |
| `EDITOR_MAX_WALL` | No | `7800` | review_autofix, implement | Maximum wall-clock seconds per editor attempt (~130 min). Budget-aware: auto-capped to remaining job time minus a 2-min buffer. The 3-hour job cap typically allows about one full-length attempt; retries are only possible when earlier attempts finish well under the wall cap. A watchdog kill near the 130-min limit consumes most of the remaining job budget, so undersizing per-issue scope (the orchestrator's 60-minute target) is mandatory. |
| `TARGETED_FILE_CONTEXT_MAX_BYTES` | No | `102400` (100 KB ≈ 25k tokens) | implement, review_autofix, orchestrate_poll | Total byte budget for the targeted-file-context block built by `scripts/targeted_file_context.py`. The mechanism pre-loads files named in the plan (implement) / `LAST_RUN_CHANGED_FILES_FILE` (autofix) / conflicted-file allowlist (resolver) so the model's first turn is a write rather than a recon read. Files are processed in source order until the budget is exhausted; a file that would push the cumulative byte count over the cap is NOT head-truncated, it gets a `(NN bytes; would overflow total budget — read with read tool, max_bytes=…, used=…)` marker so the model uses its native targeted-read flow rather than being misled by a truncated head. Every path the caller passes is reported, either inlined or as a marker — there is no separate file-count cap. Set to `0` to disable inlining entirely (block becomes a single-line "(disabled)" marker). Lower this on cost-sensitive repos; raise on small-file repos to fit more verbatim content. |
| `EDITOR_MIN_ATTEMPT_SECS` | No | `300` | review_autofix | Minimum remaining job budget (seconds) required to start an editor attempt. Prevents futile retries near the job deadline. |
| `EDITOR_DRAIN_GRACE_SECS` | No | `60` | review_autofix | Upper bound (seconds) on draining the editor's stderr-FIFO heartbeat reader after codex exits. A stall-killed codex can leave orphaned tool-subprocesses holding the FIFO's write-end open, so the reader never sees EOF; without this bound the drain blocks until the ~4h job ceiling. On timeout the FIFO holders are reaped (releasing the reader and stopping any lingering `danger-full-access` child). |
| `REVIEW_PR_STATE_POLL_INTERVAL_SECS` | No | `10` | review_autofix | Sleep interval in seconds for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; the GitHub PR-state API check runs every 9 polls (default ~90s). Must be an integer in `10..3600`; invalid or out-of-range values emit `rate_limit_audit_fallback` warning and fail open to `10`. |
| `MAX_POST_CODEX_REPAIR_ATTEMPTS` | No | `3` | implement | Maximum in-job post-Codex syntax-repair attempts after `Validate syntax of changed files` fails. Must be a non-negative integer (`0` disables in-job repair); invalid values fallback to `3`. The repair loop runs only for syntax-validator failures, enforces an allow-list scope guard, and then falls back to the existing diagnose/fix-up path when attempts are exhausted. |
| `BULK_DELETE_THRESHOLD` | No | `3` | implement | Maximum number of file deletions allowed in a single AI implementation commit when **any** staged deletion is a non-`.md` file. This is the strict cap that catches accidental source-tree wipes. Set higher for legitimate large refactors, or bypass via the repository variable `ALLOW_BULK_DELETE=true`. See "Destructive-commit guard" below. |
| `BULK_DELETE_THRESHOLD_MD` | No | `100` | implement | Maximum number of file deletions allowed in a single AI implementation commit when **every** staged deletion is a `.md` file. Lets docs/scratchpad cleanups (e.g. `analysis/*.md` backlog purges) commit without operator intervention while keeping the strict `BULK_DELETE_THRESHOLD` cap whenever any source file is staged for deletion. Canonical `.md` files (`agents.md`, `ai_pipeline.md`, `CLAUDE.md`, `unattended_system_instructions.md`) remain covered by the canonical-source check regardless of this threshold. |
| `ALLOW_BULK_DELETE` | No | `false` | implement | When `true`, the destructive-commit guard ignores both bulk-delete rejection paths (`BULK_DELETE_THRESHOLD` and `BULK_DELETE_THRESHOLD_MD`). Canonical workflow-source file deletions are still blocked unless `ALLOW_WORKFLOW_EDITS=true`. Use for legitimate large refactors approved by a human. |
| `ENFORCE_FILES_TOUCHED` | No | `true` | implement | Master toggle for the `files_touched` scope-enforcement guard. When `true`, the AI implementation commit is refused if any staged path falls outside the issue's declared `files_touched` allowlist. Set to `false` to globally downgrade the guard to a logged skip (no blocking). Issues that declare no `files_touched` allowlist are never blocked regardless of this value. See "files_touched scope guard" below. |
| `ALLOW_OUT_OF_SCOPE_FILES` | No | `false` | implement | Per-run escape hatch for the `files_touched` scope guard, mirroring `ALLOW_BULK_DELETE`. When `true`, out-of-scope staged paths are logged as a warning but allowed to commit instead of blocking. Use when a human has confirmed the drift is legitimate. Dependency lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `go.sum`, `composer.lock`) are auto-allowed and never need this override; compiled build outputs are not. |
| `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS` | No | `30` | workflow-log-analysis | Age (in days) above which dated `analysis/workflow-optimization-<date>.md` reports are git-removed in the same commit as a new report. Filename date stamps are authoritative; the just-written report is always preserved. Invalid values fail open to `30` with a warning. |
| `BATCH_API_DISABLED` | No | `false` | memory_maintenance | Deprecated compatibility variable. The active workflow-log-analysis batch path was removed (the workflow is now Codex-only). `memory_maintenance.yml` still reads this var and echoes it in a single `batch_noop` log line so external log scrapers that grep for `batch_*` events keep working; the value does not change any current behaviour. |
| `BATCH_API_PROVIDER` | No | `auto` | memory_maintenance | Deprecated compatibility variable. Same status as `BATCH_API_DISABLED` — only surfaced in `memory_maintenance.yml`'s `batch_noop` log line for backward-compatible telemetry. |
| `BATCH_API_POLL_TIMEOUT_HOURS` | No | `24` | memory_maintenance | Deprecated compatibility variable. Same status as `BATCH_API_DISABLED` — only surfaced in `memory_maintenance.yml`'s `batch_noop` log line for backward-compatible telemetry. |
| `ALT_EDITOR_MODEL` | No | `openai/gpt-5.4` | test-and-mark-stable (`e2e-alt-model-test` job) | Model used by the release-gate's alternate-model happy-path run. Defaults to `openai/gpt-5.4` (same as the production `WORKFLOW_EDITOR_MODEL` default) — the previous canary `openai/gpt-5.3-codex` was retired after the 2026-05-07 ablation suite identified `apply_patch_tool_type: "freeform"` on the OpenRouter Responses path as the underlying tool-call-emission failure shared by both gpt-5.4 and gpt-5.3-codex (the alt-canary stopped distinguishing them). Operators wanting cross-provider canary coverage can override to any catalog-listed non-OpenAI slug (e.g. one of `minimax/minimax-m2.5`, `google/gemini-3-flash-preview`, `qwen/qwen3-coder-plus`, all priority-10 in `scripts/codex_model_catalog.json`) via the repo var. Adding the chosen slug to `scripts/codex_model_catalog.json` is recommended so codex can register `apply_patch_tool_type` for it (the dispatcher emits a `::warning::` and proceeds when the slug is missing — codex falls back to bundled metadata, but reliability degrades). The override is propagated to the implement run by parsing it out of the smoke issue body in `implement.yml`'s "Detect smoke test" step (gated on the `[E2E Smoke Test alt-model]` title sub-tag). Has no effect outside the release gate. |
| `E2E_ALT_MODEL_ENABLED` | No | `true` | test-and-mark-stable (`e2e-alt-model-test` job) | External-dependency opt-out. Set to `false` to skip the alt-model job when the upstream OpenRouter model (selected via `ALT_EDITOR_MODEL`) is temporarily unavailable, deprecated, or your API key lacks access. The validate gate accepts `skipped` as a pass for this job, so flipping the flag unblocks releases when the orthogonal alt-model dependency is the only thing failing. Re-enable once the upstream is healthy. |
| `LOG_ANALYZER_MODEL` | No | `openai/gpt-5.4-mini` | test-and-mark-stable (Phase 8 soft-error analyser) | Lightweight model used by the release-gate post-run log analyser (`scripts/analyze_soft_errors.py`) to summarise soft failures (rate-limit recoveries, codex fallbacks, summariser hard-fails, editor no-ops) into the Telegram release notification. Non-blocking; analyser failures fall back to a stub report rather than failing the gate. The script collects logs from every phase run (clarify, plan, implement, review_autofix, orchestrate_poll, cancel_on_pr_close), filters to soft-error candidates, truncates per-run to 40K chars, and emits a markdown report whose first line carries a parseable status code (`ok` / `no_runs` / `api_skipped` / `call_failed` / `analyser_empty`). The full report is uploaded as the `soft-error-report-${run_id}` workflow artifact and a truncated copy is appended to the Telegram release message. |
| `LOG_ANALYZER_REASONING` | No | `medium` | test-and-mark-stable (Phase 8 soft-error analyser) | Reasoning effort for `LOG_ANALYZER_MODEL`. Default is `medium` per the OpenAI gpt-5.4 prompt guide (cross-run log triage is research/synthesis). Earlier revisions defaulted to `none` for cost; if cost or `gpt-5.4-mini` empty-output behaviour matters more than triage depth on a given repo, set `LOG_ANALYZER_REASONING=none`. Other accepted values: `xhigh`, `high`, `low`; values must match what the chosen model accepts. |
| `MEMORY_LEARNINGS_EXTRACT_ENABLED` | No | `true` | memory_maintenance | Enables the fail-open merged-run `repo_learnings` extraction step before compaction. When disabled, when `OPENROUTER_API_KEY` is unavailable, or when extraction/promotion fails, the workflow logs a warning and continues with compaction. |
| `WRITE_GUARDS_ENABLED` | No | `true` | implement, plan, review_autofix, validate | Enable the write-guard policy from `.github/ai/write_guards.v1.json` at the plan post-Codex workspace boundary, implement commit boundary, review-editor commit boundary, and validate fix/harness boundary. Set to `false` to bypass the guard; bypasses are logged with `WRITE_GUARD_BYPASS_ENV`. |

**Thinking levels** — control the model's reasoning effort per phase. Valid values: `xhigh`, `high`, `medium`, `low`, `none`. Most `openai/gpt-5.4` phases default to `xhigh` — see the table below. **Conflict resolver exception:** `THINKING_LEVEL_CONFLICT_RESOLVER` defaults to `high` (lowered from `xhigh` after `timeout`-killed retries on degenerate orchestrator-stack integrations — see the row's description for the originating runs). No cycle-based downgrades are applied — every phase uses the configured reasoning effort for all cycles. **E2E smoke test exception:** when an issue or PR title contains `[E2E Smoke Test]`, the clarify, plan, and reviewer phases force `low` reasoning; the editor phase keeps `medium`; the implement phase keeps its production default (now `xhigh`) unmodified. The review-blocked judge is not overridden and retains its configured reasoning level. See `agents.md` for the authoritative per-phase reasoning/verbosity table.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `THINKING_LEVEL_CLARIFY` | `xhigh` | clarify | Reasoning effort for the clarification phase |
| `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` | `xhigh` | clarify | Reasoning effort used only when clarify runs Codex for `ai:orchestrator-managed` issues on forced human `/reclarify` |
| `THINKING_LEVEL_PLAN` | `xhigh` | plan | Reasoning effort for the planning phase |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | implement | Reasoning effort for the implementation phase |
| `THINKING_LEVEL_IMPLEMENT_REPAIR` | `xhigh` | implement | Reasoning effort for the post-Codex syntax-repair sub-phase (`MODEL_REPAIR_REASONING_EFFORT`). |
| `THINKING_LEVEL_DIAGNOSE` | `xhigh` | implement | Reasoning effort for the post-Codex diagnose sub-phase (`MODEL_DIAGNOSE_REASONING_EFFORT`). |
| `THINKING_LEVEL_ANALYSIS` | `xhigh` | workflow-log-analysis | Reasoning effort for the API-redundancy Codex pass (passed via Codex `model_reasoning_effort`). The deep-audit pass no longer reads this var — its reasoning is hardcoded at `xhigh` in the workflow YAML; edit the hardcoded value in `workflow-log-analysis.yml` if you need to override it. |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | review_autofix | Reasoning effort for the reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `xhigh` | review_autofix | Reasoning effort for the editor model (applying fixes) |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `xhigh` | review_autofix | Reasoning effort for the review-blocked judge (non-orchestrator PRs) |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | orchestrate | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | orchestrate_poll | Reasoning effort for judge evaluation |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `xhigh` | orchestrate_clarify_respond | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `xhigh` | validate | Reasoning effort for runtime validation harness generation and diagnosis |
| `MODEL_REASONING_EFFORT_DISCOVER` | `xhigh` | validate | Per-phase override applied only to the validate-discover step (`.ai/validate.yml` hint generation). Patched into `~/.codex/config.toml` before the discover codex call and restored to `THINKING_LEVEL_VALIDATE` on both the normal exit path and the EXIT trap so abnormal exits cannot leak the override. Accepted values: `xhigh`, `high`, `medium`, `low` (`none` is rejected to match the catalog's advertised levels for the gpt-5.x family). |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `high` | orchestrate_poll, review_autofix | Reasoning effort for the Codex-based merge conflict resolver (orchestrator integration-sync runs and review_autofix's post-editor resolver step). Default lowered from `xhigh` after runs `25627236793` / `25627316961` hit a hung-thinking failure mode at `xhigh` on degenerate orchestrator-stack integrations (Codex consumed the full per-attempt budget enumerating duplicate helper definitions outside the conflict markers and was SIGTERMed before invoking apply_patch on all three retry attempts). `high` trades some merge depth for finishing inside the per-attempt budget. Override per-repo to `xhigh` if your conflicts routinely benefit from deeper reasoning; valid levels are `xhigh`, `high`, `medium`, `none`. |
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

#### Optional `.github/ai` operator surfaces

Several Symphony-era behaviors are configured by optional committed files rather than additional repo vars:

- `.github/ai/WORKFLOW.md` is opt-in and is loaded by `scripts/load_workflow_overlay.py`. On current HEAD it is validated by `ai-memory/schemas/workflow_overlay.v1.json`, and the shipped schema currently supports only `schema_version` plus `prompt_overrides[]` append/replace entries. This repository ships a no-op overlay (front matter `schema_version` only, no `prompt_overrides`): its presence sets `WORKFLOW_OVERLAY_ENABLED=true` so the loader path is exercised, but no rendered prompt is altered. Add `prompt_overrides[]` entries to it to tune per-mode prompts.
- `.github/ai/concurrency_caps.yml` is opt-in and is parsed by `scripts/orchestrate_lib.py`. Missing or empty files disable per-state caps and restore the legacy uncapped dispatch behavior.
- `.github/ai/workspace_hooks/<phase>/<hook>.sh` is opt-in and is executed by `scripts/run_workspace_hook.sh`. The shipped hook names are `after_create`, `before_run`, `after_run`, and `before_remove`; missing hook files are a no-op.
- `state-snapshot` is the per-tick orchestrator artifact written by `scripts/build_state_snapshot.py` and uploaded from `orchestrate_poll.yml` when `STATE_SNAPSHOT_ARTIFACT_ENABLED!=false`. Branch publication of the rolling history to `STATE_SNAPSHOT_BRANCH_NAME` (default `state-snapshot`), capped by `STATE_SNAPSHOT_HISTORY_DEPTH`, is enabled by default (`STATE_SNAPSHOT_BRANCH_ENABLED=true`); set `STATE_SNAPSHOT_BRANCH_ENABLED=false` to disable it without affecting the artifact upload.

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
> `MAX_AUTOFIX_ITERATIONS` (default `5`). When `ENABLE_REVIEW_BLOCKED_JUDGE`
> is `true` (the default), a judge LLM evaluates the PR and decides to:
> merge as-is, push a `[judge-fix]` commit (re-triggers review with reset
> counter), merge_with_followup (merge as-is and open a follow-up issue
> tracking the deferred gap — preferred over close+reissue at IS_FINAL
> when the PR is shippable), or close the PR and create a replacement
> issue. The judge respects `MAX_REVIEW_BLOCKED_RETRIES` (default `2`) by
> counting `[judge-fix]` commits in the branch history. Orchestrator-
> managed PRs are skipped (handled by the orchestrate_poll workflow
> instead). If the judge is disabled or fails, the PR is labeled
> `ai:review-blocked` and requires human intervention. When review passes
> with no fixes needed, it labels linked issues `ai:ready-to-merge` and
> enables auto-merge if configured.

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
> does not decide how the just-pushed `[ai-autofix]` commit hands off to the
> next review/autofix iteration. When `AUTOFIX_SKIP_SELF_TRIGGERED=true` is enabled,
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
> `workflow_dispatch` retrigger step still mirrors the guard for ledger-only
> commits and for the legacy `AUTOFIX_CONTINUATION_ENABLED=false` path, but the
> current default `AUTOFIX_CONTINUATION_ENABLED=true` immediately dispatches the
> next autofix iteration after a **productive** `[ai-autofix]` commit
> (`DID_COMMIT=true`, `LEDGER_ONLY_COMMIT!=true`, `CONFLICT_RESOLVED!=true`).
> That continuation run intentionally bypasses the post-commit peer-dedup,
> because the only same-branch peer is the gate-skipped synchronize run and it
> cannot act as the successor iteration. `AUTOFIX_DISPATCH_SKIPPED
> reason=self_triggered_autofix ... continuation_enabled=<val>` therefore now
> means either a ledger-only commit or the legacy opt-in path, not the normal
> productive-autofix case. `[ai-merge-resolve]` commits still fire a
> follow-up verification pass for post-conflict-resolution safety.
> `workflow_dispatch`, `opened`, `reopened`, and `ready_for_review`
> events are never skipped. The orchestrator stall cron
> (`internal-orchestrate-poll.yml`, `*/5 * * * *`) remains the safety net, but
> non-orchestrator PRs no longer wait for that ~5 min path after a productive
> autofix commit. Log
> prefixes `AUTOFIX_GATE_SKIP`, `AUTOFIX_GATE_NO_SKIP_IDENTITY`,
> `AUTOFIX_GATE_SKIP_QUERY_FAILED`, and
> `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix` are stable audit
> handles. Set `vars.AUTOFIX_SKIP_SELF_TRIGGERED=true` to opt in, or set
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
> `synchronize` event whose gate job is skipped when
> `AUTOFIX_SKIP_SELF_TRIGGERED=true`, so auto-merge cannot be deferred to the
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

### Consolidator, ledger, and floor rules

Each `review_autofix.yml` iteration builds a small local review-artifact chain before the editor runs:

- `reviewer_bundle.txt` is the authoritative reviewer input.
- `floor_tags.txt` is produced by `scripts/review_floor_rules.sh`. With `REVIEW_FLOOR_RULES_ENABLED=1` (default), floor matches are treated as non-skippable signals. Invalid or missing `REVIEW_FLOOR_KEYWORDS_FILE` overrides fail open to the built-in keyword catalog.
- `consolidator_raw.txt` and `review_issues.txt` are advisory only. The consolidator is enabled by default, but empty output, parser failures, or uncovered anchors never gate the run — the editor still works from `reviewer_bundle.txt`.
- `ledger_status.txt` plus `REVIEW_LEDGER_PATH` (default `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt`) persist per-PR issue history across autofix iterations via `actions/cache`. Statuses move through `NEW`, `PERSISTING`, `FIXED`, `RESURGENT`, and `accepted-residual`; after `REVIEW_LEDGER_PERSIST_LIMIT=2`, still-open issues are collapsed to `accepted-residual` stubs in `review_issues.txt` while the ledger keeps the durable history.
- `REVIEW_REVIEWER_CHECKLIST_ENABLED=1` appends the checklist prompt when the support prompt is present.
- `REVIEW_REVIEWER_ITERATION_SCOPING=1` lets later reviewer passes focus on last-run changed files plus actionable ledger rows; the first pass remains full-diff. The current workflow summary on this branch still reports `Reviewer scope = full-diff`, so use the runtime artifacts when debugging exact scope.

The workflow summary now reports bundle size, floor-tag count, consolidator model/invocation/output size, parser counts, ledger state counts (including `accepted-residual`), editor invocation/commit status, and `CONSOLIDATOR_OVERRIDDEN` count. When the editor intentionally diverges from advisory consolidator guidance, it records `CONSOLIDATOR_OVERRIDDEN: <issue_id> — <reason>` in its summary; the editor remains the final authority on the diff.

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

**Optional label-sync wrapper:** If you want your repository's `ai:*` labels to
stay aligned with the upstream contract, add
[`workflow-templates/ai-sync-labels.yml`](workflow-templates/ai-sync-labels.yml)
as `.github/workflows/ai-sync-labels.yml`. It supports manual
`workflow_dispatch` runs plus the stable-release `repository_dispatch` hook.

**Central consumer drift audit:** This source repo also runs
[`.github/workflows/audit_consumer_drift.yml`](.github/workflows/audit_consumer_drift.yml)
on a weekly cron plus manual `workflow_dispatch`. The audit reads the live
[`.github/ai/consumer_repos.json`](.github/ai/consumer_repos.json) registry,
fetches each consumer's installed `.github/workflows/ai-*.yml` wrappers via
the GitHub contents API, diffs them against this repo's checked-in
[`workflow-templates/ai-*.yml`](workflow-templates/), and reports drift
read-only — it never writes to consumer repositories.

#### Install profiles

If you use `ai-update-workflows.yml`, you can narrow which wrappers it
auto-creates by setting the `WORKFLOW_PROFILE` repository variable. Supported
values are `core`, `standard`, and `full`; the default is `full`, which
preserves today's behavior of installing every wrapper template.

- `core` installs the six-wrapper manifest in
  [`workflow-templates/profiles/core.txt`](workflow-templates/profiles/core.txt):
  `ai-clarify.yml`, `ai-plan.yml`, `ai-implement.yml`, `ai-review.yml`,
  `ai-issue-pr-status.yml`, and `ai-cancel-on-pr-close.yml`.
- `standard` installs `core` plus the orchestrator/validation additions listed
  in
  [`workflow-templates/profiles/standard.txt`](workflow-templates/profiles/standard.txt):
  `ai-orchestrate.yml`, `ai-orchestrate-poll.yml`,
  `ai-orchestrate-clarify-respond.yml`, `ai-validate.yml`, and
  `review_rb_judge_dispatch.yml`.
  The standard manifest also includes the optional `ai-sync-labels.yml`
  wrapper so stable-channel syncs can auto-install the label-sync entrypoint.
- `full` installs every top-level wrapper listed in
  [`workflow-templates/profiles/full.txt`](workflow-templates/profiles/full.txt).

Profile downgrades are non-destructive: switching from `full` to `core` or
`standard` stops creating out-of-profile wrappers in future syncs, but does
not delete wrappers that are already present in `.github/workflows/`.

> **Terminology note:** the minimum manual-bootstrap wrappers are
> `ai-clarify.yml`, `ai-plan.yml`, and `ai-implement.yml`. The `core` install
> profile is a separate six-wrapper auto-install manifest used only by
> `ai-update-workflows.yml`.

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
| `workflow-log-analysis.yml` | `workflow_dispatch` (typically called from comprehensive-test-and-release / test-and-mark-stable smoke gates) | Periodic Codex audit of workflow runs (analyze, deep-audit, api-redundancy passes); see [`probably_unnecessary_but_read_if_stuck.md`](probably_unnecessary_but_read_if_stuck.md) for the runbook |
| `check_failure_triage.yml` | `check_run.completed` (failure) | LLM diagnoses a failing PR check and opens an `ai:check-triage` issue for the pipeline to fix. Opt-in via `CHECK_FAILURE_TRIAGE_ENABLED`; see "Check Failure Triage Phase" below |

<!-- §Workflow Log Analysis And Improvement and §Workflow Log Analysis moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need workflow-log-analysis pipeline runbook details (collector/analyzer contracts, phase behavior, env vars). -->

### Check Failure Triage Phase

When a check fails on a pull request, the **check-failure triage** workflow
(`check_failure_triage.yml`, driven in consumers by the
`ai-check-failure-triage.yml` wrapper and self-hosted via
`internal-check-failure-triage.yml`) analyses the failure and opens a GitHub
issue so the rest of the pipeline can fix it through the normal, gated path. It
**never pushes code itself** — the fix flows through `clarify → plan →
implement → review`, which is the "safest possible" route. With
`AUTO_IMPLEMENT_ON_CLEAR_PLAN=true` (the default) the opened issue can flow all
the way to a fix PR without human action.

- **Trigger:** `check_run: completed` with a `failure`, `timed_out`, or
  `action_required` conclusion, on a check associated with an open PR. The
  workflow file lives on the default branch (required for `check_run` events).
- **Opt-in:** disabled unless the repo variable `CHECK_FAILURE_TRIAGE_ENABLED`
  is `true`. While disabled the wrapper job is skipped immediately (no checkout
  / no model call).
- **Diagnosis:** the repo is checked out at the failing head SHA; the diagnosis
  model (`WORKFLOW_CHECK_TRIAGE_MODEL`, default `openai/gpt-5.4`, `xhigh`) reads
  the failing check's logs (via `collect_pr_check_runs_context.py`) and the
  branch code, then writes the issue body (summary, evidence, root cause,
  suggested fix, affected files) per `prompts/mode-check-failure-triage.txt`.
- **De-duplication:** a per-`repo+PR+check` concurrency group keeps one triage
  in flight; an HTML-comment fingerprint marker
  (`<!-- check-failure-triage:fp=… -->`) means no second issue is opened for a
  check that already has an open triage issue.
- **Loop bound:** each triage issue records a generation counter
  (`<!-- check-failure-triage:gen=N -->`) and a stable lineage root. A fix PR's
  failure links back to its source issue (branch `ai/issue-<N>`) and increments
  the generation; once it exceeds `CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH`
  (default 3) the chain stops opening issues and instead labels the PR
  `ai:check-triage-escalated` and sends a Telegram CRITICAL for human
  attention. The triage workflow also skips its own check-run by name to
  prevent self-triggering.
- **Failure modes:** the workflow fails open. Missing logs → the issue is filed
  with raw context; an empty model response → a fallback body is filed; a
  failed `gh issue create` or a triage-workflow crash → a Telegram CRITICAL is
  sent and the run fails (no partial state is left). Stable log lines are
  prefixed `CHECK_TRIAGE`.

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
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.4` (every phase) | Model for code editing / reasoning tasks. The previous legacy editor split (patch-heavy phases on a separate older slug) was retired after openai/codex#11151. See main table above. |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `true` | Allow AI edits to workflow files and automatic wrapper updates |
| `ENABLE_AUTO_MERGE` | `true` | Auto-merge PRs (squash) when review passes and checks are green |
| `MAX_AUTOFIX_ITERATIONS` | `5` | Maximum consecutive autofix rounds before marking `ai:review-blocked` |
| `ENABLE_REVIEW_BLOCKED_JUDGE` | `true` | Enable review-blocked judge for non-orchestrator PRs |
| `THINKING_LEVEL_REVIEW_BLOCKED_JUDGE` | `xhigh` | Reasoning effort for review-blocked judge |
| `MAX_REVIEW_BLOCKED_RETRIES` | `2` | Maximum judge `fix` retries for review-blocked PRs before IS_FINAL (terminal `merge` / `merge_with_followup` / `close_and_reissue`). Used by both review_autofix and orchestrate_poll |
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
| `REVIEW_RUN_MAX_RUNTIME_MINUTES` | `250` | Freshness window the in-flight/zombie guards apply to review-family runs (AI Review, Internal Review, Review Autofix); they can run past the stall threshold up to the 240-min job timeout; floored at `STALL_THRESHOLD_MINUTES` |
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
| `RESOLVER_ESCAPE_THRESHOLD_N` | `5` | Per-tier same-head, same-signature failure step size for resolver retry state; multiples of `N` advance `strict` → `ratio` → `count_only` → `warn_only`, and the next multiple escalates the final PR issue with `ai:resolver-escalated` |
| `FINGERPRINT_QUARANTINE_RUNS_M` | `3` | Consecutive unchanged-drift observations before a fingerprint moves into ai-memory quarantine and emits `FINGERPRINT_QUARANTINED_V1` |
| `DRIFT_AUDIT_ENABLED` | `false` | Opt-in gate for the daily `drift-audit.yml` workflow; when enabled it clusters `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` markers and opens/updates tracker issues |
| `BRANCH_REBUILD_ENABLED` | `false` | Enable last-resort `orchestrator/project-*` integration-branch rebuilds after resolver escalation has already persisted on the final PR head |
| `BRANCH_REBUILD_THRESHOLD_HOURS` | `24` | Hours an escalated final PR head must stay unchanged before branch rebuild is allowed |
| `BRANCH_REBUILD_COOLDOWN_HOURS` | `48` | Minimum hours between branch rebuild attempts, enforced from the persisted `BranchRebuildAuditV1` audit record |
| `ACTIONS_RUNS_CACHE_TTL_SECONDS` | `60` | Cross-tick cache TTL (seconds) for `GET /actions/runs` snapshots persisted on the `ai-memory` branch and reused by orchestrator poll run-state readers |
| `AI_MEMORY_BRANCH` | `ai-memory` | Branch used for persistent AI memory |
| `AI_MEMORY_ROOT` | `ai-memory` | Memory root path used by workflows |
| `AI_MEMORY_RETRIEVAL_PROFILES` | `ai-memory/config/retrieval_profiles.v1.json` | Retrieval role config |
| `AI_MEMORY_ENABLED` | `true` | Enable/disable memory operations |
| `AI_MEMORY_KEYWORD_MODEL` | `openai/gpt-5.4-nano` | Model for semantic keyword extraction during retrieval |
| `AI_MEMORY_KEYWORD_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL for keyword model |
| `AI_MEMORY_TOKEN_BUDGET_<ROLE>` | _(from profile)_ | Per-role token budget override (e.g. `AI_MEMORY_TOKEN_BUDGET_IMPLEMENTATION=3200`) |
| `THINKING_LEVEL_CLARIFY` | `xhigh` | Reasoning effort for clarification (`xhigh`, `high`, `medium`, `none`) |
| `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` | `xhigh` | Clarify-only override for forced human `/reclarify` on `ai:orchestrator-managed` issues (normal clarify path auto-posts `/answer [auto-answered-by-orchestrator]` without Codex) |
| `THINKING_LEVEL_PLAN` | `xhigh` | Reasoning effort for planning |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | Reasoning effort for implementation |
| `THINKING_LEVEL_ANALYSIS` | `xhigh` | Reasoning effort for the workflow-log-analysis API-redundancy pass (deep-audit is hardcoded at `xhigh` in the workflow YAML; edit there to override) |
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
| `ORCHESTRATE_POLL_CALLER_WORKFLOW` | `ai-orchestrate-poll.yml` | _(deprecated no-op — self-retrigger removed; value is ignored, retained for backward compatibility)_ |
| `EDITOR_IDLE_TIMEOUT` | `1200` | Editor watchdog idle timeout (seconds); killed if no output and no active network connections |
| `EDITOR_MAX_WALL` | `7800` | Max wall-clock seconds (~130 min) per editor attempt; auto-capped to remaining job budget |
| `EDITOR_MIN_ATTEMPT_SECS` | `300` | Minimum job budget (seconds) required to start an editor attempt |
| `EDITOR_DRAIN_GRACE_SECS` | `60` | Upper bound (seconds) on draining the editor stderr-FIFO heartbeat reader after codex exits; on timeout, processes still holding the FIFO are reaped so the drain can't hang until the ~4h job ceiling |
| `REVIEW_PR_STATE_POLL_INTERVAL_SECS` | `10` | Sleep interval (seconds) for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; GitHub PR-state API checks run every 9 polls (default ~90s); must be integer `10..3600`, else warn (`rate_limit_audit_fallback`) and fall back to `10` |
| `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS` | `30` | Age (days) above which sibling `analysis/workflow-optimization-<date>.md` reports are git-removed in the same commit as a new report. Filename date is authoritative; the new report is always preserved. |
| `BATCH_API_DISABLED` | `false` | _(deprecated compat)_ Active batch path removed; only echoed in `memory_maintenance.yml`'s `batch_noop` telemetry line for log-scraper backward compatibility |
| `BATCH_API_PROVIDER` | `auto` | _(deprecated compat)_ Same status as `BATCH_API_DISABLED` — surfaced only in `memory_maintenance.yml` `batch_noop` log line |
| `BATCH_API_POLL_TIMEOUT_HOURS` | `24` | _(deprecated compat)_ Same status as `BATCH_API_DISABLED` — surfaced only in `memory_maintenance.yml` `batch_noop` log line |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | Tool call budget for decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | Tool call budget for judge (needs deep repo inspection) |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | Token warning threshold for orchestration |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `xhigh` | Reasoning effort for auto-answering clarification questions |
| `THINKING_LEVEL_VALIDATE` | `xhigh` | Reasoning effort for runtime validation harness generation and diagnosis |
| `THINKING_LEVEL_CONFLICT_RESOLVER` | `high` | Reasoning effort for the Codex-based merge conflict resolver (used by orchestrate_poll integration-sync and review_autofix's post-editor resolver step). Default lowered from `xhigh` after `timeout`-killed retries on degenerate orchestrator-stack integrations; see the row in §"Thinking levels" above. |
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
| `ORCHESTRATE_DECOMPOSER_PER_ATTEMPT_TIMEOUT_SECS` | `3000` | Per-attempt wall-time timeout (seconds) for a single decomposer `codex exec` invocation in the `Run Codex (decomposer)` step of `.github/workflows/orchestrate.yml`. The codex call is wrapped in `timeout --signal=TERM --kill-after=30s -- ${ORCHESTRATE_DECOMPOSER_PER_ATTEMPT_TIMEOUT_SECS}` so a runaway first attempt cannot exhaust the full 240-min job budget (raised from 60 min after run 25742821278 was SIGKILLed at the 60-min cap while still inside attempt 1/3, making the 3-attempt retry loop structurally meaningless). 3 × 50-min attempts = 150 min fits inside the 240-min job cap with ~90 min headroom for prompt assembly, JSON validation, tracking-issue / branch / Wave-1 setup, and post steps. Default + validation + clamp run **once before the retry loop**, normalising the value to `3000` when unset / non-numeric / above the upper bound, so the value enforced by the `timeout` wrapper is constant across iterations. Override (in seconds) only for dispatches that need shorter attempts (e.g. forcing earlier retries on a known-small decomposition); values above `3000s` would consume the post-codex headroom and are clamped down with a `::warning::`. Exit `124` is always classified as `codex timeout 124`; exit `137` is classified as timeout only when the elapsed wall clock reached the configured budget (the `timeout --kill-after=30s` backstop), otherwise it is surfaced as a non-timeout `codex exit 137` so likely OOM / external SIGKILL is not misreported. When the too-few-issues retry path appends corrective feedback, it rebuilds the prompt from the snapshotted base first so retry feedback does not accumulate across re-prompts. |
| `ENABLE_PHASE_FAILURE_COMMENTS` | `true` | Contract-defined gate for `AI_PHASE_FAILURE_V1` issue comments. Current branch status: reserved (not consumed yet); validate/workflow-log-analysis still emit marker comments when tracking issue context exists. |
| `ENABLE_LABEL_REPAIR_SWEEP` | `true` | Contract-defined gate for poller label-repair sweep. Current branch status: reserved (not consumed yet); `reconcile_managed_issue_labels` runs every poll cycle for current-wave managed issues. |
| `LABEL_REPAIR_DRY_RUN` | `false` | Contract-defined dry-run mode for label repair. Current branch status: reserved (not consumed yet); label diffs are applied live when detected. |
| `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE` | `50` | Contract-defined cap for per-cycle label-repair mutations. Current branch status: reserved (not consumed yet); effective scope is the current-wave issue set. |
| `SEMBLE_ENABLED` | `false` | Opt-in gate for Semble-backed context retrieval. Reusable workflows read this repo variable from the caller repo; the Semble install/index steps live in `.github/workflows/*.yml`, not in `workflow-templates/*.yml` wrapper copies. |
| `REVIEW_FLOOR_RULES_ENABLED` | `1` | Enable floor-rule tagging; emits non-skippable `floor_tags.txt` findings before the editor runs. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | `(empty)` | Optional keyword-catalog override for `scripts/review_floor_rules.sh`; empty / missing / unreadable falls back to the built-in catalog. |
| `REVIEW_CONSOLIDATOR_ENABLED` | `1` | Enable the advisory consolidator stage. The raw reviewer bundle remains authoritative even when this is on. |
| `REVIEW_CONSOLIDATOR_MODEL` | `openai/gpt-5.4` | Model slug used for the consolidator stage in `review_autofix.yml`. |
| `REVIEW_CONSOLIDATOR_REASONING` | `xhigh` | Reasoning effort for the consolidator model call. |
| `REVIEW_CONSOLIDATOR_TIMEOUT_SECS` | `300` | Timeout (seconds) for the consolidator model call. |
| `REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT` | `16000` | Max output tokens requested from the consolidator model. |
| `REVIEW_PARSER_FAILOPEN` | `1` | Parser kill switch: malformed / missing consolidator structure degrades to advisory fail-open artifacts instead of stopping the editor. |
| `REVIEW_REVIEWER_CHECKLIST_ENABLED` | `1` | Append the reviewer checklist block to prompts when the checklist template is available. |
| `REVIEW_REVIEWER_ITERATION_SCOPING` | `1` | Allow later reviewer passes to scope from last-run changed files plus actionable ledger rows; first pass remains full-diff. |
| `ORCH_INTEGRATION_STALE_ALERT_HOURS` | `0` | Hours an integration branch may remain ahead of default after the last squash before the poller emits `INTEGRATION_STALE_ALERT_SENT` and a `WARNING` alert. **Set to `0` to disable** — and the reusable workflow `.github/workflows/orchestrate_poll.yml` now passes `0` by default, because `main` only catches up at the single end-of-project squash, so the alert otherwise fires for the entire lifetime of every multi-wave project (healthy progress, not a stall). `6` remains the script fallback when the env is unset/non-numeric; set repo variable `ORCH_INTEGRATION_STALE_ALERT_HOURS` to a positive integer to re-enable. |
| `ORCH_INTEGRATION_STALE_REALERT_HOURS` | `12` | Minimum hours between repeated stale-integration alerts; the window clears once the branch catches up / squash lands |
| `ORCH_INTEGRATION_MAX_AHEAD_COMMITS` | `10` | Backpressure **floor**; the effective threshold is `max(this, planned_issue_count + ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN)` so a project's own merges never self-deadlock backpressure. Activates `ai:integration-backpressure`, pauses additional sub-issue merges, and auto-clears once the backlog drops below the effective threshold |
| `ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN` | `20` | Headroom added to a project's planned sub-issue count when deriving the size-aware backpressure threshold (sync-merges + judge merges + fix-up issues). Raised from `5` to `20` after the #2974 integration over-drift deadlock (22 commits ahead vs the old `max(10, 10+5)=15` threshold). Non-negative integer; script-level fallback `5` |
| `REVIEW_LEDGER_REREVIEW_ENABLED` | `false` | Enable consolidator-side suppression of repeated `accepted-residual` / `won't-fix` findings from the existing review ledger and the review-blocked judge's ledger-fed prior-round decision input. |
| `REVIEW_APPROVAL_RUBRIC_ENABLED` | `false` | Enable logical `review_state` output from the review-blocked judge and outbound PR-review posting via `post_review_comment.sh --review-state`. |
| `REVIEW_BREAK_GLASS_ENABLED` | `false` | Enable the anchored `@codex break-glass` override scan; it downgrades only the outbound `REQUEST_CHANGES` review event to a comment-only review. |
| `REVIEWER_RISK_TIER_ENABLED` | `0` | Enable deterministic `trivial | lite | full` reviewer fan-out by reviewer-visible diff LOC/file count. |
| `REVIEWER_RISK_TIER_TRIVIAL_LOC` | `10` | Trivial-tier LOC threshold. |
| `REVIEWER_RISK_TIER_TRIVIAL_FILES` | `20` | Trivial-tier changed-file threshold. |
| `REVIEWER_RISK_TIER_LITE_LOC` | `100` | Lite-tier LOC threshold. |
| `REVIEWER_RISK_TIER_LITE_FILES` | `20` | Lite-tier changed-file threshold. |
| `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` | sensitive-path regex | Force full reviewer fan-out on matching paths (default covers `scripts/`, `.github/workflows/`, `.github/ai/`, `prompts/`, `workflow-templates/`, `db/contracts/`, and `ai-memory/`). |
| `REVIEWER_TIER_TRIVIAL_MODELS` | _(empty)_ | Optional comma-separated trivial-tier reviewer subset; empty falls back to the first live reviewer model from `REVIEWER_MODELS`. |
| `REVIEWER_TIER_LITE_MODELS` | _(empty)_ | Optional comma-separated lite-tier reviewer subset; empty falls back to the first two live reviewer models from `REVIEWER_MODELS`. |
| `REVIEWER_FILTER_UNINTERESTING_ENABLED` | `false` | Enable pre-review stripping of low-signal lock/generated/minified files before reviewer fan-out. |
| `REVIEWER_FILTER_EXTRA_GLOBS` | _(empty)_ | Optional comma-separated extra skip globs for `review_filter_uninteresting_files.sh`. |
| `REVIEWER_FILTER_EXEMPT_GLOBS` | `db/contracts/**,**/migrations/**,**/migrate/**` | Comma-separated exemption globs that stay reviewer-visible even when they match a skip rule. |
| `REVIEWER_CIRCUIT_BREAKER_ENABLED` | `0` | Enable per-reviewer health-state caching and same-family failback attempts. |
| `REVIEWER_FAILBACK_MAX_RETRIES` | `1` | Retryable-failure budget before a reviewer slot consults `reviewer_failback_chains.json`. |
| `REVIEWER_HEALTH_OPEN_THRESHOLD` | `3` | Consecutive retryable failures required to mark a reviewer slot `open` in the health cache. |
| `REVIEWER_HEALTH_OPEN_TTL_SECS` | `1800` | Seconds an `open` reviewer-health entry suppresses dispatch before automatic expiry. |
| `AGENTS_MD_MATERIALITY_ENABLED` | `1` | Post the deterministic, non-blocking `AGENTS.md` materiality advisory when a material change omits an `agents.md` update (on by default; set `0` to disable). |
| `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` | `0` | Reserved only; deterministic v1 still makes no materiality model call when this flag is on. |
| `AGENTS_MD_MATERIALITY_MODEL` | `openai/gpt-5.4-mini` | Reserved future materiality fallback model slug. |
| `AGENTS_MD_MATERIALITY_REASONING` | `medium` | Reserved future materiality fallback reasoning effort. |
| `CONTEXT_BUDGET_WARN_RATIO` | `0.7` | Per-model context-window ratio above which review surfaces emit `CONTEXT_BUDGET_WARN`. |
| `MAX_PROMPT_TOKENS_FOR_PHASE` | _(empty)_ | Absolute prompt-token override that takes precedence over `CONTEXT_BUDGET_WARN_RATIO`; phase-specific `MAX_PROMPT_TOKENS_FOR_<PHASE>` overrides remain supported. |
| `CODEX_HEARTBEAT_ENABLED` | `1` | Enable the `codex_heartbeat.sh` wrapper on long-running review / validate Codex calls. |
| `CODEX_HEARTBEAT_INTERVAL_SECS` | `30` | Silence interval (seconds) between emitted `CODEX_HEARTBEAT` lines. |
| `MEMORY_LEARNINGS_EXTRACT_ENABLED` | `true` | Enable the fail-open merged-run `repo_learnings` extraction step before memory compaction |
| `CHECK_FAILURE_TRIAGE_ENABLED` | `false` | Opt-in switch for the check-failure triage workflow. When `true`, a failing PR check is analysed by the diagnosis model, which opens an `ai:check-triage` issue for the pipeline to fix. Off by default. |
| `CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH` | `3` | Max auto-fix generations in a single failure lineage before the chain is escalated (`ai:check-triage-escalated` + Telegram) instead of opening another issue. |
| `WORKFLOW_CHECK_TRIAGE_MODEL` | `WORKFLOW_EDITOR_MODEL` (`openai/gpt-5.4`) | Diagnosis model for check-failure triage. |
| `THINKING_LEVEL_CHECK_TRIAGE` | `xhigh` | Reasoning effort for the check-failure triage diagnosis call. |
| `VERBOSITY_CHECK_TRIAGE` | `low` | Codex verbosity for the check-failure triage diagnosis call. |

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

### Opt-in budget helper

- `OPENROUTER_PROMPT_BUDGET_TOKENS=160000` (default): fallback budget for `scripts/openrouter_prompt_cache.py::compact_if_over_budget(sections, budget_tokens)` when a future caller passes `budget_tokens=None`. The helper follows the repo's shared `~4 chars/token` budgeting rule and remains dormant until a caller explicitly opts in.

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

- **Observed support (route-dependent):** `openai/gpt-5.4` via OpenRouter Responses API can benefit from provider-managed prefix caching, but availability/reporting can vary by routed provider/model.
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
12d. **Review/autofix learnings sweep (flagged):** `review_autofix.yml` now supports deterministic reviewer risk tiers, low-signal diff filtering, consolidator re-review suppression, per-reviewer health/failback state, non-blocking `AGENTS.md` materiality advisories, `CODEX_HEARTBEAT` / `CONTEXT_BUDGET_WARN` telemetry, and review-blocked judge review-state posting with optional human `@codex break-glass` downgrade from outbound `REQUEST_CHANGES` to a comment-only review event.
1. **Decomposition:** The LLM reads your repo and breaks the project into scoped issues with a dependency graph. A tracking issue (labeled `ai:orchestrator-tracking`) and integration branch are always created, even for single-issue decompositions — this ensures every orchestrator-managed task goes through the full pipeline including post-merge validation and fixups.
2. **Wave dispatch:** Wave 1 issues (no dependencies) are created immediately and enter the existing clarify → plan → implement → review pipeline automatically. If clarification questions are raised, the `orchestrate_clarify_respond` workflow answers them automatically using an LLM. A **data-provision guard** (`scripts/clarify_data_provision_guard.py`) post-processes the LLM's answers before posting: if the selected option requires the respondent to provide concrete external data (PR URLs, commit SHAs, branch names) that the auto-responder cannot supply, the guard overrides the answer with the most conservative fallback option from the same question. This prevents circular clarification loops where the auto-responder repeatedly selects a "provide the URL" option without providing one. When `plan.yml` emits structured `Q<ID>` clarification blocks with single-letter `(RECOMMENDED)` options for every question, `plan.yml` now posts a synthesized `/answer Q1: A, ... [auto-answered-by-orchestrator]`; if parsing fails or any recommendation is non-single-letter (for example `A+C`), it does not auto-answer and keeps the human `/answer` loop.
3. **Auto-merge:** The poller automatically merges PRs via squash merge when they reach `ai:ready-to-merge`. If a PR has merge conflicts (e.g. `main` advanced since the PR was created), the poller automatically updates the PR branch via the GitHub API before retrying the merge. This requires either (a) no branch protection rules, or (b) branch protection with "Require status checks" that have already passed. See [Enabling auto-merge](#enabling-auto-merge) below.
4. **In-progress conflict resolution:** When the base branch advances and creates merge conflicts on open PRs whose tracking issue is in the `in_progress` or `done` wave status (still going through the review/autofix cycle, or sitting in `ai:done` awaiting promotion to `ai:ready-to-merge`), the poller detects the conflict (`mergeable == false`). It first tries a GitHub API branch update; if that fails (real conflicts), it dispatches the review workflow via `workflow_dispatch`. The review workflow's built-in Codex conflict resolver then handles the resolution on a dedicated runner with a clean environment.
5. **Polling:** Every ~5 minutes (cron schedule), the poller checks if the current wave's issues have reached `ai:merged`. When all are merged, it runs the judge. The legacy end-of-run self-retrigger (cooldown sleep + `workflow_dispatch`) was removed — each cycle is started by the wrapper's cron entry.
6. **Judge:** Full repo checkout + tool access (shell, file reads). Compares merged code against the project spec. Decides: complete, in_progress (next wave or fix-ups), or failed.
7a. **Clean-wave skip (flagged):** When `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`, and the completed wave has no failed issues, project is not complete, and it is not a stuck-wave invocation, the poller advances `current_wave` and increments `judge_cycle` without calling Codex judge. `judge_stall_cycles` is unchanged.
7b. **Clean project-completion skip (flagged):** When `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`, the final wave is complete with all issues merged, no failures, no review-blocked issues, and no stuck-wave invocation, the poller emits a synthetic `complete` verdict without calling the Codex judge. The outcome is deterministic in this case — the LLM judge cannot add value and risks empty-output failures.
8. **Next wave:** When the judge approves, the poller creates the next wave's issues (deferred creation — they don't exist until their dependencies are met). This triggers `clarify.yml` via `issues.opened`.
9. **Review-blocked resolution:** When a PR exhausts its autofix iterations (`ai:review-blocked`), the poller invokes a dedicated review-blocked judge (medium thinking, full PR context). The judge makes autonomous architectural and security trade-off decisions — it does not defer to humans. It can: (a) merge the PR as-is if remaining issues are cosmetic or low-risk, (b) push an `[orchestrator-fix]` commit with targeted fixes (resets the autofix counter, re-triggers review), (c) close the PR and create a replacement issue with refined guidance, or (d) `merge_with_followup` — merge as-is and open a follow-up issue tracking a deferred-but-non-blocking gap (preferred over close+reissue at IS_FINAL when the PR is shippable so the existing in-PR work is preserved). After `MAX_REVIEW_BLOCKED_RETRIES` (default 2), the judge must choose merge, merge_with_followup, or close+reissue — no further fix attempts.
10. **Implementation-failed recovery:** When the implementation phase reaches the post-Codex pre-commit path with no committable file changes despite an approved plan (e.g. workflow edits stripped without `ALLOW_WORKFLOW_EDITS=true`, or model failure), `implement.yml` labels the source issue `ai:implementation-failed`. The poller automatically closes that issue and creates a replacement with additional diagnostic guidance, so the pipeline retries without manual intervention. For no-op implementation failures this behavior is unchanged; retries are bounded by `MAX_IMPL_NOOP_REISSUES`.
10a. **Post-Codex syntax repair (in-job):** If `Validate syntax of changed files` fails, `implement.yml` runs an in-job recovery loop before commit/push. The loop is capped by `MAX_POST_CODEX_REPAIR_ATTEMPTS` (default `3`; must be a non-negative integer, where `0` disables in-job repair and invalid values fall back to `3`), invokes Codex with `prompts/mode-implement-repair.txt` plus captured diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), and re-runs syntax validation via `scripts/validate_changed_files_syntax.sh` after each attempt. Repair edits are scope-guarded to the initial post-Codex changed-file set, intersected with captured-file entries when present. Any out-of-scope tracked edits are rolled back and out-of-scope untracked files are deleted; the attempt is counted as failed.
10b. **Post-Codex diagnose + fix-up issue creation:** For targeted post-Codex implementation failures, `implement.yml` captures diagnostics (`${RUNTIME_DIR}/post_codex_validation_errors.txt`), runs a non-fatal syntax check step first, then enforces a separate fatal syntax gate step so repair opportunities can run before final failure. When syntax repair is exhausted/unsuccessful (or for other targeted post-Codex failures with captured diagnostics), it runs the diagnose pass (`prompts/mode-implement-diagnose.txt`) and creates orchestrator-compatible fix-up issue(s). Each created fix-up now receives both `ai:clarification` (pipeline-entry) and `ai:implement-fix-up` (ops marker) labels. The source issue summary comment includes machine-readable blocker metadata (`IMPLEMENT_FIXUP_BLOCKERS_V1`) with `fixup_issue_numbers` and `blocks_source_issue`, which the poller persists additively into orchestrator state for implementation-failed reissue handling. If diagnosis/parsing fails, it creates a deterministic fallback fix-up issue with raw captured diagnostics so failures are never swallowed. This path applies `ai:implementation-failed` and suppresses the generic failure relabel/comment path (preventing re-add of `ai:awaiting-approval`). Out-of-scope failures (missing/empty capture file) continue using the existing generic failure behavior unchanged.
10c. **Implementation-failed blocker gating:** If an `ai:implementation-failed` source issue has post-Codex failure context and linked fix-up blocker issues, the poller defers close/reissue while any blocker issue is still `open` (or when blocker status lookup is unknown). During deferral, it logs and sends Telegram context including mode (`post-codex-validation`), blocker list/statuses, and the defer reason. Reissue resumes only after blockers are no longer open; reissued guidance text is mode-specific (no-op guidance for no-op failures, syntax/blocker-sequencing guidance for post-Codex validation failures). Blocker dependency metadata is persisted additively on the wave issue entry (`depends_on` when already present, otherwise `reissue_depends_on`) for backward compatibility.
10d. **Destructive-commit guard (`ai:destructive-blocked`):** Before creating the AI implementation commit, `implement.yml` inspects the staged deletion set. The commit is refused — and the workflow run fails — on either of two conditions: (a) any deletion touches the canonical workflow-source list (`agents.md`, `ai_pipeline.md`, `unattended_system_instructions.md`, `prompts/**`, `scripts/**`, `.github/ai/**`) and `ALLOW_WORKFLOW_EDITS` is not `true`, or (b) the total staged deletions exceed the effective bulk-delete threshold and `ALLOW_BULK_DELETE` is not `true`. The effective threshold is `BULK_DELETE_THRESHOLD` (default `3`) when at least one staged deletion is a non-`.md` file, and the lenient `BULK_DELETE_THRESHOLD_MD` (default `100`) when every staged deletion is a `.md` file — docs/scratchpad cleanups such as `analysis/*.md` backlog purges commit without operator intervention, while any source-file deletion still trips at the strict cap. On rejection the issue is labeled `ai:destructive-blocked`, a visible comment is posted listing the blocked deletions, and a CRITICAL Telegram alert is sent so a human can intervene. The `Validate approval phase label` step at the top of every subsequent `implement.yml` run refuses to redispatch any issue carrying `ai:destructive-blocked` until a human removes the label after auditing the earlier rejection — the orchestrator's judge-cycle may still regenerate the same task under a fresh issue number, so the TG alert is the intended human-in-the-loop signal. This guard exists because PRs #917/#931 saw a test harness that set `GITHUB_REPOSITORY=owner/repo` trigger a consumer-repo cleanup block in `scripts/orchestrate_poll_process.sh` from within the real coding-workflows checkout, causing the AI implementation commit to silently delete ~10,700 lines across 28 tracked source files. The gate in the poller/review_rb_judge scripts has since been switched from the env var to a git-remote-URL check; the destructive-commit guard in `implement.yml` is the defense-in-depth layer that catches any future destructive path regardless of its trigger.
10e. **Targeted vs legacy post-Codex failure flow:** Targeted post-Codex failures with captured diagnostics follow 10a/10b (syntax repair first; if unresolved, diagnose + fix-up issue creation, then label source issue `ai:implementation-failed`) plus blocker-aware reissue gating in 10c. The no-op pre-commit path in 10 remains the close/re-issue retry lane. Other implement workflow failures (for example, missing/empty capture artifacts) remain on the legacy path (`failure()`/`cancelled()` handling in `implement.yml`) with failure comments/alerts.
10f. **Success-no-op short-circuit (Guard 0, `ai:closed`):** The "Run Codex implementation" step in `.github/workflows/implement.yml` snapshots the worktree with `git status --porcelain -uall` into `${RUNTIME_DIR}/codex_pre_baseline.txt` BEFORE the retry loop. Detection, retry-nudge, and success checks all diff against this baseline via `grep -vxFf` so runtime support checkouts (`.codex-workflow-src`, `.codex-workflow-src-main`, `ai-memory/schemas`) don't register as Codex-produced changes. When the baseline-relative delta is empty AND Codex stdout matches `/no file changes were made|nothing to change|already (aligned|implemented|satisfied|up[- ]to[- ]date|done|exists|present|complete)|no changes needed|no repository changes (were )?made|no file changes made|no repository changes (were )?required|no files (were )?modified|no repository changes (were )?needed|no file changes (were )?needed/i`, the step writes `${RUNTIME_DIR}/codex_success_noop.flag` and breaks with success. The "Handle no-op implementation" step's Guard 0 sees this flag first (before Guard 1's pathspec hard-fail and Guard 2's ancestor-chain cap), closes the issue with `ai:closed` + an ✅ "Already implemented" comment, and exits with `0`. This prevents the orchestrator re-issue loop from spawning duplicate sub-issues when Codex correctly reports the requested work is already on the integration branch (observed failure: issue #141 after `npm run audit:ci` was already exit-0 from a sibling sub-task). Fail-open: missing flag/`RUNTIME_DIR` or a failed flag write falls through to Guards 1/2 as before.

10g. **files_touched scope guard (`ai:scope-blocked`):** Sibling to the destructive-commit guard (10d), this guard enforces the per-issue `files_touched` allowlist that orchestrator decomposition declares (and that issue bodies carry as a `files_touched:` block). Before the AI implementation commit, `implement.yml` computes the staged change set (`git diff --cached --name-only --diff-filter=ACMRD`) from the same staged index the destructive guard inspects — at both the preflight stage (fail fast) and the commit-time path — and refuses the commit (the run fails, nothing is committed or pushed) when any staged path falls outside the allowlist. Matching supports exact paths, directory-prefix entries (`frontend/`), and globs (`frontend/**`), with leading `./` and trailing `/` normalized; dependency lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `go.sum`, `composer.lock`) are auto-allowed but compiled build outputs are not. Parsing reuses the same `files_touched:` block semantics as the review-time scope verifier (`scripts/review_reject_verify.sh`) via the shared helper `scripts/files_touched_scope_guard.py`. On rejection the issue is labeled `ai:scope-blocked`, a visible comment lists the out-of-scope paths plus the declared allowlist, and a CRITICAL Telegram alert is sent; the `Validate approval phase label` step refuses to redispatch any issue carrying `ai:scope-blocked` until a human removes the label. The guard **fails open** — it skips silently when the issue declares no `files_touched` allowlist, when the master toggle `ENFORCE_FILES_TOUCHED` is `false`, or when the helper is unavailable — and `ALLOW_OUT_OF_SCOPE_FILES=true` downgrades a block to a warning for a single run. This guard exists because consumer-repo project #244 / issue #254 ("frontend-send-status") blew past its `frontend/`-only scope in one commit — editing four root TypeScript files and committing sixteen compiled `.js` twins — with nothing in `implement.yml` to stop it, forcing a downstream judge cleanup that then tripped the destructive guard and required manual intervention.
11. **Auto-recovery:** On failure, the judge can revert problematic PRs and create fix-up issues. Those fix-up issues include the standard orchestrator metadata block (`Tracking issue`, `Integration branch`, `Local ID`, `Managed by`) in the issue body. Recovery is attempted up to `MAX_RECOVERY_ATTEMPTS` (default 3) times; if all attempts fail, the project stops and the operator is notified via Telegram.
12. **Validation-failure recovery:** When runtime validation fails, the poller transitions the project back to the judge for re-evaluation (labeled `ai:validation-recovery`) up to `MAX_VALIDATION_RECOVERY_ATTEMPTS` (default 2) times. The judge sees the validation diagnosis in tracking issue comments, can issue fix-up work (with orchestrator metadata), and then re-validates. After exhausting the recovery budget, the project goes to terminal `ai:validation-failed`.
12a. **Integration branch delivery:** Orchestrator projects now create a per-project integration branch (`orchestrator/project-<tracking_issue>`). All orchestrator child issues include `Integration branch` metadata so implementation PRs target the integration branch instead of `main`. Branch resolution order is strict: child issue metadata footer first, then tracking issue metadata, and default-branch fallback only when no integration metadata exists. If metadata exists but the branch is invalid/missing, the poller fails safe instead of silently falling back to default branch. The poller periodically syncs default branch changes into this branch via the merge API.
12b. **Sync conflict handling and superseded detection:** Before sync merge attempts, the poller checks whether the integration branch is effectively superseded by the default branch (tracked child PRs are terminal and affected-path deltas are already represented on the default branch). Superseded projects persist `sync.status = superseded-by-main`, post one final tracking comment, and skip future sync attempts without recurring Telegram warnings. Real unresolved conflicts include parsed conflict paths, a deduped fingerprint to prevent repeated spam, and a rebuild runbook link: [docs/orchestrator-integration-branch-rebuild-runbook.md](docs/orchestrator-integration-branch-rebuild-runbook.md).
12c. **Integration self-healing:** If a periodic `main` → integration-branch sync returns HTTP 409 (real conflict), the poller routes recovery through `heal_integration_branch_conflict`: it (a) ensures/creates the final integration→default PR (eagerly, if it does not yet exist), (b) dispatches the review/autofix workflow through `_dispatch_review_for_conflicts` against that PR to run the existing Codex conflict resolver on a clean runner, and (c) records the attempt in new tracking-state fields (`integration_sync_status`, `integration_sync_last_error`, `integration_conflict_dispatch_count`, `integration_conflict_dispatch_ts`, `integration_conflict_unresolved_ticks`). Dispatches are throttled by `CONFLICT_DISPATCH_COOLDOWN_SECS` (default 900s). The retry budget is **branch-aware**: head refs matching `orchestrator/project-*` honour `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` (default `1` — single resolver shot, then judge), while non-orchestrator integration branches honour `INTEGRATION_CONFLICT_MAX_RETRIES` (default `3`). After the effective budget is exhausted the orchestrator escalates by invoking the judge with full PR context via `codex exec`. Only after both the automated resolver *and* the judge escalation fail is the project marked terminally `failed`. The same healing flow is triggered from `finalize_integration_merge_if_needed` whenever the final PR is observed with `mergeable=false`, so the project no longer halts on first conflict.

12c-i. **Integration-sync intent fingerprints:** When a sub-issue PR merges into an orchestrator integration branch, the poller captures `must_contain` / `must_not_contain` regex fingerprints from the merged diff and persists them under `merged_issue_fingerprints[<issue_num>]` in the orchestrator state comment. The implementing PR is resolved by `scripts/orchestrate_poll_process.sh::_subissue_closing_pr_number`, which selects the most-recently-merged PR on the orchestrator's conventional `ai/issue-<n>` head branch and, only if that lookup is empty, falls back to the newest merged cross-referenced PR whose body carries a closing keyword (`Closes`/`Fixes`/`Resolves`, in `#N` or issue-URL form) targeting the issue. It deliberately does **not** pick the most-recent cross-reference: a `Refs #N` cross-reference from an unrelated infrastructure PR is not an implementation and must not be fingerprinted (the pre-fix behaviour captured such a PR's diff, leaving the wave-dispatch gate permanently wedged because those lines were never merged onto the integration branch — observed on project #2867 / issue #2872). When no implementing PR can be identified, capture is skipped rather than fingerprinting an arbitrary PR. Capture (in `scripts/orchestrate_poll_process.sh::capture_intent_fingerprints_for_merged_subissue`) applies two filters before persisting: (1) a net-no-op filter that drops any stripped line a PR both removed and re-added (else the pair would be self-contradictory), and (2) a substring-overlap filter that drops removed-line stripped text that is a literal substring of any added-line stripped text on the same file (else under `re.search` any tree satisfying the longer `must_contain` would also match the shorter `must_not_contain`, producing a structurally unsatisfiable pair when a sub-issue extends a line by appending text). The `review_autofix.yml` resolver step uses three affordances on top of these fingerprints when the PR head ref matches `orchestrator/project-*`:
- **Intent injection into the resolver prompt.** The conflict resolver prompt is rendered from `prompts/integration-sync-conflict-resolver.txt` (instead of the generic `prompts/conflict-resolver.txt`) and includes the tracking-issue title/body, the list of merged sub-issues already on this integration branch, and the full `merged_issue_fingerprints` JSON. The template instructs the model to treat each fingerprint as a hard test case and to **synthesize** a new hunk when the conflict is between two independent rewrites of the same code rather than picking side A or side B verbatim. The template also contains two anti-regression hardening blocks aimed at the dominant observed failure mode ("pick default-branch side verbatim, drop HEAD sub-issue content"): (1) a "do NOT pick the default-branch side verbatim" rule tying non-empty-fingerprint files to "keep HEAD or synthesize", and (2) a per-file fingerprint pre-flight that requires the model to reconcile every `must_contain` / `must_not_contain` pattern for a file before moving on to the next file, plus a self-check that requires reporting each pattern's match status before declaring success.
- **Fingerprint verification gate.** Before the `[ai-merge-resolve]` commit lands, `scripts/verify_integration_fingerprints.py` walks every captured pattern against the post-resolve working tree. A `must_contain` pattern that no longer matches, or a `must_not_contain` pattern that reappears, is treated as a silent intent regression and HARD-fails the resolver step (the merge state is left intact so the next poll tick re-enters healing and — by default — escalates immediately to the integration judge). A silent-regression detector additionally logs a warning whenever the post-resolve tree contains strictly fewer total `must_contain` matches than were captured. Before evaluating, the verifier runs the same defensive cross-dedup as the capture half (drop `(file, regex)` pairs present in both lists, plus `must_not_contain` regexes whose source is a literal substring of a `must_contain` regex on the same file) and emits a `::warning::` per drop class; this companion check rescues state files captured before the capture-side substring filter landed (e.g. `tele-funtoken-msg-scoring` PR #2852, where `must_not_contain=critic\-driven\ cohort\-mix\ rollouts\.` was a strict prefix of `must_contain=critic\-driven\ cohort\-mix\ rollouts\.\ When\ critic\ authority\ is\ enabled,\ accepted` on the same file and the resolver burned its 3-attempt budget against an impossible constraint). On integration-sync resolver runs, `scripts/review_conflict_resolve.sh` first captures `--baseline-fingerprints-state` and then reruns the verifier with `--compare-against-baseline`, so only resolver-introduced regressions hard-fail; pre-existing drift stays visible through `PRE_EXISTING_FINGERPRINT_DRIFT_V1` warning/notice markers instead of blocking the commit.
- **Retry-loop with reflexion (verify-in-loop + per-attempt reset).** Previously, the resolver step ran `codex exec` once and then evaluated the fingerprint verifier after the retry loop; the loop only retried when `codex exec` crashed or produced empty output, so a bad-but-well-formed model output terminated the whole run with zero retries consumed on the real failure class. Large integration PRs hit this reliably. `scripts/review_conflict_resolve.sh` now runs the codex resolver up to `INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS=3` times with the soft quality gates — residual Git conflict markers (`<<<<<<< ` / `>>>>>>> ` scan over every path in `resolver_unmerged_allowlist.txt`) and the full `scripts/verify_integration_fingerprints.py` pass — running **inside** the loop. On a soft failure the working tree is restored from a pre-first-attempt snapshot (every file in the allowlist, snapshotted via `cp -a` into `${RUNTIME_DIR}/resolver_attempt_base/` before the loop starts) and the next attempt is given a **reflexion prompt** built from `prompts/integration-sync-conflict-resolver-retry-prelude.txt` concatenated in front of the rendered `${CONFLICT_RESOLVER_PROMPT_FILE}`. The prelude names each file with residual markers and each fingerprint regex that regressed, so the retry fixes specific violations rather than re-rolling the whole merge blind. On intermediate attempts the verifier output is captured with annotations suppressed (no false-positive `::error::` flood in the GHA log); on exhaustion the verifier is re-run at normal verbosity so the `"Aborting [ai-merge-resolve] commit: integration fingerprint verification rejected the resolver output."` annotation lands exactly as before. Hard gates (workflow-file allowlist violation, `scripts/check_resolver_diff.sh`) still run **once** post-loop on the accepted attempt — retrying them is unsafe (a hallucinated workflow edit must never be handed back to the model as "try again, here's what went wrong"). The reflexion prompt is integration-sync only; generic (non-integration) resolver runs still benefit from verify-in-loop + per-attempt reset + marker pre-scan but retry with the original prompt verbatim. `prompts/integration-sync-conflict-resolver-retry-prelude.txt` is a soft dependency: a missing template falls open to "retry with original prompt" plus a `::warning::`, matching the handling pattern of `prompts/conflict-resolver.txt` so older consumer-repo `script_ref` pins bootstrap cleanly.
- **No-progress detection + step wall-clock cap + immediate-judge dispatch.** Three small additions close the failure mode where a structurally-impossible integration merge (sub-issues with logically contradictory `must_contain` / `must_not_contain` fingerprints) consumed an entire 180-min job budget (the job's `timeout-minutes:` cap at the time of that incident; later raised to 240 — see (2) below) on three resolver attempts that all reproduced the identical fingerprint violation set. (1) **No-progress detection** inside the retry loop — after each attempt's soft validation, when `IS_INTEGRATION_SYNC=true` and the current `${RUNTIME_DIR}/resolver_fp_violations.txt` is byte-identical to the previous attempt's snapshot at `${RUNTIME_DIR}/resolver_fp_violations_prev.txt` (compared via `sort` then `cmp -s` so verifier output reordering is not a false-negative trigger), the attempt counter is promoted to `INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS` so the existing exhaustion block runs immediately. Same `CONFLICT_RESOLVED=false` + `exit 1` shape as natural exhaustion, so downstream tooling (telegram alert, `ai:integration-judge-failed` transitions, immediate dispatch trap below) sees the same signal whether the loop bailed early or ran to MAX_ATTEMPTS. Restricted to integration-sync runs because only fingerprint violations carry stable per-pattern identity; residual-marker presence/absence is not a reliable progress signal. (2) **Step wall-clock cap + per-attempt cap** — the `Run Codex resolver, validate, stage, commit` step in `.github/workflows/review_autofix.yml` carries `timeout-minutes: 170`, sized so 3 × 50-min attempts plus ~20 min for soft validation, commit, and the EXIT-trap dispatch fit inside the job's `timeout-minutes: 240` outer cap (raised from 180 so ~24 min of pre-resolver work — disk cleanup, checkout, codex install, runtime workspace, prompt prep, reviewers, editor — plus this 170-min step plus ~46 min of post-resolver work fit cleanly; a flagged review comment on PR #2453 noted the previous 180-min outer cap left only ~10 min of job-level headroom over the 170-min step cap, which could SIGKILL the EXIT-trap dispatch on a long resolver run). The step cap was raised from 60 min after run 25629086684 / PR #2865 on `tele-funtoken-msg-scoring`, where every one of the 3 resolver attempts hit the previous 18-min per-attempt ceiling without ever producing `apply_patch` on a 7-file mixed-implementation merge — the symptom in the log was three back-to-back `Conflict resolver retry … (prev markers=0, prev fingerprint_violations=0)` lines followed by `Conflict resolver failed after retries.`, with the misleading "0 markers / 0 fp" counts reflecting the fact that soft-validation never ran on a timed-out attempt. The default + validation + clamp of `CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS` runs **once before the retry loop**, normalising the value to the default of `3000` (50 min, raised from 18 min) when unset / non-numeric / above the upper bound, so the value enforced by the `timeout` wrapper, the value substituted into the retry-prompt template, and the value printed in the retry log are guaranteed to agree across all iterations. Inside the retry loop each `codex exec` invocation is wrapped in `timeout --signal=TERM --kill-after=30s "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}"` so a runaway first attempt cannot exhaust the full 170-min step budget before the retry loop iterates — without this cap, `INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS=3` is structurally meaningless on hangs because attempt 1 alone can absorb the entire step cap on a large multi-file conflict set. 3 attempts × 50 min = 150 min fits inside the step cap with ~20 min headroom for soft validation, commit, and the EXIT-trap dispatch. The wrapper's non-zero exit flows through the `if [ "${_codex_exit}" -ne 0 ]; then …; continue; fi` branch in `scripts/review_conflict_resolve.sh`, which captures the exit code AND the wall-clock elapsed time, then classifies the failure: `124` (SIGTERM after the per-attempt timer fired) is unconditional `_prev_attempt_failure_kind="timeout"`; `137` (SIGKILL) is `"timeout"` only when `_attempt_elapsed >= CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS` (a `timeout`-driven SIGKILL fires at duration + ~30s; an OOM kill or external SIGKILL at minute 2 of a 50-min budget gives elapsed ≪ duration and instead routes to `"exec_error"`). On a real timeout the next iteration's reflexion prompt picks the timeout-aware variant (`prompts/integration-sync-conflict-resolver-retry-timeout-prelude.txt`) telling the model the previous attempt was killed by the per-attempt timer (any partial `apply_patch` calls were discarded by the per-attempt working-tree restore, no soft-validation data was captured) and to be decisive — pick the smallest convergent resolution and call `apply_patch` early. Other non-zero exits (config / auth / model errors that codex itself returns before the timer fires) are recorded as `"exec_error"` and retry with the original prompt verbatim — the script has no useful reflexion data to feed back, so the standard prelude's "your output failed validation" framing would be actively misleading. Soft-validation failures (codex returned 0 but the gates rejected its output) record `_prev_attempt_failure_kind="validation"` and use the standard violations-listing prelude as before. Override `CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS` (in seconds) for per-PR tuning when a particular conflict set legitimately needs longer attempts; values above 3000s start eating into the 20-min headroom and are clamped down with a `::warning::` log. (3) **Immediate orchestrator-poll dispatch** via an EXIT trap in `scripts/review_conflict_resolve.sh` (`_dispatch_integration_judge_now`) — on any non-zero exit from the resolver script when `IS_INTEGRATION_SYNC=true`, the script fires `gh workflow run ${ORCHESTRATE_POLL_WORKFLOW_FILE:-internal-orchestrate-poll.yml}` against the current repo so the orchestrator integration judge picks up on the next concurrency slot rather than waiting up to 5 min for the `*/5` cron tick. Dedup: skipped when a poller run is already `in_progress` or `queued` (the poller's `concurrency.group: ai-orchestrate-poll-${{ github.repository }}` + `cancel-in-progress: false` already serialises runs across the repo). Idempotent within a single script invocation. Fail-open: missing `GH_PAT`/`GITHUB_REPOSITORY`, an `gh` rate-limit, or an unknown workflow filename on a consumer repo logs `::warning::` and falls through; the cron tick remains the safety net so unattendedness is preserved. Exit-0 paths (resolver succeeded in deciding no commit was needed) intentionally do not fire — no escalation is warranted. Consumer repos that ship the orchestrator poller under a non-default filename can override via the `ORCHESTRATE_POLL_WORKFLOW_FILE` env var.
- **Pre-codex working-set expansion.** The verifier is also invoked in `--list-violated-files` mode by `scripts/review_conflict_prepare.sh` after the merge replay but before the resolver prompt is rendered. Any file whose fingerprints already fail against the auto-merged tree — i.e. `git merge` resolved the textual diff cleanly but the resolution silently dropped a merged sub-issue line, or reintroduced one the sub-issue had deleted — is appended to the resolver working set: the prompt's in-scope file list (so Codex is told to inspect it), the unmerged-paths allowlist (so the workflow-file violation guard in `scripts/review_conflict_resolve.sh` permits an edit), and the conflicted-paths set (so `scripts/check_resolver_diff.sh`'s `touched ⊆ conflicted` guard permits the edit). Without this expansion the dominant fail-mode observed in practice — main carries an independent rewrite of a hunk a sub-issue also rewrote, `git merge`'s 3-way resolution picks main's side verbatim, no conflict markers are emitted, and the resolver is structurally unable to touch the file — reaches the post-codex verifier as an irrecoverable violation and wastes the entire run. Fail-open: if the verifier is not bootstrapped on the current `script_ref` or exits `2` (plumbing failure), the expansion is skipped and the run proceeds with the git-marked conflicted set only.

- **Path-level deletion contract (`must_not_exist`).** Alongside the text-regex `must_contain` / `must_not_contain` pair, capture also records every file the merged sub-PR removed outright (PR diff status `removed` — `+++ /dev/null` against `--- a/<path>`) under a separate `must_not_exist` list inside the same `merged_issue_fingerprints[<issue_num>]` entry. Unlike the text-regex side, `must_not_exist` capture is **path-agnostic**: the resolver-safe `ALLOWED_PREFIXES` allowlist (`.github/`, `scripts/`, `prompts/`, `ai-memory/`, `tests/`, `workflow-templates/`, `docs/`, `db/contracts/`, and root `{agents,README,CLAUDE}.md`) does NOT apply, because the allowlist's rationale (skip binary-prone / generated / out-of-scope-for-resolver-edits content) is specific to regex capture and doesn't carry over to a binary "is this path present?" check. This closes the failure mode where a back-merge resolver silently re-introduced a consumer-source file (e.g. `backend/foo.py`) that an earlier sub-issue had deleted — the text-regex side never fingerprinted the deletion because `backend/` is outside the allowlist, so the resolver was free to resurrect it with no contract violation. `scripts/verify_integration_fingerprints.py` rejects with exit 1 if any `must_not_exist` path is present in the post-resolve tree (or at the verified ref — see the wave-dispatch gate below).
- **Wave-dispatch integration-state gate.** Before posting `## Wave N+1 Dispatched` and creating new sub-issues for the next wave, the orchestrator poller runs `scripts/verify_integration_fingerprints.py --ref <integration_branch>` against the integration branch HEAD. The `--ref` mode reads file contents via `git show <ref>:<path>` and existence via `git cat-file -e <ref>:<path>`, so the gate verifies without checking the branch out. If the integration HEAD violates any captured `must_contain` / `must_not_contain` / `must_not_exist` fingerprint (typically because a back-merge from `main` reintroduced files an earlier wave's sub-issue had deleted), the poller blocks the wave dispatch: it bumps the stall cycle, posts a tracking comment naming the violation lines, and fires a `WARNING` telegram alert instead of creating the next wave's issues. Without this gate, a regression on the integration branch silently propagated into the next wave's planner runs as "BLOCKED: required final sanity grep still non-zero outside this issue's scoped files" — wasting one wave per regression on a defect originating several merges earlier. The gate fails open on plumbing issues (fingerprints empty, verifier script missing, branch ref not fetchable) so a transient network or capture gap never blocks dispatch. `INTEGRATION_VERIFY_REF` env var is honoured as a fallback for callers whose argv plumbing can't pass `--ref`. Immediately before the verifier runs, the gate also invokes `_purge_stale_fingerprint_entries_on_integration_branch` (`scripts/orchestrate_poll_process.sh`) to self-heal `merged_issue_fingerprints` entries the gate cannot reasonably satisfy. Capture is idempotent, so a single bad capture writes an entry that no later poll tick overwrites. Two stale shapes are caught from local git plumbing alone (zero GitHub API calls): (1) the recorded PR has no commit referencing `(#<pr>)` on the integration branch — its diff cannot be on the branch; (2) the recorded PR DOES have a merge commit (subject ending `(#<pr>)`) but the entry's `captured_at` predates that commit's committer date — capture ran against an open-PR snapshot whose content was iterated before the squash-merge landed. Healthy entries (capture ran AFTER the merge) have `captured_at` > merge committer date and are kept untouched, so a genuine post-merge resolver regression still hard-fails the gate as designed. A complementary verifier-side defense in `scripts/verify_integration_fingerprints.py` covers the case the self-heal cannot reach — a *healthy* capture whose `must_contain` line is later modified **in place** by a legitimate non-resolver commit (a subsequent sub-issue PR squash-merge, an `[ai-autofix]` commit, or an out-of-band fix PR merged onto the integration branch), leaving the stale exact `re.escape` regex unmatched at HEAD. For each such `must_contain` miss the verifier attributes the line's disappearance to a single commit via a first-parent fixed-string pickaxe (`git log -S<line> --first-parent <ref>` against the captured line, recovered by un-escaping the regex); when the newest hit is dated after `captured_at` and is **not** an `[ai-merge-resolve]` resolver commit, the miss is reclassified as legitimate post-capture evolution, the violation is skipped, and a `::warning::FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1 issue=#<n> pr=#<p> file=<path> superseding_commit=<sha>` marker is emitted. A file-level "did any resolver commit touch this file after capture" check is deliberately **not** used — routine resolver back-merges of `main` touch many files without reverting any specific line and would re-block every project that ever back-merged. The defense fails closed: a resolver commit removed the line, no attributable commit, a non-`re.escape` regex, a missing/unparseable `captured_at`, or working-tree mode (the resolver's own pre-commit self-check) all surface the violation as before, so genuine resolver reverts still hard-fail the gate. Forensic origin: project #3042 wave 7 — PR #3076 (a non-resolver fix) inserted `--skip-git-repo-check` into issue #3044's codex-exec lines, and an `[ai-autofix]` refactor rewrote issue #3058's `GH_PAT`/`GH_TOKEN` block into a `dispatch_token` local and extended issue #3066's `REQUIRED_BOOTSTRAP_SCRIPTS` list, dropping the gate to 835/841 `must_contain` matches and wedging wave 7 on six phantom regressions even though every merged sub-issue's intent was preserved on the branch. Each purge emits `::warning::FINGERPRINT_STATE_SELFHEAL_V1 issue=#<n> pr=#<p> reason=<...> ref=<sha>` and the gate posts a single tracking-issue comment + INFO Telegram notice enumerating the purged entries so the audit trail is visible. Forensic origin: project #2867 / issue #2872 — pre-PR-#2907 capture latched onto open PR #2894 (a `Refs #2872` cross-reference); PR #2894 was then iterated in review and squash-merged ~2h after capture, so the captured fingerprints never matched the merged content; the gate hard-failed every poll tick on phantom regressions until this self-heal landed.

Capture is **going-forward only**: sub-issues merged before fingerprinting was enabled have no entries in `merged_issue_fingerprints` and are silently skipped by the verifier (fail-open). Capture and verification are tunable via `FINGERPRINT_PER_FILE_CAP` / `FINGERPRINT_MIN_PATTERN_CHARS`. The resolver safety scripts (`verify_integration_fingerprints.py`, `review_conflict_prepare.sh`, `review_conflict_resolve.sh`) are bootstrapped via `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS`, which prefers the main snapshot over the branch copy so wedged integration branches still pick up self-heal fixes shipped on `main`; when the verifier is absent from both refs the resolver still fails open with a warning rather than hard-failing older pins. Operationally: a verification rejection always surfaces in the workflow run log with `::error::Aborting [ai-merge-resolve] commit: integration fingerprint verification rejected the resolver output.`, the resolver step exits 1, the conflict resolver workflow lands without a commit, and the orchestrator's next poll tick takes the judge path because `unresolved_ticks` was already incremented by the dispatch.

- **Resolver escape valve, sticky PR-body state, and tier ladder.** `scripts/review_conflict_resolve.sh` persists one `<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1 ... -->` JSON block in the final PR body, keyed to the PR head SHA. It records the normalized failure signature, `consecutive_failure_count`, `verification_tier`, `escalation_threshold`, and `escalated` / `escalated_at`, emits `FINGERPRINT_TIER_DOWNGRADED_V1` when the tier moves to `ratio`, `count_only`, or `warn_only`, and applies `ai:resolver-escalated` to the **final PR issue** only after the same head and normalized failure signature exhaust the warn-only tier. The poller reads that sticky PR-body state and suppresses further first-line redispatch until the head SHA changes.
- **Adaptive quarantine + scheduled drift audit.** `scripts/verify_integration_fingerprints.py` tracks unchanged pre-existing drift in ai-memory; after `FINGERPRINT_QUARANTINE_RUNS_M` consecutive unchanged observations it skips that `fp_key` and emits `FINGERPRINT_QUARANTINED_V1`. `.github/workflows/drift-audit.yml` runs daily at `03:00 UTC`, stays inert unless `DRIFT_AUDIT_ENABLED=true`, and uses `scripts/drift_audit.sh` to scan recent review/autofix logs for `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` clusters, then create or refresh tracker issues for persistent drift. Every enabled run also posts a Telegram run summary linking to the workflow run and writes a GitHub Actions job-summary report.
- **Last-resort branch rebuild.** When `BRANCH_REBUILD_ENABLED=true`, the poller can rebuild only `orchestrator/project-*` integration branches whose final PR already carries persisted resolver escalation and whose `escalated_at` age exceeds `BRANCH_REBUILD_THRESHOLD_HOURS` with no successful or attempted rebuild inside `BRANCH_REBUILD_COOLDOWN_HOURS`. The destructive delete/recreate path fails safe on protected branches, missing ai-memory audit storage, or missing replay metadata, and every attempt persists a `BranchRebuildAuditV1` record (`ai-memory/schemas/branch_rebuild_audit.v1.json`) capturing trigger timestamps, replay commits, branch-protection status, outcome, and any failure detail.
12d. **Atomic final merge:** When a project is complete (or validated), the poller creates/reuses a final PR from integration branch to default branch and squash-merges it. As soon as the integration branch is numerically ahead of the default branch, the poller now ensures that PR exists as a **draft** and keeps a managed `<!-- VALIDATION_STATUS_V1 -->` section in the PR body so validation state is visible before merge eligibility. The poller only promotes that draft PR to ready when the tracking issue reaches `ai:ready-to-merge` (with backward-compatible fallback for already-validated / validation-disabled legacy flows). Operators can also apply the `ai:force-merge` label during the validation lifecycle: when the integration branch still has queued work, the poller promotes the existing eager draft PR immediately, posts audit comments on both the tracking issue and the integration PR, and persists a deterministic operator-bypass audit entry keyed by tracking issue + integration SHA. Separately, `ORCH_INTEGRATION_MAX_AHEAD_COMMITS` (default `10`) is the *floor* for integration backpressure: once the integration branch is ahead of default by at least the **effective** threshold — `max(ORCH_INTEGRATION_MAX_AHEAD_COMMITS, planned_issue_count + ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN)` — the poller labels the tracking issue `ai:integration-backpressure`, pauses additional sub-issue merges into the integration branch, points operators at the open integration PR in the completion-status comment, and clears the label automatically after the backlog shrinks below the effective threshold. The size-aware floor is required because the integration→default PR only drains at completion, so a project with more planned sub-issue commits than a flat floor would otherwise self-deadlock (backpressure pausing the very merges needed to reach completion); the configured value still bounds anomalous over-drift beyond a project's planned scope. **"Complete" requires the default branch to contain the integration branch tip** (`ahead_by == 0` via GitHub's compare API): `check-wave-status`'s `project_complete` and `finalize_integration_merge_if_needed`'s pinned "merged" state both re-check this on every tick. If the integration branch has drifted ahead (e.g. an early auto-merge of the eager final PR was followed by new wave PRs landing on the integration branch), the pinned state is cleared and a fresh final PR is opened for the unmerged diff. Both checks fail closed on a compare-API error (treat as "default does NOT contain integration tip" and defer to the next tick). See shubhodeep1/binance-blessings#135 for the regression case that motivated this gate.
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

Orchestrator-managed PRs use the same per-PR autofix loop as non-orchestrator PRs (reviewer/editor → up to `MAX_AUTOFIX_ITERATIONS=5` `[ai-autofix]` commits → per-PR review-blocked judge with up to `MAX_REVIEW_BLOCKED_RETRIES` retries). Two orchestrator-aware behaviours sit on top of that uniform loop, both gated by the master switch `ORCH_PR_AUTOFIX_FLOW_ENABLED` (default `true`):

1. **PR mode classification** — `review_autofix.yml`'s retrigger guard tags each PR as `orch_intermediate`, `orch_final`, or `other` for observability. The classification used to override the per-PR autofix cap (intermediate PRs were capped at 1 iteration with the judge skipped) — that override has been removed: catching blocking issues per sub-issue PR (where the diff is small and the linked-issue context is narrow) is cheaper and more reliable than letting them accumulate and surface en masse on the integration→default-branch final merge.
2. **Final-PR cap bypass** — `orchestrate_poll_process.sh` bypasses the orchestrator-level `MAX_JUDGE_CYCLES` cap while the integration→default-branch final PR is open and pending merge, so the final PR can run unlimited 5-autofix→judge cycles until mergeable.

**PR mode classification** (in `review_autofix.yml` retrigger_guard step):

The retrigger guard reads `headRefName` and `baseRefName` from `${PR_META_FILE}` and matches them against `ORCH_INTEGRATION_BRANCH_PATTERN` (default `^orchestrator/project-`):

| Mode | Detection | Per-PR autofix cap | Per-PR judge | Orchestrator-level cycle cap (project-wide) |
|---|---|---|---|---|
| `orch_intermediate` | head matches pattern AND base matches pattern (sub-issue PR → integration branch) | `MAX_AUTOFIX_ITERATIONS` (default `5`) | Runs after exhaustion (full `merge` / `fix` / `merge_with_followup` / `close_and_reissue` actions; per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`) | `MAX_JUDGE_CYCLES` (default `25`) — counts orchestrator-issued judge runs at wave-completion / project-evaluation events; per-sub-issue rb_judge runs do not increment this counter |
| `orch_final` | head matches pattern AND base does NOT match pattern (integration branch → default branch) | `MAX_AUTOFIX_ITERATIONS` (default `5`) | Runs after exhaustion (full `merge` / `fix` / `merge_with_followup` / `close_and_reissue` actions; per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`) | **Bypassed** (unlimited cycles while `final_merge_status=pending`) |
| `other` | neither head nor base matches pattern (non-orchestrator PR) | `MAX_AUTOFIX_ITERATIONS` (default `5`) | Runs after exhaustion (full `merge` / `fix` / `merge_with_followup` / `close_and_reissue` actions; per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`) | n/a — the PR is not part of any orchestrator project |

**Intermediate PR behavior** (`orch_intermediate`):
- Reviewer + editor run on each `pull_request.synchronize` (or `workflow_dispatch`) up to `MAX_AUTOFIX_ITERATIONS` consecutive `[ai-autofix]` commits, addressing CI / lint check-run failures via the existing `CHECK_RUNS_AUTOFIX_ENABLED=true` path along the way.
- On exhaustion the per-PR review-blocked judge runs and decides `merge` (auto-merge into the integration branch), `fix` (push a `[judge-fix]` commit, which resets the autofix counter and lets the loop continue — capped at `MAX_REVIEW_BLOCKED_RETRIES`, default `2`), `merge_with_followup` (auto-merge AND open a follow-up issue tracking a deferred-but-non-blocking gap — preferred over `close_and_reissue` at IS_FINAL when the PR is shippable; follow-up issue inherits `ai:orchestrator-managed` from the parent), or `close_and_reissue` (close the sub-issue PR and create a refined issue).
- The PR merges into the integration branch only when the existing orchestrator merge gate clears: `mergeable=true` AND `_pr_checks_completed` AND `ai:ready-to-merge` label set.
- The `force_rb_judge` stall-recovery path (dispatched by the orchestrator stall poller for issues stuck at `ai:review-blocked` past the threshold) is unchanged — it forces `max_iterations_reached=true` so the rb_judge step fires directly against the existing PR state.

**Final PR behavior** (`orch_final`):
- Inner loop matches the same `MAX_AUTOFIX_ITERATIONS=5` cycle: 5 consecutive `[ai-autofix]` commits → judge runs → judge may push `[judge-fix]` → autofix resumes → repeat.
- The orchestrator-level `MAX_JUDGE_CYCLES` cap is **bypassed** while `state.final_merge_pr` is non-empty AND `state.final_merge_status="pending"`. The final-PR loop terminates implicitly when reviewer/editor produce zero `[ai-autofix]` commits AND judge approves; the existing final-merge gate (`finalize_integration_merge_if_needed` at `scripts/orchestrate_poll_process.sh`) then merges integration → default branch only when `mergeable=true` + checks complete (mergeability conflicts hand off to `heal_integration_branch_conflict`, unchanged).
- Bypass observability: each cycle that would otherwise have failed against the cap emits `[final-merge] judge cap bypassed (final-PR loop active: PR #<n>, status=pending); JUDGE_STALL_CYCLES=<m> > MAX_JUDGE=<k>, proceeding to judge invocation.` to the orchestrator log.

**Non-orchestrator PRs** (`other`): unchanged. `MAX_AUTOFIX_ITERATIONS=5`, judge runs after exhaustion, per-PR retries governed by `MAX_REVIEW_BLOCKED_RETRIES`. The orchestrator-level `MAX_JUDGE_CYCLES` cap does not apply — the PR is not part of any orchestrator project.

**Failure modes**:
- **Intermediate PR judge picks `close_and_reissue`**: the sub-issue PR closes and a new issue is created with refined guidance. The orchestrator's existing closed-PR / failed-sub-issue handling resumes from there. On a small sub-issue diff this is generally safe and is preferable to merging a fundamentally flawed approach into the integration branch where it would surface (much more expensively) on the final-merge judge cycle.
- **Intermediate PR with persistent CI failure**: autofix attempts CI fixes across its `MAX_AUTOFIX_ITERATIONS` runs; if CI stays red, `_pr_checks_completed` returns false and the orchestrator does not merge. Existing stall-recovery contracts (`STALL_THRESHOLD_DONE_MINUTES`, `STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES`) recover the issue.
- **Final PR with bad judge verdict loop**: with the cap bypassed, the loop continues indefinitely as long as judge keeps producing `[judge-fix]` commits. Operator intervention path: set `ORCH_PR_AUTOFIX_FLOW_ENABLED=false` to restore the `MAX_JUDGE_CYCLES` cap, then take action against the PR.
- **Branch naming mismatch**: if your orchestrator pushes to a branch that does not match `^orchestrator/project-`, the classifier falls through to `other` and the final-PR auto-merge suppressor also treats the PR like a non-orchestrator branch. Operationally you lose both the `orch_final` cap bypass and the integration-PR auto-merge exclusion until you override `ORCH_INTEGRATION_BRANCH_PATTERN` to match your naming.

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

When the orchestrator-managed loop (`recover_stalled_issue`) and the standalone loop (`run_standalone_stall_recovery`) both detect a stall, they consult `_check_fresh_push_guard` immediately after the existing `issue_has_active_workflow` check. If the stalled issue's phase is `ai:done` or `ai:ready-to-merge` **and** the linked PR's head commit was pushed within the last **50 minutes** (hardcoded, not tunable), the recovery dispatch is suppressed for that cycle and a stable `STALL_SKIP issue=<n> reason=fresh_push pr=<p> pushed_age_secs=<s> phase=<phase> action=<action>` line is emitted. The stall counter is not incremented; the next poll tick re-evaluates. The window was originally 30 minutes; it was bumped to 50 minutes because typical `review_autofix` cycles run 35-45 minutes end-to-end on busy consumer repos, so a single cycle outlasted the original window and the guard never fired between cycles.

Rationale: `issue_has_active_workflow` only matches the moment a queued/in-progress workflow run is visible on the PR branch. Between an autofix push and the `pull_request.synchronize`-driven next run materialising (or while the autofix-retrigger dedup is swapping runs per the [Autofix retrigger dedup](#autofix-retrigger-dedup) section), no run is briefly visible to the regex in `build_active_issue_set`, so the phase-age stall timer can fire even though fresh work has landed. The `pushedDate` signal is a more reliable "work landed recently" guard for the short gap.

Data source: both stall paths already fetch the linked PR via batched GraphQL — `_fetch_linked_pr_status_graphql` (orchestrator-managed, reused via `STALL_MANAGED_LINKED_PR_CACHE`) and `_fetch_candidate_issue_details_graphql` (standalone, via `_candidate_details_json`). Both helpers were extended to request `commits(last: 1) { nodes { commit { pushedDate committedDate } } }` on the cross-referenced PullRequest, adding a `headPushedAt` field to each `linked_pr` entry (coalesced: `pushedDate` first, `committedDate` as fallback when push metadata is null — e.g. squashed commits). Zero additional API calls in the steady-state path.

Fail-open: `_check_fresh_push_guard` returns "not fresh" (i.e. lets the existing stall flow proceed) when the phase is outside `{ai:done, ai:ready-to-merge}`, when the linked-PR cache entry is missing or `null`, when `headPushedAt` is missing or unparseable, or when the computed push-age is negative (clock skew). The guard can never cause a stall recovery to fire that otherwise would not have fired; it only suppresses dispatches within the 50-minute fresh-push window.

Cross-reference fallback (decouples both freshness layers): the `headPushedAt` signal above is derived solely from the issue→PR cross-reference timeline (`timelineItems(CROSS_REFERENCED_EVENT)`), which has been observed to be transiently suppressed — edits, Actions-bot-authored PRs, and certain merge-queue interactions, the same brittleness `close_linked_pr` works around via `_linked_prs_by_branch_name` (issue #2552 / PR #2568). When it is empty for an issue, `headPushedAt` is null and **both** freshness guards — this fresh-push guard (Layer 1) and the `ai:done` re-anchor (Layer 2 below) — fail open in lock-step, which previously produced a false-positive `retrigger_review` against a PR that had just been pushed. To decouple the guards from that single source, `_check_fresh_push_guard_with_fallback` (used by both stall loops) re-resolves the linked PR by its deterministic `ai/issue-<n>` head branch — one `gh pr list --head … --json number,commits` call via the shared `_resolve_linked_pr_fresh_by_branch` helper — and re-checks freshness from the head commit's `committedDate` before allowing a recovery to dispatch. It fires only for `ai:done` / `ai:ready-to-merge` issues whose primary cross-ref entry lacked a usable `headPushedAt`, so the steady-state path adds zero API calls (§15). On a fallback hit the `STALL_SKIP … reason=fresh_push …` line carries an extra trailing ` source=branch_fallback` field (the base prefix is byte-for-byte unchanged); every fallback attempt also emits a non-silenced `STALL_FRESH_PUSH_FALLBACK issue=<n> phase=<phase> source=branch_name resolved=<iso|none>` line so a recurrence is traceable (the primary cross-ref fetch swallows its own failures via `2>/dev/null`).

Log prefix `STALL_SKIP issue=... reason=fresh_push pr=... pushed_age_secs=... phase=... action=...` is a public contract (CLAUDE.md §6 Naming Immutability) — downstream log analysis and dashboards pivot on it; renames require the alongside-old-name shim documented in §6. The `source=branch_fallback` field is appended only on the branch-fallback path and is additive (it never alters or removes the existing fields), preserving that contract.

### Stall recovery: ai:done clock re-anchor

`detect_stalls` (scripts/orchestrate_lib.py) normally measures stall duration as `now_ts - status_since_ts`, where `status_since_ts` only advances when the phase label changes. During a multi-cycle `review_autofix` loop the phase stays `ai:done` even though commits, editor pushes, and reviewer runs land every 35-45 minutes, so `status_since_ts`-only elapsed grows monotonically past `STALL_THRESHOLD_DONE_MINUTES` (120 min default) and the stall detector fires every cycle. The downstream guards (active-workflow guard, fresh-push guard, in-flight review guard at the empty-commit push site) catch the false-positive action, but the per-cycle detection still incurs guard-ladder work and log noise.

For phase `ai:done` only, the effective stall anchor is `max(status_since_ts, headPushedAt_epoch)`. `headPushedAt` is the linked PR's last push time, already pulled into the per-tick `_current_wave_details_json` GraphQL prefetch via `_fetch_candidate_issue_details_graphql` (zero extra API calls per CLAUDE.md §15). When a fresh push landed within the stall threshold, the effective elapsed drops back below the threshold and the issue is no longer flagged as stalled, eliminating the per-cycle pseudo-stall while the autofix loop is converging.

Scope is intentionally narrow — only `ai:done` is re-anchored. Other phases retain their existing `status_since_ts`-only semantics. The mapping is plumbed through the CLI as `--head-pushed-at-json` (a JSON object `{"<issue_num>": "<ISO 8601 timestamp>", ...}`).

Layer-2 branch fallback: before invoking the detector, the bash prefetch step re-resolves the `ai/issue-<n>` head branch (via `_resolve_linked_pr_fresh_by_branch`, the same helper the Layer-1 fresh-push guard uses) for any `ai:done` wave issue whose cross-reference produced no usable `headPushedAt` (missing, empty, or unparseable), and merges the resolved head-commit `committedDate` into the `--head-pushed-at-json` map. This keeps the re-anchor from being blinded in lock-step with the fresh-push guard when the shared cross-reference source is transiently empty or malformed while preserving the intentionally narrow `ai:done`-only scope (see "Cross-reference fallback" above). It is bounded — it fires only for those `ai:done` issues with no usable primary timestamp, normally zero — and an always-visible `STALL_REANCHOR_FALLBACK issue=<n> source=branch_name resolved=<iso>` line records each enrichment (always-visible rather than `::debug::` because a successfully re-anchored issue is no longer flagged, so the Layer-1 `STALL_FRESH_PUSH_FALLBACK` line never fires for it).

Fail-open: when `headPushedAt` is missing, null, the empty string, or unparseable (and the branch fallback also resolves nothing), `detect_stalls` falls back to the legacy `status_since_ts`-only behaviour. Clock-skewed future timestamps are clamped at `now_ts` so a forward-drifting headPushedAt cannot make an issue appear perpetually fresh — the worst case is treated as fresh until wall-clock time reaches the future timestamp, after which stall detection resumes. The bash prefetch step fails open on any jq error and passes `{}`, so a GraphQL outage degrades cleanly to the legacy detector.

### Stall recovery: merge-conflict pre-dispatch override

The standalone stall loop (`run_standalone_stall_recovery`) reroutes the `retrigger_review` recovery action to the conflict resolver (`_dispatch_review_for_conflicts`) whenever the latest linked PR is known to be in a merge-conflict state. Without this override, the retrigger path pushes an empty commit to the PR head branch to re-kick Review Autofix — but autofix operates on the branch as-is and cannot resolve a merge conflict with base, so the next stall cycle repeats the same no-op dispatch until `MAX_STALL_RECOVERIES_PER_ISSUE` is reached.

Detection (`_check_open_pr_conflict_guard`) fires when the cached linked-PR entry shows `state=OPEN` AND (`mergeable ∈ {CONFLICTING,false}` OR `mergeStateStatus/mergeable_state == DIRTY`) — matching the same signal the rebase-bot already uses. Primary data source is `_candidate_details_json`, extended in `_fetch_candidate_issue_details_graphql` to include `headRefName`, `mergeable`, and `mergeStateStatus` on the cross-referenced PR node (zero additional API calls on cache hit). When the cache is missing **or** returns `UNKNOWN` mergeability (GitHub computes mergeability asynchronously — a push kicks off a background job and the API briefly returns `mergeable=null`/`mergeable_state=unknown` per GitHub REST docs), the guard falls back to a REST `GET /pulls/{n}` retry loop of up to **5 attempts** with sleeps **5 s → 10 s → 15 s → 20 s** between retries (50 s worst case per conflicting-unknown PR). The first request kicks off GitHub's recomputation; subsequent retries typically return the definitive state. The loop breaks early when state is settled: either `mergeStateStatus/mergeable_state == DIRTY` (conflict already known even if `mergeable` is still unknown) **or** `mergeable ∈ {true,false}` with `mergeable_state ≠ unknown`. On all-attempts-still-unknown the guard fails open and the legacy retrigger_review dispatch runs. API hygiene (CLAUDE.md §15): the retry loop's final PR JSON is stashed in an iteration-local cache (`_STD_ITER_PR_JSON_CACHED`) so the legacy retrigger_review case reuses it instead of issuing a redundant `gh api` fetch for the same PR in the fail-open path.

On a hit the poller logs `STALL_RECOVERY issue=<n> reason=open_pr_merge_conflict pr=<p> phase=<phase> action=dispatch_conflict_resolver override_from=retrigger_review`, emits a Telegram WARNING, and `continue`s the loop. The `stall_recovery_count` counter is **not** incremented — conflict resolution has its own budget and does not consume the retrigger-style recovery allowance. Duplicate same-cycle dispatches are suppressed via `_CONFLICT_DISPATCH_TRACKER` (return code 2 → `STALL_SKIP reason=open_pr_merge_conflict_dispatch_skipped`); dispatch failures (rc≠0 and ≠2) log `STALL_RECOVERY reason=open_pr_merge_conflict_dispatch_failed` and skip this cycle without burning the counter.

A belt-and-braces check is also wired into `execute_stall_recovery_action retrigger_review`: if the pre-dispatch guard was bypassed (cache empty, managed-path entry, etc.) the action-level check fetches the PR JSON once (reusing the head_ref lookup), detects the conflict, and recursively dispatches `resolve_merge_conflict` with `STALL_RECOVERY_SHOULD_INCREMENT` forced to `false` so the override remains budget-neutral.

Fail-open: when neither cache nor REST fallback can confirm a conflict state, the legacy `retrigger_review` empty-commit push runs as before. The guard can never cause an action that otherwise would not have fired; it only redirects `retrigger_review` → `resolve_merge_conflict` within the conflict window.

A second belt-and-braces check in `execute_stall_recovery_action retrigger_review` covers the **failed-autofix** case: after the merge-conflict override runs (and the PR is known to be mergeable), the action queries `gh run list --workflow <wf> --branch <head_ref> --limit 1` for each of `ai-review.yml`, `internal-review.yml`, `review_autofix.yml`, and if the most recent completed run concluded `failure`, `cancelled`, or `timed_out`, it calls `_dispatch_review_for_conflicts` directly instead of pushing an empty commit. The existing cycle-local `_CONFLICT_DISPATCH_TRACKER` and `_has_active_autofix_run` guards inside that helper prevent duplicate dispatch. On dispatch success (rc=0), the action emits `STALL_RECOVERY_EFFECTIVE_ACTION=redispatch_review_autofix`, consumes one recovery attempt (`STALL_RECOVERY_SHOULD_INCREMENT=true`), and returns 0. On already-dispatched-this-cycle/active (rc=2), it emits the same effective action but returns without incrementing the recovery counter. On dispatch failure (rc=1) the legacy empty-commit push runs as the fallback. Rationale: a failed Review-Autofix run leaves the PR with no in-flight worker and no review verdict, and an empty-commit push does not re-dispatch the workflow on its own (only `pull_request.synchronize` on a real code delta does), so the PR would otherwise sit until `MAX_STALL_RECOVERIES_PER_ISSUE` is exhausted.

A third guard at the same empty-commit push site protects an **in-flight** review pass. Before pushing, both push sites (`execute_stall_recovery_action` and `run_standalone_stall_recovery`) scan the per-tick `_load_actions_runs_cached` blob for a fresh `in_progress`/`queued` `AI Review` / `Internal Review` / `Review Autofix` run on the head branch and skip the push if one is found (`STALL_RECOVERY_EFFECTIVE_ACTION=retrigger_review_skipped_inflight`). Because that blob is the same source `build_active_issue_set` consumes — and can miss a live run (cache TTL/304-reuse, the per-status 50-item listing window, pagination, or `head_branch=null` on `workflow_dispatch`) — a false negative there would let the empty commit advance the branch under a still-editing `review_autofix` run, tripping its `AUTOFIX_PRE_EDITOR_STALE_BASE → soft_exit` and discarding a full review pass. To close that gap, when the cached scan finds nothing the guard issues a single authoritative branch-scoped confirmation via `_direct_inflight_review_run_on_branch` — one `gh run list --branch <head_ref> --json …` call (server-side `--branch` filter, so it is not subject to the global 50-item listing cap the cached blob is) — and skips the push if a fresh matching run surfaces (logged `direct check — cached scan missed it`). Bounded per CLAUDE.md §15: it fires only on the cache-miss path immediately before the destructive push (the fail-open cache-miss fallback §15 sanctions), never on the steady-state path where the cached scan already finds the run; freshness mirrors the cached scan's review-run window (`REVIEW_RUN_MAX_RUNTIME_MINUTES`, default 250 — a review_autofix run legitimately runs past `STALL_THRESHOLD_MINUTES` up to the codex-agent job's 240-min timeout, so the narrower stall window misclassified a still-editing run as a zombie and clobbered it, the PR #3082 / issue #3081 "stuck 169m, attempt 2" loop) so a genuinely hung run still does not block recovery forever. Limitation: `gh run list --json` exposes `workflowName`/`name` but not the workflow file path, so a consumer that renamed the review workflow's display name is matched only if `workflowName` still resolves; on a miss the guard fails open (push proceeds) — no worse than the pre-fix behaviour. Any `gh`/`jq`/`date` error also fails open.

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

**Pathspec hard-fail escalation (`.codex-workflow-src*`).** The "Handle no-op implementation" step in `.github/workflows/implement.yml` inspects the `remaining_changes` output from the commit step. The setup steps clone the runtime support checkout (`.codex-workflow-src` / `.codex-workflow-src-main`) into the worktree as untracked, gitignore-free directories, so a raw outer-repo `git status --porcelain` always lists them as `?? .codex-workflow-src/` / `?? .codex-workflow-src-main/` on every consumer-repo run (in this repo those paths are gitignored and never appear, which is why the false-positive only ever bit consumer repos). Those bare untracked entries are stripped from `remaining_changes` at the source — whole-line `grep -vxE '\?\? \.codex-workflow-src(-main)?/?'` — before this guard runs, so a genuine no-op (including one where Codex edited a file then reverted it) no longer false-escalates. Because those support-source dirs are themselves nested Git checkouts, the outer repo's porcelain never descends into them; if Codex dirties one, the workflow re-queries `git -C .codex-workflow-src status --porcelain` / `.codex-workflow-src-main` and prefixes the inner paths back into `remaining_changes` (for example ` M .codex-workflow-src/scripts/foo.sh`). Guard 1 uses the directory-only matcher `\.codex-workflow-src(-main)?(/|$)`, so it still fires when Codex actually wrote into the runtime-fetched checkout and the commit pathspec exclusions (around `add_u_excludes` / `add_o_excludes` in the same workflow) silently stripped those edits, without over-matching similarly prefixed top-level files such as `.codex-workflow-src-notes.md`. Treating that as a normal no-op produced an infinite re-issue loop — observed in tracking issue #1292 for `local_id=validation-render-self-heal` (30+ duplicate sub-issues in ~5 hours). When it fires, the step:

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

**General tracked alerts (deleted at successful terminal state only):**
- Orchestrator-managed issue alerts use general tracking (`<!-- tg_cleanup:id1,id2,... -->`), cleaned up only when the tracking issue reaches a **successful** terminal state (project complete / validated and merged) via the poller. Failure / blocked terminal states (integration branch missing, validation failed deterministically, validation recovery exhausted, final-merge budget exhausted, judge stall-cycle limit exceeded, judge repeat-fingerprint breaker, recovery attempts exhausted) deliberately leave the tracked alert history intact so the alert chat retains the breadcrumb trail needed to diagnose the underlying issue.
- Any remaining tracked messages (general or phase) are cleaned up by `issue_pr_status.yml` only when a PR is **merged**. PRs closed without merging (abandoned / failed) deliberately preserve their tracked alerts for post-mortem.

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
- A fetch the poller could not confirm — the `gh api` actions-runs call failed (auth/transient) or returned an unparseable body — also fails open (to the stale cache if present, else an empty blob) and emits `::warning::rate_limit_audit_fallback helper=_load_actions_runs_cached reason=fetch_unconfirmed repo=<owner/repo> api_rc=<code> status='<http-status|none>' cache_hit=<bool> err='<first-stderr-line>'`. A genuine-empty **success** (a confirmed fetch with zero in-flight runs) is silent, so the presence of this line distinguishes "the fetch never succeeded" from a real `total=0` — the ambiguity that otherwise makes an `Active issue set is empty (... total=0 ...)` poll unattributable (perms vs poisoned/empty TTL cache vs transient). `api_rc` is captured with `cmd || api_rc=$?` so it reflects the real fetch exit code.

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
3. Clear `ai:harness-broken` if it is present and transition the tracking label from `ai:validation-failed` to `ai:validating`.
4. Refresh the draft integration PR's managed `<!-- VALIDATION_STATUS_V1 -->` section to show that revalidation is in progress.
5. Record a `revalidate_event.v1` audit entry in AI memory for the current actor + integration SHA.
6. Dispatch a fresh validation run (cycle 1).

Rapid repeats are deduplicated for 5 minutes per actor + integration SHA: if the same user posts `/revalidate` again against the same integration commit inside that window, the poller leaves state unchanged and replies that the earlier reset was already processed. This is useful after fixing the root cause manually (e.g. correcting a Docker config, adding a missing env var, or updating a dependency). There is no limit on how many times `/revalidate` can be used — the operator decides when to stop retrying.

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
| `ORCH_INTEGRATION_STALE_ALERT_HOURS` | `0` | When the integration branch remains ahead of default for at least this many hours since the last successful squash, the poller emits `INTEGRATION_STALE_ALERT_SENT` and sends a `WARNING` alert. **Set to `0` to disable** — the reusable workflow `.github/workflows/orchestrate_poll.yml` now passes `0` by default (the alert otherwise fires for the entire lifetime of every multi-wave project, since `main` only catches up at the single end-of-project squash). Set the repo variable to a positive integer such as `6` to re-enable. The script fallback remains `6` for direct callers that omit or mis-set the env. Must be a non-negative integer; invalid values fall back to `6`. |
| `ORCH_INTEGRATION_STALE_REALERT_HOURS` | `12` | Minimum hours between repeated stale-integration alerts while the branch is still ahead. The dedupe window clears automatically after a successful squash / when `ahead_by=0`. Must be a positive integer; invalid values fall back to `12`. |
| `ORCH_INTEGRATION_MAX_AHEAD_COMMITS` | `10` | Backpressure **floor**; the effective threshold is `max(this, planned_issue_count + ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN)` so a project's own planned merges never self-deadlock. Activates `ai:integration-backpressure`, pauses additional sub-issue merges, and auto-clears once `ahead_by` drops below the effective threshold. Must be a positive integer; invalid values fall back to `10`. |
| `ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN` | `20` | Headroom added to a project's planned sub-issue count when deriving the size-aware backpressure threshold (absorbs `main`→integration sync-merges, judge conflict-merges, and judge-added fix-up issues). Raised from `5` to `20` after project #2974 self-deadlocked at 22 commits ahead. Must be a non-negative integer; the `orchestrate_poll` workflow sets `20` and invalid values fall back to the script-level `5`. |
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
- Renderer-supported template families are currently `python-mongo-flask`, `node-hardhat-solidity`, `node-runtime`, `python-repo-checks`, and `python-mongo-repo-checks`.
- Use `node-runtime` for generic Node/npm repositories that should run repo-local checks inside a single app container; use `node-hardhat-solidity` only when validation needs Hardhat/Foundry/Anvil/RPC-specific probes and shutdown helpers.
- `.ai/validate.yml` must set `type` explicitly. The renderer does not auto-detect a family from `entry`, `package.json`, or repository contents.
- For `node-runtime`, `custom_tests` and `skip_tests` are array fields of shell-command strings in `.ai/validate.yml`; they render into `CUSTOM_TESTS_JSON` / `SKIP_TESTS_JSON` for the repo-check runner.
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
  - Uses `OPENROUTER_API_KEY` for the codex-driven discovery dispatch below. The workflow writes `~/.codex/config.toml` via `scripts/write_codex_config.sh` (provider `openrouter`, `env_key=OPENROUTER_API_KEY`); without this secret `codex exec` exits non-zero and discovery degrades to drift-monitoring only. `GH_PAT` alone is not sufficient — it authenticates GitHub git/PR operations, not the model provider.
- Workflow: [`.github/workflows/validation-refresh.yml`](.github/workflows/validation-refresh.yml)
- Triggers:
  - Daily cron (`17 2 * * *`)
  - Manual dispatch (`workflow_dispatch`) with optional `repos_file` and `branch_name` inputs
- Runtime:
  - Reads target repositories from `.github/ai/consumer_repos.json`
  - For each target repo, clones the repo into a temporary workspace and checks out the `ai/validation-refresh` branch locally (the local branch is never pushed back to the remote). If `.ai/validate.yml` is missing, refresh bootstraps it from `examples/validation-fixtures/python-repo-checks.yml` and ensures executable `scripts/run_validation_repo_checks.sh` exists (diagnostics include `manifest_bootstrapped_from`, `repo_check_entry_seeded`, and `repo_check_entry_preserved_existing`). It then renders validation assets from `.ai/validate.yml` using `scripts/render_validation_templates.py`, runs deterministic lint (`scripts/validation_lint.py`) and deterministic self-test (`scripts/validate_driver.sh`).
- Drift reporting only — no PRs:
  - This workflow does NOT commit, push, or open pull requests in consumer repos. Consumers are expected to render validation assets on demand inside their own validation flow, so there is no need to ship a `chore(validation): refresh validation assets` PR ahead of time.
  - When the rendered output differs from what is checked into the consumer repo, the result includes a `validation_assets_drifted_no_push` diagnostic. The outcome is `green` when render/lint/self-test all pass and `red` when any stage fails — a consumer-side pipeline failure is `red` whether or not the assets drifted.
- Failure/no-op behavior:
  - Manifest-less repos are bootstrapped in the temp clone (not skipped), but the seeded `.ai/validate.yml` and `scripts/run_validation_repo_checks.sh` are onboarding stubs. Repo owners still need to replace placeholder checks/values with real repository-specific validation logic and commit them in their own repo.
  - Pipeline failure with no file diff: records `red` with the `pipeline_failed_without_changes` diagnostic. This is a consumer-side pipeline failure (render, lint, or self-test) with no asset drift (same severity class as a drift-present `red`), so it is monitored but does NOT fail the runner. Only genuine refresh-mechanism failures (clone/checkout/manifest bootstrap/unexpected exceptions) record `error`, which fails the runner and gates the release smoke (`orphan-workflows-test`).
  - Workflow writes machine-readable summary JSON, appends a human summary to `$GITHUB_STEP_SUMMARY`, and sends Telegram failure notification (`TG_BOT_SECRET` + `TG_ADMIN_CHAT_ID`) on workflow failure.

#### Codex-driven `.ai/validate.yml` discovery dispatch

The refresh runner ALSO runs codex-driven discovery against each consumer's clone (script: [`scripts/validation_discovery_bootstrap.py`](scripts/validation_discovery_bootstrap.py)). Discovery is layered on top of the drift-monitoring pipeline above and DOES open PRs on consumer repos when the discovered `.ai/validate.yml` is either (a) absent on the consumer's default branch, or (b) present but with a different `type` from what codex would propose.

- **Trigger:** automatic on the same daily cron / `workflow_dispatch`. Set `VALIDATION_DISCOVERY_ENABLED=false` (or the `discovery_enabled` workflow input) to skip discovery for that run while preserving drift monitoring.
- **Codex model:** `openai/gpt-5.4` at reasoning effort `xhigh` (overridable via repo `vars` `VALIDATION_DISCOVERY_MODEL` / `VALIDATION_DISCOVERY_REASONING_EFFORT`). Reads the entire consumer tree via `codex exec --sandbox danger-full-access` and emits YAML against [`scripts/templates/slot_manifest.schema.json`](scripts/templates/slot_manifest.schema.json).
- **Outcome routing:**
  - Manifest missing → opens a "seed" PR titled `chore(validation): seed .ai/validate.yml (type: <family>)` with the discovered manifest + onboarding entry script.
  - Manifest present and `type` matches discovery → no PR opened, `outcome=success_agree` recorded.
  - Manifest present and `type` mismatches discovery → opens a "discovery disagrees" PR titled `chore(validation): discovery proposes type change (X → Y)`.
  - Codex exhausts retries → `outcome=failed` recorded, no PR opened.
  - `git push` denied (PAT scope missing) → `outcome=push_denied` recorded, no PR opened.
- **Per-PR idempotency:** branch name `automation/validate-discovery/<short_sha>/<type>` is deterministic; an existing open PR on that branch is reused, never churned. When the consumer's default-branch HEAD advances, a fresh branch with the new SHA is computed and a new PR is opened.
- **Cross-cycle dedup:** discovery outcomes are recorded on the `ai-memory` branch under [`ai-memory/schemas/validation_discovery.v1.json`](ai-memory/schemas/validation_discovery.v1.json) (path: `orchestrator/validation_discovery/<owner>__<repo>/history.json`). A consumer with a `success_*` entry within `VALIDATION_DISCOVERY_DEDUP_DAYS` (default `7`) is skipped on the next cycle; failures do NOT block re-attempts.
- **Operator opt-out:** set `discovery_enabled=false` on `workflow_dispatch` to skip an individual run; set `VALIDATION_DISCOVERY_ENABLED=false` as a repo `var` to disable the feature globally. `discovery_dry_run=true` exercises the dedup + memory plumbing without invoking codex or opening PRs.
- **Time budget (timeout protection):** the codex discovery phase is bounded by `VALIDATION_DISCOVERY_BUDGET_SECS` (default `2100`, ~35m). Consumers are processed sequentially and the runner stops invoking codex once the remaining budget can no longer cover one consumer's worst case (`VALIDATION_DISCOVERY_MAX_ATTEMPTS × 300s`); remaining consumers record `skipped_budget` and fall through to drift monitoring only. This keeps the job within its 60-minute `timeout-minutes` cap regardless of consumer count or per-call codex latency — failed/skipped outcomes do not dedup, so they are retried on the next cycle.
- **Required PAT scopes:** `GH_PAT` must have `repo` scope on every consumer in [`.github/ai/consumer_repos.json`](.github/ai/consumer_repos.json) (same scope already required for `mark-stable.sh` dispatch).

| Env var | Default | Description |
|---|---|---|
| `VALIDATION_DISCOVERY_ENABLED` | `true` | Master gate. `false` skips the discovery dispatch entirely (drift monitoring still runs). |
| `VALIDATION_DISCOVERY_DEDUP_DAYS` | `7` | Days to skip a consumer after a successful discovery outcome is recorded on `ai-memory`. |
| `VALIDATION_DISCOVERY_MAX_ATTEMPTS` | `3` | Codex invocation retries before giving up on a consumer. |
| `VALIDATION_DISCOVERY_BUDGET_SECS` | `2100` | Aggregate wall-clock budget (seconds) for the codex discovery phase across all consumers. Once the remaining budget can no longer cover one consumer's worst case (`MAX_ATTEMPTS × 300s`), the rest record `skipped_budget` and fall through to drift monitoring only, keeping the job under its 60-minute `timeout-minutes` cap. A non-positive value disables the gate (unbounded). |
| `VALIDATION_DISCOVERY_MODEL` | `openai/gpt-5.4` | Codex model id. |
| `VALIDATION_DISCOVERY_REASONING_EFFORT` | `xhigh` | Codex reasoning effort. |
| `VALIDATION_DISCOVERY_PR_BRANCH_PREFIX` | `automation/validate-discovery` | Branch prefix for proposed PRs. |
| `VALIDATION_DISCOVERY_PR_LABEL` | `automation:validate-bootstrap` | Optional label applied to created PRs (best-effort; falls back to label-less when the consumer repo doesn't have it). |
| `VALIDATION_DISCOVERY_DRY_RUN` | `false` | Exercise the dedup + memory write paths without invoking codex or opening PRs. |

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

> **Semble rollout note.** `workflow-templates/*.yml` remain thin caller wrappers. The opt-in `SEMBLE_ENABLED` gate plus the Semble install/index steps live in the reusable workflows under `.github/workflows/` (`clarify`, `plan`, `implement`, `orchestrate`, `orchestrate_poll`, `orchestrate_clarify_respond`, `review_autofix`, `validate`, and `workflow-log-analysis`). Consumer repos pick up those reusable-workflow changes only after a new `@stable` tag is cut; merging changes on `main` here does not update already-installed consumer wrappers by itself. `workflow-log-analysis.yml`'s three Codex passes (analyze-commit-notify, deep-audit, api-redundancy) each install Semble, build an index of the repo state they will analyze, and pass a `{{SEMBLE_PREFETCH}}` block into the rendered prompt via `scripts/render_prompt.sh`; misses are fail-soft (empty prefetch → blank placeholder).

## Contributing

1. Make changes in a feature branch
2. Test via canary channel on pilot repos
3. Promote to stable after validation

See `docs/release-policy.md` for the full release process.
