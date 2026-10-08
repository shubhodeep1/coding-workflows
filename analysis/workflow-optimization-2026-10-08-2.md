## Executive Summary

- **Review latency is the largest bottleneck.** In `shubhodeep1/coding-workflows`, review/autofix ran 303 times (p50 459s); run `37722509410` had a 27m15s gap between gate completion and agent start. **Impact:** potentially minutes per affected run if redundant dispatches can be avoided; **confidence: medium**—the gap is measured, but its cause is not logged.
- **CI failures are concentrated but not one incident.** Four of seven CI runs failed, including engine-selection assertions (`37734087435`) and a missing staged helper (`37727100809`). Fix the failing contracts rather than automatically rerunning CI. **Impact:** fewer failed validations and reruns; **confidence: high** for the failures, medium for the combined root cause.
- **Conflict isolation caused two review failures.** Runs `37722414925` and `37734366740` refused an unsafe host fallback for the same host-only conflicted path. Preserve that refusal; detect the condition before starting Semble and route it for manual resolution. **Impact:** roughly 32–37s less setup per such failure and fewer futile repeat attempts; **confidence: high**.
- **Repeated reviews dominate measured tokens, but redundancy is unproven.** Two PR #6719 runs (`37719734690`, `37722528306`) consumed 18,989,472 of 24,338,452 reported OpenRouter total tokens. The archive does not establish that their head, base, and review configuration were identical. **Impact:** up to 8,864,979 reported tokens *if* one run proves unnecessary; **confidence: low** for realizable savings.
- **Telemetry needs deduplication.** The collector reports seven Semble queries, but judge run `37734366740` contains the same query in both its combined job log and individual step log. Six distinct query events are visible. **Impact:** correct query, byte, source, and bootstrap rates before rollout decisions; **confidence: high**.

## Speed Optimizations

1. **Critical path—coalesce only proven-identical review dispatches.** Runs `37722509410` and `37719715490` show gate-to-agent gaps of 27m15s and 23m44s, respectively; these are **inferred between-job waits**, not measured runner queue states. Log gate-end, agent-queued, and agent-start timestamps and a review fingerprint covering PR head, base, rubric/configuration, and required-check state. Skip a dispatch only when a valid completed result matches. **Estimated saving:** up to the avoided wait and run time for a confirmed duplicate; none is established in this window. **Risk: medium**—never suppress review on a changed base or required check.
2. **Critical path—honor a PR closure detected after the gate.** Run `37725087089` lasted 2,031s; its agent logged that PR #6719 had merged, then failed at “Checkout PR head branch” with an unavailable workspace. Recheck PR state immediately before checkout and agent launch; emit `stop_reason=merged_since_gate` and finish as a safe no-op only when closure is verified. **Estimated saving:** approximately the remaining three-plus minutes after the observed closure warning; potentially more when caught earlier. **Risk: low** if open-PR checkout failures remain fatal.
3. **Failed-path latency—preflight resolver isolation.** Run `37722414925` encountered 28 unmerged paths; it and judge run `37734366740` then hit a host-only conflicted path. Classify unresolvable paths before Semble bootstrap or model work. **Estimated saving:** the observed 36.5s and 32.2s bootstrap times on these two paths; **risk: low** if the existing refusal remains unchanged.
4. **Measure before tuning compute and scans.** The `review / codex-agent` job occupied about 494s of 517s in `37733373063`; poller p50 was 596s across ten runs. Emit stage-level active, wait, API, and model milliseconds before changing reasoning effort or poll intervals. Separately, `cancel_on_pr_close` run `37734354820` spent about 48s of 58s in merge-train release, examining 38 PRs and releasing none. **Estimated saving:** unquantified for model/poller work; at most 48s in that observed no-op scan. **Risk: low** for logging, higher for behavioral tuning.

## Cost Optimizations

