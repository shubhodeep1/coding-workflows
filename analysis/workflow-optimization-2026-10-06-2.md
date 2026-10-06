## Executive Summary

- **Repeated conflict-resolution failures are the clearest systemic signal.** In `shubhodeep1/coding-workflows`, 42 failed review/autofix runs concern six PRs, and two review-blocked judge runs failed at the same “Run Codex resolver, validate, stage, commit” step. The repository workflow runs that step only on its merge-conflict path; the actual error and whether successive runs used the same head and failure fingerprint are unverified. **Impact:** resolving or safely suppressing a confirmed repeat could save roughly 7–16 minutes per avoided run, based on the named failures. **Confidence: high** in the cluster, **low** in its cause.
- **The existing repeat-failure safeguard needs an audit before a change.** `review_autofix.yml` already has a default three-identical-failure fingerprint cap. PR #6538 nevertheless has eight failed runs at the resolver step; changing the cap without head SHA, fingerprint, and gate-decision evidence could suppress legitimate new attempts. **Impact:** potentially substantial avoided reruns; savings are not yet attributable. **Confidence: medium**.
- **Review and CI dominate elapsed time, while skips distort the overall median.** Review/autofix has 227 runs, a 556-second p50 and 1,068-second p95; CI has a 1,127-second p50 across 10 runs. The overall two-second p50 includes 646 runs classified as neither success, failure, nor cancellation; recent examples are skipped. **Impact:** critical-path work merits attention before micro-optimizations. **Confidence: high**.
- **Reported AI cost is concentrated but not representative of all runs.** Five slow review runs account for *all* 157,466,029 reported OpenRouter total tokens and all 125 reported calls. Thirteen calls lack available usage, and `cache_hit_rate` is null. **Impact:** a targeted investigation of those runs may find savings, but a fleet-wide dollar estimate would be unsound. **Confidence: high**.
- **Diagnostic coverage is the immediate low-risk investment.** The supplied run rows have no GH API call totals or memory-retrieval events, and the named full-log directory is absent in this environment. Add bounded, structured summaries to existing logs and preserve existing gate/failure markers in collection; do not add API requests. **Impact:** makes the next failure and cost audit actionable. **Confidence: high**.

## Speed Optimizations

1. **Critical path — prevent only *verified* identical resolver reruns.** PR #6538 failed eight times at the resolver step, including runs `37510790245` (776 seconds) and `37513226959` (530 seconds); PRs #6498, #6438, #6462, #6209, and #6509 also recur. **Likely category:** merge/conflict handling, inferred from the step’s workflow guard; the failing operation remains unknown. **Change:** correlate the already-emitted failure fingerprint and gate-cap decision by PR and head SHA, then fix a demonstrated cap-coverage or dispatch gap without blocking a changed head or explicit judge dispatch. **Estimated saving:** one confirmed duplicate costs about 7–16 minutes in the observed failures. If—and only if—all 42 PR runs shared one head and fingerprint per PR, the three-failure cap implies a theoretical ceiling of 24 avoidable runs; that condition is not established. **Risk:** low for correlation logging, medium for gate changes.
2. **Critical path — measure resolver sub-stages before tuning timeouts.** The resolver step is the failure point for run `37508567446` (946 seconds), while successful review run `37503702639` lasted 4,648 seconds. The resolver script already logs retries and timeout classifications; add one bounded result line per attempt with elapsed time, attempt number, failure category, validation result, and final exit status. Separate model execution from validation and commit. **Estimated saving:** unknown until those durations are collected; targeting the dominant sub-stage has more upside than setup tweaks. **Risk:** low for logging; do not shorten the existing safety timeout on this evidence.
3. **Queueing, not compute — reduce unnecessary launches where event semantics permit.** Run `37513343789` waited about 84 seconds for a runner within a 108-second release; readiness run `37513476036` waited about 60 of 69 seconds. **Change:** record queued-to-start time and skip reason by job, then move only demonstrably safe, non-required event filters before runner allocation. **Estimated saving:** up to the observed 50–84-second waits for launches proven unnecessary, not for required checks. **Risk:** medium; never remove a required status check to reduce queueing.

## Cost Optimizations

