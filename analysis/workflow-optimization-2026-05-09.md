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

## Deep Audit — Workflows & Scripts (2026-05-09)

### Section 1: Bug & Correctness Sweep

Reviewed all **34** workflow files under `.github/workflows/` and all **60** top-level scripts under `scripts/`. The findings below are the material correctness issues.

#### BUG-001 — Phase-label replacement helper can drop concurrently added labels
- **File path**: `scripts/label_helpers.sh:146-187`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: `set_issue_phase_label_resilient()` first reads the full current label set (`gh api --paginate .../labels` at lines 163-164), then computes a new array by removing known phase labels and adding the target label (lines 175-176), and finally replaces the issue’s entire label set with `PUT /issues/{n}/labels` (lines 185-187). That is a classic read-modify-write race: if another workflow adds any non-phase label between the GET and the PUT, that label is absent from `_new` and gets silently removed. This is especially risky in this repo because multiple workflows mutate labels on the same issues/PRs.
- **Recommended fix**: Stop doing full-set replacement for phase transitions. Use an additive/remove-only sequence instead: add the target label, then remove only known phase labels that are present and are not the target. The existing safer pattern in `.github/workflows/implement.yml:709-710` already uses `gh issue edit --add-label ... --remove-label ...`; reuse that approach inside `scripts/label_helpers.sh` so all callers get race-safe semantics.

#### BUG-002 — Telegram tracking-comment updates are last-writer-wins
- **File path(s)**:
  - `scripts/tg_helpers.sh:167-198`
  - `scripts/tg_helpers.sh:240-270`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: Both `tg_store_msg_id()` and `tg_store_phase_msg_id()` fetch recent issue comments, select the first existing tracking comment, and PATCH the entire comment body after appending the new message ID. In `tg_store_msg_id()`, that happens at lines 183-198; in `tg_store_phase_msg_id()`, at lines 254-270. If two notifications land close together, both helpers can read the same old body and then race to PATCH it; the later PATCH wins and the earlier message ID is lost. The repo’s cleanup paths already support multiple tracking comments rather than a single canonical one: `tg_cleanup_phase_msgs()` iterates all matching phase comments at `scripts/tg_helpers.sh:326-351`, and `tg_cleanup_msgs()` iterates all matching tracking comments at `scripts/tg_helpers.sh:395-422`.
- **Recommended fix**: Stop mutating shared tracking comments. The simplest safe fix is to create a new hidden tracking comment per Telegram message ID or per `(phase, message_id)` pair, and let the existing cleanup walkers delete all matching comments. If comment compaction is still desired, add optimistic concurrency protection and retry on mismatch instead of PATCHing blind.

#### BUG-003 — `memory_processed_command_claim()` is not fail-open and can abort plan/implement
- **File path**: `scripts/memory_helpers.sh:236-245`
- **Severity**: High
- **Category tag**: `bug`
- **Description**: `memory_processed_command_claim()` directly returns the exit status of `python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-claim "$@"` with no fail-open wrapper. That is materially unsafe at the current call sites. In `.github/workflows/plan.yml:483-507`, the helper is invoked inside `CLAIM_RESULT="$(memory_processed_command_claim ...)"` under `set -euo pipefail`; a transient memory failure exits the step before the workflow can apply any fallback logic. `.github/workflows/implement.yml:717-742` does the same with `claim_json="$(memory_processed_command_claim ...)"`. This contradicts `README.md:25`, which states that memory errors should never fail workflows. The repo also already contains evidence that fail-open is the intended behavior: `.github/workflows/orchestrate_clarify_respond.yml:851-862` wraps the same helper in `|| true` and synthesizes a permissive fallback result when it fails.
- **Recommended fix**: Make `memory_processed_command_claim()` mirror the fail-open behavior of the other robust wrappers in `scripts/memory_helpers.sh`: catch Python failure, emit `AI_MEMORY_TELEMETRY` with `fail_open:true`, and return synthetic JSON that lets the caller continue. A compatible shape would be an object with `.operation_result.claimed = true` so existing `jq` parsing remains valid. After that, keep `plan.yml` and `implement.yml` reading the structured claim result instead of handling shell failure.