1. **First verify review duplication.** PR #6719 runs `37719734690` and `37722528306` each made 14 reported OpenRouter calls and together account for **78.0%** of reported OpenRouter total tokens. Log the review fingerprint and prior-result decision; reuse a result only on an exact safe match. **Estimated saving:** at most 8,864,979 reported tokens if the smaller run is proved redundant; **quality risk:** high without base/configuration and check-state matching. Token totals are not dollar charges.
2. **Trim measured prompt pressure without dropping required evidence.** Four `CONTEXT_BUDGET_WARN` events occurred in those two PR #6719 runs: review prompts of 185,222–190,402 tokens against a logged 183,500-token warning threshold. Inspect assembled prompt component sizes and remove repeated, nonessential dynamic material *after* preserving review evidence and the stable cacheable prefix. **Estimated saving:** at least 17,562 prompt tokens across those four prompts would bring their logged sizes to the threshold; latency and dollar savings are unknown. **Quality risk: medium**.
3. **Evaluate Semble on net context, not query count.** The collector reports 71,335 query bytes and 53 sources; removing the duplicated judge event leaves **62,287 bytes, 46 sources, and six queries** in the inspected logs. Three `reviewer-context` queries returned 38,285 bytes. `static_dup_bytes` is reported as zero where present, but some query lines omit it; neither figure proves Semble reduced prompt expansion. Log selected bytes versus the static context they replace and downstream prompt bytes. Keep fail-open behavior. **Estimated token saving: unmeasured; quality risk: low** for measurement.
4. **Defer model downgrades and Serena claims.** Review logs configure `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: high`, and `MODEL_EDITOR: openai/gpt-6-sol`; actual per-role model, effort, price, and outcome comparisons are absent. Record those per call before a narrowly scoped quality canary. Serena has **zero queries, response bytes, tool calls, fallbacks, and probes** in the reported telemetry; sampled review logs show it disabled. There is no evidence yet that it replaces downstream work. **Estimated savings: unavailable; quality risk: high** for an unmeasured downgrade.

## Reliability Improvements

1. **Repair CI contract drift; do not mask it with retries.** CI failed in `37720983247`, `37724526807`, `37727100809`, and `37734087435` (4/7 runs). Evidence includes workflow-heal assertions, a poller test killed after 180s, `operator_step_issue.py` invoked but not staged, and utility-role model assertions. Log the pinned support identity, staged-helper manifest check, resolved engine configuration, and failing test name at the first failure; fix each contract at its source. **Expected impact:** reduce the observed CI failure/rerun burden; **rollback:** retain the previous tested configuration, not an unconditional test retry. Integration-fingerprint errors printed by passing fixtures must not be counted as production merge failures.
2. **Make host-only conflicts an explicit terminal/manual route.** The resolver’s `sandbox_path_host_only` refusal in review `37722414925` and judge `37734366740` is a safety control, not evidence that a host fallback should be enabled. Emit conflict class, path count, and whether the conflict set changed; avoid redispatching an unchanged unresolved set. **Expected impact:** prevent repeat futile resolver failures; **fail-open consideration:** never fail open to host execution—retain manual merge.
3. **Close the merged-PR race.** Run `37725087089` recognized merged PR #6719 but subsequently failed checkout. A fresh pre-checkout state check and explicit benign terminal outcome should prevent this specific failure while leaving unverified/open PR errors fatal. **Expected impact:** potentially one of the three observed review failures in an equivalent race; **rollback:** restore the existing fatal behavior if closure cannot be verified.
4. **Separate MCP test signals from runtime availability.** Of 27 reported `SEMBLE_FALLBACK` events, **24** are `target=overflow` contract-test fixtures in CI; the **three runtime** events are `binary-unavailable` overflow fallbacks in implement run `37722515048`. These are localized fail-open events, not 27 production outages. Emit target-scoped ready/probe state and reason before first overflow use; keep the static fallback. No Serena availability conclusion is possible from zero probes. **Expected impact:** diagnose the three runtime misses without suppressing work; **risk: low**.
5. **Track nonfatal cleanup warnings separately.** Successful review runs including `37733373063` and `37733375444` reported post-job `git-submodule` cleanup failure without a working tree. Log workspace removal and post-checkout cleanup outcomes in order; preserve the working tree until action cleanup where feasible. **Expected impact:** fewer misleading warnings and clearer detection of a genuine future checkout failure; **risk: low**.

No `BREAK_GLASS` event was reported. The four context-budget warnings indicate **prompt-size risk**, not evidence of policy/rubric pressure.

## AI Memory Health

Ten distinct `AI_MEMORY_TELEMETRY` retrieves in the inspected deep dives selected records (**10/10 hit rate**); none returned zero records or reported `enabled: false`. Average `estimated_tokens` was **1,398.2** against an average **1,420-token** budget (98.5%); `keyword_method` was `llm` nine times and `plain` once, with no `none`. Review run `37722528306` retrieved 25 records in 1,369/1,400 tokens. Keep retrieval enabled, but log selected-record deduplication and marginal usefulness before increasing its budget; memory’s approximately 1.4k tokens do **not** explain a roughly 190k-token review prompt by themselves.

One `fail_open: true` event was a **`write_lessons_learned` failure**, not a retrieve failure, in judge-related run `37725105416`. Seven observed memory operations needed two push attempts; none exceeded two. Add a sanitized push-retry reason and final persistence outcome while preserving fail-open review execution. `promote` and `compact` were not observed, so longer-term memory lifecycle health is unmeasured.

