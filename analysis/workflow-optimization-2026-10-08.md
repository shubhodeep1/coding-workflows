## Executive Summary

- **Review checkout is the most urgent failure to diagnose.** Three `review / codex-agent` runs failed at `Checkout PR head branch` after **1,648–2,077 seconds of total run time** (37694424988, 37693141615, 37703899777). Add stage and retry timing before changing checkout behavior. **Impact:** up to 91.5 minutes of failed-run time across these three runs is at stake, but the recoverable portion is unknown. **Confidence: high** in the pattern, low in its cause.
- **Check-failure triage repeatedly fails at posting.** Eight of 20 triage runs failed, all at `Post check-failure triage issue`; only one succeeded and 11 had other outcomes. Record whether failure occurred in body validation, `gh_retry`, or response validation. **Impact:** potentially fewer than eight failed triage runs per comparable window. **Confidence: high** in the failure point, low in the proposed cause.
- **CI is a substantial feedback bottleneck:** nine of 18 runs failed; CI p50/p95 duration was **1,130/1,317 seconds**. Failures span contract tests, unit tests, and generated-doc drift, rather than one demonstrated common defect. Log the first failing test and elapsed phase. **Impact:** earlier actionable failures; time saved is unmeasured. **Confidence: high.**
- **Cost and availability conclusions are coverage-limited.** Parsed logs from 124 of 1,000 runs show **48.35 million OpenRouter tokens**, but usage is unavailable for 10 of 75 calls, so aggregate `cache_hit_rate` is null. The supplied full-log directory is absent and the collector reports zero randomly sampled successes. Restore the archive and a successful-run baseline. **Impact:** enables defensible savings and root-cause estimates; no immediate token saving. **Confidence: high.**

## Speed Optimizations

Ranked by observed end-to-end exposure; **run duration is not step duration**, so none of these is a promised saving.

1. **Review checkout — critical path.** Runs 37694424988, 37693141615, and 37703899777 ended at `Checkout PR head branch` after 1,648, 1,765, and 2,077 seconds. The current workflow already retries `git fetch` up to four attempts; its failing operation is unknown without logs. Add one structured checkout summary with `stage`, `duration_ms`, `fetch_attempts`, `git_exit_class`, `head_match`, and `outcome`, plus timings around fetch and branch switch. Preserve the existing stale-head and workspace guards. **Estimated saving:** unknown, bounded above by the 91.5 minutes of combined failed-run elapsed time if these failures can be prevented. **Risk: low** for logging; do not alter retry policy yet.
2. **Poller wait versus work — possible critical path.** All 15 `orchestrate_poll` runs succeeded, but p50/p95 was **705/1,060 seconds**; run 37696614320 took 1,183 seconds. Log per-cycle `wait_ms`, API time, decision time, candidate count, and terminal reason. Change polling cadence only if wait—not required checks—dominates. **Estimated saving:** unquantifiable until that split exists. **Risk: low** for logging; cadence changes need a safety review.
3. **CI failure feedback — critical path for affected PRs.** Run 37697219359 failed in `Targeted file context contract tests` after 1,360 seconds; runs 37697182047 and 37706481621 failed in the review-pipeline plumbing step after 653 and 689 seconds. Emit the first failing test name, its elapsed time, and job-start delay; then move consistently fast-failing checks earlier without removing any gate. **Estimated saving:** up to the remaining CI time after the first failure, not measurable here. **Risk: low** if checks and required conclusions remain intact.
4. **Sync checkout — micro-optimization.** The `log_summary` for successful run 37709349520 attributes roughly **80 of 88 seconds** to checkout. Measure fetch and checkout subphases before testing a narrower checkout against required refs. **Estimated saving:** at most 80 seconds for that observed run, not an established per-run average. **Risk: medium** for changing checkout semantics.

## Cost Optimizations

