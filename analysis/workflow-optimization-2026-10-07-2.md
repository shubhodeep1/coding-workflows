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
