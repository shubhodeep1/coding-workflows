## Executive Summary

- **`review_autofix` is the biggest speed-and-cost target.** In `shubhodeep1/coding-workflows`, the `review_autofix` family ran **79** times with **25 cancelled** runs, **713.3s avg**, **48s p50**, and **2615.3s p95**. A recent cancelled run, `25589012577` (`Internal: AI Review & Autofix`), reviewed a **1-file / 8-addition** `claude/*` PR in comment-only mode but still ran with `REVIEWER_REASONING_EFFORT=xhigh`, `EDITOR_REASONING_EFFORT=xhigh`, and was cancelled during `review / codex-agent (claude-branch-review) / Run reviewer models` after roughly **16 minutes**. **Estimated impact:** save **6-15 minutes** and a large share of reviewer tokens on affected runs. **Confidence:** **high**.

- **CI is dominated by one serial test file, and it is a low-risk optimization.** In `CI` run `25589012524`, `lint` took **601s**, and `python3 tests/test_orchestrate_poll_process.py` alone consumed about **515s** (`02:22:27` → `02:31:03`) while reporting **157 passed**. **Estimated impact:** cut **3-5 minutes per CI run** by sharding that suite. **Confidence:** **high**.

- **`test_and_mark_stable` is too tightly coupled to a long child workflow.** Failed run `25568050819` (`Test & Mark Stable Release`) spent about **10,878s** polling child run `25568083133` in `orphan-workflows-test / Dispatch & watch — workflow-log-analysis`, with **715** observed status polls at a fixed **15s** interval, before the child ended `conclusion=failure`. **Estimated impact:** remove up to **~3 hours** from failed release paths and reduce watcher API calls by **75-99%** depending on how aggressively the watcher is decoupled. **Confidence:** **high**.

- **A real `workflow_log_analysis` failure was observed, but the repo snapshot already contains the mitigation.** Failed run `25568083133` (`Workflow Log Analysis`) hit `CONFLICT (add/add)` in `analyze-commit-notify / Commit and push report` on `analysis/workflow-optimization-2026-05-08-3.md`. The current `.github/workflows/workflow-log-analysis.yml` now includes a **retry-with-rename** path for the base report and **patch-replay (`git format-patch` + `git am --3way`)** retries for the append jobs. **Estimated impact:** avoids repeat report-push failures and downstream reruns if the new logic holds under concurrency. **Confidence:** **high**.

- **AI memory and prompt cache are enabled, but observability is the bigger issue than raw feature availability.** In the sampled deep-dive logs, I observed **58** JSON `AI_MEMORY_TELEMETRY` lines, but `retrieve` hit only **1/14 = 7.1%** of the time; **13/14** retrieves returned zero records. Separately, **92** sampled `review_autofix_cache_probe` lines all reported `cache_enabled=true`, but `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` were all `na`. **Estimated impact:** medium once telemetry is repaired; current savings cannot be quantified confidently. **Confidence:** **high**.

- **Do not spend API-optimization effort on `issue_pr_status` first.** Recent run `25589386854` (`Internal: Issue-PR Status Sync`) finished in **9s**, and source inspection of `.github/workflows/issue_pr_status.yml` shows GraphQL batching via `closingIssuesReferences` plus batched orchestrator classification, with REST fallback only when needed. **Estimated impact:** low from further tuning there; focus should stay on `test_and_mark_stable`, `review_autofix`, and multi-job review flows. **Confidence:** **high**.

## Speed Optimizations

### 1. Shard `tests/test_orchestrate_poll_process.py` in CI
**Type:** Critical-path win  
**Expected latency reduction rank:** #1  
**Implementation risk:** Low

- **Evidence:** In `CI` run `25589012524`, the `lint` job took **601s** total, and the step running `python3 tests/test_orchestrate_poll_process.py` consumed about **515s** (`02:22:27` → `02:31:03`) before printing `157 passed, 0 failed, 157 total`.
- **Root cause:** One serial, monolithic test file dominates the whole job.
- **Exact change:** In `.github/workflows/ci.yml`, replace the single `python3 tests/test_orchestrate_poll_process.py` invocation with **2-4 shards**. The safest implementation is explicit function lists or prefix-based groups, because the repo already demonstrates per-test invocation from the same file inside `.github/workflows/test-and-mark-stable.yml`.
- **Estimated time savings:**  
  - **2 shards:** likely reduce the suite wall time from ~515s to ~260-300s.  
  - **4 shards:** likely reduce it to ~130-180s.  
  - Overall CI could move from **~601s** toward **~220-300s**.  
  These are **inferences** based on the observed serial runtime.
- **Why this is low risk:** The test file is already exercised heavily and independently; this is a packaging change, not a logic change.

### 2. Add a cheaper review profile for tiny `claude/*` branch-review runs
**Type:** Critical-path win  
**Expected latency reduction rank:** #2  
**Implementation risk:** Low-Medium