1. **Prevent expensive failed review work before tuning models.** Failed review run 37706649851 reports **18,281,276 OpenRouter tokens across 24 calls** and four context-budget warnings; its recorded failure point is `Install project dependencies (best-effort)`. Instrument sandbox preparation and failure stage first—these counters do not establish which step consumed the tokens. **Potential saving:** up to that observed token volume *if* an equivalent full rerun is avoided; no such rerun is established. **Quality risk: none** from diagnostics.
2. **Reduce repeated uncached context, conditionally.** Across parsed runs, OpenRouter logged **9,061,174 prompt**, **3,228,070 cache-write**, and **35,463,043 cache-read tokens**. Keep invariant instructions at a stable prompt prefix and put run-specific diagnostics afterward; measure prefix/version and per-call usage before pruning context. A *hypothetical* 10% reduction in prompt-plus-write tokens equals **1.23 million tokens** in this observed sample, not a forecast. **Quality risk: medium** if relevant review context is removed; start with ordering and duplication only. Nine `CONTEXT_BUDGET_WARN` events across review runs 37706649851 (4), 37697219756 (3), and 37691518672 (2) make prompt growth worth measuring.
3. **Measure Semble’s net contribution, not just query size.** Review runs made **6 `SEMBLE_QUERY` calls**, logging **83,594 bytes**, 45 sources, and 744 static-duplicate bytes; six bootstraps took **220,808 ms** total, with none failed or unused. Log per target the context bytes retained versus equivalent static expansion and whether a bootstrap was reused. Present data do **not** show tokens saved by Semble. Implement run 37697468722 instead logged **11 runtime fallbacks and no successful query**; record their target and reason before changing its fail-open path. **Estimated saving:** unknown. **Quality risk: low** for measurement, potentially high for removing retrieved context.
4. **Treat model changes as a controlled experiment.** `issue_pr_status` run 37709348544 used `openai/gpt-6-sol` for an approximately 139-second activation-verification step; plan run 37708452606 configured that editor model with an `openai/gpt-5.6-sol` fallback. Log actual model, reasoning level, usage, and verification outcome, then compare a lower-cost setting only on a bounded non-merge-authorizing path. **Dollar saving:** unavailable—no comparable prices or per-model costs were supplied. **Quality risk: medium.**

Serena recorded **zero queries, tool calls, response bytes, fallbacks, and probes**. There is no evidence that it replaced downstream work—or added noisy response bytes—in this window.

## Reliability Improvements

1. **Triage posting: classify failures before remediation.** Eight failed `check_failure_triage` runs share the posting step, including 37696949470, 37699524692, and 37703547478. `check_failure_triage.sh` already checks for an open issue with the same fingerprint, and the workflow posts through `gh_retry`; neither proves why posting failed. Emit `CHECK_TRIAGE_POST` with `stage`, `attempts`, `error_class`, `issue_created`, and a non-sensitive fingerprint identifier; distinguish validation, authorization, rate-limit, and transport failures. Keep deduplication and the token-scoped posting step. **Expected impact:** diagnose the 8/20 failure cluster and target a safe fix; reduction is unknown. **Rollback:** remove the diagnostic emission without changing posting.
2. **Review checkout: retain fail-closed integrity checks.** The three checkout failures above may reflect fetch, head movement, or a local guard; no step logs distinguish them. Record which guard or git operation exited, its duration, and retry outcome. Retry only a *confirmed* transient transport class under the existing bounds; never retry past a head mismatch as though it were transient. **Expected impact:** reduce repeat failures if transport is confirmed; presently unknown. **Rollback:** revert any retry adjustment while retaining logging.
3. **CI: distinguish independent defects from a shared setup fault.** Nine of 18 CI runs failed; named points include generated-doc drift (37702585144), poll-process tests (37694929386), and review-pipeline plumbing (37697182047, 37706481621). Log test identifier, first exception class, fixture/setup phase, and elapsed time by job. Keep every required test. **Expected impact:** faster diagnosis and fewer speculative reruns, not a quantified failure-rate reduction. **Rollback:** diagnostics are additive.
4. **Separate MCP tests from production availability.** The aggregate has **43 `SEMBLE_FALLBACK` events: 32 classified as CI contract-test fallbacks and 11 runtime fallbacks**, all 11 attributed to successful implement run 37697468722. Contract-test fallbacks are not evidence of a broken deployment; 11 runtime fallbacks without a query warrant inspection but do not establish their cause. Emit per-target reason and attempted-query counts while preserving fail-open behavior. Six bootstraps had **zero failures**. Serena had **no probes**, so its availability is unverified rather than healthy. **Expected impact:** detect a masked rollout if one exists; no failure reduction can yet be estimated.
5. **Prompt pressure, not demonstrated policy pressure.** The nine review `CONTEXT_BUDGET_WARN` events indicate prompt-size risk. `BREAK_GLASS` was **0 in parsed telemetry**, so this sample does not show rubric/policy break-glass pressure. Log prompt size by stable and dynamic component, model context window, and warning threshold; do not weaken review requirements. **Rollback:** logging-only.

## AI Memory Health

No deep-dive step logs were accessible, and the supplied run summaries contain **no `retrieve` operation**. Retrieval hit rate, average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, disabled retrieves, and push-retry counts are therefore **not measurable**, not zero.

