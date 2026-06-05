## Executive Summary

- **Fix the Phase-4 cancelled-sibling poller bug first.** `Test & Mark Stable Release` run **26989483268** spent the full **30-minute `REVIEW_TIMEOUT`** re-checking cancelled review run **26990671639** and failed, while `issue_pr_status` run **26993925214** documents that sibling run **26990664122** had already succeeded on the same `PIN_SHA`. **Estimated impact:** save ~30 minutes and >180 GH API polls per occurrence. **Confidence:** high.
- **`review_autofix` latency is overwhelmingly inside `review_codex-agent`, not the gate.** Sampled runs **26968187150** and **26977120613** spent **14,431.8s** and **14,433.6s** in `step-001-review_codex-agent`, while `review_gate` took only **3.2s** and **1.3s`; even successful small-diff runs **26992643842** and **26992679673** still took **1199s** and **1172s**. **Estimated impact:** 8–240 minutes/run depending on path. **Confidence:** high.
- **CI is the second major bottleneck and has a repeated deterministic failure.** `ci` family p50 is **1462.5s**, p95 **1555.85s**; slow run **26965553101** spent **1655.8s** in `lint`. Three failures (**26956143718**, **26968085383**, **26991372201**) all hit the same assertion: Codex stdin must come from the per-attempt prompt file. **Estimated impact:** remove most recent CI failures and cut healthy CI by ~5–9 minutes. **Confidence:** high.
- **`workflow_log_analysis` is both expensive and brittle.** Run **26989503339** spent **2099.6s** in `deep-audit` before failing its required heading contract after reconnect storms; the direct log also shows `tokens used` = **2,059,608**. Its `analyze-commit-notify` step spent **1973.0s**, and `AI_MEMORY_TELEMETRY` reports `summarize_unselected_runs` used **131,338 tokens** to summarize **88/100** runs. **Estimated impact:** save 15–35 minutes and large token spend per analysis run. **Confidence:** medium.
- **Prompt-cache telemetry is effectively blind right now.** Repo-wide, **114** runs had parsed log telemetry, but `cache_hit_rate=null`, all `or_*` token/cache counters are **0**, `break_glass_count=0`, and `context_budget_warn_count=0`. **Estimated impact:** medium; instrumentation is needed before model/cache tuning. **Confidence:** high.
- **AI memory retrieval is not helping sampled review runs.** Exact top-level `AI_MEMORY_TELEMETRY` in sampled `review_autofix` runs (**26952637519**, **26960113331**, **26968187150**, **26977120613**, **26989803986**) shows `retrieve` hit rate **0/5**, `estimated_tokens=0`, `keyword_method="llm"`. **Estimated impact:** small-to-medium latency/token savings plus better relevance if fixed. **Confidence:** high.

## Speed Optimizations

**Critical-path wins**

1. **Prefer the latest non-cancelled review run at `PIN_SHA` in Phase 4**
   - **Evidence:** `test_and_mark_stable` run **26989483268** logged repeated `Review run was cancelled — checking for newer run...` messages until `##[error]Review phase stalled — no activity for 30 minutes`; `issue_pr_status` run **26993925214** documents that run **26990664122** (`success`) and **26990671639** (`cancelled`) shared the same `PIN_SHA`, and the newer cancelled run won the `sort_by(.created_at) | last` selection.
   - **Root cause:** poller selects newest matching run, not newest **usable** run.
   - **Exact change:** in `.github/workflows/test-and-mark-stable.yml`, select the latest **non-cancelled** run at `PIN_SHA`, and only fall back to the latest cancelled run if *all* matches are cancelled.
   - **Estimated time savings:** ~**30 min** per affected smoke run.
   - **Implementation risk:** **low**; the documented fix preserves the all-cancelled timeout behavior.

