## Executive Summary

The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted for this run, so no `errors/`, `slow/` or `recent/` step logs could be read. Evidence comes from `analysis_context.json`, the `log_summary` fields, and the repository code. `insufficient_data` is `false`.

- **The merge-conflict resolver failed 17 times in a row on two forward-merge PRs and was redispatched each time with nothing changed.** All 17 real `review_autofix` failures stopped at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`. That is 9 on PR #7794 (6,267s) and 8 on PR #7795 (5,039s), from 09:18 to 12:00, totalling **11,306 runner-seconds (about 3.1h)**.
  - The repo already holds the matching fix (#7796, `scripts/review_conflict_prepare.sh:378-389`), which keeps the modify/delete copies of `agents.d`/`changelog.d` fragments on `auto/forward-merge-stable-*` heads. Both PRs then passed: runs 39599362428 (453s) and 39599347515 (498s), each with `AUTOFIX_EDITS_PUSHED: true`.
  - **What is missing is a no-progress breaker and a reason code.** The sweep run 39596230401 logged `AUTOFIX_SWEEP_DISPATCH pr=#7794`, and the backfill classified both PRs as `retryable`.
  - **Impact:** about 3h of runner time per incident like this. **Confidence:** high for the pattern; medium that #7796 is the root-cause fix, since step logs were unavailable.
- **40 of the 57 failures (70%) are zero-second runs with no jobs and no logs, not real failures.** Every one of them has `duration_seconds=0` and `job_name=null`, the run is named after the workflow file path, and the log archive returns HTTP 404.
  - 32 of them came in a single burst at 10:48:15–10:48:24: 7 each of clarify, plan, implement and orchestrate_clarify_respond, plus 1 each of check_failure_triage, review, issue_pr_status and cancel_on_pr_close. That suggests 7 trigger events fanning out to the issue-triggered workflows.
  - Taking them out, the real failure rate is **17/299 completed runs (5.7%)**, against the headline **57/299 (19.1%)** of completed runs.
  - **Impact:** the failure metrics and possibly heal intake get the wrong signal. **Confidence:** high on the classification, low on the root cause.
- **Runners waiting in the queue are the main latency cost on short workflows.** "All GitHub-hosted runners with label [ubuntu-latest] are busy" appears in several runs:
  - run 39600169184: about 156s of 167s waiting
  - run 39601060602: about 88s of 100s
  - run 39600652364: about 144s
  - run 39600169447: about 44s of 53s
  - CI run 39596480371: about 2m42s
  
  70.1% of runs (701/1000) end as `other`/skipped. **Impact:** 1–2.5 min per run when traffic bursts. **Confidence:** medium.
- **Semble bootstrap costs about 40s per query, and 69% of review-family bootstrap time went to runs that failed anyway.** The review family had 21 bootstraps totalling 834,905ms (39.8s each). The 10 failing resolver runs with telemetry account for 577,967ms of that and 141,036 query bytes. **Impact:** about 10 min saved per incident like this one. **Confidence:** high.
- **Prompt caching is healthy but only measured for some runs.** `cache_hit_rate=0.681` (16.38M cache-read tokens against 5.00M uncached prompt and 2.67M cache-write).
  - All 50 OpenRouter calls come from just 5 successful review runs.
  - Resolver runs, Claude-engine heal intakes (`claude-opus-5-5 effort=high`) and implement runs report 0 tokens.
  - Run 39583325606 had a 27.7% hit rate with **0 cache-write tokens** over 848k prompt tokens.
  
  **Confidence:** high that coverage is incomplete.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Critical path: stop redispatching the resolver while nothing has changed.**
   - **Evidence:** 17 failures on PRs #7794 and #7795, totalling 11,306s, with the same failing step every time.
   - **Root cause:** the sweep and heal backfill treat a resolver failure as retryable no matter how many times the same (head SHA, reason) pair has already failed. `review_autofix_sweep.yml` has no skip reason for this; the reasons it has are `active_run`, `skip_ai_marker`, `merge_train_queued`, `claude_capacity_wait` and `active_snapshot_incomplete`.
   - **Change:** after the terminal reason code from Reliability #1 exists, add `AUTOFIX_SWEEP_SKIP reason=resolver_no_progress`. It fires when the last N (default 2) `internal-review.yml` runs for the PR failed in the resolver step with the same reason and the same head and base SHAs.
   - **Savings:** about 9 of the 11 wasted runs in this incident, around 2.5h of runner time. Developers waiting on the PR save nothing directly, but the runner queue frees up.
   - **Risk:** low if it fails open: when the reason is unknown, keep today's behavior.
