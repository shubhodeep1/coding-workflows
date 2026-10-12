## Executive Summary

- **Fix telemetry accounting before using it to justify spend changes.** In `shubhodeep1/coding-workflows`, review runs `38247461041` and `38246098799` each contain identical OpenRouter usage lines in both the job log and a step log. They account for **23,333,522 duplicated tokens—32.7% of the reported 71,341,349**. The reported total is not a reliable spend figure. Deduplicate at collection and log suppressed-event counts. **Estimated impact:** measurement accuracy, not actual token savings. **Confidence: high.**
- **One workflow-size defect caused six of 12 CI failures.** Runs `38231787878`, `38231791899`, `38234600809`, `38234605778`, `38237682041`, and `38237688201` failed the 18,000-character guard: `.github/workflows/update_workflows.yml`, `update-wrappers / Commit and push updates`, measured **19,821 characters**. Move expression values out of the inline `run:` body without weakening the guard. **Estimated impact:** remove this six-run failure pattern; no guaranteed change to unrelated failures. **Confidence: high.**
- **Long review runs include substantial pre-agent delay.** In `38232003857`, the gate log ended **66m38s** before `codex-agent` began; in `38246151160`, the gap was **55m27s**. The available data cannot separate runner queueing from dependencies. Add job-timing diagnostics from already-fetched job metadata before changing concurrency. **Estimated headroom:** up to the observed gaps, conditional on their cause. **Confidence: high for gaps, low for recoverable time.**
- **A closed PR became a failed review.** Run `38232003857` set `PR_CLOSED=true`, then `Checkout PR head branch` failed because the skipped workspace was unavailable; no agent tokens were logged. Guard workspace-dependent steps on the closed-PR state while retaining the error for a missing workspace on an open PR. **Estimated impact:** prevent the observed false failure and avoid unnecessary setup on similarly closed PRs. **Confidence: high.**
- **Merge-queue pressure is visible, but API volume is not.** Sweep `38253333868` examined **38 candidates**, skipped **32 merge-queued** and **four active**, and dispatched **two**. Close cleanup `38251322512` examined **32 PRs in about 188s**, releasing none; PR `#7259` retained nine listed blockers. Existing `GH_PAT_BUDGET` observations do not provide attributable call counts. Add per-cycle cache-hit and endpoint-call counters before changing the safety checks. **Confidence: high for outcomes, low for API-savings estimates.**

## Speed Optimizations

1. **Diagnose gate-to-agent delay — critical-path opportunity.** The `review / gate` to `review / codex-agent` gaps were **66m38s** in `38232003857` and **55m27s** in `38246151160`, versus an approximately **8m14s** gap in `38232028208`. **Root cause: unclassified**; workflow-level `run_started_at` does not explain job scheduling. **Change:** have the collector report `gate_finished_at`, `agent_queued_at`, `agent_started_at`, dependency gap, and runner-queue gap using its existing jobs payload. Then coalesce only demonstrably superseded, same-head work; keep required gates. **Savings:** potentially minutes to the observed gap, not yet forecastable. **Risk:** low for logging; medium for dispatch changes.

2. **Make check-run waits explainable, not silently shorter — critical path.** `review / codex-agent / Collect PR check-run failures` waited its **300s** limit in both `38246098799` (one check still active; **101,381-byte** context) and `38246151160` (two active; **544-byte** context). The bounded wait protects review quality; shortening it could omit late CI failures. **Change:** emit `CHECK_RUNS_WAIT_SUMMARY` with elapsed time, snapshot count, pending count, timeout outcome, and whether the head changed; assess whether independent preparation can overlap the wait without changing the final snapshot or merge guard. **Savings:** up to five minutes *only if safe overlap is demonstrated*. **Risk:** low for logging, medium for reordering.

3. **Profile the long CI jobs before splitting more work — critical path.** `CI / tests-release-and-log-analysis` took approximately **604s** in `38250939132` and **535s** in `38251668637`; their workflows took **934s** and **914s**. **Root cause below job level is unmeasured.** **Change:** emit per-test-group duration and setup/compute breakdowns, then move an independently verified slow group within the existing parallel-job structure. **Savings:** bounded by the measured 535–604s job segment; no defensible narrower estimate yet. **Risk:** low for profiling, medium for test movement.

