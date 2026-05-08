## Executive Summary

- **The biggest end-to-end latency issue is `review_autofix`, not CI.** `review_autofix` averages **396s** but has a **p95 of 1,603s** and **63/91 runs were cancelled**; individual runs hit **1,607s** (`run 25539629561`), **1,836s** (`run 25540703716`), **1,472s cancelled** (`run 25541559049`), and **1,034s cancelled** (`run 25541898149`). The highest-leverage fix is to cancel superseded PR review runs earlier and tighten comment-only review timeouts. **Estimated impact:** save **10–25 minutes** on busy PRs and materially reduce wasted model spend. **Confidence: high.**

- **`workflow_log_analysis` is the clearest token-cost hotspot.** Deep-dive logs show `summarize_unselected_runs` consuming **214,237 tokens** in failed `run 25473131401`, **252,552** in `run 25496176737`, and **253,384** in `run 25505931104`, all on `openai/gpt-5.4-mini`. **Estimated impact:** save **100k–200k+ tokens per analysis run** by caching unchanged summaries and shrinking unselected-run coverage. **Confidence: high.**

- **`test_and_mark_stable` is dominated by long polling/orchestration waits.** The workflow family averages **2,588s**, with a **p50 of 3,147s** and a failure at **4,599s** in `run 25496132733`, which failed in `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`. **Estimated impact:** save **20–45 minutes** per stable-release test by reducing poll fan-out, tightening wait caps, and skipping redundant alt-model paths when not needed. **Confidence: high.**

- **CI is consistently slow but stable; it is a pure speed problem, not a reliability problem.** All **58 CI runs succeeded**, but the family averages **619s** and repeatedly shows `lint` dominating runtime: `run 25542159321` **562s**, `run 25542146528` **610s**, `run 25539568937` **649s**, `run 25540703618` **665s`. **Estimated impact:** save **2–4 minutes per CI run** by splitting/parallelizing the bundled lint/test work. **Confidence: high.**

- **AI memory retrieval is active but mostly not helping review flows.** In the extracted deep-dive logs, I found **12 `retrieve` operations** with only a **25.0% hit rate**; **9/12** returned zero records, and reviewer retrieves in `review_autofix` runs `25489285267`, `25535269023`, and `25540703716` all returned `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`. **Estimated impact:** modest direct latency savings, but strong upside for review consistency once retrieval quality improves. **Confidence: medium-high.**

- **Two observed failures were avoidable workflow-control failures, not model-quality failures.** `test_and_mark_stable` `run 25542716411` failed in **18s** because it was dispatched from `main` instead of `stable`; two `implement` failures (`runs 25496323404`, `25496338569`) burned about **22k–24k tokens each** across two attempts before aborting on “announced edit/apply_patch … but produced no file changes.” **Estimated impact:** reduce failure/retry noise with low-risk guardrail changes. **Confidence: high.**

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1) Cancel superseded `review_autofix` runs much earlier
**Type:** Critical-path win

- **Evidence**
  - `review_autofix` has **63 cancelled runs out of 91 total** in the current window.
  - Slow/cancelled examples: `run 25541559049` **1,472s cancelled**, `run 25541898149` **1,034s cancelled**, `run 25541936220` **353s cancelled**.
  - Slow successful runs still consume large wall time: `run 25539629561` **1,607s**, `run 25540703716` **1,836s**.
  - `run 25539629561` shows separate `gate` and `review` jobs each waiting for hosted runners, then running long reviewer logic with multi-model settings and long timeouts (`CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`, `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS: 2400`).

- **Root cause**
  - The system is letting older PR review runs continue deep into reviewer execution even after they are no longer the freshest run for that PR/head state.
  - Cancellation is happening late, after runner pickup and often after model work has started.

- **Exact change**
  1. Add/strengthen a **concurrency group keyed by repository + PR number + workflow family** with `cancel-in-progress: true`.
  2. Add a **freshness gate** at the start of the expensive reviewer job: if a newer run exists for the same PR/head SHA, exit before reviewer-panel work.
  3. For **comment-only / claude-branch-review paths**, reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` from **1200** to a smaller bounded value such as **300–600**.
  4. Keep current behavior as fallback for merge/blocking paths; only tighten for non-mutating review paths.

- **Estimated time savings**
  - **10–25 minutes** saved on superseded PRs.
  - Large reduction in runner occupancy and model time on hot PRs with frequent pushes.

- **Implementation risk**
  - **Low.** This is operationally safe if keyed correctly and restricted to same PR/workflow concurrency.

---

### 2) Cut `test_and_mark_stable` poll/wait overhead
**Type:** Critical-path win

- **Evidence**
  - Workflow family `test_and_mark_stable`: **avg 2,588s**, **p50 3,147s**, **p95 4,453.8s**, only **3 runs** in window with **2 failures**.
  - `run 25496132733` failed after **4,599s** at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`.
  - Successful `run 25505874733` still took **3,147s**.
  - In that success run, multiple jobs are started independently on `ubuntu-latest` (`e2e-smoke-test`, `e2e-alt-model-test`, `orchestrate-decompose-test`, `orphan-workflows-test`, `validate-standalone-test`, `validate-scripts`), each incurring runner startup and polling overhead.

- **Root cause**
  - The workflow appears to fan out into multiple checks and long wait loops, especially around e2e/phase polling.
  - The alt-model path is expensive and can become the controlling tail latency.

- **Exact change**
  1. Add **phase-specific hard caps** with explicit fail-fast summaries for clarify/plan/implement waits rather than letting a generic long poll dominate.
  2. Skip the **alt-model e2e path** unless the change actually touches the model-sensitive codepath or an input explicitly requests it.
  3. Reuse one orchestration state fetch per poll cycle instead of independent per-phase polling where possible.
  4. If a precursor phase returns a terminal skip/failure state, stop downstream waits immediately.

- **Estimated time savings**
  - **20–45 minutes** on worst-case stable-release validation runs.
  - Smaller but still meaningful savings on successful release tests.

- **Implementation risk**
  - **Low to medium.** Safe if default behavior remains unchanged for explicitly requested full release validation.

---

### 3) Split CI’s monolithic `lint` work into parallel jobs
**Type:** Critical-path win

- **Evidence**
  - CI family is perfectly reliable but consistently slow: **58/58 success**, **avg 619.4s**, **p95 665.2s**.
  - Repeated direct evidence that `lint` dominates runtime:
    - `run 25542159321`: **562s**, `lint` dominated from `07:06:14` to `07:15:24`.
    - `run 25542146528`: **610s**, `lint` dominated ~`606s`.
    - `run 25539568937`: **649s**, `lint` dominated ~`636s`.
    - `run 25540703618`: **665s`, `lint` dominated almost the full run.
  - The same step bundles unit tests (`81 passed`), additional tests (`25 passed`), workflow script ref checks, coverage output, and lint tooling.

- **Root cause**
  - Independent checks are serialized inside one job/step family, so the slowest bundle dictates total CI time.

- **Exact change**
  1. Split CI into at least:
     - **Python/unit tests**
     - **workflow/actionlint + script-ref checks**
     - **semantic-cache coverage/tests**
  2. Cache Python dependencies and downloaded lint tools if not already cached.
  3. Preserve the same required-status interface by keeping a thin top-level “CI” aggregator job if needed.

- **Estimated time savings**
  - **2–4 minutes per CI run**.
  - Earlier failure signal on broken tests.

- **Implementation risk**
  - **Low.** This is a structural refactor, not a behavior change.

---

### 4) Remove no-op workflow fan-out on clarify/plan/respond paths
**Type:** Cross-pipeline win

- **Evidence**
  - Skip-heavy workflow families:
    - `clarify`: **105 skipped / 116 total**
    - `plan`: **91 skipped / 100 total**
    - `orchestrate_clarify_respond`: **98 skipped / 100 total**
    - `implement`: **88 skipped / 100 total**
  - Most of these runs finish in **1–3s**, but they still create workflow records, evaluation overhead, and occasionally runner scheduling overhead in related downstream paths.

