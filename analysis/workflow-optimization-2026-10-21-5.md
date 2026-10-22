Checkpoint:
- Read only, no files edited. Looked at `review_autofix.yml` (the "Checkout PR head branch" step), `internal-cancel-on-pr-close.yml` (heal-pr-reconcile checkout), `workflow_failure_heal_pr_reconcile.sh`, `gh_helpers.sh` (how GH_PAT_BUDGET is emitted), `collect_workflow_logs.py` (the regex that parses it) and `cost_audit.py` (how Semble fallbacks and bootstraps are classified).
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox. The evidence below comes from the supplied analysis context, its `log_summary` fields, and the repo code.

## Executive Summary

- **Reliability is high, with one failure we can't diagnose: 1 failure in 1,000 runs (0.1%).** Review/autofix run `39600043919` failed in job `review / codex-agent` at step "Checkout PR head branch" after 715s. That step (`.github/workflows/review_autofix.yml:2832-2945`) has at least 7 different `exit 1` paths, and only some print a `::error::` line. The log directory wasn't mounted, so the root cause can't be found. **Fix:** add a structured reason line on each failure path. Impact: the next failure of this kind becomes diagnosable from one line, and the ~12 min of compute lost per failed run can then be addressed. Confidence: high on the gap, low on the cause.
- **Review/autofix is the slowest stage on the critical path (p50 809s, p95 3,019s, 86 runs).** The worst runs bunch together between 12:27 and 13:25 UTC: `39606614099` 4,850s, `39604185434` 4,437s, `39604068731` 4,206s. Evening runs of the same workflow took 787–1,515s (`39689237254`, `39692063953`, `39689267631`). CI shows the same pattern: 2,962–3,166s at midday (`39602689033`, `39604952766`, `39604027142`) against 812–855s in the evening. **Inference:** this is contention, either for hosted runners or for the Claude pool, during a burst of PR activity. It is not the per-run work. The impact could be 50–70% off the p95 if contention is confirmed. Confidence: medium.
- **The token data we have is concentrated: two review runs account for 99.5% of logged tokens.** Logged OpenRouter totals are 16.88M tokens (3.32M uncached prompt, 12.95M cache read, 0.61M cache write, 85.6k completion) across 34 calls. Review runs `39604068731` (10.36M) and `39689267631` (6.43M) are almost all of it. Only 2 of 86 review runs report any tokens. Most codex-agent logs were 1.1–1.66M characters and truncated, so real spend is understated, likely by a large factor. Confidence: high on the gap.
- **`test_and_mark_stable` writes prompt cache that is never read.** Run `39600101749` made 12 calls: 80,449 cache-write tokens out of 83,269 prompt tokens, with `cache_hit_rate=0.0`. Cache writes cost more than plain input, so this is pure overhead. Fixing it saves roughly 97% of that workflow's cache-write premium. Confidence: high.
- **The heal-pr-reconcile job does a full-history fetch on every PR close, then does nothing.** `internal-cancel-on-pr-close.yml:50` uses `fetch-depth: 0`. In `39694096171`, checkout took ~135s of a 146s run. In `39692005068`, ~72s of 78s preceded a script that ran for 0.6s, with ~560K characters of tag output. Both ended in `noop reason=no_heal_issue`. A shallow checkout saves ~70–135s per merged/closed PR. Confidence: high.
- **None of the 56 Semble fallbacks happened in production.** All 56 are contract-test fixtures (CI 52, test_and_mark_stable 4); runtime fallbacks are 0. The real waste is elsewhere: 3 Semble bootstraps went unused in `workflow_log_analysis` run `39601437388` (102,982 ms), and the orchestrate poller runs with `SEMBLE_ENABLED: true` but `SEMBLE_AVAILABLE: false` and emits no telemetry about it. Confidence: high.
- **About 49% of runs (486 of 1,000) end as "other" (mostly skipped).** clarify 92/98, plan 85/90, orchestrate_clarify_respond 89/90, implement 75/90, check_failure_triage 49/49, workflow_failure_heal 47/47. Each costs a runner pickup and a log entry but does no work. This is mostly noise. Medium priority.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Review/autofix and CI slowdown during bursts (critical path).**
   - **Evidence:** midday review runs took 2,054–4,850s (`39606614099`, `39604185434`, `39604068731`, `39600275607`, `39612678637`). Evening runs took 787–1,515s. Midday CI took 2,113–3,166s; evening CI took 812–855s (`39692090495`, `39692063687`, `39689237026`). Implement runs bunch in the same window: `39603654703` 1,936s, `39603528999` 1,560s, `39603360674` 1,470s, `39603939898` 1,290s, `39605996180` 1,269s.
   - **Root cause (inference):** contention. Two candidates: (a) hosted-runner concurrency (poller runs waited 98s–3.5 min for a runner in `39690716481`, `39683175108`, `39689601717`, `39674426598`), and (b) `CLAUDE_POOL_CAPACITY_WAIT_ENABLED: true` blocking review agents while pool accounts are busy.
   - **Change:** add the diagnostics needed to tell (a) from (b). Do not restructure anything yet.
     - Emit one `CLAUDE_POOL_CAPACITY_WAIT role=<r> waited_ms=<n> outcome=<acquired|timeout>` line wherever `capacity_wait` blocks (`scripts/ai_engine.sh`, around lines 225/274). This is a new prefix, so register it in an `agents.d/` fragment (§25).
     - Have the collector record job `started_at - created_at` (queue time) per job. That data is already in the Jobs API response the collector fetches.
   - **Expected saving:** once the cause is known, cutting midday p95 back toward evening levels removes ~1,500–3,000s per affected run (inference).
   - **Risk:** logging only, negligible.

