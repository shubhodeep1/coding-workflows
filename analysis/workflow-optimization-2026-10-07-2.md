## Executive Summary

- **Review conflict resolution is the main failure cluster.** In `shubhodeep1/coding-workflows`, 10 of 12 failed `review_autofix` runs stopped at `review / codex-agent → Run Codex resolver, validate, stage, commit`; two stopped at checkout. Classifying resolver exits is the highest-priority reliability action. **Potential impact:** up to 10 fewer failed review runs in a comparable 406-run window *if* the underlying causes are corrected; this is a ceiling, not a forecast. **Confidence:** high on failure location, low on cause.
- **Runner contention adds minutes to some reviews.** Run `37579333799` waited about six minutes for `ubuntu-latest`; runs `37579327585` and `37579318834` waited about 353 and 335 seconds. Staggering eligible review dispatches could recover roughly **5–6 minutes on similarly contended runs**, provided it does not merely move the wait earlier. **Confidence:** medium.
- **Observed AI usage is concentrated, but not a whole-window cost estimate.** Three slow review runs—`37579201840`, `37569249887`, and `37567073410`—account for **108.43M of 142.78M reported OpenRouter tokens**. Usage is drawn from log-covered runs, not all 1,000 runs. **Impact:** high potential for targeted investigation; dollar savings unmeasured. **Confidence:** high on recorded totals.
- **Triage has a separate posting failure.** All three unskipped `check_failure_triage` runs failed at `Post check-failure triage issue` (`37565255965`, `37565998804`, `37566321572`), after a combined 6,078 recorded Codex tokens. Classify the posting failure before changing diagnosis behavior. **Potential impact:** up to three recovered triage outcomes in this window. **Confidence:** high on location, low on cause.
- **The evidence gap is material.** The supplied `/home/runner/work/_temp/workflow-log-output/summary.json` and deep-dive directory are absent from this environment. The assembled context covers outcomes but omits failure stderr, per-target MCP rows, attributable API-call counts, and memory-retrieval metrics. Recommendations below remain bounded to the supplied telemetry and inspected repository code. **Confidence:** high.

## Speed Optimizations

Ranked by **measured latency opportunity**, not guaranteed savings:

1. **Reduce review runner queueing — critical path.** `review / codex-agent` waited approximately 335–360 seconds in runs `37579318834`, `37579327585`, and `37579333799`. **Root cause:** reported hosted-runner contention. **Change:** pace eligible sweep/dispatch bursts using existing workflow controls, while preserving per-PR freshness and merge-train guards; record dispatch-to-runner-start time by job. **Estimated saving:** up to 5–6 minutes on affected runs *if capacity becomes available sooner*; otherwise the saving is zero. **Risk:** medium—over-throttling can delay reviews.
2. **Measure review-agent compute before changing its gates — critical path.** In successful reviews `37583944217` and `37579297567`, `codex-agent` occupied about 328/350 and 491/519 seconds respectively. The checkout step already checks source and PR-head consistency, so another unchecked skip would risk missing a review. **Change:** emit a stage-duration summary for reviewer, editor, resolver, and validation, keyed to the checked-out head SHA; test any same-head no-op shortcut only when existing gates prove the required review is complete. **Estimated saving:** not identifiable without stage timings and head equivalence; immediate logging saves 0 seconds. **Risk:** low for logging, high for an unproven shortcut.
3. **Shorten branch-to-PR discovery when trusted metadata exists — critical path for that run.** `resolve-claude-branch-pr / Resolve PR for head branch` spent about 60 of 68 seconds in run `37582388582`. **Change:** where the dispatch already carries a PR number, use it only after checking its head against the branch; retain discovery as fallback. Log lookup attempts and wait time. **Estimated saving:** up to 60 seconds on an equivalent metadata-backed run. **Risk:** low if the head check remains mandatory.
4. **Explain merge-train scan time before reducing it — cleanup-path optimization.** `cancel-active-runs / Release merge-queued PRs` took about 42/52 seconds in `37582002860` and 31/47 seconds in `37584658439`; each examined 38 queued PRs and released none. The logged `cap=20` limits *older PRs checked per candidate*, not the 38-candidate release scan. **Change:** add reason counts and elapsed milliseconds to the existing release summary, then optimize only the measured dominant reason. `scripts/review_merge_train.sh` already caches file lists per distinct PR and reads active-run status lazily; retain both safeguards. **Estimated saving:** 0–42 seconds on comparable cleanup runs, depending on the diagnosed work; not yet a demonstrated review critical-path gain. **Risk:** low for logging.
5. **Reuse installation artifacts only where the version matches — micro-optimization.** Codex installation took 4,940 ms of a 15-second `workflow_failure_heal_intake` run (`37580496877`). **Change:** test reuse of existing version-keyed workflow cache entries before adding new setup steps. **Estimated saving:** at most about five seconds on similar intake runs. **Risk:** low with version and integrity checks.