- **Root cause**
  - The orchestrator emits many event-triggered workflows that immediately evaluate false.
  - This is likely intentional for composability, but it creates pipeline noise and queue churn.

- **Exact change**
  1. Push more gating upstream into the event-dispatch layer so these workflows are not started unless their primary condition is plausible.
  2. For comment-driven paths, centralize routing in a single lightweight dispatcher that only dispatches `clarify`, `plan`, or `respond` when the comment actually matches that command/state.
  3. Preserve current skip semantics as a fallback, but reduce unnecessary top-level workflow creation.

- **Estimated time savings**
  - Small per run, but meaningful at system level: lower queue pressure and cleaner orchestration traces.
  - Indirect savings to runner availability for the expensive families.

- **Implementation risk**
  - **Low to medium.** Safe if existing conditions remain as defense-in-depth.

---

### 5) Collapse artifact cleanup/listing overhead in Copilot code review
**Type:** Secondary critical-path win

- **Evidence**
  - `copilot_pull_request_reviewer` averages **240.9s** and reaches **325s** (`run 25542486702`) and **234s** (`run 25540705615`).
  - Repeated hotspots:
    - `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<run_id>/artifacts`
    - `Cleanup artifacts`
    - `Upload results/system` waiting for runner pickup.
  - `run 25542486702` shows separate artifact download/upload/cleanup stages and explicit artifact listing.

- **Root cause**
  - Extra post-processing jobs and artifact enumeration create overhead after the agent work itself.
  - Some of the workflow time is pure orchestration and runner pickup between steps/jobs.

- **Exact change**
  1. Pass artifact IDs/names between jobs via outputs instead of relisting run artifacts during cleanup.
  2. Skip cleanup when only the known single artifact exists and short retention is acceptable.
  3. Merge lightweight cleanup into the upload/results stage where possible to avoid an extra runner allocation.

- **Estimated time savings**
  - **1–2 minutes per Copilot review run**.

- **Implementation risk**
  - **Low.** Straightforward workflow simplification.

---

### 6) Trim redundant support-source checkout in same-repo `review_autofix`
**Type:** Micro-optimization

- **Evidence**
  - `run 25539629561` checks out:
    - the PR repo,
    - `.codex-workflow-src` at a pinned script ref,
    - `.codex-workflow-src-main` at `main`,
    - then stages support files.
  - This happens even when `wf_source="shubhodeep1/coding-workflows"` and `IS_WORKFLOW_SOURCE_REPO=true`.

- **Root cause**
  - The workflow keeps consumer-repo compatibility logic even when running inside the workflow source repo itself.

- **Exact change**
  - In same-repo runs, bypass the extra support-source checkout path unless the pinned `SCRIPT_REF` differs from checked-out content in a way the current run truly requires.
  - Keep the existing fallback for cross-repo consumers.

- **Estimated time savings**
  - **Seconds**, not minutes.

- **Implementation risk**
  - **Low**, if restricted to `IS_WORKFLOW_SOURCE_REPO=true`.

---

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1) Cache or shrink `workflow_log_analysis` unselected-run summarization
- **Evidence**
  - Deep-dive logs show extreme token use in `summarize_unselected_runs`:
    - `run 25473131401`: **214,237 tokens**
    - `run 25470798500`: **259,600 tokens**
    - `run 25474659590`: **232,690 tokens**
    - `run 25480827754`: **201,113 tokens**
    - `run 25477691662`: **136,338 tokens**
    - `run 25496176737`: **252,552 tokens**
    - `run 25505931104`: **253,384 tokens**
  - These are all logged under `AI_MEMORY_TELEMETRY` / `summarize_unselected_runs`.
  - The workflow family also averages **2,281.5s** in the current window, with slow runs at **1,942s** and **2,621s**.

- **Root cause**
  - The analysis workflow is summarizing a broad unselected-run set repeatedly, even when much of that context is unchanged or already deep-dived.

- **Exact change**
  1. Persist summarized-run outputs keyed by `(run_id, attempt, log_digest)` and reuse them on re-runs.
  2. Reduce the default unselected-run target from **100** to a smaller adaptive set:
     - recent failures/cancellations,
     - recent long runners without deep dives,
     - runs from workflow families with sparse evidence.
  3. On retry after a downstream Codex/report failure, **reuse the previous summarization artifact** instead of repeating `summarize_unselected_runs`.

- **Estimated savings**
  - **100k–200k+ tokens per workflow_log_analysis run**, plus lower runtime.

- **Quality-risk notes**
  - **Low risk** if cache invalidation is tied to run/log digest.
  - Keep a “force full analysis” input for manual deep audits.

---

### 2) Add a minimal-editor mode for fully specified smoke/canary implement tasks
- **Evidence**
  - The two failed `implement` runs were smoke-like, fully specified tasks editing only `tests/e2e_smoke_canary.txt`.
  - `run 25496323404` used:
    - attempt 1: **11,954 tokens**
    - attempt 2: **10,696 tokens**
    - then failed with “announced an edit/apply_patch … but produced no file changes.”
  - `run 25496338569` used:
    - attempt 1: **11,805 tokens**
    - attempt 2: **12,152 tokens**
    - same failure mode.
  - Models used were `openai/gpt-5.4` and `openai/gpt-5.3-codex`, both with `reasoning effort: medium`.

- **Root cause**
  - High-capability editor models are being used with medium reasoning for trivial, fully specified single-file replacement tasks.
  - The agent over-explores instead of directly writing the requested file.

- **Exact change**
  1. Detect “fully specified, single-file, replace-content-only” tasks.
  2. Route them to a **minimal editor prompt** with:
     - low/minimal reasoning,
     - one allowed file,
     - one validation check,
     - explicit instruction to write first and avoid exploratory narration.
  3. After one no-diff attempt, switch strategy immediately instead of repeating the same agent loop.

- **Estimated savings**
  - Roughly **15k–20k tokens per smoke implement run**.
  - Also saves 1–2 minutes and avoids failed reruns.

- **Quality-risk notes**
  - **Low risk** if restricted to tightly detected smoke/canary tasks.

---

### 3) Downshift `review_autofix` reviewer panel on tiny diffs
- **Evidence**
  - Reviewer configuration in recent runs includes **six reviewer models**:
    - `minimax/minimax-m2.5`
    - `moonshotai/kimi-k2.5`
    - `deepseek/deepseek-v4-pro`
    - `z-ai/glm-5`
    - `qwen/qwen3.6-plus`
    - `x-ai/grok-4.1-fast`
  - Also enabled:
    - `ENABLE_REVIEWER_TWO_PASS: true`
    - `REVIEWER_PASS2_REASONING_LARGE: xhigh`
  - Yet several runs are tiny diffs:
    - `run 25539369871`: `files=1 additions=1`
    - `run 25541936220`: `files=1 additions=1`
    - `run 25539569003`: `files=2 additions=2`
    - `run 25541898149`: `files=3 additions=2`

- **Root cause**
  - The expensive multi-model/two-pass review policy is being applied too broadly to very small PRs.

- **Exact change**
  1. For PRs below a conservative threshold, e.g.:
     - `<= 3 files`
     - `<= 25 changed lines`
     - no workflow/runtime-critical paths,
     use a reduced panel of **2–3 reviewer models**.
  2. Disable second-pass high reasoning on these tiny diffs.
  3. Keep the full panel for workflow, orchestration, merge, or judge-related files.

- **Estimated savings**
  - **40–70% reviewer token reduction** on small diffs.
  - Also meaningful latency reduction.

- **Quality-risk notes**
  - **Medium risk** if applied too aggressively.
  - Mitigate by keeping full review for critical file patterns.

---