2. **Critical path: free runner slots by cutting the number of skipped runs.**
   - **Evidence:**

     | Family | Skipped / total |
     |---|---|
     | clarify | 157/168 |
     | plan | 154/165 |
     | implement | 153/164 |
     | orchestrate_clarify_respond | 154/164 |
     | workflow_failure_heal | 38/40 |
     | check_failure_triage | 28/29 |

     Many skipped runs last 7–10s (e.g. 39597339524 at 10s and 39597339650 at 7s), so they probably took a runner for a gate job. That is an inference.
   - **Change:** move label and event filters into the `on:` filters (`types:`) or a job-level `if:` that needs no runner.
   - **Savings:** shorter queue waits at bursts. The 44–156s waits above happened at the same moments as these bursts.
   - **Risk:** low, provided the gating logic is copied exactly.
3. **Semble bootstrap reuse.**
   - **Evidence:** 2 bootstraps in one run, e.g. run 39595145653 (91,456ms) and run 39593690473 (89,142ms).
   - **Change:** reuse the index built by the first bootstrap within the job. Pass `SEMBLE_INDEX_AVAILABLE` and the index path forward instead of rebuilding on the resolver side.
   - **Savings:** about 40s per two-bootstrap run.
   - **Risk:** low; the existing fallback path is unchanged.
4. **Orchestrator poll overlap.** p50 is 459s and p95 1,260s. 12 of 28 runs were cancelled, 7 of them as `cancelled_before_first_step` after waiting 285–571s (e.g. 39594267711 at 571s). Polls run longer than the cron interval, so queued polls get superseded. This is a micro-win on runner occupancy; see Orchestrator Health.
5. **Micro-optimization: the AI-memory `finalize-task` step took about 2m12s** in run 39596480531 (12:03:19–12:05:31) with `push_attempts: 1`. Add timing around the commit and push phases before changing anything.

## Cost Optimizations

Ranked by expected token or dollar savings.

1. **Do not spend resolver model calls on attempts that cannot succeed.** Under the old code, the fragment-only conflict was outside what the model resolver could ever fix (`left unmerged Git index entries`, per the comment at `review_conflict_prepare.sh:369-370`). Each of the 17 runs still ran up to `INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS=3` attempts (`review_conflict_resolve.sh:397`).
   - **Gap:** model usage in these runs is not recorded (`or_calls=0`, `codex_calls=0`).
   - **Change:** the same no-progress breaker as Speed #1, plus resolver token telemetry (Reliability #3).
   - **Savings:** up to 3 model attempts × 15 avoidable runs. The amount cannot be measured.
2. **Fix the cache-write miss in the low-hit review run.** Run 39583325606 had a 27.7% hit rate, `or_cache_write_tokens=0`, 848,331 uncached prompt tokens and 6 calls. In the other runs, hit rates were 53.5–83.0% with 500k–840k cache-write tokens.
   - **Inference:** that run's model or provider route did not write to the cache, or the prompt prefix changed between calls.
   - **Change:** log the model and provider for each OpenRouter call next to the token counts, and compare.
   - **Savings:** if that run matched the 68% average, about 400k fewer uncached prompt tokens per run.
   - **Quality risk:** none.
