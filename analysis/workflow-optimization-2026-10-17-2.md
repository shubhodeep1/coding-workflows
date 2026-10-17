## Executive Summary

- **The `Resolve integration ref` refusal is still unfixed and keeps recurring. It caused 14 of the 15 failures (93%) in this window.** Ten clarify runs and four plan runs failed at this step. Six of them came after this morning's report window closed (00:28): clarify 38989442558 (01:06), 38991000287 (01:28), 38992076216 (01:43) and 38992809814 (01:54), and plan 38989786896 and 38989791508 (01:11). Counting only runs that actually executed, clarify failed 10 of 19 (53%) and plan failed 4 of 12 (33%). The candidate root cause from the earlier report has not changed: `scripts/resolve_integration_ref.sh:198` still matches `<!--\s*check-failure-triage:` anywhere in the issue body, and line 225 still refuses with `label_missing`. **Impact:** fixing it removes about 93% of failures and the heal-intake run that follows each one. **Confidence:** high that the step is the problem; medium for `label_missing` as the reason, because the run rows carry no reason field (see Diagnostic addition D1).
- **Every Codex token figure in this window is an artifact of the parser.** Implement's total of 7,932,834 tokens is exactly 6 × 1,322,139. That 1,322,139 is reproduced exactly by `CODEX_TOKENS_RE` (`scripts/cost_audit.py:62-64`) matching the echoed script text of `implement.yml:3651-3773`: 75,222 + 12,345 + 1,234,567 (example values in the comments), plus 2 + 2 (from `2>/dev/null`), plus 0 (from `[^0-9]`), plus 1 (from `tail -1`). The two pairs of runs with identical totals (38985760245 = 38978076450 = 1,322,139; 38988118138 = 38987942375 = 2,644,278) confirm it. The 2026-10-11 report attributed the same 1,322,139 to run 38083270579, so that attribution was an artifact too. Separately, `build_run_cost_telemetry` (`cost_audit.py:564`) projects through `RUN_COST_TELEMETRY_FIELDS`, which leaves out the `claude_*` fields and `or_phases`. Claude usage, including heal intake on `claude-opus-5-5 effort=high`, is therefore invisible to this analysis. **Impact:** Codex and Claude spend are both unmeasured. **Confidence:** high, since the sum reproduces exactly from source.
- **One review/autofix run consumed 90% of all measured LLM tokens.** Run 38987587333 used 37,638,374 OpenRouter tokens over 20 calls in 2,934s, about 1.88M tokens per call. The baseline run 38916979717 used about 0.39M per call (3.92M over 10 calls). That is 4.8× per call and 9.6× per run. **Impact:** if this outlier recurs, it alone costs more than every other AI workflow combined. **Confidence:** high on the numbers; the cause is unknown because per-phase data (`or_phases`) is dropped.
- **A manual promote very likely cancelled the scheduled release gate after 90 minutes of work.** The promote cycle's gate run 38986967451 (`[cycle:38986857764]`) ran for 5,419s and was cancelled during `Phase 7: Close PR and verify cancel_on_pr_close fires`, about 3.6 minutes after the workflow_dispatch promote 38993107138 dispatched gate run 38993222716. Its `e2e-smoke-test` job uses `concurrency: group: e2e-smoke-test-<repo>, cancel-in-progress: true` (`test-and-mark-stable.yml:517-520`). The cycle then exited cleanly via `skip_cycle smoke_gate_cancelled` (`promote_main_cycle.sh:504-508`), so no gate was bypassed. The cost is 90 minutes and 19 wasted OpenRouter calls. **Confidence:** medium; the causal link is inferred from timing.
- **Hosted runners are saturated and add queue time to short jobs.** Runs logged "All GitHub-hosted runners with label [ubuntu-latest] are busy" and waited 74s of a 90s run (38993362737), 40s of 47s (38993357327), 18s of 30s (38993455727) and 165s before the poller started (38992457310). **Confidence:** high.
- **Data gap.** The deep-dive directory `/home/runner/work/_temp/workflow-log-output` was not mounted, so no untruncated step logs could be read. Every per-run claim below comes from the `analysis_context` rows, the `log_summary` fields, and the repository source. The window also hit the 1,000-run cap, and 697 of those runs (69.7%) were skipped or `other`.

## Speed Optimizations

1. **Stop the clarify/plan refusal loop (critical path).**
   - **Evidence:** clarify refusal runs last 22–31s, but each one blocks the issue entirely. Refusals came about every 10–25 minutes from 23:45 to 01:54. Heal-intake runs followed right behind: 38992100284 started 01:44:05, after clarify 38992076216 (01:43:44); 38992835808 started 01:54:58, after 38992809814 (01:54:35).
   - **Root cause:** a deterministic refusal (exit 3) that `clarify.yml:205-207` and `plan.yml:281-283` turn into `exit 1`. Retrying cannot help.
   - **Change:** land the anchoring fix BUG-001 from today's earlier report:
     ```python
     has_marker = re.match(r"\s*<!--\s*check-failure-triage:", body) is not None
     ```
     Add D2 first if the reason has to be confirmed before the change.
   - **Savings:** unblocks the affected issues' whole clarify → plan → implement chain, typically 45–90 minutes of pipeline progress per issue, and avoids a 69–73s heal-intake run per refusal.
   - **Risk:** low. Genuine triage issues whose header is on the first line keep the fail-closed path.
2. **Do not let a manual promote preempt an in-flight scheduled gate (critical path for releases).**
   - **Evidence:** run 38986967451 was cancelled at Phase 7 after 5,419s, as described above.
   - **Change:** in the `promote` job's dispatch step, before running `gh workflow run test-and-mark-stable.yml`, list in-progress `test-and-mark-stable.yml` runs, as `promote_main_cycle.sh:464` already does for the reverse direction. Emit `::warning::` with their run IDs (D5). The smallest safe behaviour is to log only. A follow-up can make it wait, opt-in through a defaulted variable.
   - **Savings:** up to about 90 minutes of gate time per collision.
   - **Risk:** low when it only logs.
3. **Skip unused Semble bootstraps in workflow log analysis (micro).**
   - **Evidence:** run 38987121359 did 4 bootstraps, all 4 unused, costing 154,323ms (7.4% of its 2,091s). The same thing happened in 38903068381 (128s) in the earlier report, so it has not been fixed.
   - **Change:** in lazy mode, bootstrap only when the first query runs.
   - **Savings:** about 2.5 minutes per analysis run.
   - **Risk:** low.
4. **Overlap the review/autofix Semble bootstrap with setup (micro).**
   - **Evidence:** 130,779ms across 3 bootstraps for 3 queries, about 43.6s per query.
   - **Change:** start the bootstrap in the background during checkout and PR-metadata steps. This is an inference; step ordering is not visible in the data.
   - **Savings:** up to about 40s per review run, about 2% of the 1,700–2,900s slow runs.
   - **Risk:** low.
5. **Runner queueing (non-compute).**
   - **Evidence:** 18–165s waits, listed in the Executive Summary. There is also an unexplained 107s gap between jobs in 38993276170 (`cancel` job ended 02:01:40, heal-pr-reconcile started 02:03:27).
   - **Change:** none on the critical path. Short gating jobs (lint, readiness) cannot avoid the shared pool. Track the queue as described in D9.
   - **Savings:** not attributable yet.

## Cost Optimizations

1. **Fix the telemetry before tuning any model (prerequisite).**
   - **Evidence:**
     - The phantom Codex sum above.
     - Clarify and plan runs that failed before `Install Codex CLI` still record `codex_calls` of 2–6, for example 38992809814 (6 calls, 2 tokens).
     - Claude fields never reach run rows.
   - **Change:**
     - Anchor the regex to whole lines. It has to tolerate the Actions timestamp prefix and must not match indented comment or script lines.
     - Make the projection additive. This is backward compatible because no existing key changes:
     ```python
     _TS = r"(?:\d{4}-\d{2}-\d{2}T\S+Z\s+)?"
     CODEX_TOKENS_RE = re.compile(
         rf"^{_TS}tokens\s+used\s*\n{_TS}([0-9][0-9,]*)\s*$"
         rf"|^{_TS}tokens\s+used[:\s]+([0-9][0-9,]*)\s*$",
         re.IGNORECASE | re.MULTILINE,
     )
     # value = m.group(1) or m.group(2)

     RUN_COST_TELEMETRY_FIELDS = (
         ...,  # existing fields unchanged
         "claude_calls", "claude_input_tokens", "claude_output_tokens",
         "claude_cache_write_tokens", "claude_cache_read_tokens",
     )
     # build_run_cost_telemetry: also copy claude_cost_usd and a JSON-safe or_phases
     ```
     - Add negative fixtures taken from `implement.yml:3651-3653,3663,3665,3667,3773` and `clarify.yml:1524`. Assert they yield 0 calls.
   - **Savings:** none directly, but every Codex and Claude cost decision depends on these numbers.
   - **Quality risk:** none.