4. **Keep Semble lazy — micro-optimization only.** Reported review bootstrap time is **334,381ms across nine logged events**, but duplicate log entries inflate that total; five distinct deep-dive bootstraps were ready and none were unused. Queries themselves took roughly **0.4–0.6s** in those logs. **Change:** log distinct bootstrap IDs and critical-path time; do not eagerly bootstrap or optimize query milliseconds ahead of the waits above. **Savings:** unproven. **Risk:** low.

## Cost Optimizations

1. **Correct duplicate accounting first.** `38247461041` reports **22** OpenRouter calls and **10,727,522** tokens where the same **11** usage lines appear twice; `38246098799` reports **24** calls and **35,939,522** tokens where the same **12** lines appear twice. **Root cause:** job-aggregate and per-step logs are both parsed as events. **Exact change:** select one canonical log representation per job, or deduplicate with a stable emitted event ID; log `usage_lines_seen` and `usage_lines_suppressed`, with a mixed-log regression test. **Savings:** **zero actual dollars**; at least **23,333,522 tokens** of false attribution removed. **Quality risk:** none if distinct calls remain countable.

2. **Bound low-value check-run context while retaining every failure identity.** `38246098799` generated **101,381 bytes** of check-run context and separately warned at **165,632/200,000 prompt tokens (82.82%)**. Contribution to that prompt is an **inference**, not measured attribution. **Exact change:** keep every failing check’s name, status, and head association, but deduplicate repeated descriptions and cap expanded summaries/tails to a measured byte budget; log `context_bytes_before`, `context_bytes_injected`, and omitted-detail count. A **20,000-byte** cap would remove up to **81,381 bytes in that run’s context file**, not a proven token saving. **Quality risk:** moderate unless full failure identities remain available to the editor.

3. **Measure model and retrieval return before changing selection.** Run `38247461041` configured `minimax/minimax-m3` review, `openai/gpt-6-sol` editing, reviewer reasoning `xhigh`, and editor reasoning `high`; reported usage is duplicated. Across the supplied aggregation, Semble logged **nine queries / 135,943 bytes / 77 sources / 1,668 static-duplicate bytes**, also subject to duplication. Deep-dive queries target `reviewer-context`; their roughly **15KB** responses do not establish net prompt savings. **Exact change:** record model/effort, unique input-cache-output usage, Semble bytes actually injected, and static-context bytes displaced per call. Trial lower effort only on a narrow, quality-checked review tier after correction. **Savings:** unquantifiable in dollars or tokens until deduplicated and priced. **Quality risk:** potentially high for blanket model or reasoning downgrades. Serena was disabled in sampled reviews (`38246098799`, `38249515875`); its zero queries show **no demonstrated tool/model replacement benefit**, not a failed rollout.

## Reliability Improvements

1. **Resolve the repeated size-guard breach (configuration/contract).** The six CI failures named in the summary share the same **19,821-character** `update_workflows.yml` body. Extract its shell body or pass expressions via step environment while preserving behavior and the 18,000-character test. Emit one concise failure signature containing workflow, job, step, measured length, and limit. **Expected impact:** eliminate the largest observed failure cluster. **Rollback:** restore the previous body only if it still passes the guard; do not relax the limit.

2. **Make closed-PR review a clean no-op (lifecycle).** In failed review `38232003857`, `PR_CLOSED=true` preceded the missing-workspace error; `.github/workflows/review_autofix.yml` already guards many downstream steps but not the checkout/workspace path consistently. Add an explicit closed-PR `REVIEW_SKIP reason=pr_closed` and skip workspace-dependent setup; keep open-PR workspace failure fatal. **Expected impact:** remove this observed false failure and its avoidable setup. **Rollback:** revert the skip condition without changing merge authorization.

