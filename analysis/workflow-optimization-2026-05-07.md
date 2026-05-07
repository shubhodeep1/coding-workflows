## Executive Summary

- **Fix the `test_and_mark_stable` review-trigger race first.** This family is the biggest end-to-end bottleneck and the least reliable: `5` runs total, `3` failures (`60%`), `avg 3902.8s`, `p50 3987s`, `p95 4461.2s`. Failed runs `25445414047` and `25428461223` died in `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`, and run `25416934394` died in `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`. Deep-dive logs show `no_review_triggered`, “PR already ... merged/closed before bait could be injected,” and “no successor run ... appeared within 30m.” **Estimated impact:** cut failing release-test wall time by ~30–60 minutes per failed run and materially improve stable-release reliability. **Confidence:** high.

- **`review_autofix` comment-only review is the main operational drag and cancellation sink.** Family metrics show `107` runs, `72` cancelled, `avg 401.8s`, `p95 1525.4s`. Runs `25468425312` (cancelled `1778s`), `25469919575` (cancelled `1083s`), `25469590668` (cancelled `688s`), and `25465658242` (success `1049s`) were dominated by the reviewer path, often with `editor/commit/judge/auto-merge skipped`. **Estimated impact:** trimming reviewer fanout and shortening waits on comment-only paths can likely save ~6–20 minutes on long review runs and reduce cancellation churn. **Confidence:** medium-high.

- **CI is reliable enough to trust but too serial for fast feedback.** The `ci` family runs `80` times with `avg 604.1s`, `p50 613s`, `p95 649.1s`; many recent successes cluster at `595–640s` (`25470469558`, `25469590588`, `25468204936`, `25467372084`). Failures are concentrated in orchestrator-related tests, e.g. run `25469919488` failing `test_judge_reasoning_effort_uses_configured_value_without_downgrade` plus integration-fingerprint regressions. **Estimated impact:** sharding the current long `lint` job should save roughly ~4–6 minutes of PR feedback time on successful runs (inference from current serial runtime). **Confidence:** high.

- **Two avoidable agent failure modes are burning retries and tokens: missing Codex dependency in `clarify`, and empty-output loops in `implement`.** Run `25441973385` failed after three identical `@openai/codex-linux-x64` dependency errors; runs `25417030055` and `25417040196` aborted after “2 consecutive attempts with no actionable output.” **Estimated impact:** remove these retry loops and fail fast/preflight to save ~1–2 minutes per bad run plus associated model spend. **Confidence:** high.

- **`workflow_log_analysis` is the clearest token-cost hotspot.** Across five sampled slow runs, `AI_MEMORY_TELEMETRY` logged `7` `summarize_unselected_runs` events totaling **1,678,264 tokens** (`25445456156`, `25441969004`, `25431219427`, `25428493736`, `25416954546`). **Estimated impact:** narrowing or deduplicating that summarization scope could save hundreds of thousands of tokens per analysis run, likely the largest direct cost reduction in the current window. **Confidence:** high.

---

## Speed Optimizations

Ranked by expected latency reduction.

### 1) Critical-path: eliminate the `test_and_mark_stable` review-trigger / auto-merge race

- **Evidence**
  - `test_and_mark_stable` family: `avg 3902.8s`, `p50 3987s`, `p95 4461.2s`, `3/5` failures.
  - Run `25445414047` failed after `3987s` at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`.
  - In `step-008-e2e-smoke-test.log`, the workflow logged:
    - `PR #... is already ... merged/closed before bait could be injected`
    - `Likely cause: review_autofix.yml's deterministic-skip-merge job auto-merged the PR before the e2e gate's force-review/e2e-smoke-test labels took effect`
    - `Review run pin was advanced ... but no successor run with head_sha=... appeared within 30m`
    - final state `REVIEW="no_review_triggered"`.
- **Root cause**
  - Release E2E waits on a review run that often becomes impossible once deterministic skip merge closes the PR before the bait commit and label guards fully take effect.
- **Exact change**
  - Move/guarantee the `force-review` and `e2e-smoke-test` labels before any auto-merge-eligible review path can run.
  - Add a hard pre-check immediately before bait injection: if PR is closed or merged, mark the run as a terminal precondition failure and skip the 30-minute review wait entirely.
  - Keep the current SHA pinning logic, but only after confirming the PR is still open and review-triggerable.
- **Estimated time savings**
  - ~30 minutes per failed E2E run that currently waits the full timeout.
  - Additional release-cycle savings from fewer reruns.
- **Implementation risk**
  - **Low-medium.** This is mainly reordering and earlier fail-fast logic, not a behavior-breaking interface change.

### 2) Critical-path: trim `review_autofix` comment-only path runtime

- **Evidence**
  - Family metrics: `107` total, `72` cancelled, `p95 1525.4s`.
  - Runs `25468425312` (`1778s` cancelled), `25469919575` (`1083s` cancelled), `25469590668` (`688s` cancelled), and `25465658242` (`1049s` success) were dominated by review execution.
  - Recent logs repeatedly show `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`.
  - Run `25469590668` had:
    - `REVIEWER_MODELS: minimax/minimax-m2.5, moonshotai/kimi-k2.5, deepseek/deepseek-v4-pro, z-ai/glm-5, qwen/qwen3.6-plus, x-ai/grok-4.1-fast`
    - `ENABLE_REVIEWER_TWO_PASS: true`
    - `REVIEWER_PASS2_REASONING_LARGE: xhigh`
    - `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`
    - `CHECK_RUNS_POLL_INTERVAL_SECS: 20`.
- **Root cause**
  - Comment-only reviews still pay for broad multi-model fanout, 2-pass review, and long check-run waits even when edit/merge phases are disabled.
- **Exact change**
  - Add a lighter “comment-only / claude-branch-review” profile:
    - reduce reviewer fanout for small or comment-only diffs,
    - disable second pass unless diff size crosses a higher threshold,
    - shorten `CHECK_RUNS_WAIT_TIMEOUT_SECS` when `editor/commit/judge/auto-merge` are already skipped.
- **Estimated time savings**
  - ~6–20 minutes on long review runs (inference from `688–1778s` cancelled runs and `1049s` successful run).
- **Implementation risk**
  - **Medium.** Review quality must be watched closely; safest rollout is to gate by comment-only mode and small diffs first.

### 3) Critical-path: shard the long serial `ci` lint/test job

- **Evidence**
  - `ci` family: `avg 604.1s`, `p50 613s`, `p95 649.1s`.
  - Recent successful runs: `25470469558` `595s`, `25469590588` `623s`, `25468204936` `639s`, `25467335858` `622s`, `25467372084` `640s`.
  - Log summaries consistently say `lint` dominated runtime for about 10 minutes.
- **Root cause**
  - The current `lint` job serializes YAML lint, actionlint, syntax checks, and multiple test suites into one long feedback path.
- **Exact change**
  - Split `ci` into at least three parallel jobs:
    1. static checks (`yaml`, `actionlint`, syntax),
    2. orchestrate/process unit tests,
    3. integration fingerprint / regression tests.
  - Keep the same required checks, just parallelize failure domains.
- **Estimated time savings**
  - ~4–6 minutes on green PRs (inference from current ~10-minute serial runtime).
- **Implementation risk**
  - **Low.** Pure workflow refactor; no product behavior change.

### 4) Critical-path: short-circuit alt-model waits when no downstream run is discoverable