2. **Find out why review run 38987587333 cost 9.6× baseline.**
   - **Evidence:**

     | Run | Calls | Total tokens | Uncached prompt | Cache read | Hit rate | Duration |
     |---|---|---|---|---|---|---|
     | 38987587333 | 20 | 37,638,374 | 4,553,316 | 31,755,680 | 0.849 | 2,934s |
     | 38916979717 | 10 | 3,920,024 | 744,482 | 2,856,829 | 0.736 | 2,297s |

   - **Root cause:** unknown. A hypothesis (inference) is an agentic reviewer or editor loop that replays a growing conversation, since cache reads average about 1.59M per call. Telemetry cannot tell which phase is responsible, because `or_phases` is computed but dropped.
   - **Change:** propagate `or_phases` into run rows (Cost 1), and add the per-call size field described in D3.
   - **Savings:** if one phase accounts for the extra size, capping its turns or context could bring the run back near the 3–4M baseline, about 33M tokens per occurrence.
   - **Quality risk:** medium if turns are capped without evidence. Measure first.
3. **The e2e smoke test writes cache entries it never reads.**
   - **Evidence:** in 38986967451, 19 calls wrote 130,274 cache tokens out of 133,317 prompt tokens (97.7%) and read 0, so `cache_hit_rate=0.0`.
   - **Root cause (inference):** each smoke call has a unique prefix, or cache breakpoints sit after per-run dynamic content.
   - **Change:** for the smoke-test review phase only, set `OPENROUTER_PROMPT_CACHE_DISABLED=true`. The variable already exists (`clarify.yml:62`); its use in the smoke path still needs to be confirmed. Alternatively, move dynamic content after the breakpoint.
   - **Savings:** removes the cache-write surcharge on about 130K tokens per gate run. This is small in absolute terms.
   - **Quality risk:** none, since the cache currently provides no benefit here.
4. **Semble context is lean and not noisy.**
   - **Evidence:** 5 queries returned 58,724 bytes in total from 35 sources. Only 1,041 bytes (1.8%) were static duplicates. Review used 47,614 bytes, about 12K tokens, which is under 0.04% of run 38987587333's tokens.
   - **Assessment:** no change is needed. There is no control run, so any reduction in prompt expansion Semble provides cannot be measured.
5. **Serena** had zero `SERENA_*` events, so it cannot be evaluated (Reliability 6).

## Reliability Improvements

1. **`Resolve integration ref` refusals (14 of 15 failures). Category: deterministic input-contract mismatch.**
   - **Evidence:**

     | Workflow | Run IDs |
     |---|---|
     | clarify | 38959970027, 38983115876, 38983214967, 38983537115, 38984124962, 38986413434, 38989442558, 38991000287, 38992076216, 38992809814 |
     | plan | 38969365068, 38976601176, 38989786896, 38989791508 |

     The two 01:11 plan failures lasted 328s and 370s, compared with 22–31s for every other refusal. The step sits after the Codex cache restore (`plan.yml:120-137`), so runner queue time or a slow cache restore probably makes up the difference. This is unverified.
   - **Possible amplification (low confidence):**
     - Clarify fires on `issues.opened` by `github-actions[bot]` (`clarify.yml:25`).
     - If heal or triage issues filed after a refusal quote the triggering body, each new issue would be refused in turn. The repeating cadence fits this pattern.
     - `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH=3` and `MAX_ISSUES_PER_DAY=20` cap the loop.
     - D2 records the issue author type and marker position, which would confirm or rule this out.
   - **Fix:** land BUG-001 (Speed 1). Keep the fail-closed behaviour for issues whose triage header is on the first line.
   - **Expected impact:** the failure rate among executed runs drops from 4.95% to about 0.3%.
   - **Rollback:** the change is a one-line regex in the resolver's Python classifier.
2. **Heal intake already de-duplicates before calling the model, which helps.**
   - **Evidence:** the duplicate decision at `workflow_failure_heal_intake.sh:1322-1326` runs before `# --- Run the diagnosis model` at line 1589. The fingerprint is workflow + step + signature (line 990).
   - **Correction (inference):** the earlier report counted 543s of Opus time; that is probably too high. Repeat refusals with the same signature should exit as `duplicate` before diagnosis. The 69–73s intake runs (38992100284, 38992835808) fit an install-plus-dedup path, but about 55K characters of their logs were truncated.
   - **Change:** make the collector surface the intake's `fingerprint fp=… step=… runs=…` and `duplicate existing_issue=…` lines (D1 generalised). Then intakes that reach the model can be counted.
3. **Lineage state is left stale after an implement run completes (AI memory).** The `finalize-task` for issue 7519 (run 38993276204) shows implement run 38988118138 still `in_progress`. That run succeeded at about 01:17 (1,720s). Fix: see AI Memory Health.
4. **CI failure 38987587185 (1 of 17 CI runs).**
   - **Evidence:** step `Clarify loop guard unit tests` (`ci.yml:1383-1389`) runs four test files serially under `set -euo pipefail`. The row cannot show which file failed. Later CI runs (38989612657, 38991042020, 38991674846) succeeded.
   - **Fix:** run the four files in one `python3 -m pytest -q` invocation so a single failure names the file. If the files are plain scripts, emit `::group::<file>` around each instead. Behaviour does not change.
   - **Impact:** diagnosability only.
5. **Gate preemption (Speed 2).** Fail-open is already correct: `promote_main_cycle.sh:504-508` treats a cancelled gate as `skip_cycle smoke_gate_cancelled`, not as success. The waste is the only problem.
6. **Serena and Semble status.**
   - **Serena:** 0 probe, query and fallback events across 125 runs with telemetry. Either it is disabled or it is silent when skipped. Emit `SERENA_PROBE result=skipped reason=disabled` once per job so a broken rollout cannot hide.
   - **Semble fallbacks are healthy:**
     - 12 of the 13 were contract-test fallbacks (4 in CI 38987587185, 8 in 38986967451).
     - 1 was a runtime fallback (implement 38985760245).
     - There were 0 failed bootstraps.
     - Poller 38992457310 started with `SEMBLE_AVAILABLE=false` under `SEMBLE_BOOTSTRAP_MODE: lazy`, then bootstrapped twice (91,860ms) and queried twice. That is the expected lazy behaviour, not a fallback.
7. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** 0 of each in every family. No rubric or prompt-size pressure was observed, but log truncation (39K–830K characters) limits this conclusion.

### Proposed diagnostic-logging additions (ranked)

| ID | Where | Addition | Why | Fail-open |
|---|---|---|---|---|
| D1 | `scripts/collect_workflow_logs.py` (uses existing `_set_diagnostic_failure_reason`) | For failed runs whose log was downloaded, take the first line matching `::error::(INTEGRATION_REF_RESOLVE\|CI_TRIAGE_PR_BRANCH_ROUTING)\b.*` and store it under a new scope `error:`. Pass `diagnostic_failure_reason` through to `failing_runs` rows in `analysis_context.json`. | Classifies all 14 refusals by `reason=` without a deep dive. No new log prefix is needed. | Leave the field null if the log is missing or the pattern does not match. |
| D2 | `scripts/resolve_integration_ref.sh`, `CI_TRIAGE_PARSE_PY` before `refuse("label_missing")` | Print to stderr: `CI_TRIAGE_PR_BRANCH_ROUTING issue=<n> pr=<pr> outcome=refuse_detail reason=label_missing marker_line=<first index of the marker or -1> author_type=<user.type> managed=<bool> tracking_line=<bool>` | Confirms or refutes BUG-001 (mid-body marker) and the bot-issue loop. The line reuses the registered prefix and never ends with `reason=pr_not_open`, so the caller's grep at `clarify.yml:190` / `plan.yml:244` is unaffected. Do not append fields to the existing `outcome=refused` line, because that grep is anchored with `$`. | Stderr only. The stdout contract (`refuse <reason> <pr>`) is unchanged. |
| D3 | OpenRouter usage producers (`scripts/review_run_reviewers.sh`, `scripts/openrouter_prompt_cache.py`) | Add an optional trailing `prompt_bytes=<n> turn=<n>` after `usage_available=`. `OPENROUTER_RE` already treats trailing fields as optional. | Explains the 1.88M-tokens-per-call outlier. | Old parsers ignore the extra fields. |
| D4 | `scripts/cost_audit.py` | Anchored Codex regex plus `claude_*` and `or_phases` in run rows (Cost 1). | Makes cost telemetry trustworthy. | Additive keys only. |
| D5 | `promote-main-to-stable.yml` `promote` job, before the dispatch step | List `test-and-mark-stable.yml` runs with status in_progress or queued. Emit `::warning::PROMOTE_GATE_PREEMPT active_runs=<ids> actor=${{ github.triggering_actor }}`. | Makes gate cancellation attributable. | Warning only. A new prefix needs an `agents.d/<n>-promote-gate-preempt.md` fragment under "Stable log prefixes (contractual)" (§25). |
| D6 | `workflow-failure-heal-intake` and the autofix sweep | Record `GH_PAT_BUDGET phase=start`, using the pattern at `clarify.yml:75-87`, so `used_in_job` is computed. Every observed line currently says `used_in_job=unknown`. | Per-job API attribution. | Already fails open (`remaining=unknown`). |
| D7 | Validate wrapper | When the tracking number resolves to 0 (run 38987119757 `[tracking:0]` ran 771s), log the source of the tracking number and the reason. | A validate run with tracking 0 may be wasted compute. | Log only. |
| D8 | `ci.yml:1383-1389` | Use per-file `::group::` markers, or a single pytest invocation. | Names the failing test file. | n/a |
| D9 | `collect_workflow_logs.py` | Add `queue_seconds = first job started_at - run created_at` from the jobs payload already fetched for failed and slow runs. | Quantifies runner saturation. | Null on a missing field. |