One summary does quote `finalize-task` on successful `issue_pr_status` run 37709348544: `ok=true`, `enabled=true`, `fail_open=true`, `reason=no_linked_issues`. The flag alone does not indicate a failed operation. Verify that review, implement, and poller steps emit and retain `retrieve` records with selected-count, estimated tokens, budget, method, miss reason, and fail-open status; add a per-run operation summary to the collector so inaccessible archives do not erase the health baseline.

## GH API Call Audit

**Actual GitHub API call counts, endpoint hotspots, retries, and rate-limit events were not supplied.** Do not infer call counts from items examined.

- Successful `cancel_on_pr_close` run 37707986412 spent roughly **48 of 59 seconds** in `Release merge-queued PRs (merge train)`, reporting **38 examined, 0 released**. `scripts/review_merge_train.sh` already fetches the open-PR list once, lazily fetches active runs once, and caches file lists per distinct PR; recommending those same caches again would be redundant. Its release loop can still request file lists for candidates and older PRs. Log candidate disposition, distinct file-list fetches, pages, cache hits, active-listing calls, and elapsed/API time. Only if those counts show avoidable lookups, reuse existing prefetched data or apply a safety-preserving prefilter. **Estimated call reduction: unknown**; 38 examined is not 38 API calls.
- Triage’s duplicate-issue lookup and issue creation are separate operations. For the eight posting failures, add endpoint-*class*, attempt, HTTP/error class, and wait duration to `gh_retry` diagnostics—not response bodies or credentials. This identifies whether retries or rate limits contributed; **call reduction is unknown**.
- Follow `CLAUDE.md` §15: extend existing reads first, retain cycle-local caches, batch per-item reads only where supported, and fail open on cache miss. A job-local `GH_API_AUDIT` summary of calls by endpoint template, retries, rate-limit waits, and cache hits would quantify both call-count and rate-limit-risk reductions without adding an API call. Existing `GH_PAT_BUDGET` snapshots can supplement it, but shared-token usage is not a per-job call count.

## Prompt Cache & Memory System

**Aggregate `cache_hit_rate`: unavailable.** `scripts/cost_audit.py` deliberately returns null when any OpenRouter usage call is unavailable; **10/75 calls (13.3%)** lack usage in this window. Successful review run 37706201852 has the one supplied valid rate, **60.5235% across 24 calls**. It is not a fleet-wide hit rate. Record usage availability and cache read/write per call and preserve a stable prompt prefix; measure whether dynamic PR metadata, warnings, or retrieved context precede that prefix before attributing misses to fragmentation.

The nine review context-budget warnings are a reason to cap or deduplicate *verified* low-value dynamic context, not to suppress memory or Semble blindly. Memory retrieval effectiveness is unknown because no `retrieve` lines were available. These logging and prefix-order changes may improve tokens and latency, but their magnitude—and any reliability gain from avoiding oversized prompts—requires a new measured baseline.

## Orchestrator Health

The 150 clarify, 146 plan, and 145 implement runs are dominated by **“other” outcomes** (146, 139, and 143 respectively); recent examples are skipped, but the summary does not classify every “other” outcome. A skipped trigger is not evidence of a stuck clarification or failed wave. Emit a structured dispatch decision with `workflow_family`, `issue_or_pr_key`, `decision`, and `reason`, then correlate stage transitions rather than treating run counts as completed work.

Poller runs succeeded **15/15** but had 705-second p50 duration; record wave, deferral, conflict-heal, queue, and terminal-state transitions alongside wait time. Successful heal-intake run 37708607843 reported **`lineage_cap gen=4 max=3`**, evidence of an escalation guard firing, not proof that healing succeeded. Track cap hits and subsequent human resolution. Listed failing and slow runs show `run_attempt=1` and `retries=0`; that field cannot rule out internal `gh_retry` or git-fetch attempts.

## Pipeline Flow Bottlenecks

| Stage | Window evidence | Bottleneck type and next diagnostic |
|---|---|---|
| Clarify → plan → implement | Mostly “other”; successful plan run 37708452606 took 631s and implement run 37697468722 took 2,612s | **Flow versus compute unknown:** log dispatch reason and issue-level stage entry/exit. |
| Review/autofix | 250 runs; p50/p95 **476/1,594s**; three checkout-stage failures | **Compute, setup, or retry unknown:** capture job queue delay and timed checkout/reviewer/editor phases before changing the critical path. |
| Validate/CI | 18 runs; p50/p95 **1,130/1,317s**; nine failures | **Compute/failure feedback:** identify first failing test and parallel-job completion gap; retain all gates. |
| Orchestrate/merge | Poller p50 **705s**; merge-train scan 38 examined/0 released in run 37707986412 | **Wait versus API/merge overhead unknown:** log poll sleep, API/cache work, and per-candidate release reason. |