2. **Several full review cycles per PR.**
   - **Evidence:** PR 7795 had 3 slow review runs (`39604185434` 4,437s, `39600275607` 3,079s, `39612678637` 2,054s), about 9,570s in total. PR 7807: 3 runs (`39613695584` 1,850s, `39600391051` 1,163s, `39622468218` 920s). PRs 7820, 7821 and 7817: 2 runs each, about 900–1,320s apiece.
   - **Root cause:** each push or autofix iteration starts a full review. We can't see from this data whether the later passes re-reviewed unchanged files.
   - **Change:** find out whether the existing iteration summary (`review_autofix_step_iteration_summary.sh`) records a per-iteration diff size. If it does, add `review_iteration=<n>` and `changed_files=<n>` to the run summary so the collector can measure redundant passes. Narrow the scope of follow-up reviews only after that data exists.
   - **Saving:** unknown until measured.
   - **Risk:** low.

3. **heal-pr-reconcile full fetch (off the critical path, but runs on every PR close).**
   - **Evidence:** `39694096171` (~135s checkout out of 146s) and `39692005068` (~72s before a 0.6s script).
   - **Root cause:** `fetch-depth: 0` at `internal-cancel-on-pr-close.yml:50`. The script already runs its own targeted `git fetch --no-tags` (`workflow_failure_heal_pr_reconcile.sh:159,165`), and its noop exit at line 104 happens before any git work.
   - **Change:** use `fetch-depth: 1`. If the rebase path needs history (inference: a merge-base may be required), deepen only inside the script, after `HEAL_ISSUES` is found to be non-empty (`git fetch --unshallow` or `--shallow-since`).
   - **Saving:** 60–130s per PR close. cancel_on_pr_close p95 should drop from 298s toward ~15s.
   - **Risk:** low. The job is already `continue-on-error: true`.

4. **CI long pole: `tests-release-and-log-analysis`.**
   - **Evidence:** about 8–10 min, with roughly 10 min of unlogged work after the last visible test step and before `Post Set up Node 22 for the claude-pool-broker Worker tests` (`39692005590`, `39689237026`, `39689267485`).
   - **Change:** print start/end timestamps around the Worker test step, then move it into its own parallel job if it dominates.
   - **Saving:** about 5–8 min per CI run, with medium confidence.
   - **Risk:** low.

5. **Micro-optimization: the promote_candidate_follow freeze-tag probe takes about 2.7s.** See `39692005702`. Not worth changing on its own.

## Cost Optimizations

Ranked by expected token savings.

