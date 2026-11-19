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
## Deep Audit — Workflows & Scripts (2026-10-21)

Scope: I read these files in depth: `.github/workflows/review_autofix_sweep.yml`, `scripts/review_conflict_resolve.sh`, `scripts/review_conflict_prepare.sh`, `scripts/gh_helpers.sh` (the retry helpers), `scripts/workflow_failure_heal_non_retryable_backfill.sh`, `scripts/orchestrate_poll_process.sh` (loop sites only), `scripts/review_rb_judge.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/test-and-mark-stable.yml` (polling loops), `.github/workflows/implement.yml` (the Codex step), the `internal-*.yml` wrappers, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `sync_ai_labels.yml` and `tests/test_workflow_file_size_limit.py`. Other files were covered only by the repo-wide pattern greps cited below. Findings already in the report above (the resolver terminal reason code, the no-progress sweep breaker, zero-job run classification) are only cross-referenced here, not repeated.

### Section 1: Bug & Correctness Sweep

**SEC-001** — `.github/workflows/review_autofix.yml:2068-2077`, `scripts/review_conflict_resolve.sh:3643`, `scripts/review_rb_judge.sh:2648`
- **Severity:** High
- **Category:** `security`
- **Description:**
  - The step "Configure git auth for memory helper clones" runs `git remote set-url origin "https://x-access-token:${GH_TOKEN}@${SERVER_HOST}/${GITHUB_REPOSITORY}"` using `secrets.GH_PAT`. That stores the PAT in plain text in the PR worktree's `.git/config` for the rest of the `codex-agent` job.
  - `review_conflict_resolve.sh:3643` writes the PAT URL again after the model has run. That write adds nothing, because "Push all pending commits" (`review_autofix.yml:4610`, set-url at `:4670`) resets the URL itself.
  - The same token-in-URL pattern appears 21 times across 15 files (grep of `x-access-token:${`).
  - Any process later in the job that can read `.git/config` can read the PAT. That includes PR-tree helpers, which the job does run, per the `PYTHONSAFEPATH` guard at `review_autofix.yml:2060-2062`. [NEEDS VERIFICATION] whether the model sandboxes (`codex_isolated_exec.sh`, the OpenCode resolver) can see the host `.git/config`.
- **Recommended fix:**
  - Use the header-based pattern the repo already has at `review_autofix.yml:355-362`: `git -c http.extraHeader="AUTHORIZATION: basic <b64>" … fetch/push`. Apply it per command, so the token never lands in `.git/config`.
  - Delete the redundant set-url at `review_conflict_resolve.sh:3643`.
  - At the end of the job, run `git remote set-url origin https://github.com/${GITHUB_REPOSITORY}` inside an `if: always()` step.

**SEC-002** — `.github/workflows/integration-pr-readiness.yml:51-79`, `.github/workflows/lint-pr-body-auto-close.yml:49-50,112-115`
- **Severity:** Medium
- **Category:** `security`
- **Description:**
  - Both required-check workflows check out the PR head (`actions/checkout@v5` with no `ref:`) and then run scripts from that tree: `scripts/check_integration_pr_readiness.py` and `scripts/lint_pr_body_auto_close.py`.
  - `integration-pr-readiness.yml` holds `statuses: write` (`:39`) and posts the `orchestrator/integration-pr-not-ready` status. A same-repo branch, including a sub-PR merged into `orchestrator/project-*`, can edit the script to post `success`. That defeats the §19 / P3 merge guard both files exist to enforce. Fork PRs get a read-only token, so the exposure is same-repo branches.
- **Recommended fix:** check out the base commit into a separate path and run the script from there. This mirrors the "Check out trusted CI recovery helper" step at `review_autofix_sweep.yml:144-149`:
  - `ref: ${{ github.event.pull_request.base.sha }}`
  - `path: .trusted-support`
  - `persist-credentials: false`

  Then run `python3 .trusted-support/scripts/check_integration_pr_readiness.py …`.

**SEC-003** — `.github/workflows/internal-clarify.yml:20-21`, `.github/workflows/clarify.yml:27-28,722-768`
- **Severity:** Low
- **Category:** `security`
- **Description:**
  - The job-level `if:` deliberately lets **non**-OWNER/MEMBER/COLLABORATOR `User` comments that start with `/reclarify\n<!-- ai:pr-dependency-released:` or `…partial-delivery-restart:` start `clarify.yml` with `secrets: inherit`.
  - The author is checked against the GH_PAT login only inside the job (`clarify.yml:759-767`, fail-closed). Inference: any commenter can make the job start and use a runner, but cannot get privileged behavior.
- **Recommended fix:** when a new optional repo var such as `vars.PIPELINE_BOT_LOGIN` is set, also require `github.event.comment.user.login == vars.PIPELINE_BOT_LOGIN` in both `if:` arms. When it is unset, keep today's behavior.

**BUG-001** — `.github/workflows/review_autofix_sweep.yml:212-239,590-609`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The enumerate step runs under `set -euo pipefail` (`:213`). Its PR listing at `:228-237` is a raw `gh api --paginate`, not wrapped in `gh_api_retry`, even though `gh_api_retry` is sourced at `:216-220` for the snapshot.
  - A single transient 5xx fails the step before `ci-cancelled-pr-snapshot.json` is written (`:239`). The two steps that follow, "Re-run cancelled CI failed jobs once" (`:590-598`, no `if:`) and "Re-dispatch stale security holds" (`:600-609`), then default to `success()` and are skipped for that tick. Only the backfill step has `!cancelled()` (`:564`).
