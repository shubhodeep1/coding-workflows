## Executive Summary

- **Highest-impact fix: add a trivial-task fast path for `implement` and bail after the first confirmed no-op on fully specified canary tasks.** In failed `implement` runs `25224008847` and `25224028373`, Codex spent two attempts on a one-file smoke task and ended with `Codex produced no actionable output 2 attempts in a row`; downstream `test_and_mark_stable` then failed because no PR was created. **Estimated impact:** cut 4-15 minutes from stable-release test failures and eliminate a recurring rerun class; meaningful token savings per failed smoke task. **Confidence:** high.

- **`test_and_mark_stable` is the dominant end-to-end latency and reliability problem.** The family has **5 runs, 4 failures, 0 successes, avg 4,324s, p50 4,912s, p95 5,486s**. Runs `25215477856` and `25223836137` show repeated queued/in-progress polling and long waits on downstream workflows, including an alt-model path that timed out before review. **Estimated impact:** 15-30 minutes saved on bad runs and large failure-rate reduction if polling and downstream dependency conditions are tightened. **Confidence:** high.

- **`review_autofix` is the largest avoidable AI-cost sink on slow successful paths.** Slow run `25215784558` took **3,032s**, ran **6 reviewer models**, kept `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`, warmed Serena, and finished with `DID_COMMIT: false` / `EDITOR_NOOP_SUSPICIOUS: true`. **Estimated impact:** 30-60% token/cost reduction on small PRs and 8-20 minutes saved on long review runs by tiering reviewer fan-out and skipping editor/no-op paths earlier. **Confidence:** medium-high.

- **`workflow_log_analysis` mostly works, but its failure mode is a Git conflict after successful analysis.** Failed run `25208727402` completed analysis, spent **203,782 tokens**, then failed on `git pull --rebase` with `CONFLICT (add/add)` while pushing the generated report. **Estimated impact:** near-elimination of this workflow’s sampled failure mode with low implementation risk. **Confidence:** high.

- **Short workflows are paying disproportionate checkout/setup tax.** Recent `orchestrate_poll` run `25236665847` had `has_work:false`, but still did a full-history checkout (`fetch-depth: 0`) and spent ~8.4s in checkout inside a 40s run. Similar short workflows also show hosted-runner wait as a large share of total time. **Estimated impact:** 5-10s saved per no-work poll and lower Git/API churn across frequent runs. **Confidence:** high.

- **AI memory telemetry is present but unevenly surfaced in sampled runs.** Structured memory ops were observed in deep-dive logs, including one `retrieve` with `records_selected: 2`, `estimated_tokens: 56`, `keyword_method: plain`, and multiple `record-run-event` writes with `push_attempts: 1`; many recent run summaries also explicitly noted memory telemetry as absent. **Estimated impact:** better observability and safer tuning rather than direct runtime savings. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Critical path: add a smoke/canary fast path in `implement`**
   - **Evidence:** Failed `implement` runs `25224008847` and `25224028373` were trivial one-file tasks (`tests/e2e_smoke_canary.txt`) yet still performed full setup, Serena staging, memory operations, and two Codex attempts before bailing. `test_and_mark_stable` summaries tied smoke failures to “all implement runs completed but no PR was created.”
   - **Root cause:** The production `implement` workflow treats tiny deterministic overwrite tasks like normal open-ended coding tasks.
   - **Exact change:** Detect “single-file, fully specified canary/smoke” tasks and route them to a low-reasoning, single-attempt editor path with a strict changed-files contract; if the first pass returns empty/no-op, fail immediately with diagnostics instead of retrying.
   - **Estimated time savings:** **4-15 minutes** on failing stable-test runs; **1-3 minutes** on each failed implement branch of the chain.
   - **Implementation risk:** **Low-medium.** Safe if scoped only to explicit smoke-test task patterns.

2. **Critical path: collapse `test_and_mark_stable` watch loops and stop waiting on impossible success conditions**
   - **Evidence:** `test_and_mark_stable` has **avg 4,324s** and **0/5 successes**. Run `25223836137` polled through clarify → plan → implement and then stayed in repeated `ai:implementing` waits for over an hour before `Alt-model run timed out before reaching review stage`. Run `25215477856` repeatedly observed downstream runs in `queued` state and invoked the soft-error analyzer after multiple phases.
   - **Root cause:** The workflow serially dispatches and polls several downstream workflows with conservative wait windows and weak early-exit logic.
   - **Exact change:** Use phase-specific maximum wait budgets, progressive backoff after the first few polls, and immediate abort when the prerequisite success condition becomes impossible (for example, implement finished without creating a PR). Run soft-error analysis only on failed/timeout branches, not after every successful intermediate phase.
   - **Estimated time savings:** **15-30 minutes** on failed stable runs; **5-10 minutes** on future successful runs if the chain remains unchanged.
   - **Implementation risk:** **Low.** Mostly watcher logic and guardrails.

3. **Critical path: fix alt-model propagation into downstream implement/review jobs**
   - **Evidence:** Failed run `25223836137` reached clarify and plan, then timed out waiting for alt-model implement/review progression. The workflow never reached review, and repo-level logs repeatedly reference production implement behavior instead of an actual differentiated alt-model path.
   - **Root cause:** The alt-model override likely is not consistently propagated into the dispatched downstream workflows or their environment.
   - **Exact change:** Make alt-model selection explicit in dispatch inputs and echo resolved model values at the start of clarify, plan, implement, and review jobs; fail fast if the expected override is absent.
   - **Estimated time savings:** **Up to the full 79-minute timeout path** for misconfigured alt-model test runs.
   - **Implementation risk:** **Medium.** Requires tightening interface contracts across reusable workflows.