- **Evidence**
  - Failed run `25416934394` took `4579s` and failed at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`.
  - `25445414047/step-009-e2e-alt-model-test.log` logged:
    - `⚠ alt-model clarify run not found`
    - `⚠ alt-model plan run not found`
    - `⚠ alt-model implement run not found`
    - `⚠ alt-model review_autofix run not found`.
- **Root cause**
  - The workflow continues a long alt-model waiting path even when the analyzer has already established that no corresponding workflow runs were found.
- **Exact change**
  - Convert “run not found” from a warning into an early terminal state once the discovery window closes, with explicit reason codes for absent clarify/plan/implement/review runs.
- **Estimated time savings**
  - Potentially tens of minutes on failing alt-model E2E runs; exact savings depend on current timeout branch.
- **Implementation risk**
  - **Low-medium.** Needs careful guardrails so slow-starting runs are not misclassified as absent.

### 5) Micro-optimization: reduce artifact cleanup overhead in `copilot_pull_request_reviewer`

- **Evidence**
  - Family p95 is `375.6s`; recent run `25469921065` took `401s` and `25469366729` took `331s`, both with “Cleanup artifacts” dominating visible tail latency.
  - `25469921065` logs show:
    - artifact listing at `/repos/shubhodeep1/coding-workflows/actions/runs/25469921065/artifacts`
    - per-artifact deletion in cleanup steps.
- **Root cause**
  - Tail-end API cleanup work adds visible latency after review results are already available.
- **Exact change**
  - Skip cleanup when the artifact list is empty.
  - If policy allows, skip explicit delete for single short-retention artifacts and rely on retention settings.
  - Otherwise, move cleanup behind a non-blocking best-effort path if workflow semantics allow.
- **Estimated time savings**
  - Likely tens of seconds to a few minutes on affected runs.
- **Implementation risk**
  - **Low**, if retention policy is acceptable; **medium** if manual deletion is currently compliance-required.

---

## Cost Optimizations

Ranked by expected token / dollar savings.

### 1) Reduce `workflow_log_analysis` unselected-run summarization scope and deduplicate passes

- **Evidence**
  - `workflow_log_analysis` family: `avg 2980.6s`, `p50 3041s`, `p95 3462.2s`.
  - Deep-dive `AI_MEMORY_TELEMETRY` showed `7` `summarize_unselected_runs` events totaling **1,678,264 tokens**:
    - `25445456156`: `240,605` and `255,754`
    - `25441969004`: `225,273`
    - `25431219427`: `241,574` and `255,754`
    - `25428493736`: `203,550`
    - `25416954546`: `255,754`.
- **Root cause**
  - The analysis workflow is summarizing a very large set of unselected runs, and some runs appear to perform multiple large summarization passes.
- **Exact change**
  - Summarize only runs not already covered by deep-dive logs.
  - Cap unselected-run summarization below the current apparent 100-run target when repo/family variance is low.
  - Deduplicate repeated summarization stages within the same analysis run.
- **Estimated savings**
  - Hundreds of thousands of tokens per analysis run; in the sampled window, even a 50% cut would save ~839k tokens.
- **Quality-risk notes**
  - **Low-medium.** Keep deep-dive coverage and high-value outlier summaries; trim only redundant breadth.

### 2) Downshift reviewer fanout / second-pass reasoning on comment-only reviews

- **Evidence**
  - `review_autofix` comment-only path still used six reviewer models in run `25469590668`, with `ENABLE_REVIEWER_TWO_PASS: true` and `REVIEWER_PASS2_REASONING_LARGE: xhigh`.
  - Many cancelled runs never reached edit/merge behavior, so the expensive multi-model review often produced no merge outcome.
- **Root cause**
  - The review path is using a high-cost ensemble configuration even when the workflow mode only posts comments.
- **Exact change**
  - For `CLAUDE_BRANCH_REVIEW_MODE` or comment-only gate paths:
    - cut reviewer count,
    - disable pass 2 unless diff is large,
    - prefer medium reasoning by default.
- **Estimated savings**
  - Likely the second-largest direct model-cost reduction after workflow-log analysis. Exact token totals were not emitted in the sampled review logs, so savings are directionally strong but not directly quantifiable here.
- **Quality-risk notes**
  - **Medium.** Roll out only on comment-only mode first; compare reviewer issue-find rate before broadening.

### 3) Fail fast on repeated no-op `implement` behavior

- **Evidence**
  - `25417030055` and `25417040196` both failed with:
    - `Codex produced no actionable output 2 attempts in a row ... agent loop is stuck in exploration`
    - `Codex bailed: 2 consecutive attempts with no actionable output`.
  - Both runs also performed memory retrieval before failing.
- **Root cause**
  - The agent is retrying even after a strong signal that the task framing is not producing edits.
- **Exact change**
  - After the first empty-output + announced-edit-without-change cycle, switch immediately to a diagnostic/fallback prompt or return to clarify instead of retrying the same implementation prompt.
- **Estimated savings**
  - ~1 failed implementation attempt worth of tokens and runtime per affected run.
- **Quality-risk notes**
  - **Low.** This removes low-yield retries rather than successful work.

### 4) Preflight the Codex binary in `clarify`

- **Evidence**
  - `25441973385` logged the same missing dependency three times:
    - `Missing optional dependency @openai/codex-linux-x64`
    - ended with `Codex clarify failed after 3 attempts`.
- **Root cause**
  - Environment bootstrap failure is being retried like a model failure.
- **Exact change**
  - Add a short preflight after install to verify the Codex CLI and platform package resolve before entering the attempt loop.
- **Estimated savings**
  - Two wasted retries per clarify failure of this class.
- **Quality-risk notes**
  - **Low.** Purely removes redundant retries.

### 5) Reduce repeated prompt/context expansion and make prompt-cache effectiveness measurable

- **Evidence**
  - `review_autofix` explicitly pre-assembles “static context cacheable across runs.”
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` in run `25469590668`.
  - But the current telemetry window has **no prompt-cache create/read counters**, so actual hit rate is unknown.
- **Root cause**
  - Prompt caching appears enabled, but there is not enough observability to tell whether unstable prefixes or dynamic context placement are reducing reuse.
- **Exact change**
  - Emit per-call prompt-cache create/read metrics.
  - Keep static policy/instruction blocks ahead of PR-specific metadata, diff context, and runtime noise.
  - Reuse already-fetched PR metadata/check-run context inside the same job rather than reconstructing prompt inputs in later steps.
- **Estimated savings**
  - Unquantified in this window; likely meaningful in `review_autofix` because that workflow repeatedly assembles large, stable review context.
- **Quality-risk notes**
  - **Low.** This is an observability and prompt-stability improvement, not a model-capability downgrade.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1) Fix the release E2E review-trigger race

- **Failure evidence**
  - `test_and_mark_stable` failed `3/5` times.
  - Run `25445414047` ended in `no_review_triggered`; deep log says the PR was already merged/closed before bait injection and no successor review run appeared within `30m`.
  - Run `25428461223` failed at the same Phase 4 wait point.
- **Root cause category**
  - Workflow orchestration race / state-transition bug.
- **Exact fix**
  - Make label/backstop protection happen before review/auto-merge paths can close the PR; fail fast when the PR is already closed.
- **Expected reliability impact**
  - Highest in the current dataset; should materially reduce release-test failures and reruns.
- **Rollback / fail-open**
  - If the new guard is too aggressive, keep the old wait path behind a feature flag for a short period.

### 2) Fix `clarify` environment bootstrap before retries begin

- **Failure evidence**
  - Run `25441973385` failed after three identical `@openai/codex-linux-x64` errors.
- **Root cause category**
  - Dependency/bootstrap failure.
- **Exact fix**
  - Pin and verify Codex CLI plus platform-specific optional dependency during setup; abort immediately on install validation failure.
- **Expected reliability impact**
  - High for this failure mode; converts opaque three-attempt failure into deterministic setup validation.
- **Rollback / fail-open**
  - Keep existing retry loop only for runtime/model failures, not install failures.

### 3) Change `implement` empty-output retries into a structured fallback

- **Failure evidence**
  - Runs `25417030055` and `25417040196` both failed on consecutive empty-output behavior.
- **Root cause category**
  - Agent-loop control / prompt-task mismatch.
- **Exact fix**
  - After the first no-actionable-output cycle, switch to:
    - a constrained repair prompt,
    - or a return-to-clarify path,
    - or a targeted “edit these files only” instruction.
- **Expected reliability impact**
  - Medium-high: fewer dead-end implementation failures and less retry churn.