- **Evidence:** `review_autofix` has the largest long-tail variance in the sample: **79 runs**, **713.3s avg**, **48s p50**, **2615.3s p95**, **25 cancelled**. In run `25589012577`, the gate logged:
  - `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW pr=2340 ... editor/commit/judge/auto-merge skipped.`
  - `AUTOFIX_GATE_DET_SKIP_EVAL pr=2340 files=1 additions=8 ... skip=false`
  - but the reviewer phase still used:
    - `REVIEWER_REASONING_EFFORT: xhigh`
    - `EDITOR_REASONING_EFFORT: xhigh`
    - `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`
    - `CHECK_RUNS_POLL_INTERVAL_SECS: 20`
  and was cancelled in `review / codex-agent (claude-branch-review) / Run reviewer models` after about **16 minutes**.
- **Source evidence:** `.github/workflows/review_autofix.yml` defaults both reviewer and editor reasoning to **`xhigh`**, and even the pass-2 “small vs large diff” branches default to **`xhigh`**, so the diff-size gate is effectively a no-op at default settings.
- **Root cause:** The workflow spends its full expensive review budget even on **comment-only**, **tiny-diff**, `claude/*` runs.
- **Exact change:** Reuse the already-existing “Detect smoke test and tune LLM settings” pattern in `.github/workflows/review_autofix.yml` for a second fast path:
  - trigger it when `CLAUDE_BRANCH_REVIEW=true` and the diff is tiny,
  - set reviewer reasoning to `low`,
  - set editor reasoning to `medium` (mostly harmless here because editor is skipped),
  - reduce `XPOLL_SUMMARISER_LINES_PER_REVIEWER` (for example **160 → 80**),
  - and add a **pre-LLM stale-head check** right before `Run reviewer models`.
- **Estimated time savings:** **6-15 minutes** on affected tiny-diff comment-only reviews. This is an **inference**, but the observed 16-minute cancelled review on a 1-file / 8-addition PR makes the direction clear.
- **Why risk is acceptable:** This path already skips editor/commit/judge/auto-merge. Lowering reasoning on comment-only branch-review output is much safer than lowering reasoning for full autofix runs.

### 3. Remove `workflow_log_analysis` from the stable-release critical path, or at least stop watching it synchronously to completion
**Type:** Critical-path win  
**Expected latency reduction rank:** #3  
**Implementation risk:** Medium

- **Evidence:** `Test & Mark Stable Release` run `25568050819` failed in `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` after **10,962s** total. The watcher:
  - printed `Watching run #25568083133`,
  - observed **715** actual `status=... conclusion=...` polls,
  - ran from `16:52:25` to `19:53:38` (~**10,878s**),
  - and ended with child `conclusion=failure`.
- **Source evidence:** `.github/workflows/test-and-mark-stable.yml` sets:
  - step `timeout-minutes: 265`,
  - `DEADLINE=$(( $(date +%s) + 15600 ))`,
  - fixed `sleep 15`,
  - and polls `gh api "repos/${TEST_REPO}/actions/runs/${NEW_ID}"`.
- **Root cause:** A long diagnostic/reporting workflow is being treated like a synchronous release gate.
- **Exact change:** Preferred:
  - dispatch the child workflow,
  - capture the child `run_id`,
  - post it to the tracking issue / release log,
  - and **continue** once the child is registered or starts running.
  
  If full decoupling is not acceptable, then at minimum:
  - keep 15s polling only until the child leaves `queued/pending`,
  - then back off to **60s**.
- **Estimated time savings:**  
  - **Full decoupling:** up to **~181 minutes** on bad cases like `25568050819`.  
  - **Backoff only:** minimal wall-time gain, but major API-call reduction.
- **Risk note:** This changes release semantics, so make it opt-in via a repository variable first if needed.

### 4. Reduce multi-job startup and artifact handoff overhead in Copilot code review
**Type:** Micro-optimization  
**Expected latency reduction rank:** #4  
**Implementation risk:** Medium

- **Evidence:** In `Copilot code review` run `25589012859` (**240s**), `Prepare/system`, `Agent/system`, and `Upload_results/system` each showed hosted-runner waits, and cleanup enumerated artifacts through `gh api /repos/shubhodeep1/coding-workflows/actions/runs/25589012859/artifacts`. In another recent run, `25587222088` (log summary only), `Cleanup artifacts` alone accounted for roughly **178s** of a **221s** run.
- **Root cause:** Several short jobs each pay their own queue/startup cost and move artifacts between jobs.
- **Exact change:** When that workflow source is available, collapse:
  - `Prepare` + `Agent`, and/or
  - `Upload results` + `Cleanup artifacts`,
  while keeping cleanup best-effort.
