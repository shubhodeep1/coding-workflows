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
## Deep Audit — Workflows & Scripts (2026-10-22)

Scope and method: this pass reads the code paths behind this report's own findings: review checkout, heal-pr-reconcile, Claude capacity wait, Semble telemetry, GH_PAT budget, poller Semble, and expression and file-size headroom. It also checks those findings against the code. Findings already filed in earlier deep audits are referenced by file and ID instead of repeated: `analysis/workflow-optimization-2026-10-21.md`, `-21-2.md`, `-21-3.md` and `-21-4.md`. No files were modified.

### Section 1: Bug & Correctness Sweep

**BUG-001 — heal-pr-reconcile closes the heal PR and its issue on transient or infrastructure failures, not only on real conflicts**
- **File:** `scripts/workflow_failure_heal_pr_reconcile.sh:154-195, 265-273`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - `_retarget_heal` returns these reasons:
    - `no_git_checkout` (154-157)
    - `fetch_failed`, a plain `git fetch` with no retry (159-164)
    - `worktree_failed` (176-179)
    - `merge_conflict` for any `git merge` non-zero exit (189-193). That covers a real conflict, but also "refusing to merge unrelated histories", a lock file, or a missing object.
  - The caller leaves only `heal_branch_moved` and `push_state_unknown` alone (265-271). Every other reason reaches `_close_heal ... "source_merged_${RETARGET_FAIL_REASON}"` (272-273).
  - `_close_heal` closes the heal PR and closes the heal issue as `not_planned` (124, 133).
  - So one network blip during the fetch at 159 permanently closes a valid fix.
  - This matters more if this report's Speed #3 lands (`fetch-depth: 1` at `internal-cancel-on-pr-close.yml:50`). Today the full-history checkout supplies the merge base. Under a shallow clone, the merge at 189 may fail for history reasons and be filed as `merge_conflict` *(inference; [NEEDS VERIFICATION] of git's shallow-fetch behaviour for `refs/pull/<n>/head`)*.
- **Recommended fix:**
  - Classify the merge failure before choosing the reason:
    ```bash
    if [ -n "$(git -C "${merge_dir}" diff --name-only --diff-filter=U 2>/dev/null)" ]; then
    	RETARGET_FAIL_REASON="merge_conflict"
    else
    	RETARGET_FAIL_REASON="merge_failed"
    fi
    ```
  - At 265, switch to a `case` that leaves the PR open and warns for `fetch_failed|no_git_checkout|worktree_failed|merge_failed|heal_branch_moved|push_state_unknown`.
  - Close only for `merge_conflict|nothing_to_apply|base_branch_missing|push_rejected|base_update_failed`.
  - Add a 3-attempt backoff (2s/4s/8s) to the two fetches at 159 and 165, mirroring the fetch loop at `review_autofix.yml:2908-2923`.
  - Land this before (or with) any `fetch-depth` reduction.

**BUG-002 — Semble query telemetry is thrown away in workflow-log-analysis, so `cost_audit.py` reports used bootstraps as "unused"**
- **File:** `.github/workflows/workflow-log-analysis.yml:453, 1296, 1988, 2588`; `scripts/semble_helpers.sh:21-40, 387`; `scripts/cost_audit.py:879-880`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - All four prefetch sites call `semble_query_block ... 2>/dev/null || true` inside `$(...)`.
  - `_semble_log_event` writes every `SEMBLE_QUERY` / `SEMBLE_FALLBACK` line to **stderr** (`printf ... >&2` at 28, 34, 39). `semble_query_block` emits `SEMBLE_QUERY` at 387.
  - So these runs never log a query line. `cost_audit.py:879-880` then sets `semble_bootstraps_unused = semble_bootstraps` whenever `semble_query_calls == 0`.
  - This conflicts with the report's Executive Summary bullet ("3 Semble bootstraps went unused in `workflow_log_analysis` run `39601437388`") and its Cost #4 recommendation. The prompt for this deep-audit pass itself carried a populated `=== SEMBLE: Workflow Deep Audit Context ===` block, which means the query at 1988 ran and returned chunks.
  - The same redirect also hides any runtime `SEMBLE_FALLBACK` in this workflow. That would explain part of "runtime fallbacks are 0" *(inference)*.
- **Recommended fix:**
  - Drop the `2>/dev/null`, matching the other callers that keep stderr: `orchestrate_poll_process.sh:955`, `review_rb_judge.sh:896`, `implement.yml:5032`.
  - If stderr noise is a concern, filter instead: `2> >(grep -E '^SEMBLE_(QUERY|FALLBACK|BOOTSTRAP) ' >&2 || true)`.
  - Treat the report's Cost #4 "bootstrap eagerly but never query" as a telemetry artefact until re-measured.

**BUG-003 — "Checkout PR head branch" has more unannotated failure exits than the report lists**
- **File:** `.github/workflows/review_autofix.yml:2832-2966`; `scripts/git_ref_health_check.sh:71-78, 109-114`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** This extends the report's Reliability #1 (run `39600043919`). Beyond the paths listed there:
  - **2836-2837:** `HEAD_REF="$(jq -r '.headRefName' "${PR_META_FILE}")"` runs under `set -euo pipefail` *before* any other check. A missing or corrupt `pr_meta.json` (written by `review_autofix_step_init_runtime_workspace.sh:45` and `review_collect_pr_metadata.sh:256`) kills the step with only jq's stderr.
  - **2873-2874:** plain `echo "Unable to determine PR head branch"` with no `::error::`.
  - **2894:** `git_ref_health_check.sh repair` exits 1 on an unresolved suspicious ref (74-75, stderr, no annotation) and on remaining broken refs (110-113, **stdout**, no annotation).
  - **2964:** an unguarded `git rev-parse HEAD`.

  An `ERR` trap added mid-step, as Reliability #1 sketches, would miss 2836-2837 unless it is the first statement.