### 4) Stop spending review/autofix model budget on runs that will be cancelled anyway
- **Evidence**
  - `review_autofix` had **63 cancelled runs**.
  - Several cancellations occurred after substantial runtime:
    - `run 25541559049`: **1,472s cancelled**
    - `run 25541898149`: **1,034s cancelled**
    - `run 25539435497`: **259s cancelled**
    - `run 25539569003`: **123s cancelled**

- **Root cause**
  - Older review runs continue long enough to start reviewer/model work before being cancelled by a newer event.

- **Exact change**
  - Same concurrency/freshness fix as in Speed #1, but prioritize it as a **cost** control too:
    - cancel stale runs before the reviewer panel starts,
    - perform stale-run detection immediately after gate evaluation.

- **Estimated savings**
  - Potentially **entire review-run model cost** on superseded PR revisions.

- **Quality-risk notes**
  - **Low risk** if freshness is computed by PR/head revision.

---

### 5) Fix prompt-cache observability before tuning cache strategy further
- **Evidence**
  - Cache is enabled in sampled review runs (`OPENROUTER_PROMPT_CACHE_DISABLED: false`; `cache_enabled=true` in usage logs).
  - But numeric token/cache fields are missing:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - This is visible in review cache-probe logs for runs such as `25489285267`, `25539629561`, and `25540703716`.

- **Root cause**
  - Prompt-cache instrumentation is incomplete, so the team cannot tell whether cache reads are actually landing.

- **Exact change**
  1. Emit per-call numeric cache metrics in the same log record as the existing `openrouter usage` line.
  2. Add a stable `cache_key_prefix_hash` so fragmentation can be detected.
  3. Record hit/miss status explicitly.

- **Estimated savings**
  - Direct short-term savings are **unknown**, but this is the prerequisite for reliable cache optimization.

- **Quality-risk notes**
  - **Very low risk.** Pure telemetry improvement.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1) Convert the `test_and_mark_stable` branch misdispatch into an automatic redirect
- **Failure evidence**
  - `run 25542716411` failed in **18s** at `source / Validate dispatch ref`.
  - Log message: workflow “must be dispatched from the `stable` branch (got `main`)”.

- **Root cause category**
  - **Workflow control / operator pathing**, not model or test failure.

- **Exact fix**
  - When dispatched from `main`, automatically trigger the `stable`-ref version of the workflow or route the caller to `promote-main-to-stable` programmatically, then exit successfully with a clear summary.
  - Keep the current hard fail only for impossible/unsafe states.

- **Expected reliability impact**
  - Likely removes the current most recent `test_and_mark_stable` failure mode immediately.
  - The family only has **3 runs**, so each failure matters a lot to its reported failure rate.

- **Rollback / fail-open considerations**
  - Safe to fail back to the current explicit error if auto-dispatch fails.

---

### 2) Add a smoke-task fallback path after one no-diff Codex attempt
- **Failure evidence**
  - `implement` failures `25496323404` and `25496338569` both failed at `Run Codex implementation`.
  - In both runs, Codex announced an edit/apply_patch, produced no file changes twice, then the loop aborted as “stuck in exploration.”

- **Root cause category**
  - **Agent loop / prompt-strategy mismatch** for simple tasks.

- **Exact fix**
  1. After the first “announced edit but no file changes” event on a smoke task, switch from normal Codex loop to a deterministic single-file write path.
  2. Enforce stricter smoke prompt constraints and one explicit post-write diff check.
  3. If the task is fully specified, bypass the general repair/diagnostics loop entirely.

- **Expected reliability impact**
  - Reduces implement hard failures and reduces misleading “implementation-failed” issue comments for trivial tasks.

- **Rollback / fail-open considerations**
  - If deterministic write fails validation, fall back to current general path.

---

### 3) Quarantine or classify failing nightly self-test fixtures instead of failing the whole night blindly
- **Failure evidence**
  - `nightly_validation_selftest` has **1 run / 1 failure**: `run 25534774492`, **92s**.
  - The matrix reported: `fixtures=3 passed=1 failed=2`.

- **Root cause category**
  - **Validation fixture instability or expected-negative handling**.
  - Exact failing fixture names are not present in the excerpted log body; they are likely in uploaded artifacts.

- **Exact fix**
  1. Use the uploaded self-test artifact from `run 25534774492` to identify the two failing fixtures.
  2. If they are expected-negative cases, classify them separately and fail only on unexpected regressions.
  3. If they are true regressions, split the fixture summary into explicit failing names in the main step summary so repair can be targeted quickly.

- **Expected reliability impact**
  - Prevents noisy nightly red builds if failures are expected/known.
  - If they are regressions, reduces time-to-diagnosis.

- **Rollback / fail-open considerations**
  - Keep the current strict fail until fixture classification is verified.

- **Evidence gap**
  - The current excerpt does **not** identify the two failing fixture names; next collection step is to inspect the uploaded artifact from `run 25534774492`.

---

### 4) Add early stale-run detection to `review_autofix`
- **Failure evidence**
  - While cancellations are not “failures,” they are a major rerun/waste signal: **63 cancelled** review runs.
  - Examples: `25541559049`, `25541898149`, `25539435497`.

- **Root cause category**
  - **Superseded work / duplicate triggering**.

- **Exact fix**
  - Before reviewer execution, query whether a newer run exists for the same PR/head and exit cleanly if so.

- **Expected reliability impact**
  - Reduces cancellation churn and makes workflow history more interpretable.

- **Rollback / fail-open considerations**
  - If freshness detection errors, continue with existing behavior.

---

### 5) Surface failing stable-release subtests earlier in `test_and_mark_stable`
- **Failure evidence**
  - `run 25496132733` failed after **4,599s** on the alt-model wait step.
  - `validate` later evaluated false because upstream results were already failed.

- **Root cause category**
  - **Late failure detection / poll-heavy orchestration**.

- **Exact fix**
  - Promote the wait-step failure state into a short-circuiting terminal result as soon as the watched subworkflow exceeds timeout or enters terminal failure, rather than allowing additional downstream evaluation layers.

- **Expected reliability impact**
  - Fewer ultra-long failures and easier diagnosis.

- **Rollback / fail-open considerations**
  - Keep the current downstream summary/notify behavior; only shorten the time to terminal state.

---

## AI Memory Health

- I found **48 `AI_MEMORY_TELEMETRY` entries** in the extracted `errors/`, `slow/`, and `recent/` deep-dive logs.
- Operation distribution in those extracted logs:
  - `record-run-event`: **21**
  - `retrieve`: **12**
  - `record-candidate`: **7**
  - `summarize_unselected_runs`: **4**
  - `processed-command-check`: **2**
  - `processed-command-claim`: **2**

### Retrieval effectiveness

- **Retrieve hit rate:** **25.0%** (**3/12** retrieves had `records_selected > 0`)
- **Average `estimated_tokens`:** **14.0**
- **`keyword_method` distribution:**
  - `plain`: **4**
  - `none`: **8**
  - `llm`: **0**

### What is working

- `implement` memory retrieval appears healthy in the two failed implement runs:
  - `run 25496323404`: `retrieve` returned `records_selected=2`, `estimated_tokens=56`, `keyword_method=plain`
  - `run 25496338569`: same pattern (`records_selected=2`, `estimated_tokens=56`, `keyword_method=plain`)
- Push durability looked stable:
  - all telemetry entries with `push_attempts` showed **`push_attempts=1`**
  - I found **no high push retry counts**

### What is not working

- Reviewer retrieval is mostly empty:
  - `run 25489285267`: `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`
  - `run 25535269023`: same
  - `run 25540703716`: same
- Across all extracted retrieve ops:
  - **9/12** returned **0 records**

### Flags requested by the rubric

- **`fail_open: true` entries:** **none observed**
- **`enabled: false` entries:** **none observed**
- **0-record retrieves:** **9 observed**, mostly in reviewer flows
- **High push retry counts:** **none observed**
- **Operations not seen in the extracted logs:** I did **not** find `finalize-task`, `promote`, or `compact` events in the extracted deep-dive folders.