4. **Local but high-frequency: remove full-history checkout from no-work `orchestrate_poll` cycles**
   - **Evidence:** Recent successful `orchestrate_poll` run `25236665847` had `has_work:false`, yet `actions/checkout` used `fetch-depth: 0`; checkout alone took about **8.4s** of a **40s** run and enumerated many branches.
   - **Root cause:** Poll logic pays full repo checkout cost even when the cycle only needs API state and ledger writes.
   - **Exact change:** Skip checkout entirely when `has_work:false`, or use `fetch-depth: 1` only when a local file is actually needed.
   - **Estimated time savings:** **5-10s per no-work poll run**.
   - **Implementation risk:** **Low.**

5. **Critical path on slow reviews: tier reviewer fan-out and no-op editor execution in `review_autofix`**
   - **Evidence:** Slow run `25215784558` took **3,032s**, used **6 reviewer models**, and ended with `DID_COMMIT: false` and `EDITOR_NOOP_SUSPICIOUS: true`. Repo metrics show `review_autofix` p95 at **~1,985s** with **22 cancelled** runs out of 63.
   - **Root cause:** Small or low-risk PRs still traverse an expensive many-reviewer + editor path.
   - **Exact change:** For one-file or tiny diffs, cap reviewer fan-out at 2-3 models; if reviewer consensus is “no change” or editor audit is empty/suspicious, stop before the editor/judge/merge stages.
   - **Estimated time savings:** **8-20 minutes** on long review runs; smaller but frequent savings on routine PRs.
   - **Implementation risk:** **Medium.** Needs careful quality thresholds.

6. **Local micro-optimization: avoid unnecessary setup-uv / Serena bootstrap on empty or trivial workdirs**
   - **Evidence:** `implement` run `25224008847` logged `Empty workdir detected` and `No file matched ... cache will never get invalidated`, yet still restored the `setup-uv` cache and staged Serena support.
   - **Root cause:** Tool bootstrap runs before confirming the task actually needs those tools.
   - **Exact change:** Gate UV/Serena setup behind task complexity or required-tool detection; skip cache save/restore when the dependency glob is empty.
   - **Estimated time savings:** **10-30s per trivial implement/review run**.
   - **Implementation risk:** **Low.**

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Lower model effort and retry budget for trivial smoke/canary implement tasks**
   - **Evidence:** Failed `implement` summaries for the same failure mode reported attempt token counts ranging from about **4.3k-4.5k per attempt** in one sample and **88k-89k per attempt** in another diagnostic summary, despite the task being a one-file canary edit. Run `25224008847` then bailed after two no-actionable-output attempts.
   - **Root cause:** High-capability/higher-context implementation settings are being used for deterministic toy tasks.
   - **Exact change:** Force a cheaper editor profile and one-attempt limit for canary/smoke tasks; if no file delta is produced, stop and surface a contract error instead of retrying.
   - **Estimated savings:** **~9k to >170k tokens per failed smoke task**, depending on the path sampled, plus avoided downstream reruns.
   - **Quality-risk notes:** Low if restricted to explicit smoke-test templates.

2. **Reduce `review_autofix` reviewer fan-out for tiny PRs**
   - **Evidence:** Slow run `25215784558` had `REVIEWERS_SUCCESSFUL: 6`, with six external reviewer models plus `MODEL_EDITOR: openai/gpt-5.3-codex`, yet no commit was produced.
   - **Root cause:** Reviewer breadth is fixed too high relative to PR size/risk.
   - **Exact change:** Introduce size-based review tiers: tiny/single-file PRs use 2-3 reviewers; larger risky diffs retain broader fan-out.
   - **Estimated savings:** **30-60% of reviewer token spend** on small PRs; meaningful wall-clock reduction too.
   - **Quality-risk notes:** Medium; offset by keeping full fan-out for risky diffs.

3. **Add a cheaper “smoke mode” for `workflow_log_analysis` when invoked by stable-release canaries**
   - **Evidence:** Failed `workflow_log_analysis` run `25208727402` consumed **203,782 tokens** before failing only at the Git push step. `test_and_mark_stable` orphan-workflow testing references large budgets and long timeout allowances for this analysis chain.
   - **Root cause:** Stable smoke/orphan validation is using the same deep analysis profile as full optimization reporting.
   - **Exact change:** When the workflow is dispatched from smoke/orphan validation, narrow the collection window and disable the deepest optional passes unless prior signals indicate regressions.
   - **Estimated savings:** **100k-200k tokens per smoke-triggered analysis run**.
   - **Quality-risk notes:** Low if full-depth mode remains the default for scheduled/manual analysis.

4. **Avoid avoidable reruns by failing fast when implement cannot create a PR**
   - **Evidence:** Stable test failures were not due to infra; they were due to implement finishing without creating a PR and downstream watchers continuing. This multiplies token usage across clarify, plan, implement, watchers, and review waits.
   - **Root cause:** Orchestrator/watch logic keeps spending on downstream phases after the real failure has already occurred.
   - **Exact change:** Emit a machine-readable “PR not created” terminal state and stop the chain immediately.
   - **Estimated savings:** **Entire downstream workflow cost** on these failure paths.
   - **Quality-risk notes:** Low.