#### BUG-004 — `memory_finalize_task()` can fail `issue_pr_status` despite the documented fail-open contract
- **File path**: `scripts/memory_helpers.sh:216-224`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: `memory_finalize_task()` also proxies the raw exit code of `ai_memory.py` with no fail-open guard. In `.github/workflows/issue_pr_status.yml:399-445`, the `Finalize lineage on close` step runs under `set -euo pipefail` and loops over linked issues calling `memory_finalize_task`. A transient ai-memory push or branch error therefore exits the step and fails the workflow, even though `README.md:25` says memory errors should never fail workflows.
- **Recommended fix**: Wrap `memory_finalize_task()` the same way the other fail-open memory helpers are wrapped: log a warning, emit telemetry with `fail_open:true`, and return `0` on failure. If maintainers still want visibility, emit one structured warning per issue number rather than letting the helper terminate the workflow.

### Section 2: GitHub API Call Redundancy Audit

I did **not** repeat the already-documented fixed-interval stable-release watcher hotspot from the in-progress report. The items below are **additional code-level API redundancies** found in this snapshot.

#### API-001 — `review_autofix` re-fetches PR metadata/comments via 4+ REST calls instead of using the repo’s GraphQL helper
- **File path**: `.github/workflows/review_autofix.yml:1369-1383`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: In the normal PR path, `Collect PR metadata` performs four separate fetch flows:
  1. `pulls/${PR_NUMBER}` into `PR_PAYLOAD_FILE` at line 1369,
  2. paginated issue comments at lines 1370-1371,
  3. paginated reviews at lines 1372-1373,
  4. paginated review comments at lines 1374-1375.  
  That is **minimum 4 API calls**, and more when any of the paginated endpoints spans multiple pages. The repo already has `gh_pr_with_all_comments()` in `scripts/gh_helpers.sh:761-900`, which does a **single GraphQL fetch** on the happy path and only falls back to REST when pagination or GraphQL failure requires it. The current workflow is therefore bypassing an existing batching/caching pattern.
- **Recommended fix**: Extend `gh_pr_with_all_comments()` once so it also emits the `reviews` array currently needed for `PR_REVIEWS_FILE`, then replace this entire 4-call block with a single helper invocation.  
  - **Current call count**: minimum **4**, plus any extra pages.  
  - **Proposed call count**: **1** GraphQL call on the common path, with the helper’s existing REST fallback when GraphQL pagination boundaries are hit.  
  - **Existing pattern/helper to extend**: `scripts/gh_helpers.sh:761-900` (`gh_pr_with_all_comments`).

#### BATCH-001 — `review_autofix` body-text linked-issue fallback does up to 20 per-issue REST fetches
- **File path**: `.github/workflows/review_autofix.yml:1455-1485`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: When `closingIssuesReferences` returns `[]`, the fallback path extracts issue numbers from the PR body and then loops over them, issuing one `gh api "repos/${{ github.repository }}/issues/${_fb_num}"` call per issue at lines 1470-1485. The workflow caps the fallback at 20 issues (lines 1463-1468), but that still means **1 initial GraphQL request + up to 20 REST issue fetches** in a single execution path.
- **Recommended fix**: Replace the per-issue loop with one aliased GraphQL batch query that fetches `number`, `title`, and `body` for all fallback issue numbers at once. The repo already has an aliased GraphQL batching pattern in `scripts/orchestrate_poll_process.sh:1241-1307` (`_fetch_issue_labels_batch_graphql()`); adapt that into a generic `gh_issues_batch_by_number` helper in `scripts/gh_helpers.sh`.  
  - **Current call count**: **1 GraphQL + N REST**, where `N <= 20`.  
  - **Proposed call count**: **2 GraphQL** total on the fallback path, because the existing cap is already below the repo’s 25-item GraphQL batch size.  
  - **Existing pattern/helper to extend**: `scripts/orchestrate_poll_process.sh:1241-1307` (`_fetch_issue_labels_batch_graphql`).