3. **Semble value check.** Each review query returns about 11.9KB from about 4.8 sources (251,275 bytes over 21 queries, 101 sources). `static_dup_bytes_total=0` and `echo_lines_dropped=0`, so the context is not redundant.
   - **Verdict:** Semble adds compact, non-duplicated context. Its cost is time (about 40s per bootstrap), not tokens. Keep it. Skipping it for fragment-only conflicts would be a minor optional win.
4. **Heal intake model choice.** Every heal intake in the sample shows `AI_ENGINE_SELECTED role=WORKFLOW_HEAL engine=claude model=claude-opus-5-5 effort=high source=default`, at 104–420s. There were 102 runs in the window (58 success, 27 cancelled).
   - **Inference:** some of these were triggered by the 40 zero-job failures. Several intakes ran at 10:49–10:56, right after the 10:48 burst: runs 39587349064, 39587767335 and 39588292659.
   - **Change:** in the intake, before the model runs, skip a reported run whose jobs list is empty and whose log archive returns 404. Log `reason=zero_job_run`.
   - **Savings:** up to one Opus high-effort intake per phantom failure.
   - **Quality risk:** low; a real workflow-file problem should still raise a single deduplicated alert.
5. **Serena:** `SERENA_ENABLED: false`, with 0 queries, probes and fallbacks. There is nothing to assess.

## Reliability Improvements

Ranked by expected cut in failures and reruns.

1. **Add a structured terminal reason to the resolver (diagnostic logging).**
   - **Evidence:** `review_conflict_resolve.sh` has about 20 distinct `::error::` exits, for example lines 82, 664, 836, 2955, 2972, 3001, 3279, 3285, 3292, 3447 and 3497. `_resolver_exit_trap` (lines 365–372) only dispatches the integration judge and records no reason.
   - **Root cause category:** observability gap. The same issue was raised in `analysis/workflow-optimization-2026-09-30-2.md`; it is still open.
   - **Fix:** set `RESOLVER_TERMINAL_REASON` just before each failing exit, and print one line from the trap:

     ```bash
     _resolver_exit_trap() {
       local _rc=$?
       if [ "${_rc}" -ne 0 ]; then
         # One bounded line per failed run; the reason comes from a fixed set (unmerged_index, scope_unverified, out_of_scope_exhausted, markers_exhausted, soft_validation_exhausted, fingerprint_rejected, workflow_allowlist, check_diff_failed, isolation_unavailable, host_only_manual, unknown).
         echo "RESOLVER_TERMINAL rc=${_rc} reason=${RESOLVER_TERMINAL_REASON:-unknown} attempt=${attempt:-0}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS:-0} head_ref=${HEAD_REF:-unknown} unmerged=$(git ls-files -u 2>/dev/null | awk '{print $4}' | sort -u | wc -l)"
         _dispatch_integration_judge_now || true
       fi
       return "${_rc}"
     }
     ```

   - **Follow-ups:**
     - Register the new `RESOLVER_TERMINAL` prefix in a fragment at `agents.d/<pr>-resolver-terminal-reason.md`, under `Stable log prefixes (contractual)`.
     - Teach `scripts/cost_audit.py` / `collect_workflow_logs.py` to count the line per run.
   - **Impact:** makes 11,306s of failed-run time diagnosable, and is required by the no-progress breaker.
   - **Rollback/fail-open:** it only logs. The `|| true` and the unchanged `rc` keep current fail-closed behavior.