- **Estimated time savings:** **30-180s** per run, depending on runner queue time.
- **Evidence caveat:** This recommendation is **log-based only**; the `copilot_pull_request_reviewer` workflow source is not present in this repo snapshot.

## Cost Optimizations

### 1. Extend the existing “skip or cheapen redundant review” policy to tiny `claude/*` comment-only runs
**Expected savings rank:** #1  
**Quality-risk note:** Low-Medium

- **Evidence:** The repo already has a cost-saving skip path for self-triggered autofix runs: `review_autofix` run `25588866605` exited in **6s** with `AUTOFIX_GATE_SKIP reason=self_triggered_autofix`. But recent tiny-diff `claude/*` run `25589012577` still incurred a full expensive reviewer pass with:
  - six `REVIEWER_MODELS`,
  - `REVIEWER_REASONING_EFFORT=xhigh`,
  - summariser enabled,
  - cache probe present,
  - and cancellation after ~16 minutes.
- **Root cause:** The workflow has a good “skip redundant self-triggered autofix” policy, but that policy does not extend to **comment-only tiny-diff branch reviews**.
- **Exact change:** Keep the six-model panel initially, but lower reasoning and summariser input volume on `CLAUDE_BRANCH_REVIEW=true` tiny-diff runs. This preserves model diversity while attacking the most obvious token multiplier.
- **Estimated savings:** **30-60% reviewer-token reduction** on affected runs is a reasonable **inference**, especially because the current path is paying `xhigh` reasoning across a six-model panel for a 1-file / 8-addition review.
- **Why I am not recommending a model downgrade first:** For general `review_autofix`, model diversity may still be doing useful work. The first, safer knobs are reasoning level and prompt size.

### 2. Lower the default scope of `summarize_unselected_runs` instead of downgrading its model
**Expected savings rank:** #2  
**Quality-risk note:** Medium

- **Evidence:** Two `workflow_log_analysis` deep-dive runs emitted explicit token telemetry for `summarize_unselected_runs`:
  - run `25568083133`: **100/100** runs summarized, **121,044 tokens**, model `openai/gpt-5.4-mini`
  - run `25567987209`: **97/100** runs summarized, **148,158 tokens**, model `openai/gpt-5.4-mini`
  
  Source defaults in `.github/workflows/workflow-log-analysis.yml` are:
  - `WORKFLOW_LOG_SUMMARY_MAX_RUNS=100`
  - `WORKFLOW_LOG_SUMMARY_TOKEN_BUDGET=1500000`
- **Root cause:** The summarizer is already using a relatively small model, but it is being asked to summarize a wide long tail on every analysis run.
- **Exact change:** Lower the default `WORKFLOW_LOG_SUMMARY_MAX_RUNS` from **100** to something like **40-60**, and prioritize:
  1. failures,
  2. cancellations,
  3. p95 outliers,
  4. latest unselected runs,
  5. previously unseen workflow families.
- **Estimated savings:** Roughly **40-60% fewer summarization tokens per `workflow_log_analysis` run**. Based on observed runs, that is about **48k-89k tokens saved** each time.
- **Quality-risk note:** Medium. Coverage of “boring” long-tail successes will shrink, so keep failures, cancels, and recent outliers pinned into the retained set.

### 3. Prevent rerun cascades from concurrency collisions and release-child coupling
**Expected savings rank:** #3  
**Quality-risk note:** Low

- **Evidence:** The observed pair of failures:
  - `workflow_log_analysis` run `25568083133` lasted **10,867s**
  - `test_and_mark_stable` run `25568050819` lasted **10,962s**
  
  Together they burned **21,829 workflow seconds** before terminating unsuccessfully.
- **Root cause:** One failure (`CONFLICT (add/add)` during report push) cascaded into another workflow that was synchronously waiting on it.
- **Exact change:** Two-part:
  1. keep and verify the landed `workflow_log_analysis` rename-retry fix,
  2. stop making the stable-release watcher hard-fail on a diagnostic child workflow.
- **Estimated savings:** Avoiding a single recurrence saves **hours of workflow time** plus associated model/API spend.
- **Quality-risk note:** Low, provided the child workflow still posts its result somewhere visible.

### 4. Fix prompt-cache telemetry before attempting cache “optimizations”
**Expected savings rank:** #4  
**Quality-risk note:** Low

- **Evidence:** Across **92 sampled `review_autofix_cache_probe` log lines** in the parsed deep-dive set, all had `cache_enabled=true`, but all meaningful counters were `na`:
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
  
  Source inspection showed:
  - `scripts/review_run_reviewers.sh` emits `na` when usage payloads are absent,
  - `scripts/openrouter_prompt_cache.py` already contains usage normalization logic.