#### API-002 — Final-merge logic fetches the same PR endpoint 6-8 times for adjacent fields
- **File path**: `scripts/orchestrate_poll_process.sh:3390-3502`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The final-merge path repeatedly calls `repos/${GITHUB_REPOSITORY}/pulls/${final_pr}` just to read adjacent fields from the same payload:
  - `.state` and `.merged_at != null` at lines 3394-3395,
  - `.state`, `.mergeable`, and `.merged_at != null` again at lines 3449-3451,
  - then the same three fields again after merge at lines 3500-3502.  
  On the merge-attempt path, that is **6 REST GETs** if `final_pr` is discovered in this invocation, and **8 REST GETs** if `STATE_FILE` already contained `final_pr` and the early pre-check runs too.
- **Recommended fix**: Fetch the PR JSON once per decision point and parse all three fields from that cached payload. `scripts/gh_helpers.sh:562-590` already provides `gh_api_json_to_file()` for one-fetch/validate/retry semantics; use that or add a tiny `fetch_pr_json_once` helper next to this code.  
  - **Current call count**: **6-8** GETs to the same endpoint on the merge path.  
  - **Proposed call count**: **2** GETs on the common path (pre-merge and post-merge), or **3** if the early “already merged?” pre-check is retained for previously-known `final_pr`.  
  - **Existing pattern/helper to extend**: `scripts/gh_helpers.sh:562-590` (`gh_api_json_to_file`).

#### API-003 — The stable-release wait loop downloads the same job log twice per polling iteration
- **File path**: `.github/workflows/test-and-mark-stable.yml:1404-1482,1524-1538`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: After `ELAPSED >= 600` and when `JOB_ID` is known, the `Phase 4: Wait for review & autofix to complete` step first downloads `actions/jobs/${JOB_ID}/logs` into `LOG_FILE` for grep-based shortcuts at lines 1404-1482, then later downloads the same endpoint again solely to compute `wc -c` at lines 1534-1536. That is **2 full log downloads per poll iteration** once the live-log branch activates. The broader 15-second watcher itself is already covered in the current report; this finding is the extra duplicate `/logs` download inside each iteration.
- **Recommended fix**: Reuse the existing `LOG_FILE` temp-file cache. When the first download succeeds, compute `LOG_SIZE` with `wc -c < "$LOG_FILE"` instead of calling the API again. If the first download failed, skip the size signal for that iteration rather than issuing a second `/logs` request.  
  - **Current call count**: **2** `/actions/jobs/{id}/logs` downloads per eligible iteration.  
  - **Proposed call count**: **1** download per eligible iteration.  
  - **Existing pattern/helper to extend**: the step’s existing `LOG_FILE` temp-file cache at lines 1404-1425.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Six core workflows duplicate the same support-source checkout/staging pipeline
- **File path(s)**:
  - `.github/workflows/clarify.yml:160-226`
  - `.github/workflows/plan.yml:191-256`
  - `.github/workflows/implement.yml:337-417`
  - `.github/workflows/orchestrate.yml:270-337`
  - `.github/workflows/orchestrate_poll.yml:223-295`
  - `.github/workflows/orchestrate_clarify_respond.yml:200-265`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: These workflows all repeat the same sequence:
  1. resolve `SCRIPT_REF`,
  2. checkout `.codex-workflow-src`,
  3. checkout a `main` fallback snapshot,
  4. iterate a per-workflow file list and `install` scripts into `scripts/`.  
  The structure is largely identical and is already drifting in file lists and optional assets. Every support-bootstrap fix now has to be copied into at least six workflows, with `review_autofix.yml` carrying an even larger variant of the same pattern.