2. **Terminate `review_codex-agent` immediately after terminal abort conditions**
   - **Evidence:** run **26968187150** logged `Editor aborted — PR #3082 is closed (attempt 1)` at **19:59:11Z**, but the step was not cancelled until **23:14:03Z** (~**11,692s** later). Run **26977120613** logged `Editor killed — no output for 1205s...` at **21:20:16Z**, but the step was not cancelled until **00:18:21Z** (~**10,686s** later).
   - **Root cause:** terminal editor states are detected, but the workflow does not short-circuit the surrounding long-running step.
   - **Exact change:** once PR-closed or watchdog-idle-kill is detected, emit final diagnostics and exit the step/workflow immediately instead of waiting for external cancellation.
   - **Estimated time savings:** **~3 hours** on worst-case cancelled runs; major p99 reduction.
   - **Implementation risk:** **medium-low**; keep fail-open logging before exit.

3. **Introduce a small-diff review fast path**
   - **Evidence:** `review_autofix` run **26992643842** took **1199s** on a PR with `files=1 additions=1` and logged `REVIEWER_PASS2_REASONING_LARGE: xhigh`; run **26992679673** took **1172s** on `files=6 additions=3`, with the `review` step spanning ~**1140s**.
   - **Root cause:** trivial diffs still run an expensive second-pass review path.
   - **Exact change:** reuse the existing deterministic gate inputs (`files`, additions/deletions, labels) to skip pass-2 or cap reasoning for tiny diffs unless risky paths or override labels are present.
   - **Estimated time savings:** **8–15 min** on many small PR reviews.
   - **Implementation risk:** **medium**; keep `force-review`/risk-path overrides.

4. **Split/parallelize CI `lint`**
   - **Evidence:** `ci` family p50 is **1462.5s**; p95 is **1555.85s**; slow run **26965553101** spent **1655.8s** in `step-001-lint.log`. Successful runs **26992679538** (**1323s**) and **26993277512** (**1125s**) both report `lint` dominating runtime.
   - **Root cause:** one monolithic `lint` job serializes fast checks and contract tests.
   - **Exact change:** split `actionlint`, Python syntax, `ruff`, workflow-ref checks, and contract tests into parallel jobs; make the known-noisy `test_review_autofix_editor_noop_cascade_contract.py` fail fast early.
   - **Estimated time savings:** **5–9 min** per healthy CI run.
   - **Implementation risk:** **medium-low**; behavior stays identical, only scheduling changes.

5. **Fail fast on malformed `workflow_log_analysis` deep-audit output**
   - **Evidence:** run **26989503339** spent **2099.6s** in `step-001-deep-audit.log`, hit multiple `Reconnecting...` events, then ended with `I'm sorry, but I cannot assist with that request.` and `Deep audit Codex output did not start with the required ... heading`.
   - **Root cause:** the workflow lets a clearly-invalid model session continue until the step naturally fails late.
   - **Exact change:** if the first structured output chunk is malformed/refusal text, stop the attempt immediately and retry with narrowed scope or fail fast.
   - **Estimated time savings:** **20–35 min** per failed analysis run.
   - **Implementation risk:** **medium**; may reduce report breadth on retry paths.

**Micro-optimizations**

6. **Reduce tiny workflow fan-out that mostly pays runner queue time**
   - **Evidence:** `issue_pr_status` runs **26993875889**/**26993925214** took **10s**/**13s** and both logged `Job is waiting for a hosted runner to come online`; `forward_merge_stable_to_main` run **26994091142** took **18s** and also waited for a runner; `review_autofix` sweep run **26994004178** took **7s** and waited as well.
   - **Root cause:** many near-no-op workflows still incur full hosted-runner startup.
   - **Exact change:** tighten event filters and top-level `if:` guards so no-op status-sync/sweep/dispatch workflows do not launch.
   - **Estimated time savings:** **seconds to low minutes per PR**; also lowers queue contention.
   - **Implementation risk:** **low**.

## Cost Optimizations

