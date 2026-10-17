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