1. **Avoidable reruns outrank model tuning.** The 42 failed runs across six PRs repeatedly reach an expensive review-family step, but their zero token fields do **not** establish zero spend. **Change:** first join dispatch origin, head SHA, fingerprint, cap result, and per-run usage; suppress only confirmed same-head, same-fingerprint repeats through the existing safeguard. **Estimated saving:** one full invocation per avoided repeat; token and dollar amounts unavailable. **Quality risk:** low only when new heads and judge bypass remain intact.
2. **Investigate the five measured high-cost runs.** Run `37499560592` alone reports 73,231,494 total tokens—46.5% of the reported window—including 64,987,770 cache-read tokens. The other four measured runs supply the remainder; the 125 calls are not spread across 125 costing runs. **Change:** emit phase/model/reasoning and stable-context versus dynamic-context byte counts per call, then trim duplicated context in the highest-volume phase while retaining validation inputs. **Estimated saving:** not quantifiable from current phase-level data. **Quality risk:** medium for prompt removal; compare review outcomes before rollout.
3. **Bound retrieval output, not useful retrieval.** `plan` run `37513231492` logged 72 Semble queries and 577,080 returned bytes; review run `37499560592` logged 16 queries and 105,440 bytes. Semble’s logged bytes measure returned output, not tokens saved. **Change:** log target, query hash, returned bytes, selected-chunk count, and whether content entered the prompt; deduplicate identical successful results within a run and test smaller chunk limits where results prove repetitive. **Estimated saving:** unknown until prompt inclusion and duplication are measured. **Quality risk:** medium if relevant evidence is removed.
4. **Defer model or reasoning downgrades.** Run `37513231492` reports `openai/gpt-6-sol` as configured, while recent review summaries report `openai/gpt-6-luna` for ancillary roles; actual per-model usage and dollars are absent. Resolver reasoning is configured separately. **Change:** collect role/model/reasoning usage first; trial cheaper settings only on bounded, non-judgmental work with an outcome comparison. **Estimated saving:** unknown. **Quality risk:** high for untested reviewer or resolver changes. Serena logged no queries, probes, or tool calls, so there is no evidence it replaced downstream work or added response noise.

## Reliability Improvements

1. **Classify the resolver failures at source.** Of 50 failed runs, 44 are review/autofix; 42 concern the six repeatedly failing PRs and two are judge runs `37510586096` and `37510669078`. **Root-cause category:** conflict-resolution path, not a verified timeout, model failure, validation failure, or push failure. **Fix:** supplement existing attempt logs and `AUTOFIX_FINGERPRINT` markers with a final sanitized failure category and stage exit code; have the collector retain the existing gate-cap decision alongside it. **Expected impact:** diagnosis first, then fewer repeat failures once a common cause is confirmed. **Rollback/fail-open:** logging must not alter exits, retry budgets, or the judge bypass.
2. **Keep test fallbacks separate from production availability.** CI logs contributed 20 Semble fallbacks—4 in `37500876441`, 8 in `37507192651`, and 8 in `37503451645`—all classified as `contract-test`; recorded runtime fallbacks are zero. Poller run `37512719372` has a summary showing Semble enabled but unavailable, without a complete availability reason. **Fix:** report contract-test and runtime counts separately, plus a per-target preflight reason when Semble is enabled but unavailable. **Expected impact:** distinguishes an intentional fail-open test from a masked unavailable rollout. **Rollback:** preserve the existing fallback behavior; do not treat the 20 tests as production incidents. Serena has no probe observations with which to assess availability.
3. **Expose independent secondary failures.** CI run `37499559962` failed at “Orchestrate lib unit tests” after 825 seconds; triage runs `37499182533` and `37501367749` failed at context collection and issue posting respectively; clarify-response run `37514217111` failed at “Check orchestrator metadata.” **Root-cause category:** unknown without step logs. **Fix:** record a bounded exit category for each failing step and distinguish API, input, and validation errors without printing payloads. **Expected impact:** fewer blind triage reruns; failure-rate reduction cannot yet be estimated. **Fail-open:** retain current step and safety behavior.
4. **Separate prompt pressure from policy pressure.** There are four `CONTEXT_BUDGET_WARN` events: two each in review runs `37503702639` and `37499560592`; recorded `BREAK_GLASS` count is zero. **Fix:** retain warning phase, prompt tokens, context window, and threshold in collected summaries. **Expected impact:** identifies prompt-size risk before context exhaustion; these counts alone do not establish rubric or policy pressure. **Rollback:** none for additive logging.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` event is present in the supplied rows or excerpts, and the full logs cannot be opened here. Consequently retrieve hit rate (`records_selected > 0`), average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, `fail_open`, `enabled: false`, and push retry counts are **not measurable**—not zero.

`scripts/ai_memory.py` emits retrieval fields including `records_selected`, `estimated_tokens`, `token_budget`, `keyword_method`, and `miss_reason`. Verify emission in a sampled `review_autofix` step and preserve these bounded event fields in collection, grouped by operation and role. Record push retry counts without memory content or credentials. **Expected impact:** identifies ineffective or unavailable retrieval before changing memory budgets; no retrieval optimization is justified yet.

## GH API Call Audit

- **Observed activity, not a call count:** `cancel_on_pr_close` run `37513128493` reports `MERGE_TRAIN_RELEASE_SUMMARY examined=38 released=0`; its release step took about 50 seconds. “Examined” is **not** 38 API requests. Repository code already paginates the open-PR inventory, caches file lists per run, and reads active runs once lazily for that release invocation. Preserve those safeguards. **Change:** log inventory pages, unique file fetches, cache hits, active-run listing pages, and queued-reason counts. **Estimated reduction:** unknown; this establishes whether reuse across callers would help.
- **Potential hotspot to measure:** `scripts/review_merge_train.sh` documents normally ten active-run listing calls when release first evaluates a queued PR. `review_autofix_sweep.yml` separately snapshots active review statuses once per workflow, rather than per PR. **Change:** count normalized route calls by workflow/job/step and compare snapshots before considering reuse. If an identical within-job lookup is repeated *N* times, cycle-local reuse can remove up to *N − 1* reads; no such repetition is demonstrated by this telemetry. **Rate-limit impact:** conditional reduction, currently unquantifiable.
- **Hygiene constraint:** `CLAUDE.md` §15 requires checking existing responses, batching per-item lookups, cycle-local caches, and safe fallback. The repository already uses batched linked-issue lookups. Instrument `gh_helpers.sh` and relevant existing call sites with route template, status class, elapsed time, retry count, and rate-limit class, emitted as a job summary **without a new API call** or request arguments. Supplied telemetry has no API call, retry, or rate-limit totals; do not infer absence of rate limits from that gap.

## Prompt Cache & Memory System

Review telemetry records 129,436,175 cache-read tokens and 3,567,758 cache-write tokens, but **`cache_hit_rate` is null**. `scripts/cost_audit.py` deliberately withholds that rate when any OpenRouter usage call is unavailable; this window has 13 such calls out of 125. Cache-read volume is not a substitute hit rate or a dollar-saving estimate.

The two slow review runs with budget warnings (`37503702639`, `37499560592`) warrant comparing stable prefix size and dynamic insertions across calls. **Inference:** moving changing timestamps, run metadata, and retrieved snippets after a stable prefix *may* improve reuse; fragmentation has not been measured. Log a privacy-safe prefix hash and stable/dynamic byte counts alongside usage availability, then compare same-role calls before changing prompt order. Preserve memory-retrieval fields proposed above. **Expected impact:** token and latency savings, and reduced context-risk, are measurable after instrumentation; current data cannot price them. Maintain fail-open behavior on cache or memory misses.

## Orchestrator Health

The poller completed 8 of 9 runs successfully, with one cancellation and a 747-second p50. That establishes activity, not healthy wave progression. `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have respectively 151/155, 148/152, 149/154, and 150/152 runs in “other”; recent rows in these families are skipped. **Inference:** substantial trigger fanout may be harmless gating, but the supplied data cannot distinguish intentional skips from stalled work.