1. **Narrow `workflow_log_analysis` scope before changing models**
   - **Evidence:** run **26989503339** logged direct `tokens used` = **2,059,608** in `deep-audit`; `AI_MEMORY_TELEMETRY` in `analyze-commit-notify` reports `summarize_unselected_runs` with `targeted=100`, `summarized=88`, `tokens_used=131338`.
   - **Root cause:** the analysis workflow spends tokens on very broad deep audit plus broad success-run summarization.
   - **Exact change:** always keep all failed/slow runs, but cap unselected success-run summarization by workflow-family novelty; stop after one or two representative successes per family; fail fast on malformed deep-audit sessions.
   - **Estimated savings:** **>131k tokens** immediately from summarization scope reduction, plus potentially **orders of magnitude** more on failed deep-audit sessions.
   - **Quality-risk notes:** **medium**; preserve all failures/outliers to avoid losing evidence.

2. **Lower reasoning level on tiny review diffs before swapping models**
   - **Evidence:** run **26992643842** used `REVIEWER_PASS2_REASONING_LARGE: xhigh` for a `files=1 additions=1` PR and still took **1199s**.
   - **Root cause:** reasoning depth, not model family, appears mis-sized for tiny changes.
   - **Exact change:** keep the current model, but reduce/skip pass-2 reasoning when diff size is below existing deterministic thresholds and no override label/risky path is present.
   - **Estimated savings:** likely the largest recurring review-token reduction on small PRs; wall-clock evidence suggests **8–15 min** saved per eligible run.
   - **Quality-risk notes:** **medium**; retain full path for risky files and `force-review`.

3. **Eliminate avoidable reruns/timeouts caused by cancelled-sibling polling**
   - **Evidence:** run **26989483268** failed after a full 30-minute review timeout even though the successful sibling run already existed; cancelled run **26992634811** also repeated `Review run was cancelled — checking for newer run...` for the full `30-minute REVIEW_TIMEOUT`.
   - **Root cause:** duplicate or superseded review runs still burn polling time and often trigger follow-on reruns.
   - **Exact change:** fix non-cancelled run selection and suppress duplicate dispatches that point at the same head SHA.
   - **Estimated savings:** full timed-out wait windows plus any duplicate review execution.
   - **Quality-risk notes:** **low**.

4. **Keep Semble enabled for reviewer context; clean up the noisy fallback accounting**
   - **Evidence:** sampled `review_autofix` runs **26952637519**, **26960113331**, **26968187150**, **26977120613**, **26989803986** each logged exactly one `SEMBLE_QUERY target=reviewer-context` at about **14.7–15.3 KB** and **478–521 ms**; sampled average is **~15.1 KB/query** and **~495 ms/query**. Family-level parsed telemetry for `review_autofix` shows **23** queries / **258,156** bytes / **0** fallbacks. By contrast, direct fallbacks in CI/test runs (**26956143718**, **26968085383**, **26991372201**, **26989483268**) were all `SEMBLE_FALLBACK target=overflow ... reason=...missing_semble`.
   - **Root cause:** production reviewer-context queries look modest and targeted, but fallback totals are polluted by fixture/runtime-test missing-binary cases and likely by self-referential `workflow_log_analysis` output.
   - **Exact change:** keep Semble on for `reviewer-context`; exclude test-fixture/self-generated report text from production cost rollups.
   - **Estimated savings:** avoids a false “disable Semble” decision that would likely re-expand raw prompt context. **Inference:** Semble is probably reducing prompt expansion in review flows, but prompt-token telemetry is too incomplete to prove the exact savings.
   - **Quality-risk notes:** **low**.

5. **Do not spend effort tuning Serena yet**
   - **Evidence:** sampled review logs repeatedly show `SERENA_ENABLED: false` and `SERENA_AVAILABLE: false`; repo aggregate only shows `serena_query_calls=1`, `serena_fallbacks=1`, `serena_probe_skipped=1`, with `serena_query_response_bytes=0`, and I could not find a trustworthy operational `SERENA_QUERY`/`SERENA_FALLBACK` line outside self-analysis text.
   - **Root cause:** Serena is mostly disabled or telemetry is contaminated; it is not yet replacing downstream model/tool work in a measurable way.
   - **Exact change:** keep Serena fail-open and explicit-disabled; defer tool-replacement tuning until real operational telemetry exists.
   - **Estimated savings:** prevents wasted tuning/debug effort; current direct response-byte savings are effectively **0**.
   - **Quality-risk notes:** **low**.