- **Rollback / fail-open**
  - Keep a manual override to allow a second attempt for tasks known to need repo exploration.

### 4) Stabilize orchestrator-related CI regressions separately from syntax/lint

- **Failure evidence**
  - CI family failure rate is `7.5%` (`6/80`).
  - `25469919488` failed `test_judge_reasoning_effort_uses_configured_value_without_downgrade`.
  - The same run also logged fingerprint-regression errors:
    - missing `must_contain` in `.github/workflows/implement.yml`
    - reappeared `must_not_contain` in `.github/workflows/implement.yml`
    - reappeared `must_not_contain` in `scripts/foo.sh`.
- **Root cause category**
  - Regression-test brittleness / orchestrator logic churn.
- **Exact fix**
  - Isolate orchestrator/process tests and integration-fingerprint verification into their own required shard, then repair the specific reasoning-effort and fingerprint expectations.
- **Expected reliability impact**
  - Medium: same overall quality bar, but fewer full-CI failures from one volatile test cluster and clearer ownership.
- **Rollback / fail-open**
  - Keep all shards required; do not downgrade the gate, only separate it.

### 5) Treat poller runner starvation as infrastructure-unavailable, not silent workflow failure

- **Failure evidence**
  - `orchestrate_poll` run `25424218738` failed after `903s`, but the available system log only shows repeated queue waits:
    - `08:18:52` waiting,
    - `08:23:52` waiting,
    - `08:28:52` waiting.
- **Root cause category**
  - Queueing / runner acquisition.
- **Exact fix**
  - Distinguish “runner never acquired” from workflow logic failure in reporting and alerting; optionally auto-retry short poller jobs once after queue starvation.
- **Expected reliability impact**
  - Medium for operator clarity, low-medium for true job-success rate.
- **Rollback / fail-open**
  - Safe: reporting-only change can be rolled back independently of retry policy.

---

## AI Memory Health

- **Telemetry presence**
  - `AI_MEMORY_TELEMETRY:` is present and functioning in the sampled deep-dive logs; this is a healthy sign.
  - Observed ops in this window: `record-run-event` (`47`), `retrieve` (`23`), `processed-command-check` (`9`), `processed-command-claim` (`9`), `summarize_unselected_runs` (`7`), `record-candidate` (`4`).
  - I did **not** observe `finalize-task`, `promote`, `compact`, or `processed-command-complete` in the sampled deep-dive set.

- **Retrieve effectiveness**
  - Observed `retrieve` ops: `23`
  - Retrieves with `records_selected > 0`: `9`
  - **Retrieve hit rate:** `39.1%`
  - Retrieves returning `0` records: `14`
  - **Zero-result rate:** `60.9%`

- **Average retrieval size**
  - Average `estimated_tokens`: **21.9**
  - **Budget comparison gap:** no explicit retrieval budget field was emitted in the sampled telemetry, so `estimated_tokens` vs. budget cannot be computed directly.

- **`keyword_method` distribution**
  - `none`: `13`
  - `plain`: `10`
  - `llm`: `0`

- **Flags**
  - `enabled: false` retrieve entries observed: `0`
  - `fail_open: true` retrieve entries observed: `0`
  - High push retry counts were limited but present:
    - `implement` run `25417030055` logged `record-run-event` with `push_attempts: 2`
    - `clarify` run `25441973385` logged `record-run-event` with `push_attempts: 2`

- **Workflow-specific observations**
  - Reviewer retrieval is weak in this window:
    - `review_autofix` run `25469590668`: `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: none`
    - same pattern appeared in `25469919575` and `25470469660`.
  - Clarify retrieval also missed in the failed clarify run:
    - `25441973385`: `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: plain`
  - Implementation retrieval did better:
    - `25417030055` and `25417040196`: `records_selected: 2`, `estimated_tokens: 56`, `keyword_method: plain`

- **Recommendations**
  1. Improve reviewer memory indexing/query terms first; reviewer path had repeated `0`-hit retrievals in long, expensive runs.
  2. Emit an explicit retrieval budget field to make `estimated_tokens / budget` auditable.
  3. Investigate why `llm` keyword extraction was unused in this sample; if intentional, document it, and if not, test it on reviewer retrieval where `plain/none` underperformed.
  4. Track `push_attempts > 1` as a small but useful early signal of memory-write friction.

---

## GH API Call Audit

No separate repository-specific GH API hygiene document was included in the supplied telemetry, so the audit below is based on observed workflow behavior.

### 1) Highest-volume API polling: `test_and_mark_stable`

- **Evidence**
  - Sampled run `25445414047` contained about **`82` `gh api`** occurrences.
  - The failed E2E path repeatedly polled workflow runs, labels, PR state, and successor-run status while waiting for review progression.
- **Redundant pattern**
  - Multiple polling loops appear to re-fetch overlapping workflow-run state during the same E2E scenario.
- **Concrete change**
  - Centralize E2E run-state polling into one helper that caches:
    - current PR state,
    - latest relevant workflow runs,
    - latest head SHA / pin SHA mapping.
- **Estimated call-count reduction**
  - Likely tens of API calls per E2E run.
- **Rate-limit risk reduction**
  - Medium. This is the largest single observed GH API hotspot.

### 2) Repeated metadata fetches in `review_autofix`

- **Evidence**
  - Run `25469590668` contained about **`30` `gh api`** calls and **`2` `/rate_limit`** probes.
  - Steps separately collected PR metadata, PR check-run failures, linked issue context, and review gate data.
- **Redundant pattern**
  - The same PR/check-run state is gathered in multiple review steps even though the job already has it.
- **Concrete change**
  - Fetch PR metadata, linked issues, file list, and check-run status once in an early step; persist as JSON in the workspace for later steps.
- **Estimated call-count reduction**
  - ~20–40% on this path, depending on the branch taken.
- **Rate-limit risk reduction**
  - Medium, especially on high-churn PRs that trigger many review reruns.

### 3) Rate-limit probing in `cancel_on_pr_close`

- **Evidence**
  - Run `25470748896` had about **`8` `gh api`** calls and **`2` `/rate_limit`** checks.
  - That run found no matching runs to cancel.
- **Redundant pattern**
  - The workflow checks rate limits proactively even when there may be no actionable cancellation work.
- **Concrete change**
  - Only call `/rate_limit` after a real API failure or after confirming there are candidate runs to cancel.
- **Estimated call-count reduction**
  - Small per run, but meaningful because this workflow is short and frequent.
- **Rate-limit risk reduction**
  - Low-medium.

### 4) GraphQL/file-list duplication in status/review helpers

- **Evidence**
  - `issue_pr_status` run `25470748881` showed about **`10` `gh api`** calls and **`4` GraphQL** queries.
  - `copilot_pull_request_reviewer` run `25469921065` showed `github.paginate(github.rest.pulls.listFiles)` in `Prepare`.
- **Redundant pattern**
  - PR details and file lists are fetched in multiple workflows that may run close together on the same PR/head SHA.
- **Concrete change**
  - Within each workflow run, persist fetched PR details/file lists once and reuse them across downstream steps.
  - Where workflows dispatch each other, pass a compact artifact or output with the already-fetched PR metadata if the called workflow can consume it safely.
- **Estimated call-count reduction**
  - Moderate on review/status workflows; higher on large-file PRs.
- **Rate-limit risk reduction**
  - Medium.

### 5) Per-artifact delete loop in `copilot_pull_request_reviewer`

- **Evidence**
  - `25469921065` cleanup listed artifacts at `/actions/runs/.../artifacts`, then deleted per artifact.
- **Redundant pattern**
  - One list call plus one delete call per artifact creates tail-end API overhead.
- **Concrete change**
  - Short-circuit when artifact list is empty; if retention policy permits, avoid explicit deletion for short-lived single-artifact runs.
- **Estimated call-count reduction**
  - Small-to-moderate, proportional to artifact count.
- **Rate-limit risk reduction**
  - Low.

---