2. **Classify zero-job runs separately from real failures.**
   - **Evidence:** 40 runs at 0s with no jobs and a 404 log archive. That is 6 of the 23 `review_autofix` failures, and all 7 failures each in clarify, plan, implement and orchestrate_clarify_respond.
   - **Root-cause hypotheses (low confidence):**
     1. Invalid workflow YAML on the triggering ref. Workflow-file-issue runs carry the file path as their name, as with `.github/workflows/internal-clarify.yml` here.
     2. A reusable-workflow ref that could not be resolved at 10:48.

     The cluster lines up with a CI push at 10:48:15 (run 39587252941) and the poll cancellation 39587242911 at 10:48:10. The review-only phantoms at 09:33:06, 10:01:42 and 10:30:56 each coincide with a CI run on the same second (39577836367, 39581504268, 39585173074/39585178610), so `pull_request` events reached `internal-review.yml` but the run could not start.
   - **Fix:** in the collector, tag rows with `jobs_total=0` plus a 404 log archive as `zero_job_run` instead of `failure`, and keep them in a separate count.
   - **Next collection step:** for one such run ID (e.g. 39587270100), record the run's `head_sha` and `event` so the bad ref can be identified.
   - **Impact:** corrects 70% of reported failures. **Fail-open:** if classification fails, the row stays a failure.
3. **Record resolver and Claude-engine token use.** Runs that failed in the resolver report all-zero model counters. Heal intakes report no tokens despite `claude-opus-5-5`. Emit the same per-call usage line the OpenRouter path uses, so `claude_cost_usd` and `claude_*` fields show up in the per-run `cost_telemetry`.
4. **Confirm #7796 on the next forward merge.** Run 39600085501 hit conflicts again in `agents.d/7604-synced-security-finding-intake.md` and `changelog.d/wfopt-0922-validate-dispatch-notice.md`, opened PR #7807 and dispatched review run 39600391051.
   - **Indicator:** that run should pass the resolver step, or have no resolver work left, without falling through to the model.
   - If it fails, the reason code from #1 shows whether the stage-1+3 keep path ran.
5. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** 0 across all 120 runs with log telemetry. There is no prompt-size or rubric pressure. **SEMBLE_FALLBACK:** 17 in total. 16 are `contract_test` fallbacks from CI (8 each in runs 39599714990 and 39596480371), which is expected test behavior. 1 is a runtime fallback in implement run 39581774872 (1,315s), a rare fail-open that is healthy. No Serena probe failures (Serena is disabled).

## AI Memory Health

- **Telemetry found (from `log_summary` only):** 2 `finalize-task` events.
  - Run 39600085692: `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues","source":"issue_pr_status.yml"}`. This is a correct no-op for PR #7806, which has no linked issues.
  - Run 39596480531: `{"op":"finalize-task","ok":true,"did_push":true,"final_state":"merged","issue_number":7800}`, with `push_attempts: 1`. It worked, but the step took about 2m12s.
- **No `retrieve`, `record-candidate`, `promote`, `compact` or `processed-command-*` events were visible.** So hit rate, `estimated_tokens` against budget, and the `keyword_method` split (`llm`/`plain`/`none`) cannot be computed.
  - Visible `log_summary` snippets of the implement, review and poll steps were heavily truncated (848k–1.34M chars dropped), so missing `retrieve` lines are not proof of missing emission.
- **Recommendations:**
  1. Make the collector extract `AI_MEMORY_TELEMETRY:` lines before truncating, as it already does for cost telemetry.
  2. Add a `duration_ms` field to `finalize-task`, so the 2m12s step can be broken down into commit and push.

## GH API Call Audit

| Workflow / job | `used_in_job` | Evidence |
|---|---|---|
| review_autofix / codex-agent | 195, 213 | runs 39599362428, 39599347515 |
| workflow-failure-heal-intake / intake | 22–98 (2× `unknown`) | 39600431697 = 22, 39595518158 = 98, 39596901919 = 70; `unknown` in 39596025327, 39595973174 |
| review_autofix_sweep / sweep | 3 | 39596230401 |
| review_autofix / post-merge-validate-dispatch | 0 | 39596480719 |
| lint_pr_body_auto_close | 1 paginated `pulls/<n>/commits` call | 39600169447, 39599715058 |
| promote_candidate_follow | 1 expected permanent 404 on `git/ref/tags/promote-cycle-frozen` (not retried) | 39601060602, 39596480426 |