6. **Instrument prompt-cache behavior before trying to optimize it**
   - **Evidence:** across **114** telemetry-covered runs, repo-wide `cache_hit_rate=null`, `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `or_calls=0`. Actual runtime `CONTEXT_BUDGET_WARN` events were also **0**; only config lines like `CONTEXT_BUDGET_WARN_RATIO: 0.7` appear in review logs.
   - **Root cause:** there is no usable prompt-cache visibility in this window.
   - **Exact change:** emit per-phase prompt-cache metrics and a stable prompt fingerprint/hash so cacheability and fragmentation can be measured.
   - **Estimated savings:** **unknown until instrumented**.
   - **Quality-risk notes:** **low**; this is observability-first.

## Reliability Improvements

- **Window note:** across the telemetry-covered runs, `break_glass_count=0` and `context_budget_warn_count=0`. I did **not** see evidence of runtime break-glass pressure or true context-budget overflows in this window; repeated `CONTEXT_BUDGET_WARN_RATIO: 0.7` lines are configuration, not event counts.

1. **Fix the Phase-4 cancelled-sibling selection bug**
   - **Failure evidence:** `test_and_mark_stable` run **26989483268** failed in `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`; its log ended with `✗ Review ..... FAILED (timeout)`. `issue_pr_status` run **26993925214** documents the exact bad pair: success **#26990664122** vs cancelled **#26990671639** on the same SHA.
   - **Root cause category:** poller state-selection bug.
   - **Exact fix:** prefer latest non-cancelled run at pinned SHA; only use cancelled if every match is cancelled.
   - **Expected reliability impact:** removes the proven timeout mode in release smoke tests and likely reduces related review-gate cancellation loops.
   - **Rollback / fail-open:** safe; preserve current timeout behavior on true all-cancelled sets.

2. **Fix the deterministic CI contract regression**
   - **Failure evidence:** CI failures **26956143718**, **26968085383**, and **26991372201** all failed in `tests/test_review_autofix_editor_noop_cascade_contract.py` with the same assertion: `codex stdin must be fed from the per-attempt prompt file, not from the unchanging ${EDITOR_PROMPT_FILE}`.
   - **Root cause category:** deterministic workflow/plumbing drift.
   - **Exact fix:** update the review apply-fixes path so Codex stdin is read from `${attempt_prompt_file}` on each attempt.
   - **Expected reliability impact:** addresses **3 of the 4** observed CI failures in this window.
   - **Rollback / fail-open:** keep the contract test in CI as the guardrail.

3. **Preserve validate/implement failure diagnostics before archives disappear**
   - **Failure evidence:** `validate` family failed **9/10** times in the window, and **8** validate failures plus **1** implement failure returned `partial_data:missing_log_archive ... HTTP 404` (e.g. validate runs **26991370442**, **26989824610**, **26985113434**, **26977117868**, **26968184196**, **26968082635**, **26960106413**, **26956137504**; implement run **26953236458**).
   - **Root cause category:** observability / artifact-retention gap.
   - **Exact fix:** write a concise failing-step summary to `$GITHUB_STEP_SUMMARY` and upload a last-N-lines artifact before teardown.
   - **Expected reliability impact:** reduces blind reruns and speeds root-cause identification; it does not directly lower failure rate, but it lowers rerun rate.
   - **Rollback / fail-open:** if upload fails, keep the job result unchanged.

4. **Treat sampled Semble fallbacks as healthy fail-open test noise, not a broken production rollout**
   - **Failure evidence:** direct fallback lines in CI/test contexts were all `SEMBLE_FALLBACK target=overflow ... reason=...missing_semble ... ms=0` in runs **26956143718**, **26968085383**, **26991372201**, and **26989483268**. Parsed family totals show `ci=70`, `test_and_mark_stable=5`, `review_autofix=0`; `workflow_log_analysis=33` should be treated as suspect because that run embeds self-generated `SEMBLE_*` text.
   - **Root cause category:** fail-open dependency absence in tests plus telemetry contamination.
   - **Exact fix:** keep fail-open behavior; mark fixture/self-analysis fallbacks separately from production Semble telemetry and parse only exact top-level telemetry prefixes.
   - **Expected reliability impact:** reduces false “Semble rollout is broken” conclusions while preserving healthy fail-open behavior.
   - **Rollback / fail-open:** no behavior change to runtime paths.

5. **Treat Serena as disabled, not failed**
   - **Failure evidence:** review runs **26952637519**, **26960113331**, **26968187150**, **26977120613**, and **26989803986** all show `SERENA_ENABLED: false` and `SERENA_AVAILABLE: false`. Aggregate summary has `probe_skipped=1`, `probe_failed=0`.
   - **Root cause category:** disabled rollout / incomplete telemetry, not runtime outage.
   - **Exact fix:** surface “disabled” explicitly in summaries and only alert on real probe failures, not skips.
   - **Expected reliability impact:** lowers false-positive reliability alarms.
   - **Rollback / fail-open:** none needed.

6. **Short-circuit terminal `review_codex-agent` states**
   - **Failure evidence:** runs **26968187150** and **26977120613** both entered terminal editor states hours before cancellation.
   - **Root cause category:** stuck terminal state / missing exit path.
   - **Exact fix:** exit cleanly after PR-closed abort or idle-kill after persisting diagnostics.
   - **Expected reliability impact:** fewer orphaned cancellations and less runner waste; cleaner state for downstream pollers.
   - **Rollback / fail-open:** emit final note and return neutral/cancelled status instead of hard-failing if needed.

## AI Memory Health

- I found exact top-level `AI_MEMORY_TELEMETRY` entries in **6** unique deep-dive runs: `workflow_log_analysis` **26989503339** and `review_autofix` **26952637519**, **26960113331**, **26968187150**, **26977120613**, **26989803986**.
- **Retrieve hit rate:** **0/5 = 0%**. Every sampled review retrieve logged `records_selected=0`.
- **Average `estimated_tokens`:** **0**. The logs did **not** emit a retrieval budget field, so estimated-tokens-vs-budget cannot be computed for this window.
- **`keyword_method` distribution:** **llm=5**, `plain=0`, `none=0`.
- **Fail-open / disabled:** `fail_open=true` was **not** present in sampled retrieve events; `enabled=false` was **not** present either.
- **Push reliability:** no sampled memory write had `push_attempts > 1`; sampled `record-run-event`/`record-candidate` writes all pushed on attempt 1.
- **Other ops observed:** one `record-candidate` in run **26989803986**; one `summarize_unselected_runs` in run **26989503339** with `targeted=100`, `summarized=88`, `tokens_used=131338`.

**Recommendation:** reviewer-role memory retrieval is currently all miss / zero-budget. The smallest safe fix is to short-circuit retrieval when the candidate pool is empty or `estimated_tokens=0`, and add one extra telemetry field for candidate-pool size so you can distinguish “no relevant memory exists” from “retrieval logic failed to surface it.”

## GH API Call Audit

- **Evidence gap:** the collected summary did **not** include explicit GH API call counters or rate-limit totals, so the audit below is derived from log behavior plus repository code. I did **not** observe any rate-limit or secondary-rate-limit events in the sampled logs.

1. **Highest avoidable call pattern: Phase-4 polling in `test_and_mark_stable`**
   - **Evidence:** run **26989483268** used `POLL_INTERVAL=10` and `REVIEW_TIMEOUT=30`; it re-checked the same cancelled review run until timeout.
   - **Pattern:** repeated lookup of a terminal cancelled run after state should have converged.
   - **Concrete change:** fix run selection first; it should remove most repeated GH Actions status/list calls in that loop.
   - **Estimated call reduction:** **>180** poll iterations per affected run.
   - **Rate-limit risk reduction:** **high** for this path.

2. **Positive audit: `issue_pr_status` already follows the repo’s batching rule**
   - **Evidence:** `.github/workflows/issue_pr_status.yml` explicitly says `Single batched GraphQL call (one API request regardless of N) per CLAUDE.md §15` at the orchestrator classification block; recent runs **26993875889** and **26993925214** completed in **10s** and **13s**.
   - **Pattern:** good batching; GraphQL first, REST only as fail-open fallback.
   - **Concrete change:** preserve this pattern for any future linked-issue classification work.
   - **Estimated call reduction:** already realized.
   - **Rate-limit risk reduction:** **high** vs per-issue REST loops.

3. **Latent hotspot: repeated paginated comment fetches in `orchestrate_poll_process.sh`**
   - **Evidence:** code inspection shows per-tracking-issue paginated comments fetch in `run_standalone_stall_recovery` (`scripts/orchestrate_poll_process.sh`, lines **10798–10815**) and multiple other `issues/.../comments?per_page=100` fetch sites in the same script.
   - **Pattern:** unbatched/repeated REST calls inside loops.
   - **Concrete change:** fetch tracking-issue comments once per poll cycle, cache them in `RUNTIME_DIR`, and reuse parsed orchestrator state across recovery helpers.
   - **Estimated call reduction:** **O(T)** paginated comment calls per cycle, where `T` = tracking issues.
   - **Rate-limit risk reduction:** **medium**, especially as orchestrator-managed issue count grows.
   - **Inference:** this is a code-level hotspot; it was not directly visible as a current rate-limit failure in sampled runs.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is not actionable yet.** Repo-wide, `cache_hit_rate=null`, all `or_*` token/cache counters are **0**, and `context_budget_warn_count=0` across **114** telemetry-covered runs.
- **Do not confuse infra caches with prompt caches.** Run **26989803986** logged `Cache hit occurred on key setup-uv... not saving cache.`; run **26989503339** logged `Cache hit occurred on the primary key codex-v0.114.0-v2, not saving cache.` These are dependency/tool caches, not prompt-cache hits.
- **No proven prompt-budget pressure in this window.** Review logs frequently print `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but actual parsed `CONTEXT_BUDGET_WARN` event count is **0**.
- **Memory retrieval is miss-only in sampled review runs.** That weakens any claim that prompt/context assembly is benefiting from historical memory.

