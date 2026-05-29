## Executive Summary

- `review_autofix` is the clear bottleneck: 69 of 411 runs, but 71,236s of 94,262s total runtime (75.6%). The worst outliers were dominated by the `gate` → `codex-agent` runner handoff: run `26624807639` lost 4,163.1s before `codex-agent` started, and run `26622285035` lost 1,096.7s. **Estimated impact:** high, 18–69 minutes saved on affected runs. **Confidence:** high.
- Cancelled `review_autofix` runs are the biggest avoidable cost sink: 9 cancellations burned 12,462s total, including runs `26626033377` (4,699s) and `26622992792` (2,674s). **Estimated impact:** high, ~3.46 hours of runtime recovered in a similar window. **Confidence:** high.
- `review_autofix` also pays a fixed setup tax even when caches are warm: sampled slow runs spent 27.7–172.9s in `jlumbroso/free-disk-space`, averaging 80.8s. **Estimated impact:** medium, ~1–3 minutes per affected run. **Confidence:** high.
- The only hard failure was CI run `26629481886`, job `lint`, step `Review autofix review-pipeline plumbing contract test`, caused by brace-expansion rename handling in the reviewer diffstat filter harness. **Estimated impact:** medium globally, high for CI stability (1 of 7 CI runs failed). **Confidence:** high.
- Semble looks useful and cheap in this window: 12 runtime `SEMBLE_QUERY` events, 141,607 logged bytes total, ~488.8ms average latency, mostly targeted `reviewer-context`/`overflow` fetches. Serena was effectively absent: 0 queries, 0 fallbacks, 0 probes. **Estimated impact:** medium cost/control-plane benefit by keeping Semble; no current Serena upside. **Confidence:** medium.
- AI memory write-side health is good, but reviewer retrieval is mostly ineffective: 8 of 9 retrieves returned 0 records; all 8 reviewer retrieves were zero-hit, while the only implementation retrieve hit 1 record with 28 estimated tokens in run `26630085547`. **Estimated impact:** medium cost/context-quality improvement if tuned. **Confidence:** high.
- Orchestrator control flow is stable but noisy: 261 skipped runs across `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` consumed only 518s total, so this is mainly an observability problem. The bigger orchestration latency is the `*/5` poll cadence plus 117.6s average poll runtime. **Estimated impact:** medium end-to-end latency reduction if tightened. **Confidence:** medium.

## Speed Optimizations

1. **Inline `review_autofix` gate into the heavy job (critical-path win)**
   - **Evidence:** In `review_autofix`, run `26624807639` spent 4,163.1s between `review.codex-agent.if` evaluating true (`08:41:24.471Z`) and runner pickup (`08:50:47.549Z`); run `26622285035` spent 1,096.7s on the same gap. Both were successful runs, so this is pure queue overhead.
   - **Root cause:** `.github/workflows/review_autofix.yml` splits a tiny `gate` job from the long `codex-agent` job, forcing a second hosted-runner acquisition.
   - **Exact change:** Move `Evaluate review gate` into the start of `codex-agent` for PR-backed runs, and keep separate jobs only for the rare deterministic-skip/post-merge dispatch paths.
   - **Estimated time savings:** 18–69 minutes on bad outliers; this is the strongest lever on `review_autofix` p95.
   - **Implementation risk:** medium; job-output plumbing will need refactoring.

2. **Lower default reasoning effort for routine `plan` and `implement` runs (critical-path win, inference)**
   - **Evidence:** Run `26629610285` (`plan`) used `MODEL_EDITOR=openai/gpt-5.4` and `MODEL_REASONING_EFFORT=xhigh` and took 655s. Run `26630085547` (`implement`) used the `xhigh` default and spent 639.8s inside `Run Codex implementation` alone. Success-only averages were 628.8s for `plan` and 857.8s for `implement`.
   - **Root cause:** `plan.yml` and `implement.yml` default to `xhigh` reasoning on every real run, not just hard cases.
   - **Exact change:** Make `THINKING_LEVEL_PLAN` and `THINKING_LEVEL_IMPLEMENT` tiered: default to `high` for normal/small issues, reserve `xhigh` for retries, large diffs, or manual override.
   - **Estimated time savings:** ~60–180s per `plan` run and ~1–3 minutes per `implement` run (**inference**, no A/B in this window).
   - **Implementation risk:** medium; quality should be checked with an A/B rollout.