- **Recommended fix**: Move this logic into a shared module, preferably a new `scripts/stage_workflow_support.sh` with an entrypoint such as `stage_workflow_support <mode> <dest_dir> <script_ref>`, or a thin composite action that wraps that script. Update callers in `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, and `orchestrate_clarify_respond.yml`; then fold `review_autofix.yml` into the same interface with a richer `mode=review_autofix`.

#### DUP-002 — Label-helper fallbacks are redefined inline in multiple workflows and already behave differently from the canonical helper
- **File path(s)**:
  - `.github/workflows/issue_pr_status.yml:239-249`
  - `.github/workflows/review_autofix.yml:3812-3853`
  - `.github/workflows/review_autofix.yml:3928-3975`
  - `.github/workflows/review_autofix.yml:4683-4706`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: `issue_pr_status` and three late `review_autofix` blocks re-declare local `ensure_label_exists()` / `set_issue_phase_label_resilient()` fallbacks inline instead of depending on `scripts/label_helpers.sh`. These fallback copies only POST the target label; they do not implement the canonical helper’s phase-label replacement semantics from `scripts/label_helpers.sh:146-197`. As a result, behavior depends on which code path successfully loaded the helper.
- **Recommended fix**: Keep the authoritative implementation in `scripts/label_helpers.sh` and expose a single loader/fallback entrypoint there, for example `ensure_label_helpers_loaded <support_dir>` or `set_issue_phase_label_postonly <issue_number> <target_label> <repo>`. Update `issue_pr_status.yml` and the three `review_autofix` callers to source/use the shared function rather than retyping it.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — `test-and-mark-stable` wait-review step is already in the high-risk zone
- **File path**: `.github/workflows/test-and-mark-stable.yml:1202-1585`
- **Severity**: High
- **Category tag**: `expression-limit`
- **Description**: The `run:` body for **`Phase 4: Wait for review & autofix to complete`** contains `${{ }}` interpolations and currently measures **~19,899 characters**, leaving only **~1,101 characters** of headroom under GitHub Actions’ 21,000-character expression limit. That is above the requested **18,000-character high-risk threshold**. The block already combines bait-SHA pinning, rate-limit-aware API helpers, live-log shortcuts, adaptive activity signals, and timeout diagnostics, so ordinary maintenance growth can push it over the hard limit.
- **Recommended fix**: Extract this entire state machine into an external script under `scripts/`, e.g. `scripts/wait_review_autofix.sh <pr_number> <issue_number> <test_repo>`, and leave only environment wiring and output handling in YAML. If a full extraction is too large for one change, split live-log analysis and inactivity detection into separate steps first.

#### EXPR-002 — `test-and-mark-stable` canary verification step is above the medium-risk threshold
- **File path**: `.github/workflows/test-and-mark-stable.yml:1672-2077`
- **Severity**: Medium
- **Category tag**: `expression-limit`
- **Description**: The `run:` body for **`Phase 4b: Verify editor restored canary (pytest + retry)`** is **~17,408 characters**, leaving **~3,592 characters** of headroom. That exceeds the requested **15,000-character medium-risk threshold**. The step currently embeds package-install fallback, file fetch, pytest execution, retry dispatch, PR-state polling, and multi-status output mapping in one interpolated block.
- **Recommended fix**: Extract the verification/retry logic into `scripts/verify_editor_canary.sh <pr_number> <issue_number> <bait_sha> <workflow_file>`, or split the step into separate “install pytest”, “verify once”, and “retry/re-dispatch” steps so each expression body stays smaller.

#### EXPR-003 — `review_autofix` PR metadata collection step is above the medium-risk threshold
- **File path**: `.github/workflows/review_autofix.yml:1284-1673`
- **Severity**: Medium
- **Category tag**: `expression-limit`
- **Description**: The `run:` body for **`Collect PR metadata`** is also **~17,408 characters**, leaving **~3,592 characters** of headroom. That is above the **15,000-character medium-risk threshold**. The block currently mixes custom retry code, no-PR synthesis, PR metadata fetches, linked-issue GraphQL fetch, body-text fallback resolution, and context-file generation in one interpolated YAML expression.
- **Recommended fix**: Move this logic into `scripts/collect_review_pr_metadata.sh`, or split it into multiple steps: one for no-PR synthesis, one for PR/comments fetch, one for linked-issue resolution, and one for context-file rendering. Reusing `gh_pr_with_all_comments()` and a new batched linked-issues helper would reduce both API count and expression size.

No workflow file in `.github/workflows/` exceeds the **800 KB** early-warning threshold. Largest observed files were `review_autofix.yml` (**285,829 bytes**) and `test-and-mark-stable.yml` (**272,275 bytes**).

### Section 5: Cross-Cutting Concerns

No `TODO`, `FIXME`, or `HACK` markers were found under `.github/workflows/` or `scripts/`.

#### CONSIST-001 — `tg_helpers.sh` uses rate-limit-aware reads but raw best-effort writes
- **File path**: `scripts/tg_helpers.sh:175-179,194-205,246-250,266-276,346-350,417-421`
- **Severity**: Low
- **Category tag**: `consistency`
- **Description**: The helper reads GitHub issue comments through `curl_gh_api` at lines 169-172, 241-244, 314-317, and 383-386, but all GitHub writes/deletes in the same file use raw `curl -s -X POST/PATCH/DELETE` with `|| true`. That means transient 403/429/5xx failures on tracking-comment creation, updates, or cleanup do not get the same retry/backoff handling that reads already get.
- **Recommended fix**: Route GitHub writes through `curl_gh_api` or `gh api` wrapped by `gh_retry`, while preserving the current best-effort behavior. That keeps the helper fail-open but makes write paths consistent with the repo’s shared GitHub API policy.

#### SHELL-001 — Unquoted issue-list expansion is still a real SC2086 site
- **File path**: `scripts/orchestrate_poll_process.sh:10606-10607`
- **Severity**: Low
- **Category tag**: `shellcheck`
- **Description**: `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` expands `${ISSUE_NUMS}` unquoted before sorting. Shell splitting is being relied on here, but glob expansion and empty-input edge cases are still possible, and ShellCheck correctly flags this as SC2086.
- **Recommended fix**: Normalize the issue list to a newline-delimited or array form before sorting, e.g. `printf '%s\n' "${ISSUE_NUMS_ARRAY[@]}" | sort -un`, or convert the existing string with a delimiter-safe transform before the loop. Keep the numeric validation already present in the downstream loop.

#### SHELL-002 — Secret-path `case` alternatives are unreachable because earlier globs subsume them
- **File path**: `scripts/validate_changed_files_syntax.sh:70-74`
- **Severity**: Low
- **Category tag**: `shellcheck`
- **Description**: In the `case "${file},${basename_lc}" in` block, the `*.env*` arm on line 71 already matches `.envrc` and `.env*`, so the later `*,*.envrc|*,.env*` alternatives on lines 72-73 are unreachable. ShellCheck flags this as SC2221/SC2222. The current outcome is harmless because every matching branch sets `skip_dump=1`, but the dead alternatives make the policy harder to reason about.
- **Recommended fix**: Remove the unreachable alternatives or tighten the earlier glob so the later patterns have distinct meaning. Add one short comment documenting the intended precedence.

#### DEAD-001 — Two memory-helper wrappers are currently dead API surface inside this repo
- **File path**: `scripts/memory_helpers.sh:172-192,226-234`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `memory_processed_command_list()` and `memory_promote()` are defined here, but no call sites were found for either symbol under `.github/workflows/`, `scripts/`, or the audited repo documentation/config files. Within this repository snapshot, they are unused API surface and therefore at risk of drifting away from real runtime behavior.
- **Recommended fix**: Either remove the unused wrappers until they have a caller, or add the intended workflow integration plus a regression test that exercises them. If they are intentionally reserved for near-future use, mark that clearly in comments and add a tracking test so they do not silently rot.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-003, EXPR-001 |
| Medium | 10 | BUG-001, BUG-002, BUG-004, API-001, API-002, API-003, BATCH-001, DUP-001, EXPR-002, EXPR-003 |
| Low | 5 | DUP-002, CONSIST-001, SHELL-001, SHELL-002, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 5 | Medium |
| API call optimization | 4 | Medium |
| Code modularization | 9 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 6 | Small |