- **Hotspot:** review codex-agent uses about 200 calls per run. 17 avoidable resolver reruns at that rate is about 3,400 calls that a no-progress breaker would save. This assumes the failed runs used a similar amount before the resolver step, which is an inference. The lowest remaining PAT budget seen was 3,509 (run 39595518158). No 429s or secondary rate limits were seen.
- **Gap:** the per-step breakdown of the 195–213 calls is not visible.
  - **Change:** emit `GH_PAT_BUDGET phase=step step=<id>` at the boundaries of the existing reviewer, editor and resolver steps. This reuses the existing budget helper and adds no API calls (the rate-limit header is already read).
  - Then check the heaviest step against the batching rule in §14: `_fetch_candidate_issue_details_graphql`-style aliasing.
- **Heal intake `used_in_job=unknown`:** a gap in the budget start snapshot. Make sure the start phase is recorded before the first `gh` call.
- **No per-item loop was found in the visible evidence.** The sweep's use of 3 calls for 3 candidates is efficient.

## Prompt Cache & Memory System

- **`cache_hit_rate` = 0.681** (repo and review family). OpenRouter tokens total 24,373,455: 4,999,045 uncached prompt, 16,377,857 cache-read, 2,667,725 cache-write and 328,828 completion, over 50 calls (all usage-available).
- **Per run:**

  | Run | Hit rate | Calls | Total tokens | Cache-write tokens |
  |---|---|---|---|---|
  | 39591232772 | 83.0% | 12 | 7.96M | — |
  | 39593820061 | 69.4% | 12 | 4.34M | — |
  | 39585178697 | 66.6% | 10 | 6.34M | — |
  | 39582343288 | 53.5% | 10 | 4.55M | — |
  | 39583325606 | 27.7% | 6 | 1.18M | 0 |

- **Fragmentation hypothesis (inference):** the 53.5% and 27.7% runs have uncached prompt tokens of about 1.58M and 0.85M. The high-hit runs have about 0.48–0.50M. That points to a dynamic block (diff or Semble output) placed before the stable rubric prefix in those runs.
  - **Change:** log the byte size of the stable prefix and a hash of its first N bytes for each call, so prefix drift shows up.
  - **Expected:** raise the two low runs to about 68%, saving about 1.2M uncached tokens per pair of runs.
- **No `CONTEXT_BUDGET_WARN`** (count 0), so prompt growth is not eroding the cache.
- **Coverage gap:** only 5 of 48 review runs report usage. Claude-engine paths report none.

## Orchestrator Health

- **Poller:** 28 runs: 16 success, 12 cancelled, 0 failures.
  - Successful polls ran 480–1,525s (run 39581092916: 1,525s; 39585738193: 1,312s).
  - 7 cancellations were `cancelled_before_first_step` after 285–571s in the queue: runs 39594267711, 39576455401, 39590900949, 39577498658, 39592695745, 39587242911, 39585103653.
  - **Inference:** poll duration is longer than the cron interval, and the concurrency group supersedes queued polls. That is benign but wastes queue slots.
  - **Indicator:** watch the ratio of `cancelled_before_first_step` to successful polls.
  - **Smallest mitigation:** set `cancel-in-progress: false` with a single pending slot. That is GitHub's default for a concurrency group; check what is configured before changing it.
- **Poll visibility:** the body of every sampled poll was truncated by about 848k chars, so wave progression, deferrals and conflict-heal retries are not visible.
  - Semble showed `SEMBLE_AVAILABLE: false` in polls 39598558750 and 39595463005; with lazy mode that is expected.