1. **Close the token-telemetry gap in review/autofix first.**
   - **Evidence:** 20 review runs carry parsed log telemetry, but only 2 report OpenRouter usage. `claude_*` counters are absent from the supplied rows, even though `AI_ENGINE_SELECTED ... engine=claude` appears (`39693542526`). codex-agent logs are 1.12–1.66M characters and truncated (`39692063953`, `39689267631`, `39689237254`).
   - **Root cause:** usage lines sit in the middle of very large step logs, and the summaries only see the truncated window. We could not confirm whether the collector parses the full archive.
   - **Change:** emit one per-job rollup line at job end (the same pattern as `GH_PAT_BUDGET`), e.g. `AI_USAGE_ROLLUP engine=<e> calls=<n> prompt=<n> cache_read=<n> cache_write=<n> completion=<n>`. Teach `cost_audit.py` to parse it, as an addition alongside existing fields.
   - **Saving:** none directly. It is a prerequisite for any reliable cost work.
   - **Quality risk:** none.

2. **Cache writes without reads in `test_and_mark_stable`.**
   - **Evidence:** `39600101749` wrote 80,449 cache tokens across 12 calls, read 0, `cache_hit_rate=0.0`.
   - **Root cause (inference):** either the prompt prefix differs on every call, or each call is a one-off whose cache entry expires before reuse.
   - **Change:** for that workflow's calls, either make the prompt prefix stable (move dynamic fields after the static instructions) or skip cache-control on one-shot calls. `OPENROUTER_PROMPT_CACHE_DISABLED` already exists as a config knob (seen in `39689601717`), so scope it per workflow rather than adding something new.
   - **Saving:** the cache-write premium on about 80k tokens per release run.
   - **Quality risk:** none.

3. **Review prompt size.**
   - **Evidence:** `39604068731` averaged about 149k uncached plus 830k cached prompt tokens per call over 10 calls. `39689267631` averaged about 146k uncached per call over 12 calls with `cache_write=0`, which means the 0.727 hit rate came from cache entries written by earlier runs.
   - **Root cause:** the prompts are large. An overall cache hit rate of 0.767 is decent, but 146–149k uncached tokens per call means each call still sends a large changing section.
   - **Change:** measure where the uncached part comes from (diff, Semble context, memory) by adding a byte count per section to the prompt-assembly log. Then move the stable parts first.
   - **Saving:** each 0.05 gain in hit rate saves about 165k uncached tokens per 1.6M-prompt review run (inference).
   - **Risk:** low.

4. **Semble usage.**
   - **Evidence:** review_autofix ran 3 queries returning 50,710 bytes from 22 sources with `static_dup_bytes_total=0`. That is about 16.9 KB per query with no duplication of static context, so Semble adds focused context, not noise.
   - **Cost:** bootstrap time is high relative to use: 101,823 ms for 3 bootstraps, about 34s each. In `workflow_log_analysis` run `39601437388`, 3 bootstraps (102,982 ms) were never queried (`semble_bootstraps_unused=3`; `cost_audit.py:879-880` counts every bootstrap as unused when a run made no queries).
   - **Change:** in `workflow_log_analysis`, do not bootstrap Semble when the analysis prompt will not query it. Lazy mode should already ensure this, which suggests something calls bootstrap eagerly; check the bootstrap call sites.
   - **Saving:** about 100s of runner time per log-analysis run.
   - **Risk:** none.

5. **Serena:** disabled (`SERENA_ENABLED: false` in `39694096172`, `39689237254`). There are no queries and nothing to evaluate.

6. **Model selection:** heal intake runs `claude-opus-5-5 effort=high` (`39693542526`, 135s). There is no token data to judge whether a cheaper tier would do, so this stays a data gap.

## Reliability Improvements

Ranked by expected reduction in failures or reruns.

1. **Undiagnosable "Checkout PR head branch" failure.**
   - **Evidence:** `39600043919`, failure_point `review / codex-agent` / `Checkout PR head branch`, 715s, attempt 1, no retry.
   - **Root cause category:** unknown, possibly workspace/infra or git transport. The step's failure exits are:
     - workspace missing (`review_autofix.yml:2862`)
     - OpenCode config present (2867)
     - empty `HEAD_REF` (2873, no `::error::`)
     - `git_ref_health_check.sh repair` failing under `set -e` (2894, no annotation)
     - fetch failing after 4 attempts (2916)
     - `git reset --hard` or `git checkout -f -B` failing (2926/2944, no annotation)
   - **Fix:** add an `ERR` trap at the top of the step that emits a structured line, plus explicit reasons on the bare `exit 1` paths:
     ```bash
     trap 'rc=$?; echo "REVIEW_CHECKOUT_FAIL reason=${_rcf_reason:-unannotated} line=${LINENO} rc=${rc}"' ERR
     # before each exit 1 / risky command: _rcf_reason=workspace_missing | opencode_config | head_ref_empty | ref_repair | fetch_exhausted | reset_failed | checkout_failed
     ```
     Register the `REVIEW_CHECKOUT_FAIL` prefix through an `agents.d/<pr>-review-checkout-fail.md` fragment (§25). Watch the 480,000-byte workflow size limit (§24); adding a few lines is fine, but check with `wc -c`.
   - **Impact:** the next occurrence can be classified straight away. If `fetch_exhausted` keeps recurring, the backoff can be extended.
   - **Rollback:** logging only. The trap does not change exit codes.