- **Recommended fix:**
  - Use `gh_api_retry --paginate -X GET "repos/${REPOSITORY}/pulls" …` with `GH_TOKEN="${READ_TOKEN}"`.
  - Add `if: ${{ !cancelled() }}` to both recovery steps (the security-hold step keeps its existing conditions inside the `!cancelled()` gate).
  - Have `ci_cancelled_rerun.py` treat a missing snapshot as "no candidates".

**BUG-002** — `.github/workflows/test-and-mark-stable.yml:891-892,917-918`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - `issues/${ISSUE_NUMBER}/comments?per_page=5&direction=desc` is meant to read the **latest** bot comment. The per-issue "list issue comments" endpoint only accepts `since`, `per_page` and `page`, so it returns the 5 **oldest** comments. [NEEDS VERIFICATION]: no web access to confirm the current REST docs. As a result, the failure check at `:919-924` inspects stale comments.
  - `COMMENT_COUNT` at `:891-892` takes `--jq 'length'` over the first page only (30 comments by default). The inactivity detector stops seeing activity once an issue has more than 30 comments.
- **Recommended fix:**
  - Read `.comments` (the total count) from the issue response already fetched at `:883`.
  - For the latest comment, use `issues/<n>/comments?since=<last_poll_iso>`, or GraphQL `comments(last: 5)`.

**BUG-003** — `.github/workflows/test-and-mark-stable.yml:1606-1608,4293,5863,5886-5887,6140`, `scripts/unblock_judge.sh:1206`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** these sites use raw `gh api … --jq … 2>/dev/null || echo ""`, the exact anti-pattern documented at `gh_helpers.sh:1224-1240`: on a non-2xx, `gh` prints the error JSON to stdout.
  - At `:1606-1608`, two identical error bodies make `HEAD_A == HEAD_B` non-empty, so the step logs "PR head SHA stable" without ever reading a SHA.
  - At `:5863`, an error body becomes `pre_clarify_run_id` in `$GITHUB_OUTPUT`. At `:5887`, `${PRE:-0}` is then interpolated straight into a jq program (`select(.id > ${PRE:-0})`).
  - Any API error inside the `:5886` loop sets `NEW_ID` to error text and `break`s, which writes garbage to `run_id`.
  - `unblock_judge.sh:1206` fails safe (`fixup_unavailable`) but has no retry.
- **Recommended fix:**
  - Source `scripts/gh_helpers.sh` and use `_safe_gh_jq … || echo ""`, or `gh_retry gh api`, which buffers stdout.
  - Check numeric outputs with `[[ "${X}" =~ ^[0-9]+$ ]] || X=0` before writing to `$GITHUB_OUTPUT`.
  - Pass `PRE` to jq with `--argjson`. `gh api --jq` cannot take variables, so pipe to `jq --argjson pre "${PRE}"`.

**BUG-004** — `scripts/orchestrate_poll_process.sh:27721-27736`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The re-check after review-blocked handling builds `LABELS_JSON` by string concatenation (`LABELS_JSON+="\"${inum}\":${LABELS}"`). It lacks the `[ -n "${LABELS}" ] || LABELS='[]'` guard used in the other three copies (`:25452`, `:27746`, `:28142`).
  - An empty `LABELS` gives invalid JSON to `check-wave-status` at `:27750`. Under `set -euo pipefail` (`:21`), a failing `$(python3 …)` assignment ends the poll tick. Inference: this is unlikely, since `_safe_gh_jq` success always prints an array.
- **Recommended fix:** reuse the guarded `jq -c --argjson` merge pattern from `:25453`, or the helper proposed in DUP-004.

**BUG-005** — `scripts/workflow_failure_heal_non_retryable_backfill.sh:418-426`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - After a confirmed dispatch, the backfill marker comment is posted with a single raw `gh api -X POST` (`:423`). If that POST fails, the only trace is `marker=failed` on a plain log line, not an annotation.
  - When the intake also rejects the report (so it posts no diagnosis marker), the next 30-minute tick re-selects the PR and dispatches the same report again. Each tick costs up to 3 calls per candidate, per the header at `:50-52`. [NEEDS VERIFICATION]: whether the intake dedupes repeated reports.
- **Recommended fix:**
  - Emit `::warning::WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL … marker=failed`.
  - Retry the POST once through `gh_api_retry`. It already caps a non-idempotent POST at one attempt (`gh_helpers.sh:931-933`), so wrap it in an outer re-check that lists the PR's last comments for the marker before re-posting.

Cross-reference: `_resolver_exit_trap` (`scripts/review_conflict_resolve.sh:365-372`) records no terminal reason. That gap is already covered by Reliability #1 and Speed #1 of the report above and is not repeated here.

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `scripts/gh_helpers.sh:1350-1403` (`curl_gh_api`), `scripts/gh_helpers.sh:1271-1335` (`gh_api_json_to_file`)
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** both helpers retry **permanent** errors:
  - `curl_gh_api` retries every non-2xx response except rate limits, with 1/2/4/8s backoff (`:1389-1395`). That includes 404, 401 and 422.
  - `gh_api_json_to_file` does the same in its `else` branch (`:1313-1322`).
  - `gh_retry` (`:1099-1106`) and `gh_retry_to_file` (`:1184-1192`) short-circuit on `_is_gh_permanent_failure`, and `gh_api_retry` returns 2 on `permanent` (`:988-993`).