- **Recommended fix:**
  - Put the trap on the line right after `set -euo pipefail` (2834). Set `_rcf_reason=pr_meta_unreadable` before 2836.
  - Change 2873 to `echo "::error::Unable to determine PR head branch"`.
  - In `git_ref_health_check.sh`, prefix the two failure messages with `::error::[git-ref-health]` and send 110 to stderr.
  - Register the new prefix through an `agents.d/<pr>-review-checkout-fail.md` fragment (§25), as Reliability #1 already says.

**BUG-004 — `_close_heal` posts "Closing without merging" before it knows the close worked**
- **File:** `scripts/workflow_failure_heal_pr_reconcile.sh:117-127`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - `_comment "${heal_pr}" "${pr_detail}..."` (120-123) runs before `PATCH .../pulls/${heal_pr} state=closed` (124).
  - If the PATCH fails, the function logs `heal_pr_close_failed` and returns (125-126). The PR stays open with a comment saying it was closed, and a later reconcile run adds a second identical comment.
- **Recommended fix:** Swap the order: PATCH first, then comment only on success. The issue path at 129-134 already comments before closing and can stay as is, because issue closure is attempted regardless.

**BUG-005 — heal-pr-reconcile fails at checkout on fork PR closes, before the script's own fork guard runs** `[NEEDS VERIFICATION]`
- **File:** `.github/workflows/internal-cancel-on-pr-close.yml:26, 35, 45-50`; `scripts/workflow_failure_heal_pr_reconcile.sh:85-89`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - On `pull_request` events from forks, `secrets.GH_PAT` is empty, so `actions/checkout@v5` gets `token: ''`. *(Inference: checkout's `token` input is read as required; an empty value fails the step.)*
  - The checkout step has no `continue-on-error`, so the job fails red.
  - The script's `skip reason=fork_head` guard (85-89) is never reached.
- **Recommended fix:** Add `&& github.event.pull_request.head.repo.full_name == github.repository` to the job `if:` at line 26. That moves the fork guard ahead of checkout.

No new SEC findings in the files read. The raw `vars.*` written to `$GITHUB_ENV` at `orchestrate_poll.yml:753-754` is already covered by `-21.md` SEC-002. The PAT basic-auth header in git argv at `review_autofix.yml:355` is covered by `-21-3.md` SEC-001.

### Section 2: GitHub API Call Redundancy Audit

**API-001 — `gh label create` on an existing label is still retried (carried forward, plus an inline variant)**
- **File:** `scripts/gh_helpers.sh:192-195, 804-816, 1066-1069, 1099-1124`; `scripts/label_helpers.sh:175-191`; `.github/workflows/review_autofix.yml:1165-1192, 1408, 1445`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:**
  - **Shared helper (still unfixed):** `_is_gh_permanent_failure` (194) has no "already exists" pattern, and `_gh_cmd_is_unsafe_post` (811-814) does not list `label create`. So `gh_retry gh label create` on an existing label makes 5 attempts with 15s of sleep (1+2+4+8), before `label_helpers.sh:188` recognises "already exists". This is the same finding as `-21-3.md` API-001, which was RISKY_SKIP pending a stderr capture. The repo's own stubs already record the text: `tests/test_implement_post_codex_recovery.py:288`, `tests/test_workflow_log_analysis_failure_contract.py:217`.
  - **New, inline variant:** the deterministic-skip-merge job defines its own `gh_retry` (1165-1187: 4 attempts, 2/4/8s) and `ensure_label_exists` (1189-1192). It calls that for `ai:review-skipped` (1408) and `ai:ready-to-merge` (1445).
- **Calls today vs after fix:**
  - Shared helper: 5 calls and 15s → 1 call and 0s per call.
  - Inline variant: 4+4 = 8 calls and 28s of sleep → 2 calls per deterministic-skip run, or 0 with a cache.
- **Recommended fix:**
  - Add `already exists` to the `_is_gh_permanent_failure` regex. Callers that grep stderr keep working, because `gh_retry` re-emits it.
  - In the inline job, use `gh label create --force` (idempotent, one call). Or skip `ensure_label_exists` and rely on the label POST at 1409, which creates missing labels on GitHub's side.
  - Extend the per-process cache pattern (`_ENSURED_LABELS_CACHE`, `orchestrate_poll_process.sh:4008-4016`) into `label_helpers.sh`.

**API-002 — codex-agent reads `GET /pulls/{n}` twice before the reviewers start** `[NEEDS VERIFICATION]`
- **File:** `.github/workflows/review_autofix.yml:2229-2253` (call at 2240); `scripts/review_collect_pr_metadata.sh:231`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - "Check PR state (defense-in-depth)" fetches the full PR object (`gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}"`) only to read labels and state.
  - A few steps later, `review_collect_pr_metadata.sh:231` fetches the same object into `PR_PAYLOAD_FILE`.
  - The gate job has already read it as well (`needs.gate.outputs.head_sha` / `head_ref`).
- **Calls today vs after fix:** 2 → 1 per codex-agent job (about 86 calls a day at the report's run rate).
- **Recommended fix:**
  - Have the "Check PR state" step write its response to `${RUNTIME_DIR}/pr_state_probe.json` with a timestamp.
  - Let `review_collect_pr_metadata.sh` reuse it when it is less than 120s old, the same freshness bound as `gh_review_pr_state` (`gh_helpers.sh:110`). Otherwise fall back to the live read.
  - Verification needed: the stale-head check at `review_autofix.yml:2850` compares `PR_PAYLOAD_FILE .head.sha`, and reuse must not widen that window.

**BATCH-001 — per-linked-issue label POSTs in three loops**
- **File:** `.github/workflows/review_autofix.yml:1446-1453`; `scripts/review_autofix_step_mark_ready_to_merge.sh:77-81`; `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:57-61`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** Each loop makes one `POST /issues/{n}/labels`, or one `set_issue_phase_label_resilient` that can make several calls, per linked issue. That is N calls per run. N is usually 1–3.
- **Calls today vs after fix:** N → 1 aliased GraphQL `addLabelsToLabelable` mutation, plus 1 label-ID lookup. That only pays off at N ≥ 3. The REST path through `set_issue_phase_label_resilient` also removes stale phase labels, which a plain add does not.
- **Recommended fix:** Leave it as is unless N ≥ 3 shows up in the logs. If it does, follow the aliased-batch shape of `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:196-256`) and keep the REST path as the fail-open fallback.

The per-issue PR lookup in heal-pr-reconcile (`workflow_failure_heal_pr_reconcile.sh:239-247`) is already filed as `-21-4.md` API-006. The `promote-cycle-frozen` 404 telemetry is covered by this report's GH API section.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — 19 inline `gh api rate_limit` budget blocks duplicate `gh_pat_budget()`**
- **File:**
  - `scripts/gh_helpers.sh:31-77` (shared helper)
  - Inline copies in `.github/workflows/review_autofix.yml` (12): 386, 736, 770, 966, 993, 1058, 1131, 1467, 1534, 1795, 1970, 5738
  - Inline copies in other workflows (7): `clarify.yml` 82, 2335, 2422; `workflow-failure-heal-intake.yml` 146, 394; `orchestrate_poll.yml` 109; `validation-improvements-intake.yml` 47
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - The inline blocks read `.resources.core` from the response body. `gh_pat_budget` prefers the `x-ratelimit-*` headers when `x-ratelimit-resource: core` (52-59).
  - In clarify, heal-intake and orchestrate_poll, the *start* is inline (body) and the *end* is `gh_pat_budget` (headers). So `used_in_job` mixes two sources.
  - Any change to the `GH_PAT_BUDGET` line format, such as the report's `GH_PAT_BUDGET_NOTE` idea, has to be made in 20 places.
  - In the review gate job, `gh_helpers.sh` is already bootstrapped (`GH_API_RETRY_HELPERS`, 331-379) before the inline start step at 381.
- **Recommended fix:**
  - Owner: `scripts/gh_helpers.sh` `gh_pat_budget <start|end> <workflow> <job> <snapshot_file>`, unchanged.
  - Replace each inline body with: `source "${GH_API_RETRY_HELPERS:-scripts/gh_helpers.sh}" 2>/dev/null && gh_pat_budget start review_autofix gate "${GH_PAT_BUDGET_FILE}" || echo 'GH_PAT_BUDGET phase=start ... used_in_job=unknown'`. This is the shape already used at `clarify.yml:2278-2283`.
  - Keep the inline copy only for steps that run before any helper is staged (`orchestrate_poll.yml:109`, per its comment at 107-108).
  - This also shrinks `review_autofix.yml` (§24).

**DUP-002 — workflow-log-analysis repeats the Semble install/index/prefetch sequence four times**
- **File:** `.github/workflows/workflow-log-analysis.yml`
  - Setup steps: 357-382, 948-993, 1810-1855, 2409-2454
  - Prefetch blocks: 446-460, 1286-1303, 1977-1999, 2577-2599
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - Each AI-pass job runs its own `Setup uv` / `Install semble` / `Build semble index`, about 34s per bootstrap according to the report.
  - Each job also has a near-identical prefetch block that differs only in the query file and header label.
  - These query sites gate on `SEMBLE_INDEX_AVAILABLE == true`, so they cannot use lazy mode. The poller already defaults to lazy (`orchestrate_poll.yml:49`) and gates through `semble_should_query` (`orchestrate_poll_process.sh:944-948`).
  - Partial overlap with `-21-2.md` DUP-001, which covers the AI-pass bodies but not the Semble steps.
- **Recommended fix:**
  - New `scripts/wla_semble_prefetch.sh <query_file> <header_label>`. It sources `semble_helpers.sh`, gates on `semble_should_query` with a fallback to the `SEMBLE_INDEX_AVAILABLE` check, prints the block, and keeps stderr (BUG-002).
  - Set `SEMBLE_BOOTSTRAP_MODE: lazy` at workflow level, and gate the eager steps with `&& env.SEMBLE_BOOTSTRAP_MODE != 'lazy'`, as at `orchestrate_poll.yml:925`.
  - Update the callers at the four prefetch sites.

### Section 4: Expression Size Limit Risk Assessment

There is no `wc` in this sandbox. Sizes below are estimated from line counts and line-length histograms (`Grep` counts by length band). `tests/test_workflow_file_size_limit.py:66-98` already fails CI at 18,000 characters for any `run:` body containing `${{`, so no High-band body can exist on main. Only the Medium band (15,000–18,000) and growth risk are in play.

**EXPR-001 — orchestrate_poll "Stage workflow support files" is an interpolated body in or near the Medium band** `[NEEDS VERIFICATION]`
- **File:** `.github/workflows/orchestrate_poll.yml:605-897`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The ~290-line body contains three `${{ }}` expressions: 753 and 754 (`vars.PROMPT_PRELUDE_REFACTOR_ENABLED`, `vars.UNATTENDED_IDENTITY_REINJECT_ENABLED`) and 875 (`github.repository`). Because of them, the whole body counts as one expression.
  - It includes one ~1,000-character staging list (614) and several 200–420-character lists (736, 780, 796).
  - **Estimated size:** ~14,000–17,000 characters, leaving ~4,000–7,000 of the 21,000 limit and possibly under 1,000 before the 18,000 CI guard.
  - Every new staged script grows line 614.
- **Recommended fix:**
  - Move the three values into the step's `env:` block, e.g. `PROMPT_PRELUDE_REFACTOR_ENABLED_INPUT: ${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED || 'false' }}`.
  - Use `${GITHUB_REPOSITORY}` at 875.
  - With no `${{` left, the body drops out of expression scope entirely. That is cheaper than extracting a script and needs no new file.

**EXPR-002 — review_autofix Telegram steps interpolate only for URLs**
- **File:** `.github/workflows/review_autofix.yml:5041-5185` (interpolations at 5051-5052, 5111, 5162, 5184); `.github/workflows/review_autofix.yml:5187-5349` (5204-5205, 5209)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - Estimated ~10,000–12,000 and ~11,000–13,000 characters, so ~8,000–10,000 of headroom.
  - The judge-decision `case` gains a branch with each new judge outcome, which keeps growing it.
  - The only interpolations are `github.server_url`, `github.repository` and `github.run_id`.
- **Recommended fix:** Use the default runner env vars `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. That removes both bodies from expression scope with no change in behaviour.

**EXPR-003 — small interpolated bodies that are about to grow**
- **File:** `.github/workflows/review_autofix.yml:2832-2966` (2888); `.github/workflows/review_autofix.yml:3951-4093` (4023)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - "Checkout PR head branch" is about 7,000 characters, with one `${{ github.repository }}`. The trap and reasons proposed in Reliability #1 and BUG-003 will add to it.
  - The resolver step uses `"${{ github.repository }}"` once inside its body.
  - Headroom is large (~14,000 characters).
- **Recommended fix:** Replace both with `${GITHUB_REPOSITORY}` while making the BUG-003 edit.

**EXPR-004 — whole-file headroom** `[NEEDS VERIFICATION]`
- **File:** `.github/workflows/test-and-mark-stable.yml` (7,714 lines); `.github/workflows/review_autofix.yml` (5,749 lines)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - **Estimates from the histograms:**

    | File | Lines | Estimated size | Plausible range |
    |---|---|---|---|
    | `test-and-mark-stable.yml` | 7,714 | ~430 KB | 260–577 KB |
    | `review_autofix.yml` | 5,749 | ~370 KB | 232–490 KB |

  - `test-and-mark-stable.yml` may sit within ~50 KB of the 480,000-byte guard (§24).
  - No workflow is close to 800 KB. Note that §24's 512,000-byte limit, not the prompt's 1 MB, is the binding one; see `-21-2.md` CONSIST-004. Whole-file sizes are tracked in `-21-4.md` EXPR-003.
- **Recommended fix:**
  - Run `wc -c .github/workflows/*.yml` in the next CI log (the guard test already prints the largest file).
  - Apply DUP-001, which removes ~12 inline blocks from `review_autofix.yml`.
  - If `test-and-mark-stable.yml` is above 430,000 bytes, move its largest non-interpolated phase-wait bodies into `scripts/` (`-21-3.md` DUP-007).

### Section 5: Cross-Cutting Concerns

**CONSIST-001 — the report's capacity-wait diagnostic assumes a blocking wait that does not exist and duplicates a registered prefix**
- **File:** `scripts/ai_engine.sh:141-149, 263-301`; `scripts/review_autofix_step_claude_capacity_wait.sh:23-38`; `agents.md:4291`; `scripts/collect_workflow_logs.py`; `scripts/cost_audit.py`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:**
  - Speed #1 proposes a new `CLAUDE_POOL_CAPACITY_WAIT role= waited_ms= outcome=` line "wherever `capacity_wait` blocks (`ai_engine.sh` ~225/274)". In the code, `ai_engine_fallback` never blocks:
    - With the wait on, it sets `AI_ENGINE_FALLBACK_EXIT=80`, logs `AI_ENGINE_CAPACITY_WAIT role= reason=` and returns (274-276, 296-300).
    - The review step then *fails* with `AUTOFIX_FAILURE_REASON=claude_capacity` (`review_autofix_step_claude_capacity_wait.sh:35-38`).
    - `ai_engine.sh`, `claude_engine.py`, `claude_anthropic_relay.py` and `claude_pool_token.sh` contain no `sleep` / `time.sleep`.
  - So candidate (b) cannot stretch run duration. It shortens runs and defers them to the hourly resume pass.
  - The prefix `AI_ENGINE_CAPACITY_WAIT` is already registered (`agents.md:4291`). Neither `collect_workflow_logs.py` nor `cost_audit.py` parses it (no matches).
- **Recommended fix:**
  - Drop the new prefix.
  - Teach `cost_audit.py` to count the existing `AI_ENGINE_CAPACITY_WAIT ... outcome=deferred` lines per family.
  - Put the contention diagnosis effort into per-job queue time. The collector currently derives duration only from `run_started_at → updated_at` (`collect_workflow_logs.py:1123-1127`), with no job-level `created_at → started_at`.

**CONSIST-002 — poller Semble "silently unavailable" is lazy mode working as designed**
- **File:** `.github/workflows/orchestrate_poll.yml:49, 96, 924-985`; `scripts/orchestrate_poll_process.sh:941-948`; `scripts/semble_helpers.sh:251-258`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - `SEMBLE_BOOTSTRAP_MODE` defaults to `lazy` (49).
  - "Create runtime workspace" seeds `SEMBLE_AVAILABLE=false` (96).
  - The eager install and index steps run only when `has_work == 'true' && SEMBLE_BOOTSTRAP_MODE != 'lazy'` (925, 930, 953).
  - Queries go through `semble_should_query`, and a failed lazy bootstrap already logs `SEMBLE_BOOTSTRAP ... state=failed` (`semble_helpers.sh:254/258`).
  - The report's Reliability #3 fix (a poll-start `SEMBLE_FALLBACK ... reason=unavailable` when the flag is on and the binary is missing) would fire on every tick. It would turn the "0 runtime fallbacks" metric into noise.
- **Recommended fix:** Do not emit at poll start. If visibility is wanted, emit one `SEMBLE_BOOTSTRAP mode=lazy state=not_needed` line from the poll summary when no judge query ran. Leave the fallback count to the real failure path.

**CONSIST-003 — heal-pr-reconcile runs `main`'s script while its sibling job is pinned to `promote-candidate`**
- **File:** `.github/workflows/internal-cancel-on-pr-close.yml:16, 45-54`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - The `cancel` job calls `cancel_on_pr_close.yml@promote-candidate`, whose support ref follows the protected candidate (`cancel_on_pr_close.yml:36-53`).
  - `heal-pr-reconcile` checks out `ref: main` and runs `scripts/workflow_failure_heal_pr_reconcile.sh` from it. That bypasses the promote-cycle pin (`docs/plans/pin-promote-cycle-to-candidate-plan.md`).
  - The impact is contained only because the step is `continue-on-error: true`.
- **Recommended fix:** Fetch only the script from the same ref the cancel job resolves (the `SUPPORT_REF source=promote_candidate` logic). Or move the job into `cancel_on_pr_close.yml` behind a source-repo `if:`. Combine this with the report's shallow-fetch change and BUG-001.

**CONSIST-004 — heal intake fetches job logs with raw `gh api` and no retry**
- **File:** `scripts/workflow_failure_heal_intake.sh:1205-1227`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - The call is deliberately not wrapped in `gh_retry`, because that would buffer unredacted bytes to a temp file (comment at 1209-1210).
  - As a result, any transient 5xx records `(job log unavailable)` (1225-1226). That removes the error signature the downstream dedup gate relies on.
- **Recommended fix:** Wrap the existing pipeline in a 3-attempt loop with 2s/4s backoff, re-running `gh api ... | python3 "${HEAL_PY}" redact-stream > "${RAW_LOG}"` each time. Redaction still happens before the first write, and the retry contract matches `gh_helpers.sh` transient handling.

**DEBT-001 — stale reviewer-scoping note and mismatched defaults**
- **File:** `scripts/review_autofix_step_iteration_summary.sh:74`; `scripts/review_run_reviewers.sh:2320-2349`; `.github/workflows/review_autofix.yml:1896`
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:**
  - The summary says `iteration_scoping=... (plumbed only on this branch; reviewer scope remains full-diff)`. But `review_run_reviewers.sh:2335-2337` scopes follow-up iterations from `LAST_RUN_CHANGED_FILES_FILE`.
  - Defaults disagree: the workflow uses `'1'` (1896), the reviewer script `:-0` (2321) and the summary `:-1` (74).
  - The report's Speed #2 asks whether later passes re-review unchanged files. `review_run_reviewers.sh:2346-2349` already prints `Reviewer iteration scoping: scoped.` / `full-diff (<reason>)` every run.
- **Recommended fix:**
  - Rewrite line 74 to report the actual `REVIEWER_SCOPE_REASON`.
  - Align the script default with the workflow (`:-1`), or document why the script stays fail-closed at `0` (§8).
  - Have the collector parse the existing `Reviewer iteration scoping:` line instead of adding `review_iteration=` / `changed_files=` fields.

**DEAD-001 — unreachable branches and an unused required input**
- **File:** `scripts/gh_helpers.sh:83-118`; `scripts/workflow_failure_heal_pr_reconcile.sh:62, 81`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:**
  - `gh_review_pr_state` accepts `merged` (91, 108). REST `GET /pulls/{n}` `.state` is only `open` or `closed`, so that branch never matches.
  - In reconcile, `HEAD_SHA` is validated as 40-hex (81) and never used afterwards. Retargeting uses `refs/pull/<n>/head` (150, 160). A missing `SOURCE_HEAD_SHA` still skips the whole reconcile as `missing_context`.
- **Recommended fix:**
  - Drop `merged` from the two regexes, or derive it from `.merged_at` as `review_autofix.yml:2245` does.
  - Either use `HEAD_SHA` to confirm `refs/pull/<n>/head` matches the closed head, or remove it from the required-input guard (keep the env name per §10).

**SHELL-001 — `ls | wc -l` file counting (SC2012)**
- **File:** `.github/workflows/review_autofix.yml:5129-5130`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `editor_attempts="$(ls -1 .../editor_attempt_*.err | wc -l ...)"` and `ls -1 ... | sort -V | tail -n 1` parse `ls` output. This is safe with today's file names, but it is flagged by SC2012.
- **Recommended fix:** Use `shopt -s nullglob; attempt_files=("${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err); editor_attempts="${#attempt_files[@]}"`, then pick the last file from the sorted array.

No `TODO`/`FIXME`/`HACK`/`XXX` markers exist in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 7 | BUG-001, BUG-002, BUG-003, API-001, DUP-002, EXPR-001, CONSIST-001 |
| Low | 14 | BUG-004, BUG-005, API-002, BATCH-001, DUP-001, EXPR-002, EXPR-003, EXPR-004, CONSIST-002, CONSIST-003, CONSIST-004, DEBT-001, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | None (no Critical/High findings) | Small |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `.github/workflows/review_autofix.yml`, `scripts/review_collect_pr_metadata.sh` | Small |
| Code modularization | `.github/workflows/review_autofix.yml`, `clarify.yml`, `workflow-failure-heal-intake.yml`, `orchestrate_poll.yml`, `validation-improvements-intake.yml`, `workflow-log-analysis.yml`, new `scripts/wla_semble_prefetch.sh` | Medium |
| Expression size reduction | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/test-and-mark-stable.yml` (if over 430 KB) | Small |
| Medium/Low fixes | `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/internal-cancel-on-pr-close.yml`, `.github/workflows/workflow-log-analysis.yml`, `scripts/git_ref_health_check.sh`, `scripts/cost_audit.py`, `scripts/collect_workflow_logs.py`, `scripts/review_autofix_step_iteration_summary.sh`, `scripts/review_run_reviewers.sh`, `scripts/workflow_failure_heal_intake.sh`, `scripts/gh_helpers.sh`, `agents.d/` fragment | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-22)

### Safety Tag Legend
`SAFE_TO_MERGE` means every merge precondition was confirmed by reading the code: same endpoint or a strict field subset, same pagination, filters and auth, same function or step, no mutation in between, unchanged error and retry handling, no concurrency boundary crossed, and any cache contract kept. The implement stage may act on these without further review. `NEEDS_VERIFICATION` means the change looks correct, but at least one precondition could not be proven from static reading; the listed checks must pass before anyone implements it. `RISKY_SKIP` means a hard trigger applies (retry or poll loop, pagination, stall-recovery or race-defense path, auth or identity flow, or a change to log keys). These are listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001 — The review-blocked judge reads `GET /pulls/{n}` and then runs a GraphQL query that returns the same base ref** — `RISKY_SKIP`
- **Files and lines:**
  - `scripts/review_rb_judge.sh:1071-1073` (REST PR read)
  - `scripts/review_rb_judge.sh:1114-1122` (GraphQL linked issues plus `baseRefName`)
  - `scripts/review_rb_judge.sh:1139` (title/body fallback)
  - Later re-reads: `:1927`, `:2228` (inside the mergeable poll loop), `:2345`, `:2762` (a second poll loop)
- **Endpoints:** `GET /repos/{owner}/{repo}/pulls/{n}`; `POST /graphql` (`pullRequest{baseRefName closingIssuesReferences{…}}`)
- **Current call count:** 2 at judge entry (1071 + 1114). On the merge path, add 1 (1927) and 1–6 (the 2228 loop, `PR_MERGEABLE_POLL_ATTEMPTS` default 6).
- **Proposed call count:** 1 at entry, by folding `state`, `merged`, `isDraft`, `title`, `body` and `headRefOid` into the 1114 GraphQL query. Every later re-read stays as it is.
- **Evidence:** the base ref is fetched twice. The GraphQL call selects `baseRefName`, and its fallback reads the REST payload already in memory:
  ```bash
  _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
  ...
  PR_BASE_REF="$(printf '%s' "${RB_LINKED_ISSUES_GRAPHQL_JSON}" | jq -r '.data.repository.pullRequest.baseRefName // ""' 2>/dev/null || true)"
  if [ -z "${PR_BASE_REF}" ]; then
  	PR_BASE_REF="$(printf '%s\n' "${_pr_meta}" | jq -r '.base.ref // ""' 2>/dev/null || true)"
  ```
- **Proposed fix (not authorized):** extend the 1118 query with the PR scalar fields and derive `_pr_state`, `_pr_merged` and `_pr_draft` from it. Keep the REST read only as a fallback when the GraphQL call fails.
- **Safety rationale:** this is a stall-recovery judge. The two calls fail differently: REST `_safe_gh_jq` falls back to `'{}'`, GraphQL to `|| true`. `_pr_meta` gates the `RB_JUDGE_DRAFT_SKIP` log key (1099). The later re-reads (1927, 2228, 2762) defend against merges and pushes that land during the LLM judge call.
- **Downstream signal:** do not auto-implement. A human must confirm that collapsing the REST and GraphQL failure modes cannot turn a REST-only outage into a silent "not draft / open" decision. The draft-skip and merge-guard tests must also be updated alongside the change.

**MERGE-002 — codex-agent reads the PR's issue comments up to three more times after `Collect PR metadata` cached them** — `RISKY_SKIP`
- **Files and lines:**
  - `scripts/review_collect_pr_metadata.sh:235-236` (paginated, written to `PR_ISSUE_COMMENTS_FILE`)
  - `scripts/review_rb_judge_security_pass.sh:210-212` (`--paginate --slurp`)
  - `scripts/pr_checks_lib.sh:997-999` (`_pr_base_sync_count`, reached from `_pr_base_fresh_for_merge` at `:1147` on the overlap path)
  - Gate job's clipped copy: `scripts/review_autofix_step_evaluate_gate.sh:343-347`
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{n}/comments?per_page=100` (paginated)
- **Current call count:** P pages at metadata collection, plus P in the RB security hold, plus P per overlap-path freshness check. Up to 2 freshness checks per merge attempt: `review_autofix.yml:1356/1367`, `review_rb_judge.sh:2258/2271`.
- **Proposed call count:** unchanged (recommended). The theoretical minimum is P, reusing `PR_ISSUE_COMMENTS_FILE`.
- **Evidence:** the precedent for reuse already exists. The single-issue security gate reads the cached file instead of calling the API (`review_single_issue_security_pass.sh:320`: `local pr_json="${PR_PAYLOAD_FILE:-}" comments="${PR_ISSUE_COMMENTS_FILE:-}"`). The other two readers fetch live, and `pr_checks_lib.sh:987-990` documents that choice.
- **Safety rationale:** every re-read is paginated. Each looks for markers that may have been posted after the 235 snapshot: `ai:single-issue-security-pass-blocked:v1` and `ai:merge-base-sync:v1`, the latter written by the update-branch path in the same run. These are race-defense reads.
- **Downstream signal:** do not auto-implement. Reuse would need an append-on-POST cache layer that every comment writer in the job invalidates. That design needs manual review of every comment-posting step between 2655 and 4413.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — Heal intake reads `/user` again for `HEAL_TRUSTED_AUTHOR` after `PROVENANCE_LOGIN` already holds the same identity** — `NEEDS_VERIFICATION`
- **Files and lines:**
  - `scripts/workflow_failure_heal_intake.sh:952-955` (first `/user` read)
  - `scripts/workflow_failure_heal_intake.sh:1427-1434` (second read)
  - Existing reuse precedent: `scripts/workflow_failure_heal_intake.sh:1593-1598`
- **Endpoint:** `GET /user`
- **Current call count:** 2 per `autofix_failure` intake with a numeric `SOURCE_GEN`. For `phase_failure` the second read is already skipped, because `PHASE_COMMENT_AUTHOR="${PROVENANCE_LOGIN}"` at 1267.
- **Proposed call count:** 1.
- **Evidence:** both reads are at script top level, use the same `GH_TOKEN` (the only `GH_TOKEN` unsets are in sub-process `env -u` at 2114/2122), and have no mutation between them. Scope recovery already does this reuse:
  ```bash
  recovery_login="${PROVENANCE_LOGIN:-}"
  [ -n "${recovery_login}" ] || recovery_login="${HEAL_TRUSTED_AUTHOR:-}"
  ```
  The 1431-1432 comment ("the only other /user read is for self-repo phase reports") is wrong: line 953 also reads `/user` for `autofix_failure`.
- **Proposed fix:** before the live read at 1433, add `[ -n "${HEAL_TRUSTED_AUTHOR}" ] || HEAL_TRUSTED_AUTHOR="${PROVENANCE_LOGIN:-}"`. Keep the live `gh_retry gh api --method GET user` as the fallback when the variable is still empty, and correct the comment.
- **Safety rationale:** endpoint, auth and scope match, and the fail-open fallback is kept. It is not `SAFE_TO_MERGE` for two reasons. The two reads use different jq filters (`--jq .login` at 954 versus `.login // empty` at 1433), so error or null output could differ. And the value feeds a trusted-author budget gate (`--trusted-author`, 1443).
- **Downstream signal:** verify three things before implementing:
  1. A `/user` failure under `gh_retry` at 954 can never leave `PROVENANCE_LOGIN` set to a non-login string such as `null` or an error body. If it can, normalise it with `.login // empty` first.
  2. `SOURCE_GEN` can be numeric when `SOURCE_KIND=autofix_failure`.
  3. The heal-intake tests that stub `user` still pass with one fewer stub hit.

**REUSE-002 — Up to 5 `GET /user` reads in review jobs although the gate already exports the token's login** — `NEEDS_VERIFICATION`
- **Files and lines:**
  - Producer: `scripts/review_autofix_step_evaluate_gate.sh:334` → `:1371` (`fingerprint_cap_marker_author_login`) → `.github/workflows/review_autofix.yml:328` (job output)
  - Existing consumers of that output: `review_autofix.yml:1521` (fingerprint-cap-block) and `review_autofix.yml:5686` (`AUTOFIX_STREAK_AUTHOR_LOGIN`)
  - Live re-reads:
    - `scripts/review_merge_train.sh:792` (gate step at `review_autofix.yml:3250`)
    - `scripts/review_conflict_prepare.sh:1122/1124` (integration-sync only)
    - `scripts/review_single_issue_security_pass.sh:370` (gate mode, step `review_autofix.yml:4172`)
    - `scripts/review_rb_judge_security_pass.sh:210`
    - `scripts/pr_checks_lib.sh:997` (per overlap-path freshness check; also sourced by deterministic-skip-merge at `review_autofix.yml:1220`)
- **Endpoint:** `GET /user`
- **Current call count:** 1 in the gate, plus 0–5 or more in codex-agent and deterministic-skip-merge, depending on which features fire.
- **Proposed call count:** 1 (the gate's read) whenever the gate output is non-empty. Each consumer keeps its live read as the fallback.
- **Evidence:** the comments behind these live reads are out of date:
  ```bash
  # The gate job also probes /user, but its identity is not exported to this job.
  SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
  ```
  `pr_checks_lib.sh:989-990` makes the same claim ("the review gate's own comments read happens in another job and cannot be shared"). Lines 328 and 5686 show the login *is* exported and already used in codex-agent.
- **Proposed fix:** expose `needs.gate.outputs.fingerprint_cap_marker_author_login` to the consuming steps under a new, collision-checked env name (§10). In each script, prefer that env value when it matches the existing login regex (`^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$`), and otherwise fall back to the current live read. Do not change `_mt_resolve_automation_login`'s caller contract.
- **Safety rationale:** this crosses the gate → codex-agent job boundary (sequential `needs`, not parallel). The gate fills the login only when `gate_fetch_marker_comments` actually ran (call sites at 411, 621, 657, 841, 1218 and 1294 are all conditional). Token sources differ: the gate uses `secrets.GH_PAT` (616), while the security pass uses `secrets.GH_PAT || github.token` (4187). The value also authenticates trusted markers, which makes it a trust-boundary input.
- **Downstream signal:** verify three things:
  1. With `GH_PAT` unset, the gate output is empty, so every consumer keeps its live read and `SECURITY_PASS_AUTHOR_LOGIN_FALLBACK` path.
  2. No consumer runs under a token other than `secrets.GH_PAT` when the gate output is non-empty. Check the `GH_TOKEN` of the steps at 3250, 3910/3951, 4172 and 4324, and of the deterministic-skip-merge step at 1140.
  3. `pr_checks_lib.sh` keeps its live read for non-review callers.

**REUSE-003 — `_pr_base_fresh_for_merge` re-reads the paginated `pulls/{n}/files` list after a required-checks wait on the same bound head** — `RISKY_SKIP`
- **Files and lines:**
  - Call pairs: `review_autofix.yml:1356` and `1367`; `review_autofix.yml:1319` and `1330`; `scripts/review_rb_judge.sh:2258` and `2271`
  - Each goes through `scripts/pr_checks_lib.sh:1139` → `_pr_base_freshness` → `:884`
  - The same list is also read at `scripts/review_autofix_step_evaluate_gate.sh:718` (gate job) and `scripts/review_rb_judge.sh:3288`
- **Endpoints:** `GET /repos/{owner}/{repo}/pulls/{n}/files?per_page=100` (paginated); `GET /compare/{head}...{base}` (must stay live)
- **Current call count:** 2 × P pages per merge attempt when the base moved (the second only when `PR_CHECKS_WAIT_WAITED_S > 0` and `0 < ahead_by`, with fewer than 300 base files).
- **Proposed call count:** P, by caching keyed on `head_sha`. The compare call is still re-read.
- **Evidence:** both calls receive the same `head_sha` (`PR_HEAD_SHA` or `RB_JUDGED_HEAD_SHA`), and the merge is bound with `--match-head-commit`, so the PR-side path list is expected to be the same.
- **Safety rationale:** the call is paginated (`--paginate --slurp`, plus a 3,000-file cap check at 907). It sits on a merge race-defense path that deliberately re-validates after a wait of up to 45 minutes. A base retarget during the wait can change the PR-side diff.
- **Downstream signal:** do not auto-implement. A human needs to confirm whether a retarget (`retarget_merged_base.sh`) during the wait can change `pulls/{n}/files` for an unchanged head, and must review the page-boundary and cap semantics before introducing any per-head cache.

### Dead Calls (DEAD-API-###)
No findings. Checked and rejected:
- `review_autofix_step_mark_ready_to_merge.sh:62`, `review_autofix_step_mark_review_blocked_workflow_failure.sh:43` and `review_autofix.yml:4485`: live PR-text reads that fire only when `PR_META_FILE` is blank or missing. They are guarded fallbacks, not dead code.
- `workflow_failure_heal_intake.sh:2237`: branch-existence probe whose exit code is consumed.
- `workflow_failure_heal_intake.sh:1141-1145` and `1164-1167`: already reuse the provenance and bind snapshots.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — confirmed `_is_gh_permanent_failure` (`gh_helpers.sh:194`) has no `already exists` pattern. Adding one changes retry classification for every `gh_retry` caller. In the inline variant, `gh label create --force` would overwrite a label's color and description, which is an observable change. Keep it pending the stderr capture that `-21-3.md` asked for.
- API-002: NEEDS_VERIFICATION — new detail: between the probe (`review_autofix.yml:2240`) and `review_collect_pr_metadata.sh:231` run OS-dependency install (2366), workspace cache restore (2440) and the Semble install and index build (2500-2575, ~34s per bootstrap). A 120s freshness bound will often miss. The probe is a defense-in-depth liveness check and must stay live, so only collect-metadata may reuse it, never the reverse. The gate's `pr_labels` (`evaluate_gate.sh:110`) is not exported and would not save a call anyway, because state still needs the live read.
- BATCH-001: NEEDS_VERIFICATION — agree with leaving it unchanged. `set_issue_phase_label_resilient` also strips stale phase labels, which a GraphQL `addLabelsToLabelable` batch does not. Confirm from logs that N ≥ 3 before considering it.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 3 | MERGE-001, MERGE-002, REUSE-003 |

The counts cover only findings new in this pass. The Deep Audit IDs are tagged in the cross-reference list above: API-001 RISKY_SKIP, API-002 NEEDS_VERIFICATION, BATCH-001 NEEDS_VERIFICATION.

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.