## Prompt Cache & Memory System

### Prompt cache behavior

- **What is working**
  - Prompt caching appears to be intentionally enabled in `review_autofix`:
    - `OPENROUTER_PROMPT_CACHE_DISABLED: false` in run `25469590668`
    - dedicated step: `Pre-assemble static context cacheable across runs`.
- **What is missing**
  - I found **no prompt-cache create/read/hit/miss counters** in the sampled deep-dive logs, so actual prompt-cache effectiveness cannot be quantified.
- **Likely fragmentation risks**
  - The workflow clearly separates a static pre-assembly step from later PR-specific steps, which is good.
  - However, dynamic PR metadata, check-run context, and runtime env content are still being assembled across multiple later steps. That can fragment cacheability if any of that content leaks into the shared prefix. This is an inference, not a directly logged cache miss.

### Memory retrieval effectiveness

- Reviewer retrieval underperformed in this window:
  - `review_autofix` sampled retrieves repeatedly returned `0` records with `keyword_method: none`.
- Implementation retrieval performed better:
  - both sampled failed implement runs retrieved `2` records with `estimated_tokens: 56` and `keyword_method: plain`.

### Concrete improvements

1. **Emit prompt-cache metrics**
   - Add create/read/hit/miss telemetry per model call.
   - **Expected impact:** finally makes token and latency savings measurable.
   - **Risk:** low.

2. **Make the stable prompt prefix stricter**
   - Keep org/repo policy, reviewer instructions, and stable workflow rules in the pre-assembled block.
   - Append PR metadata, file lists, diffs, and transient timestamps only after the cacheable prefix.
   - **Expected impact:** lower token re-send and latency on repeated `review_autofix` calls.
   - **Risk:** low.

3. **Reuse fetched metadata across review steps**
   - Feed one normalized PR/check-run payload into prompt construction rather than refetching and reconstructing variants per step.
   - **Expected impact:** lower prompt variance, lower GH API count, and better cache reuse.
   - **Risk:** low.

4. **Improve reviewer memory retrieval**
   - Tune reviewer memory queries to use PR/file-domain keywords instead of falling back to `none`.
   - **Expected impact:** better reviewer recall and potentially fewer repeated comments on recurring workflow patterns.
   - **Risk:** low-medium.

---

## Orchestrator Health

### Current health snapshot

- **Good signs**
  - Gating is generally active: `clarify`, `plan`, and `implement` families each have `p50 1s` and high `other_count`, which indicates many runs are being skipped cleanly rather than executing unnecessarily.
  - Successful poller run `25470191414` completed in `56s` and logged `poll_completed` with `has_work: false`, suggesting idle cycles are cheap when runner pickup is healthy.

- **Primary pain points**
  1. **Review-cycle churn**
     - `review_autofix` has `72` cancellations out of `107` runs.
     - Multiple long comment-only review runs were cancelled after heavy work, indicating overlapping waves and obsolete work.
  2. **State-transition races**
     - Release E2E shows a race between PR merge, label propagation, bait injection, and review triggering.
  3. **Agent dead-end loops**
     - `implement` retried after no-actionable-output signals.
     - `clarify` retried install failures.
  4. **Queue sensitivity**
     - `orchestrate_poll` failure `25424218738` appears to be runner starvation rather than orchestration logic failure.

### Smallest safe mitigations

1. **Deduplicate comment-only review waves by head SHA**
   - Cancel or suppress superseded review jobs earlier when a newer head SHA is already queued/running.
2. **Add terminal-state detection to release E2E waits**
   - If the PR is merged/closed, stop waiting for successor runs.
3. **Separate environment failures from model failures**
   - Install/bootstrap failures should not enter model retry loops.
4. **Tag queue-starved runs explicitly**
   - Distinguish orchestration failures from “runner unavailable” in summaries and alerts.

### Observable indicators teams should track

- `review_autofix` cancellation rate
- `test_and_mark_stable` `no_review_triggered` count
- successor-review-run success rate after bait/fix push
- `implement` empty-output abort count
- clarify/install preflight failure count
- poller runs with runner wait >5 minutes
- AI memory retrieve hit rate by role (`reviewer`, `clarify`, `implementation`)

---

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact across the pipeline.

### 1) Review/autofix stage is the dominant execution bottleneck
- **Compute overhead:** long reviewer runs with six-model fanout and second-pass reasoning.
- **Retry/cancellation overhead:** `72/107` review runs cancelled.
- **Merge/conflict overhead:** comment-only paths still wait on review machinery even when merge/edit is skipped.
- **Fix**
  - Light-weight comment-only profile, dedupe by head SHA, shorter waits.

### 2) Release E2E waits are the dominant workflow-level latency amplifier
- **Retry/wait overhead:** `test_and_mark_stable` failures spent `3427–4579s`.
- **Merge/conflict overhead:** PR closes before bait injection or successor review run appears.
- **Fix**
  - Earlier label application, terminal-state detection, and shorter impossible-state exits.

### 3) CI feedback is slowed by a monolithic serial `lint` job
- **Compute overhead:** repeated ~10-minute runs even when only one test cluster is volatile.
- **Fix**
  - Shard static checks, orchestrator unit tests, and integration fingerprint tests.

### 4) Queueing overhead is significant on short orchestration workflows
- **Queueing evidence:** poller failure `25424218738` never appears to reach actual work; multiple review/status runs also logged hosted-runner waits.
- **Fix**
  - Prefer fewer redundant short workflows where safe; tag queue-starvation distinctly; auto-retry only tiny poller/status jobs if needed.

### 5) Workflow-log analysis is a back-office bottleneck with large token spend
- **Compute + cost overhead:** `avg 2980.6s` and `1.678M` sampled summarization tokens.
- **Fix**
  - Narrow summarization scope and dedupe repeated passes.

Flow-wise, the main bottleneck chain is:

**clarify / implement retries or skips → review_autofix fanout + cancellations → release E2E wait loops → CI regression reruns / analysis overhead**

The biggest end-to-end wins are therefore:
1. fix release E2E trigger race,
2. slim comment-only reviews,
3. shard CI.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `test_and_mark_stable`: very long (`avg 3902.8s`) and failure-prone (`60%` failure rate).
  - `review_autofix`: cancellation-heavy (`72/107`) with very high p95 (`1525.4s`).
  - `ci`: consistent ~10-minute feedback loop (`avg 604.1s`, `p50 613s`).
  - `workflow_log_analysis`: expensive back-office workflow (`avg 2980.6s`) with very high summarization token use.

- **Top failure modes**
  - Release E2E waits for review runs that never trigger after PR merge/closure (`25445414047`, `25428461223`).
  - Alt-model E2E phases not finding downstream runs (`25416934394`).
  - Clarify bootstrap failure due to missing `@openai/codex-linux-x64` (`25441973385`).
  - Implement retry loops on no-actionable-output (`25417030055`, `25417040196`).
  - CI regressions in orchestrator/fingerprint tests (`25469919488`).

- **Highest-cost drivers**
  - `workflow_log_analysis` summarization (`1,678,264` sampled tokens across `7` telemetry events).
  - Multi-model, two-pass comment-only review runs.
  - Avoidable agent reruns in clarify/implement.
  - Repeated GH API polling in release E2E and review workflows.

- **Top 3 prioritized actions**
  1. **Fix the release E2E review-trigger race** by moving label/backstop protection earlier and failing fast when the PR is already closed.
  2. **Create a lighter `review_autofix` comment-only profile** with fewer reviewer models, reduced second-pass use, and shorter wait timeouts.
  3. **Shard CI** into parallel static checks, orchestrator tests, and integration-fingerprint tests while repairing the current failing assertions.

---

## Metrics Appendix

### Overall and repo-level summary