## GH API Call Audit

**Endpoint call counts, per-item redundancy, API retries, and rate-limit-event totals are not collected here.** Review `GH_PAT_BUDGET` snapshots—for example, `37734366740`—report `used_in_job=unknown`; changing reset windows prevent a reliable subtraction. Do not turn “38 PRs examined” in `37734354820` into “38 API calls.”

The repository’s `CLAUDE.md` §15 requires reuse, batching, cycle-local caches, and safe cache-miss fallbacks. `scripts/review_merge_train.sh` already paginates open PRs, reads the active-run list once per release, and caches file lists across blocker checks. Preserve those safeguards. Instrument the existing `gh_retry` boundary with **sanitized endpoint template, method, call count, retry count, elapsed milliseconds, and rate-limit outcome**; add merge-train release totals for file-cache hits/misses and distinct file fetches. Record no request bodies or credentials. Only if measured lookups remain per-item should an existing batch/prefetch be extended: for **N measured calls** and batch size **B**, the theoretical reduction is `N − ceil(N/B)`; this window cannot supply N. Keep fresh authorization/merge-state reads fresh rather than reusing a stale cache.

## Prompt Cache & Memory System

Reported OpenRouter usage contains **17,201,809 cache-read** and **1,423,891 cache-write tokens**, but nine of 42 calls lack usable usage data, so aggregate `cache_hit_rate` is **null**, not zero. The fully covered PR #6719 run `37722528306` reports **62.4915%**. Record cache creation/read/miss, provider-usage availability, and a non-content prefix fingerprint per call; do not compute an aggregate hit rate by treating unavailable calls as misses.

A cache-fragmentation cause is **not established**. Test whether run-specific status, timestamps, or expanding diff material precede the already preassembled static context; place stable instructions first and changing evidence later only when prompt semantics remain intact. Measure prefix reuse and component bytes before/after. The four review `CONTEXT_BUDGET_WARN` events show that prompt growth can erode headroom even with cache reads. The near-full memory retrieval budgets warrant monitoring, not indiscriminate removal of useful records. **Expected token/latency impact:** unquantified until component and prefix measurements exist; **reliability benefit:** earlier detection of prompt-size pressure.

## Orchestrator Health

Clarify, plan, implement, and clarify-respond together produced **593 skipped runs out of 603**; these may be appropriate gates, but most skipped runs have no step logs or structured reason. Emit `phase_gate` with issue, prior state, decision, and bounded reason at dispatch/gate time, including when a job never starts. Plan run `37734406788` reported an `existing_pr` skip while also noting a planning-stall retrigger; whether this is wasted recovery is **an inference**, not proved. Log the recovery’s prior state, age, deduplication key, and outcome.

The poller completed all ten observed runs but had **596s p50**; its wave progression, deferrals, sleep versus compute, and terminal-state counts are missing. Emit one bounded `ORCH_TICK` summary per tick: issues considered, wave transitions, deferrals by reason, conflict-heal attempts/budget, and terminal outcomes. Integration-readiness run `37734087430` found **2/7 sub-issues unchecked** on #6664—evidence of pending work, not proof of a stuck project. Duplicate-issue triage in `37735019466` skipped opening another issue for #6710, a useful idempotency signal to retain.

## Pipeline Flow Bottlenecks

| Flow segment | Observed bottleneck | Next diagnostic or safe action |
|---|---|---|
| Clarify → plan → implement | Mostly gated/skipped: 593/603 combined; active implement outlier `37722515048` took 1,818s. | Log gate reasons and active-phase substep/model time; do not infer low work cost from 1s skipped medians. |
| Review/autofix | 303 runs, p50 **459s**, p95 **786.9s**; gate-to-agent gap reached **27m15s** in `37722509410`. | Measure both job queue intervals and active agent stages; suppress only fingerprint-proven duplicate dispatches. |
| Validate/CI | CI p50 **654s**, four failures in seven runs. | Emit first failing test and staged-support identity; repair contracts before rerun. |
| Orchestrate/retry | Poller p50 **596s**; `37720983247` includes a test poller killed at 180s. | Separate live poller wait/API/model time from CI fixture subprocess time. |
| Merge/conflict | `37722414925` found 28 unmerged paths, then hit host-only isolation; `37734354820` spent about 48s in a release scan with zero releases. | Preflight resolver eligibility; instrument blocker-cache hits and no-op scan time. |

These are **run and sampled-step observations**, not an end-to-end issue-to-merge percentile; that linkage and merge-wait telemetry are absent.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review/autofix execution and between-job waits, then poller and CI time. **Top failure modes:** four distinct CI regressions, two host-only resolver refusals, and one merged-PR checkout race. **Highest measured cost:** PR #6719’s two token-heavy reviews; equivalence is unverified.