## Cost Optimizations

1. **Diagnose expensive failed or repeated reviews first.** Failed resolver run `37567464163` recorded 12 OpenRouter calls and **10.65M total tokens**; slow runs `37579201840` and `37569249887` recorded 53.22M/30 calls and 29.65M/16 calls. **Inference:** correcting a repeatable terminal resolver failure could avoid expensive unsuccessful attempts, but the logs supplied do not establish why these attempts failed or whether same-PR runs used the same head. **Change:** record head SHA, resolver attempt number, terminal reason, model, reasoning setting, and token categories per attempt; act on a demonstrated repeat pattern. **Estimated saving:** up to the tokens consumed by an actually preventable failed attempt—not all tokens on these runs. **Quality risk:** do not suppress a required fresh review.
2. **Constrain demonstrated prompt growth, not cached context indiscriminately.** Review run `37579333799` recorded **four `CONTEXT_BUDGET_WARN` events**, 2.875M prompt tokens, and 26 calls. **Change:** log prompt-component bytes and warning phase/model/ratio; remove repeated nonessential material only after comparing review outcomes. As a *scenario*, removing 10% of the window’s 18.02M recorded prompt tokens would save 1.80M prompt tokens; redundancy has **not** been measured. **Quality risk:** medium for pruning evidence-bearing context.
3. **Assess Semble by net context replaced.** `review_autofix` logged **36 `SEMBLE_QUERY` calls, 294,789 bytes**—about 8,189 logged bytes/query—and zero fallbacks; failed run `37565153077` alone logged six queries and 37,424 bytes. Query bytes show context added, **not** whether Semble prevented larger prompt expansion. **Change:** alongside each query’s existing `target`, `bytes`, and `ms`, record selected-chunk count, bytes inserted, and the legacy-context bytes actually omitted. Trim low-value results only after that comparison. **Estimated token/dollar saving:** unknown. **Quality risk:** medium.
4. **Keep model and reasoning changes behind a quality comparison.** Recent review summaries show `openai/gpt-6-luna` configured for materiality/summarization (`37583942151`) and `openai/gpt-6-sol` for consolidation (`37579321616`); configuration is not proof of per-call usage or price. The conflict resolver defaults to `high` reasoning in `review_autofix.yml`. **Change:** collect actual model, reasoning, tokens, and outcome by phase; compare a cheaper setting first on non-decision-making summarization, not conflict resolution or the final judge. **Estimated dollar saving:** unavailable without prices and per-model usage. **Quality risk:** high for blanket downgrades.
5. **Do not count Serena as a saving yet.** Sampled reviews report `SERENA_ENABLED: false` (for example `37583956662`); aggregate Serena queries, tool calls, and response bytes are zero. There is no evidence here that Serena replaced downstream tool/model work—or added noisy response context. **Change:** leave rollout unchanged and measure per-tool downstream calls and bytes if enabled. **Estimated saving:** unknown.

## Reliability Improvements