3. **Separate transient dependency failures from contract failures.** CI `38250387138`, `static-checks / Install actionlint`, received **HTTP 504** from `curl`; retain version and SHA-256 verification, but add bounded retry only for transient download responses, logging attempt and final HTTP category. **Expected impact:** could recover that one observed CI failure if a subsequent attempt succeeds. The other isolated CI failures need their own fixes: `38245816108` found an unwired test; `38245692461` found a release-tag publishing-off contract mismatch; `38246083359` observed `plan_gate_helper_unavailable` and correctly deferred; `38246118726` enforced the `agents.md` direct-edit policy. Do not suppress these guards. **Rollback:** disable download retries independently; preserve verification and fail-closed gates.

4. **Close visibility gaps without guessing at causes.** Poller `38246066105` has a **0s** failure, no observed job conclusion, and a **404 missing log archive**; its cause is unknown. Emit collector `LOG_ARCHIVE_STATUS`, preserving existing jobs/annotation findings and distinguishing no archive from a workflow-validation failure. Successful reviews `38249614479` and `38253462436` nevertheless warned that no standalone validation could be dispatched for merged PRs `#7198` and `#7213`; emit `VALIDATION_DISPATCH` eligibility, reason, and outcome rather than treating workflow success as validation evidence. **Expected impact:** better triage and fewer masked handoff gaps; failure-rate reduction unmeasured. **Rollback:** diagnostic-only fields are removable without altering dispatch.

**Policy and MCP interpretation:** The supplied count is **zero `BREAK_GLASS`**. Its **two `CONTEXT_BUDGET_WARN` log lines** both belong to `38246098799` but represent **one duplicated deep-dive event**—prompt-size pressure, not evidence of a break-glass policy problem. Reported **100 `SEMBLE_FALLBACK` lines** are tagged `context=contract-test`, `target=overflow`, including `tests-promote-stall-and-review` in `38231787878`; **runtime fallbacks are zero**. Some contract lines are duplicated by aggregate/step logs. Neither they nor the zero Serena probes establish a broken runtime rollout.

## AI Memory Health

In **10 distinct deep-dive `review_autofix` runs**, all **10 `retrieve` operations** selected records: **100% sampled hit rate**, **1,390.2 average estimated tokens against a 1,400-token budget**; keyword method was **`llm` 10, `plain` 0, `none` 0**. No sampled retrieve selected zero records, had `enabled=false`, or reported `fail_open=true`. After removing repeated aggregate/step lines, there were **20 `record-run-event` operations**; two used **two push attempts**, all reported success. Five distinct `record-candidate` operations reported success. Run `38249614377` separately summarizes a successful `finalize-task` push, but is not part of the deep-dive retrieve sample.

**Action:** emit a deduplicated memory-event ID and per-operation `push_attempts`, selected-record count, budget utilization, and fail-open reason. Track hit and retry rates by role; the near-full reviewer budget merits a relevance audit, not an automatic reduction. **Expected impact:** detect degrading retrieval or push reliability without sacrificing the observed hits. No `promote`, `compact`, or processed-command event was found in the inspected deep-dive logs; verify emission where those operations actually execute rather than interpreting absence as failure.

## GH API Call Audit

- **Merge train:** `cancel_on_pr_close` run `38251322512` spent about **188s** examining **32 PRs** and released **zero**. `scripts/review_merge_train.sh` already caches `pulls/{n}/files` **per distinct PR per run**, prefetches some metadata with GraphQL, and performs safety-sensitive active-run listings. Under `CLAUDE.md §15`, do **not** add per-PR loop reads or remove the second active-run view merely to cut calls. Emit `MERGE_TRAIN_API_SUMMARY` with endpoint-group attempts, file-cache hits/misses, GraphQL batches, active-list passes, retries, and elapsed time, with no extra API requests. **Call reduction:** not measurable yet; each demonstrated duplicate file miss fixed within one cycle would save one paginated file lookup and its associated rate-limit exposure.
- **Check-run polling:** `38246098799` logged eight waits before its final timed-out snapshot; `38246151160` logged nine. `collect_pr_check_runs_context.py` fetches the check-run listing once per snapshot, so those logs imply **at least nine and ten listing invocations**, respectively; pagination or retries could increase HTTP requests. Emit listing page/request counts and snapshot-change counts. Retain backoff and the final safety snapshot. **Call reduction:** none safely established; use the counters to find redundant unchanged-snapshot requests before adjusting intervals.
- **Budget visibility:** Across inspected deep logs, **26 distinct job-end `GH_PAT_BUDGET` observations** had `used_in_job=unknown`; reset changes and shared quota make these unsuitable as call counts. Record wrapper-local attempts by endpoint group, cache result, retry reason, and 403/429 category instead of adding budget probes. **Expected rate-limit impact:** measurable attribution first; no observed production rate-limit incident is established here. The collector’s archive **404** is a missing-log signal, not evidence of quota exhaustion.