## AI Memory Health

- **Telemetry found:** exactly 1 `AI_MEMORY_TELEMETRY` line in the whole window:
  `AI_MEMORY_TELEMETRY: {"did_push": true, "final_state": "closed", "issue_number": 7519, "ok": true, "op": "finalize-task"}`
  in run 38993276204 (`issue_pr_status`), with `push_attempts=1` and `did_commit=true`. There were no push retries. The step `Finalize linked issue lineage state` took about 104s of the 153s run.
- **Retrieve hit rate, `estimated_tokens` against budget, and `keyword_method` distribution cannot be computed.** No `retrieve` operations appear in any `log_summary`, and the deep-dive logs were not mounted. No `fail_open: true` or `enabled: false` entries were observed.
- **Anomaly:** the lineage for issue 7519 still shows implement run 38988118138 as `in_progress`, although that run completed successfully at about 01:17. The issue was then closed after PR #7525 was closed unmerged. The likely cause (inference) is that implement's terminal `record-run-event` was not emitted, or failed open silently.
- **Recommendation:**
  - Verify that implement emits `record-run-event` with the final state on every exit path, including success.
  - Have the collector count `AI_MEMORY_TELEMETRY` lines by `op` per run, so that missing retrieve and record events show up as zero counts rather than silence.
  - Memory maintenance run 38988761330 (489s, success) produced no telemetry in the supplied data.

## GH API Call Audit

- **Rate-limit headroom:** the two intake readings share a reset window: 4,796 remaining (38992100284, about 01:45) and 4,496 (38992835808, about 01:56). That is about 300 PAT calls in 11 minutes across the repo, roughly 27 per minute, or about 1,600 per hour if sustained. This is under the 5,000 budget but not idle. The poller 38992457310 (01:52–02:04) overlapped this interval and is the likely main consumer (inference).
- **No 429s or secondary rate limits** appear in any visible `log_summary`.
- **The hot paths are well-behaved:**
  - Lint PR body makes one paginated `pulls/<n>/commits` call per run (38993357327, 38992869084).
  - Cancel-on-close makes zero cancel calls when there is nothing to cancel (38993362737, 38992631951).
  - The autofix sweep skipped 2 PRs via the active-run check with `snapshot_incomplete=false` (38993237746).
- **Duplicated resolver reads:** each clarify and plan run makes one `repos/<repo>/issues/<n>` read through `get_issue_json`, plus a shallow clone of `main` and a fetch of `github.sha` (`clarify.yml:143-146`). Because the refusals repeat, this costs about 3 calls per refusal × 14, which is negligible. The more important point is that the roughly 125-line staging block exists in 5 copies (DUP-001 in the earlier report). It has not changed.
- **Gap:** every `GH_PAT_BUDGET` line says `used_in_job=unknown`, so no per-job attribution is possible (D6). This audit could not cross-check the batch helpers in §14 (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) against poller logs, because the logs were truncated by about 830K characters.

## Prompt Cache & Memory System

- **Overall `cache_hit_rate` is 0.8335.** It rests almost entirely on review/autofix at 0.8388 (34.6M of 34.6M cache-read tokens).
- **Per-run variance:** 0.849 (38987587333) against 0.736 (38916979717). The earlier report saw 0.451–0.536 runs. Fragmentation persists between runs.
  - **Likely cause (inference):** dynamic content placed before the cache breakpoint.
  - **Change:** order prompts as static rules, then the rubric, then Semble context, then the diff and findings. Log a prefix hash per call (D3) so this can be measured.
- **`test_and_mark_stable` is pure write overhead:** hit rate 0.0, with 130,274 tokens written (Cost 3).
- **No `CONTEXT_BUDGET_WARN` lines**, so there is no sign of prompt-size pressure eroding the cache.
- **Memory retrieval effectiveness cannot be assessed** (AI Memory Health).

## Orchestrator Health

- **Poller:** 62 runs, 58 successful and 4 cancelled. p50 is 570.5s and p95 is 893s. Runs start about every 8–12 minutes, so the poller is occupied almost continuously.
  - 38992350966 was cancelled before its first step after 98s. This is concurrency supersession and is expected.
  - 38992457310 waited about 165s for a runner.
  - Indicator to track: poll start-to-pickup delay, which needs D9.
- **Clarify-respond:** 153 runs, of which 4 succeeded and 149 were skipped or `other`. The executed slow runs took 707–731s (38990742901, 38983917083, 38961134217). No failures, so there is no loop signal.
- **Clarification blockage:** the 14 refusals are the dominant orchestrator pain point. Affected issues cannot progress past clarify or plan, and the step does not recover on its own (Reliability 1). Indicator to track: refusals per hour by `reason=` (D1).
- **Validate:** 2 runs. `[tracking:0]` 38987119757 ran for 771s, and `[tracking:7490]` 38917449931 ran for 945s. The first needs D7.
- **Release cycle:** gate preemption, covered in Speed 2.
- **Unblock judge** (11 runs, p50 38s) and **integration PR readiness** (18 runs) were all successful.

## Pipeline Flow Bottlenecks

| Stage | Representative slow runs (s) | Overhead type |
|---|---|---|
| clarify | 484–509 (38960548948, 38983542780, 38987127016) | compute |
| clarify-respond | 707–731 | compute |
| plan | 618–1,108 (38984434649 at 1,108) | compute |
| implement | 1,630–1,943 (38985760245, 38988118138, 38987942375) | compute (Codex usage unmeasured) |
| review/autofix | 1,664–2,934 (38987587333, 38989796391, 38991042054) | compute and tokens |
| CI | p50 823, p95 1,059 | compute, in parallel with review |
| gate | 5,419 (cancelled) | preemption |

- **Order of impact:**
  1. **Retry and refusal overhead:** clarify and plan refusals block whole issue lanes.
  2. **Review/autofix compute:** it is the longest per-PR stage and the largest token consumer.
  3. **Implement:** 27–32 minutes, but cost is unmeasured until D4 lands.
  4. **Queueing:** 18–165s per job when runners are saturated.
  5. **Merge and conflict overhead:** not observable. There are no conflict-heal signals in the data.
- **Fixes, in end-to-end order:** Speed 1, then D3 with Cost 2, then D4, then Speed 2.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Review/autofix: p95 2,320s, max 2,934s.
  - Implement: about 1,700–1,943s.
  - Poller occupancy: p50 570s every 8–12 minutes.
  - Runner queueing: 18–165s.
- **Top failure modes:**
  - `Resolve integration ref` refusals: 14 of 15 failures.
  - One CI unit-test failure (38987587185).
  - Gate preemption cancelled 38986967451 after 90 minutes.
- **Highest-cost drivers:**
  - Review run 38987587333: 37.6M tokens, 90.3% of measured tokens.
  - Review family overall: 99.7% of OpenRouter tokens.
  - Implement and heal (Codex and Claude): unmeasured.
- **Top 3 actions:**
  1. Land the BUG-001 anchoring fix, with D1 and D2 to confirm the reason.
  2. Fix the Codex parser and project Claude and `or_phases` into run rows (D4), then diagnose the 38987587333 outlier with D3.
  3. Add the preemption warning to the promote dispatch (D5).

## Metrics Appendix

**Run outcomes (window: 1,000 runs, capped)**

| Metric | Value |
|---|---|
| success / failure / cancelled / other | 279 / 15 / 9 / 697 |
| Failure rate (all rows / executed only) | 1.5% / 4.95% (15/303) |
| p50 / p95 duration | 1s / 722s |
| Failures at `Resolve integration ref` | 14 of 15 (93.3%) |

**Per family (executed runs)**