1. **Classify resolver terminal failures — highest observed review failure count.** The 10 resolver-step failures include `37567444773` (1,981 seconds) and `37567464163` (1,441 seconds). The repository already bounds resolver attempts and detects some no-progress cases in `scripts/review_conflict_resolve.sh`; `run_attempt=1` does **not** rule out internal retries. **Root-cause category:** unknown within resolver execution, validation, or commit. **Exact fix:** emit one sanitized `RESOLVER_TERMINAL` record with `run_id`, attempt count, per-attempt milliseconds, exit class (`timeout`, `no_progress`, `markers`, `fingerprint`, `scope`, `commit`, or `other`), and validation counts; fix the leading observed class. **Expected impact:** potentially addresses the 10/12 review-failure cluster; no failure-rate reduction is claimable yet. **Rollback/fail-open:** keep existing attempt caps, intent guards, and judge escalation; logging failure must not change resolver outcome.
2. **Classify triage posting failures.** Three of 23 triage runs failed; the other 20 were skipped, so **all three unskipped runs failed** at the token-scoped posting step. **Root-cause category:** unknown—artifact/marker validation and issue creation occur in that step. **Exact fix:** log a final stage code (`body_missing`, `marker_invalid`, `metadata_invalid`, `create_failed`, `created`) plus redacted HTTP status class and `gh_retry` attempt count. Use the existing fingerprint when investigating ambiguous creates; do not add an unbounded lookup loop. **Expected impact:** isolates a potentially complete attempted-run failure mode. **Rollback/fail-open:** retain marker and token checks; never post an unvalidated body.
3. **Classify checkout failures without weakening head protection.** Runs `37576091530` and `37566576825` failed at `Checkout PR head branch` after 2,673 and 2,117 seconds. The step already retries transient fetches, so “missing retry” is not established. **Root-cause category:** unknown checkout substage. **Exact fix:** emit a sanitized terminal record with substage, SHA-match booleans, fetch attempts, elapsed milliseconds, and Git exit class; avoid branch text and credentials. **Expected impact:** two failures become actionable; savings depend on the diagnosed cause. **Rollback/fail-open:** preserve stale-head and workspace-integrity refusals.
4. **Expose successful-run degradations.** Head-gate status publication was skipped in successful reviews `37583954169`, `37583942151`, and `37583894670`; successful runs including `37583972044` reported a post-job Git-submodule warning. **Exact fix:** log helper-checkout/provenance result and status-publication outcome; separately log whether a working tree exists at cleanup. **Expected impact:** separates missing review status from harmless cleanup noise. **Rollback/fail-open:** retain current outcome semantics until the required-status contract is confirmed; do not disable checkout cleanup.

The four observed `CONTEXT_BUDGET_WARN` events are all attributed in supplied metrics to review run `37579333799`: they indicate **prompt-size risk**, not proven policy pressure. `BREAK_GLASS` count is zero in covered telemetry. Semble recorded **0 fallbacks/36 queries**; no masked broken rollout is evident among those queries, but targets are missing. Serena recorded **0 probes and 0 runtime fallbacks**; with disabled sampled runs, zero probes say nothing about availability.

## AI Memory Health

No `AI_MEMORY_TELEMETRY: retrieve` object is present in the supplied run excerpts or visible `log_summary` entries, and the deep-dive logs could not be opened. **Retrieve hit rate, average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, `fail_open`, `enabled: false`, and push-retry counts are all unavailable—not zero.** Repository memory helpers contain structured emission paths. Collect and expose operation counts and these fields by workflow/run, beginning with one covered review and one poller run; alert on emission missing from an otherwise memory-enabled run. This improves diagnosis without changing retrieval or fail-open behavior.

## GH API Call Audit

**Attributable endpoint call counts, rate-limit events, and internal retry counts were not supplied.** The repository’s `CLAUDE.md` §15 requires reuse, cycle-local caching, and batched GraphQL rather than new per-item lookups. Existing `GH_PAT_BUDGET phase=end` readings in `review_autofix.yml` are shared-quota deltas, **not attributable request counts**.

| Observed path | Evidence and safe action | Estimated call/rate-limit impact |
| --- | --- | --- |
| `cancel-active-runs / Release merge-queued PRs` | Runs `37582002860` and `37584658439`: 38 queued PRs examined, none released. The release code caches file lists per PR and reads active-run status once per invocation; preserve both. Add counts by `active`, `blocked`, `listing_incomplete`, `lookup_failed`, and `dispatch_failed`, plus endpoint-class calls/cache hits. | Unknown until calls are counted; avoids falsely treating 38 examinations as 38 redundant API reads. |
| `review / codex-agent` | Review is the only family with recorded OpenRouter and Semble usage, but API hotspots are absent from the assembled report. Surface existing job-end budget lines and count request attempts by endpoint class, pagination, retry sleep, and rate-limit response—without logging URLs with parameters or adding API reads. | Unknown; should reveal whether batching or reuse can reduce calls. |
| `triage / Post check-failure triage issue` | Three posting failures, no HTTP classification supplied. Add sanitized status class and retry outcome to the existing create call. | No immediate call reduction; diagnoses failed/ambiguous writes without speculative polling. |