**Concrete improvements**
1. Emit real prompt-cache telemetry per phase (`cache_hit_rate`, prompt tokens, cache read/write tokens, prompt fingerprint/hash).
2. Keep stable prompt prefixes stable and move highly dynamic fields (run IDs, timestamps, tail summaries) to the end of prompts. **Inference:** this is the most likely cache-fragmentation fix, but current telemetry cannot prove fragmentation directly.
3. Separate infrastructure-cache logging from prompt-cache telemetry in analysis output so `not saving cache` lines do not get misread.

**Estimated impact**
- **Tokens/latency:** not quantifiable yet because prompt-cache counters are absent.
- **Reliability:** high observability gain with low implementation risk.

## Orchestrator Health

- **Front-half orchestration looks healthy.** `clarify` had **191** runs with **0** failures and p95 **6s**; `plan` had **183** runs with **0** failures and p95 **8.9s**; `implement` had **183** runs with **1** failure, but that failure is obscured by a missing log archive. In smoke-test run **26989483268**, clarify, plan, and implement all passed before review failed.
- **The weak point is the poller layer, not wave progression.** The worst failures are all “wait for other workflow/state” paths: Phase-4 review wait, internal review-gate cancellation loops, and repeated status checks on cancelled runs.
- **Issue close / lineage control plane is mostly behaving.** Recent `issue_pr_status` runs **26993875889** and **26993925214** cleanly logged `No linked issues found; skipping lineage finalization.`; the workflow also explicitly protects orchestrator-tracking issues from PR-close mutation.
- **Conflict-heal / deferral pain points were not directly observed** in the sampled logs.
- **Observable indicators to track next:**
  - count of runs logging `Review run was cancelled — checking for newer run...`
  - p95/p99 of `review_codex-agent`
  - count of missing log archives by workflow family
  - `orchestrate_poll` duration p95 (currently **350.15s**)
  - AI memory retrieve hit rate (currently **0%** in sampled review runs)