## Prompt Cache & Memory System

Reported review usage is **56,582,011 cache-read**, **3,706,174 cache-write**, and **10,378,109 prompt tokens** across the supplied aggregation, **but each total is contaminated by confirmed duplicated usage lines**. Aggregate `cache_hit_rate` is **null**: two of 96 reported calls have unavailable usage. Available per-run rates vary—**84.59%** in `38246098799`, **82.85%** in `38232028208`, and **56.17%** in `38249515875`—and are not a reliable portfolio average until deduplication.

The workflow already has a “Pre-assemble static context cacheable across runs” step. **Action:** retain that stable prefix, append PR/check-run and memory material afterward, and log prefix fingerprint plus static/dynamic bytes and provider cache reads per *unique* call. Unstable prefix placement is a **candidate fragmentation cause, not proven** by these logs. In `38246098799`, correlate its unique **165,632-token** warning with injected context sections before trimming; preserve all check-failure identities. **Estimated impact:** lower uncached input and warning frequency if dynamic expansion is confirmed, with token/latency savings to be measured; reliability improves by detecting budget pressure before truncation.

## Orchestrator Health

- **Deferrals are concentrated at the merge train.** Sweep `38253333868` skipped **32/38** candidates as merge-queued and four as active, while dispatching two. Cleanup `38251322512` left `#7259` queued behind nine blockers. Log per-PR blocker count, oldest-blocker age, first-queued time, bypass decision, and release reason; retain conservative conflict and active-run checks. Track queued-to-released age rather than treating skips as failures.
- **Phase and judge visibility is incomplete.** Clarify, plan, and implement show many skipped runs, not failed executions; `unblock_judge_dispatch` has seven successful runs but no log telemetry in the supplied aggregation. Emit skip/deferral reason and issue/wave state transitions from existing decisions, plus judge dispatch outcome and retry tier. This separates intentional no-ops from stalled waves without new API calls.
- **Preserve safe holds.** CI `38246083359`’s plan auto-approve test returned `outcome=defer reason=plan_gate_helper_unavailable`; a missing helper must not auto-approve. Log helper availability and staged-support identity, fix the contract/staging mismatch, and track consecutive holds. Report conflict-heal retry and terminal-state counters alongside wave progression; this window does not measure their rates.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck | Type | Safest next step |
|---|---|---|---|
| Clarify → plan → implement | Clarify **134/140**, plan **128/135**, implement **130/134** skipped; active-work latency cannot be inferred from their 1s medians. | Selection/visibility | Log the skip reason and issue/wave transition, not “failure.” |
| Review/autofix entry | Gate-to-agent gaps **66m38s** (`38232003857`) and **55m27s** (`38246151160`). | Queue/dependency, unclassified | Split the gap using existing job timestamps before changing dispatch. |
| Review/autofix compute | Two `Collect PR check-run failures` steps reached **300s** (`38246098799`, `38246151160`); `38246098799` also had a unique prompt-budget warning. | Bounded wait and context expansion | Log wait/API/context breakdown; retain merge safety. |
| Validate / CI | CI p50 **909s**, p95 **1,590.5s**; six failures shared one size-guard defect. | Compute and repeated contract failure | Fix the body, then profile the slow job groups. |
| Orchestrate / merge | Poller p50 **1,139s**; cleanup `38251322512` spent about **188s** releasing none. | Poll/merge-blocker overhead | Log queue age, cache misses, API attempts, and release outcomes. |