Do **not** remove merge-train’s active-run double-check or replace its per-run cache with a cross-run stale cache to chase an unmeasured API saving. If instrumentation identifies another unbatched per-item *read*, extend an existing response or batch it under §15; the reduction is then measurable as **N reads to `ceil(N/batch_size)`**, not an observed saving in this window.

## Prompt Cache & Memory System

The aggregate `cache_hit_rate` is **null**: 22 of 111 OpenRouter calls have usage unavailable. Two fully reported review runs show **79.5748%** (`37567073410`) and **78.7996%** (`37567464163`) cache-hit rates. The covered aggregate records 119.75M cache-read and 4.02M cache-write tokens, but dividing these by aggregate tokens would produce an invalid global hit rate while usage is missing. `setup-uv`/`claude-cli` cache hits in run `37583951755` are installation caches, not prompt-cache hits.

**Change:** emit actual per-call usage availability and cache read/write by phase/model; retain stable instruction prefixes and place run-specific SHA, timestamps, and transient tool output after them where the prompt contract permits. Log prompt-component sizes before attributing misses to unstable prefixes or dynamic noise—neither cause is demonstrated here. Pair memory retrieval’s selected-record count and token budget with each prompt so zero-hit retrieval and context growth can be measured. **Impact:** token and latency improvement unknown until miss causes are measured; better coverage also prevents an apparent cache decline from being a telemetry artifact. Preserve existing cache/memory fail-open behavior.

## Orchestrator Health

`orchestrate_poll` completed **11/11 runs successfully** (p50 313 seconds, p95 361.5 seconds), but supplied rows do not report waves, deferrals, judge outcomes, or terminal-state ages. Clarify, plan, and implement were predominantly skipped—respectively **99/102, 96/99, and 97/100**—which is not itself evidence of stuck work. `workflow_failure_heal_intake` had eight cancellations among 54 runs; their reasons are absent.

The actionable stall signals are narrower: `cancel-active-runs` preserved **three active runs without PR linkage** in `37582002860` and `37584658439`, and the merge-train release examined 38 candidates without release. Keep the conservative preservation and queue guards. Add per-cycle counts for unlinked-run age, queued reason and age, wave transition, judge invocation/outcome, conflict-heal attempt, skip reason, and terminal disposition. Track these as rates and oldest-item ages; investigate a persistently rising age before changing orchestration rules.

## Pipeline Flow Bottlenecks

- **Clarify → plan → implement:** family p50s of one second are dominated by skips. Actual implementation can be long (`37570462072`: 1,589 seconds; `37568535049`: 1,512 seconds). Emit skip reason and active job-stage durations; do not use aggregate p50 to budget an active task.
- **Review/autofix → validation:** review is the largest observed active bottleneck (406 runs; p50 **487 seconds**, p95 **1,886.75 seconds**), with both runner queueing and agent compute demonstrated above. CI is also substantial (20/20 successful; p50 **1,115.5 seconds**, p95 **1,367.2 seconds**), but step-level CI evidence is absent. Capture queue, compute, and validation separately before changing tests.
- **Retry → merge/conflict overhead:** 10 review failures localize to the conflict resolver; the supplied `retries=0` is a *workflow-run* field, not its internal attempt count. The merge-train cleanup scan adds 31–42 seconds in two sampled runs, but zero releases do not prove a defect. Prioritize resolver terminal classification, then measure queue reasons and release outcomes.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review p50/p95 **487/1,886.75 seconds**; observed runner waits of about **335–360 seconds**; CI p50 **1,115.5 seconds**. **Top failure modes:** 10 resolver-step and two checkout-step review failures, plus three triage posting failures. **Highest recorded cost:** review/autofix’s **142.78M OpenRouter total tokens** in log-covered telemetry; three slow reviews contribute **108.43M**.