| Family | Runs | Success | Fail | Cancelled | p50 s | p95 s |
|---|---|---|---|---|---|---|
| clarify | 165 | 9 | 10 | 0 | 1 | 188.6 |
| plan | 152 | 8 | 4 | 0 | 1 | 481.6 |
| implement | 152 | 7 | 0 | 2 | 1 | 79.2 |
| review_autofix | 34 | 33 | 0 | 0 | 20 | 2,320.5 |
| ci | 17 | 16 | 1 | 0 | 823 | 1,058.8 |
| orchestrate_poll | 62 | 58 | 0 | 4 | 570.5 | 893.2 |
| orchestrate_clarify_respond | 153 | 4 | 0 | 0 | 1 | 10 |
| workflow_failure_heal_intake | 55 | 26 | 0 | 2 | 10 | 250.8 |
| promote_main_to_stable | 2 | 2 | 0 | 0 | 2,810 | 5,246.3 |
| test_and_mark_stable | 1 | 0 | 0 | 1 | 5,419 | 5,419 |
| validate | 2 | 2 | 0 | 0 | 858 | 936.3 |

**Tokens and cache**

| Metric | Value |
|---|---|
| OpenRouter calls (usage available / unavailable) | 49 (49 / 0) |
| OpenRouter prompt / completion / total | 5,431,115 / 301,659 / 41,697,365 |
| Cache write / read | 1,483,041 / 34,612,509 |
| `cache_hit_rate` (overall / review / test_and_mark_stable) | 0.8335 / 0.8388 / 0.0 |
| Codex calls / tokens (as reported) | 120 / 7,932,854 (**invalid**: parser artifact) |
| Claude calls / tokens | not projected into run rows (gap) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (n=119) | 1,000 / 4,971,700 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**GH API**

| Run | Job | Remaining | Reset | Used in job |
|---|---|---|---|---|
| 38992100284 | heal intake | 4,796 | 1792204652 | unknown |
| 38992835808 | heal intake | 4,496 | 1792204652 | unknown |
| 38993237746 | autofix sweep | 4,339 | 1792205300 | unknown |

**Semble**

| Family | Queries | Bytes | Sources | Static dup bytes | Bootstraps (unused) | Bootstrap ms | Fallbacks (contract / runtime) |
|---|---|---|---|---|---|---|---|
| review_autofix | 3 | 47,614 | 29 | 1,041 | 3 (0) | 130,779 | 0 / 0 |
| orchestrate_poll | 2 | 11,110 | 6 | 0 | 2 (0) | 91,860 | 0 / 0 |
| workflow_log_analysis | 0 | 0 | 0 | 0 | 4 (4) | 154,323 | 0 / 0 |
| ci | 0 | 0 | 0 | 0 | 0 | 0 | 4 / 0 |
| test_and_mark_stable | 0 | 0 | 0 | 0 | 0 | 0 | 8 / 0 |
| implement | 0 | 0 | 0 | 0 | 0 | 0 | 0 / 1 |
| **Total** | **5** | **58,724** | **35** | **1,041** | **9 (4)** | **376,962** | **12 / 1** |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | Queries | Response bytes | Fallbacks |
|---|---|---|---|---|---|---|
| (all) | 0 | 0 | 0 | 0 | 0 | 0 |

No other MCP servers were observed. No `<NAME>_QUERY`, `<NAME>_FALLBACK` or `<NAME>_PROBE` prefixes appeared beyond Semble.

**Data gaps and next collection steps**
- The deep-dive logs (errors, slow, recent) were not mounted. Mount them, or apply D1 so failure reasons travel in the run rows.
- The 1,000-run cap was hit: 69.7% of rows were skipped or `other`, and the window covers roughly 2026-10-16 14:24 to 2026-10-17 02:04.
- Codex token data is invalid, and Claude and `or_phases` are dropped (D4).
- There are no `retrieve` memory events and no per-job API usage (D6).
- No merged-PR counts or review-iteration counts were supplied.

**Assumptions:**
- The `label_missing` reason is inferred from source, not from the run logs.
- The gate cancellation is attributed from timing plus the `cancel-in-progress: true` concurrency group.
- The dedup-before-diagnosis ordering comes from reading the source, not from intake logs.
## Deep Audit — Workflows & Scripts (2026-10-17)

> **Scope and method.** This is the second deep-audit pass today. The first is in `analysis/workflow-optimization-2026-10-17.md`; it is cited below as "AM-report" and its IDs as "AM-BUG-001" and so on. I only cross-reference findings that pass already covered. Files read in depth for this pass: `resolve_integration_ref.sh`, `cost_audit.py` (regex, projection, `parse_log`), `promote-main-to-stable.yml` (`cycle` and `promote` jobs), `promote_main_cycle.sh` (head and steps 6–7), `auto_release_stable.sh`, `auto-release-stable.yml`, the resolver step and the Codex/summary steps of `clarify.yml`, the memory/lineage steps and the no-op guard of `implement.yml`, the lineage step of `issue_pr_status.yml`, `ai_memory.py` (`record-run-event`), `ai_memory_lib.py` (`finalize_task_lineage`), `label_helpers.sh`, `review_rb_judge.sh` (`_resilient_phase_swap`), `gh_retry` in `gh_helpers.sh`, `plan_split_dispatch.sh`, the API reads of `review_autofix_step_evaluate_gate.sh`, the capacity-wait path of `ai_engine_fallback_report.sh`, the guard job of `check-failure-triage-historical.yml`, and the four agent jobs of `workflow-log-analysis.yml`. This run had no shell, so file sizes are line counts and character counts are estimates (marked).

### Section 1: Bug & Correctness Sweep

**Still open from AM-report.** AM-BUG-001 (the mid-body triage marker causes `label_missing`; `resolve_integration_ref.sh:198`, `:205`, `:224-225`) is unchanged. AM-BUG-002 is also unchanged: `clarify.yml:211-214` and `plan.yml:287-290` still fall back to the default branch on resolver rc 2. `implement.yml:639-648` and `validate.yml:254-261` fail closed on that rc. Neither is restated here.

**BUG-001 — Cost telemetry counts echoed script text as Codex usage and drops all Claude and per-phase data**
- **File:** `scripts/cost_audit.py:62-64`, `:162-198`, `:562-566`, `:690-695`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** This repeats the Executive Summary, Cost 1 and D4 of this report, with exact source locations so it can be tracked as one item.
  - `CODEX_TOKENS_RE = re.compile(r"tokens\s+used[^0-9\n]*\n?\s*([0-9][0-9,]*)", re.IGNORECASE)` has no line anchor.
  - The run log echoes every step's script. Each echoed copy of the following lines therefore counts as one "Codex call" whose token count is a number taken from the script text:
    - `grep -A1 'tokens used' … | tail -1` at `clarify.yml:1524`, `plan.yml:1717`, `orchestrate.yml:1478`, `orchestrate_clarify_respond.yml:1387` and `implement.yml:3773`;
    - the comment examples and greps at `implement.yml:3650-3667`.
  - Separately, `build_run_cost_telemetry` copies only `RUN_COST_TELEMETRY_FIELDS` (`:162-198`). That tuple has no `claude_*`, `claude_cost_usd` or `or_phases` key, although `parse_log` computes all of them (`:642-657`, `:719-748`).
- **Recommended fix:** Apply the anchored regex and the additive field list already written in Cost 1 of this report. Add negative fixtures for the six workflow lines above. `or_phases` is a nested dict, so serialise it to a JSON-safe dict in `build_run_cost_telemetry`. Do not add it to `AGGREGATABLE_COST_FIELDS`.

**BUG-002 — A manual promote can cancel the daily cycle's in-flight smoke gate, because the `promote` job has no active-gate check**
- **File:** `.github/workflows/promote-main-to-stable.yml:401-444`. Related: `.github/workflows/test-and-mark-stable.yml:518-520`; existing guards at `scripts/promote_main_cycle.sh:446-466` and `scripts/auto_release_stable.sh:162-181`.
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The gate's `e2e-smoke-test` job uses `group: e2e-smoke-test-${{ inputs.test_repo || github.repository }}` with `cancel-in-progress: true`. Any new gate run therefore cancels the one in progress.
  - Both automated dispatchers wait for the gate to be idle first: the cycle loops on an active-run count until `gate_busy`, and `auto_release_stable.sh` skips with `release_in_flight`.
  - The `promote` job's `Dispatch test-and-mark-stable on stable` step calls `gh workflow run` straight away. Its `concurrency: promote-main-to-stable` (`:157-159`) serialises it only with `auto-release-stable.yml`. It does not serialise it with the `cycle` job, which has no group.
  - This is the mechanism behind the cancellation of 38986967451 (Speed 2 / D5; the causal link is inferred from timing).
  - `stable` has already been fast-forwarded at `:269-275` before the dispatch. So if the step were changed to *skip* rather than warn, the release would pass to `auto-release-stable.yml`. That run does not pass `version_tag` (`auto_release_stable.sh:202-204`), so it would cut a patch release instead of the intended minor bump.
