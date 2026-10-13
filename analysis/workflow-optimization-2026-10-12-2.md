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

## Deep Audit — Workflows & Scripts (2026-10-12)

### Section 1: Bug & Correctness Sweep

The read-only sweep covered 55 workflow files and 216 repository scripts. It found no Bash syntax error in `scripts/*.sh`; workflow shell bodies were checked separately. The existing report already covers the closed-PR checkout failure and actionlint download failure, so those findings are not repeated here.

- **BUG-001** — `scripts/propagate_consumer_secrets.sh:5-14` (**High**, `bug`). **Description:** The script says a workflow invokes it on registry changes, dispatch, and a schedule, but that workflow is absent from `.github/workflows/`. The wired CI test reads the absent path in `tests/test_propagate_consumer_secrets.py:309-332` (`.github/workflows/ci.yml:1628-1631`). Thus the documented unattended propagation has no trigger in this checkout. **Recommended fix:** Restore the source-repository workflow with the documented push, dispatch, and weekly triggers; wire this script and its secret environment through the contract already asserted by the test. Do not remove the test or treat a manual script invocation as a replacement.

- **BUG-002** — `scripts/orchestrate_poll_process.sh:25575-25661` (**High**, `bug`). **Description:** Implementation-failure reissue reads the source issue’s title and body in two calls, converts either API failure to an empty string, then closes the source before attempting to create its replacement. A failed read or replacement creation can therefore leave a closed source with no valid successor. **Recommended fix:** Fetch and validate one complete source-issue snapshot before mutation; create and verify an idempotently identifiable successor, persist its mapping, and only then close the source. On failure, leave the source open for the next tick.

- **BUG-003** — `.github/workflows/validation-improvements-intake.yml:413-424` (**High**, `bug`). **Description:** The ledger append runs under `set -euo pipefail`, but its first `printf '- Consumer run: %s\n'` treats the leading hyphen as an option and exits nonzero in Bash. Four subsequent ledger lines, and three PR-body lines at `.github/workflows/validation-improvements-intake.yml:471-474`, use the same form. A read-only Bash invocation reproduced exit code 2. **Recommended fix:** Use `printf --` for every hyphen-leading format in both blocks, and add a test that executes the append and PR-body assembly paths.

- **BUG-004** — `scripts/workflow_retro_fanout.sh:309-329` (**Medium**, `bug`). **Description:** Once an existing weekly comment ID is known, any failed PATCH falls through to a POST with the same marker. If the PATCH took effect but its response was lost, that fallback creates a duplicate comment—an inference requiring a failure-injection test. **[NEEDS VERIFICATION]** **Recommended fix:** Do not POST merely because PATCH failed. Retry an idempotent PATCH for classified transient errors, or defer to the next run; POST only after a fresh read confirms the original comment is absent.

- **SEC-001** — `.github/workflows/workflow-log-analysis.yml:1237-1245` (**Medium**, `security`). **Description:** `github.ref_name` is interpolated directly into a quoted Bash assignment in the report-push step; it is also interpolated into the notification URL at `.github/workflows/workflow-log-analysis.yml:1413-1417`. Inference: a dispatchable ref containing shell metacharacters could alter the generated script. Whether GitHub ref restrictions and dispatch permissions make that reachable is unverified. **[NEEDS VERIFICATION]** **Recommended fix:** Read the runner-provided `GITHUB_REF_NAME` as data inside Bash instead of inserting the expression into `run:`, retaining the existing branch-type check.

### Section 2: GitHub API Call Redundancy Audit

Counts below are invocation counts on the stated path, before pagination or retry attempts. The existing report’s merge-train and check-run-polling audit is not repeated.

- **API-001** — `scripts/orchestrate_poll_process.sh:12252-12265` (**Medium**, `api-redundancy`). **Description:** When `final_pr_json_snapshot` does not match the recorded final PR, two consecutive `_safe_gh_jq` calls fetch the same `pulls/{final_pr}` resource for `.state` and `.merged_at`. **Current → proposed:** 2 GETs → 1 GET per cache miss. **Recommended fix:** Fetch one object, check its PR number, then derive both fields locally, as `close_linked_pr` already does for several fields at `scripts/orchestrate_poll_process.sh:14875-14887`. Keep the subsequent integration-branch freshness check.