**Top three actions:** (1) add terminal-stage/attempt diagnostics to resolver, checkout, and triage posting without relaxing guards; (2) measure and pace review dispatch bursts against runner queue time; (3) expose per-call usage, per-target MCP, memory, and endpoint-class API telemetry before changing models, prompts, or API paths.

## Metrics Appendix

**Scope and outcomes.** One repository; 1,000 runs from the supplied GitHub Actions API context. “Other” comprises skipped outcomes in the supplied family counts. Durations include skips, so active-family comparisons need care.

| Family | Runs | Success | Failure | Cancelled | Skipped | p50 / p95 duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 1,000 | 523 | 15 | 10 | 452 | 15 / 1,084 s |
| `review_autofix` | 406 | 392 | 12 | 2 | 0 | 487 / 1,886.75 s |
| `ci` | 20 | 20 | 0 | 0 | 0 | 1,115.5 / 1,367.2 s |
| `check_failure_triage` | 23 | 0 | 3 | 0 | 20 | 1 / 200.4 s |
| `orchestrate_poll` | 11 | 11 | 0 | 0 | 0 | 313 / 361.5 s |

Overall success/failure rates are **52.3%/1.5% of all runs**; among the 538 success-or-failure runs, success is **97.2%**. Log telemetry covers **124/1,000** runs. These are not all-run AI-spend totals.

| Covered telemetry | Value |
| --- | ---: |
| OpenRouter calls; usage available / unavailable | 111; 89 / 22 |
| Prompt / completion / total tokens | 18,016,806 / 993,705 / 142,778,404 |
| Cache-read / cache-write tokens; aggregate `cache_hit_rate` | 119,753,310 / 4,017,963; **unavailable** |
| Recorded Codex calls / tokens | 3 / 6,078 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 4; all four warnings in review run `37579333799` |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 341,000 / 3,197,880 across 123 samples |

**MCP availability and bytes.** Targets and per-tool breakdowns were not included in the assembled metrics; zeros below are observed aggregate counts, not proof that a disabled system was available.

| Server / target in supplied data | Queries | Logged query or response bytes | Fallbacks | `probe_ok` | `probe_failed` | `probe_skipped` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Semble / target **not supplied** | 36 | 294,789 query bytes | 0 | n/a | n/a | n/a |
| Serena / no target observed; disabled in sampled reviews | 0 | 0 response bytes | 0 | 0 | 0 | 0 |

Serena per-tool calls/bytes: **none reported**. **Other MCP servers observed:** names/counts are not exposed in the supplied context, so absence cannot be verified. Semble’s measured fallback rate is **0/36**; probe-availability rates and target-specific rates are unavailable. GitHub API endpoint calls, rate-limit counts, internal retry counts, memory-retrieval metrics, and dollar costs are likewise **not supplied**. The next collection should make the named `summary.json` and targeted `errors/`, `slow/`, and `recent/` logs available, or carry their sanitized stage and target rollups into the assembled context.

## Deep Audit — Workflows & Scripts (2026-10-07)

### Section 1: Bug & Correctness Sweep

- **ID:** BUG-001 · **File path and lines:** `scripts/review_rb_judge.sh:1043-1058,1852-1859` · **Severity:** High · **Category:** `bug` · **Description:** Both merged-PR checks substitute `{}` when the API read fails. An unknown state therefore becomes “not merged,” allowing the judge to proceed past the guard for actions its own comments identify as unsafe on merged PRs. · **Recommended fix:** Require a valid PR state and merged-status response before authorizing `fix` or `close_and_reissue`; emit a skip reason and retain `ai:review-blocked` when either read is inconclusive.

- **ID:** BUG-002 · **File path and lines:** `scripts/orchestrate_poll_process.sh:23973-23991,24034-24059` · **Severity:** High · **Category:** `bug` · **Description:** The implementation-failure path turns failed title/body reads into empty strings, removes the failure label, and closes the source issue **before** confirming replacement creation. If creation fails, it logs a warning but leaves the source closed and the state pointing to it. · **Recommended fix:** Validate one complete source-issue snapshot, create and verify the numbered replacement first, then update state and close the source. On failure, leave the source open and eligible for retry.