### Recommendation

1. Improve reviewer retrieval keys so they do not default to `keyword_method=none`.
2. Emit retrieval diagnostics when `records_selected=0`:
   - candidate keys,
   - memory store path,
   - scope used (`run`, `PR`, `issue`).
3. Verify telemetry emission for `promote`, `compact`, and any memory-maintenance paths, since those operations were not visible in the extracted deep-dive set.

---

## GH API Call Audit

**Important caveat:** a rough string scan across extracted logs found many `gh api` mentions, but that scan is inflated by `workflow_log_analysis` logs re-quoting prior runs. I therefore treat the scan as directional only and anchor recommendations to direct run evidence below.

### 1) `review_autofix` post-merge validation dispatch is the highest API-hygiene concern
- **Evidence**
  - `run 25542146566`:
    - `gh api graphql` for linked issues
    - `gh workflow run`
    - `gh issue edit ... --remove-label` loop
  - `run 25542553413`:
    - `gh api graphql` queried linked issues for PR `2267`
    - `gh workflow run` attempted dispatch of `ai-validate.yml` / `internal-validate.yml`
    - linked issues fallback found issue `44`
- **Pattern**
  - One lookup to find linked issues, then per-issue dispatch/edit work.
- **Redundancy risk**
  - Per-issue mutation loops can become expensive if a PR closes many linked issues.
- **Concrete batching/reuse change**
  1. Expand the initial GraphQL query to fetch all fields needed for dispatch/label decisions in one round-trip.
  2. Only issue `gh issue edit` for issues that actually carry the relevant label.
  3. Pass linked-issue results between steps as JSON output instead of re-querying.
- **Estimated call-count reduction**
  - **30–60%** for multi-issue merged PRs.
- **Rate-limit risk reduction**
  - Medium; especially helpful during bursty merge periods.
- **Repo-rule cross-reference**
  - The PR body in `run 25539629561` explicitly references the repo rule “extend an existing call rather than adding a new one” (cited there as `CLAUDE.md §15`), so this recommendation aligns with existing API hygiene intent.

---

### 2) `cancel_on_pr_close` is doing retry/rate-limit setup even on no-op runs
- **Evidence**
  - `run 25542553416` and `run 25542146553` both reported:
    - no matching queued/in-progress runs found
    - `_gh_retry` wrapper present
    - `_rl_wait` calling `gh api -i /rate_limit`
- **Pattern**
  - A no-op path still pays for retry scaffolding and rate-limit probing.
- **Concrete change**
  1. Only call `/rate_limit` after an actual retry-worthy API failure.
  2. Short-circuit the no-op branch before initializing the retry helper state.
- **Estimated call-count reduction**
  - Small per run, but this path is frequent and very cheap to optimize.
- **Rate-limit risk reduction**
  - Low to moderate.

---

### 3) `implement` preflight uses multiple lightweight calls that can be collapsed
- **Evidence**
  - In failed runs `25496323404` and `25496338569`, preflight logic includes:
    - `gh api repos/.../issues/<issue>` for issue state/labels
    - `gh pr list --search "issue:<ISSUE_NUMBER>" --state open`
    - additional ref-resolution logic afterward
- **Pattern**
  - Multiple serial API lookups before the expensive work begins.
- **Concrete change**
  - Replace issue-state and open-PR existence checks with a single GraphQL query returning:
    - issue state,
    - labels,
    - linked/open PR presence.
- **Estimated call-count reduction**
  - Save **1–2 calls per implement run**.
- **Rate-limit risk reduction**
  - Small, but worthwhile because it also reduces preflight latency.

---

### 4) `copilot_pull_request_reviewer` artifact cleanup relists artifacts every run
- **Evidence**
  - `run 25542486702`, `run 25540705615`, `run 25539437547`, and `run 25535270944` each show `gh api /repos/.../actions/runs/<run_id>/artifacts` during cleanup.
- **Pattern**
  - Cleanup stage enumerates artifacts even though the workflow already knows what it uploaded/downloaded.
- **Concrete change**
  - Emit uploaded artifact IDs as job outputs and feed them directly to cleanup logic.
- **Estimated call-count reduction**
  - At least **one artifact-list call per run**, plus fewer follow-up lookups.
- **Rate-limit risk reduction**
  - Modest.

---

### 5) `test_and_mark_stable` e2e polling likely has the largest raw call volume
- **Evidence**
  - My rough command-string scan found **81 `gh api` mentions** in `slow/.../test_and_mark_stable/25505874733/step-008-e2e-smoke-test.log`.
  - `run 25496132733` failed after **4,599s** while waiting on alt-model clarify→plan→implement progression.
- **Pattern**
  - Repeated status polling in long-running e2e orchestration.
- **Concrete change**
  1. Fetch all watched workflow/run states in one poll cycle rather than per-phase calls.
  2. Increase poll interval once a run is stable/in-progress.
  3. Stop polling as soon as a terminal failure/skip state is seen.
- **Estimated call-count reduction**
  - **50–80%** on long release-test runs.
- **Rate-limit risk reduction**
  - High for this workflow family.

---

## Prompt Cache & Memory System

### Prompt cache behavior

- **Cache appears intended to be on**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` is present in:
    - `implement` failures `25496323404`, `25496338569`
    - `review_autofix` runs such as `25539629561`
    - `orchestrate_poll` `run 25541510287`
- **But cache effectiveness is not observable**
  - `review_autofix` cache-probe logs show `cache_enabled=true` while all numeric fields remain `na`:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - This occurs in slow review runs including `25489285267`, `25539629561`, and `25540703716`.

### Memory retrieval effectiveness

- Memory retrieval is useful in `implement`, but largely ineffective in `review_autofix`.
- The reviewer path is retrieving with `keyword_method=none` and returning zero records repeatedly, which suggests either:
  - insufficient retrieval keys,
  - wrong scope,
  - or too little promoted reviewer memory.

### Cache-fragmentation observations

- There is evidence the workflow is **trying** to separate static and dynamic prompt material:
  - review logs contain comments like “later step becomes a no-op cache read”
  - there is a “Pre-assemble static context cacheable across runs” step in `review_autofix`
- However, because cache numeric metrics are missing, there is **no evidence-grade confirmation** that cache reads are actually hitting.

### Concrete improvements

1. **Emit actual cache metrics**
   - Add numeric `prompt_tokens`, `completion_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and `hit/miss`.
   - Also emit a stable `cache_key_prefix_hash`.

2. **Keep dynamic noise after the stable prefix**
   - Ensure PR title/body, timestamps, run IDs, and volatile review state are appended after the stable prompt prefix, not embedded in the prefix itself.

3. **Promote useful reviewer memory more aggressively**
   - Since reviewer retrieves are often empty, promote small durable reviewer findings that are clearly reusable across nearby PRs.

4. **Separate telemetry from optimization**
   - First fix observability, then tune cache strategy. Right now the system is “cache-configured but cache-unmeasured.”

### Estimated impact

- **Tokens:** likely meaningful for repeated `review_autofix` runs, but not yet quantifiable from current logs.
- **Latency:** likely modest-to-good once cache hits are confirmed.
- **Reliability:** better observability reduces blind tuning and makes regression diagnosis easier.

---

## Orchestrator Health

### What looks healthy

- `orchestrate_poll` appears stable:
  - **34/34 success**
  - **avg 64.0s**
  - examples: `run 25539706033` **97s**, `run 25541510287` **100s**, `run 25538156781` **96s**
- Recent poller logs include successful memory run-ledger entries and no retry/rate-limit incidents.

### What looks unhealthy or operationally noisy

- The orchestrator-adjacent workflow graph is extremely skip-heavy:
  - `clarify`: **105 skipped / 116**
  - `plan`: **91 skipped / 100**
  - `orchestrate_clarify_respond`: **98 skipped / 100**
  - `implement`: **88 skipped / 100**