- **Root cause:** The system cannot yet measure whether the prompt cache is helping.
- **Exact change:** Preserve and log the raw provider `usage` payload for reviewer/editor calls, then run it through the normalizer. Do this for real calls, not just the probe.
- **Estimated savings:** **Not quantifiable from this window**. The recommendation is a prerequisite for safe cost tuning, not a guaranteed direct saver.
- **Quality-risk note:** Low; this is observability work.

## Reliability Improvements

### 1. Validate the already-landed `workflow_log_analysis` concurrency fix under actual parallel load
**Rank:** #1 by expected rerun-rate reduction  
**Root cause category:** Concurrent write / Git rebase collision

- **Failure evidence:** `Workflow Log Analysis` run `25568083133` failed in `analyze-commit-notify / Commit and push report` after:
  - `REPORT_FILE="analysis/workflow-optimization-2026-05-08-3.md"`
  - `git pull --rebase origin "${TARGET_BRANCH}"`
  - `CONFLICT (add/add): Merge conflict in analysis/workflow-optimization-2026-05-08-3.md`
- **Current state in source:** The repo snapshot already includes:
  - `steps.commit_report.outputs.report_file`
  - retry-with-rename logic using `git mv` to the next free suffix
  - `push_max_attempts=5`
  - patch-replay retries (`git format-patch` + `git am --3way`) in the deep-audit and API-redundancy append jobs
- **Exact fix:** Do **not** redesign this path again yet. Instead:
  - add a concurrency regression test/canary that intentionally dispatches two same-branch `workflow-log-analysis` runs,
  - assert that both succeed and publish distinct final report paths,
  - verify downstream consumers use `commit_report.outputs.report_file`.
- **Expected reliability impact:** High. It directly targets the only observed `workflow_log_analysis` failure mode in the sample.
- **Rollback / fail-open:** The canary should warn first rather than block release flows until it has proven stable.

### 2. Stop letting a diagnostic child workflow fail the stable-release pipeline
**Rank:** #2 by expected failure-rate reduction  
**Root cause category:** Orchestration coupling / brittle dependency

- **Failure evidence:** `Test & Mark Stable Release` run `25568050819` failed because child `workflow_log_analysis` run `25568083133` failed. The parent watcher kept polling for ~**10,878s** and then ended with child `conclusion=failure`.
- **Exact fix:** Reclassify `workflow_log_analysis` as **advisory** for stable marking, or at minimum fail-open only for the report-generation/analysis branch of that child workflow while keeping direct smoke/integration tests blocking.
- **Expected reliability impact:** High. The sampled family has **2 total runs** and a current mix of **1 failure / 1 cancelled / 0 success**; even allowing for the tiny sample, the coupling is clearly brittle.
- **Rollback / fail-open:** Put the advisory mode behind a repo variable so it can be reverted if release owners want the old hard gate back.

### 3. Exit superseded `review_autofix` runs before reviewer-model fan-out starts
**Rank:** #3 by expected cancellation-waste reduction  
**Root cause category:** Late cancellation / stale-head execution

- **Failure/cancellation evidence:** `review_autofix` had **25 cancelled** runs out of **79**. Recent run `25589012577` was cancelled during `Run reviewer models` after ~16 minutes. Source inspection shows the workflow uses a concurrency group with `cancel-in-progress: ${{ needs.gate.outputs.claude_branch_review == 'true' }}`, so late cancellation on `claude/*` runs is expected behavior.
- **Exact fix:** Right before `Run reviewer models`, query the current PR head SHA and compare it to the initial `PR_HEAD_SHA`. If the head moved, exit cleanly as **superseded** before invoking reviewers.
- **Expected reliability impact:** Medium. This will not reduce hard failures, but it should materially reduce wasted cancelled work and make behavior more predictable.
- **Rollback / fail-open:** On API failure, keep today’s behavior and continue.

### 4. Clear the upcoming runtime deprecation risk now
**Rank:** #4 by expected future incident reduction  
**Root cause category:** Platform/runtime deprecation

- **Evidence:** In slow `review_autofix` run `25586809675`, the final warning reported that `actions/cache/restore@v4` and `actions/cache/save@v4` are still on **Node.js 20** and warned of future runner changes. Separately, `Copilot code review` runs `25589012859` and `25588867412` logged `Buffer()` deprecation warnings during artifact download.
- **Exact fix:** Upgrade or repin the affected actions/dependencies to Node 24–compatible versions, and validate them under the already-present `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` setting.
- **Expected reliability impact:** Medium. This is not the source of current failures, but it is a real future-breakage risk.
- **Rollback / fail-open:** Pin to the last known-good version if an upgrade regresses behavior, but timebox that fallback because the runner platform will keep moving.

## AI Memory Health

**Health verdict:** Available and fail-open-friendly, but currently low-yield.

### What I observed in the sampled deep-dive logs
Across the parsed deep-dive files, I observed **58** JSON `AI_MEMORY_TELEMETRY` lines with this operation mix:

| Operation | Count |
|---|---:|
| `record-run-event` | 25 |
| `retrieve` | 14 |
| `record-candidate` | 10 |
| `summarize_unselected_runs` | 5 |
| `processed-command-check` | 1 |
| `processed-command-claim` | 1 |
| `processed-command-complete` | 1 |
| `finalize-task` | 1 |

I did **not** observe sampled JSON `promote` or `compact` operations in the deep-dive set.

### Retrieve effectiveness
- **Retrieve hit rate:** **1 / 14 = 7.1%**
- **Average `estimated_tokens`:** **2**
- **Keyword method distribution:**
  - `none`: **13 / 14 = 92.9%**
  - `plain`: **1 / 14 = 7.1%**
  - `llm`: **0 / 14**
- **Zero-record retrieves:** **13**
- **`fail_open: true` entries:** **0 observed** on retrieve ops
- **`enabled: false` entries:** **0 observed**
- **Budget comparison:** a retrieval budget field was **not emitted** in the sampled retrieve JSON, so I cannot compare `estimated_tokens` against a formal budget.

### Concrete examples
- In `review_autofix` run `25589012577`, `review / codex-agent (claude-branch-review) / Retrieve reviewer memory context fail-open` emitted:
  - `enabled: true`
  - `estimated_tokens: 0`
  - `keyword_method: none`
  - `records_selected: 0`
- The **only** non-zero sampled hit I saw was an `implementation` retrieve with:
  - `keyword_method: plain`
  - `records_selected: 1`
  - `estimated_tokens: 28`

### Push retry health
- In the sampled deep-dive JSON, I saw **2 parsed `record-candidate` telemetry lines** with `push_attempts=2`.
- I did **not** see higher retry counts than that.

### Assessment
- The system appears **available**: telemetry is being emitted, retrieves are enabled, and I did not see sampled retrieve disables or fail-open retrieve errors.
- The system appears **low-value today**: most retrieves are returning nothing, and almost all are staying on `keyword_method=none`.
- **Inference:** average `estimated_tokens=2` is far below what would normally be useful contextual memory, which suggests the retrieval layer is rarely filling any meaningful context budget.

### Recommended next steps
1. **Add a second retrieval attempt using `plain` keywords when the initial `none` path returns zero records.** The only positive hit in the sample came from `plain`.
2. **Emit a retrieval budget field** alongside `estimated_tokens` so usefulness can be measured relative to available context.
3. **Track retrieve hit rate by workflow family and role** (`reviewer`, `implementation`, etc.), because the sampled window already suggests role-level differences.

## GH API Call Audit

**Overall audit result:** I did **not** observe sampled `HTTP 429` or secondary rate-limit incidents, so the problem is **volume and redundancy**, not an active rate-limit fire.

The repo already has some good API hygiene:
- `.github/workflows/issue_pr_status.yml` batches linked-issue and orchestrator metadata through GraphQL.
- `scripts/orchestrate_poll_process.sh` uses local `git merge-tree` for sibling conflict probing, which is the right zero-HTTP pattern.
- `review_autofix` already has a `gh_retry` wrapper.

### Highest-value call reductions

| Workflow / job / step | Evidence | Problem pattern | Concrete change | Estimated call reduction |
|---|---|---|---|---|
| `test_and_mark_stable` / `orphan-workflows-test` / `Dispatch & watch — workflow-log-analysis` | Run `25568050819` polled child run `25568083133` **715** times over ~**10,878s** | Fixed-interval per-run status polling | Prefer recording child `run_id` and continuing; if blocking must stay, switch from fixed `15s` to staged/exponential backoff | **75-99%** fewer watcher calls |
| `review_autofix` / `gate` + `codex-agent` | Slow run `25586809675` fetched `pulls/{PR}`, `commits/{PR_HEAD_SHA}`, and paginated `pulls/{PR}/files` in `gate`; `codex-agent` then re-fetched PR state/default-branch metadata | Duplicate metadata fetch across jobs | Pass `pr_state`, `pr_head_sha`, `default_branch`, and `pr_files_json` forward as outputs or a temp artifact instead of refetching | At least **3-6** API calls per run, plus one paginated file walk |
| `issue_pr_status` / `sync-status` | Recent run `25589386854` finished in **9s**; source already uses GraphQL `closingIssuesReferences` and batched orchestrator classification | This is already one of the cleaner paths | **No urgent batching rewrite recommended** | Minimal remaining gain |
| `copilot_pull_request_reviewer` / cleanup path | Run `25589012859` listed artifacts through `gh api /repos/.../actions/runs/25589012859/artifacts`; log summaries for `25587222088`/`25588867412` show cleanup and artifact handling dominate visible work | Artifact lifecycle overhead across multiple jobs | If workflow source confirms it is safe, reuse artifact IDs across steps and consider dropping explicit cleanup for short-retention ephemeral artifacts | Low-Moderate |