- **ID:** BUG-003 · **File path and lines:** `scripts/orchestrate_poll_process.sh:22974-22991,23035-23044,23350-23359` · **Severity:** High · **Category:** `bug` · **Description:** These review-blocked merge paths check a fetched head SHA but call `gh pr merge` without `--match-head-commit`. A push between the check and merge can change the head being authorized; the reviewed-head binding already used by `scripts/review_rb_judge.sh:2028-2029` is absent here. · **Recommended fix:** Compare the current head with the judge-observed head, require a valid SHA, and pass that SHA with `--match-head-commit` to both auto and direct merge attempts.

- **ID:** BUG-004 · **File path and lines:** `.github/workflows/check_failure_triage.yml:476-479`; also `scripts/check_failure_triage.sh:664-669` and `scripts/workflow_failure_heal_intake.sh:1031-1038` · **Severity:** Medium · **Category:** `bug` · **Description:** Issue creation runs through `gh_retry`. If GitHub creates an issue but the response is lost, a retry can create another; the pre-create fingerprint lookup does not resolve that ambiguous outcome. This is a duplicate-side-effect risk, **not** an established cause of the triage posting failures already covered in the report. · **Recommended fix:** Use a single create attempt, then perform a bounded fingerprint lookup after an ambiguous failure before deciding whether to fail or retry. `scripts/security_audit.sh:2055-2058` already treats issue creation as non-idempotent.

- **ID:** BUG-005 · **File path and lines:** `scripts/workflow_retro_fanout.sh:304-329` · **Severity:** Medium · **Category:** `bug` · **Description:** When updating an existing week-marker comment fails, the function immediately posts a new comment. An update accepted by GitHub whose response was lost can therefore leave two comments for the same week. · **Recommended fix:** Re-read the existing comment after an ambiguous PATCH; if its body matches, accept success. Otherwise retry the idempotent PATCH, and reserve POST for the path with no existing marker.

- **ID:** SEC-001 · **File path and lines:** `scripts/gh_helpers.sh:738-749` · **Severity:** Medium · **Category:** `security` · **Description:** On a successful API command returning invalid JSON, `gh_api_json_to_file` prints the first 50 lines of the raw response to workflow logs. Its callers fetch issue and PR payloads, so malformed responses could expose their contents in logs. Whether a particular response contains confidential material is **[NEEDS VERIFICATION]**. · **Recommended fix:** Log response length, a digest, endpoint class, and failure classification—not raw response text. Keep the response in its temporary file for controlled diagnosis.

### Section 2: GitHub API Call Redundancy Audit

Counts below are logical reads before pagination and retries; they are **code-path estimates**, not measured request totals from the report.

- **ID:** API-001 · **File path and lines:** `scripts/orchestrate_poll_process.sh:23973-23975` · **Severity:** Medium · **Category:** `api-redundancy` · **Description:** Each reissued issue fetches the same issue endpoint separately for `.title` and `.body`: **2N reads → N reads** for N reissues. · **Recommended fix:** Fetch one validated JSON snapshot with `_safe_gh_jq`, extract both fields locally, and apply BUG-002’s no-mutation-on-missing-content guard. No GraphQL batching helper is needed for this duplicate within one item.

- **ID:** API-002 · **File path and lines:** `scripts/gh_helpers.sh:738-770,810-839` · **Severity:** Medium · **Category:** `api-redundancy` · **Description:** `gh_api_json_to_file` and `curl_gh_api` retry permanent failures through the default five-attempt budget: **up to 5 calls → 1 call** for a classified 404 or 422. Unlike `gh_retry`, neither path applies the existing permanent-failure check at that point. · **Recommended fix:** Reuse `_is_gh_permanent_failure` for captured `gh` stderr and classify `curl_gh_api` HTTP statuses before sleeping; retain exponential backoff for transient failures.