2. **Runs that lose their work to cancellation.**
   - **Evidence:** 27 cancelled runs: workflow_failure_heal_intake 18 (e.g. `39693583661` 15s, `39693562589` 13s), implement 8, orchestrate_poll 1.
   - **Root cause (inference):** concurrency-group supersession. The quick cancels look intended, but the 8 implement cancellations should be checked against how long they ran.
   - **Fix:** have the collector record `cancelled_after_s` per family, and flag cancelled implement runs that lasted more than 300s as lost work.
   - **Impact:** visibility only.
   - **Rollback:** not applicable.

3. **Semble masked as unavailable in the orchestrate poller.**
   - **Evidence:** `39692629272`, `39690716481`, `39689601717` and `39688610020` all show `SEMBLE_ENABLED: true`, `SEMBLE_AVAILABLE: false`, `SEMBLE_BIN` empty, and zero `SEMBLE_FALLBACK` or `SEMBLE_PROBE` lines.
   - **Interpretation:** either the poller doesn't use Semble (which is fine, but then the flag is misleading), or it silently runs without it. With zero runtime fallbacks we can't tell "unused" from "silently degraded".
   - **Fix:** emit one `SEMBLE_FALLBACK target=orchestrate_poll reason=unavailable` line at poll start when the flag is enabled but the binary is missing. This uses the existing prefix, so `cost_audit.py` counts it as a runtime fallback automatically.
   - **Fail-open:** unchanged.

4. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** both 0 across all families. `REVIEW_BREAK_GLASS_ENABLED: false` (`39689237254`). There is no sign of rubric or prompt-size pressure in the data we have, but the truncated logs could hide warnings, so confidence is medium.

5. **Noise that hides real warnings (low priority).**
   - The post-job warning `git submodule foreach ... cannot be used without a working tree` appears in every codex-agent run (`39692063953`, `39689267631`, `39689237254`).
   - The poller logs a `Node.js 20 is deprecated` warning for `actions/cache/restore@v4` (`39687239893`, `39685550243`, `39683175108`, …). `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` already covers it at runtime.
   - **Fix:** suppress or neutralise the submodule cleanup (e.g. `submodules: false` with a clean-path guard), and schedule an action bump once a Node 24 release is confirmed. I can't verify release tags from here.

## AI Memory Health