### Specific findings

#### 1. Stable-release watcher is the clearest API hotspot
- The watcher in `test_and_mark_stable` is not just slow; it is also a heavy caller because it polls one run endpoint every **15 seconds** for hours.
- This is the most obvious place where **batched polling is impossible** and **backoff/decoupling is mandatory**.

#### 2. `review_autofix` has reusable data that is not being reused enough
- `gate` already computes PR-level facts that `codex-agent` needs again.
- This is a classic “same workflow, same PR, same SHA, fetched twice” pattern.
- Safe change: move fetched PR metadata into job outputs or a machine-readable file created by `gate` and consumed downstream.

#### 3. `issue_pr_status` is already following the repo’s API-hygiene pattern
- Source inspection shows:
  - GraphQL `closingIssuesReferences`
  - a single batched GraphQL alias query for orchestrator/managed classification
  - per-issue REST only as fallback
- Recommendation: leave this alone except for minor cleanup.

#### 4. `copilot_pull_request_reviewer` needs source confirmation before major rewrites
- The logs clearly show artifact list/download/delete churn and multiple job startups.
- But since the workflow source is not in this repo snapshot, changes here should be treated as **log-driven candidates**, not immediate edit instructions.

## Prompt Cache & Memory System

**System verdict:** Good design intent, weak telemetry.

### What looks healthy
- Prompt cache is **enabled**, not disabled:
  - `review_autofix` run `25589012577` logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`
  - recent `orchestrate_poll` run `25588545849` log summary also showed `OPENROUTER_PROMPT_CACHE_DISABLED: false`
- The review flow explicitly includes a step named **`Pre-assemble static context cacheable across runs`**, which is the right architectural direction.
- `scripts/openrouter_prompt_cache.py` already contains normalization logic for usage/cache fields.

### What is not healthy
Across **92 sampled `review_autofix_cache_probe` log lines** in the parsed deep-dive set, all had:
- `cache_enabled=true`
- but all useful counters were `na`:
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`

So the current question is **not** “is prompt caching broken?”  
The current question is **“why are we not seeing usable cache counters?”**

### Likely failure point
Source inspection of `scripts/review_run_reviewers.sh` shows the probe logger emits `na` when it cannot extract usage fields from the returned payload. Because `scripts/openrouter_prompt_cache.py` already knows how to normalize usage, the most likely gap is **upstream usage-payload preservation**, not normalization logic.

### Likely fragmentation causes
These are partly **inferences** from workflow structure rather than directly measured facts:

1. **Dynamic blocks may be too close to the prompt prefix.**  
   If volatile data such as PR metadata, check-run context, or memory retrieval output enters the prompt before the stable reviewer instructions, cache keys will fragment across runs.

2. **Tiny-diff comment-only reviews are probably carrying too much variable context.**  
   Run `25589012577` was a 1-file/8-addition branch-review-only PR, but still used the full expensive reviewer setup. Even if caching is working, that prompt likely contains more dynamic noise than necessary for reuse.

3. **The cache probe is too narrow.**  
   Today’s logs mainly prove “cache feature flag was on,” not “real reviewer/editor calls got cache reads.”

### Concrete improvements
1. **Log raw provider `usage` JSON for actual reviewer/editor calls, not just the probe.**
2. **Normalize and emit `cache_creation_input_tokens` and `cache_read_input_tokens` on every real call.**
3. **Keep the cacheable prefix truly static**:
   - repo policy,
   - reviewer instructions,
   - tool contract,
   - stable support files,
   then append volatile diff/check-run/memory blocks after a fixed delimiter.
4. **Make tiny-diff branch-review prompts smaller and more stable** before trying broader cache experiments.

### Estimated impact
- **Tokens:** medium potential, but not measurable yet
- **Latency:** medium potential on repeated reviewer/editor calls
- **Reliability:** high benefit from making cache behavior observable before tuning it

## Orchestrator Health

**Health verdict:** The control logic is sophisticated and guarded, but the routing layer is noisy.

### What looks healthy
Source inspection of `scripts/orchestrate_poll_process.sh` shows strong anti-loop protections already exist, including:
- `merge_deferral_count`
- integration conflict dispatch counters
- lifetime dispatch caps
- judge repeat-fingerprint breaking
- clean-wave skip fast path
- clean-project completion skip fast path

That is a strong foundation. I would **keep** those protections.

### Where the system still feels operationally heavy

#### 1. Too many no-op child workflow runs
The repo-wide `other_count` is **768 / 1000** runs, and several orchestration families are overwhelmingly skipped/neutral:

- `clarify`: **213 total**, **13 success**, **200 other**
- `plan`: **196 total**, **11 success**, **185 other**
- `implement`: **196 total**, **9 success**, **186 other**
- `orchestrate_clarify_respond`: **196 total**, **2 success**, **194 other**