1. **Instrument decisions and time first:** per-job queued/start/end times, review fingerprints, phase-gate reasons, and first failing test.
2. **Fix safe terminal paths:** verified merged-PR no-op and preflight/manual routing for host-only conflicts; never permit the unsafe fallback.
3. **Correct measurement and then optimize:** deduplicate combined/per-step MCP events, add API request counters, and assess exact-match review reuse and prompt components.

## Metrics Appendix

**Window and coverage.** `summary.json` covers **October 8, 2026, 02:48:30–05:57:37 UTC**, one repository—not a weekly trend. Supplied enriched context reports telemetry on **116 runs** and 114 positive wall-clock samples; the archive’s top-level collector summary reports **18 directly parsed telemetry runs**. Success log sampling is incomplete. Dollar costs, per-endpoint API calls, issue-to-merge times, and actual per-call model selection are unavailable.

| Family | Runs | Success | Failure | Skipped | p50 / p95 duration |
|---|---:|---:|---:|---:|---:|
| All | 1,000 | 364 (36.4%) | 7 (0.7%) | 629 (62.9%) | 6s / 598s |
| Review/autofix | 303 | 300 | 3 (0.99%) | 0 | 459s / 786.9s |
| CI | 7 | 3 | 4 (57.1%) | 0 | 654s / 1,360.7s |
| Orchestrate poll | 10 | 10 | 0 | 0 | 596s / 882.9s |
| Implement | 150 | 2 | 0 | 148 | 1s / 10s |

Of the **371 non-skipped terminal runs**, 364 succeeded (98.1%); that conditional rate must not be substituted for the all-run rate.

| Token, cache, and review telemetry | Supplied aggregate | Coverage/interpretation |
|---|---:|---|
| OpenRouter total / prompt / completion tokens | 24,338,452 / 5,416,844 / 296,257 | 42 calls; 33 usage-available, 9 unavailable |
| OpenRouter cache read / write tokens | 17,201,809 / 1,423,891 | Aggregate hit rate **unavailable** |
| Codex tokens / calls | 1,324,165 / 12 | Separate accounting; do not add to OpenRouter total as dollars |
| `cache_hit_rate` | null aggregate; **62.4915%** in `37722528306` | Only that cited run has complete applicable usage |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 2,450,570 | 114 enriched samples; mixes short skipped and active runs |
| `break_glass_count` / `context_budget_warn_count` | 0 / **4** | Warnings: two each in `37719734690` and `37722528306` |

| GH API evidence | Observed | Missing diagnostic |
|---|---|---|
| Review gate/agent budget snapshots | Start/end lines; `used_in_job=unknown` in sampled run `37734366740` | Calls by endpoint, retries, cache hits, comparable-window budget use |
| Merge-train release, `37734354820` | 38 PRs examined, cap 20 noted, 0 released, approximately 48s in release step | Distinct API calls versus cached/local examinations |
| Rate limits | No count supplied | Per-request 403/429, retry reason and elapsed time |

| MCP target and workflow | Queries / logged bytes / sources | Fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context`, review/autofix | 3 / 38,285 / 28 distinct log events | 0 | 0 / 0 / 0* |
| Semble `conflict-resolver-context`, review and judge | 2 / 18,096 / 14 distinct log events | 0 | 0 / 0 / 0* |
| Semble `review-blocked-judge-context`, review/autofix | 1 / 5,906 / 4 distinct log events | 0 | 0 / 0 / 0* |
| Semble `overflow`, CI contract tests | 0 / 0 / 0 | **24 test fixtures** | 0 / 0 / 0* |
| Semble `overflow`, implement `37722515048` | 0 / 0 / 0 | **3 runtime**, `binary-unavailable` | 0 / 0 / 0* |
| Serena, no target observed | 0 / 0 / 0; **0 response bytes and 0 tool calls** | 0 | 0 / 0 / 0* |

\*No target-scoped probe event was observed; zeros **do not establish availability**. Serena per-tool breakdown is unavailable because no tool calls were recorded. **Other MCP servers observed:** none.

The supplied collector totals are **7 Semble queries / 71,335 bytes / 53 sources**, **7 bootstraps / 243,482ms**, zero failed or unused bootstraps, and **27 fallbacks** (24 contract-test, three runtime). Combined-job and individual-step lines in `37734366740` duplicate one 9,048-byte, seven-source query and one 32,242ms bootstrap. Counting distinct displayed events yields **6 queries / 62,287 bytes / 46 sources** and **6 bootstraps / 211,240ms**; preserve both raw and deduplicated counts until the collector is corrected.