- **ID:** BATCH-001 · **File path and lines:** `scripts/orchestrate_poll_process.sh:28-46,86` · **Severity:** Medium · **Category:** `api-batching` · **Description:** Reclarify replay lists candidates, then reads each live issue and its complete comments separately: **2N + 3 baseline reads → `ceil(N/25) + 3`** if one aliased GraphQL request per 25 issues supplies both shapes. This excludes conditional writes and pagination fallbacks. GraphQL comment-history and author-field parity require **[NEEDS VERIFICATION]**. · **Recommended fix:** Extend the 25-item alias pattern of `_fetch_candidate_issue_details_graphql` with the fields and completeness indicator this trust check needs; retain the two REST reads for incomplete or missing aliases and make the final authorization against fresh data.

- **ID:** BATCH-002 · **File path and lines:** `scripts/orchestrate_poll_process.sh:23798-23824` · **Severity:** Medium · **Category:** `api-batching` · **Description:** The blocker loop fetches issue state once per blocker occurrence: **N reads → `ceil(U/25)`** for U distinct blockers prefetched at this decision point. Reuse across multiple failed issues is presently absent. Safe freshness across those decisions is **[NEEDS VERIFICATION]**. · **Recommended fix:** Extend `_fetch_issue_labels_batch_graphql` to return state, or use a narrower alias query modeled on `_fetch_candidate_issue_details_graphql`; cache by blocker number for this cycle and preserve the existing “unknown means defer” behavior on misses.

- **ID:** BATCH-003 · **File path and lines:** `scripts/review_collect_pr_metadata.sh:209-225,251-265` · **Severity:** Medium · **Category:** `api-batching` · **Description:** The normal PR-context path makes three REST reads—PR, issue comments, review comments—plus a linked-issues GraphQL read: **4 logical reads → potentially 1** complete GraphQL read. Optional top-level reviews add a fifth read. The existing `gh_pr_with_all_comments` pattern is relevant, but full REST field and pagination parity is **[NEEDS VERIFICATION]**. · **Recommended fix:** Extend that GraphQL helper’s query and output contract to include linked issues and every field consumed from the current files; use the existing REST path when any connection is incomplete. Do not replace the snapshots until downstream field parity is tested.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001 · **File path and lines:** `.github/workflows/mark-stable.yml:691-840`; `.github/workflows/test-and-mark-stable.yml:6095-6244` · **Severity:** Medium · **Category:** `duplication` · **Description:** The 6,305-character tag-and-pointer `run:` bodies are byte-identical, including `publish_tag_with_remote_verification` and the stale-tip checks. · **Recommended fix:** Put the shared body in a new `scripts/release_tag_helpers.sh` entry point, `release_publish_verified_tags <version> <source-branch> <tested-sha>`, and call it from both steps after their existing checkout and environment setup. Preserve immutable-tag and stale-tip checks together.

- **ID:** DUP-002 · **File path and lines:** `.github/workflows/review_autofix.yml:5333-5362,6725-6754` · **Severity:** Low · **Category:** `duplication` · **Description:** Normal and partial-finalize ledger cache stage-out steps have identical 1,610-character bodies; only their step conditions differ. · **Recommended fix:** Add `review_ledger_stage_out <workspace-path> <pr-number> <ledger-relpath>` to a trusted staged review helper, register it in `scripts/stage_workflow_support.sh:60-70`, and leave both workflow conditions unchanged.

- **ID:** DUP-003 · **File path and lines:** `scripts/check_failure_triage.sh:74-92`; `.github/workflows/check_failure_triage.yml:462-475` · **Severity:** Medium · **Category:** `duplication` · **Description:** The script and token-scoped posting step separately implement closely matching check-name sanitization and routing-key neutralization. Drift would change what is considered safe between diagnosis and posting. · **Recommended fix:** Move the transformation into a trusted `scripts/check_triage_metadata.py` function, `sanitize_check_name_display(raw: str) -> str`, with a CLI used by both callers; test identical outputs before removing either inline copy.

The only >70% similarity found among the smaller workflow files was between the short `internal-plan.yml` and `internal-implement.yml` wrappers. Their distinct triggers, permissions, and called phases do not justify combining them.

### Section 4: Expression Size Limit Risk Assessment