The merge-train release examined 38 queued candidates and released none in run `37513128493`; blockers, active reviews, and lookup failures are not broken out in its supplied summary. Add one per-tick state-transition summary: tracking issue, wave, previous/new state, eligible and deferred counts by reason, conflict-heal attempts, judge dispatches, terminal counts, and age since progress. Preserve existing per-PR markers and avoid new lookups. **Expected impact:** makes stalled versus correctly deferred work observable; no safe cadence or dispatch change follows from `released=0` alone.

## Pipeline Flow Bottlenecks

| Segment | Evidence | Bottleneck type and next diagnostic |
|---|---|---|
| Clarify → plan → implement | Recent runs such as `37514457684`, `37514457733`, and `37514457877` skipped; the three families have 448 “other” outcomes combined. | **Gating/fanout, unverified as a stall:** collect reason and upstream issue/state for each skip before changing triggers. |
| Review/autofix → conflict resolution | Review p50 556 seconds; 42 failures on six PRs at the guarded resolver step; run `37503702639` reached 4,648 seconds successfully. | **Compute/retry and merge-conflict overhead:** collect sub-stage and per-attempt times, then address the dominant verified cause first. |
| Validate/CI | CI p50 1,127 seconds; `37499559962` failed after 825 seconds; three CI runs were cancelled after 2,713–2,816 seconds. | **Compute or superseded work, not separable here:** record job start, cancellation reason, head SHA, and test sub-stage timing; retain required validation. |
| Orchestrate/merge | Poller p50 747 seconds; release run `37513128493` examined 38 and released zero. | **Waiting, deferral, or merge overhead unknown:** count each queued reason and time since last state change. |
| Runner queue | `37513343789` waited ~84 seconds; `37513476036` ~60 seconds. | **Queueing:** report queued-to-start separately from execution; avoid only demonstrably unnecessary launches. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review/autofix and CI elapsed time, repeated guarded conflict-resolution runs, and sampled 50–84-second runner waits. **Top failure modes:** 44 review-family failures concentrated at the resolver step; one CI test failure and two triage failures have no supplied error text. **Highest measured cost:** five slow review runs account for all 157,466,029 reported tokens; `37499560592` accounts for 73,231,494.