- **BATCH-001** — `scripts/promote_main_cycle.sh:243-265` (**Medium**, `api-batching`). **Description:** `last_cycle_baseline_sha` searches for at most ten candidate issues, then fetches each candidate’s first 100 comments individually. **Current → proposed:** 1 search + *N* comment GETs → 1 search + `ceil(N/25)` aliased GraphQL calls; for ten candidates, up to 11 → 2 calls on the successful path. **Recommended fix:** Extend the alias-and-keyed-result pattern in `scripts/orchestrate_poll_process.sh:17094-17165` to preserve candidate order, the first-100-comment window, and the existing trusted-author test. Fall back to each candidate’s REST read when its batch is incomplete; verify comment ordering and parity in tests. **[NEEDS VERIFICATION]**

- **API-002** — `scripts/orchestrate_poll_process.sh:23015-23039` (**Medium**, `api-redundancy`). **Description:** A wave fetches candidate details and then independently fetches labels for the same issue set, although the first helper already returns labels and `labels_complete` (`scripts/orchestrate_poll_process.sh:17173-17182`). Both helpers batch 25 issues. **Current → proposed:** normally `2 × ceil(N/25)` GraphQL calls → `ceil(N/25)` when all label pages are complete, plus existing miss fallbacks. **Recommended fix:** Reuse only complete labels from the candidate snapshot; retain the dedicated label batch for missing/incomplete entries and test whether its later freshness is required by label reconciliation. **[NEEDS VERIFICATION]**

- **API-003** — `scripts/workflow_retro_fanout.sh:222-227` and `scripts/workflow_retro_fanout.sh:276-293` (**Low**, `api-redundancy`). **Description:** The existing tracker is selected from an issue listing filtered by `ai:retro`, then receives an unconditional `gh issue edit --add-label ai:retro`. **Current → proposed:** 1 listing + 1 label edit → 1 listing + 0 edits per existing tracker. **Recommended fix:** Reuse the listing’s label evidence; first test whether the edit intentionally repairs a label removed between listing and mutation. No GraphQL batch is needed for this single-tracker path. **[NEEDS VERIFICATION]**

BUG-002 also contains a same-endpoint opportunity: its two source-issue reads can become one validated read. Its safety ordering, not call reduction, is the primary fix.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — `scripts/render_prompt.sh:12-41` and `scripts/assemble_prompt.sh:12-41` (**Medium**, `duplication`). **Description:** Both entry points define the same `resolve_prompt_file` logic; they also duplicate `resolve_assembly_source_path` at `scripts/render_prompt.sh:138-159` and `scripts/assemble_prompt.sh:72-93`. **Recommended fix:** Put the path-resolution logic in a *new, proposed* `scripts/prompt_path_helpers.sh`, exposing `prompt_resolve_file <path> <script-dir>` and `prompt_resolve_assembly_source <resolved-path>`. Update both callers and their trusted-support staging lists together; retain resolution-order tests.

- **DUP-002** — `scripts/review_run_reviewers.sh:2052-2061`, `scripts/review_apply_fixes.sh:1022-1031`, and `scripts/review_conflict_prepare.sh:1265-1274` (**Low**, `duplication`). **Description:** Three identical `append_semble_query_section` functions print a labeled, byte-capped file excerpt. **Recommended fix:** Add `semble_append_query_section <label> <path> [max-bytes]` to existing `scripts/semble_helpers.sh`; source that helper from all three callers after confirming it is in each trusted bundle. Preserve the empty-file no-op and byte cap.

- **DUP-003** — `.github/workflows/workflow-log-analysis.yml:915-958`, `.github/workflows/workflow-log-analysis.yml:1623-1655`, and `.github/workflows/workflow-log-analysis.yml:2129-2163` (**Low**, `duplication`). **Description:** The analysis, deep-audit, and API-redundancy steps repeat tracking-issue validation, event-helper setup, and similar phase-failure handling. **Recommended fix:** Extract the common portion to a *new, proposed* `scripts/workflow_log_analysis_failure_helpers.sh` with `log_analysis_fail <phase> <step> <mode> <attempt> <summary>`; keep each step’s distinct status and output handling in YAML, passing Actions expressions through `env:`.