Measurements are decoded inline-script character counts, excluding YAML indentation. Runtime substitutions can change the final lengths; no `if:` condition approached 15,000 characters—the longest scanned was about 703 characters at `.github/workflows/review_autofix.yml:5609`.

- **ID:** EXPR-001 · **File path and lines:** `.github/workflows/implement.yml:1003-1393` · **Severity:** High · **Category:** `expression-limit` · **Description:** The staged-support `run:` body contains three `${{ }}` interpolations and measures approximately **19,132 characters**, leaving **1,868** against the stated 21,000-character expression limit. Its fully substituted length is **[NEEDS VERIFICATION]**. · **Recommended fix:** Extract this body to a trusted `scripts/implement_stage_workflow_support.sh`; pass repository and variable values through step `env`, while preserving the installed-path ledger and verified support-source checks.

- **ID:** EXPR-002 · **File path and lines:** `.github/workflows/implement.yml:3509-3829` · **Severity:** Medium · **Category:** `expression-limit` · **Description:** The preflight destructive/scope guard has one `${{ }}` interpolation in an approximately **15,517-character** body, leaving **5,483** characters to 21,000. Its fully substituted length is **[NEEDS VERIFICATION]**. · **Recommended fix:** Move the guard to a trusted script under `scripts/`, passing `github.repository` through step `env`; retain its temporary-index cleanup and output contract.

No scanned workflow exceeds 800 KB. The repository also documents and tests a **stricter byte-size guard** than the prompt’s 1 MB figure; see DEBT-001.

### Section 5: Cross-Cutting Concerns

- **ID:** DEBT-001 · **File path and lines:** `.github/workflows/review_autofix.yml:1-7747`; `tests/test_workflow_file_size_limit.py:24-27`; `agents.md:944-962` · **Severity:** Medium · **Category:** `tech-debt` · **Description:** `review_autofix.yml` measures **470,236 bytes**—only **9,764 bytes** below this repository’s 480,000-byte CI guard. `agents.md` documents a measured 512,000-byte operational limit, so the prompt’s 1 MB threshold is not the safe planning threshold for this repository. · **Recommended fix:** Before adding substantial inline content, extract large steps using the documented `review_autofix_step_<slug>.sh` pattern; retain the guard and aim for the documented 50,000-byte headroom.

- **ID:** DEAD-001 · **File path and lines:** `scripts/workflow_retro.py:40,152-181` · **Severity:** Low · **Category:** `dead-code` · **Description:** With the fixed positive five-attempt constant, `_gh_json` returns or raises on every final-attempt path; the `raise RuntimeError(last_error)` after the loop cannot execute. · **Recommended fix:** Remove that terminal raise, or make the attempt budget configurable and validate it before the loop if a zero-attempt path is intended.

No `TODO`, `FIXME`, or `HACK` markers matched in the scoped workflows or scripts. All 106 shell scripts passed `bash -n`; local `shellcheck` and `actionlint` were unavailable, so this audit does **not** claim full ShellCheck or YAML/actionlint compliance. The local Python 3.11 parser could not parse `workflow_retro.py`’s f-string expression at line 793; the invoking workflow and CI specify Python 3.12 (`.github/workflows/workflow-log-analysis.yml:244-247`, `.github/workflows/ci.yml:194-198`), so that local result is not reported as a pipeline defect.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
| --- | ---: | --- |
| Critical | 0 | — |
| High | 4 | BUG-001, BUG-002, BUG-003, EXPR-001 |
| Medium | 12 | BUG-004, BUG-005, SEC-001, API-001, API-002, BATCH-001, BATCH-002, BATCH-003, DUP-001, DUP-003, EXPR-002, DEBT-001 |
| Low | 2 | DUP-002, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
| --- | --- | --- |
| Critical/High bug fixes | `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh` | Large |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `scripts/review_collect_pr_metadata.sh` | Large |
| Code modularization | Two release workflows, `review_autofix.yml`, triage workflow/script, staged-support registry, and new shared helpers | Large |
| Expression size reduction | `.github/workflows/implement.yml` and extracted trusted scripts | Medium |
| Medium/Low fixes | Triage creation paths, `workflow_retro_fanout.sh`, `gh_helpers.sh`, `workflow_retro.py`, and review workflow size reduction | Medium |