5. **Improve prompt-cache observability before further prompt tuning**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` was repeatedly observed in `implement`, `review_autofix`, `orchestrate_poll`, and `workflow_log_analysis`, but no sampled logs showed prompt-cache create/read counters.
   - **Root cause:** Cache is enabled, but hit/miss behavior is not exposed in the sampled telemetry.
   - **Exact change:** Emit cache create/read/hit/miss counters per AI step and stabilize prompt prefixes by moving run-specific noise below the shared instruction prefix.
   - **Estimated savings:** Unquantified now; likely moderate across repetitive workflows.
   - **Quality-risk notes:** Very low; this is instrumentation-first.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Harden the empty-output bail path and align tests with production diagnostics**
   - **Failure evidence:** Failed CI run `25210565611` hit `Implement post-Codex recovery unit tests` with `34 passed, 2 failed, 36 total`; the failing tests were `test_codex_empty_output_streak_bail_and_flag` and `test_failure_diagnostics_posted_to_source_issue`. The same production behavior caused `implement` failures `25224008847` and `25224028373`.
   - **Root cause category:** Workflow/logic contract mismatch.
   - **Exact fix:** Centralize the empty-output bail message and diagnostic reason in one shared helper used by both runtime and tests; ensure the issue comment distinguishes “empty-streak bail” from “attempt budget exhausted.”
   - **Expected reliability impact:** Should remove a repeat CI failure cluster and make implement failures more diagnosable and less sticky.
   - **Rollback/fail-open:** Safe rollback to current helper if message parsing breaks; fail-open remains diagnostic-only.

2. **Make alt-model configuration verifiable and terminally fail if missing**
   - **Failure evidence:** `test_and_mark_stable` run `25223836137` failed at `Wait for clarify→plan→implement (alt-model)` after long repeated waits and never reached review.
   - **Root cause category:** Cross-workflow configuration propagation.
   - **Exact fix:** Require downstream jobs to print the resolved model and compare it to the requested override; if mismatch, fail immediately with a configuration error.
   - **Expected reliability impact:** Large reduction in alt-model false negatives and wasted timeout runs.
   - **Rollback/fail-open:** Fail closed for the test-only alt-model path; keep production default path unchanged.

3. **Make `workflow_log_analysis` report publishing conflict-safe**
   - **Failure evidence:** Run `25208727402` failed after successful analysis on `git pull --rebase origin main` with `CONFLICT (add/add)` in `analysis/workflow-optimization-2026-05-01-4.md`.
   - **Root cause category:** Concurrent write/merge conflict.
   - **Exact fix:** Include run ID in the report filename or write to a per-run scratch path then atomically update an index; alternatively, on conflict, publish artifact-only and mark the step warning/non-fatal.
   - **Expected reliability impact:** Near-eliminate the sampled failure mode for this family.
   - **Rollback/fail-open:** Prefer artifact upload + warning as the fail-open path.

4. **Add terminal-state detection to stable smoke watchers**
   - **Failure evidence:** In run `25215477856`, the workflow continued through clarify/plan/implement/review watches and repeated soft-error analysis even after the true blocking condition was known.
   - **Root cause category:** Orchestrator/watcher state machine.
   - **Exact fix:** When implement concludes without PR creation, stop the watcher chain immediately and annotate the source issue once.
   - **Expected reliability impact:** Lower false timeouts, fewer secondary failures, fewer operator-visible noisy artifacts.
   - **Rollback/fail-open:** Low-risk; watchers can revert to current behavior if terminal-state parsing fails.

5. **Reduce support-ref fallback churn in PR status / validate dispatch**
   - **Failure evidence:** Recent `issue_pr_status` run `25237276167` warned `Support checkout ref ... unavailable; using main.` Recent `review_autofix` run `25237276160` warned `No standalone validation workflow could be dispatched for merged PR #1913.`
   - **Root cause category:** Fallback-path ambiguity.
   - **Exact fix:** Resolve support ref once per job, store it, and short-circuit repeated fallback probes; for validate dispatch, resolve the canonical workflow name once rather than trying multiple dispatch commands late.
   - **Expected reliability impact:** Moderate reduction in spurious warnings and flaky post-merge behavior.
   - **Rollback/fail-open:** Keep current fallback behavior if resolution fails.

## AI Memory Health

Structured `AI_MEMORY_TELEMETRY:` lines were observed in sampled deep-dive logs, so memory telemetry is present in this window.

- **Observed operations:** `processed-command-check`, `processed-command-claim`, `record-run-event`, `retrieve`, and `summarize_unselected_runs`.
- **Retrieve hit rate:** **100% in the sampled deep-dive retrieve set (1/1 retrieves returned records)**.
  - Sampled retrieve: failed `implement` run `25224008847` logged `{"op":"retrieve","records_selected":2,"estimated_tokens":56,"keyword_method":"plain","role":"implementation"}`.
- **Average `estimated_tokens` vs budget:** Only one sampled retrieve exposed `estimated_tokens`, so the observable average is **56 tokens**; no budget field was present in that sample, so budget utilization cannot be computed from the sampled deep dives.
- **`keyword_method` distribution:** **plain: 100%** of sampled deep-dive retrieves; **llm: 0%, none: 0%** in the sample.
- **Flags checked:**
  - **0 sampled retrieves returned 0 records**
  - **No sampled `fail_open: true` entries**
  - **No sampled `enabled: false` entries**
  - **No sampled push retry counts > 1**; observed `record-run-event` writes used `push_attempts: 1`
- **Other observed telemetry:**
  - `implement` run `25224008847`: `processed-command-check`, `processed-command-claim`, `record-run-event` for `phase_started` and `phase_failed`
  - `orchestrate_poll` sampled run: `record-run-event` for `poll_started` / `poll_completed`
  - `workflow_log_analysis` deep-dive run `25208727402`: `summarize_unselected_runs`