- **DUP-004** — `scripts/review_run_reviewers.sh:406-420`, `scripts/review_conflict_resolve.sh:286-300`, and `scripts/review_rb_judge.sh:329-343` (**Low**, `duplication`). **Description:** These scripts redeclare `read_codex_stall_guard_state` despite its existing implementation in `scripts/watchdog_helpers.sh:186-201`. **Recommended fix:** Source the trusted `watchdog_helpers.sh` copy in each role and call its existing `read_codex_stall_guard_state <status-file>` interface. Remove local copies only after testing missing-helper behavior for each caller.

No whole-workflow consolidation is recommended from the inspected pairs: short internal wrappers and their reusable workflows serve different trigger contracts.

### Section 4: Expression Size Limit Risk Assessment

Measurements are estimated **static, YAML-dedented `run:` body characters**, including trailing newlines—not values after Actions interpolation. The sweep found 229 interpolated bodies and excluded 674 bodies without `${{ }}`. No multiline `if:` approached 21,000 characters; the largest measured about 1,208. No workflow exceeded 800 KB: the largest, `.github/workflows/test-and-mark-stable.yml`, was 415,425 bytes, below this repository’s stricter 480,000-byte CI guard.

- **EXPR-001** — `.github/workflows/implement.yml:1092-1496` (**High**, `expression-limit`). **Description:** “Stage workflow support files” measures approximately **20,140 characters**, leaving **860** below 21,000; it contains three interpolations. Runtime-expanded length remains unmeasured. **[NEEDS VERIFICATION]** **Recommended fix:** Extract the body to a trusted support script resolved from the verified support checkout already used by this stage; pass `github.repository` and the two variable values through step `env:`. Preserve the staging ledger and missing-script failure.

- **EXPR-002** — `.github/workflows/implement.yml:3712-4104` (**High**, `expression-limit`). **Description:** “Preflight destructive-commit guard” measures approximately **19,826 characters**, leaving **1,174**; one interpolation makes the entire body exposed to the limit. It also contains inline heredocs. Runtime-expanded length remains unmeasured. **[NEEDS VERIFICATION]** **Recommended fix:** Extract the guard to a trusted script under `scripts/`, staged in `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`; use an environment value for the interpolated repository identifier. Retain its temporary-index cleanup and fail-closed guard decisions.

- **EXPR-003** — `.github/workflows/update_workflows.yml:837-1257` (**High**, `expression-limit`). **Description:** “Commit and push updates” measures approximately **19,822 characters**, leaving **1,178**, with nine interpolations. This cross-references the same step’s size failure in the current report’s *Reliability Improvements 1*; the small difference from that report’s 19,821-character measurement is consistent with counting a trailing newline. Runtime-expanded length remains unmeasured. **[NEEDS VERIFICATION]** **Recommended fix:** Move expression values to `env:` and extract the body to a script from the fetched release support tree, preserving the current commit, lease, PR outputs, and manifest-verification boundary. Alternatively split commit and PR delivery into smaller steps without changing the `commit_push` output contract.

- **EXPR-004** — `.github/workflows/plan.yml:2107-2414` (**Medium**, `expression-limit`). **Description:** “Auto-approve clear plan” measures approximately **15,655 characters**, leaving **5,345**, with five interpolations and an inline heredoc. Runtime-expanded length remains unmeasured. **[NEEDS VERIFICATION]** **Recommended fix:** Move the body to a trusted, staged plan script under `scripts/`, pass repository context via `env:`, and retain the live issue/provenance check and automation-path hold before posting `/approved`.

### Section 5: Cross-Cutting Concerns

- **CONSIST-001** — `scripts/security_dependency.py:94-110` (**Low**, `consistency`). **Description:** A prerequisite issue is read with one raw `gh api` subprocess call, unlike classified GET retries in `scripts/gh_api_retry.py:403-439`. A transient read failure becomes a held dependency; this is safe but can delay progress until another run. **Recommended fix:** Use the existing Python retry classifier for this GET, retaining the present `None` → held verdict after retries. Common-path calls remain **1 → 1**; only transient failures add bounded attempts.