3. **Conditionalize `free-disk-space` in `review_autofix` (micro-optimization)**
   - **Evidence:** Across 8 sampled slow `review_autofix` deep-dive runs, the `jlumbroso/free-disk-space@v1.3.1` step cost 27.7–172.9s, averaging 80.8s, while `setup-uv` was already hitting cache.
   - **Root cause:** Expensive cleanup runs unconditionally even when the runner already has enough headroom.
   - **Exact change:** Add a precheck on free space / repo size class; skip `free-disk-space` when the runner starts above a safe threshold.
   - **Estimated time savings:** ~1–3 minutes per affected run; roughly ~1.0–1.5h aggregate if this window is representative.
   - **Implementation risk:** low-medium; keep the current path for low-disk cases.

4. **Dispatch the poller on phase completion instead of waiting for cron (end-to-end speed, inference)**
   - **Evidence:** `.github/workflows/internal-orchestrate-poll.yml` is scheduled at `*/5 * * * *`; `orchestrate_poll` had 22/22 successes but still averaged 117.6s, and run `26622432141` spent ~125s of 133s in the poll job once it started.
   - **Root cause:** State changes wait for the next scheduled poll even when upstream phases already know they changed workflow state.
   - **Exact change:** Keep cron as a backstop, but add `workflow_dispatch` of `internal-orchestrate-poll.yml` from major phase-complete points.
   - **Estimated time savings:** up to ~5 minutes of handoff latency per transition (**inference**).
   - **Implementation risk:** medium; must rely on existing poller concurrency to dedupe.

## Cost Optimizations

1. **Stop paying for cancelled and superseded `review_autofix` runs**
   - **Evidence:** 9 cancelled `review_autofix` runs consumed 12,462s total; the biggest were `26626033377` (4,699s), `26622992792` (2,674s), `26618298656` (1,731s), `26619609885` (1,720s), and `26618263808` (1,566s). Cancellation rate in this family was 13.0%.
   - **Root cause:** Long-running review work survives long enough to be cancelled after substantial LLM/setup spend.
   - **Exact change:** Combine the `gate` and heavy review job; then pilot the existing `AUTOFIX_SKIP_SELF_TRIGGERED=true` knob for repos where the cron/manual fallback is acceptable.
   - **Estimated savings:** immediate recovery of ~3.46h runtime in a similar window from cancellations alone; source comments indicate self-trigger skip can cut about half of autofix LLM spend per fix cycle (**inference**, pilot first).
   - **Quality-risk notes:** low-medium; keep manual `workflow_dispatch` and orchestrator backstops.

2. **Reduce reviewer fan-out on low-risk diffs (inference)**
   - **Evidence:** `review_autofix` is 75.6% of total runtime. Runtime summaries show the full reviewer panel is still configured with 6 models (`minimax`, `kimi`, `deepseek`, `mistral-small`, `qwen`, `grok`) plus `XPOLL_SUMMARISER_MODEL=gpt-5.4-mini`; workflow source keeps `ENABLE_REVIEWER_TWO_PASS=true` by default.
   - **Root cause:** Expensive reviewer fan-out is the default path even though deterministic skip and small-diff logic already exist.
   - **Exact change:** Use a smaller reviewer set / single-pass mode for low-risk file classes and borderline-small diffs, while keeping the full panel for risky changes.
   - **Estimated savings:** likely the biggest token lever after rerun suppression, but not directly measurable from this window.
   - **Quality-risk notes:** medium; gate it behind explicit risk tiers and monitor defect escape rate.