- **Recommended fix:** Before `gh workflow run`, reuse the `auto_release_stable.sh:162-166` read (one `actions/workflows/test-and-mark-stable.yml/runs?per_page=30` call; better, the DUP-001 helper). If any active run's `display_title` contains `[cycle:`, emit `::warning::PROMOTE_GATE_PREEMPT active_runs=<ids>` (D5; it needs an `agents.d/` prefix fragment under §25). Keep the dispatch as it is by default. A bounded wait can come later behind a new defaulted variable, for example `PROMOTE_GATE_PREEMPT_WAIT_SECS` (default `0`). Check the name for collisions first.

**BUG-003 — Some guarded release reads exit without their documented `*_SKIPPED reason=guard_unavailable` line**
- **File:** `scripts/auto_release_stable.sh:105`, `:151`, `:162`, `:170`, `:176`; `scripts/promote_main_cycle.sh:467`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - Both scripts promise that "every exit is a logged, stable prefix" (`auto_release_stable.sh:12`, `promote_main_cycle.sh:22`), and both run under `set -euo pipefail` (`:53`, `:84`).
  - The listed lines are bare `var="$(gh_retry gh api … | jq …)"` assignments. If `gh_retry` exhausts its retries, the assignment fails and `set -e` ends the script with only gh_retry's `::error::gh command failed after N attempts`. The regex checks that follow, such as `:106` and `:457`, never run.
  - Contrast `auto_release_stable.sh:115-123` and `:130-132`, which map the same failure to `skip_release guard_unavailable`.
  - Effect (inference): a transient API failure becomes a failed scheduled run. That probably also starts a heal intake, where a clean skip was intended.
- **Recommended fix:** Append an explicit handler to each read, for example `runs_json="$(gh_retry gh api "…/runs?per_page=30")" || skip_release guard_unavailable "lookup=workflow-runs:${AUTO_RELEASE_STABLE_WORKFLOW_FILE}"`. Do the same at `promote_main_cycle.sh:467` with `skip_cycle guard_unavailable "lookup=workflow-runs-before"`.

**BUG-004 — The phase-label PUT can wipe every non-phase label when the caller lacks `pipefail`**
- **File:** `scripts/label_helpers.sh:221-222`, `:249-251`; `scripts/review_rb_judge.sh:959-960`, `:997-998`, `:1012-1016`. Behaviour source: `scripts/gh_helpers.sh:1083-1088` and `:1129-1137`.
- **Severity:** Low (latent)
- **Category:** `bug`
- **Description:**
  - `gh_retry` writes stdout only on success; on failure it prints nothing and returns 1.
  - Failure detection in `if ! _cur="$(gh_retry gh api --paginate …/labels --jq '[.[].name]' 2>/dev/null | jq -cs 'add // []')"` therefore depends entirely on the caller's `pipefail`. Without it, the pipeline's status is `jq`'s 0, and `jq -cs 'add // []'` on empty input yields `[]`.
  - The function then computes `_new = [target]` and PUTs it (`:249-251`). That replaces every label on the issue, including `ai:orchestrator-managed` and `ai:orchestrator-tracking`.
  - `label_helpers.sh` is a sourced library that sets no shell options itself (`:1-21`). It is meant for "workflow steps" (`:3`), and GitHub's default `run:` shell is `bash -e {0}`, without pipefail.
  - Every current caller does set it: `issue_pr_status.yml:210`, `review_autofix.yml:1525`, `review_autofix_step_mark_ready_to_merge.sh:7` and `review_autofix_step_mark_review_blocked_workflow_failure.sh:4`. So there is no live failure today.
- **Recommended fix:** Make the helper independent of the caller's shell options. Capture the raw output first, then merge the pages:
  ```bash
  local _raw=""
  if ! _raw="$(gh_retry gh api --paginate "repos/${repo}/issues/${issue_number}/labels" --jq '[.[].name]' 2>/dev/null)"
  then
  	# existing POST-add fallback, unchanged
  fi
  _cur="$(printf '%s' "${_raw}" | jq -cs 'add // []')"
  ```
  Apply the same change to both GETs in `_resilient_phase_swap`.

**BUG-005 — Implement's success-no-op path leaves a `phase_started` ledger event with no terminal event `[NEEDS VERIFICATION]`**
- **File:** `.github/workflows/implement.yml:2228-2242` (start), `:5943-5963` (success), `:6220-6263` (failure or cancel), `:4933-4966` (no-op guard)
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The start event runs whenever `SKIP_IMPLEMENT != 'true'`.
  - The success event requires `did_commit == 'true' && pr_url != ''`.
  - The failure event requires `failure() || cancelled()`.
  - The `Handle no-op implementation` step's Guard 0 treats `codex_success_noop.flag` as a *legitimate success*: it closes the issue with `ai:closed` and opens no PR.
  - If that run then ends green, the run ledger holds `phase_started` with no `phase_completed` or `phase_failed`. Not verified: whether a later step exits non-zero on this path.
- **Recommended fix:** Add a `Record implementation run no-op event` step guarded by `steps.commit_changes.outputs.did_commit == 'false' && success()`. It should call `memory_record_run_event --event-type phase_completed --status ok --message "Implementation phase completed without changes"`, which is the same helper as `:5963`.

**BUG-006 — An `Integration branch:` line anywhere in a CI-triage issue body skips triage verification `[NEEDS VERIFICATION]`**
- **File:** `scripts/resolve_integration_ref.sh:31`, `:409-417` (before the triage routing at `:419-429`)
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - `extract_integration_branch` uses a `MULTILINE` pattern that matches any line starting with `Integration branch:`.
  - `resolve_ref` acts on that line *before* `resolve_ci_triage_branch`. So an `ai:check-triage` issue whose body contains such a line routes to that branch, and none of the label, header, author or live-PR checks at `:226-251` and `:330-367` run.
  - This is the same unanchored-body-scan family as AM-BUG-001. Not verified: whether `check_failure_triage.sh` ever writes log excerpts that start a line with that text.
- **Recommended fix:** When routing is on and `child_json` carries `ai:check-triage`, run `resolve_ci_triage_branch` first and ignore the body's `Integration branch:` line. Add a fixture with both a valid triage header and an `Integration branch:` line; it should expect the PR head branch.

### Section 2: GitHub API Call Redundancy Audit

No new per-iteration REST loops were found in the files read this pass. AM-BATCH-001 (wave reconcile), AM-BATCH-002, AM-API-001 and AM-API-002 still apply and are not repeated.

**API-001 — The historical-triage guard lists every `ai:check-triage` issue ever filed, on every push to `main`**
- **File:** `.github/workflows/check-failure-triage-historical.yml:18-20` (trigger), `:75-78` (list), `:85`, `:97`, `:107`, `:123`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** `gh api --paginate --method GET repos/…/issues -f state=all -f labels=ai:check-triage -F per_page=100` runs on every push. The page count grows with the triage-issue history. Before the dedup hit, a push costs 1 + P calls, rising to P + 4 calls once the issue exists... more precisely: P calls when the issue already exists (the guard stops at `:79-81`), and P + 4 calls otherwise.
- **Recommended fix:** The single-use purpose is complete once the issue exists, so remove the workflow (DEAD-001). That brings the cost to **0 calls**. As an interim step, add `-f sort=created -f direction=desc` and stop at the first page that contains the marker. That is ≤ P calls, typically 1.

**API-002 — Each capacity-wait report re-reads the capacity tracker's whole comment history**
- **File:** `scripts/ai_engine_fallback_report.sh:252` (read), `:258` (one appended comment per wait)
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** Each wait adds a comment to the tracker, then the next wait paginates all of them (`--paginate --slurp …/comments?per_page=100`) to look for this run's own `run=${run_token}` marker. **Current:** ⌈C/100⌉ calls per wait, where C grows for as long as the tracker stays open. **Projected:** 1 call. `[NEEDS VERIFICATION]`: how long the tracker stays open in `provider_outage.py`.
- **Recommended fix:** Only this run can have posted the marker, so add `&since=<run start ISO time>`. `workflow_retro_fanout.sh:349` already uses the same `since=` pattern. Fail open to today's full read if the timestamp is unavailable.

**API-003 — The promote cycle lists the gate's runs twice back to back**
- **File:** `scripts/promote_main_cycle.sh:455-456` (idle check, `runs?per_page=30`), `:467` (`runs?event=workflow_dispatch&per_page=50`)
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** The final idle-check response comes from the same endpoint a few seconds earlier. **Current:** 2 calls per dispatched cycle. **Projected:** 1. Identification stays correct: `before_gate_ids` only has to exclude earlier runs titled `[cycle:${GITHUB_RUN_ID}]`, for example from a job re-run, and the title filter at `:484` already covers that.
- **Recommended fix:** Use `per_page=50` in the idle query, keep its last response, and derive `before_gate_ids` from it with `jq -c '[.workflow_runs[]? | select(.event == "workflow_dispatch") | .id]'`. If the DUP-001 helper lands, have it return the raw response as well.