| Scope | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall window | 1000 | 308 | 14 | 79 | 599 | 1.4% | 147.632 | 2.0 | 640.0 |
| `shubhodeep1/coding-workflows` | 1000 | 308 | 14 | 79 | 599 | 1.4% | 147.632 | 2.0 | 640.15 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 80 | 74 | 6 | 0 | 0 | 7.5% | 604.1 | 613.0 | 649.05 |
| `review_autofix` | 107 | 33 | 0 | 72 | 2 | 0.0% | 401.78 | 75.0 | 1525.4 |
| `test_and_mark_stable` | 5 | 2 | 3 | 0 | 0 | 60.0% | 3902.8 | 3987.0 | 4461.2 |
| `orchestrate_poll` | 34 | 33 | 1 | 0 | 0 | 2.9% | 82.82 | 51.0 | 163.4 |
| `workflow_log_analysis` | 5 | 5 | 0 | 0 | 0 | 0.0% | 2980.6 | 3041.0 | 3462.2 |
| `clarify` | 191 | 24 | 1 | 0 | 166 | 0.5% | 14.21 | 1.0 | 98.5 |
| `implement` | 159 | 18 | 2 | 7 | 132 | 1.3% | 25.04 | 1.0 | 180.5 |

### Selected failure evidence

| Run ID | Workflow family | Duration (s) | Failure point | Key evidence |
|---|---|---:|---|---|
| `25445414047` | `test_and_mark_stable` | 3987 | `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` | `no_review_triggered`; no successor review run within `30m`; PR already merged/closed before bait injection |
| `25428461223` | `test_and_mark_stable` | 3427 | `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` | Same failure point as above |
| `25416934394` | `test_and_mark_stable` | 4579 | `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)` | alt-model clarify/plan/implement/review runs not found |
| `25441973385` | `clarify` | 122 | `clarify / Run Codex` | Missing `@openai/codex-linux-x64`; failed after 3 attempts |
| `25417030055` | `implement` | 130 | `implement / Run Codex implementation` | 2 consecutive no-actionable-output attempts; loop stuck in exploration |
| `25469919488` | `ci` | 475 | `lint / Orchestrate poll process unit tests` | reasoning-effort test failed; integration fingerprint regressions |
| `25424218738` | `orchestrate_poll` | 903 | no job step started | repeated runner wait lines; likely queue starvation |

### AI memory metrics

| Metric | Value |
|---|---:|
| Total telemetry objects parsed | 99 |
| `retrieve` ops | 23 |
| Retrieve hits (`records_selected > 0`) | 9 |
| Retrieve hit rate | 39.1% |
| Zero-record retrieves | 14 |
| Zero-record rate | 60.9% |
| Avg `estimated_tokens` per retrieve | 21.9 |
| `keyword_method = none` | 13 |
| `keyword_method = plain` | 10 |
| `keyword_method = llm` | 0 |
| `enabled: false` retrieves | 0 |
| `fail_open: true` retrieves | 0 |

### Prompt-cache observability snapshot

| Metric | Observed value |
|---|---|
| Prompt cache enabled flag seen | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false` in `review_autofix` run `25469590668`) |
| Static cacheable assembly step seen | Yes (`Pre-assemble static context cacheable across runs`) |
| Prompt cache create/read counters | Not observed |
| Prompt cache hit/miss rate | Not measurable from current telemetry window |

### GH API sampled hotspots

| Run ID | Workflow family | Approx `gh api` count | GraphQL count | `/rate_limit` count | Primary hotspot |
|---|---|---:|---:|---:|---|
| `25445414047` | `test_and_mark_stable` | 82 | 0 | 0 | repeated workflow/PR/review polling |
| `25469590668` | `review_autofix` | 30 | 0 | 2 | PR metadata, check-run context, review gating |
| `25469921065` | `copilot_pull_request_reviewer` | 10 | 0 | 0 | artifact list/delete and PR file pagination |
| `25470748896` | `cancel_on_pr_close` | 8 | 0 | 2 | rate-limit probe and per-run cancel POST |
| `25470748881` | `issue_pr_status` | 10 | 4 | 0 | GraphQL linked-issue/status lookups |

### `workflow_log_analysis` summarization token events

| Run ID | `summarize_unselected_runs` events | Tokens used |
|---|---:|---:|
| `25445456156` | 2 | 496,359 |
| `25441969004` | 1 | 225,273 |
| `25431219427` | 2 | 497,328 |
| `25428493736` | 1 | 203,550 |
| `25416954546` | 1 | 255,754 |
| **Total** | **7** | **1,678,264** |

### Recent successful CI timing samples

| Run ID | Duration (s) | Notes |
|---|---:|---|
| `25470469558` | 595 | `lint` dominated runtime |
| `25469590588` | 623 | `lint/system` waited for hosted runner |
| `25468204936` | 639 | `lint` took ~626s |
| `25467335858` | 622 | `lint` ran ~608s |
| `25467372084` | 640 | `lint` dominated runtime |


## Deep Audit — Workflows & Scripts (2026-05-07)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`
  - **File path** — `.github/workflows/test-and-mark-stable.yml:1311-1321,1351-1364,1473-1477,1647-1652`
  - **Severity** — High
  - **Category tag** — `bug`
  - **Description** — The smoke-gate wait logic treats several review-workflow failure states as success-like outputs. A completed run with `conclusion == failure` is rewritten to `status=completed_with_findings` at lines 1317-1321; an in-progress run with failed steps is also rewritten to `completed_with_findings` at lines 1359-1364; and an in-progress run is promoted to `status=success` as soon as the reviewer-majority heuristic passes at lines 1473-1477. The downstream canary-verification step then runs when `steps.wait-review.outputs.status` is either `success` or `completed_with_findings` at lines 1649-1652. That means a genuine workflow failure can be reclassified as acceptable and pushed into the editor-canary phase, where the root cause is likely to be misattributed.
  - **Recommended fix** — Split review outcomes into distinct terminal states: `success`, `review_found_issues`, `workflow_failed`, and `timed_out`. Only allow `verify-bait-removed` to run after `success` or an explicitly-reviewed `review_found_issues` state that still guarantees the editor/commit path finished. If the reviewer-majority shortcut must remain, gate it on a completed reviewer phase marker from the review workflow rather than log-count inference alone.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`
  - **File path** — `.github/workflows/review_autofix.yml:505-514,1355-1369,3771-3778,3892-3899,4626-4633`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — `review_autofix` fetches the PR title/body once into `PR_PAYLOAD_FILE` and normalizes it into `PR_META_FILE` at lines 1355-1369, but later re-fetches the same `repos/.../pulls/${PR_NUMBER}` payload in three linked-issue fallback paths and the post-merge validate-dispatch fallback at lines 505, 3775, 3896, and 4630. Current call count on the exercised path is 5 PR fetches for the same fields; proposed call count is 1. This is a pure missing-cache-reuse problem because the normalized metadata already exists on disk.
  - **Recommended fix** — Make `PR_META_FILE` the only source of PR title/body text for all fallback parsing, and delete the later `gh api repos/.../pulls/${PR_NUMBER}` calls. If a richer one-shot fetch is needed, extend `scripts/gh_helpers.sh::gh_pr_with_all_comments()` to emit the normalized `meta` block once and pass that artifact between steps.

- **ID** — `BATCH-001`
  - **File path** — `.github/workflows/review_autofix.yml:497-502,519-529`
  - **Severity** — Medium
  - **Category tag** — `api-batching`
  - **Description** — The post-merge validate-dispatch step already issues a GraphQL query that returns each linked issue number plus its labels at lines 497-502, but the fallback JSON deliberately sets `labels: null` at line 514 and the loop then performs `gh issue view ... --json labels` at line 523 for every issue whose labels are unknown. Current call count is 1 GraphQL call plus up to `N` per-issue REST calls; proposed call count is 1 batched call. This is exactly the per-iteration API pattern CLAUDE.md §15 warns against.
  - **Recommended fix** — Keep labels populated in the fallback object as an empty array rather than `null`, or issue one alias-based GraphQL follow-up for all fallback issue numbers before entering the loop. The closest existing batching pattern to extend is `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql()`, which already batches issue-number lookups with labels.

- **ID** — `API-002`
  - **File path** — `.github/workflows/issue_pr_status.yml:295-349,503-512`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — `issue_pr_status` already batches linked-issue labels and bodies into `ORCH_RESP` at lines 295-349 to classify tracking vs managed issues, but the later Telegram-alert suppression step re-fetches each linked issue body individually via `_safe_gh_jq "repos/.../issues/${issue_number}" --jq '.body // ""'` at line 507. Current call count is 1 batched GraphQL query plus up to `N` body fetches; proposed call count is 1. The later loop can derive the same orchestrator-managed answer from `MANAGED_ISSUES` or the earlier body-bearing batch result.
  - **Recommended fix** — Persist a single boolean such as `HAS_ORCHESTRATED_LINKED_ISSUE` or reuse `MANAGED_ISSUES` from the earlier batch classification instead of re-reading every issue body. If the step needs richer issue metadata later, extend the cached object using the batching pattern already implemented in `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql()`.

- **ID** — `API-003`
  - **File path** — `.github/workflows/test-and-mark-stable.yml:1248-1525`
  - **Severity** — High
  - **Category tag** — `api-redundancy`
  - **Description** — The main `wait-review` loop issues overlapping API reads every poll iteration: workflow-run discovery at lines 1285-1290, jobs at 1353 and possibly again at 1496/1516, live job logs at 1408 and again for byte-size at 1520-1521, PR head state at 1502-1503, and PR review comments at 1506-1507. Once `ELAPSED >= 600` and the run is in progress, the loop can hit up to 6 GitHub API calls per iteration for one review run. Proposed call count after consolidation is 3-4 per iteration by fetching jobs/logs once, deriving both the shortcut checks and size from the same artifact, and reducing unchanged PR/comment polling. This is the line-level source of the GH API hotspot already visible in the existing report.
  - **Recommended fix** — Turn the loop into a single per-iteration snapshot helper that writes `actions/runs`, `jobs`, `logs`, and PR state to temp files once and derives all signals from those cached blobs. Reuse the retry/file pattern in `scripts/gh_helpers.sh::gh_api_json_to_file()` for one-fetch-per-endpoint-per-iteration behavior, and remove the second `/actions/jobs/{id}/logs` fetch by computing both grep matches and byte size from the same downloaded log file.

- **ID** — `API-004`
  - **File path** — `.github/workflows/test-and-mark-stable.yml:1966-1991`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — The retry poller sleeps a fixed 15 seconds and then re-checks both PR state and workflow runs until a 25-minute deadline. That yields up to 100 `actions/workflows/.../runs` lookups and roughly 100 PR-state lookups on one retry path before completion or timeout. Current call count is therefore about 200 read calls on the worst-case path; proposed call count is about 50 with a stepped interval (for example 15s warm-up, then 30s/60s backoff after the first few minutes). This is a tight polling loop with no adaptive backoff despite being a long wait path.
  - **Recommended fix** — Extract a reusable poll helper that supports phased intervals and cached PR-state reuse, then apply it here. The retry/backoff primitives already exist in `scripts/gh_helpers.sh`; this path should extend them with a poll scheduler rather than hard-code `sleep 15` for the full 25-minute window.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`
  - **File path** — `.github/workflows/clarify.yml:53-126; .github/workflows/plan.yml:82-157; .github/workflows/implement.yml:228-301; .github/workflows/orchestrate_clarify_respond.yml:88-160; .github/workflows/validate.yml:72-145`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — The same “resolve integration ref” bootstrap block appears in five workflows: set `resolver_repo/resolver_ref`, stage a temporary clone, inject an authenticated git wrapper, fetch/checkout the canonical ref, and then run `scripts/resolve_integration_ref.sh`. The bodies are near-identical, including the same fallback logging and cleanup trap. This is high-risk duplication because a bug or auth/logging fix must be copied across five large workflows.
  - **Recommended fix** — Move the bootstrap into a shared shell helper, e.g. `scripts/resolve_integration_ref_bootstrap.sh`, with a signature like `resolve_integration_ref_bootstrap <issue_number> <repo_full_name> <current_sha>`. Update the five workflows to source or invoke that helper and only consume the emitted ref/output values.