3. **Trim Copilot review prompt size before changing models**
   - **Evidence:** Copilot run `26630677335` built a 21,219-token prompt; run `26620560625` built a 28,244-token prompt and retrieved 20 memories. The sampled model mix (`claude-opus-4.7` vs `gpt-5.5`) is not apples-to-apples enough to justify a model swap from this window.
   - **Root cause:** Prompt growth from memory inserts plus tool-search excerpts is a safer cost target than model swapping.
   - **Exact change:** Cap memory inserts to highest-confidence items, dedupe repeated `search_file`/`search_dir` snippets, and suppress low-value tool chatter in the prompt path.
   - **Estimated savings:** ~4k–8k prompt tokens per Copilot review run if prompt size is cut 20–30%.
   - **Quality-risk notes:** low-medium; prompt trimming is safer than switching models on thin evidence.

4. **Keep Semble enabled; do not invest in Serena yet**
   - **Evidence:** 12 real `SEMBLE_QUERY` events logged 141,607 bytes total at ~488.8ms average latency; targets were `reviewer-context` (8) and `overflow` (4). `SERENA_QUERY`, `SERENA_FALLBACK`, and `SERENA_PROBE` were all 0, and deep logs repeatedly showed `SERENA_ENABLED: false`.
   - **Root cause:** Semble is actually being used; Serena is not.
   - **Exact change:** Keep Semble on; defer Serena rollout/tuning until production probes or queries exist.
   - **Estimated savings:** avoids spending engineering effort on a currently unused path; Semble likely reduces prompt expansion rather than increasing it (**inference**).
   - **Quality-risk notes:** low.

5. **Tune reviewer memory retrieval or turn it down**
   - **Evidence:** 8/9 AI-memory retrieves returned 0 records; all 8 reviewer retrieves were zero-hit, and 7 of those used `keyword_method=llm`. The only implementation retrieve hit 1 record with 28 estimated tokens in run `26630085547`.
   - **Root cause:** Reviewer retrieval is adding control-plane work without returning usable context.
   - **Exact change:** Tighten the `reviewer` profile in `ai-memory/config/retrieval_profiles.v1.json`, or switch reviewer retrieval to plain keywords / bypass retrieval after repeated zero-hit runs.
   - **Estimated savings:** small per run, but worthwhile because it cleans the highest-cost workflow family.
   - **Quality-risk notes:** low; current reviewer memory is mostly empty anyway.

## Reliability Improvements

1. **Fix brace-expansion rename parsing in the reviewer diffstat filter**
   - **Failure evidence:** The only hard failure was run `26629481886`, workflow `CI`, job `lint`, step `Review autofix review-pipeline plumbing contract test`. The traceback shows `test_reviewer_filter_stat_harness_handles_brace_expansion_renames()` failed through `_run_reviewer_stat_filter_harness(...)`, and the failing inline helper was `filter_reviewer_stat_file_against_skips`.
   - **Root cause category:** parser edge case / contract-test regression.
   - **Exact fix:** Harden brace-expansion rename parsing so malformed or nested rename diffstat rows fail open and preserve the original line instead of failing the subprocess.
   - **Expected reliability impact:** removes 100% of observed failures in this window; CI family failure rate would drop from 14.3% (1/7) to 0 if this bug is isolated.
   - **Rollback/fail-open:** on parse ambiguity, log a warning and keep the raw diffstat row.

2. **Prevent long stale `review_autofix` runs from reaching expensive stages**
   - **Failure evidence:** 9 cancelled `review_autofix` runs, 12,462s total wasted runtime; 5 long cancellations accounted for 12,390s.
   - **Root cause category:** overlap/superseded work (**inference**).
   - **Exact fix:** remove the extra runner handoff, then add an early stale-head/self-trigger exit before reviewer/editor spend.
   - **Expected reliability impact:** fewer reruns and fewer operator/manual cancels in the noisiest workflow family.
   - **Rollback/fail-open:** keep current concurrency and manual dispatch as fallbacks.

3. **Treat missing Copilot memory votes as benign**
   - **Failure evidence:** Run `26629488419` logged `HTTP 422: A memory with the specified fact and scope was not found.` before a later successful memory upvote.
   - **Root cause category:** external-state mismatch / eventually consistent API.
   - **Exact fix:** preflight existence before voting, or treat 404/422 vote misses as debug-level no-ops.
   - **Expected reliability impact:** reduces noisy review failures and false alarms in Copilot review runs.
   - **Rollback/fail-open:** if the API contract changes, keep best-effort voting.