Recent examples:
- `clarify` run `25588864744`: skipped in **3s**
- `plan` run `25588864748`: skipped in **2s**
- `implement` run `25588864743`: skipped in **1s**
- `orchestrate_clarify_respond` run `25588864767`: skipped in **1s**

This is **not a correctness bug**. It looks more like **routing noise**: workflows are starting, evaluating `if`, and exiting immediately.

**Smallest safe mitigation:** introduce a thinner front-door router or narrow event triggers so only the likely-matching child workflow starts.

#### 2. `orchestrate_poll` has a long tail even though the fast path is fine
- Family metrics: **24 runs**, **259.5s avg**, **108s p50**, **1205.25s p95**
- Recent fast-path run `25588545849` completed in **106s**, and its `npm install -g "@openai/codex@v0.114.0"` step took only **3s**

That tells me bootstrap is not the issue. The long tail is more likely coming from **rare conflict/judge/heal paths**. That conclusion is an **inference** from the p50/p95 spread plus the complexity in `scripts/orchestrate_poll_process.sh`; I did not inspect the deepest slow poller run logs in detail.

### Observable indicators teams should track
I would put these into one machine-readable run summary for every `orchestrate_poll` execution:

| Indicator | Why it matters |
|---|---|
| `% skipped runs` for `clarify` / `plan` / `implement` / `respond` | Measures routing noise |
| `judge_cycle` and `judge_stall_cycles` | Measures judge-loop pressure |
| `merge_deferral_count` by wave | Measures unresolved merge churn |
| `integration_conflict_total_dispatches` | Detects chronic integration-heal loops |
| `% poller runs > 10 minutes` | Captures long-tail orchestration episodes |
| `review_autofix` cancellation rate on `claude/*` branches | Detects stale-run waste before/after preflight fixes |

## Pipeline Flow Bottlenecks

Below is the end-to-end bottleneck map for this sample window.

### 1. Queueing overhead
- **Observed in:** `review_autofix`, `copilot_pull_request_reviewer`, `ci`, `cancel_on_pr_close`, `orchestrate_poll`
- **Evidence:** repeated hosted-runner wait messages in:
  - `review_autofix` slow run `25586809675` (`review / gate/system` and `review / codex-agent/system`)
  - `copilot` run `25589012859` (`Prepare/system`, `Agent/system`, `Upload_results/system`)
  - `ci` run `25589012524` (`lint/system`)
- **Interpretation:** Since new infrastructure is off-limits, the only safe lever here is **reducing job count**, not adding runner capacity.

### 2. Compute bottlenecks
- **Primary compute hot spot:** `review_autofix`
  - `79` runs, `713.3s avg`, `2615.3s p95`
  - cancelled tiny-diff branch-review example `25589012577`
  - slow success `25586809675` at **2790s**
- **Secondary compute hot spot:** `CI`
  - single-file poller suite consumed **515s** in `25589012524`

### 3. Retry / polling overhead
- **Worst case:** `test_and_mark_stable` child watcher
  - **715** status polls in `25568050819`
- **Secondary case:** `review_autofix`
  - default `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`
  - default `CHECK_RUNS_POLL_INTERVAL_SECS=20`

### 4. Merge / conflict overhead
- **Observed directly:** `workflow_log_analysis` report push collision in `25568083133`
- **Observed indirectly / source-based inference:** orchestrator integration-heal logic is extensive, and `orchestrate_poll` p95 is much larger than p50, which is consistent with occasional conflict/judge/heal overhead

### 5. No-op orchestration fan-out
- **Observed directly:** hundreds of `clarify` / `plan` / `implement` / `respond` runs that start and skip almost immediately
- **Impact:** low per run, but high aggregate control-plane noise and extra queue pressure

### Recommended fix order by end-to-end impact
1. **Shard CI poller tests**
2. **Cheapen tiny-diff `claude/*` branch-review runs and add stale-head preflight**
3. **Decouple or soften the `workflow_log_analysis` stable watcher**
4. **Collapse multi-job review flows where source control allows**
5. **Reduce no-op orchestration fan-out**

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long tails and cancellations (`79` runs, `25` cancelled, `2615.3s p95`)
- `CI` serial poller suite (`25589012524`: `515s` inside one test file)
- `test_and_mark_stable` synchronous child watcher (`25568050819`: ~`10,878s` watching child run)
- `copilot_pull_request_reviewer` multi-job startup + artifact cleanup overhead (log-based; source unavailable)

**Top failure modes**
- Concurrent report push collision in `workflow_log_analysis` (`25568083133`)  
- Stable-release failure caused by waiting on a child analysis workflow (`25568050819`)  
- Late cancellation of comment-only `review_autofix` runs after expensive reviewer work (`25589012577`)