- This suggests the control plane is functional, but too many top-level workflows are being spawned only to immediately decide “not applicable.”
- `review_autofix` shows recurring operational pain:
  - duplicate/superseded runs,
  - long waits,
  - late cancellations,
  - expensive reviewer settings on tiny diffs.

### Observable indicators the team should track

1. **Skip ratio by workflow family**
   - Especially `clarify`, `plan`, `respond`, `implement`
2. **Superseded-run cancellation ratio**
   - Especially in `review_autofix`
3. **Poller p95 duration**
   - `orchestrate_poll` is currently healthy; watch for drift above current **121.8s p95**
4. **Terminal-stall signatures**
   - The PR body in `run 25539629561` references downstream `AI_STANDALONE_STALL_STATE_V1` heartbeats; that is an important indicator to track if emitted here too
5. **Empty memory retrieve rate**
   - Currently **75%** in the extracted deep-dive sample

### Smallest safe mitigations

- Add upstream gating to reduce no-op top-level workflow creation.
- Add stale-run detection for review flows.
- Keep orchestrate poller logic unchanged for now; it is one of the healthier subsystems in the sampled data.

---

## Pipeline Flow Bottlenecks

### 1) Queueing bottlenecks
- Runner queue/wait appears across many workflows:
  - CI (`25542159321`, `25542146528`, `25541898060`)
  - `review_autofix` (`25539629561`, `25539569003`)
  - Copilot review (`25542486702`, `25540705615`)
  - even no-op `cancel_on_pr_close` paths (`25542553416`, `25542146553`)
- **Impact:** queueing amplifies already-slow critical-path jobs.

**Fix order:** reduce no-op workflows and stale runs first; that indirectly frees runner capacity.

---

### 2) Compute bottlenecks
- **CI**: monolithic `lint` bundles many checks into a 9–11 minute wall-clock job.
- **Review/autofix**: multi-model review plus long summarizer/check-run waits create 16–30 minute runs.
- **Stable-release test**: long polling around clarify→plan→implement dominates total time.

**Fix order:** `review_autofix` concurrency/timeouts → `test_and_mark_stable` poll reduction → CI job splitting.

---

### 3) Retry / rework bottlenecks
- `implement` failures retried a second full model attempt even after the first attempt clearly produced no diff.
- `forward_merge_stable_to_main` has bounded retry/backoff on `git push` and verification fetch, but those are short and healthy (`runs 25542146518`, `25542553448`).

**Fix order:** special-case implement smoke fallback; leave forward-merge retry logic mostly as-is.

---

### 4) Merge / conflict / duplicate overhead
- The biggest waste is not merge conflict healing itself in this sample; it is **duplicate review runs** and **late cancellations**.
- Review runs on small diffs still trigger the heavy reviewer panel.

**Fix order:** freshness gate and reduced review panel for tiny diffs.

---

### 5) Clarify → plan → implement control-flow overhead
- The core issue is not that these flows are failing frequently; it is that many workflows are launched only to skip.
- This creates trace noise and queue churn, even when runtime is only 1–3 seconds.

**Fix order:** upstream router/gating cleanup after the critical-path review and stable-release work.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-running and cancellation-heavy: **91 runs**, **63 cancelled**, **p95 1,603s**
- `ci` consistently slow: **58 runs**, **avg 619s**, all successful
- `test_and_mark_stable` extremely slow and failure-prone in this window: **3 runs**, **avg 2,588s**, **2 failures**
- `workflow_log_analysis` very expensive in both time and tokens: **2 runs**, **avg 2,281.5s**

**Top failure modes**
- Wrong-branch stable-release dispatch (`run 25542716411`)
- Implement smoke tasks getting stuck in no-diff exploration (`runs 25496323404`, `25496338569`)
- Nightly validation self-test fixture failures (`run 25534774492`)
- Long alt-model e2e wait failure in stable-release test (`run 25496132733`)

**Highest-cost drivers**
- `workflow_log_analysis` unselected-run summarization at **214k–253k tokens** per run in observed deep dives
- `review_autofix` six-model reviewer panel + two-pass settings, even on very small PRs
- Wasted model/runner spend on cancelled superseded review runs

**Top 3 prioritized actions**
1. **Add concurrency + stale-run cancellation to `review_autofix`**
2. **Cache/shrink `workflow_log_analysis` unselected-run summarization**
3. **Reduce `test_and_mark_stable` polling and alt-model default scope**

---

## Metrics Appendix

### Overall repository metrics

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 679 | 218 | 5 | 66 | 390 | 0.74% | 146.99 | 2.0 | 651.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other/Skipped | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 58 | 58 | 0 | 0 | 0 | 619.43 | 618.5 | 665.15 |
| `review_autofix` | 91 | 23 | 0 | 63 | 5 | 396.01 | 64.0 | 1603.0 |
| `implement` | 100 | 7 | 2 | 3 | 88 | 23.83 | 1.0 | 123.4 |
| `test_and_mark_stable` | 3 | 1 | 2 | 0 | 0 | 2588.0 | 3147.0 | 4453.8 |
| `orchestrate_poll` | 34 | 34 | 0 | 0 | 0 | 64.0 | 53.5 | 121.8 |
| `copilot_pull_request_reviewer` | 23 | 23 | 0 | 0 | 0 | 240.87 | 222.0 | 358.9 |
| `workflow_log_analysis` | 2 | 2 | 0 | 0 | 0 | 2281.5 | 2281.5 | 2587.05 |

### Skip-heavy orchestrator-adjacent families

| Workflow family | Total runs | Skipped/Other | Successful | Failure | Cancelled |
|---|---:|---:|---:|---:|---:|
| `clarify` | 116 | 105 | 11 | 0 | 0 |
| `plan` | 100 | 91 | 9 | 0 | 0 |
| `orchestrate_clarify_respond` | 100 | 98 | 2 | 0 | 0 |
| `implement` | 100 | 88 | 7 | 2 | 3 |

### Observed token metrics from deep-dive logs

> Aggregate token totals for all workflows were **not** available in the collector summary. Table below includes only direct, evidence-grade token values visible in extracted deep-dive logs.

| Run ID | Workflow family | Step / operation | Observed tokens |
|---|---|---|---:|
| `25496323404` | `implement` | Codex attempt 1 | 11,954 |
| `25496323404` | `implement` | Codex attempt 2 | 10,696 |
| `25496338569` | `implement` | Codex attempt 1 | 11,805 |
| `25496338569` | `implement` | Codex attempt 2 | 12,152 |
| `25473131401` | `workflow_log_analysis` | `summarize_unselected_runs` | 214,237 |
| `25470798500` | `workflow_log_analysis` | `summarize_unselected_runs` | 259,600 |
| `25474659590` | `workflow_log_analysis` | `summarize_unselected_runs` | 232,690 |
| `25480827754` | `workflow_log_analysis` | `summarize_unselected_runs` | 201,113 |
| `25477691662` | `workflow_log_analysis` | `summarize_unselected_runs` | 136,338 |
| `25496176737` | `workflow_log_analysis` | `summarize_unselected_runs` | 252,552 |
| `25505931104` | `workflow_log_analysis` | `summarize_unselected_runs` | 253,384 |

### AI memory retrieval metrics from extracted deep-dive logs

| Metric | Value |
|---|---:|
| Total telemetry entries found | 48 |
| `retrieve` ops | 12 |
| Retrieve hit rate | 25.0% |
| Retrieves with `records_selected = 0` | 9 |
| Avg `estimated_tokens` on retrieve | 14.0 |
| `keyword_method = plain` | 4 |
| `keyword_method = none` | 8 |
| `keyword_method = llm` | 0 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Push attempts distribution | all observed pushes were `1` |

### Prompt-cache observability status