- **What we found:** only two `AI_MEMORY_TELEMETRY` lines, both `op=finalize-task`, `ok=true`, `enabled=true`, `fail_open=true`, `reason=no_linked_issues`, `source=issue_pr_status.yml` (`39694096097` for PR #7824, `39692005100` for PR #7822). Both are correct noops: the PRs had no linked issues.
- **Retrieve operations:** none observed. So hit rate, `estimated_tokens` against budget, and the `keyword_method` split (llm / plain / none) cannot be computed. We also have no `record-candidate`, `promote`, `compact` or `processed-command-*` lines.
- **Likely cause (inference):** retrieves happen inside codex-agent, implement and plan jobs, whose logs were cut by 848K–1.66M characters.
- **Recommendation:**
  - Emit a per-job memory rollup at job end, e.g. `AI_MEMORY_TELEMETRY: {"op":"job-rollup","retrieves":n,"hits":n,"tokens":n,"fail_open":n}`, as an addition to the existing schema.
  - Alternatively, confirm that the collector's untruncated archive is parsed for `AI_MEMORY_TELEMETRY`.
  - Until then, treat memory retrieval effectiveness as unmeasured.

## GH API Call Audit

- **Highest per-job usage:**
  - review `codex-agent`: 278 calls (`39692063953`) and 286 calls (`39689267631`)
  - heal `intake`: 77 (`39680782018`), 74 (`39680780928`), 23 (`39693542526`)
  - Everything else is small: sweep 2–9 (`39694131936` 9, `39679849078` 8), post-merge 0–3, the lint commit fetch is one paginated call.
- **Rate-limit risk:** low. The lowest remaining budget seen was 3,606 (`39683270126`). There were no 429s or secondary-limit events. That said, 86 review runs × ~280 calls ≈ 24k calls per window, so review is the dominant consumer.
- **`used_in_job=unknown`** appears in 6 runs (`39689237254`, `39690520076`, `39683270126`, `39675217863`, `39673217184`, `39673215195`). In `gh_helpers.sh:66-72`, `used` stays unknown when the rate-limit reset window changed between job start and end, or when no start snapshot exists.
  - **Fix:** emit a separate line, e.g. `GH_PAT_BUDGET_NOTE reason=reset_rolled|no_start_snapshot`.
  - Do **not** append a field to `GH_PAT_BUDGET`. The collector regex at `collect_workflow_logs.py:109-113` is anchored with `$` and would stop matching.
- **Breaking down the ~280 codex-agent calls:** we can't attribute them by endpoint from this data.
  - **Recommendation:** add a per-endpoint counter inside `gh_retry` and print the top 5 at `phase=end` as a separate `GH_API_TOP` line.
  - Then check per-item lookups against the existing caches: `gh_review_pr_state` (job-local PR state, `gh_helpers.sh:79-83`) and the §14 batch helpers.
  - **Estimated reduction (inference):** 30–50% if PR state, comments or check-runs are being re-read in watchdog loops.
- **Heal intake** (74–77 calls against 23 in a lighter run): the number probably grows with the number of candidate issues. Check whether issue/PR status lookups can use one aliased GraphQL query, following the `_fetch_candidate_issue_details_graphql` pattern.
- **promote_candidate_follow:** every run probes `git/ref/tags/promote-cycle-frozen`, gets a 404, and logs it as `GH_API_RETRY outcome=permanent` (`39694346025`, `39693230942`, `39692005702`, `39686425065`, `39678638550`, `39670391714`). That is one call per run, and a missing tag is the normal state, so it inflates the "permanent error" count. Recommendation: treat a 404 at this call site as expected and suppress the retry-telemetry line (or log a distinct `PROMOTE_FREEZE_TAG state=absent`), keeping the `GH_API_RETRY` format unchanged everywhere else.

## Prompt Cache & Memory System

- **Overall:** `cache_hit_rate` 0.767 (12.95M read vs 3.32M uncached prompt). review_autofix 0.775, test_and_mark_stable 0.0.
- **Run level:**
  - `39604068731`: 0.805 hit rate, but 529,773 cache-write tokens. The run warmed its own cache.
  - `39689267631`: 0.727 hit rate with zero writes, so it reused a prefix written by an earlier run. Prefix stability across runs works, at least within the same SHA (`9b712bed`).
- **Fragmentation risks (inference):**
  - Workflow and support scripts move between SHAs several times a day (`9b712bed` → `df51fcdc` → `e7005fa8` per `PROMOTE_CANDIDATE_FOLLOW outcome=advanced`, `39692005702` and `39694346025`). If the prompt template or instruction files are embedded early in the prompt, every promote invalidates the shared prefix.
  - Per-run fields (run ID, timestamps, SHAs) placed before static instructions would fragment the cache in the same way.
- **CONTEXT_BUDGET_WARN:** 0, so prompt growth is not yet eating into cache value.
- **Recommendations:**
  1. Make test_and_mark_stable prefixes stable, or stop cache writes there (see Cost #2).
  2. Log a hash of the static prompt prefix per call (`PROMPT_PREFIX_HASH`), so the collector can tie cache misses to prefix changes.
  3. Keep dynamic content (diff, PR metadata, memory hits) after all static instruction blocks.
  - **Expected impact (inference):** +3–8 points of hit rate on reviews, i.e. about 100–250k fewer uncached tokens per large review run.

## Orchestrator Health

- **Poller:** 61 runs, 60 success, 1 cancelled. p50 628s, p95 957s. Midday outliers: `39604298172` 1,123s, `39614653867` 1,072s.
  - Every poll's log is cut by about 848K characters (e.g. `39692629272`, `39687239893`), so we can't see wave progression, deferrals or conflict-heal retries.
  - Runner queue before the poll started: up to 3.5 min (`39690716481`), 2m41s (`39683175108`), 2 min (`39689601717`), 98s (`39674426598`).
- **Clarify / plan / implement / respond:** mostly skipped (clarify 92/98, plan 85/90, respond 89/90, implement 75/90 other plus 8 cancelled). Only 1 orchestrate run (690s) and 1 validate run (725s) in the window. No clarification loops or stuck states are visible, but evidence is thin.
- **PR6576 shard follow-up guard** works: `PR6576_FOLLOWUP outcome=skip reason=marker_present issue=7072` (`39692005582`).
- **Historical triage dedup** works: `HISTORICAL_TRIAGE skip reason=already_filed issue=7300` (`39692005935`).
- **Merge train:** `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` in every scheduled cleanup seen. The recurring warning "Scheduled close cleanup preserved 3 active run(s) without pull-request linkage" (`39693339993`, `39691654684`, `39689788621`) shows the same 3 runs being kept across consecutive passes.
  - Add the run IDs and ages to that warning, so long-lived orphans can be spotted.
- **Indicators to track:**
  - poller wall time and queue time per run
  - a per-poll summary line (issues examined / dispatched / deferred) emitted at the end of the poll step, so it survives truncation
  - `examined/released` trend in `MERGE_TRAIN_RELEASE_SUMMARY`
  - count of preserved orphan runs
- **Smallest safe mitigation:** emit a compact `ORCH_POLL_SUMMARY` line as the last line of the poll step. Check first whether `orchestrate_poll_process.sh` already prints one and only the truncation hides it; if so, reuse it.

## Pipeline Flow Bottlenecks

| Stage | Queue | Compute | Retry | Merge/conflict |
|---|---|---|---|---|
| clarify/plan | small (mostly skipped, p50 2s) | plan p95 330s; `39602669800` 864s | none seen | n/a |
| implement | not measured | p95 981s; midday 1,269–1,936s | 8 cancelled | n/a |
| review/autofix | ~0.1–7s pickup (evening) | **p50 809s / p95 3,019s, the dominant cost** | 2–3 full passes per PR (7795, 7807, 7817, 7820, 7821) | stale-head skip path exists (`AUTOFIX_STALE_BASE_SKIP`) |
| CI | ~2.5 min in `39689237026` | p50 996s, long pole `tests-release-and-log-analysis` | none | n/a |
| orchestrate_poll | 35s–3.5 min | ~10 min per poll | none seen | n/a |
| post-merge (cancel/heal-reconcile, forward-merge) | small | heal-reconcile 70–135s of full fetch; forward-merge 143s (`39694097324`) | none | forward-merge `push_attempt=1` |

Fixes, ordered by end-to-end impact:
1. Diagnose midday contention (pool wait plus job queue time).
2. Measure, then reduce redundant review passes per PR.
3. Split the CI long pole.
4. Shallow-fetch heal-reconcile.
5. Reduce skipped-run fan-out. Move cheap `if:` gates into the caller workflows where possible so runs are never created; this must preserve the trigger contracts.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - review/autofix p95 3,019s, with midday runs up to 4,850s (`39606614099`)
  - CI p95 2,827s (midday up to 3,166s, `39602689033`)
  - poller about 10 min per cycle plus up to 3.5 min queue
- **Top failure modes:**
  - "Checkout PR head branch" failure with no annotation (`39600043919`)
  - 27 cancellations (heal intake 18, implement 8)
  - Semble silently unavailable in the poller
- **Highest-cost drivers:**
  - review/autofix (16.79M of 16.88M logged tokens; 278–286 GH calls per codex-agent job)
  - the cache-write-only pattern in test_and_mark_stable
  - unused Semble bootstraps (~103s) in workflow_log_analysis
- **Top 3 actions:**
  1. Add the `REVIEW_CHECKOUT_FAIL` reason trap to "Checkout PR head branch", plus an `agents.d` fragment for the new prefix.
  2. Add `CLAUDE_POOL_CAPACITY_WAIT waited_ms=` and per-job queue-time collection to find the cause of the midday slowdown.
  3. Switch heal-pr-reconcile to `fetch-depth: 1` and deepen only when heal issues exist.

## Metrics Appendix

**Run outcomes (1,000 runs, success sample rate 0.07)**

| Metric | Value |
|---|---|
| success / failure / cancelled / other | 486 / 1 / 27 / 486 |
| failure rate | 0.1% |
| p50 / p95 duration | 13s / 1,411s |
| avg duration | 272s |
| runs with log telemetry | 115 |

**Per-family (selected)**

| Family | Runs | Success | Fail | Cancel | Other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| review_autofix | 86 | 83 | 1 | 0 | 2 | 809 | 3,019 |
| ci | 48 | 48 | 0 | 0 | 0 | 996 | 2,827 |
| orchestrate_poll | 61 | 60 | 0 | 1 | 0 | 628 | 957 |
| workflow_failure_heal_intake | 138 | 74 | 0 | 18 | 46 | 130.5 | 434 |
| implement | 90 | 7 | 0 | 8 | 75 | 2 | 981 |
| clarify | 98 | 6 | 0 | 0 | 92 | 2 | 392 |
| plan | 90 | 5 | 0 | 0 | 85 | 2 | 330 |
| cancel_on_pr_close | 59 | 59 | 0 | 0 | 0 | 13 | 298 |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 0 | 7,965 | 7,965 |

**Tokens / cache (OpenRouter; Codex 0; Claude counters not present in the supplied rows)**

| Scope | Calls | Prompt | Cache read | Cache write | Completion | Total | cache_hit_rate |
|---|---|---|---|---|---|---|---|
| all | 34 | 3,318,735 | 12,946,871 | 610,222 | 85,637 | 16,880,278 | 0.767 |
| review_autofix | 22 | 3,235,466 | 12,946,871 | 529,773 | 81,594 | 16,792,966 | 0.775 |
| test_and_mark_stable | 12 | 83,269 | 0 | 80,449 | 4,043 | 87,312 | 0.0 |

**Review telemetry**

| Metric | Value |
|---|---|
| wall_clock_p50_ms (repo) | 16,000 |
| wall_clock_p99_ms (repo) | 4,792,180 |
| wall_clock_p50_ms / p99 (review_autofix) | 77,500 / 4,771,530 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**GH API (`GH_PAT_BUDGET used_in_job`)**

| Workflow/job | Observed |
|---|---|
| review_autofix / codex-agent | 278, 286, unknown |
| workflow-failure-heal-intake / intake | 77, 74, 23, unknown ×2 |
| review_autofix_sweep / sweep | 2, 2, 2, 8, 9, unknown ×3 |
| review_autofix / post-merge-* | 0, 2, 3, 3 |
| lowest remaining | 3,606 |
| 429 / secondary limit | 0 |

**Semble**

| Metric | Value |
|---|---|
| query calls / bytes | 3 / 50,710 (about 16.9 KB per query) |
| sources / static_dup_bytes | 22 / 0 |
| fallbacks total / contract-test / runtime | 56 / 56 / 0 (CI 52, test_and_mark_stable 4) |
| bootstraps / failed / unused | 6 / 0 / 3 (all unused in `39601437388`) |
| bootstrap ms total | 204,805 (review 101,823; log analysis 102,982) |

**Serena**

| Metric | Value |
|---|---|
| query calls / response bytes / tool calls / ms | 0 / 0 / 0 / 0 |
| fallbacks | 0 |

**Per-target MCP availability**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all targets) | 0 | 0 | 0 |
| semble (orchestrate_poll) | no probe lines; env `SEMBLE_AVAILABLE=false` | — | — |

**Other MCP servers observed:** none.

**Data gaps:**
- `/home/runner/work/_temp/workflow-log-output` was not mounted, so there were no deep-dive logs for `39600043919` or the slow runs.
- poll and codex-agent logs were truncated by 848K–1.66M characters.
- no `AI_MEMORY_TELEMETRY` retrieve lines.
- no Claude usage counters.
- `used_in_job=unknown` in 6 runs.
- 72 of the slow-run rows have no `cost_telemetry`.

**Actions taken:** read-only analysis of the supplied context plus the repo files listed in the checkpoint. No files were changed.
**Assumptions:** the timing bunching is read as contention (inference). Midday versus evening is compared on durations only, because there was no queue-time data.
**Missing context:** the deep-dive logs for the failed run, untruncated poller and review logs, and per-job queue times.