- **Gap:** Many recent run summaries explicitly reported memory telemetry as absent. That suggests telemetry coverage is inconsistent across workflows or sampled excerpts.

**Recommendation:** Keep the current structured memory telemetry format, but require every AI-bearing workflow to emit at least one `retrieve`/`record-run-event` line and export a simple per-run memory summary (retrieves, hits, zero-hit retrieves, fail-open count). That is the smallest change that makes future optimization defensible.

## GH API Call Audit

The sample shows good retry/fail-open discipline in places, but still several high-redundancy patterns.

1. **`test_and_mark_stable` watcher loops are the biggest GH API hotspot**
   - **Evidence:** Run `25215477856` repeatedly logged downstream run states like `status=queued conclusion=` across `workflow-log-analysis`, implement, review, and other phases, plus repeated soft-error analyzer invocations and artifact uploads.
   - **Redundancy pattern:** Polling the same workflow families at short intervals through long waits.
   - **Concrete change:** Fetch all downstream run state in one cycle-local query result, cache it for the poll iteration, and use progressive backoff once a run has been queued/in-progress for several minutes.
   - **Estimated reduction:** **Tens to hundreds of API calls per stable-test run**, plus lower secondary rate-limit risk.
   - **Rule cross-check:** This repo already uses fail-open helpers; extend that hygiene to cycle-local caching and batched status reads.

2. **`issue_pr_status` does GraphQL discovery plus fallback lookups that can be consolidated**
   - **Evidence:** Recent run `25237276167` used `gh api graphql` to resolve linked issues, then separately queried orchestrator issue context and fell back when support refs were unavailable.
   - **Redundancy pattern:** Multiple API lookups for PR-linked issue context in one short job.
   - **Concrete change:** Fetch PR number, linked issues, and needed labels/state in one GraphQL document and pass the parsed JSON between steps instead of re-querying.
   - **Estimated reduction:** **2-5 API calls per PR-close/status-sync run**.
   - **Rule cross-check:** Aligns with mandatory batching and cycle-local cache expectations.

3. **`orchestrate_poll` appears to call `/rate_limit` preemptively**
   - **Evidence:** Recent run `25236665847` summary explicitly mentioned `_gh_retry` and `/rate_limit`; the job had `has_work:false`.
   - **Redundancy pattern:** Rate-limit introspection on a no-work fast path.
   - **Concrete change:** Only query `/rate_limit` on actual retry/error paths, or cache one result for the whole job.
   - **Estimated reduction:** **1-2 GH API calls per poll cycle**, which matters because the workflow runs frequently.
   - **Rule cross-check:** Keep fail-open behavior, but avoid paying for protection when nothing is failing.

4. **`copilot_pull_request_reviewer` cleans artifacts with a list-then-delete loop**
   - **Evidence:** Recent run `25236338702` listed `/actions/runs/25236338702/artifacts`, extracted `.artifacts[].id`, then deleted each artifact in a loop.
   - **Redundancy pattern:** Per-artifact delete calls.
   - **Concrete change:** Reduce artifact fan-out so cleanup has fewer artifacts to enumerate/delete; if only one artifact is needed, publish one bundle instead of several step-specific artifacts.
   - **Estimated reduction:** **N-1 delete calls** where N artifacts are currently emitted.
   - **Rule cross-check:** GitHub’s delete API is per artifact, so the win comes from fewer artifacts, not REST batching.

5. **Post-merge validate dispatch repeats workflow lookup/fallback logic**
   - **Evidence:** Recent `review_autofix` run `25237276160` used `gh api graphql` for linked issues and then tried dispatch fallback paths before warning that no standalone validation workflow could be dispatched.
   - **Redundancy pattern:** Late discovery of workflow availability.
   - **Concrete change:** Resolve the dispatch target once, cache it, and skip dispatch attempts entirely when linked issues do not carry `ai:orchestrator-validate-required`.
   - **Estimated reduction:** Small per run, but reduces noisy failed dispatches and warning volume.

## MCP & Serena Efficiency

- **Observed state:** Serena is being set up, but the sampled logs do not expose detailed tool-call traces (symbol lookups, broad reads, repeated file-region reads), so precise churn quantification is not possible from this window.
- **Evidence of overhead:**
  - Slow `review_autofix` run `25215784558` logged `Warming Serena uvx cache`, `Creating .serena/project.yml`, and `Git MCP setup skipped (GIT_MCP_DISABLED=true)`.
  - Failed trivial `implement` run `25224008847` staged Serena-related support and resolved a Serena binary even though the task was a one-file canary edit.
- **Likely inefficiency:** The pipeline is paying Serena bootstrap cost even on tasks that should not need repo-wide semantic exploration.
- **Concrete recommendations:**
  1. **Guard Serena startup by task complexity.** Skip Serena bootstrap for single-file deterministic smoke tasks and for comment-only review paths.
  2. **Enable targeted Git MCP/diff access where available.** `Git MCP setup skipped` means review/edit flows are likely falling back to broader Git or shell reads than necessary.
  3. **Parallelize safe metadata/setup reads.** Support-file resolution, PR metadata fetch, and memory retrieval are independent early reads that can run in parallel before the editor path begins.
  4. **Keep Serena mandatory only where symbol-level correctness matters.** For stable smoke canaries, the benefit is low and the startup tax is visible.
- **Expected impact:** Lower startup latency and token overhead in `implement`/`review_autofix`; better correctness-per-token on real code changes once targeted Git/diff fetch is enabled.
- **Data gap:** No sampled Serena tool transcript exposed repeated file reads or symbol churn, so read-amplification claims should be re-measured after enabling richer Serena telemetry.