| Signal | Observed status |
|---|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | `false` in sampled `implement`, `review_autofix`, `orchestrate_poll` runs |
| Cache enabled logs present | Yes (`cache_enabled=true` in `review_autofix` cache-probe logs) |
| Numeric prompt/cache metrics present | Mostly **no** (`prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`) |
| Evidence of actual cache hits | Not evidence-grade in current logs |

### GH API hotspot summary

| Workflow / run evidence | Observed pattern | Audit note |
|---|---|---|
| `review_autofix` (`25542146566`, `25542553413`) | `gh api graphql`, `gh workflow run`, `gh issue edit` loop | Highest batching opportunity |
| `cancel_on_pr_close` (`25542553416`, `25542146553`) | cancel API + `/rate_limit` retry scaffolding on no-op path | Easy cleanup |
| `implement` (`25496323404`, `25496338569`) | issue lookup + PR lookup preflight | Can collapse into one query |
| `copilot_pull_request_reviewer` (`25542486702`, `25540705615`, `25539437547`) | artifact listing via `gh api /actions/runs/<id>/artifacts` | Reuse outputs instead of relisting |
| `test_and_mark_stable` (`25505874733`, `25496132733`) | long e2e polling; rough scan found many `gh api` mentions in e2e logs | Likely biggest raw API consumer in release tests |

If you want, I can turn this report into a **prioritized implementation checklist** with “owner / change file / expected validation” columns.

## Deep Audit — Workflows & Scripts (2026-05-08)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `.github/workflows/review_autofix.yml:238-253`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The gate step treats an unreadable PR as a reason to skip the entire review run. The only PR-state fetch in this path is a raw `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"` at lines 244-246. If that call fails, `pr_state` remains empty, and lines 250-253 flip `SHOULD_RUN=false` with `SKIP_REASON=pr_state_unknown`. That means a transient GitHub API/auth/rate-limit failure can silently suppress reviewer/editor execution instead of retrying or failing loudly.  
  **Recommended fix** — Replace the raw fetch with `gh_retry`/`_safe_gh_jq` from `scripts/gh_helpers.sh`, or hard-fail the gate job when PR state cannot be determined after retries. The safe contract here should be: only skip when the API positively says `closed`/draft/skip marker; never skip on transport failure.

- **ID** — BUG-002  
  **File path** — `.github/workflows/implement.yml:2144-2150`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — Self-repo detection is based on a basename suffix match: `if [[ "${{ github.repository }}" == *"/coding-workflows" ]]; then is_self_repo="true"`. That marks any repository whose name ends in `/coding-workflows` as the canonical source repo, not just `shubhodeep1/coding-workflows`. Downstream staging exclusions at lines 2150-2179 depend on `is_self_repo`; a same-basename consumer repo would incorrectly bypass consumer-repo protections and could stage runtime-fetched `scripts/`, `prompts/`, and `ai-memory/` content.  
  **Recommended fix** — Compare against the exact canonical slug (`shubhodeep1/coding-workflows`) or reuse the already-established source-repo flag exported by support-file staging, rather than inferring from the repository basename.

- **ID** — BUG-003  
  **File path** — `.github/workflows/review_autofix.yml:515-567`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The post-merge validate-dispatch loop issues at most one standalone validate dispatch per merged PR (`validation_dispatched` flips to `true` at lines 554-557), but it still removes `ai:orchestrator-validate-required` from every subsequent linked issue that has the label at lines 564-566. Because the dispatched workflow is invoked with `tracking_issue=0`, no per-issue validation target is passed. Inference: if one merged PR closes multiple validation-required issues, a single repo-level validate run can clear the per-issue label on all of them. [NEEDS VERIFICATION]  
  **Recommended fix** — Either keep label removal inside the validate workflow after it confirms the intended issue set, or pass explicit issue numbers into the dispatch and clear labels one issue at a time only after that issue’s validation is known to have been covered.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — BATCH-001  
  **File path** — `.github/workflows/review_autofix.yml:1373-1408`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — The PR hydration path performs five logical fetches in one straight-line execution path before any review logic runs: `pulls/{pr}` (line 1373), issue comments pagination (1374-1375), reviews pagination (1376-1377), review comments pagination (1378-1379), and linked issues via GraphQL (1403-1408). The repo already has a GraphQL-first consolidated helper, `gh_pr_with_all_comments`, in `scripts/gh_helpers.sh`, but this workflow reimplements the fetch fan-out inline.  
  **Current call count** — 5 logical fetches on the happy path, plus extra underlying requests when pagination expands.  
  **Proposed call count after fix** — 2 logical fetches by using `gh_pr_with_all_comments` for PR meta/discussion plus the existing linked-issues GraphQL call, or 1 logical fetch if `gh_pr_with_all_comments` is extended to include `closingIssuesReferences`.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh::gh_pr_with_all_comments`  
  **Recommended fix** — Replace the four manual PR/discussion fetches with one helper call, materialize `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEWS_FILE`, and `PR_REVIEW_COMMENTS_FILE` from that normalized payload, and only keep a separate linked-issues query if helper extension is deferred.

- **ID** — BATCH-002  
  **File path** — `scripts/review_rb_judge.sh:146-176`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The judge fetches linked issue numbers once via GraphQL at lines 146-151, then loops over those numbers and performs one `GET /repos/{repo}/issues/{issue_number}` per issue at line 169 to recover body and labels. That is a per-item API loop in a hot path that already has batched GraphQL patterns elsewhere in the repo.  
  **Current call count** — 1 GraphQL call + N issue REST calls for N linked issues.  
  **Proposed call count after fix** — 1 GraphQL call total by returning `number`, `body`, and labels from the initial linked-issues query or by reusing an aliased batch helper.  
  **Existing batching pattern to extend** — `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql`  
  **Recommended fix** — Extend the judge’s initial GraphQL query to include the fields now fetched in the loop, or wrap the linked issue numbers in `_fetch_candidate_issue_details_graphql` and consume its cycle-local JSON instead of issuing per-issue REST calls.

- **ID** — BATCH-003  
  **File path** — `scripts/gh_helpers.sh:916-932`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — `_gh_issue_timeline_with_cross_refs_rest()` first paginates the issue timeline, then iterates through every cross-referenced PR URL and performs a separate `gh api "${pr_url}"` lookup at line 927. That creates an N+1 pattern exactly where the repo already has GraphQL-first fallbacks for richer timeline hydration.  
  **Current call count** — 1 timeline request + N PR-detail requests for N cross-referenced PR URLs.  
  **Proposed call count after fix** — 1 GraphQL call in the common case, or `ceil(timeline_items / page_size)` GraphQL calls if pagination is required; still no per-PR REST loop.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh::gh_issue_timeline_with_cross_refs` GraphQL path, plus the aliased-GraphQL style used by `scripts/orchestrate_poll_process.sh::_fetch_linked_pr_status_graphql`  
  **Recommended fix** — Keep PR state/merge metadata in the GraphQL traversal and page GraphQL when necessary instead of dropping to `_gh_issue_timeline_with_cross_refs_rest()`’s per-URL REST lookups.

- **ID** — API-001  
  **File path** — `scripts/review_conflict_resolve.sh:102-119`  
  **Severity** — Low  
  **Category tag** — `api-redundancy`  
  **Description** — `_dispatch_orchestrator_poll_on_failure()` performs two separate `gh run list` calls against the same workflow/repo pair: one for `--status in_progress` (lines 102-108) and one for `--status queued` (113-119). The function only needs to know whether any active run exists before dispatching.  
  **Current call count** — 2 logical calls.  
  **Proposed call count after fix** — 1 logical call by listing recent runs once and filtering `queued|in_progress` locally.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh::autofix_retrigger_has_inflight_peer`  
  **Recommended fix** — Reuse the single-list-and-filter pattern from `autofix_retrigger_has_inflight_peer`, or add a tiny shared helper that returns “has active run” for a workflow file in one request. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:467-481, 1232-1254, 1719-1750, 2388-2396`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `test-and-mark-stable.yml` contains four separate inline GitHub API retry wrappers with near-identical responsibilities: two `gh_api_safe()` definitions, one `gh_api_with_retry()`, and another `gh_api_safe()` variant. They all implement the same concerns—rate-limit backoff, empty-safe fallback, and stderr capture—but with slightly different behavior and logging. That raises drift risk inside the repo’s longest, most operationally sensitive workflow.  
  **Recommended fix** — Extract a shared helper script, for example `scripts/test_and_mark_stable_helpers.sh`, with stable signatures such as `gh_api_safe "$@"` and `gh_api_with_retry <attempts> "$@"`. Update the wait-clarify, wait-review, verify-bait-removed, and wait-closing callers to source that helper instead of carrying four local variants.