4. **Keep Semble fail-open; fix the CI harness instead**
   - **Failure evidence:** 5 `SEMBLE_FALLBACK` events were observed, all in run `26629481886`, all `target=overflow`, all `ms=0`, all caused by missing `missing_semble` fixture paths.
   - **Root cause category:** test-fixture/probe availability, not runtime production failure.
   - **Exact fix:** make the CI contract test inject the Semble fixture explicitly or assert the fallback path directly.
   - **Expected reliability impact:** prevents test-only fixture problems from looking like a production Semble regression.
   - **Rollback/fail-open:** none needed; current production behavior is already safe.

## AI Memory Health

- **Telemetry is present and healthy:** 40 unique `AI_MEMORY_TELEMETRY` events were observed in deep logs, so emission is working.
- **Retrieve effectiveness is poor for reviewer flows:** 9 retrieves total, hit rate 11.1% (1/9). Average `estimated_tokens` was 3.1, far below configured role budgets (`reviewer` 1400, `implementation` 1600 in `ai-memory/config/retrieval_profiles.v1.json`), which means the problem is retrieval usefulness, not token pressure.
- **Keyword-method mix:** `llm` 7, `plain` 1, `none` 1.
- **Zero-hit pattern:** all 8 reviewer retrieves were zero-hit, across runs `26575258172`, `26580059509`, `26590027158`, `26591960650`, `26620555977`, `26622285035`, `26624807639`, and `26624817222`.
- **Healthy implementation path:** run `26630085547` retrieved 1 implementation memory with `estimated_tokens=28` using `keyword_method=plain`.
- **No degraded transport signs:** 0 retrieves had `fail_open: true`; 0 had `enabled: false`; 0 telemetry records showed push retries >1.
- **Observed op mix:** `record-run-event` 18, `record-candidate` 9, `retrieve` 9, plus a normal single `processed-command-check`/`claim`/`complete` and `finalize-task`. No `compact` or `promote` telemetry appeared in sampled deep logs.
- **Recommendation:** keep implementation retrieval as-is; tighten or downgrade reviewer retrieval before expanding memory usage further.

## GH API Call Audit

1. **Largest live hotspot: Copilot session-log uploads**
   - **Evidence:** In run `26630677335` (`copilot_pull_request_reviewer`, step `Processing Request Linux`), 16 Copilot API calls were visible: 11 `PUT /agents/sessions/<id>/logs`, 3 `PUT /agents/sessions/<id>`, 1 job `GET`, 1 progress `POST`. Similar repeated `/logs` uploads also appeared in run `26622998126` (8 uploads, from `log_summary`) and run `26620560625` (3 uploads, from `log_summary`).
   - **Recommendation:** if the action allows it, batch or lower log upload frequency; otherwise reduce verbose tool output so fewer log flushes are worth sending.
   - **Estimated reduction:** 30–60% fewer Copilot log-upload calls in that step.
   - **Rate-limit impact:** low GitHub REST risk, but still useful control-plane cleanup.

2. **Internal workflow API hygiene is mostly good already**
   - **Evidence:** `review_autofix.yml` reuses a single `/pulls/{n}` call for state, merged flag, head ref, labels, additions, and deletions, and only paginates `/files` when doc-only detection needs filenames. `plan.yml` uses issue timeline data instead of fuzzy `gh pr list --search`. `internal-review.yml` resolves claude-branch PRs with a head-specific query.
   - **Recommendation:** preserve these patterns; they are the right baseline.

3. **Best source-based cleanup target: repeated issue-comment reads in `plan`**
   - **Evidence:** Source inspection of `.github/workflows/plan.yml` shows multiple separate `gh api repos/.../issues/.../comments` reads and progress-comment lookups across the `plan` job.
   - **Root cause:** comment state is fetched repeatedly instead of snapshotted once and reused.
   - **Exact change:** fetch comments once early, persist the progress-comment ID/body locally, and only `PATCH`/`POST` when content actually changes.
   - **Estimated reduction:** ~5–10 GitHub REST calls per full `plan` run (**source-based estimate**).
   - **Rate-limit impact:** low-medium; mostly hygiene and latency trimming.