These stages are **not additive elapsed times**: jobs overlap, skipped runs dominate some family distributions, and queue time is not separated in the supplied metrics.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** gate-to-agent gaps of **55–67 minutes** in two sampled reviews; **300s** check-run waits in two others; CI median **909s**. **Top failure modes:** six repeated workflow-size-guard failures, one closed-PR workspace false failure, one actionlint download **504**, plus distinct policy/contract failures. **Highest-cost driver:** review/autofix accounts for all **96 reported OpenRouter calls**, but at least **23** of those call records and **23,333,522 tokens** are duplicated.

**Top three actions:**
1. Deduplicate job/step telemetry and emit suppression counts; re-baseline cost, cache, warnings, and MCP rates.
2. Fix the 19,821-character workflow body and closed-PR workspace guards without weakening their safety behavior.
3. Add job-gap, check-wait, merge-train API/cache, and validation-dispatch summaries; optimize only the measured dominant cause.

## Metrics Appendix

**Scope and coverage.** The supplied analysis covers **1,000 runs in one repository**; `insufficient_data=false`, but telemetry is selective. Its enriched context reports **122 runs with log telemetry**. The mounted raw `summary.json` has **27** such runs and `sampled_success_runs=0`; the additional context includes summaries of unselected runs. Dollar prices, attributable GH API totals, and a deduplicated portfolio cache rate are unavailable.

| Workflow family | Runs | Success | Failure | Other/cancelled | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All | 1,000 | 288 | 14 | 697 / 1 | 2s / 1,136s |
| CI | 51 | 39 | 12 (**23.5%**) | 0 / 0 | 909s / 1,590.5s |
| Review/autofix | 81 | 79 | 1 (**1.2%**) | 1 / 0 | 846s / 3,113s |
| Orchestrate poll | 11 | 10 | 1 (**9.1%**; missing archive) | 0 / 0 | 1,139s / 2,047s |
| Clarify / plan / implement | 140 / 135 / 134 | 6 / 7 / 4 | 0 / 0 / 0 | 134 / 128 / 130 skipped | 1s each / 11.1s, 186.8s, 11s |

Overall **14/1,000 = 1.4%** failed; among runs concluded success or failure, **288/302 = 95.4%** succeeded. Neither figure turns the **697 skipped** runs into attempted work.

| Reported usage/health metric | Supplied aggregation | Audit qualification |
|---|---:|---|
| OpenRouter calls; prompt / completion / total tokens | 96; 10,378,109 / 676,499 / **71,341,349** | At least **23 calls and 23,333,522 total tokens duplicated** in two deep-dive runs; actual complete spend unknown. |
| Cache read / write; aggregate `cache_hit_rate` | 56,582,011 / 3,706,174; **null** | Duplicated usage; two reported calls have unavailable usage. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | **8,000 / 4,441,400** | 121 mixed-family samples; not model-call latency. |
| `break_glass_count` / `context_budget_warn_count` | **0 / 2** | Two warning lines are **one distinct** deep-dive event in `38246098799`. |
| GH API calls / rate-limit events | **Not measured / none established** | **26** inspected job-end PAT budget observations all said `used_in_job=unknown`. |

| MCP target / context | Reported queries; logged bytes | Reported fallback / bootstrap | Probe OK / failed / skipped | Qualification |
|---|---:|---:|---:|---|
| Semble `target=reviewer-context`, review/autofix | **9; 135,943 bytes**, 77 sources, 1,668 static-duplicate bytes | **0 runtime fallback**; 9 ready bootstraps, 0 failed/unused, 334,381ms reported | **0 / 0 / 0** | At least **two queries / 31,068 bytes** and **77,574 bootstrap ms** duplicate aggregate/step events. Deep-dive distinct queries returned roughly 15KB each; prompt displacement unmeasured. |
| Semble `target=overflow`, CI contract tests | 0; 0 bytes | **100 reported contract-test fallbacks**, 0 runtime | **0 / 0 / 0** | Deliberate missing-binary test context; counts also include some duplicate logs. No query-based fallback rate is meaningful. |
| Serena, disabled in sampled reviews | **0; 0 response bytes; 0 tool calls** | 0 fallback | **0 / 0 / 0** | No per-tool breakdown or availability conclusion is possible without queries/probes. |

**Other MCP servers observed:** none with validated query, fallback, or probe events. The immediate collection addition is a stable event ID and log-origin field so these tables can distinguish **events** from repeated **log lines**.