- **ID** — `DUP-002`
  - **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/orchestrate_poll.yml:67-100; .github/workflows/mark-stable.yml:308-335,457-484; .github/workflows/review_autofix.yml:1273-1305; .github/workflows/test-and-mark-stable.yml:4403-4429`
  - **Severity** — Low
  - **Category tag** — `duplication`
  - **Description** — Multiple workflows re-embed their own `_rl_wait` / `_gh_retry` wrappers even though the repository already has centralized GH retry primitives in `scripts/gh_helpers.sh` (`_gh_rate_limit_wait`, `_safe_gh_jq`, `gh_api_json_to_file`, and related helpers). The duplicated wrappers differ slightly in breaker handling, stderr capture files, and backoff behavior, which makes future rate-limit fixes easy to land inconsistently.
  - **Recommended fix** — Standardize on `scripts/gh_helpers.sh` everywhere and, where checkout timing is the blocker, fetch only that helper first or ship a tiny inline bootstrap that sources it. The shared surface should be one of: `gh_retry <cmd...>`, `gh_retry_to_file <outfile> <cmd...>`, or `gh_api_json_to_file <outfile> gh api ...`. Callers above should be migrated to those helpers instead of maintaining local clones.

- **ID** — `DUP-003`
  - **File path** — `.github/workflows/review_autofix.yml:504-514,3771-3783,3892-3904,4626-4638`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — The same fallback regex block for extracting linked issue numbers from PR title/body is duplicated four times inside `review_autofix`, including identical `REPOSITORY_ESCAPED`, `grep -oiE`, `grep -oE '[0-9]+$'`, and “Found linked issues via PR body/title fallback” handling. This is brittle duplication inside one already-large workflow; any regex bug fix or keyword expansion must be edited in four places.
  - **Recommended fix** — Extract a helper such as `extract_linked_issue_numbers_from_pr_text <repo> <pr_meta_file>` into `scripts/gh_helpers.sh` or a new `scripts/issue_link_helpers.sh`, returning newline-delimited issue numbers. Then replace all four inline blocks with that single helper and keep `PR_META_FILE` as the only input source.

### Section 4: Expression Size Limit Risk Assessment

Counts below are lower-bound source-length measurements of `run:` blocks that contain `${{ }}` interpolation; the compiled template expression GitHub evaluates will be at least this large.

- **ID** — `EXPR-001`
  - **File path** — `.github/workflows/test-and-mark-stable.yml:1188-1571`
  - **Severity** — High
  - **Category tag** — `expression-limit`
  - **Description** — The interpolated `wait-review` run block is approximately 23,499 characters before template compilation, which is already 2,499 characters over GitHub Actions’ 21,000-character expression limit. This block is in the exact class of failure the repo has already hit before, and because the source length alone exceeds the limit, it has no remaining headroom.
  - **Recommended fix** — Extract the entire wait loop into a checked-in script under `scripts/`, pass only the few required inputs through `env`, and keep the workflow-side `run:` body to a small launcher. This is the preferred fix over further inlining because the block is already over the hard limit.

- **ID** — `EXPR-002`
  - **File path** — `.github/workflows/test-and-mark-stable.yml:1658-2062`
  - **Severity** — High
  - **Category tag** — `expression-limit`
  - **Description** — The interpolated `verify-bait-removed` run block is approximately 21,288 characters, leaving negative headroom of about 288 characters against the hard 21,000-character limit. Minor prompt text or logging additions are enough to make the workflow reject at load time with `Exceeded max expression length 21000`.
  - **Recommended fix** — Move the verification/retry logic into an external script and keep only argument wiring in the workflow. If extraction must be staged, split the step into separate “fetch canary,” “run pytest,” and “dispatch retry/poll retry” steps so no single expression remains near the limit.

- **ID** — `EXPR-003`
  - **File path** — `.github/workflows/review_autofix.yml:1271-1592`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Collect PR metadata` run block is approximately 16,437 characters, leaving about 4,563 characters of headroom. It is below the hard limit but already above the 15,000-character medium-risk threshold, and it continues to accumulate bootstrap, retry, GraphQL, diff, and file-rendering logic in one interpolated step.
  - **Recommended fix** — Split this into separate steps or move the GH-fetch/bootstrap portion into `scripts/` so the workflow-side interpolation only passes environment and file paths. Reusing `scripts/gh_helpers.sh` from a script entrypoint would remove most of the inline bulk.