**Retry hygiene:** no new tight loops. `plan_split_dispatch.sh:179-187` wraps a single-attempt POST (`gh_retry` drops unsafe POSTs to 1 attempt, `gh_helpers.sh:1066-1069`) in a 3-try loop with a 2s sleep. That is bounded, and a duplicate release marker is harmless by design (`:170-171`).

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The "active workflow runs" jq filter is copied 6 times, and 3 variants disagree on which statuses count as active**
- **File:**
  - Identical copies: `scripts/apply_analysis_on_main.sh:309`, `scripts/auto_release_stable.sh:166`, `:173`, `:177`, `scripts/promote_main_cycle.sh:343`, `:456`.
  - Divergent variants: `scripts/gh_helpers.sh:2143` (`queued|pending|in_progress`, without `waiting`/`requested`), `scripts/gh_helpers.sh:2167` (all five), `.github/workflows/cancel_on_pr_close.yml:240` and `scripts/worktree_registry.sh:252` (`queued|in_progress` only).
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Every copy is a `gh api …/actions/workflows/<file>/runs` read followed by the same `select(.status == "queued" or …)` count. The divergent copies treat runs waiting on environment approval (`waiting`) as idle. BUG-002's fix would add a seventh copy.
- **Recommended fix:** Add one helper to `scripts/gh_helpers.sh`. Document its batching contract per §14: 1 call, prints a count, returns 1 on API failure so the caller fails closed.
  ```bash
  gh_workflow_active_run_count()
  {
  	# <repo> <workflow_file> [exclude_run_id] [per_page]
  	...
  }
  ```
  Update the six identical callers and the `promote` dispatch step. Keep the divergent variants as they are unless a separate change confirms they should match.

**DUP-002 — Codex `tokens used` parsing is implemented four different ways, and the most common one misreads the single-line format**
- **File:**
  - Naive `grep -A1 'tokens used' … | tail -1 | tr -d '[:space:]'`: `clarify.yml:1524`, `plan.yml:1717`, `orchestrate.yml:1478`, `orchestrate_clarify_respond.yml:1387`, `implement.yml:3773`.
  - Robust two-format parser: `implement.yml:3663-3670`.
  - Python variants: `scripts/ledger_emit_substate.sh:158-162` and `scripts/cost_audit.py:62-64`.
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - For the single-line form `tokens used 12345`, which `implement.yml:3652` documents, the naive parser returns the line *after* the match. Run summaries then show non-numeric text, and the `TOKEN_WARN_THRESHOLD` check is silently skipped through `2>/dev/null`, for example at `clarify.yml:1535`.
  - The same file, `implement.yml`, uses both parsers.
- **Recommended fix:** Add `codex_tokens_used <log_file>` to `scripts/codex_helpers.sh`, containing the `implement.yml:3663-3670` logic. It should print digits or nothing. Replace the five naive call sites and the per-attempt loop with it. Keep `ledger_emit_substate.sh`'s Python parser, which reads the codex log file and not the run log, but align it to the same two formats.

**DUP-003 — `workflow-log-analysis.yml` repeats a ~10-step agent toolchain and three in-step helper sets across its jobs**
- **File:** `.github/workflows/workflow-log-analysis.yml`
  - Toolchain blocks: `:246-383`, `:822-994`, `:1661-1856`, `:2268-2455` (Resolve AI engine → Install Claude → Cache/Install/Persist/Restore Codex → config → uv → semble install → semble index).
  - `_wla_label_retry` / `_wla_label_create`: `:1078-1079`, `:1877-1878`, `:2477-2478`.
  - `wla_engine_ready`: `:490`, `:1336`, `:2031`, `:2630`.
  - Failure emitters: `emit_log_analysis_phase_failure` `:1100-1193`, `emit_deep_audit_failure` `:1890-1966`, `emit_api_redundancy_failure` from `:2490`.
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - 8 of the 10 "Persist/Restore Codex CLI" steps in the whole repo are in this one file; the other 2 are in `plan.yml`.
  - Every job builds the Semble index eagerly (`:376`, `:980`, `:1842`, `:2441`). That is consistent with Speed 3's "4 bootstraps, all unused", though attributing those to these steps is an inference.
  - `_wla_label_create` re-implements `ensure_label_exists` (`label_helpers.sh:151-193`).
- **Recommended fix:**
  - Create `.github/actions/wla-agent-setup/action.yml` (inputs: `model`, `reasoning`, `semble_mode` defaulting to `lazy`) and replace the four blocks with it.
  - Create `scripts/workflow_log_analysis_helpers.sh` exposing `wla_engine_ready` and `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <summary> <heading>`. Source it from the three pass steps, and use `ensure_label_exists` in place of `_wla_label_create`.
  - This also removes most of the interpolated text behind EXPR-001.

**DUP-004 — Two resilient phase-label swaps with different phase lists**
- **File:** `scripts/label_helpers.sh:201-265` (`set_issue_phase_label_resilient`, list at `:149`); `scripts/review_rb_judge.sh:954-1020` (`_resilient_phase_swap`, list at `:957`)
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Both functions do GET labels → compute the phase-free set → PUT or POST. The judge's copy adds terminal-label precedence, and its 10-label list has drifted from the contract (CONSIST-002).
- **Recommended fix:** Add an optional 4th argument, `preserve_terminal=true`, to `set_issue_phase_label_resilient`, carrying the `:977-1010` logic. Make `_resilient_phase_swap` a thin wrapper around it, and land BUG-004's fix once in the shared helper.

### Section 4: Expression Size Limit Risk Assessment

`tests/test_workflow_file_size_limit.py:32` and `:66-98` already fail CI for any interpolated `run:` body of 18,000 characters or more. Every body below is therefore under 18,000 as long as CI is green. AM-EXPR-001, -002, -003, -004 and -006 still apply.

**EXPR-001 — Three Codex-pass bodies in `workflow-log-analysis.yml` are expression-evaluated only because of a few `github.*` interpolations**
- **File:** `.github/workflows/workflow-log-analysis.yml:1068-1402` (`Run workflow log analysis`, 335 lines; interpolations at `:1113-1115`, `:1128`, `:1134-1136`, `:1173-1175`), `:1869-2123` (`Run deep audit pass`, 255 lines; `:1901-1903`, `:1915`, `:1921-1923`, `:1954-1956`), `:2469-2722` (`Run API redundancy pass`, 254 lines; `:2501-2503`, `:2515`, `:2521-2523`, `:2554-2556`)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - Every interpolation is `github.run_id`, `github.run_attempt`, `github.workflow`, `github.server_url` or `github.repository`, yet each one makes the entire body a single template expression.
  - Estimated sizes `[NEEDS VERIFICATION]`:
    - The first body is about 15,000–17,500 characters, which is in the 71–85% band and leaves roughly 3,500–6,000 characters of headroom.
    - The other two are about 12,000–14,000 each.
  - Each pass gains lines whenever a new engine or fallback branch is added, as the `:1329-1375` Claude/capacity-wait block shows.
- **Recommended fix:** Replace each value with the runner's default environment variable: `${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}`, `${GITHUB_SERVER_URL}` and `${GITHUB_REPOSITORY}`. They are identical in value, including under `workflow_call`. All three bodies then have no `${{ }}` at all and drop out of expression evaluation. The behaviour does not change. DUP-003 then shrinks the bodies further.

**EXPR-002 — Workflow file size: no new measurement**
- **File:** `.github/workflows/test-and-mark-stable.yml` (7,551 lines), `implement.yml` (6,494), `review_autofix.yml` (5,690), `plan.yml` (3,149), `workflow-log-analysis.yml` (2,833)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** Line counts are consistent with AM-EXPR-005's byte estimates (largest about 417 KB). Nothing is near the 800 KB mark in this prompt. The binding limit here is §24's 480,000-byte guard. `[NEEDS VERIFICATION]`: exact byte counts, since no byte counter was available in this run.
- **Recommended fix:** No new action beyond AM-EXPR-005. Run `wc -c` in the next pass that has a shell.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — `check-failure-triage-historical.yml` is a registered single-use workflow that still runs on every push to `main`**
- **File:** `.github/workflows/check-failure-triage-historical.yml:1-25`. Registry: `docs/scripts-pending-removal.md:223-231`.
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** The header says it skips itself once the triage issue for run 37922279826 exists. Each push still spends a runner on the guard job plus the paginated listing (API-001). The concurrency group (`:24`) and the env (`:31-33`) hard-code the historical run. It is in the same state as AM-DEAD-001 (`pr6576-shard-followup.yml`).
- **Recommended fix:** Run the three removal preflight checks in the registry entry. If they pass, delete the workflow, `tests/test_check_failure_triage_historical.py`, its `ci.yml:1904` line, the `tests/test_workflow_checkout_integration_ref_audit.py` entry and the registry entry in one PR (§20.F).