- **Call count:** up to 5 calls and about 15s of sleep per permanent failure. After the fix, 1 call.
- **Recommended fix:**
  - In `gh_api_json_to_file`, check `_is_gh_permanent_failure "${stderr_content}"` before the backoff branch.
  - In `curl_gh_api`, return immediately for `http_code` 400, 401, 404 or 422.
  - Extend the existing classifier; do not add a new one.

**API-002** — `scripts/orchestrate_poll_process.sh:27718-27748`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:**
  - After review-blocked handling, the wave re-check runs one REST `issues/<n>/labels` read per wave issue (`:27728`), plus one per re-issued issue (`:27745`). This is a per-iteration call inside the poller loop.
  - The first build of the same map at `:25438-25454` already uses `_fetch_issue_labels_batch_graphql` (batch size 25, `:4360`) with per-issue REST fallback.
- **Call count:** N + M calls today. After the fix, ceil((N + M) / 25) calls, with REST only for cache misses.
- **Recommended fix:** call `_fetch_issue_labels_batch_graphql "${_wave_issue_nums_json}"` again here, followed by the existing miss-only fallback loop. See DUP-004.

**API-003** — `scripts/orchestrate_poll_process.sh:6984-6999`, `scripts/orchestrate_poll_process.sh:27863-27880`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** each blocker issue gets its own `gh_retry _safe_gh_jq "repos/…/issues/${blocker_issue}" --jq '.state'` call inside a `while read` loop. `_fetch_candidate_issue_details_graphql` (`:18339`) already returns `state`, per the comment at `:25429-25433`.
- **Call count:** N calls per blocker set today. After the fix, ceil(N / 25).
- **Recommended fix:** prefetch with `_fetch_candidate_issue_details_graphql "$(printf '%s' "${blockers_json}")"`, and fall back to the existing REST read only for missing keys.

**API-004** — `scripts/orchestrate_poll_process.sh:20668-20675`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** stall recovery issues 7 sequential `gh issue list --label <lbl> --limit 1000` calls, one per pipeline label, then merges them with `jq -s 'add | unique_by(.number)'`. Each `gh issue list` is a paginated GraphQL search.
- **Call count:** at least 7 per tick today. After the fix, 1 (or 1 per 100 results).
- **Recommended fix:** use one aliased GraphQL query modeled on `_fetch_standalone_marker_issues_graphql` (`:18252`), with seven `search(type: ISSUE, query: "repo:<r> is:issue is:open label:\"<lbl>\"")` aliases.

**API-005** — `.github/workflows/test-and-mark-stable.yml:880-937`, with the same loop shape at the inactivity checks on `:1220`, `:1520`, `:2492` and `:3832`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** each poll iteration makes 3 calls: the issue (labels), comments (`length`) and comments (last bot comment). The issue response already carries `.comments` (the total count).
- **Call count:** 3 per iteration today. After the fix, 2 (or 1 using GraphQL `issue { labels comments(last:5) { totalCount nodes } }`). This also fixes BUG-002.
- **Recommended fix:** derive `COMMENT_COUNT` from the `:883` response, either with `--jq '{l:[.labels[].name]|join(","),c:.comments}'` or with a GraphQL call through `gh_api_safe_print`.

**API-006** — `scripts/workflow_failure_heal_pr_reconcile.sh:239-247`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** each heal issue gets its own `pulls?state=open&head=<owner>:ai/issue-<n>` call. `HEAL_ISSUES` comes from one paginated listing (`:96-102`).
- **Call count:** N today. After the fix, 1 paginated `pulls?state=open` listing filtered by `.head.ref` locally, or one aliased GraphQL `ref(qualifiedName:)…associatedPullRequests` query.
- **Recommended fix:** make one listing before the loop and select by `head.ref == "ai/issue-${heal_issue}"` with jq. [NEEDS VERIFICATION]: whether the open-PR count makes the single listing cheaper than N in practice. `HEAL_ISSUES` is filtered by a source marker and is usually 1–2.

**API-007** — `scripts/orchestrate_poll_process.sh:42-58` (`replay_failed_reclarify_commands`)
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** each queued issue gets one issue GET (`:55`) plus one paginated comments listing (`:58`). The issue GET only checks state, `pull_request` and the requeue label.
- **Call count:** 2N today. After the fix, ceil(N / 25) + N; the comment history needs full pages, so it stays per issue.
- **Recommended fix:** prefetch state and labels with `_fetch_candidate_issue_details_graphql` and drop the per-issue GET. Keep the REST comments read.

**API-008** — `.github/workflows/review_autofix_sweep.yml:337-348,437-439`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** the active-run snapshot makes 3 statuses × 2 workflows = 6 paginated listings per tick. The code comment calls this §15-audited.
- **Call count:** 6 today. After the fix, 3: query the repo-level `repos/<r>/actions/runs?status=<s>` once per status and filter by `.path` in the existing jq. `dispatch_pr` already matches on `.path` (`:380`).
- **Recommended fix:** loop over statuses only. Note that repo-wide `in_progress` pages may be larger than the per-workflow pages [NEEDS VERIFICATION]. Keep the existing rule that only a 404 counts as an empty listing, and that any other failure marks the snapshot incomplete.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `scripts/comprehensive_test_and_release_gh_api.sh:~95-185` (`gh_api_safe`, `gh_api_safe_print`, `gh_api_safe_quiet_print`) vs `scripts/gh_helpers.sh:910-1035` (`gh_api_retry`)
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - `gh_api_safe` is a separate retry engine. It detects rate limits only with `grep -qi "rate limit"` (`:121`), which misses the `abuse detection`, `secondary rate` and `HTTP 429` cases that `_is_gh_rate_limit` (`gh_helpers.sh:142`) matches. It ignores `Retry-After` and `X-RateLimit-Reset` (fixed 30→120s) and never trips the shared breaker.
  - `test-and-mark-stable.yml` calls it 15 times.