4. **No actual GitHub REST rate-limit incidents surfaced in this window**
   - **Evidence:** after filtering out echoed shell code and docs, no runtime 429/secondary-rate-limit events were found in the deep logs or sampled `log_summary` entries.
   - **Recommendation:** keep current retry wrappers; they appear preventive rather than reactive in this sample.

## Prompt Cache & Memory System

- **Architecture is cache-aware:** `OPENROUTER_PROMPT_CACHE_DISABLED` defaults to `false` in `clarify`, `plan`, `implement`, `review_autofix`, and `orchestrate_poll`; `openrouter_prompt_cache.py` supports an explicit ephemeral cache breakpoint; `plan`, `clarify`, `implement`, and `review_autofix` all pre-assemble static context specifically to keep a stable prompt prefix.
- **But runtime prompt-cache metrics were not observable:** no real `prompt_tokens/completion_tokens/cache_creation_input_tokens/cache_read_input_tokens` usage lines were emitted in sampled deep logs. Only source-echoed/template lines were present.
- **Implication:** actual cache hit rate, miss rate, read/write token savings, and fail-open behavior are currently unmeasured.
- **Likely fragmentation sources (inference):** volatile issue comments, diff hunks, PR check-run log tails, Semble overflow snippets, and memory context appended during runtime. The static prefix work is good; the remaining risk is letting dynamic material drift into that prefix.
- **Concrete improvements:**
  1. Turn on runtime emission of the existing normalized usage fields for each major OpenRouter call.
  2. Keep all volatile sections strictly after the pre-assembled static block.
  3. When reviewer memory returns 0 records, emit a short constant stub instead of any variable empty section text.
  4. If `REVIEWER_CACHE_PROBE_ENABLED` exists for sampling, enable it on a tiny sample so cache savings become measurable.
- **Estimated impact:** high observability gain immediately; token/latency savings are unknown until usage lines are emitted.

## Orchestrator Health

- The orchestrator path is **stable** in this window: `clarify`, `plan`, `implement`, `orchestrate_poll`, and `orchestrate_clarify_respond` had 0 failures. The only failure in the repo was CI.
- Clarification automation is working: run `26629561866` recorded `Clarification completed: auto_answered_by_orchestrator`.
- The main control-plane issue is **noise, not failure**: 261 runs ended in `other/skipped`, mostly from `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`, but they consumed only 518s total.
- The poller is **serialized and healthy**: `.github/workflows/orchestrate_poll.yml` uses concurrency group `ai-orchestrate-poll-${repo}` with `cancel-in-progress: false`; 22/22 sampled poller runs succeeded.
- The bigger orchestrator pain point is **reaction latency**: cron-driven polling (`*/5`) plus ~118s average poll runtime. I did not see evidence of clarification loops, conflict-heal storms, or repeated MCP probe failures in this window.
- **Smallest safe mitigations:**
  - opportunistic poller dispatch on phase-complete events,
  - coalesced reporting for skipped wrapper runs,
  - ongoing tracking of: skipped-run ratio, poll cycle age, long-cancelled `review_autofix` runs, and AI-memory push retries >1 (currently zero).

## Pipeline Flow Bottlenecks

1. **Review/autofix is the dominant end-to-end bottleneck**
   - **Queueing:** second-runner delays of 1,096.7s (`26622285035`) and 4,163.1s (`26624807639`).
   - **Compute:** 60 successful runs still averaged 979.6s.
   - **Retry/rerun waste:** 9 cancellations consumed 12,462s.
   - **Fixed overhead:** ~80.8s average `free-disk-space` tax in sampled slow runs.

2. **Implement is the next biggest compute stage**
   - **Evidence:** success-only average 857.8s; in run `26630085547`, `Run Codex implementation` alone took 639.8s of an 883s run.
   - **Type:** compute-bound, not cache-bound.

3. **Plan is also compute-heavy**
   - **Evidence:** success-only average 628.8s; run `26629610285` took 655s on `gpt-5.4` with `xhigh` reasoning.
   - **Type:** compute-bound with some runner wait.

4. **Poll/orchestration adds handoff latency**
   - **Evidence:** `orchestrate_poll` averages 117.6s, and the wrapper cron fires every 5 minutes.
   - **Type:** queue/scheduler latency rather than model cost.