## Pipeline Flow Bottlenecks

| Stage | What the evidence says | Bottleneck type | Highest-impact fix |
|---|---|---|---|
| clarify → plan | Generally healthy; p95s are **6s** and **8.9s** | minimal compute | none; keep as-is |
| implement | Usually healthy/short; one missing-log failure prevents root-cause analysis | observability | preserve failure artifacts |
| review/autofix | Dominant end-to-end bottleneck: family p95 **4147.6s**; sampled `review_codex-agent` spans **920s** to **14,433s**; small diffs still take ~**20 min** | compute + stuck terminal state + poll overhead | small-diff fast path + terminal-abort short-circuit |
| review gate / release smoke wait | Proven 30-minute stall on cancelled sibling in run **26989483268** | retry/poll overhead | non-cancelled run preference at pinned SHA |
| CI / merge readiness | `ci` p50 **1462.5s**; slow `lint` **1655.8s**; repeated deterministic assertion failures | compute + deterministic regression | split CI + fix per-attempt prompt-file bug |
| validate / post-review checks | `validate` failure rate **90%**, but **8/9** failures are missing-archive 404s | observability / evidence gap | emit job summary + upload tail artifact before teardown |
| tiny post-merge/status workflows | `issue_pr_status` **10–13s**, `forward_merge` **18s**, `review` sweep **7s**, all with runner wait | queueing/fan-out | tighter top-level gating so no-op workflows do not launch |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  1. `review_autofix` long tail (`p95=4147.6s`; sampled `review_codex-agent` up to **14,433.6s`)
  2. `ci` `lint` critical path (`p50=1462.5s`, slow step **1655.8s`)
  3. queueing overhead from small post-merge/status workflows