- **ID** — `EXPR-004`
  - **File path** — `.github/workflows/validate.yml:189-481`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The `Fetch workflow support files` run block is approximately 16,512 characters, leaving about 4,488 characters of headroom. The block already mixes interpolation, remote-file bootstrap, fallback copying, and prompt/template installation in one expression-heavy step.
  - **Recommended fix** — Extract the support-file bootstrap into a dedicated script, or split remote-fetch, local-fallback, and prompt/template install into separate steps. This will also reduce duplication with similar bootstrap code in other workflows.

- **ID** — `EXPR-005`
  - **File path** — `.github/workflows/orchestrate_clarify_respond.yml:800-1082`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — The interpolated auto-answer run block is approximately 15,140 characters, leaving about 5,860 characters of headroom. It is below the high-risk threshold but already above the medium-risk threshold, so routine prompt or telemetry additions can push it toward the same limit the repository has already hit elsewhere.
  - **Recommended fix** — Move the auto-answer/comment-processing logic into an external shell or Python script and keep the workflow step as a short wrapper. If prompt text is the main growth source, store the larger literals under `prompts/` and load them at runtime.

No workflow file exceeds the 800 KB soft-warning threshold. The largest audited workflow is `.github/workflows/review_autofix.yml` at 279,722 bytes, followed by `.github/workflows/test-and-mark-stable.yml` at 264,999 bytes.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`
  - **File path** — `scripts/memory_helpers.sh:172-191,226-234`
  - **Severity** — Low
  - **Category tag** — `dead-code`
  - **Description** — `memory_processed_command_list()` and `memory_promote()` are defined in `scripts/memory_helpers.sh`, but repository-wide search in the audited scope found only their definition sites and no callers. Unused helper surface in a shared shell library increases maintenance cost and makes it harder to know which AI-memory paths are still live.
  - **Recommended fix** — Either remove these functions if they are obsolete, or add the intended call sites and tests so the interface is exercised. If they are kept for planned use, document that explicitly near the definitions to avoid future dead-code confusion.

- **ID** — `DEAD-002`
  - **File path** — `scripts/orchestrate_poll_process.sh:4747-4753,4860-4868`
  - **Severity** — Low
  - **Category tag** — `dead-code`
  - **Description** — `read_standalone_state_json()` and `stall_recovery_action_is_terminal()` are present in `scripts/orchestrate_poll_process.sh`, but repository-wide search found no call sites outside their own definitions. In a 10k+ line shell script, unused helpers add cognitive load and suggest the stall-recovery surface has drifted from the code that actually executes.
  - **Recommended fix** — Remove the unused functions or wire them into the current stall-recovery path if they still represent intended behavior. Given the script size, pruning unused helpers is preferable unless an imminent caller already exists.

- **ID** — `CONSIST-001`
  - **File path** — `.github/workflows/validation-refresh.yml:158-174; .github/workflows/issue_pr_status.yml:531-537; .github/workflows/update_workflows.yml:451-457`
  - **Severity** — Low
  - **Category tag** — `consistency`
  - **Description** — These workflows still use inline raw `curl .../sendMessage` paths even though the repo already centralizes Telegram transport and alert-level behavior in `scripts/tg_helpers.sh:103-129`. The inconsistency is visible in `validation-refresh`, which has to re-implement explicit `ALERT_MSG_LEVEL` gating because it bypasses the helper. This duplication makes formatting, suppression rules, and future transport changes easy to apply unevenly.
  - **Recommended fix** — Add a shared helper such as `tg_send_untracked <msg> <level>` or `tg_send_forced_untracked <msg> <level>` to `scripts/tg_helpers.sh`, then replace the raw `curl` blocks above with that helper. That preserves intentional “untracked” semantics without copying Telegram HTTP details into workflows.

- **ID** — `SHELL-001`
  - **File path** — `scripts/check_external_branch_advance.sh:141-153,180-183`
  - **Severity** — Low
  - **Category tag** — `shellcheck`
  - **Description** — The script builds `self_subject_shas` as a space-delimited string at lines 141-153 and later iterates it with `for sha in ${self_subject_shas}; do` at line 180. Today the values are commit SHAs, so practical breakage is unlikely, but this is still a shell word-splitting pattern that ShellCheck would flag and it makes the code depend on formatting rather than data structure.
  - **Recommended fix** — Store SHAs in an array (`self_subject_shas+=("${sha}")`) and iterate with `for sha in "${self_subject_shas[@]}"; do`. That removes SC2086-style splitting risk and matches the script’s otherwise careful fail-open behavior.

No `TODO`, `FIXME`, or `HACK` markers were present in the audited workflow/script scope.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, API-003, EXPR-001, EXPR-002 |
| Medium | 9 | API-001, BATCH-001, API-002, API-004, DUP-001, DUP-003, EXPR-003, EXPR-004, EXPR-005 |
| Low | 5 | DUP-002, DEAD-001, DEAD-002, CONSIST-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Large |
| API call optimization | 5 | Medium |
| Code modularization | 10 | Large |
| Expression size reduction | 6 | Large |
| Medium/Low fixes | 7 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-05-07)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation can be implemented directly because the same data is fetched in the same code path with matching filters, retries, and failure semantics. `NEEDS_VERIFICATION` means the overlap is real, but a human or follow-up audit must confirm edge cases before changing behavior. `RISKY_SKIP` means the redundancy is visible, but the call sits in a retry/poll/race-sensitive path or another protected context where this pass does **not** authorize auto-implementation.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/issue_pr_status.yml:188-193` and `.github/workflows/issue_pr_status.yml:295-349`
  - **Current call count** — `2` GraphQL calls on the normal `closingIssuesReferences` path before fallback
  - **Proposed call count** — `1` GraphQL call on that path
  - **Endpoint(s)** — GitHub GraphQL `/graphql` (`repository.pullRequest(number).closingIssuesReferences` and later aliased `issue(number: N)` lookups)
  - **Evidence** — The first query discovers linked issue numbers, then the same step immediately rebuilds metadata for those same issue numbers via a second GraphQL query.
    ```yaml
    ISSUE_NUMBERS="$(gh_retry gh api graphql \
      ...
      -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
      --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
    ```
    ```yaml
    ORCH_ALIAS_FRAGMENT+=" i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }"
    ...
    ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
    ```
    The second query exists only because the first query omits `labels` and `body`, even though the classification logic immediately consumes exactly those fields.
  - **Proposed fix** — In the `Update linked issue labels when PR closes` step, extend the first `closingIssuesReferences` query to return `nodes { number labels(first: 50) { nodes { name } } body }`, compute `ISSUE_NUMBERS`, `TRACKING_ISSUES`, and `MANAGED_ISSUES` from that single payload, and keep a follow-up only for a branch-derived `BRANCH_ISSUE_NUMBER` that is not already present in the GraphQL response.
  - **Safety rationale** — The overlap is strong and occurs in one workflow step, but the branch-name-derived issue union and fail-open fallback behavior mean equivalence is not fully provable from static reading alone.
  - **Downstream signal** — Verify with sample PR-close events that branch-derived issue numbers are still classified correctly when they are absent from `closingIssuesReferences`, then replace the second GraphQL query.