5. **Clarify is not a throughput problem**
   - **Evidence:** successful `clarify` runs averaged 95.3s; skipped clarify runs consumed little total runtime.
   - **Type:** mostly runner start + modest compute.

6. **Validate/merge/conflict overhead is not first-wave work in this sample**
   - **Evidence:** `validation_refresh` had only 2 runs (avg 760s), and sampled deep logs did not show merge-conflict retry storms or resolver loops dominating runtime.
   - **Type:** insufficient deep evidence; collect more before optimizing.

**Ordered by end-to-end impact:**  
`review_autofix` queue collapse → `review_autofix` rerun suppression → plan/implement reasoning-tiering → event-driven poller dispatch → skipped-run reporting cleanup.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` consumed 75.6% of all observed runtime.
  - `plan` and `implement` are compute-heavy when they actually run.
  - `orchestrate_poll` adds control-plane latency because it is cron-driven.

- **Top failure modes**
  - One CI contract-test regression (`26629481886`) in brace-expansion rename parsing.
  - Long cancelled `review_autofix` runs causing wasted runtime/cost.
  - No sampled GitHub REST rate-limit failures or MCP rollout failures.

- **Highest-cost drivers**
  - Full `review_autofix` reviewer/editor loop with long queueing and cancellations.
  - Copilot review prompts in the 21k–28k token range in sampled runs.
  - Reviewer memory retrieval that usually returns no records.

- **Top 3 prioritized actions**
  1. Collapse `review_autofix` gate and heavy work into one runner path.
  2. Pilot `AUTOFIX_SKIP_SELF_TRIGGERED=true` and cut stale reruns/cancellations.
  3. Fix the brace-expansion rename parser and make malformed diffstat rows fail open.

## Metrics Appendix

**Repo summary**

| Repo | Runs | Success | Failure | Cancelled | Other/skipped | Success rate | Failure rate | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 411 | 139 | 1 | 10 | 261 | 33.8% | 0.24% | 2.0 | 1659.0 |

**Core workflow-family metrics**

| Workflow family | Runs | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s | Runtime share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 69 | 60 | 0 | 9 | 0 | 1032.4 | 627.0 | 3498.2 | 75.6% |
| ci | 7 | 6 | 1 | 0 | 0 | 1239.6 | 1394.0 | 1633.5 | 9.2% |
| implement | 67 | 4 | 0 | 0 | 63 | 53.1 | 1.0 | 331.3 | 3.8% |
| plan | 67 | 4 | 0 | 0 | 63 | 38.9 | 1.0 | 418.5 | 2.8% |
| orchestrate_poll | 22 | 22 | 0 | 0 | 0 | 117.6 | 117.0 | 146.5 | 2.7% |
| copilot_pull_request_reviewer | 11 | 10 | 0 | 1 | 0 | 148.5 | 104.0 | 316.0 | 1.7% |
| validation_refresh | 2 | 2 | 0 | 0 | 0 | 760.0 | 760.0 | 926.5 | 1.6% |
| clarify | 72 | 4 | 0 | 0 | 68 | 7.3 | 1.0 | 45.3 | 0.6% |
| orchestrate_clarify_respond | 67 | 0 | 0 | 0 | 67 | 2.4 | 1.0 | 10.0 | 0.2% |

**`review_autofix` workflow-name breakdown**

| Workflow name | Runs | Success | Cancelled | Avg s | p95 s |
|---|---:|---:|---:|---:|---:|
| Internal: AI Review Autofix Sweep | 21 | 21 | 0 | 9.9 | 16.0 |
| Internal: AI Review & Autofix | 38 | 32 | 6 | 1494.7 | 3956.1 |
| Codex PR Self-Healing Semantic Agent | 10 | 7 | 3 | 1423.1 | 3594.9 |

**Cancelled `review_autofix` runs**

| Run ID | Workflow | Duration s | Created at |
|---|---|---:|---|
| 26626033377 | Internal: AI Review & Autofix | 4699 | 2026-05-29T08:09:47Z |
| 26622992792 | Internal: AI Review & Autofix | 2674 | 2026-05-29T06:56:08Z |
| 26618298656 | Internal: AI Review & Autofix | 1731 | 2026-05-29T04:38:39Z |
| 26619609885 | Internal: AI Review & Autofix | 1720 | 2026-05-29T05:19:41Z |
| 26618263808 | Internal: AI Review & Autofix | 1566 | 2026-05-29T04:37:31Z |
| 26591949214 | Internal: AI Review & Autofix | 22 | 2026-05-28T17:46:25Z |
| 26619224092 | Codex PR Self-Healing Semantic Agent | 21 | 2026-05-29T05:07:20Z |
| 26620564148 | Codex PR Self-Healing Semantic Agent | 16 | 2026-05-29T05:48:11Z |
| 26619104075 | Codex PR Self-Healing Semantic Agent | 13 | 2026-05-29T05:03:29Z |

**Observed prompt-token sample**

| Run ID | Workflow | Prompt tokens | Source |
|---|---|---:|---|
| 26630677335 | Running Copilot Code Review | 21219 | deep log |
| 26620560625 | Running Copilot Code Review | 28244 | `log_summary` |
| Sampled total | — | 49463 | sampled only; not full-window total |

**AI memory metrics**

| Metric | Value |
|---|---|
| Unique telemetry events | 40 |
| Retrieve ops | 9 |
| Retrieve hit rate | 11.1% (1/9) |
| Avg estimated tokens per retrieve | 3.1 |
| Keyword method mix | `llm` 7 / `plain` 1 / `none` 1 |
| Zero-hit retrieves | 8 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Push retries >1 | 0 |

**AI memory op mix**

| Operation | Count |
|---|---:|
| record-run-event | 18 |
| retrieve | 9 |
| record-candidate | 9 |
| processed-command-check | 1 |
| processed-command-claim | 1 |
| processed-command-complete | 1 |
| finalize-task | 1 |

**Semble / Serena telemetry**

| Server | Target | Queries | Fallbacks | Probe count | Logged bytes | Avg query ms | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| Semble | reviewer-context | 8 | 0 | n/a | 117265 | 492.9 | all from deep `review_autofix` runs |
| Semble | overflow | 4 | 5 | n/a | 24342 | 480.5 | all 5 fallbacks were CI fixture misses in `26629481886` |
| Serena | all | 0 | 0 | 0 | 0 | n/a | disabled in sampled runs |

**MCP availability**

| Server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | all | 0 | 0 | 0 | no `SERENA_PROBE` events observed; deep logs showed `SERENA_ENABLED: false` |

**GH API hotspot summary**

| Run ID | Workflow | Endpoint | Count | Source |
|---|---|---|---:|---|
| 26630677335 | Running Copilot Code Review | `PUT /agents/sessions/<id>/logs` | 11 | deep log |
| 26630677335 | Running Copilot Code Review | `PUT /agents/sessions/<id>` | 3 | deep log |
| 26630677335 | Running Copilot Code Review | `GET /agents/swe/agent/jobs/...` | 1 | deep log |
| 26630677335 | Running Copilot Code Review | `POST /agents/swe/agent/jobs/.../progress` | 1 | deep log |
| 26622998126 | Running Copilot Code Review | `PUT /agents/sessions/<id>/logs` | 8 | `log_summary` |
| 26620560625 | Running Copilot Code Review | `PUT /agents/sessions/<id>/logs` | 3 | `log_summary` |
| 26620560625 | Running Copilot Code Review | `PUT /agents/sessions/<id>` | 3 | `log_summary` |

**Cache observations**

| Cache signal | Observed hits | Scope | Notes |
|---|---:|---|---|
| `setup-uv` | 8 deep-log hits | slow/recent `review_autofix`, recent `implement` | base dependency cache is healthy |
| `review-ledger` restore | 8 deep-log hits | slow `review_autofix` | ledger reuse already works |
| npm `@playwright/mcp` warm | 1 | recent Copilot run `26630677335` | background cache warm only |
| OpenRouter prompt-cache read/write metrics | 0 observed | all sampled logs | instrumentation gap, not proof of no cache use |

**Other MCP servers observed:** none.