- **Top failure modes**
  1. Phase-4 review poller selecting a cancelled sibling and timing out (**26989483268**)
  2. Repeated CI prompt-file contract regression (**26956143718**, **26968085383**, **26991372201**)
  3. Missing validate/implement log archives blocking root-cause analysis

- **Highest-cost drivers**
  1. `workflow_log_analysis` deep audit + broad run summarization (**26989503339**)
  2. `review_autofix` long-running `review_codex-agent` on even tiny diffs
  3. avoidable reruns/timeouts around cancelled review siblings

- **Top 3 prioritized actions**
  1. Fix Phase-4 / pinned-SHA non-cancelled run selection.
  2. Add `review_autofix` small-diff fast path and immediate exit on PR-closed / idle-killed editor states.
  3. Split CI `lint` into parallel shards and fix the per-attempt prompt-file regression.

## Metrics Appendix

### Repo summary

| Repo | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) | Runs with log telemetry |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 250 | 16 | 17 | 717 | 175.9 | 1.0 | 1177.5 | 114 |

### Workflow-family hotspots

| Workflow family | Runs | Success | Failure | Cancelled | p50 (s) | p95 (s) | Key takeaway |
|---|---:|---:|---:|---:|---:|---:|---|
| review_autofix | 94 | 76 | 0 | 15 | 63.5 | 4147.6 | Main latency tail; cancellations matter |
| ci | 30 | 26 | 4 | 0 | 1462.5 | 1555.9 | `lint` dominates and has repeated deterministic failures |
| validate | 10 | 1 | 9 | 0 | 0.0 | 81.9 | Severe failure rate, but evidence gap due missing archives |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 4248.0 | 4248.0 | Failed on review wait loop |
| workflow_log_analysis | 1 | 0 | 1 | 0 | 4170.0 | 4170.0 | Expensive analysis workflow failed contract |
| issue_pr_status | 11 | 11 | 0 | 0 | 13.0 | 62.0 | Fast, but queueing-visible |
| orchestrate_poll | 18 | 18 | 0 | 0 | 171.5 | 350.1 | Healthy, but moderate poll-loop cost |