## Prompt Cache & Memory System

- **Prompt cache status:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` was observed in `implement`, `review_autofix`, `orchestrate_poll`, and `workflow_log_analysis`, so prompt-cache behavior is intended to be on.
- **What is missing:** The sample did **not** include cache create/read/hit counters, so actual hit rate and miss cost cannot be quantified.
- **Memory retrieval effectiveness:** In the sampled deep dive, memory retrieval did work at least once (`implement` run `25224008847`: 2 records selected, 56 estimated tokens), and sampled ledger writes were healthy (`push_attempts: 1`).
- **Cache-fragmentation risk:** The workflows print long dynamic environment/contract blocks and run-specific context. That pattern increases the risk that semantically identical tasks get different prompt prefixes unless the volatile parts are consistently placed after the stable instruction prefix.
- **Concrete improvements:**
  1. **Emit prompt-cache counters per AI step**: created, read, hit, miss, and effective cached tokens.
  2. **Freeze the stable prompt prefix**: keep system + repo contract + tool policy in a constant order; move run IDs, timestamps, issue bodies, and ephemeral diagnostics below that prefix.
  3. **Do not save/restore UV cache when dependency globs are empty**: `implement` logs showed `no-dependency-glob` cache use on empty workdirs. This is infra cache, not prompt cache, but it is still avoidable cache churn.
  4. **Surface memory-summary counters at the end of each run**: retrieves, hits, zero-hit retrieves, fail-open count.
- **Estimated impact:** Prompt-side impact is currently **unquantified** due to missing counters; infra-cache savings are small per run but frequent.

## Orchestrator Health

- **Healthy signals:**
  - Core orchestration families themselves are mostly successful: `orchestrate` 5/5 success, `validate` 5/5 success, `orchestrate_poll` 30/30 success.
  - Many clarify/plan/implement runs are intentionally skipped quickly (repo p50 is **1s**), which keeps idle overhead low.
- **Pain points:**
  1. **Stuck `ai:implementing` loops:** Alt-model stable test run `25223836137` sat in repeated implement waits until timeout.
  2. **Clarify→plan→implement success does not guarantee PR creation:** Smoke-test run `25215477856` still failed after downstream implement completed without opening a PR.
  3. **Long wait defaults:** Repo documentation echoed in logs shows `STALL_THRESHOLD_IMPLEMENTING_MINUTES=120` and `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`, which are too generous for smoke/canary paths.
  4. **Conflict-heal/publish failures happen after useful work is done:** `workflow_log_analysis` completed analysis and only then failed on report publication.
- **Smallest safe mitigations:**
  - Add a terminal “implement finished but no PR created” state.
  - Use shorter stall thresholds for smoke/alt-model validation than for production orchestration.
  - Make downstream configuration resolution observable at job start.
  - Fail publication steps open when the underlying analysis/review result already exists as an artifact.
- **Track these indicators to verify improvement:**
  - `% implement runs that create a PR within 10 minutes`
  - `% stable tests exiting on terminal-state detection before timeout`
  - `median poll cycles per orchestrated issue`
  - `% review_autofix runs ending with DID_COMMIT=false after >10 minutes`
  - `% AI-bearing runs emitting structured memory telemetry`

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Implement-stage stalls**
   - **Where:** clarify → plan → implement
   - **Type:** compute + retry overhead
   - **Evidence:** `implement` failures `25224008847` / `25224028373`; stable smoke failures downstream.
   - **Fix:** trivial-task fast path, one-attempt bail, explicit PR-created terminal signal.

2. **Stable-release orchestration watch chain**
   - **Where:** implement → review/autofix → validate/orphan workflows
   - **Type:** queueing + polling + retry overhead
   - **Evidence:** `test_and_mark_stable` family avg **4,324s**; runs `25215477856`, `25223836137`.
   - **Fix:** shorter phase budgets, progressive backoff, terminal-state detection, less per-phase soft-error work.

3. **Multi-model review/autofix overreach**
   - **Where:** review/autofix
   - **Type:** compute + token cost
   - **Evidence:** slow run `25215784558`, `REVIEWERS_SUCCESSFUL: 6`, no commit.
   - **Fix:** size-based reviewer tiers, earlier no-op exit.

4. **Runner/setup/checkout tax on short jobs**
   - **Where:** poll/status/merge/support workflows
   - **Type:** queueing + local setup overhead
   - **Evidence:** `orchestrate_poll` checkout dominated a no-work run; multiple short recent runs mention hosted-runner wait.
   - **Fix:** avoid checkout on no-work paths; keep support checkouts shallow.

5. **Publish/merge conflict overhead**
   - **Where:** workflow-log-analysis post-processing
   - **Type:** merge/conflict overhead
   - **Evidence:** run `25208727402` add/add rebase conflict after successful analysis.
   - **Fix:** conflict-safe report publication or artifact-first fail-open publishing.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `test_and_mark_stable` end-to-end chain: **5 runs, 4 failures, 0 successes, avg 4,324s**
  - `workflow_log_analysis`: **avg 4,912s**
  - `review_autofix`: **p95 ~1,985s**, plus **22 cancelled** runs
  - `ci`: **51 runs, 18 failures, 35.3% failure rate**, though the sampled failures cluster around one narrow test area

- **Top failure modes**
  - `implement` no-actionable-output bail on trivial tasks (`25224008847`, `25224028373`)
  - stable tests timing out or failing because implement created no PR (`25215477856`, `25223836137`)
  - `workflow_log_analysis` report-push conflict (`25208727402`)
  - CI failures in post-Codex recovery test coverage (`25210565611`)

- **Highest-cost drivers**
  - Deep stable/orphan workflow chains
  - `review_autofix` reviewer fan-out
  - `workflow_log_analysis` deep Codex analysis (`203,782` tokens observed in one failed run)
  - Repeated setup and checkout on short/no-work jobs

- **Top 3 prioritized actions**
  1. **Ship an `implement` smoke-task fast path with early terminal failure when no PR is created.**
  2. **Refactor `test_and_mark_stable` watchers to stop on terminal states and use shorter bounded waits, including explicit alt-model verification.**
  3. **Tier `review_autofix` reviewer fan-out and skip editor/judge when reviewer output or diff size indicates a no-op path.**

## Metrics Appendix

### Overall window

| Scope | Total runs | Success | Failure | Cancelled | Other/Skipped | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All sampled repos | 1000 | 267 | 27 | 28 | 678 | 121.3 | 1.0 | 596.0 |
| `shubhodeep1/coding-workflows` | 1000 | 267 | 27 | 28 | 678 | 121.3 | 1.0 | 596.3 |

### Notable workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 5 | 0 | 4 | 1 | 80.0% | 4324.2 | 4912.0 | 5485.6 |
| `workflow_log_analysis` | 5 | 4 | 1 | 0 | 20.0% | 4911.8 | 4834.0 | 5503.8 |
| `review_autofix` | 63 | 41 | 0 | 22 | 0.0% failures / 34.9% cancelled | 357.3 | 40.0 | 1985.0 |
| `ci` | 51 | 33 | 18 | 0 | 35.3% | 601.3 | 608.0 | 644.5 |
| `implement` | 182 | 24 | 4 | 5 | 2.2% | 37.7 | 1.0 | 239.9 |
| `orchestrate_poll` | 30 | 30 | 0 | 0 | 0.0% | 45.1 | 45.0 | 53.6 |
| `orchestrate` | 5 | 5 | 0 | 0 | 0.0% | 249.2 | 250.0 | 282.6 |
| `plan` | 182 | 20 | 0 | 0 | 0.0% | 20.4 | 1.0 | 163.0 |
| `clarify` | 212 | 25 | 0 | 0 | 0.0% | 16.1 | 1.0 | 111.4 |

### Run-specific evidence used

| Run ID | Workflow family | Outcome | Duration (s) | Key evidence |
|---|---|---|---:|---|
| 25224008847 | implement | failure | 240 | Empty workdir, no-dependency-glob UV cache, Serena bootstrap, memory retrieve hit, Codex bailed after 2 no-op attempts |
| 25224028373 | implement | failure | 197 | Same failure mode as above |
| 25210565611 | ci | failure | 568 | 34 passed, 2 failed; failures in post-Codex recovery unit tests |
| 25215477856 | test_and_mark_stable | failure | 5609 | Long downstream queued polling, repeated soft-error analyzer runs, smoke path failed after no PR creation |
| 25223836137 | test_and_mark_stable | failure | 4758 | Alt-model path timed out before review; repeated implement waits |
| 25208727402 | workflow_log_analysis | failure | 4834 | Analysis succeeded, then report push failed with add/add rebase conflict; 203,782 tokens observed |
| 25215784558 | review_autofix | success | 3032 | 6 reviewer models, Serena warmup, 1200s check-run wait budget, no commit produced |
| 25236665847 | orchestrate_poll | success | 40 | `has_work:false`, full-history checkout dominated runtime |
| 25237276167 | issue_pr_status | success | 9 | Multiple GraphQL/fallback status lookups, support-ref fallback warning |
| 25236338702 | copilot_pull_request_reviewer | success | 118 | Artifact list + per-artifact delete loop, diff fetched via GH API |

### Observed token/model/cache signals

| Run ID | Workflow | Observed token/model/cache signal |
|---|---|---|
| 25208727402 | workflow_log_analysis | `203,782` tokens used before post-analysis push failure |
| 25224008847 / related summaries | implement | sampled no-op retry diagnostics included attempt token counts from ~4.3k/4.5k to ~88k/89k depending on path |
| 25215784558 | review_autofix | `MODEL_EDITOR: openai/gpt-5.3-codex`; 6 reviewer models; prompt cache enabled; UV cache saved |
| 25236665847 | orchestrate_poll | `MODEL_EDITOR: openai/gpt-5.4`; no prompt-cache counters shown |
| multiple runs | implement/review/poll/analysis | `OPENROUTER_PROMPT_CACHE_DISABLED: false` observed, but no cache create/read counters surfaced |

### AI memory telemetry summary from sampled deep dives

| Metric | Observed value | Notes |
|---|---:|---|
| Structured telemetry present | Yes | Seen in implement, orchestrate_poll, workflow_log_analysis deep-dive logs |
| Sampled retrieves | 1 | Very small sample |
| Retrieve hit rate | 100% | 1/1 sampled retrieves had `records_selected > 0` |
| Avg estimated tokens per sampled retrieve | 56 | Budget not emitted in sampled retrieve |
| `keyword_method` distribution | plain: 100% | No sampled `llm` or `none` retrieves |
| `fail_open:true` observed | 0 | None sampled |
| `enabled:false` observed | 0 | None sampled |
| Push attempts > 1 observed | 0 | Sampled `record-run-event` writes used `push_attempts: 1` |

### GH API call summary

| Workflow / run | Observed API pattern | Redundancy risk | Recommended reduction |
|---|---|---|---|
| `test_and_mark_stable` / 25215477856 | repeated downstream run status polling + repeated soft-error analyzer artifacts | High | cache poll-cycle results, progressive backoff, run analyzers only on failure |
| `issue_pr_status` / 25237276167 | GraphQL for linked issues + additional orchestrator/fallback lookups | Medium | one GraphQL query reused across steps |
| `orchestrate_poll` / 25236665847 | `/rate_limit` helper on fast no-work path | Medium | call only on retry/error paths |
| `copilot_pull_request_reviewer` / 25236338702 | artifact list then per-artifact delete loop | Medium | reduce artifact fan-out |
| `review_autofix` / 25237276160 | GraphQL issue lookup + fallback validation workflow dispatch | Low-medium | pre-resolve dispatch target; skip when label gate absent |

If you want, I can next turn this into a shorter exec-ready memo or a prioritized implementation checklist with owners and rollout order.

## Deep Audit — Workflows & Scripts (2026-05-01)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1133-1448`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The local `gh_api_safe()` helper collapses every non-rate-limit GitHub API failure into an empty string, then downstream logic treats that empty output as valid state. Concretely: missing run data falls through to `REVIEW_RUN=""`/`"null"` at `1188-1198`, failed job fetches become `FAILED_STEPS="0"` at `1235-1239`, PR metadata failures become empty `PR_HEAD_SHA` / `REVIEW_COMMENT_COUNT` at `1383-1389`, and log fetch failures degrade to `LOG_SIZE=0` at `1401-1405`. That means a transient 5xx/auth/network failure can be misreported as `status=no_review_triggered` or `status=timeout` instead of an infrastructure/API failure.  
  **Recommended fix** — Replace `gh_api_safe()` with the shared `scripts/gh_helpers.sh` retry helpers, and carry a separate “API_failed” flag so “empty result set” is never conflated with “request failed.” The wait loop should only emit `no_review_triggered` after a confirmed successful `/actions/runs` response that contains no matching run.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/review_autofix.yml:480-530`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The post-merge validate-dispatch step falls back from GraphQL `closingIssuesReferences` to a regex over PR title/body (`issues/NN`, `issue #NN`, `closes|fixes|resolves #NN`). It then dispatches validation and removes `ai:orchestrator-validate-required` for every regex hit that currently has that label. This is materially less safe than the deterministic-skip path in the same workflow, which explicitly avoids title/body regex fallback to prevent incidental references from advancing unrelated issues. A README/docs PR mentioning `issues/45` can therefore trigger validation against the wrong issue if that issue happens to carry the validate-required label.  
  **Recommended fix** — Use the same safe resolution rule here as in the deterministic-skip path: prefer GraphQL-only linked issues, and only allow a fallback when the PR body contains an explicit closing-keyword contract that has already been normalized by a shared helper.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:353-381`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — This workflow still closes orchestrator-managed child issues on PR-close events when the child PR merges into a non-`main` base (`is_managed_child=true` path at `369-377`). That conflicts with the repo’s documented “poller owns terminal label/close for orchestrator-managed children” contract and creates a second writer competing with `scripts/orchestrate_poll_process.sh`. The code may be intentionally newer than the docs, but as written the ownership model is inconsistent and can reintroduce racey label/close behavior. `[NEEDS VERIFICATION]`  
  **Recommended fix** — Pick one owner for orchestrator-managed child finalization. Preferred fix: restore the PR-close skip for managed children and let `orchestrate_poll_process.sh` remain the sole closer/backfiller; otherwise update the poller and docs together so both paths do not mutate the same terminal state.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1188-1405`  
  **Severity** — High  
  **Category tag** — `api-redundancy`  
  **Description** — The “wait for review workflow” poll loop re-fetches overlapping GitHub state each cycle. In the active path after 10 minutes, one iteration performs: `GET /actions/runs` once, `GET /actions/runs/{id}/jobs` once, `GET /actions/jobs/{id}/logs` once for shortcut parsing, `GET /pulls/{pr}` once, `GET /pulls/{pr}/comments` once, and then `GET /actions/jobs/{id}/logs` again for byte count. That is **6 calls per active poll iteration** for the same run/PR, and the two log calls hit the identical endpoint with no reuse.  
  **Recommended fix** — Cache the log blob per iteration and reuse it for both grep + byte count; only refresh PR metadata/comment count on state change; and move the run/job state fetch onto the shared actions-run cache pattern already used elsewhere (`actions_runs_cache.v1` / orchestrator poll caching) instead of bespoke per-loop requests.  
  **Current call count** — ~**6 calls per active iteration** (5 before the live-log shortcut kicks in).  
  **Proposed call count after fix** — ~**3 calls per active iteration** (`runs`, `jobs`, one `logs` fetch), with PR metadata sampled less frequently or only on change.  
  **Existing batching pattern to extend** — Extend the shared actions-run caching approach referenced by `actions_runs_cache.v1` and the batched poll patterns in `scripts/orchestrate_poll_process.sh`.