- **SHELL-001** — `scripts/orchestrate_poll_process.sh:25667-25674` (**Low**, `shellcheck`). **Description:** `for rnum in ${REISSUED_NUMS}` performs both word splitting and pathname expansion before a per-issue API lookup, without validating each resulting number. An invalid state value could expand to workspace filenames—an inference about reachable state contents. **[NEEDS VERIFICATION]** **Recommended fix:** Iterate line-by-line over the `jq` output, require a positive decimal issue number, then use the quoted value in the existing lookup. Apply the same review to the parallel loop at `scripts/orchestrate_poll_process.sh:25281-25287`.

- **DEBT-001** — `.github/workflows/ci.yml:292-314` and `.github/workflows/ci.yml:403-425` (**Low**, `tech-debt`). **Description:** Actionlint disables inline-shell analysis, while standalone ShellCheck runs only at `--severity=error` over script files. Warning-level splitting problems such as SHELL-001 are therefore not an enforced CI contract, and inline-shell mistakes such as BUG-003 lack that coverage. **Recommended fix:** Add a focused warning-level check for changed scripts and extracted inline shell blocks, baselining existing warnings rather than weakening the current error gate; add BUG-003’s execution regression separately.

BUG-001 is the confirmed unwired-script case; it is not duplicated as a dead-code finding. The inspected workflow and script comments contained no actionable `TODO`, `FIXME`, or `HACK` marker. The deprecated `codex_mode` input at `.github/workflows/workflow-log-analysis.yml:16-20` is explicitly retained as a compatibility no-op, not safely removable dead code.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 6 | BUG-001, BUG-002, BUG-003, EXPR-001, EXPR-002, EXPR-003 |
| Medium | 7 | BUG-004, SEC-001, API-001, API-002, BATCH-001, DUP-001, EXPR-004 |
| Low | 7 | API-003, DUP-002, DUP-003, DUP-004, CONSIST-001, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | Missing workflow, poller, validation-intake workflow, and tests | Medium |
| API call optimization | 3 scripts and focused tests | Medium |
| Code modularization | 8 callers, proposed shared helpers, staging lists, and tests | Large |
| Expression size reduction | 3 workflows, trusted step scripts, staging lists, and tests | Large |
| Medium/Low fixes | Retro fan-out, log-analysis and CI workflows, dependency helper, poller, and tests | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-12)

### Safety Tag Legend

`SAFE_TO_MERGE` is authorized for direct implementation; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

No findings.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — RISKY_SKIP.** In `scripts/review_merge_train.sh:800-806`, `_mt_find_marker_comment` retrieves paginated PR comments but returns only the selected marker’s ID and creation time. On the existing-marker path, `_mt_gate` passes that ID to `_mt_upsert_comment` (`scripts/review_merge_train.sh:957-999`), which fetches the same comment’s body separately (`scripts/review_merge_train.sh:870-885`).
  - **Current → proposed:** 2 reads → 1 read per existing marker; any PATCH remains unchanged.
  - **Endpoints:** `GET /repos/{repo}/issues/{pr}/comments?per_page=100` and `GET /repos/{repo}/issues/comments/{id}`.
  - **Evidence:** The paginated listing already contains each comment’s body, but its projection discards it before `_mt_upsert_comment` compares bodies.
  - **Proposed fix:** *Manual-review option only:* extend `_mt_find_marker_comment` to return a safely encoded body with the ID and timestamp; pass it from `_mt_gate` to `_mt_upsert_comment`. Retain the individual GET whenever listing completeness, marker identity, or body freshness is uncertain.
  - **Safety rationale:** `RISKY_SKIP` is required because the source listing uses `--paginate`, and replacing the later read could miss a concurrent marker edit.
  - **Downstream signal:** Do not auto-implement. Manually test pagination failures and concurrent comment edits before deciding whether the later freshness read can be omitted.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: NEEDS_VERIFICATION — Confirm that one failed GET preserves the two existing calls’ independent retry and fallback outcomes.
- BATCH-001: NEEDS_VERIFICATION — GraphQL comment ordering, the first-100 window, and incomplete-batch REST fallback need parity tests.
- API-002: NEEDS_VERIFICATION — Confirm label completeness and whether reconciliation requires the later snapshot.
- API-003: NEEDS_VERIFICATION — Confirm that removing the label edit cannot lose an intentional repair after the listing.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 1 | REUSE-001 |

Counts cover net-new findings; cross-references are not counted.

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