**CONSIST-001 — The AI Memory Health "stale `in_progress` lineage" anomaly is expected behaviour, not a missing event**
- **File:** `.github/workflows/implement.yml:5784-5804`; `scripts/ai_memory_lib.py:2919-2972`; `.github/workflows/issue_pr_status.yml:701-724`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - Implement calls `memory_finalize_task --final-state in_progress` after it opens its PR.
  - `finalize_task_lineage` *appends* a `runs[]` entry whose `status` is the task state at that moment (`:2962-2971`). It deduplicates only by `(run_id, run_attempt)` (`:2905-2916`), so earlier entries are never updated.
  - The later `issue_pr_status` finalize adds its own `closed`/`merged` entry.
  - Implement run 38988118138 showing `in_progress` is therefore correct, and `record-run-event` (`ai_memory.py:355-396`) writes the run ledger, not lineage `runs[]`. The AI Memory Health recommendation to "verify implement emits record-run-event with the final state" targets the wrong mechanism. The real ledger gap is BUG-005.
- **Recommended fix:** Change the AI Memory Health text to match. Optionally, add a new field named `task_state_at_run` next to `status` in `finalize_task_lineage` so readers do not read it as the run's conclusion. Do not rename `status` (§10).

**CONSIST-002 — `_resilient_phase_swap` uses 10 of the contract's 20 `issue_phase` labels `[NEEDS VERIFICATION]`**
- **File:** `scripts/review_rb_judge.sh:957` vs `.github/ai/label_contract.v1.json:245-270` and `scripts/label_helpers.sh:149`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** The judge's list leaves out `ai:validating`, `ai:validated`, `ai:validation-failed`, `ai:validation-fixing`, `ai:validation-recovery`, the three `ai:security-pass*` labels, `ai:needs-human` and `ai:blocked`. A judge swap to `ai:ready-to-merge` therefore leaves those phase labels in place, and the PUT path (`:1012-1016`) keeps them next to `ai:merged`/`ai:closed`. Not verified: whether the judge deliberately leaves validation and security labels alone.
- **Recommended fix:** If it is deliberate, add a comment citing the contract group and the reason. If not, use `_AI_PHASE_LABELS` from `label_helpers.sh`, which is already sourced by the judge's caller chain (`review_rb_judge.sh` defines label fallbacks at `:930-937`). That arrives naturally with DUP-004.

**CONSIST-003 — The review gate uses raw `gh api` although it defines `gh_api_retry`**
- **File:** `scripts/review_autofix_step_evaluate_gate.sh:79` (fallback definition) vs raw calls at `:190`, `:279`, `:334`, `:343-347`, `:718`, `:1327`, `:1331`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** A single transient error means the marker read reports `gate_marker_fetch_state="api_error"` (`:350`), or the PR-files cache is left empty (`:718-720`). Both fail safe, keeping the normal review, so the cost (inference) is a full reviewer panel instead of a deterministic skip, on the most expensive workflow (see Cost 2).
- **Recommended fix:** Route the GET reads through `gh_api_retry`, which is already available at `:79`. Leave the POST at `:1331` as a single attempt, which is `gh_api_retry`'s default for unsafe POSTs.

**SHELL-001 — The unquoted `[ $IDLE -ge $INACTIVITY_LIMIT ]` from AM-SHELL-001 has four more copies**
- **File:** `.github/workflows/test-and-mark-stable.yml:902`, `:1494`, `:2466`, `:3806` (plus `:1194`, already reported)
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** SC2086. All five wait loops carry the same failure mode: an empty value gives an `integer expression expected` error under `set -e`.
- **Recommended fix:** Quote both operands at all five sites: `[ "$IDLE" -ge "$INACTIVITY_LIMIT" ]`.

**Debt markers:** none. A case-sensitive search for `TODO`, `FIXME`, `HACK` and `XXX` across `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` returned zero matches, which agrees with the AM-report.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 3 | BUG-001, BUG-002, EXPR-001 |
| Low | 17 | BUG-003, BUG-004, BUG-005, BUG-006, API-001, API-002, API-003, DUP-001, DUP-002, DUP-003, DUP-004, EXPR-002, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | none at Critical/High in this pass. AM-BUG-001 (`scripts/resolve_integration_ref.sh` plus 2 fixtures) is still the top open item | Small |
| API call optimization | `.github/workflows/check-failure-triage-historical.yml` (removal), `scripts/ai_engine_fallback_report.sh`, `scripts/promote_main_cycle.sh` | Small |
| Code modularization | `scripts/gh_helpers.sh` (`gh_workflow_active_run_count`), `scripts/codex_helpers.sh` (`codex_tokens_used`), new `.github/actions/wla-agent-setup/action.yml`, new `scripts/workflow_log_analysis_helpers.sh`, `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`, `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `implement.yml`, `workflow-log-analysis.yml`, `auto_release_stable.sh`, `apply_analysis_on_main.sh` | Medium |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml` (3 step bodies) | Small |
| Medium/Low fixes | `scripts/cost_audit.py` (+ test fixtures), `.github/workflows/promote-main-to-stable.yml` (+ `agents.d/` prefix fragment), `scripts/auto_release_stable.sh`, `scripts/promote_main_cycle.sh`, `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/implement.yml`, `scripts/resolve_integration_ref.sh` (+ fixture), `scripts/review_autofix_step_evaluate_gate.sh`, `.github/workflows/test-and-mark-stable.yml`, this report's AI Memory Health section | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-17)

### Safety Tag Legend
`SAFE_TO_MERGE` means static reading proves every precondition: same endpoint or a strict field subset, the same pagination, filters and auth, the same function or step, no mutation in between, unchanged error handling, no concurrency boundary crossed, and any cache contract kept. The implement stage may apply these directly. `NEEDS_VERIFICATION` means the change is probably correct but at least one precondition cannot be proven from the source alone. The listed checks must pass before anyone implements it. `RISKY_SKIP` means at least one hard trigger applies: a retry, backoff, wait or stall path; an auth or identity probe; `--paginate` or manual paging; a rate-limit-sensitive path; code that guards against upstream races; or log keys that other code greps for. These findings are listed so they are visible. They must not be auto-implemented, and some exist mainly to stop a future pass from wrongly merging calls that are deliberately separate.

### Consolidation Candidates (MERGE-###)

**MERGE-001 — The merge-train release loop lists the same PR's comments twice, once per marker**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `scripts/review_merge_train.sh:1393`: `_mt_find_marker_comment_id "${num}" "${MT_MARKER}"`, which calls `_mt_find_marker_comment` at `:800-806` and through it the paginated listing at `:804`.
  - `scripts/review_merge_train.sh:1413`: `_mt_upsert_comment "${num}" "${MT_RELEASED_MARKER}" …`, called with no 4th argument. At `:875` it calls `_mt_find_marker_comment_id` again, which repeats the paginated listing at `:804`. If a marker is found, the body GET at `:879` follows.
- **Current call count:** 2 × P listing pages per released PR, where P = ⌈comments/100⌉, plus 0 or 1 comment GET.
- **Proposed call count:** P listing pages per released PR.
- **Endpoint(s):** `GET repos/{owner}/{repo}/issues/{pr}/comments?per_page=100` (`--paginate`) and `GET repos/{owner}/{repo}/issues/comments/{id}`.
- **Evidence:** both lookups filter the same paginated response the same way, by author login and by `startswith(marker)`:
  ```bash
  gh_retry gh api --paginate "repos/${MT_REPO}/issues/${pr}/comments?per_page=100" \
  	--jq ".[] | select((.user.login // \"\" | ascii_downcase) == \"${MT_AUTOMATION_LOGIN}\" and ((.body // \"\") | startswith(\"${marker}\"))) | …"
  ```
  The markers do not share a prefix (`:134` `<!-- merge-train:queued -->`, `:135` `<!-- merge-train:released -->`, `:137` `<!-- merge-train:queue-retired -->`), so one listing could return the latest comment for each marker.
- **Proposed fix (manual review only):** add `_mt_find_marker_comments_multi <pr> <marker>...`, which returns `marker id created_at` lines from a single `:804`-style listing. At `:1393`, capture the `MT_RELEASED_MARKER` id as well, and pass it to `_mt_upsert_comment` as its 4th argument at `:1413`, which `:873-874` already supports.
- **Safety rationale:** several hard triggers apply.
  - The call is paginated.
  - It runs once per PR inside the release loop (`continue` at `:1386`, `:1390`, `:1395`).
  - Three mutations sit between the two lookups: the PATCH at `:1398`, the label-DELETE claim at `:1406` and the dispatch at `:1410`.
  - The claim exists to defend against concurrent release runs (`:1403-1405`). Another release run could post a `released` marker in that window, and a snapshot taken at `:1393` would miss it.
- **Downstream signal:** do not auto-implement. A reviewer must confirm that only the claiming run can write `MT_RELEASED_MARKER` once the DELETE at `:1406` succeeds, which would make a pre-claim snapshot authoritative. They must also confirm that changing the shape of the listing output keeps the `LC_ALL=C sort -k2,2r -k1,1nr` newest-first ordering per marker.