- **Recommended fix:**
  - Keep the `gh_api_safe*` names (§10).
  - Have `gh_api_safe` source `gh_helpers.sh` when present and delegate to `gh_api_retry --optional "$@"`, mapping return 75 to the current skip-cycle behavior (`RATE_LIMIT_BACKOFF > 0`).
  - Callers stay unchanged.

**DUP-002** — truthy normalizers in 9 places
- **Severity:** Low
- **Category:** `duplication`
- **Description:** function definitions:
  - `scripts/orchestrate_poll_process.sh:964` (`is_truthy`) and `:23083` (`_is_truthy`), in the same file
  - `scripts/apply_analysis_on_main.sh:183`
  - `scripts/auto_release_stable.sh:93`
  - `scripts/review_parse_consolidator.sh:102`
  - `scripts/promote_main_cycle.sh:240`
  - `scripts/review_consolidate.sh:47`
  - `scripts/workspace_init.sh:21` (`truthy`)
  - `scripts/workspace_safety_check.sh:4` (`truthy`)

  Inline copies: `review_autofix_sweep.yml:268-271`, `review_conflict_resolve.sh:2578`, `review_conflict_prepare.sh:398-403`.
- **Recommended fix:**
  - Add `cw_is_truthy <value>` to a new `scripts/bool_helpers.sh`. Before landing, check that the name is unused (no current match for `cw_is_truthy`).
  - Leave the existing names as one-line wrappers (§10).
  - In `orchestrate_poll_process.sh`, have `_is_truthy` call `is_truthy` first.

**DUP-003** — `scripts/orchestrate_poll_process.sh:6983-6999` vs `:27858-27880`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** the same blocker-state classification loop (open / closed / unknown counters plus a `#n=state` summary) appears twice, with different variable names.
- **Recommended fix:**
  - Extract `_classify_blocker_states <blockers_json>` into `orchestrate_poll_process.sh`, printing `open_count unknown_count summary`. Back it with the API-003 batch prefetch.
  - Update both callers.

**DUP-004** — `scripts/orchestrate_poll_process.sh:25447-25454`, `:27721-27748`, `:28137-28144`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** there are three loops that build the `LABELS_JSON` issue→labels map. Two have the empty-label guard and one does not (BUG-004).
- **Recommended fix:**
  - Add `_labels_json_for_issues <numbers_json> [<existing_map_json>]`. It wraps `_fetch_issue_labels_batch_graphql`, runs the miss-only REST fallback and returns the merged object.
  - Replace all three loops. This also fixes API-002 and BUG-004.

**DUP-005** — `scripts/review_rb_judge.sh:2657-2679` vs `:2680-2702`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** two near-identical "judge produced no changes → treat as merge" blocks: security hold check, `rb_security_merge_gate`, `review_head_gate_post_status`, `ensure_label_exists`, a per-issue label swap, then three `$GITHUB_OUTPUT` lines. Only the echo text differs.
- **Recommended fix:** extract `rb_approve_no_change_as_merge <message>` into `review_rb_judge.sh`. The callers must still `exit` on the hold paths, so return a status and keep the `exit` at the call sites.

**DUP-006** — `.github/workflows/test-and-mark-stable.yml:880-937` and the matching loops around `:1220`, `:1520`, `:2492` and `:3832`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** five inline inactivity-polling loops (poll labels and comments, reset the idle timer, check for success or failure, time out) live in a 7,714-line workflow.
- **Recommended fix:** move one parameterized `wait_for_issue_phase <issue> <success_label_regex> <phase_name>` into `scripts/comprehensive_test_and_release_gh_api.sh`, which already holds the `gh_api_safe_print` helpers these loops call. Keep each step's `name:`, `id:` and outputs (§24). This also shrinks the file (EXPR-003).

### Section 4: Expression Size Limit Risk Assessment

**EXPR-001** — `.github/workflows/implement.yml:2795-4102` (step "Run Codex implementation", `run:` body from `:2833` to about `:4102`)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The body is about 1,270 lines, including the inline prompt heredoc at `:2902` (`cat > "${PROMPT_TEMPLATE_FILE}" <<'EOF'`, ending at `:3041`) and the codex retry heredoc at `:3526`.
  - Greps of `${{` across `:2833-4102` found no matches: every `${{ }}` sits in the step's `env:` (`:2799-2832`). So the body is **not** subject to the 21,000-character limit today.
  - Estimated size: about 75,000–95,000 characters (inference from line count × typical width).
  - **Risk:** adding a single `${{ … }}` anywhere in the body makes the whole body one expression, about 4× over the limit, and every implement run would fail at startup. `tests/test_workflow_file_size_limit.py:66-98` would catch this before merge.
- **Headroom:** not applicable while the body has no interpolation. At 0% the moment one is added.
- **Recommended fix:**
  - Move the prompt text at `:2902-3041` to `prompts/mode-implement-inline.txt` and read it at runtime, as `review_autofix.yml:2576` was handled.
  - Move the rest of the body to `scripts/implement_run_codex.sh` using the `review_autofix_step_<slug>.sh` wrapper pattern documented in agents.md. Keep the step's `name:`, `env:` and `if:`.