The collector’s `duration_seconds` uses run start-to-update time, so **queueing cannot be separated from execution with the supplied aggregates**. Record created-to-start wait and job/step spans before ranking queue remedies against compute remedies.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 **1,594s**, CI p50 **1,130s**, poller p50 **705s**.
- **Top failure modes:** eight triage posting failures; nine CI failures across distinct steps; three review checkout-stage failures.
- **Highest observed cost:** review/autofix accounts for all **48,349,281 logged OpenRouter tokens**; implement accounts for **1,324,165 of 1,338,353 logged Codex tokens**, concentrated in run 37697468722.
- **Top three actions:** **(1)** instrument triage posting and review checkout failure stages without changing guards; **(2)** restore the full-log artifact and successful-run sampling; **(3)** add timed poll/merge-train and per-call usage/cache summaries before optimizing API calls or prompts.

## Metrics Appendix

*Window: supplied October 7–8, 2026 telemetry for one repository. `insufficient_data=false` describes the assembled window; the stated full-log directory `/home/runner/work/_temp/workflow-log-output` was absent here, so no `summary.json` or `errors/`, `slow/`, or `recent/` step logs could be verified. `summary.errors=[]` is not evidence that failed-run logs were inspected. Rates below use all runs unless noted.*

| Scope | Runs | Success | Failure | Cancelled | Other | p50 / p95 duration |
|---|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 343 (34.3%) | 21 (2.1%) | 2 | 634 | 2 / 868s |
| Review/autofix | 250 | 244 | 4 (1.6%) | 2 | 0 | 476 / 1,594s |
| CI | 18 | 9 | 9 (50%) | 0 | 0 | 1,130 / 1,317s |
| Check-failure triage | 20 | 1 | 8 (40%) | 0 | 11 | 8.5 / 269.5s |
| Orchestrate poll | 15 | 15 | 0 | 0 | 0 | 705 / 1,060s |
| Clarify / plan / implement | 150 / 146 / 145 | 4 / 7 / 2 | 0 / 0 / 0 | 0 | 146 / 139 / 143 | 1 / 11s; 1 / 11s; 1 / 10s |

Among **364 success-or-failure conclusions**, success is **94.2%**; this excludes cancellations and “other” outcomes. The repository-wide 2-second p50 is dominated by short non-success-or-failure runs and is not an active-work p50.

| Logged cost and coverage metric | Observed value |
|---|---:|
| Runs with parsed log telemetry / configured success sample / randomly sampled successes | **124/1,000** / **7%** / **0** |
| OpenRouter calls / usage available / unavailable | **75 / 65 / 10** |
| OpenRouter prompt / completion / cache-write / cache-read tokens | **9,061,174 / 596,994 / 3,228,070 / 35,463,043** |
| OpenRouter total tokens | **48,349,281** |
| Codex calls / logged tokens, reported separately | **31 / 1,338,353** |
| `cache_hit_rate` aggregate / run 37706201852 | **null / 60.5235%** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` / samples | **1,000 / 2,713,800 / 121** |
| `break_glass_count` / `context_budget_warn_count` | **0 / 9** |
| Semble queries / logged bytes / sources / static-duplicate bytes | **6 / 83,594 / 45 / 744** |
| Semble fallbacks: contract-test / runtime; bootstraps / failed / unused / total time | **32 / 11; 6 / 0 / 0 / 220,808 ms** |
| Serena queries / response bytes / tool calls / fallbacks | **0 / 0 / 0 / 0** |

| GH API and MCP availability | Observed value |
|---|---|
| GH API calls by workflow, endpoint, retry, rate-limit event | **Not collected in supplied context**; merge-train’s **38 examined** in run 37707986412 is an item count, not a call count. |
| Semble runtime fallback rate by target | **Not computable**: runtime fallback targets/reasons are unavailable, and 11 implement fallbacks have no corresponding successful query. |
| Serena per-tool response-byte breakdown | **No tool calls observed**; no breakdown available. |
| Per-target MCP availability: `probe_ok` / `probe_failed` / `probe_skipped` | **No target rows observed**; aggregate Serena **0 / 0 / 0** means availability was not measured. No Semble probe counts were supplied. |
| Other MCP servers observed | **None in supplied summaries**; absent full logs prevent ruling out uncollected prefixes. |