- **ID** — `BATCH-001`  
  **File path** — `.github/workflows/review_autofix.yml:480-530`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The post-merge validate-dispatch path does one GraphQL fetch for linked issues, but if that fallback path yields `labels: null`, it then does a separate `gh issue view --json labels` inside the loop for each candidate issue at `503-509`. For `N` linked issues, discovery becomes **1 GraphQL + 1 PR fetch + N issue-label calls** before any dispatch/edit work.  
  **Recommended fix** — After fallback issue-number extraction, batch-fetch all candidate issue labels in one GraphQL-alias call and feed the resulting map into the loop.  
  **Current call count** — **2 + N discovery calls** before dispatches/edits (`closingIssuesReferences`, PR fetch, then one label lookup per candidate issue).  
  **Proposed call count after fix** — **3 discovery calls total** (GraphQL linked-issue fetch, PR fallback fetch if needed, one batched label fetch).  
  **Existing batching pattern to extend** — `scripts/orchestrate_poll_process.sh:_fetch_issue_labels_batch_graphql()` (`1227-1303`).

- **ID** — `BATCH-002`  
  **File path** — `scripts/review_rb_judge.sh:159-170`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The judge collects linked-issue context by looping `ISSUE_NUMBERS` and calling `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"` once per issue to fetch `.body`. This is a straight per-iteration GitHub API loop in a review-blocked path.  
  **Recommended fix** — Replace the per-issue GET loop with one GraphQL alias query that fetches `{ number, body }` for all linked issues, then derive `FIRST_ISSUE` / `FIRST_ISSUE_BODY` from the local map.  
  **Current call count** — **1 + N** issue-context calls in the common path (issue-number discovery plus one body fetch per linked issue).  
  **Proposed call count after fix** — **1** batched GraphQL query for the linked-issue bodies.  
  **Existing batching pattern to extend** — Model it after `scripts/orchestrate_poll_process.sh:_fetch_issue_labels_batch_graphql()` and the other alias-based GraphQL batch helpers in that script.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/clarify.yml:162-199; .github/workflows/plan.yml:190-227; .github/workflows/implement.yml:332-371; .github/workflows/review_autofix.yml:799-836; .github/workflows/validate.yml:190-227; .github/workflows/orchestrate.yml:133-141,263-300; .github/workflows/orchestrate_clarify_respond.yml:206-245; .github/workflows/orchestrate_poll.yml:215-254`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The workflow-support bootstrap sequence is duplicated across nearly every AI workflow: resolve `wf_source`, derive `SCRIPT_REF`, checkout `.codex-workflow-src`, verify fallback-to-main, and stage support files / schemas. This duplication already spans at least 8 workflows and multiple variants, so every future fix to support-source resolution, fallback behavior, or schema staging must be applied in many places.  
  **Recommended fix** — Extract a shared module, e.g. `scripts/bootstrap_workflow_support.sh`, with functions like `resolve_workflow_support_ref <wf_source> <repo> <sha>` and `stage_workflow_support <support_ref> <need_schemas:true|false>`. Update callers in `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `validate.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, and `orchestrate_poll.yml`.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/mark-stable.yml:180-247,254-431,496-503; .github/workflows/test-and-mark-stable.yml:2519-2628,3752-3942,3975-3983`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `mark-stable.yml` and `test-and-mark-stable.yml` duplicate the release sanity/publish flow: yamllint, shell syntax checks, shellcheck, python compile checks, prompt audit, version validation, tag-existence check, changelog handling, release creation, and dry-run summary. The structure is close enough that bugfixes must be mirrored manually in two large workflows.  
  **Recommended fix** — Move the repeated release checks into `scripts/release_sanity_checks.sh` and the tag/release publication into `scripts/release_publish.sh`, or lift the common flow into one reusable workflow that both entrypoints call.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1448`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The interpolated `run:` block for the review wait loop is already about **16,627 characters**, leaving only about **4,373 characters** of headroom before GitHub Actions hits the hard **21,000-character** template-expression limit for a single `${{ }}`-expanded step. This block contains multiple `${{ }}` interpolations, a custom API wrapper, polling state machine, and inline diagnostics, so even modest future edits can push it over the limit.  
  **Recommended fix** — Extract the whole wait loop into `scripts/wait_for_review_run.sh` and keep the workflow step limited to env wiring + one script invocation. Secondary option: split the live-log shortcut logic into a separate step.  
  **Estimated current character count** — **16,627**  
  **Headroom remaining** — **4,373**

- No workflow currently exceeds the **800 KB** early-warning threshold for GitHub’s 1 MB workflow-file limit. Largest files observed in scope were `review_autofix.yml` (**267,353 bytes**), `test-and-mark-stable.yml` (**229,098 bytes**), and `implement.yml` (**172,578 bytes**).

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `.github/workflows/review_autofix.yml:2079-2089,3691-3917,4309-4497`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `retrigger_guard` now hardcodes `skip_judge=false`, but many downstream `if:` expressions still branch on `steps.retrigger_guard.outputs.skip_judge`. That keeps a dead compatibility branch alive, lengthens already-complex gate expressions, and makes review/judge routing harder to audit.  
  **Recommended fix** — Remove `skip_judge` after the compatibility window, or collapse the downstream conditions to the single live predicate (`max_iterations_reached`) and document the deprecation in one place.

- **ID** — `SHELL-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:10683-10684`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` and `for inum in ${_sorted_issue_nums}; do` rely on unquoted word-splitting. ShellCheck flags this as `SC2086`, and the code will mis-split if `ISSUE_NUMS` ever contains unexpected whitespace or glob characters.  
  **Recommended fix** — Build a newline-safe array with `mapfile -t` (or `readarray`) and iterate `"${issue_nums[@]}"` rather than expanding raw scalars through the shell parser.

- No `TODO` / `FIXME` / `HACK` markers were found in the scoped workflow and script files during this sweep.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, API-001 |
| Medium | 6 | BUG-002, CONSIST-001, BATCH-001, BATCH-002, DUP-001, EXPR-001 |
| Low | 3 | DUP-002, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 9 | Large |
| Expression size reduction | 1 | Small |
| Medium/Low fixes | 3 | Small |