### Token / cache / latency telemetry

| Scope | Codex tokens | Codex calls | OR total tokens | OR cache write | OR cache read | cache_hit_rate | wall_clock p50 ms | wall_clock p99 ms | break_glass_count | context_budget_warn_count |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| Repo total | 8104 | 4 | 0 | 0 | 0 | null | 2000 | 15081790 | 0 | 0 |
| review_autofix | 4052 | 2 | 0 | 0 | 0 | null | 1174500 | 19687490 | 0 | 0 |
| ci | 0 | 0 | 0 | 0 | 0 | null | 1224000 | 1654210 | 0 | 0 |
| workflow_log_analysis | 4052 | 2 | 0 | 0 | 0 | null | 4170000 | 4170000 | 0 | 0 |
| test_and_mark_stable | 0 | 0 | 0 | 0 | 0 | null | 4248000 | 4248000 | 0 | 0 |

### Semble / Serena telemetry

| Scope | Semble queries | Semble bytes | Avg bytes/query | Semble fallbacks | Serena queries | Serena response bytes | Serena fallbacks | Serena probe ok / failed / skipped |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Repo total | 43 | 455580 | 10595 | 108 | 1 | 0 | 1 | 0 / 0 / 1 |
| review_autofix | 23 | 258156 | 11224 | 0 | 0 | 0 | 0 | 0 / 0 / 0 |
| ci | 0 | 0 | — | 70 | 0 | 0 | 0 | 0 / 0 / 0 |
| workflow_log_analysis | 20 | 197424 | 9871 | 33 | 1 | 0 | 1 | 0 / 0 / 1 |
| test_and_mark_stable | 0 | 0 | — | 5 | 0 | 0 | 0 | 0 / 0 / 0 |

**Important note:** the `workflow_log_analysis` run embeds self-generated `SEMBLE_*`/`SERENA_*` text, so the parsed repo/family fallback totals should be treated as **upper bounds**, not clean operational counts.

### AI memory metrics

| Metric | Value |
|---|---|
| Unique deep-dive runs with exact `AI_MEMORY_TELEMETRY` | 6 |
| Sampled `retrieve` ops | 5 |
| `retrieve` hit rate | 0 / 5 = 0% |
| Avg `estimated_tokens` on `retrieve` | 0 |
| `keyword_method` distribution | `llm`: 5, `plain`: 0, `none`: 0 |
| `retrieve fail_open=true` | 0 |
| `retrieve enabled=false` | 0 |
| Writes with `push_attempts > 1` | 0 |
| Largest explicit memory-side token sink | run 26989503339 `summarize_unselected_runs`: 131,338 tokens |

### GH API call summaries

| Workflow / step | Evidence | Estimated avoidable GH API calls per run | Notes |
|---|---|---:|---|
| `test_and_mark_stable` / `e2e-smoke-test` / Phase 4 wait | run 26989483268 polled every 10s for 30m on same cancelled run | >180 | biggest proven avoidable polling hotspot |
| `issue_pr_status` / linked-issue classification | batched GraphQL block in `.github/workflows/issue_pr_status.yml` | 0 | already aligned to repo batching rule |
| `orchestrate_poll_process.sh` / standalone stall recovery | per-tracking-issue paginated comments fetch in loop | O(T) paginated calls/cycle | inference from code audit; no rate-limit failure observed |

### Per-target MCP availability

| MCP server | Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---:|---:|---:|---|
| Serena | target not exposed in collector window | 0 | 0 | 1 | aggregate-only; sampled review logs show `SERENA_ENABLED: false` / `SERENA_AVAILABLE: false` |
| Other MCP servers observed | none exposed | 0 | 0 | 0 | no target-level `other_mcp` data in `workflow_log_report.json` for this window |