- **ID** — `MERGE-002`
  - **Safety tag** — `RISKY_SKIP`
  - **File path and line ranges** — `.github/workflows/cancel_on_pr_close.yml:68-89`
  - **Current call count** — `2` paginated REST list calls
  - **Proposed call count** — `1` broader list call, if manually approved
  - **Endpoint(s)** — GitHub REST `GET /repos/{repo}/actions/runs`
  - **Evidence** — The step issues the same paginated run-list call twice, differing only by `status=queued` vs `status=in_progress`.
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
  - **Proposed fix** — Manual-only option: evaluate whether a single `actions/runs` fetch with the existing `branch` and `event` filters, followed by local `status` filtering, preserves the cancellation target set and payload bounds.
  - **Safety rationale** — Both calls use `--paginate` inside a cancellation path, so merging them would change page-boundary and response-size semantics, which this audit must treat as `RISKY_SKIP`.
  - **Downstream signal** — Do **not** auto-implement; manually test against branches with many completed `pull_request` runs to prove queued/in-progress runs are still discoverable within the fetched pages and that existing cancellation logs stay unchanged.

- **ID** — `MERGE-003`
  - **Safety tag** — `RISKY_SKIP`
  - **File path and line ranges** — `scripts/orchestrate_poll_process.sh:176-183`, `scripts/orchestrate_poll_process.sh:3393-3394`, `scripts/orchestrate_poll_process.sh:3448-3450`, `scripts/orchestrate_poll_process.sh:3476-3479`, and `scripts/orchestrate_poll_process.sh:3499-3501`
  - **Current call count** — `6` `GET /pulls/{final_pr}` reads on the existing-final-PR pre-merge path, before any failed-merge refresh
  - **Proposed call count** — `2` on that path (`1` cached pre-merge snapshot + `1` post-merge refresh only if needed)
  - **Endpoint(s)** — GitHub REST `GET /repos/{repo}/pulls/{pull_number}`
  - **Evidence** — The final-merge path re-reads the same PR multiple times for adjacent fields, then `_pr_checks_completed` does another PR fetch because no head SHA is passed.
    ```bash
    existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    ...
    pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
    pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    ```
    ```bash
    if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
      pr_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" || echo "")"
      head_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' ...)"
    fi
    ```
  - **Proposed fix** — Manual-only: in `finalize_integration_merge_if_needed()`, fetch one cached PR snapshot containing `.state`, `.mergeable`, `.merged_at`, and `.head.sha`, pass that `head.sha` into `_pr_checks_completed "${final_pr}" "${head_sha}"`, and only refresh the snapshot once after a failed merge attempt.
  - **Safety rationale** — This code is inside `scripts/orchestrate_poll_process.sh` on a race-defensive merge/finalize path, which the audit contract explicitly marks as `RISKY_SKIP` for auto-consolidation.
  - **Downstream signal** — Do **not** auto-implement; manual review must confirm unchanged behavior across mergeable=`true|false|null`, post-state-comment timing, and all existing `[final-merge]` decision branches.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/review_autofix.yml:1385-1445` and `.github/workflows/review_autofix.yml:1836-1840`
  - **Current call count** — `1` linked-issues GraphQL fetch plus `1` later issue-title REST fetch on the smoke-detection path
  - **Proposed call count** — `1` fetch on that path
  - **Endpoint(s)** — GitHub GraphQL `/graphql` (`closingIssuesReferences.nodes { number title body }`) and GitHub REST `GET /repos/{repo}/issues/{issue_number}`
  - **Evidence** — The metadata step already fetches linked issue titles and bodies, then `Detect smoke test and tune LLM settings` re-fetches the linked issue title via REST.
    ```bash
    if gh_retry "${_linked_tmp}" api graphql \
      ...
      -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
      --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
    ```
    ```python
    lines.append(f"Issue #{num}: {title}")
    if body:
        lines.append(body)
    ```
    ```bash
    ISSUE_NUM=$(echo "${PR_BODY}" | grep -oiPm1 '\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s+(?:https?://[^[:space:]]+/${{ github.repository }}/issues/|#)\K\d+\b' || true)
    if [ -n "${ISSUE_NUM:-}" ]; then
      ISSUE_TITLE=$(_safe_gh_jq "repos/${{ github.repository }}/issues/${ISSUE_NUM}" --jq '.title // ""' || echo "")
    fi
    ```
  - **Proposed fix** — Extend `Collect PR metadata` to persist the machine-readable linked-issues payload to a durable file (for example `LINKED_ISSUES_DETAILS_FILE`), then update `Detect smoke test and tune LLM settings` to read linked issue titles from that cache first and fall back to `_safe_gh_jq` only when the earlier GraphQL fetch failed or produced no linked-issue details.
  - **Safety rationale** — The data already exists earlier in the same job, but the later step’s regex-based fallback and the earlier step’s fail-open GraphQL behavior need explicit validation before the REST read can be removed.
  - **Downstream signal** — Verify smoke detection on PRs where `closingIssuesReferences` is empty but the PR body regex still resolves an issue number; keep the REST fallback only for that verified miss path.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/issue_pr_status.yml:173-182` and `.github/workflows/issue_pr_status.yml:205-208`; caller context: `.github/workflows/internal-issue-pr-status.yml:3-12`, `workflow-templates/ai-issue-pr-status.yml:5-12`
  - **Current call count** — `1` conditional PR fetch on the fallback branch
  - **Proposed call count** — `0` for the in-repo wrapper callers
  - **Endpoint(s)** — GitHub REST `GET /repos/{repo}/pulls/{pull_number}`
  - **Evidence** — The reusable workflow already seeds `PR_TITLE` and `PR_BODY` from the `pull_request` event payload, but still contains a fallback API fetch that only runs if that combined string is blank.
    ```yaml
    env:
      PR_NUMBER: ${{ github.event.pull_request.number }}
      PR_HEAD_REF: ${{ github.event.pull_request.head.ref }}
      PR_BASE_REF: ${{ github.event.pull_request.base.ref }}
      PR_MERGED: ${{ github.event.pull_request.merged }}
      PR_TITLE: ${{ github.event.pull_request.title }}
      PR_BODY: ${{ github.event.pull_request.body || '' }}
    ```
    ```bash
    PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
    if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
      PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
    fi
    ```
    In this repository, both shipped wrappers invoke the reusable workflow from `pull_request: types: [closed]`, so the event already carries the PR payload the step consumes.
  - **Proposed fix** — For the in-repo wrapper contract, remove the conditional `GET /pulls/{PR_NUMBER}` fallback and use `${PR_TITLE}` / `${PR_BODY}` only; if the reusable workflow still needs a non-event fallback for external callers, put that behind an explicit input/contract rather than an implicit hidden API read.
  - **Safety rationale** — The call appears dead for the repository’s own wrappers, but the reusable workflow surface may still have external consumers or undocumented payload assumptions that need confirmation.
  - **Downstream signal** — Confirm with sample `pull_request.closed` payloads and the intended `workflow_call` contract that `github.event.pull_request.title/body` are always populated for supported callers before deleting the fallback.

### Cross-References to Deep Audit Section

- API-001: `NEEDS_VERIFICATION` — The cached `PR_META_FILE` overlap is real, but the later fallback paths span separate jobs/branches and should be switched only after confirming identical fail-open behavior.
- BATCH-001: `NEEDS_VERIFICATION` — The per-issue label loop is a classic batching target, but the regex-derived fallback issue set needs runtime validation before replacing the current lookup path.
- API-002: `NEEDS_VERIFICATION` — The later issue-body re-fetch clearly overlaps earlier orchestrator classification data, but the alert-suppression step should only be rewired after confirming the cached metadata is still in scope there.
- API-003: `RISKY_SKIP` — This is a high-value redundancy, but it lives in a poll/race-sensitive release-gate wait loop where call-merging changes timing and observable diagnostics.
- API-004: `RISKY_SKIP` — This is an explicit retry poller with fixed cadence, so changing the read pattern/backoff is manual-only.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-001, DEAD-API-001 |
| RISKY_SKIP | 2 | MERGE-002, MERGE-003 |

### Implement-Stage Handoff

- No SAFE_TO_MERGE findings in this pass.