- **Validate:** 3 of 3 succeeded for tracking #7741 (39591521755 at 977s, 39585536702 at 847s) and #7745 (39583388693 at 845s).
- **Implement no-op:** run 39596508188 spent 1,046s before concluding no-op and closing #7803 (`ai:closed (no re-issue)`). **Indicator:** count implement no-op runs and their durations. An early no-op probe would save most of that time.
- **Clarify-respond:** 846s (39594063038) and 687s (39579154286) are long for a response phase. Instrumentation is needed.
- **Integration sync:** the forward-merge flow still needs a human for conflicts. Run 39600085501 ended "PR opened for manual conflict resolution" (PR #7807). The #7796 keep path should make these PRs resolve themselves. Track "forward-merge PRs merged without the resolver model running".

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix order |
|---|---|---|---|
| review/autofix (conflict resolve) | **Retries** (no-progress redispatch) | 17 failures, 11,306s; p50 498s, p95 1,397s | 1 – reason code + no-progress breaker |
| All short workflows | **Queueing** | 44–156s runner waits; 701 skipped runs | 2 – trigger filtering |
| review/autofix (context) | **Compute** (Semble bootstrap) | 834,905ms over 21 bootstraps | 3 – index reuse |
| merge/forward-merge | **Merge/conflict** | Forward-merge PRs #7794, #7795, #7807 conflicting on fragments | 4 – check #7796 on #7807 |
| orchestrate poll | **Queueing / overlap** | 12/28 cancelled | 5 – concurrency review |
| implement | **Compute** on no-op | 1,046s no-op (39596508188) | 6 – early no-op check |
| CI | **Compute** | p50 845s; `tests-release-and-log-analysis` about 9–11 min | 7 – shard that job (`CI_BUDGET findings=0`, so not urgent) |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - resolver reruns on forward-merge PRs (11,306s)
  - runner queue waits during bursts (up to 156s)
  - Semble bootstrap at about 40s each
  - long poller runs (p95 1,260s)
- **Top failure modes:**
  - 40 zero-job runs, cause unknown
  - 17 resolver failures on PRs #7794 and #7795; the fragment conflict is addressed by #7796
- **Highest-cost drivers:**
  - review runs at 4.3–8.0M OpenRouter tokens each
  - unmeasured resolver and Claude-engine spend
  - Opus high-effort heal intakes (102 runs)
- **Top 3 actions:**
  1. Emit `RESOLVER_TERMINAL reason=…` from `_resolver_exit_trap` (`scripts/review_conflict_resolve.sh:365`), then add `AUTOFIX_SWEEP_SKIP reason=resolver_no_progress` in `.github/workflows/review_autofix_sweep.yml`.
  2. Classify zero-job runs (jobs list empty plus 404 log archive) as `zero_job_run` in the collector and in heal intake, and record each one's `head_sha` and `event` to find the source.
  3. Add token and model telemetry for the resolver and Claude engine, plus per-step `GH_PAT_BUDGET` in review codex-agent.

## Metrics Appendix

**Run outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 (s) | p95 (s) |
|---|---|---|---|---|---|---|---|---|
| Repo total | 1000 | 203 | 57 (40 zero-job) | 39 | 701 | 5.7% of all; 19.1% of completed | 1.0 | 755 |
| review_autofix | 48 | 25 | 23 (17 resolver, 6 zero-job) | 0 | 0 | 47.9% | 498 | 1,396.6 |
| orchestrate_poll | 28 | 16 | 0 | 12 | 0 | 0% | 459 | 1,260.5 |
| ci | 21 | 21 | 0 | 0 | 0 | 0% | 845 | 907 |
| workflow_failure_heal_intake | 102 | 58 | 0 | 27 | 17 | 0% | 106 | 353.6 |
| clarify / plan / implement / clarify_respond | 168 / 165 / 164 / 164 | 4 / 4 / 4 / 3 | 7 each (all zero-job) | 0 | 157 / 154 / 153 / 154 | ~4.2% | 1.0 | 10–11 |
| validate | 3 | 3 | 0 | 0 | 0 | 0% | 847 | 964 |
| issue_pr_status | 6 | 4 | 2 (zero-job) | 0 | 0 | 33.3% | 14 | 262.5 |
| cancel_on_pr_close | 18 | 16 | 2 (zero-job) | 0 | 0 | 11.1% | 13 | 140.6 |

**Review telemetry**

| Metric | Repo | review_autofix |
|---|---|---|
| cache_hit_rate | 0.681144 | 0.681144 |
| wall_clock_p50_ms | 10,000 | 655,000 |
| wall_clock_p99_ms | 1,604,380 | 1,805,040 |
| wall_clock samples | 120 | 19 |
| break_glass_count | 0 | 0 |
| context_budget_warn_count | 0 | 0 |

**Tokens (OpenRouter)**

| Prompt (uncached) | Cache read | Cache write | Completion | Total | Calls | Usage unavailable |
|---|---|---|---|---|---|---|
| 4,999,045 | 16,377,857 | 2,667,725 | 328,828 | 24,373,455 | 50 | 0 |

Codex: 0 calls and 0 tokens. Claude cost/token fields: not present in the context (gap).

**GH API**

| Signal | Value |
|---|---|
| Max `used_in_job` | 213 (review codex-agent, 39599347515) |
| Min remaining seen | 3,509 (39595518158) |
| 429s or secondary limits | 0 seen |
| `GH_API_RETRY outcome=permanent` | 2 (expected 404 on the freeze tag) |
| `used_in_job=unknown` | 2 (heal intake) |

**Semble**

| Scope | Queries | Bytes | Avg bytes/query | Bootstraps | Bootstrap ms | Avg ms | Failed | Unused | Sources | Static dup bytes | Fallbacks (contract / runtime) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Repo | 22 | 257,320 | 11,696 | 22 | 872,635 | 39,665 | 0 | 0 | 104 | 0 | 17 (16 / 1) |
| review_autofix | 21 | 251,275 | 11,965 | 21 | 834,905 | 39,757 | 0 | 0 | 101 | 0 | 0 |
| Failed resolver runs (10 with telemetry) | 14 | 141,036 | 10,074 | 14 | 577,967 | 41,283 | 0 | 0 | 56 | 0 | 0 |
| orchestrate_poll | 1 | 6,045 | 6,045 | 1 | 37,730 | 37,730 | 0 | 0 | 3 | 0 | 0 |
| ci | 0 | 0 | — | 0 | 0 | — | 0 | 0 | 0 | 0 | 16 (16 / 0) |
| implement | 0 | 0 | — | 0 | 0 | — | 0 | 0 | 0 | 0 | 1 (0 / 1) |

**Serena:** 0 queries, 0 response bytes, 0 tool calls, 0 fallbacks.

**MCP availability by target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all families) | 0 | 0 | 0 |

**Other MCP servers observed:** none.

**Data gaps:**
- The run-logs directory was not mounted.
- 5 log archives returned 404 (`partial_data:missing_log_archive`).
- Visible logs for review, implement and poll were truncated by 0.85–1.34M chars.
- There is no `retrieve` memory telemetry.
- There is no resolver or Claude token telemetry.
- No root cause could be established for the zero-job runs.

---

- **Actions taken:** I read the repository code behind the failing step: `.github/workflows/review_autofix.yml:3910-4039`, `scripts/review_conflict_resolve.sh:355-399` and its `::error::` exits, `scripts/review_conflict_prepare.sh:360-419`, and the skip reasons in `.github/workflows/review_autofix_sweep.yml`. No files were changed; this phase's deliverable is the report.
- **Assumptions:**
  - Zero-second runs with no jobs and a 404 log archive are runs that could not start, not failures in workflow logic.
  - The #7796 fragment-keep change is what turned PRs #7794 and #7795 green at 12:24. That rests on the timing and the comment in the code, not on step logs.
- **Missing context:**
  - `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox.
  - No `summary.json`, `errors/`, `slow/` or `recent/` logs were available, so every finding rests on `analysis_context.json`, the `log_summary` fields and the repo code.