**MERGE-002 — `auto_release_stable.sh` makes three workflow-scoped run listings that cannot safely become one repo-wide listing**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/auto_release_stable.sh:162` (`test-and-mark-stable.yml` runs), `:170` (`promote-main-to-stable.yml` runs) and `:176` (`mark-stable.yml` runs), each with `per_page=30`.
- **Current call count:** 3 per tick that reaches the guard.
- **Proposed call count:** in theory 2 (keep `:162`, merge `:170` and `:176` into one `actions/runs` read filtered by `.path`). This pass recommends keeping 3.
- **Endpoint(s):** `GET repos/{owner}/{repo}/actions/workflows/{file}/runs?per_page=30` (×3). The merge candidate would be `GET repos/{owner}/{repo}/actions/runs?per_page=100`.
- **Evidence:** the three responses are reduced with the same active-status jq at `:166`, `:173` and `:177` and added together at `:174` and `:178`. The repo-wide endpoint would return all three workflows in one call. However, `runs_json` from `:162` is also read at `:188` and `:191` for `failed_attempts_on_tip`, which needs the gate's own completed history. The report records about 1,000 runs per ~12h window, so a repo-wide page of 100 covers roughly 1.2h. A `waiting` or `queued` promote or mark-stable run older than that would drop out of the window.
- **Proposed fix:** none for now. If DUP-001's `gh_workflow_active_run_count` helper lands, it must keep one workflow-scoped call per workflow file.
- **Safety rationale:** this is a race guard. `release_in_flight` at `:179-181` stops a gate dispatch that would cancel an in-flight gate. A smaller window would fail open and allow the cancellation behind BUG-002.
- **Downstream signal:** do not auto-implement. When DUP-001 is implemented, a reviewer must check that the helper does not fold these three per-workflow reads into one repo-wide read.

**MERGE-003 — The review gate's second `pulls/{n}` read is a deliberate live-head recheck and must not reuse `_pr_gate`**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/review_autofix_step_evaluate_gate.sh:87-88` (`gh_api_retry "repos/${REPOSITORY}/pulls/${PR_NUMBER}"`, the full projection) and `:1327` (`gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq 'if .state == "open" then .head.sha // "" else "" end'`).
- **Current call count:** 2 on the skip-notice path.
- **Proposed call count:** 2 (no change).
- **Endpoint(s):** `GET repos/{owner}/{repo}/pulls/{n}`.
- **Evidence:** both calls hit the same endpoint, and `:1327` reads only `.state` and `.head.sha`, a strict subset of `_pr_gate`. The code comment at `:1323-1326` explains why they are separate:
  ```bash
  # The earlier PR fetch established this head for the gate, not
  # for the comment POST: a push may have moved it in between.
  # This read cannot reuse the earlier snapshot; on failure or
  # a moved head, do not publish stale review-skip evidence.
  ```
- **Proposed fix:** none. This entry stops CONSIST-003 (moving the gate's reads to `gh_api_retry`) or a later consolidation pass from replacing `:1327` with `${pr_head_sha_gate}`.
- **Safety rationale:** a fresh read is the only protection against a head change between the gate decision and the POST at `:1331` (a time-of-check/time-of-use race). Merging the two reads would publish stale skip evidence.
- **Downstream signal:** do not auto-implement. When CONSIST-003 is applied, `:1327` may be wrapped in `gh_api_retry` for retries, but it must remain a separate live read.

**MERGE-004 — `_resilient_phase_swap`'s second labels GET is a race reconcile, not a duplicate**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/review_rb_judge.sh:959-960` (first `GET …/labels`, paginated) and `:997-998` (second `GET …/labels`, paginated).
- **Current call count:** 2 GETs on the non-terminal path, plus 1 POST, D DELETEs and 0 or 1 more DELETE (as documented at `:952-953`).
- **Proposed call count:** 2 GETs (no change).
- **Endpoint(s):** `GET repos/{owner}/{repo}/issues/{n}/labels` (`--paginate`).
- **Evidence:** both calls are identical, with the same endpoint and `--jq '[.[].name]' | jq -cs 'add // []'`. The second exists to detect an `ai:merged` or `ai:closed` label that `issue_pr_status.yml` added while the swap was running (`:968-976`, `:1002-1009`, incident `tele-funtoken-msg-scoring#4379`).
- **Proposed fix:** none. BUG-004 and DUP-004 both rewrite this function. Whoever implements them must keep two separate GETs. BUG-004's capture-then-merge change applies to each GET on its own.
- **Safety rationale:** the second read guards against a race with another writer, and both reads are paginated.
- **Downstream signal:** do not auto-implement any consolidation. When DUP-004 is implemented, a reviewer must confirm that the shared helper with `preserve_terminal=true` still performs the post-mutation GET.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — `_mt_upsert_comment` re-fetches a marker comment body that the listing it came from already returned**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/review_merge_train.sh:804` (paginated listing in `_mt_find_marker_comment`, already filtered to the marker comment) and `:879` (`gh_retry gh api "repos/${MT_REPO}/issues/comments/${existing_id}" --jq '.body'`).
- **Callers:** on the gate path, `:958` lists the comments and `:993-999` passes `"${queued_comment_id}"` as the 4th argument. On the release path, `:1413` lists again inside the upsert (see MERGE-001).
- **Current call count:** 1 body GET per upsert when a marker comment exists.
- **Proposed call count:** 0 body GETs.
- **Endpoint(s):** `GET repos/{owner}/{repo}/issues/comments/{id}`.
- **Evidence:** the `.body` at `:879` belongs to the comment the `:804` listing selected with `(.body // "") | startswith(marker)`, so the listing already had it. `:879` only compares bodies to skip a no-op PATCH (`:880-882`).
- **Proposed fix (manual review only):** have `_mt_find_marker_comment` also print `(.body | @base64)` as a third field. Bodies span several lines, so they cannot be printed raw in this line-oriented output. Let `_mt_upsert_comment` take a 5th argument with the cached body, and keep `:879` as the fallback when that argument is absent.
- **Safety rationale:** this changes the projection of a paginated call whose output is sorted line by line (`:805`). The `:879` GET is also a fresh read taken after the gate's possible bypass PATCH at `:967`, which can fail at `:973` after being applied on the server. A cached body could then skip a PATCH that was needed.
- **Downstream signal:** do not auto-implement. A reviewer must check that `sort -k2,2r -k1,1nr` still selects correctly with a third field. They must also check that no PATCH to the same comment can fall between the listing and the upsert on either path (`:967` on the gate path, `:1398` on the release path, which targets a different marker's comment).

### Dead Calls (DEAD-API-###)

No findings. Every read checked in this pass feeds a branch, a log line or an output:
- `check-failure-triage-historical.yml`: `:85` feeds the cap, `:97` feeds `head_repo` and `merge_commit_sha`, `:107` feeds `head_sha` and `details_url`, and `:123` feeds `log_available`.
- `promote_main_cycle.sh`: `self_runs_json` is used at `:343` and again at `:403`.
- `ai_engine_fallback_report.sh:229`: `login` feeds the marker-author check at `:254`.

### Cross-References to Deep Audit Section
- API-001: `RISKY_SKIP`. Its interim fix (`sort=created&direction=desc`, stop at the first page with a match) changes how pages are walked on a `--paginate` read (`check-failure-triage-historical.yml:75-78`). Prefer removal under DEAD-001.
- API-002: `RISKY_SKIP`. It is a paginated read on the capacity-wait path, and the tracker marker is what keeps stall recovery away from the item (`ai_engine_fallback_report.sh:278-280`). New detail: `run_token` is `RUN_ID` (`:216`), which every re-run attempt shares. A `since=` set from the current attempt's start would therefore miss a marker posted by an earlier attempt, and `:258` would post a duplicate wait comment. Any `since=` must use the run's first-attempt creation time, and it must come from data already in the environment, not a new API call.
- API-003: `RISKY_SKIP`. The idle read at `promote_main_cycle.sh:455-456` sits inside the gate-idle wait loop (`:454-466`). The two reads also use different filters: `:455` has no event filter and `per_page=30`, while `:467` uses `event=workflow_dispatch&per_page=50`. Deriving `before_gate_ids` from 50 runs of all events would shrink the exclusion window that protects the title match at `:484` against earlier same-title runs.
- DEAD-001 (Section 5; it determines whether API-001 costs anything): `NEEDS_VERIFICATION`. The registry's removal trigger (`docs/scripts-pending-removal.md:227`) requires the triage issue to be *closed*, but the guard stops as soon as the issue *exists* (`:79-81`). Run preflight checks `:229-231` before deleting.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | DEAD-001 (cross-reference) |
| RISKY_SKIP | 8 | MERGE-001, MERGE-002, MERGE-003, MERGE-004, REUSE-001, API-001 (cross-reference), API-002 (cross-reference), API-003 (cross-reference) |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.