**Highest-cost drivers**
- `review_autofix` six-model reviewer panel at `xhigh` reasoning on some trivial branch-review runs
- `workflow_log_analysis` unselected-run summarization at **121k-148k tokens per run** on `gpt-5.4-mini`
- Avoidable reruns / cascades from concurrency collisions and hard-coupled watchers
- Prompt cache enabled but not measurable, so cache ROI is invisible today

**Top 3 prioritized actions**
1. **Shard `tests/test_orchestrate_poll_process.py` in CI**
2. **Add a tiny-diff `claude/*` review profile + stale-head preflight in `review_autofix`**
3. **Make `workflow_log_analysis` advisory for stable release, or at minimum stop fixed-interval end-to-end watching**

## Metrics Appendix

### Repo window summary

| Scope | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 203 (20.3%) | 2 (0.2%) | 27 (2.7%) | 768 (76.8%) | 135.3 | 1.0 | 650.0 |

**Note:** `Other` is largely skipped/neutral control-plane traffic.

### Key workflow-family metrics

| Workflow family | Total | Success rate | Failure rate | Cancelled | Other | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 79 | 64.6% | 0.0% | 25 | 3 | 713.3 | 48.0 | 2615.3 |
| `ci` | 32 | 100.0% | 0.0% | 0 | 0 | 628.8 | 639.0 | 669.0 |
| `orchestrate_poll` | 24 | 100.0% | 0.0% | 0 | 0 | 259.5 | 108.0 | 1205.3 |
| `workflow_log_analysis` | 2 | 50.0% | 50.0% | 0 | 0 | 9499.0 | 9499.0 | 10730.2 |
| `test_and_mark_stable` | 2 | 0.0% | 50.0% | 1 | 0 | 5556.0 | 5556.0 | 10421.4 |
| `clarify` | 213 | 6.1% | 0.0% | 0 | 200 | 7.7 | 1.0 | 76.6 |
| `plan` | 196 | 5.6% | 0.0% | 0 | 185 | 28.8 | 1.0 | 21.0 |
| `implement` | 196 | 4.6% | 0.0% | 1 | 186 | 43.4 | 1.0 | 10.3 |
| `orchestrate_clarify_respond` | 196 | 1.0% | 0.0% | 0 | 194 | 1.3 | 1.0 | 2.0 |

### Observed token telemetry

| Workflow / run | Operation | Model | Targeted runs | Summarized runs | Tokens used |
|---|---|---|---:|---:|---:|
| `workflow_log_analysis` / `25568083133` | `summarize_unselected_runs` | `openai/gpt-5.4-mini` | 100 | 100 | 121,044 |
| `workflow_log_analysis` / `25567987209` | `summarize_unselected_runs` | `openai/gpt-5.4-mini` | 100 | 97 | 148,158 |
| **Observed total** |  |  | **200** | **197** | **269,202** |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Parsed JSON `AI_MEMORY_TELEMETRY` lines in sampled deep-dive files | 58 |
| `retrieve` ops | 14 |
| Retrieve hit rate | 7.1% (1/14) |
| Average `estimated_tokens` | 2 |
| `keyword_method=none` | 13 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| Zero-record retrieves | 13 |
| Retrieve entries with `fail_open:true` | 0 observed |
| Retrieve entries with `enabled:false` | 0 observed |
| Parsed events with `push_attempts > 1` | 2 (`record-candidate`) |

### Prompt cache summary

| Metric | Value |
|---|---:|
| Sampled `review_autofix_cache_probe` log lines | 92 |
| `cache_enabled=true` | 92 / 92 |
| `prompt_tokens` available | 0 / 92 |
| `total_tokens` available | 0 / 92 |
| `cache_creation_input_tokens` available | 0 / 92 |
| `cache_read_input_tokens` available | 0 / 92 |

### GH API hotspot summary

| Workflow / run(s) | Hotspot | Concrete evidence | Estimated reducible calls |
|---|---|---|---:|
| `test_and_mark_stable` / `25568050819` | Child-run watcher loop | ~10,878s of fixed 15s polling; **715** observed status polls | **75-99%** |
| `review_autofix` / `25586809675`, `25589012577` | Repeated PR metadata fetches across jobs | `pulls/{PR}`, `commits/{sha}`, paginated `pulls/{PR}/files`, repo default-branch lookup | **3-6+ per run** |
| `issue_pr_status` / `25589386854` | Batched GraphQL already in place | GraphQL `closingIssuesReferences` + batched orchestrator query | Low remaining gain |
| `copilot_pull_request_reviewer` / `25589012859`, `25587222088` | Artifact list/download/delete path | `gh api /actions/runs/{id}/artifacts`; cleanup dominates visible work in log summaries | Low-Moderate |

If you want, I can next turn this report into a **prioritized implementation backlog** with:
1. exact workflow files to edit,  
2. proposed variable names/defaults, and  
3. a one-week rollout plan with validation checks.