**EXPR-002** — `tests/test_workflow_file_size_limit.py:66-98`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - The expression guard only checks `jobs.*.steps[].run` strings that contain `${{`. It does not check step or job `if:`, `env:` values, `with:` inputs, `concurrency.group`, or `run-name`.
  - The current largest `if:` lines are all under 1,500 characters: `review_autofix.yml:3855`, `:4220` and `:4250` are the only `if:` lines over 600 characters, and none exceeds 1,500. So there is no current risk; this is a coverage gap.
- **Recommended fix:** extend the walk to measure every string value in the step and job mappings that contains `${{`, and every bare `if:`, against the same `RUN_EXPRESSION_GUARD_CHARS = 18_000`.

**EXPR-003** — whole-file size: `.github/workflows/test-and-mark-stable.yml` (7,714 lines), `.github/workflows/implement.yml` (7,061), `.github/workflows/review_autofix.yml` (5,749)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - The audit prompt's 1 MB / 800 KB thresholds do not match this repository. The binding limit is **512,000 bytes**, guarded at **480,000** (`tests/test_workflow_file_size_limit.py:26-27`, §24).
  - Byte sizes could not be measured in this run (no shell). By line count these three files are closest to the guard. `review_autofix.yml` already breached the hard limit once (#4327: 540,537 bytes). [NEEDS VERIFICATION]: run `wc -c` on these three files.
- **Recommended fix:** EXPR-001 and DUP-006 are the largest available byte reductions in `implement.yml` and `test-and-mark-stable.yml`, roughly 75–95 KB and 15–25 KB (inference). Per §24, apply them before any file passes about 430,000 bytes.

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `scripts/review_conflict_resolve.sh:170-239,2536,2571-2577`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:**
  - The script sources `codex_thread_reuse.sh` (`:172-185`) and defines `resolve_conflict_thread_reuse_asset`, `conflict_thread_reuse_enabled` and `render_conflict_thread_reuse_continuation` (`:187-239`), but nothing calls them. The only references are definition asserts in `tests/test_codex_thread_reuse_review.py:200-202`.
  - `:2575-2577` exist only to keep the unused `CONFLICT_RESOLVER_THREAD_REUSE_MARKER` and related variables alive (`: "${…}"`).
  - `:2578-2580` confirms that the OpenCode resolver always uses the fresh full-prompt path.
- **Recommended fix:** removing these functions removes identifiers, which needs explicit instruction under §10. Until that is given, mark the block `# DEPRECATED: unused since the OpenCode resolver (see :2580)`. Remove it together with the test asserts in one PR.

**CONSIST-001** — `.github/workflows/review_autofix_sweep.yml:288,488-491,552`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - The `default_branch_unavailable` `AUTOFIX_SWEEP_END` line (`:288`) omits `skipped_merge_queued` and `snapshot_incomplete`. Those fields are present at `:274` and `:552`.
  - The `claude_capacity_wait` skip (`:488-491`) increments no counter, so `dispatched + skipped_* + failures` can be less than `candidates`. That makes the log-analysis counts unreliable.
- **Recommended fix:**
  - Add `skipped_capacity_wait=0`, increment it at `:490`, and include it in all three END lines. This is a new log field (additive, §10).
  - Make `:288` print the same field set as `:552`.

**CONSIST-002** — `.github/workflows/lint-pr-body-auto-close.yml:85-86`, `scripts/unblock_judge.sh:1206`, `scripts/workflow_failure_heal_non_retryable_backfill.sh:150,423`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - These are raw `gh api` calls where nearby code uses the retry helpers.
  - The lint step fails a **required** check closed on any transient error. Each review PR then needs a manual rerun.
- **Recommended fix:**
  - Use `gh_api_retry --paginate` in the lint step. This needs a trusted `gh_helpers.sh`; combine it with the SEC-002 base checkout.
  - Use `gh_retry gh api` in the two scripts.

**CONSIST-003** — `.github/workflows/internal-plan.yml:15-35`, `internal-implement.yml:16-33`, `internal-clarify.yml:17-22`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - Cross-reference to Speed #2 in the report above. These wrappers already gate on a job-level `if:` placed on a reusable-workflow `uses:` job. A skipped reusable job is not given a runner, and comment-body and author filters cannot be expressed in `on: types:`.
  - Inference: the 7–10s "skipped" durations are run bookkeeping, not runner occupancy, so moving filters into `on:` would not free runner slots for these four families.
- **Recommended fix:** before acting on Speed #2, check whether the skipped runs have any job with `runner_name` set. If none do, retarget that item to the workflows that run a real gate job on a runner.

**CONSIST-004** — `.github/workflows/lint-pr-body-auto-close.yml:46`, `integration-pr-readiness.yml:43`, `sync_ai_labels.yml:34` (`ubuntu-latest`) vs `review_autofix_sweep.yml:110` (`ubuntu-24.04`)
- **Severity:** Low
- **Category:** `consistency`
- **Description:** runner labels are mixed. The queue-wait messages in the report above name the `ubuntu-latest` pool specifically.
- **Recommended fix:** pick one pinned label repo-wide (for example `ubuntu-24.04`) so capacity analysis covers one pool. This changes no behavior.

**SHELL-001** — `.github/workflows/test-and-mark-stable.yml:928,1220,1520,2492,3832`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** SC2086: `if [ $IDLE -ge $INACTIVITY_LIMIT ]; then` has unquoted operands. An empty `IDLE` raises a `[: -ge: unary operator expected` error instead of a clean comparison.
- **Recommended fix:** `if [ "${IDLE}" -ge "${INACTIVITY_LIMIT}" ]; then`, or do this as part of DUP-006.

**SHELL-002** — `scripts/orchestrate_poll_process.sh:23083-23088`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:**
  - `_is_truthy` uses `echo "$1"`. A value of `-n` or `-e` is consumed as an echo option.
  - A call with no argument fails under `set -u` (`:21`).
  - The sibling `is_truthy` at `:964-975` already uses the safe `printf '%s' "${1:-}"`.
- **Recommended fix:** make the body `is_truthy "${1:-}"`.

**SHELL-003** — `.github/workflows/sync_ai_labels.yml:125,130`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:**
  - `2> >(tee "${STDERR_LOG}" >&2)`: bash does not wait for the process substitution, so the `stderr_log` handed to later steps (`:161`) may be truncated. [NEEDS VERIFICATION]
  - `:125` interpolates `${{ inputs.dry_run }}` inline, while `:170` passes the same input through `env:`.
- **Recommended fix:**
  - Redirect with `2>"${STDERR_LOG}"`, then `cat "${STDERR_LOG}" >&2` after the command.
  - Move `dry_run` to `env: DRY_RUN` as in `:170`.

**SHELL-004** — `scripts/orchestrate_poll_process.sh:25419,25462,27723,27740,28138`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** SC2086: `for inum in ${ISSUE_NUMS}` and `for rnum in ${REISSUED_NUMS}` rely on unquoted word splitting, and glob expansion applies too. The values come from state-file JQ output, so this is a latent risk rather than a live one.
- **Recommended fix:** `mapfile -t _nums < <(printf '%s\n' "${ISSUE_NUMS}" | grep -E '^[0-9]+$')`, then `for inum in "${_nums[@]}"`, as already done for `_wave_issue_nums` at `:25418-25422`.

TODO/FIXME/HACK/XXX markers: none found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml` (repo-wide grep).

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 8 | SEC-002, BUG-001, BUG-002, BUG-003, API-001, API-002, DUP-001, EXPR-001 |
| Low | 25 | SEC-003, BUG-004, BUG-005, API-003, API-004, API-005, API-006, API-007, API-008, DUP-002, DUP-003, DUP-004, DUP-005, DUP-006, EXPR-002, EXPR-003, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, SHELL-003, SHELL-004 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/review_autofix.yml`, `scripts/review_conflict_resolve.sh`, `scripts/review_rb_judge.sh`, plus 12 other files that embed `x-access-token:${…}` in remote URLs (SEC-001) | Medium |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/review_autofix_sweep.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/test-and-mark-stable.yml` | Medium |
| Code modularization | `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/test-and-mark-stable.yml`, new `scripts/bool_helpers.sh` plus 8 scripts with truthy copies | Medium |
| Expression size reduction | `.github/workflows/implement.yml`, new `prompts/mode-implement-inline.txt`, new `scripts/implement_run_codex.sh`, `tests/test_workflow_file_size_limit.py` | Medium |
| Medium/Low fixes | `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `.github/workflows/sync_ai_labels.yml`, `scripts/unblock_judge.sh`, `scripts/workflow_failure_heal_non_retryable_backfill.sh`, `scripts/review_conflict_resolve.sh` | Small |
## API Call Consolidation & Dead-Call Analysis (2026-10-21)

### Safety Tag Legend
**`SAFE_TO_MERGE`** means static reading proves every precondition: the calls hit the same endpoint (or one asks for a strict subset of the other's fields) with the same pagination, filters and token; they sit in the same function or step with no write in between; error handling and retries stay the same; no concurrency boundary or cycle-local cache contract is crossed. The implement stage may apply these directly. **`NEEDS_VERIFICATION`** means the change looks right but at least one precondition can't be proven from the source alone, so the listed checks must pass first. **`RISKY_SKIP`** means at least one hard trigger applies: a retry, backoff or stall-recovery path; an identity, auth or permission probe; pagination; a rate-limit-sensitive path; code inside `orchestrate_poll_process.sh`; or log keys that other code greps for. These findings are listed so they stay visible, and must not be implemented automatically.

### Consolidation Candidates (MERGE-###)
No findings.

The merge-type overlaps found in this pass are already raised in the Deep Audit as API-004, API-005 and API-008. Their tags and the extra detail for each are under Cross-References below.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — `RISKY_SKIP`**
- **Calls involved:**
  - `scripts/orchestrate_poll_process.sh:3887-3908`: `unblock_trusted_login()`, cached per tick in `UNBLOCK_TRUSTED_LOGIN` / `UNBLOCK_TRUSTED_LOGIN_STATE`, called at `:18898`.
  - `scripts/orchestrate_poll_process.sh:30106-30109` and `:30361-30369`: the noop-suspicious sweep's own separate cache, `NOOP_CAP_TRUSTED_LOGIN` / `NOOP_CAP_TRUSTED_LOGIN_STATE`.
- **Endpoint:** `GET /user` (REST) in both places.
- **Call count:** up to 2 per poll tick today, when both paths run in the same process; 1 after the change.
- **Evidence:** two caches in the same main-tick process (after the sweep-only exit at `:209-213`) store the same value from the same token:
  ```bash
  # :3895
  UNBLOCK_TRUSTED_LOGIN="$(gh_retry _safe_gh_jq "user" --jq '.login // ""' 2>/dev/null || true)"
  # :30362
  NOOP_CAP_TRUSTED_LOGIN="$(gh_retry _safe_gh_jq "user" --jq '.login // ""' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
  ```
- **Proposed fix:**
  - At `:30361`, when `UNBLOCK_TRUSTED_LOGIN_STATE` is `ok`, seed `NOOP_CAP_TRUSTED_LOGIN` from `${UNBLOCK_TRUSTED_LOGIN}`, lowercased, and set `NOOP_CAP_TRUSTED_LOGIN_STATE=ok`. Otherwise keep the existing read.
  - Do **not** call `unblock_trusted_login()` from the noop path. On failure it sends a `CRITICAL` Telegram alert ("tracking projects are paused", `:3901-3904`), which would change what operators see when the noop path is the first to resolve the login.
- **Safety rationale:** RISKY_SKIP on two counts. The code is in `orchestrate_poll_process.sh`, and the login is the trust anchor that authenticates fingerprint-cap marker authors. The two caches also validate differently: `:3896` checks a regex that allows a `[bot]` suffix, while `:30363` only checks the value is non-empty.
- **Downstream signal:** do not implement automatically. Manual review must:
  1. confirm that `unblock_trusted_login` normally resolves before `:30361` in the same tick, otherwise the saving is zero;
  2. agree which validation rule wins;
  3. confirm that the `::warning::` at `:30367` still fires only on a real resolution failure.

**REUSE-002 — `RISKY_SKIP`**
- **Calls involved:**
  - `scripts/orchestrate_poll_process.sh:44` (`replay_failed_reclarify_commands`)
  - `scripts/orchestrate_poll_process.sh:157` (`detect_unrouted_blocked_comments`)
  - Both are called back to back at `:210-211` in `RECLARIFY_REQUEUE_SWEEP_ONLY` mode.
- **Endpoint:** `GET /rate_limit` in both places.
- **Call count:** 2 per sweep-only tick today; 1 after the change.
- **Evidence:**
  ```bash
  # :44
  budget="$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null)" || return 0
  # :157
  budget="$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null)" || budget=""
  ```
- **Proposed fix:** have the sweep-only branch at `:209-212` read the budget once and pass it to both functions, with each function keeping its own `-ge 500` / `-lt 500` gate.
- **Safety rationale:** RISKY_SKIP. Both calls are rate-limit probes in `orchestrate_poll_process.sh`, and the replay between them can spend 1 + 2N calls (`:46`, `:51`, `:55`, `:58`). Reusing the first reading would make the second gate less conservative, and would change the `remaining=` value in the `RECLARIFY_UNROUTED outcome=skip reason=budget_low` log line (`:159`). Inference, not checked here (no web access): GitHub documents `/rate_limit` as not counting against the core quota, so the saving would be one round trip, not quota.
- **Downstream signal:** do not implement automatically. It needs manual sign-off that a budget reading taken before the replay is acceptable for the unrouted-comment detector. The quota saving is probably nil, so this is low priority.

**REUSE-003 — `NEEDS_VERIFICATION`**
- **Calls involved:** `.github/workflows/review_autofix_sweep.yml:285`. The same job already reads the event payload's default branch at `:147`.
- **Endpoint:** `GET /repos/{owner}/{repo}` (`.default_branch`), using `READ_TOKEN` (`github.token`), so it does not spend `GH_PAT` budget.
- **Call count:** 1 per tick with at least one candidate (up to 48 per day) today; 0 after the change when the payload has the field, with 1 as the fallback.
- **Evidence:**
  ```yaml
  # :147 (same job, earlier step)
  ref: ${{ github.event.repository.default_branch || github.ref_name }}
  # :285
  sweep_default_branch="$(GH_TOKEN="${READ_TOKEN}" gh api "repos/${REPOSITORY}" --jq .default_branch 2>/dev/null || true)"
  ```
  This was already recommended and not implemented in `analysis/workflow-optimization-2026-10-15-2.md:397-401`. The env name proposed there, `SWEEP_EVENT_DEFAULT_BRANCH`, appears nowhere else in the repo, so it does not collide.
- **Proposed fix:**
  - Add `SWEEP_EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}` to the "Enumerate open PRs" step `env:` (`:202-211`).
  - At `:285`, use that value when it matches `^[A-Za-z0-9._/-]+$`; otherwise run the existing API read unchanged.
  - Keep the `default_branch_unavailable` skip at `:286-290` as is.
- **Safety rationale:** NEEDS_VERIFICATION. It is not proven that `schedule` payloads include `repository.default_branch`; the `|| github.ref_name` fallback at `:147` suggests the authors were not sure either. The value also feeds the #6629 provenance check (`:278-284`, `:376-385`), whose comment says "no fallback".
- **Downstream signal:** before implementing:
  1. Inspect `github.event` for a schedule-triggered sweep run, such as run 39596230401, and confirm `repository.default_branch` is present and non-empty.
  2. Confirm with the #6629 owner that a default branch taken at trigger time is acceptable in place of a live read for the provenance filter.

**REUSE-004 — `RISKY_SKIP`**
- **Calls involved:**
  - `scripts/review_single_issue_security_pass.sh:370`, in `single_pass_gate` (`:318-577`). It is run by the review_autofix.yml step "Single-issue security pass" (`:4172-4200`).
  - `scripts/review_rb_judge_security_pass.sh:210`, in `rb_security_block_already_reported`. It runs only when the review-blocked judge checks a security hold.
  - The data already exists as the gate job output `fingerprint_cap_marker_author_login` (`.github/workflows/review_autofix.yml:328`). It is written by `scripts/review_autofix_step_evaluate_gate.sh:1371` from the gate's own `GET /user` at `:334`, and is already reused by the heal reporter at `review_autofix.yml:5686`.
- **Endpoint:** `GET /user`.
- **Call count:** 1–2 per codex-agent run that reaches the clean-review tail today; 0 after the change when the gate output is non-empty, with the existing reads kept as fallback.
- **Evidence:**
  ```bash
  # review_single_issue_security_pass.sh:370
  SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
  # review_rb_judge_security_pass.sh:210
  author="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null)" || return 2
  ```
- **Proposed fix:** pass `needs.gate.outputs.fingerprint_cap_marker_author_login` through a new env var into both steps, and use it only when it is non-empty.
- **Safety rationale:** RISKY_SKIP, for three reasons:
  - The login authenticates who posted pipeline markers, which makes this an authentication flow.
  - The value would cross a job boundary.
  - The tokens can differ. The security-pass step runs with `GH_TOKEN: ${{ secrets.GH_PAT || github.token }}` (`:4187`) and has a `github-actions[bot]` fallback (`:4197`). The gate's evaluate step appears to run with `secrets.GH_PAT` (`:616`; the step boundary was inferred, not confirmed). So when `GH_PAT` is empty the two identities differ.
  - Also, the gate output is set only when `gate_fetch_marker_comments` actually ran (`:323-340`); it stays empty while the fetch state is `pending`.
- **Downstream signal:** do not implement automatically. A security reviewer must:
  1. confirm the gate evaluate step's `GH_TOKEN`;
  2. decide whether an identity carried across jobs is acceptable as a trust anchor for blocking or merging;
  3. make sure the saving (at most 2 of the roughly 200 calls per run) is worth the extra coupling.

### Dead Calls (DEAD-API-###)
No findings.

Calls checked and confirmed to be used:
- `scripts/review_conflict_prepare.sh:1113-1116`: the tracking issue's title and body are read at `:1327-1333` and `:1359-1369`.
- `scripts/workflow_failure_heal_non_retryable_backfill.sh:150`: `LOGIN` feeds the selector at `:291` and the reporter at `:404`, so the reporter skips its own `GET /user` (`workflow_failure_heal_autofix_report.sh:216-228`).
- `scripts/workflow_failure_heal_report.sh:172` and `:210`: the second read only happens when `HEAL_REPORTER_LOGIN` is still empty.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — it changes how the shared retry helpers `curl_gh_api` and `gh_api_json_to_file` retry and back off. Every caller of `gh_helpers.sh` inherits the change, so it needs per-caller review of which 404/422 responses those callers currently rely on being retried.
- API-002: RISKY_SKIP — the code is in `orchestrate_poll_process.sh` and re-reads labels after review-blocked handling. A batched read could return labels from before the review-blocked writes (inference). Fold it into DUP-004 only with a manual review of how fresh the labels must be.
- API-003: RISKY_SKIP — `orchestrate_poll_process.sh`; the blocker-state reads gate whether a wave advances. Keep the per-issue REST read as the fallback for missing keys, exactly as API-003 proposes.
- API-004: RISKY_SKIP — this is stall recovery in `orchestrate_poll_process.sh`, and each call is a paginated `gh issue list --limit 1000`. New detail: today each label fails on its own (`|| echo '[]'` at `:20673`), so one failed label loses only that label's issues. A single aliased query would turn one failure into seven missing labels. The batched version must fall back to the per-label loop on any error.
- API-005: RISKY_SKIP — the `:883` labels call also detects rate-limit backoff (`[ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]`, `:887`). Changing that call's `--jq` output into an object breaks the empty check unless the check is rewritten too. Note also that `.comments` is a total, while today's `length` stops at 30 (first page). That changes `CUR_STATE` (`:894`), which is the intended effect of BUG-002 but is still a behavior change.
- API-006: NEEDS_VERIFICATION — confirmed at `scripts/workflow_failure_heal_pr_reconcile.sh:243`. It is a single unpaginated `per_page=10` call, and the server-side `head=` filter is already re-checked locally at `:250`. Swapping it for a paginated list of all open PRs would *add* pagination and probably cost more, since `HEAL_ISSUES` is usually 1–2. Measure open-PR count against `HEAL_ISSUES` size before acting.
- API-007: RISKY_SKIP — the issue GET at `scripts/orchestrate_poll_process.sh:55` is deliberately a *live* re-read. The comment at `:37-41` says "The marker is data, not authorization… live issue read per queued issue", and the `:46` listing already filters by state and label. Batching it would replace a live check against races with an earlier snapshot.
- API-008: RISKY_SKIP — the calls are paginated (`--paginate`, `:340`). The 404 rule ("workflow file absent counts as no runs", `:331-334`, `:344`) is per workflow, and that distinction is lost with a single repo-wide listing. Snapshot keys are `${workflow}:${head_ref}` (`:424`), so a repo-wide listing would have to rebuild the per-workflow split from `.path`, including runs of the reusable `review_autofix.yml` reached through `internal-review.yml`. The code comment at `:322-324` marks the current shape as §15-audited.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-003, API-006 |
| RISKY_SKIP | 10 | REUSE-001, REUSE-002, REUSE-004, API-001, API-002, API-003, API-004, API-005, API-007, API-008 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
- Next best candidate: REUSE-003 can become SAFE_TO_MERGE once its two checks pass (the schedule payload carries `repository.default_branch`, and the #6629 provenance owner signs off).
- Assumptions: the step boundary at `review_autofix.yml:616` was inferred from where the `env:` blocks sit. The claim that `/rate_limit` does not count against the core quota is from memory; there was no web access to confirm it.
- Missing context: no run logs were mounted, so the per-step split of the roughly 200 calls in codex-agent is unknown. That limits how far the savings in REUSE-004 can be measured.