- **ID** — DUP-002  
  **File path** — `.github/workflows/review_autofix.yml:515-567, 3751-3808, 3878-3929, 4625-4668`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `review_autofix.yml` repeats the same two fallback patterns in multiple late-stage jobs:  
  1. local `ensure_label_exists` / `set_issue_phase_label_resilient` definitions, even though `scripts/label_helpers.sh` already owns that behavior;  
  2. the same PR body/title regex fallback for discovering linked issue numbers when `closingIssuesReferences` is unavailable.  
  The duplicated blocks are not byte-identical anymore, so any fix to label semantics or issue-link parsing has to be replicated across multiple jobs.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner of `ensure_label_exists <label> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`, and add one shared helper in `scripts/gh_helpers.sh` for linked-issue fallback extraction, e.g. `linked_issue_numbers_from_pr_meta <repo> <pr_meta_file> <pr_number>`. Update the post-merge validate, ready-to-merge, review-blocked, and workflow-failure callers to use those shared functions.

- **ID** — DUP-003  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3341-3385, 3417-3445, 3480-3505, 3550-3575, 3662-3685, 3836-3865`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The workflow repeats the same “dispatch workflow, poll for new run ID, then poll run status until completion” shell sequence at least six times for `workflow-log-analysis.yml`, `validation-refresh.yml`, `update_workflows.yml`, `internal-memory-maintenance.yml`, `orchestrate.yml`, and `internal-validate.yml`. The loops differ only in workflow filename, input fields, deadline, and success criteria.  
  **Recommended fix** — Move that pattern into one shared script such as `scripts/dispatch_and_wait_workflow_run.sh` with a signature like `dispatch_and_wait_workflow_run <repo> <workflow_file> <deadline_secs> [field=value ...]`. Update the six callers to pass workflow-specific parameters and keep only post-run assertions inline.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1202-1585`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Phase 4: Wait for review_autofix to complete` `run:` block contains `${{ }}` interpolation and is already estimated at **23,498 characters**, which is roughly **2,498 characters over** GitHub Actions’ 21,000-character expression limit. This is in the same workflow family that has already hit the hard limit before.  
  **Recommended fix** — Extract the full poll loop into an external script under `scripts/` and pass only the minimal environment into the step. Estimated headroom after the current inline form: **-2,498 characters**.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1672-2076`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Phase 4b` canary-verification `run:` block contains `${{ }}` interpolation and is estimated at **21,287 characters**, leaving the workflow approximately **287 characters over** the hard expression ceiling. This block is especially risky because it also contains inline retry logic, pytest bootstrap, and dispatch/poll code that will naturally continue to grow.  
  **Recommended fix** — Extract this entire verification flow into a dedicated script, ideally alongside the shared helper recommended in DUP-003. Estimated headroom after the current inline form: **-287 characters**.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/validate.yml:188-482`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Fetch workflow support files` is estimated at **16,511 characters** with `${{ }}` interpolation present, leaving only about **4,489 characters** of headroom. The block already contains checkout/fallback logic, heredoc file lists, and multiple branch-sensitive `${{ github.* }}` expressions, so future edits can push it across the threshold quickly.  
  **Recommended fix** — Move the support-file staging logic into an external script under `scripts/`, and keep only repo/ref/env wiring in YAML. Estimated remaining headroom: **4,489 characters**.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/review_autofix.yml:1288-1609`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Collect PR metadata` is estimated at **16,436 characters**, leaving about **4,564 characters** of headroom. The block is also one of the repo’s most API-dense steps, so any future batching/fallback additions make another expression-limit regression likely.  
  **Recommended fix** — Extract the step into a script that consumes the existing env files and returns the same artifacts (`PR_PAYLOAD_FILE`, `PR_META_FILE`, comment files). This would also pair naturally with BATCH-001. Estimated remaining headroom: **4,564 characters**.

- **ID** — EXPR-005  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:799-1081`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Parse and post answer` is estimated at **15,139 characters**, leaving about **5,861 characters** of headroom. The block mixes memory-claim logic, loop-guard logic, Telegram alerts, and comment-posting into one interpolated shell body, so it is already above the repo’s 71% warning threshold.  
  **Recommended fix** — Move the answer-posting and loop-guard orchestration into a dedicated script under `scripts/`, and leave the workflow step responsible only for env setup and invocation. Estimated remaining headroom: **5,861 characters**.

No workflow file currently exceeds the **800 KB** early-warning threshold. The largest audited workflow files are `review_autofix.yml` (**281,488 bytes**) and `test-and-mark-stable.yml` (**268,546 bytes**).

### Section 5: Cross-Cutting Concerns

- **ID** — DEAD-001  
  **File path** — `scripts/review_run_reviewers.sh:129-133`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — The cache-probe helper declares `probe_prompt` in `local probe_prompt_file probe_prompt probe_log_one ...`, but the variable is never assigned or read afterwards. ShellCheck also flags it as unused (`SC2034`).  
  **Recommended fix** — Remove `probe_prompt` from the local declaration, or wire it into the cache-probe flow if a planned future use was intended.

- **ID** — SHELL-001  
  **File path** — `scripts/validate_process.sh:223-231`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `tg_notify()` uses `local msg="$1$(_tg_link_suffix)"` on line 225, which ShellCheck flags as `SC2155`. Declaring and assigning in one statement can mask the return status of `_tg_link_suffix`, which makes failure diagnosis harder in a script that otherwise uses strict shell settings.  
  **Recommended fix** — Split the declaration and assignment, e.g. `local msg; msg="$1$(_tg_link_suffix)"`, so the command substitution’s status is not hidden.

String scan result: no `TODO`, `FIXME`, `HACK`, or `XXX` markers were found in `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, BATCH-001, EXPR-001, EXPR-002 |
| Medium | 10 | BUG-002, BUG-003, BATCH-002, BATCH-003, DUP-001, DUP-002, DUP-003, EXPR-003, EXPR-004, EXPR-005 |
| Low | 3 | API-001, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 5 | Large |
| Code modularization | 4 | Medium |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 3 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-08)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is fully proven safe to consolidate without changing filters, retry/error behavior, cache contracts, or concurrency semantics. `NEEDS_VERIFICATION` means the overlap is real, but at least one safety precondition is not statically provable from this repo read alone. `RISKY_SKIP` means the overlap exists, but the call sits in a retry/backoff, pagination, polling, race-defense, or other sensitive path where auto-consolidation would be unsafe without manual design review.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `.github/workflows/cancel_on_pr_close.yml:68-77` and `.github/workflows/cancel_on_pr_close.yml:79-88`  
  **Current call count** — 2 logical `actions/runs` list calls per no-error pass  
  **Proposed call count** — 1 logical `actions/runs` list call, with local `queued|in_progress` filtering  
  **Endpoint(s)** — `GET /repos/{repo}/actions/runs`  
  **Evidence** — the step performs two paginated listings against the same endpoint, differing only by `status=queued` vs `status=in_progress`, then concatenates them locally before extracting run IDs:
  ```bash
  queued_runs_json="$(
    _gh_retry gh api \
      --method GET \
      "repos/${REPOSITORY}/actions/runs" \
      --paginate \
      -f status=queued \
      -f event=pull_request \
      -f "branch=${PR_HEAD_REF}" \
      -f per_page=100 \
    | jq -s "${RUNS_JQ}"
  )"
  in_progress_runs_json="$(
    _gh_retry gh api \
      --method GET \
      "repos/${REPOSITORY}/actions/runs" \
      --paginate \
      -f status=in_progress \
      -f event=pull_request \
      -f "branch=${PR_HEAD_REF}" \
      -f per_page=100 \
    | jq -s "${RUNS_JQ}"
  )"
  ```
  **Proposed fix** — If a human decides to change this path, prototype a single `actions/runs` listing for the same branch/event scope and filter `status == "queued" or status == "in_progress"` locally before the existing dedupe/cancel loop. Keep the current `_gh_retry` / `_rl_wait` behavior and existing log messages unless parity is explicitly re-approved.  
  **Safety rationale** — This path is both paginated and embedded in a retry/backoff + rate-limit handler (`_gh_retry` / `_rl_wait` at `.github/workflows/cancel_on_pr_close.yml:27-52`), which hits the repo’s explicit `RISKY_SKIP` triggers.  
  **Downstream signal** — Do **not** auto-implement; manual review must prove that a single unfiltered branch/event listing does not increase page count, change cancellation eligibility, or alter rate-limit/backoff/logging behavior.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:188-193` and `.github/workflows/issue_pr_status.yml:295-297`  
  **Current call count** — 2 logical GraphQL calls on the direct-`closingIssuesReferences` happy path  
  **Proposed call count** — 1 logical GraphQL call on that path; keep the second query only for branch-derived or fallback-only issue numbers not present in the first payload  
  **Endpoint(s)** — GitHub GraphQL `repository.pullRequest(number:) { closingIssuesReferences ... }` and GitHub GraphQL `repository { issue(number:) { labels body } }`  
  **Evidence** — the step first asks GraphQL for linked issue numbers, then later asks GraphQL again for those issues’ labels/body so it can classify tracking vs managed issues:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    -f owner="${REPOSITORY%/*}" \
    -f name="${REPOSITORY#*/}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
  ```
  ```bash
  ORCH_QUERY="query { repository(owner: \"${REPOSITORY%/*}\", name: \"${REPOSITORY#*/}\") {${ORCH_ALIAS_FRAGMENT} } }"
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ```
  The second query is needed for labels/body classification, but for issues that already came from `closingIssuesReferences`, those fields could have been fetched in the first query.  
  **Proposed fix** — Extend the first GraphQL query to return `closingIssuesReferences(first: 50) { nodes { number body labels(first: 50) { nodes { name } } } }`, normalize it into a JSON map keyed by issue number, and consult that cache before building `ORCH_ALIAS_FRAGMENT`. Only alias-query issue numbers added later from `PR_HEAD_REF` or from the body/title fallback.  
  **Safety rationale** — The overlap is real and both calls occur in the same shell step with no local mutation in between, but the later classification path also includes branch-derived/fallback issue numbers that are not guaranteed to exist in `closingIssuesReferences`.  
  **Downstream signal** — Verify three cases before changing this: (1) PR with only `closingIssuesReferences`, (2) PR that relies on `ai/issue-N` branch-name augmentation, and (3) PR that reaches the body/title fallback path.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:3244-3258` and `scripts/implement_diagnose_post_codex_failure.sh:188-209`  
  **Current call count** — 2 logical fetch sites to the same jobs endpoint on the failure path, each with up to 3 attempts  
  **Proposed call count** — 1 logical fetch site on the common path by carrying forward the earlier result; retain the script-side fetch only when the carried value is absent/empty/`unknown-step`  
  **Endpoint(s)** — `GET /repos/{repo}/actions/runs/{run_id}/jobs?per_page=100`  
  **Evidence** — the workflow step fetches run jobs to derive `FAILED_STEP_NAME`:
  ```bash
  RUN_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}/actions/runs/${{ github.run_id }}/jobs?per_page=100" || true)"
  FAILED_STEP_NAME="$(printf '%s' "${RUN_JOBS_JSON}" | jq -r '[.jobs[].steps[] | select(.conclusion == "failure")] | first | .name // ""' 2>/dev/null || true)"
  echo "failed_step_name=${FAILED_STEP_NAME}" >> "$GITHUB_OUTPUT"
  ```
  Then the diagnose script immediately refetches the same endpoint and repeats the same inference:
  ```bash
  FAILED_STEP_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/jobs?per_page=100" || true)"
  FAILED_STEP_NAME="$(printf '%s' "${FAILED_STEP_JOBS_JSON}" | jq -r '
    [.jobs[].steps[]
      | select(
          .conclusion == "failure"
          or .conclusion == "cancelled"
          or .conclusion == "timed_out"
          or .conclusion == "action_required"
        )
    ]
    | first
    | .name // ""' 2>/dev/null || true)"
  ```
  **Proposed fix** — Extend `Capture post-Codex validation errors` to persist either `FAILED_STEP_NAME` or the raw jobs JSON into `${RUNTIME_DIR}`, and update `scripts/implement_diagnose_post_codex_failure.sh` to consume that artifact first. Keep the script’s existing fetch loop as a fallback only when the carried value is missing, empty, or still `unknown-step`.  
  **Safety rationale** — The endpoint and inferred field are the same, but the duplicate fetch appears to be compensating for a GitHub eventual-consistency window on just-failed runs, so behavior parity cannot be proven statically.  
  **Downstream signal** — Before implementing, compare one hard-fail run and one cancelled run to confirm the carried-forward `FAILED_STEP_NAME` matches what the second fetch would have resolved after its retry window.

- **ID** — `REUSE-003`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:65-67` and `.github/workflows/orchestrate_clarify_respond.yml:403-405`  
  **Current call count** — 2 logical `GET /issues/{issue_number}` calls per orchestrator-managed run, plus the separate tracking-issue fetch  
  **Proposed call count** — 1 logical child-issue fetch, reusing the earlier payload in the later context assembly step  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — the workflow fetches the child issue once in `Check orchestrator metadata`:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.title // ""')"
  ```
  and then fetches the same child issue again in `Fetch issue and tracking context`:
  ```bash
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_META}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_META}" | jq -r '.title // ""')"
  ```
  **Proposed fix** — Persist the first child-issue payload to a runtime file or exported env blob in `Check orchestrator metadata`, then have `Fetch issue and tracking context` reuse that payload and fall back to `gh_retry gh api` only on cache miss / parse failure. Leave the separate `TRACKING_NUM` fetch (`.github/workflows/orchestrate_clarify_respond.yml:413-414`) unchanged.  
  **Safety rationale** — The duplicate fetches hit the same endpoint and there is no workflow-local mutation in between, but the first call is raw `gh api` while the second is `gh_retry gh api`, and the later step may intentionally prefer a fresher issue body/title after setup latency.  
  **Downstream signal** — Verify that reusing the first payload is acceptable by checking whether issue body/title edits made during runner setup are expected to affect the same run, and confirm that failure handling remains acceptable if the first fetch fails.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- `BATCH-001`: `RISKY_SKIP` — Real consolidation target, but it is dominated by paginated PR comment/review fetches, so helper substitution must be parity-checked for page-boundary and fallback behavior before implementation.
- `BATCH-002`: `NEEDS_VERIFICATION` — The N+1 linked-issue REST loop is real, but batching must preserve the judge’s “first linked issue with body/labels” behavior and current fail-open semantics.
- `BATCH-003`: `RISKY_SKIP` — This helper is already on a paginated timeline/fallback path, so changing it touches exactly the page-boundary/race-sensitive area the safety policy flags.
- `API-001`: `NEEDS_VERIFICATION` — The two `gh run list` calls clearly overlap, but a single-call replacement must preserve queued-vs-in-progress detection and the current dispatch-skip logs.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | REUSE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