**Prioritized actions:**
1. Join existing review gate/fingerprint markers to PR, head, dispatch source, resolver attempt result, and failure category; audit #6538 first. Do not change the three-failure cap until that join is verified.
2. Add bounded per-stage timing and usage-availability summaries to slow review and CI runs, retaining required checks.
3. Add API route-count, merge-train deferral-reason, orchestrator-transition, and memory-retrieval summaries to existing logs without additional API requests.

## Metrics Appendix

**Scope and coverage.** Supplied GitHub Actions API context covers 1,000 runs for one repository, with `success_sample_rate=0.07`; `insufficient_data=false` describes the assembled window, **not** complete diagnostic coverage. `/home/runner/work/_temp/workflow-log-output/summary.json` and its `errors/`, `slow/`, and `recent/` directories are absent in this environment. The supplied `log_summary` strings are sometimes truncated. Full error text, per-target MCP events, API call envelopes, and memory events therefore could not be verified.

| Family | Runs | Success | Failure | Cancelled | Other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 288 (28.8%) | 50 (5.0%) | 16 | 646 | 2 / 838 s |
| Review/autofix | 227 | 183 (80.6%) | 44 (19.4%) | 0 | 0 | 556 / 1,068.2 s |
| CI | 10 | 6 (60%) | 1 (10%) | 3 | 0 | 1,127 / 2,771 s |
| Orchestrate poll | 9 | 8 | 0 | 1 | 0 | 747 / 1,184.8 s |
| Clarify / plan / implement / clarify respond | 155 / 152 / 154 / 152 | 4 / 4 / 3 / 1 | 0 / 0 / 2 / 1 | 0 | 151 / 148 / 149 / 150 | Predominantly short “other” runs |

| Logged-cost measure | All / review-autofix | Coverage note |
|---|---:|---|
| OpenRouter calls; usage available / unavailable | 125; 112 / 13 | All calls occur in five measured slow review runs |
| Prompt / completion / total tokens | 23,254,564 / 1,214,755 / 157,466,029 | Total is reported usage, **not** an additional sum to charge on top of components |
| Cache-write / cache-read tokens; `cache_hit_rate` | 3,567,758 / 129,436,175; **null** | Missing usage prevents a valid hit rate |
| `break_glass_count` / `context_budget_warn_count` | 0 / 4 | Warnings: 2 each in `37503702639` and `37499560592` |
| Log-parsed runs; wall-clock samples | 125; 123 | Parsing does not imply usage was emitted |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 10,000 / 4,305,260 | Across collected wall-clock samples, not per-model-call latency |

| MCP family | Queries | Logged query/output bytes | Fallbacks | Availability / breakdown |
|---|---:|---:|---:|---|
| Semble, plan | 72 | 577,080 | 0 | All queries attributed to run `37513231492`; target breakdown unavailable |
| Semble, review/autofix | 153 | 866,841 | 0 | Target breakdown unavailable |
| Semble, CI | 0 | 0 | 20 | All 20 classified `contract-test`; runtime fallbacks: 0 |
| **Semble total** | **225** | **1,443,921** (~6,417 bytes/query) | **20** | A live-query fallback rate is **not computable** by mixing CI test fallbacks with plan/review queries |
| Serena total | 0 | 0 response bytes | 0 | 0 tool calls; `probe_ok` / `probe_failed` / `probe_skipped`: 0 / 0 / 0; per-tool breakdown unavailable |

| MCP target | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Semble targets not supplied | N/A | N/A | N/A | No per-target availability rows in assembled context |
| Serena targets not supplied | N/A | N/A | N/A | Aggregate zero probes does not establish availability |

**Other MCP servers observed:** none identifiable in the supplied rows; unknown-server coverage cannot be checked without the full logs or per-server collector output.

| GH API signal | Supplied measurement | Collection needed |
|---|---|---|
| Calls by route/job/step; repeated lookups | Not supplied | Existing-call counters and cache-hit summaries |
| Rate-limit events; retry attempts | Not supplied | Status-class and retry summaries from existing helpers |
| Merge-train release | Run `37513128493`: 38 examined, 0 released; ~50-second step | Pages, unique fetches, and deferred reasons—not an inferred API-call count |

The next collection should preserve full resolver attempt and fingerprint-cap markers for the six PRs, then attach bounded API, memory, model-usage, and per-target MCP summaries to the same run IDs.
