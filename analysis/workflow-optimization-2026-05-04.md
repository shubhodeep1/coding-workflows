## Executive Summary

- **The highest-impact reliability fixes are in `test_and_mark_stable`, which failed 3/3 runs (100%).** The three failures were deterministic pipeline defects, not flaky runner noise: run `25271960656` failed because `e2e-smoke-test` completed but **no PR was created**, run `25273372573` failed because `release` hit `error: src refspec stable matches more than one`, and run `25281876234` failed because `sync-to-main` called `gh workflow run` from a non-git directory (`fatal: not a git repository`). **Estimated impact:** restore a broken release lane and eliminate ~57–61 minutes of wasted runtime per failed release. **Confidence:** high.

- **`review_autofix` is the biggest latency/cost hotspot when it actually runs.** Family stats show `66` runs with `32` successes and `32` cancellations; successful long-path runs reached `1457s` (`25294003283`) and `1712s` (`25297994916`), dominated by `review / codex-agent (claude-branch-review)`. Recent runs also show the workflow still provisions six reviewer models even on comment-only/reviewer-only paths. **Estimated impact:** save ~8–15 minutes on affected review runs by shrinking or gating the reviewer panel. **Confidence:** medium-high.

- **The implement loop is wasting tokens and time on low-reasoning, no-action retries.** Failed implement runs `25272034874`, `25293932552`, `25293940145`, and `25294005792` all bailed with `2 consecutive attempts with no actionable output`; `25293966619` burned through `5` attempts before failing. These runs were using `MODEL_EDITOR: openai/gpt-5.3-codex` with `MODEL_REASONING_EFFORT: none`. An evidence-grade unselected run summary (`25294079100`) recorded `42,989` tokens across retry attempts 2–5 alone. **Estimated impact:** 40–80% token reduction on failed implement paths plus faster failure detection. **Confidence:** high.

- **The poller has a repeatable micro-bottleneck that is easy to fix.** In `orchestrate_poll` run `25299336020`, `poll/Checkout repository` was the longest visible step (~`8.6s` from `03:19:52.705Z` to `03:20:01.345Z`) and fetched a large tag list, consuming ~21% of total runtime in a `40s` poll cycle. **Estimated impact:** save ~8–10s per poll run (~20–25%). **Confidence:** high.

- **GitHub API usage is functional but still has obvious per-item redundancy.** The clearest examples are `review_autofix` post-merge validate dispatch (GraphQL lookup, REST PR fallback, then per-issue `gh issue view`, `gh workflow run`, `gh issue edit`) and `issue_pr_status` (GraphQL + REST fallback + per-issue label/close/lookups). **Estimated impact:** save ~2–5 API calls per run on common paths and reduce rate-limit exposure. **Confidence:** medium.

- **AI memory is healthy on writes but weak on retrieval quality for reviewers.** Across deep-dive logs, I found `14` `retrieve` telemetry events with a **35.7% hit rate** (`5/14`), average `estimated_tokens` of `12`, and keyword selection skewed toward `none` (`9/14`). Reviewer retrievals in long `review_autofix` runs repeatedly returned `0` records, while implement retrievals returned `1–2` records at low token cost. **Estimated impact:** medium reliability/cost gain if reviewer retrieval is tuned; current write path is healthy. **Confidence:** medium-high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Shrink or gate the reviewer panel on comment-only / claude-branch-review paths
**Critical-path win**

- **Evidence**
  - `review_autofix` run `25297994916` took `1712s`, dominated by `review / codex-agent (claude-branch-review)`.
  - `review_autofix` run `25294003283` took `1457s`, also dominated by the same path.
  - Cancelled runs `25297832345` (`417s`) and `25295939655` (`244s`) still spent minutes in branch review before cancellation.
  - Recent logs show six reviewer models configured: `minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `deepseek/deepseek-v4-pro`, `z-ai/glm-5`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`.

- **Root cause**
  - The expensive multi-reviewer branch-review path is running even when the workflow is already in a **comment-only / no editor-commit-jjudge-auto-merge** mode.

- **Exact change**
  - Keep the current full panel for high-risk or merge-driving review paths.
  - On `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... comment-only path`, reduce the first-pass panel to 2–3 reviewers, or run a single summarizer first and only fan out if disagreement/high-risk files are detected.

- **Estimated time savings**
  - ~`8–15 minutes` per long `review_autofix` run on the comment-only path.
  - Additional queue relief from fewer long-running reviewer jobs.

- **Implementation risk**
  - **Medium.** Review quality could drop if downsized too aggressively; mitigate by escalating back to the full panel on disagreement, workflow edits, or large/high-risk diffs.

---

### 2. Stop full artifact cleanup scans in `copilot_pull_request_reviewer`
**Critical-path win**

- **Evidence**
  - `copilot_pull_request_reviewer` run `25294004822`: `Cleanup artifacts` consumed ~`143s`, dominating the workflow.
  - `25295941722`: cleanup dominated roughly `4m` of a `246s` run.
  - `25297837347`: total `157s`, with `Cleanup artifacts` delaying completion; hotspot call was `gh api /repos/.../actions/runs/.../artifacts`.

- **Root cause**
  - Artifact enumeration/cleanup is happening as a separate heavy phase, and appears to scan more than the minimum needed for the current run.

- **Exact change**
  - Restrict cleanup to current-run artifacts only, or rely on `retention-days`/overwrite semantics where possible.
  - If cleanup must remain, filter by exact artifact name prefix before listing/deleting.
  - Avoid a dedicated cleanup pass when there is nothing to remove.

- **Estimated time savings**
  - ~`2–4 minutes` per affected Copilot review run.

- **Implementation risk**
  - **Low.** This is backward-compatible if retention remains intact.

---

### 3. Disable unnecessary tag fetches in `orchestrate_poll`
**Critical-path win**

- **Evidence**
  - In run `25299336020`, `poll/Checkout repository` ran from `03:19:52.7049927Z` to `03:20:01.3445532Z` (~`8.6s`) and visibly fetched many tags.
  - Total poll duration was only `40s`, so checkout alone was ~`21%` of the cycle.

- **Root cause**
  - The poller checkout is pulling more repository metadata than the poll step appears to need.

- **Exact change**
  - Set checkout to shallow, no-tags mode for the repository checkout used by the poller unless a downstream step explicitly consumes tags.
  - If only support scripts are needed, keep support-source checkout separate and minimal.

- **Estimated time savings**
  - ~`8–10s` per poll cycle.
  - With `48` poll runs in-window, this is a meaningful cumulative reduction.

- **Implementation risk**
  - **Low**, if verified that no poll logic reads tags.

---

### 4. Cancel superseded `review_autofix` runs before `codex-agent` starts
**Critical-path win**

- **Evidence**
  - `review_autofix` family: `66` total runs, `32` cancelled.
  - Some cancellations happen early, but others waste real runtime: `25297832345` was cancelled after `417s`; `25295939655` after `244s`.
  - Logs show queue/start wait plus branch-review startup before cancellation.

- **Root cause**
  - Superseded runs are not always being cancelled early enough to avoid provisioning and branch-review startup.

- **Exact change**
  - Add or tighten `concurrency`/head-ref dedupe so only the newest review run per PR/head ref is allowed to enter `codex-agent`.
  - Perform supersede checks before reviewer fan-out and before support checkout.

- **Estimated time savings**
  - ~`4–7 minutes` on the worst cancelled runs.
  - Secondary benefit: reduces runner contention for other workflows.

- **Implementation risk**
  - **Low-medium.** Needs careful grouping keys to avoid cancelling independent review paths.

---

### 5. Low-priority micro-optimization: trim no-op runner work in short workflows
**Micro-optimization**

- **Evidence**
  - `cancel_on_pr_close` runs complete in `6–8s`, `issue_pr_status` in `11–13s`, `forward_merge_stable_to_main` in `13s`, and logs repeatedly show hosted-runner wait/setup dominating these tiny jobs.

- **Root cause**
  - Small orchestration-only tasks still pay full runner provisioning cost.

- **Exact change**
  - Where feasible, fold short post-merge/no-op status tasks into already-running workflows instead of dispatching a separate runner-backed workflow for every small action.

- **Estimated time savings**
  - Seconds per run, not minutes.

- **Implementation risk**
  - **Medium.** Architectural simplification helps, but this is less urgent than the items above.

## Cost Optimizations

Ranked by expected token / compute savings.

### 1. Cut implement retries after the first no-actionable-output failure
- **Evidence**
  - Implement failures `25272034874`, `25293932552`, `25293940145`, and `25294005792` all ended with `Codex produced no actionable output 2 attempts in a row`.
  - `25293966619` failed after `5` attempts.
  - Evidence-grade summary for run `25294079100` reported retry token usage of:
    - Attempt 2: `5,339`
    - Attempt 3: `23,176`
    - Attempt 4: `1,082`
    - Attempt 5: `13,392`
    - Total observed across attempts 2–5: **`42,989` tokens**
  - Implement runs were configured with `MODEL_REASONING_EFFORT: none`.

- **Root cause**
  - The retry loop is allowing repeated exploratory/no-op attempts instead of switching strategy quickly.

- **Exact change**
  - After the **first** empty-output / announced-edit-without-changes attempt:
    - switch to a shorter repair prompt,
    - inject a minimal diff-oriented instruction set,
    - or bail directly to diagnose/clarify if there are still no changed files.
  - Keep the current hard stop on consecutive no-op attempts.

- **Estimated savings**
  - ~`40–80%` token reduction on failing implement runs.
  - Also saves 1–4 minutes on those failures.

- **Quality-risk notes**
  - **Low.** Rare recoverable second attempts may be skipped, but current evidence shows repeated no-op attempts are mostly waste.

---

### 2. Restore prompt rendering validation and remove literal placeholder leakage
- **Evidence**
  - Implement failure logs `25293966619` and `25294005792` contain literal `{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}` / `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}` fragments.
  - The same logs repeatedly include large instructional blocks and moved-instrumentation notes.
  - Reviewer consensus memory recorded in `25297994916` flagged the removed unresolved-placeholder validation as a prompt-quality issue.

- **Root cause**
  - Prompt templates and prompt rendering validation are out of sync, allowing unstable prompt text and unresolved placeholders into the final prompt.

- **Exact change**
  - Reintroduce a generic unresolved-placeholder guard in prompt rendering/CI.
  - Fail fast when `{{...}}` placeholders remain, except for explicitly allowed placeholders.
  - Remove repeated instrumentation comments from the prompt body; keep them in docs, not runtime prompt text.

- **Estimated savings**
  - Several thousand prompt tokens per affected implement/review attempt.
  - Better prompt-cache reuse probability.

- **Quality-risk notes**
  - **Low.** This improves correctness and usually improves model behavior.

---

### 3. Use an adaptive reviewer panel instead of always paying for six reviewers
- **Evidence**
  - Recent review runs explicitly configure six reviewer models.
  - Long review runs are the dominant compute hotspot in the window.

- **Root cause**
  - Review cost is front-loaded even when the path is comment-only or clearly low-risk.

- **Exact change**
  - Default to a smaller panel on:
    - comment-only branch review,
    - doc-only changes,
    - or low-risk small diffs.
  - Escalate to the full panel only on disagreement/high-risk paths.

- **Estimated savings**
  - High compute/token savings on long review runs; likely the biggest review-cost lever.

- **Quality-risk notes**
  - **Medium.** Mitigate with escalation triggers.

---

### 4. Don’t keep `MODEL_REASONING_EFFORT: none` on retry-heavy implement paths
- **Evidence**
  - Every sampled failing implement run used `MODEL_REASONING_EFFORT: none`.
  - Failures are dominated by exploration/no-action loops rather than expensive successful reasoning.

- **Root cause**
  - A zero-reasoning setting appears cheaper per attempt but can cost more in aggregate when it causes repeated retries.

- **Exact change**
  - Keep attempt 1 at current settings if desired, but escalate to `low` or `medium` on attempt 2 only.
  - Pair with the tighter retry cutoff above.

- **Estimated savings**
  - Medium total-cost reduction if it eliminates multi-attempt failures.
  - Also improves success probability.

- **Quality-risk notes**
  - **Medium.** Per-attempt cost rises, but total cost likely falls on bad cases.

---

### 5. Add real prompt-cache usage telemetry before further cache tuning
- **Evidence**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears repeatedly in implement/review/poll runs.
  - But the collected window has **no cache create/read/hit/miss counters**.

- **Root cause**
  - Cache is enabled, but there is not enough telemetry to prove whether it is saving tokens.

- **Exact change**
  - Emit per-call cache create/read/hit/miss counters into logs or summary artifacts.
  - Keep the cache enabled; just add observability.

- **Estimated savings**
  - Indirect but important: lets you identify whether cache fragmentation work is paying off.

- **Quality-risk notes**
  - **Low.**

## Reliability Improvements

Ranked by expected failure-rate / rerun-rate reduction.

### 1. Fix ambiguous tag pushes in `release`
- **Failure evidence**
  - Run `25273372573`, job `release`, step `Tag version and update stable pointer`:
    - `git tag -f stable "$VERSION"`
    - `git push -f origin stable`
    - failed with `error: src refspec stable matches more than one`

- **Root cause category**
  - Git ref ambiguity / naming collision.

- **Exact fix**
  - Push explicit tag refs, e.g. push `refs/tags/stable` and `refs/tags/v1`, not bare names.
  - Keep the immutable version tag push unchanged.

- **Expected reliability impact**
  - Removes one of the three observed stable-release failure modes immediately.

- **Rollback / fail-open**
  - Safe rollback; this is a narrow refspec change.

---

### 2. Fix `sync-to-main` dispatch so `gh workflow run` is not executed from a non-repo context
- **Failure evidence**
  - Run `25281876234`, job `sync-to-main`, step `Dispatch forward-merge-stable-to-main`:
    - `gh workflow run forward-merge-stable-to-main.yml --ref stable`
    - failed with `failed to run git: fatal: not a git repository`

- **Root cause category**
  - Environment/working-directory misuse.

- **Exact fix**
  - Run from a checked-out repo, or pass `--repo` explicitly so `gh` does not depend on local git context.
  - Add a preflight `git rev-parse --is-inside-work-tree` or repo-context check.

- **Expected reliability impact**
  - Removes another one of the three observed stable-release failure modes.

- **Rollback / fail-open**
  - Safe; if preflight fails, emit a clear error before burning the full workflow.

---

### 3. Fail fast when implement completes without creating a PR
- **Failure evidence**
  - Run `25271960656`, `e2e-smoke-test`, `Phase 3b: Wait for PR creation (implement phase)`:
    - `All 1 implement workflow run(s) completed but no PR was created`
  - Separate implement failures (`25272034874`, `25293932552`, `25293940145`, `25294005792`) show the likely underlying reason: no-actionable-output bailouts.

- **Root cause category**
  - Orchestrator/implement handoff failure.

- **Exact fix**
  - In Phase 3b, watch implement run conclusions directly:
    - if implement ends `failure`, surface that immediately instead of waiting for PR creation,
    - if implement ends with no changed files / no PR, trigger diagnose or reclarify automatically,
    - preserve the current timeout as a fallback only.

- **Expected reliability impact**
  - Reduces both e2e false waits and manual debugging cycles.
  - Likely removes the most common implement-related test failure mode.

- **Rollback / fail-open**
  - Fail-open by keeping the current timeout path as a fallback if diagnose cannot run.

---

### 4. Restore prompt placeholder validation to prevent silent prompt corruption
- **Failure evidence**
  - Literal unresolved placeholders appeared in implement logs (`{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}`).
  - Reviewer memory candidate from `25297994916` flagged the removed unresolved-placeholder guard as a real issue.
  - This type of defect also lines up with no-op / confused-agent behavior.

- **Root cause category**
  - Prompt rendering contract drift.

- **Exact fix**
  - Restore generic unresolved-placeholder detection in `scripts/render_prompt.sh` and/or CI.
  - Add one smoke-render validation for reviewer/editor/judge/implement prompt paths.

- **Expected reliability impact**
  - Medium: catches a whole class of silent prompt regressions before they reach runtime.

- **Rollback / fail-open**
  - If a full generic guard is too strict, start with warning-only plus allowlist, then tighten.

## AI Memory Health

I found structured `AI_MEMORY_TELEMETRY` JSON in deep-dive logs for `implement`, `review_autofix`, `orchestrate_poll`, and `workflow_log_analysis`.

### Observed telemetry health
- **Total structured telemetry events found:** `65`
- **Operation distribution:**
  - `record-run-event`: `31`
  - `retrieve`: `14`
  - `record-candidate`: `7`
  - `processed-command-check`: `5`
  - `processed-command-claim`: `5`
  - `summarize_unselected_runs`: `3`

### Retrieval effectiveness
- **Retrieve hit rate:** `5 / 14 = 35.7%`
- **Average `estimated_tokens`:** `12`
- **Observed `keyword_method` distribution:**
  - `none`: `9`
  - `plain`: `5`
  - `llm`: `0` observed in deep-dive logs

### Important flags
- **Zero-record retrieves:** `9`
  - These are concentrated in reviewer-context retrievals, including:
    - `review_autofix` `25279043495`
    - `25278175531`
    - `25276795302`
    - `25297994916`
- **`fail_open: true` entries:** none found in structured JSON telemetry
- **`enabled: false` entries:** none found
- **High push retry counts:** only one notable case
  - `implement` run `25293966619` recorded `phase_started` with `push_attempts: 2`
  - All other observed memory writes were `push_attempts: 1`

### Budget comparison
- In sampled implement retrievals, `estimated_tokens` was `28` or `56`, and the same logs showed a `token_budget: 1600`, so retrieval size is well below the configured budget where logged.

### Interpretation
- **Write path looks healthy.** Run-event and candidate writes succeed consistently.
- **Retrieve quality is weak for reviewer mode.** Most reviewer retrievals returned `0` records with `keyword_method: none`, which means the system is not effectively surfacing prior review patterns at the point where it would be most useful.
- **Implement retrieval is in better shape** than reviewer retrieval: implement runs typically got `1–2` records with trivial token cost.

### Recommendation
1. Tune reviewer retrieval to avoid defaulting to `keyword_method: none`.
2. Backfill/promote reviewer-relevant pattern records so long review runs can actually retrieve prior consensus.
3. Track **reviewer retrieve hit rate** as a first-class KPI; current observed baseline is only `35.7%` overall and effectively worse for reviewer mode.

## GH API Call Audit

### 1. `review_autofix` post-merge validate dispatch is doing avoidable per-issue calls
- **Evidence**
  - Run `25300035937`, step `review / post-merge-validate-dispatch / Dispatch standalone validate...`
  - Observed sequence:
    - `gh api graphql` for `closingIssuesReferences`
    - fallback `gh api repos/.../pulls/${PR_NUMBER}`
    - per-issue `gh issue view ... --json labels`
    - `gh workflow run ...`
    - `gh issue edit ... --remove-label`

- **High-redundancy pattern**
  - If fallback parsing is used, labels are unknown and fetched **per issue**.
  - Dispatch and label-removal are also per issue.

- **Concrete batching/reuse change**
  - If fallback issue numbers are extracted from PR text, do **one aliased GraphQL query** to fetch labels for all extracted issue numbers at once.
  - Reuse that result for both validation gating and post-dispatch label cleanup decisions.

- **Estimated call-count reduction**
  - Save ~`1–3` API calls per merged PR with linked issues.

- **Rate-limit risk reduction**
  - Medium; this path runs often and currently mixes GraphQL + REST + per-item calls.

---

### 2. `issue_pr_status` still does per-issue follow-up lookups
- **Evidence**
  - Run `25300035959`, step `sync-status / sync-issue-status`
  - Observed calls:
    - `gh api graphql`
    - fallback PR REST read
    - `POST /issues/{n}/labels`
    - extra issue metadata lookup
    - `gh issue close`

- **High-redundancy pattern**
  - Initial discovery is batched, but state transitions still branch into per-issue REST calls.

- **Concrete batching/reuse change**
  - Reuse the initial GraphQL payload as the authoritative source for labels/body where possible.
  - Batch any missing issue metadata in one GraphQL alias query before the close/label loop.
  - Only call `gh issue close` / label mutation when the current state actually differs.

- **Estimated call-count reduction**
  - Save ~`2–4` API calls per linked-issue sync run.

- **Rate-limit risk reduction**
  - Medium.

---

### 3. `cancel_on_pr_close` always keeps a rate-limit probe handy, even on no-op runs
- **Evidence**
  - Run `25300035971`:
    - `_rl_wait()` probes `gh api -i /rate_limit`
    - `_gh_retry` wraps cancel requests
    - run ultimately found no matching queued/in-progress runs
  - Similar pattern appears in `25296023348` and `25295724078`.

- **High-redundancy pattern**
  - Rate-limit probing is built into the retry path even though most runs do not hit rate limits and many are no-ops.

- **Concrete batching/reuse change**
  - Only call `/rate_limit` after an actual 403/429/secondary-limit response.
  - For no-op runs, avoid the extra probe entirely.

- **Estimated call-count reduction**
  - ~`1` API call per cancel workflow invocation.

- **Rate-limit risk reduction**
  - Small per run, but worth doing because this workflow is frequent and short.

---

### 4. `copilot_pull_request_reviewer` calls are not excessive, but cleanup is expensive enough to optimize anyway
- **Evidence**
  - Runs `25295941722`, `25297837347`, `25294004822` show:
    - `pulls.get`
    - paginated `pulls.listFiles`
    - artifact enumeration via `/actions/runs/{run_id}/artifacts`
  - The API pattern itself is sensible; the cleanup phase is the issue.

- **Concrete change**
  - Keep current PR metadata fetches.
  - Optimize artifact cleanup scope rather than trying to over-batch already-reasonable review prep calls.

- **Estimated call-count reduction**
  - Modest.
- **Rate-limit risk reduction**
  - Low; this is more of a latency issue than a hard API abuse issue.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed state**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` is present in sampled `implement`, `review_autofix`, and `orchestrate_poll` runs.
- **Missing data**
  - The current window does **not** include prompt-cache read/create/hit/miss counters.
  - So cache effectiveness cannot be quantified from this collection.

### Cache-fragmentation risks observed
- Implement failure logs contain unstable prompt content:
  - repeated large instruction blocks,
  - repeated moved-instrumentation comments,
  - literal unresolved placeholders like `{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}`.
- This kind of prompt variance is a likely cache killer because it destabilizes the shared prefix.

### Memory interaction quality
- Memory retrieval is cheap when it hits, but reviewer-mode retrieval is frequently empty.
- That means the system is paying orchestration complexity for memory without consistently getting useful prompt compression or context reuse back.

### Concrete improvements
1. **Emit prompt-cache metrics**
   - Add explicit prompt-cache create/read/hit/miss counters to logs or summaries.
2. **Stabilize prompt prefixes**
   - Move run-specific noisy material to the tail.
   - Keep reusable instruction blocks canonical and identical across retries.
3. **Restore placeholder validation**
   - Prevent unresolved prompt-template tokens from reaching model inputs.
4. **Reduce repeated boilerplate on retries**
   - Retry prompts should be deltas, not full prompt-body re-expansions.

### Estimated impact
- **Tokens:** likely meaningful reduction on implement/review retries.
- **Latency:** moderate improvement from shorter prompt bodies and better cache reuse.
- **Reliability:** medium improvement via fewer malformed/confusing prompts.

## Orchestrator Health

### What looks healthy
- `orchestrate_poll` is operationally stable:
  - `48/48` success in the window.
  - Memory write path is working (`poll_started`, `poll_completed`).
- `clarify`, `plan`, and `orchestrate_clarify_respond` mostly skip cleanly rather than failing.

### Recurring pain points
1. **Skip-heavy orchestration churn**
   - `clarify`: `171` runs, p50 `1s`, but only `20` successes and `151` other/skipped.
   - `plan`: `145` runs, only `17` successes.
   - `implement`: `145` runs, but `124` were other/skipped.
   - This indicates a lot of orchestration wakeups that do almost nothing.

2. **Implement-to-PR handoff is the weakest transition**
   - E2E stable-release testing failed because implement completed without producing a PR.
   - Direct implement runs show no-actionable-output bailouts.

3. **Review is bimodal**
   - Some `review_autofix` runs finish in seconds (`12–39s` post-merge dispatch path).
   - Others take `24–29 minutes` in branch-review mode.
   - This makes capacity planning and queue pressure worse.

### Smallest safe mitigations
- Add an explicit **“implement ended without PR”** terminal state instead of letting downstream poll loops infer it.
- Cancel superseded review runs before reviewer fan-out.
- Track branch-review vs post-merge short-circuit as separate metrics instead of one `review_autofix` bucket.

### Observable indicators teams should track
- `review_autofix` cancellation ratio
- `implement` no-actionable-output bailout count
- `test_and_mark_stable` pass rate
- poller `has_work=false` ratio
- reviewer memory retrieve hit rate
- median `poll/Checkout repository` duration

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Implement → PR creation is the main flow break
- **Evidence**
  - `25271960656` failed waiting for PR creation.
  - Multiple implement runs bailed on no-action retries.
- **Bottleneck type**
  - Retry / orchestration handoff failure.
- **Fix**
  - Treat “implement finished, no PR created” as a first-class failure condition with diagnose/clarify fallback.

### 2. Review/autofix long-path compute dominates real work
- **Evidence**
  - `1457s` and `1712s` branch-review runs.
  - Six reviewer models configured.
- **Bottleneck type**
  - Compute / model fan-out.
- **Fix**
  - Adaptive reviewer panel and earlier supersede cancellation.

### 3. Stable release flow is blocked by deterministic script bugs
- **Evidence**
  - All `3/3` `test_and_mark_stable` runs failed.
- **Bottleneck type**
  - Reliability defect causing full reruns.
- **Fix**
  - Repair release tag push refspecs and sync-to-main repo context first.

### 4. Copilot review cleanup is spending minutes after useful work is done
- **Evidence**
  - Cleanup artifacts dominates `143s` to `4m` on several runs.
- **Bottleneck type**
  - Post-processing overhead.
- **Fix**
  - Scope artifact cleanup tightly or rely more on retention.

### 5. Poller checkout is an avoidable repeated tax
- **Evidence**
  - ~`8.6s` checkout in a `40s` run, due largely to tag fetches.
- **Bottleneck type**
  - Repeated repository I/O.
- **Fix**
  - Disable unnecessary tag fetches for poller checkout.

### 6. Queueing exists, but is usually secondary
- **Evidence**
  - Many runs log `Job is waiting for a hosted runner to come online.`
- **Bottleneck type**
  - Queueing.
- **Fix**
  - The best no-infra mitigation is reducing long/cancelled review jobs; that lowers queue pressure more than tuning tiny no-op workflows.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-path branch review (`1457s`–`1712s`)
- `ci` lint/test job consistently around `595s`–`651s`
- `copilot_pull_request_reviewer` artifact cleanup (`143s`–`4m`)
- `orchestrate_poll` repeated tag-heavy checkout (~`8.6s` of `40s`)

**Top failure modes**
- Stable release lane broken (`test_and_mark_stable` failed `3/3`)
- Implement no-actionable-output bailouts
- E2E implement completed with no PR created
- Nightly validation self-test failed (`2/3` fixtures failed in `25299383150`)

**Highest-cost drivers**
- Multi-model `review_autofix` branch review
- Repeated implement retries with low reasoning and no edits
- Prompt/template instability reducing effective reuse
- Cleanup-heavy Copilot review post-processing

**Top 3 prioritized actions**
1. **Repair `test_and_mark_stable` immediately**
   - Explicit tag refspec push
   - repo-safe `gh workflow run` in sync-to-main
   - implement-without-PR fast-fail
2. **Reduce `review_autofix` branch-review cost**
   - adaptive reviewer fan-out
   - pre-start supersede cancellation
3. **Tighten implement retry policy**
   - early bail after first no-op retry
   - retry-time reasoning escalation
   - prompt placeholder validation restored

## Metrics Appendix

### Overall Window

| Scope | Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | p50 Duration | p95 Duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 843 | 247 | 10 | 36 | 550 | 1.19% | 1s | 612s |

### Key Workflow Family Metrics

| Workflow Family | Runs | Success | Failure | Cancelled | Other/Skipped | p50 | p95 | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `ci` | 48 | 47 | 1 | 0 | 0 | 605s | 649s | Stable but consistently ~10 min |
| `review_autofix` | 66 | 32 | 0 | 32 | 2 | 49s | 1683s | Highly bimodal; long branch-review path dominates |
| `implement` | 145 | 12 | 5 | 4 | 124 | 1s | 175s | Most runs are skipped; executed failures are costly |
| `plan` | 145 | 17 | 0 | 0 | 128 | 1s | 153s | Heavy skip churn |
| `clarify` | 171 | 20 | 0 | 0 | 151 | 1s | 102s | Heavy skip churn |
| `orchestrate_poll` | 48 | 48 | 0 | 0 | 0 | 45s | 119s | Operationally healthy |
| `copilot_pull_request_reviewer` | 19 | 19 | 0 | 0 | 0 | 131s | 316s | Cleanup artifacts is the main outlier |
| `test_and_mark_stable` | 3 | 0 | 3 | 0 | 0 | 3461s | 3655s | Broken release lane |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 0 | 95s | 95s | Failed 2/3 fixtures |
| `workflow_log_analysis` | 3 | 3 | 0 | 0 | 0 | 3024s | 3263s | Very long but low frequency |

### Observed AI Memory Metrics (Deep-Dive Logs)

| Metric | Value |
|---|---:|
| Structured telemetry events found | 65 |
| `retrieve` events | 14 |
| Retrieve hit rate | 35.7% |
| Retrieve zero-record count | 9 |
| Avg `estimated_tokens` on retrieve | 12 |
| `keyword_method=none` | 9 |
| `keyword_method=plain` | 5 |
| `keyword_method=llm` | 0 observed |
| `enabled=false` retrieves | 0 |
| `fail_open=true` structured events | 0 observed |
| Max observed `push_attempts` | 2 |

### Observed Prompt Cache Signals

| Metric | Observation |
|---|---|
| Prompt cache enabled flag | `OPENROUTER_PROMPT_CACHE_DISABLED: false` observed in implement/review/poll samples |
| Cache create/read counters | Not emitted in collected window |
| Cache hit/miss counters | Not emitted in collected window |
| Fragmentation risks | Repeated runtime boilerplate, dynamic noise, unresolved placeholders in implement prompts |

### Observed Token Waste Signals

| Run ID | Workflow | Evidence | Observed Tokens |
|---|---|---|---:|
| `25294079100` | `plan` | Evidence-grade summary: attempts 2–5 used `5,339`, `23,176`, `1,082`, `13,392` tokens before failure | 42,989 |
| `25294055107` | `implement` | Evidence-grade summary captured attempt table with at least attempt 2 token use before bailout | 4,579+ |
| `25293966619` | `implement` | Deep-dive log shows 5-attempt failure with repeated `tokens used` blocks and final failure | not fully extractable from sampled lines |
| `25294005792` | `implement` | Deep-dive log shows 2-attempt no-action bailout | not fully extractable from sampled lines |

### GH API Hotspot Summary

| Workflow / Run | Step | Observed API Pattern | Redundancy Risk | Est. Reducible Calls / Run |
|---|---|---|---|---:|
| `review_autofix` / `25300035937` | `post-merge-validate-dispatch` | GraphQL closing issues, REST PR fallback, per-issue label view, workflow dispatch, issue edit | Medium | 1–3 |
| `issue_pr_status` / `25300035959` | `sync-issue-status` | GraphQL + REST fallback + per-issue label/close/lookups | Medium | 2–4 |
| `cancel_on_pr_close` / `25300035971` | `cancel-active-runs` | `/rate_limit` probe + cancel POST retry wrapper | Low-medium | 1 |
| `copilot_pull_request_reviewer` / `25294004822`, `25295941722`, `25297837347` | `Prepare` / `Cleanup artifacts` | `pulls.get`, paginated `listFiles`, artifact enumeration | Low API risk, high latency cost | modest |

### Notable Failure Runs

| Run ID | Workflow Family | Duration | Failure Point | Key Error |
|---|---|---:|---|---|
| `25271960656` | `test_and_mark_stable` | 3676s | `e2e-smoke-test` → `Phase 3b: Wait for PR creation` | `All 1 implement workflow run(s) completed but no PR was created` |
| `25273372573` | `test_and_mark_stable` | 3235s | `release` → `Tag version and update stable pointer` | `src refspec stable matches more than one` |
| `25281876234` | `test_and_mark_stable` | 3461s | `sync-to-main` → `Dispatch forward-merge-stable-to-main` | `fatal: not a git repository` |
| `25293966619` | `implement` | 331s | `implement / Run Codex implementation` | failed after 5 attempts |
| `25294005792` | `implement` | 159s | `implement / Run Codex implementation` | 2 consecutive no-actionable-output attempts |
| `25299383150` | `nightly_validation_selftest` | 95s | `validation-selftest / Run validation self-test matrix` | `fixtures=3 passed=1 failed=2` |

## Deep Audit — Workflows & Scripts (2026-05-04)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1233-1359`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The `wait-review` loop can declare success while the review workflow is still `in_progress`. At lines 1233-1359 it counts `"Reviewer .* succeeded"` log lines and exits with `status=success` once `SUCCEEDED >= 3` and a simple majority is reached, without waiting for the editor/autofix steps or final workflow conclusion. That means a later editor failure, no-op editor disposition, merge-conflict path, or downstream cleanup failure can be missed by the release gate, because the gate has already advanced.  
  **Recommended fix** — Remove the majority-pass early exit, or gate it behind a second proof that the editor path has completed successfully. The safest fix is to wait for `RUN_STATUS=completed` and `RUN_CONCLUSION=success`, while keeping the existing failed-step and editor-noop early-fail shortcuts.

- **ID** — CONSIST-001  
  **File path** — `.github/workflows/review_autofix.yml:3710-3718,3831-3838,4565-4572`  
  **Severity** — High  
  **Category tag** — `consistency`  
  **Description** — `review_autofix.yml` reintroduces permissive fallback issue parsing that `issue_pr_status.yml` explicitly removed to avoid false positives. The ready-to-merge and review-blocked fallback regexes accept bare `issues/123` and `issue #123` references, while `issue_pr_status.yml:196-210` intentionally limits fallback matches to explicit closing keywords or repo-scoped issue URLs/paths because bare prose references caused incorrect issue mutations in #1469. As written, a PR body that merely mentions another issue can cause `review_autofix` to add `ai:ready-to-merge` or `ai:review-blocked` to an unrelated issue.  
  **Recommended fix** — Centralize fallback issue extraction in one shared helper and reuse the stricter `issue_pr_status.yml` rule set everywhere: only explicit closing keywords and repo-scoped issue URLs/paths should be treated as linked issues.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `.github/workflows/review_autofix.yml:1336-1544`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — `Collect PR metadata` still hydrates PR context with four separate logical API fetches on the common path: one PR metadata call, one paginated issue-comments call, one paginated reviews call, and one paginated review-comments call. The repo already ships `gh_pr_with_all_comments()` in `scripts/gh_helpers.sh:761-899`, which performs a GraphQL-first consolidated fetch and falls back to REST only when pagination/parity requires it.  
  **Recommended fix** — Replace the four inline fetches with one `gh_pr_with_all_comments <owner> <repo> <pr_number> [preloaded_meta_json]` call, then split its JSON into the existing files (`PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEWS_FILE`, `PR_REVIEW_COMMENTS_FILE`). Extend the existing `gh_pr_with_all_comments` batching pattern rather than keeping a second hand-rolled hydrator.  
  **Current call count** — 4 logical calls on the happy path, plus page amplification for comments/reviews.  
  **Proposed call count after fix** — 1 logical call on the happy path, with fallback kept inside the shared helper.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh:761-899` (`gh_pr_with_all_comments`).

- **ID** — API-002  
  **File path** — `.github/workflows/issue_pr_status.yml:297-349,503-512`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The workflow already batch-classifies linked issues as tracking/managed via one GraphQL alias query in `sync-status`, but the later PR-merged Telegram step throws that result away and performs `_safe_gh_jq "repos/.../issues/{n}"` once per linked issue to rediscover whether any issue is orchestrator-managed. This is same-run re-fetching of data that was already available earlier in the execution path.  
  **Recommended fix** — Persist the batch classification from `sync-status` into `GITHUB_ENV` or a temp JSON file, and let the alert step test membership locally instead of issuing per-issue REST GETs.  
  **Current call count** — 1 batched GraphQL classification call + up to **N** per-issue REST body lookups in the alert step.  
  **Proposed call count after fix** — 1 batched GraphQL classification call + 0 extra issue lookups.  
  **Existing batching pattern to extend** — Reuse the same cycle-local cache idea documented in `scripts/orchestrate_poll_process.sh` batched helpers such as `_fetch_candidate_issue_details_graphql`.

- **ID** — API-003  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1255-1403`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — After `ELAPSED >= 600`, the `wait-review` loop downloads the same job log twice in the same polling iteration: first into `LOG_FILE` for marker/reviewer greps, then again via `/actions/jobs/{job_id}/logs` solely to compute `LOG_SIZE`. This doubles the most expensive call in the loop once the editor path is active.  
  **Recommended fix** — Derive `LOG_SIZE` from the already-fetched `LOG_FILE` (`wc -c < "$LOG_FILE"`) before deleting it, and keep the single downloaded copy for both content checks and byte-size tracking.  
  **Current call count** — 2 log-download calls per qualifying polling iteration for the same `job_id`.  
  **Proposed call count after fix** — 1 log-download call per qualifying polling iteration.  
  **Existing batching pattern to extend** — Extend the repo’s cycle-local cache/reuse pattern used in `scripts/orchestrate_poll_process.sh` caches and the step’s own `JOBS_JSON` reuse.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/implement.yml:2236-2269; scripts/orchestrate_poll_process.sh:4664-4694`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The ancestor-chain no-op detector is duplicated in two hot paths. `implement.yml` walks `Re-issued from #N` ancestors inline, and `scripts/orchestrate_poll_process.sh` implements the same traversal in `count_noop_ancestors()`. Both fetch the parent issue body, extract the ancestor number, then fetch parent comments and count the `"produced no repository changes"` marker. This is logic duplication with shared failure semantics and shared bug surface.  
  **Recommended fix** — Move the traversal into a shared helper module, e.g. `scripts/noop_helpers.sh`, with a function signature like `count_noop_ancestors <repo> <issue_num> <max_depth>`. Update callers in `implement.yml` (“Handle no-op implementation”) and `scripts/orchestrate_poll_process.sh` (`close_and_reissue` stall recovery) to use the shared helper.

- **ID** — DUP-002  
  **File path** — `.github/workflows/validate.yml:209-277; .github/workflows/issue_pr_status.yml:65-129`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The support-bootstrap logic is near-identical across workflows: compute `wf_source`, choose `script_ref`, clone support refs, maintain `support_primary_root`/`support_main_root`, and copy files via `copy_from_ref_or_local`. The duplication is already large in `validate.yml` and `issue_pr_status.yml`, and the same pattern appears elsewhere, so future contract changes (fallback rules, self-repo behavior, copy semantics) will drift.  
  **Recommended fix** — Extract a shared bootstrap script, e.g. `scripts/bootstrap_workflow_support.sh`, with helpers such as `checkout_support_ref <remote_url> <ref> <dest>` and `copy_from_ref_or_local <primary_root> <main_root> <repo_path> <target_path> [require_remote] [allow_main_fallback]`. Update `validate.yml` and `issue_pr_status.yml` first; then migrate other workflows incrementally.

- **ID** — DUP-003  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/comprehensive-test-and-release.yml:72-98,320-341; .github/workflows/review_autofix.yml:1254-1292; .github/workflows/test-and-mark-stable.yml:396-410,1133-1160`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Multiple workflows maintain bespoke retry/rate-limit wrappers around `gh api` (`_rl_wait`, `_gh_retry`, `gh_api_safe`, inline `gh_retry`) even though `scripts/gh_helpers.sh` already provides repo-standard `gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, and `curl_gh_api`. The wrappers are behaviorally similar but not identical, so retry policy and rate-limit handling will drift.  
  **Recommended fix** — Standardize on `scripts/gh_helpers.sh` as the owner. Add any missing convenience wrapper there instead of cloning logic into workflows. The useful signature gap is a helper like `gh_retry_to_var <var_name> -- gh api ...` or broader use of `gh_retry_to_file` + `cat`. Update the callers listed above to source the shared helper and delete their inline retry functions.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The interpolated `run:` body for the review wait/analyser block is already about **19,696 characters**, leaving only **1,304 characters** of headroom under GitHub Actions’ 21,000-character expression cap. This block contains dense inline control flow, many `${{ }}` substitutions, and has already been extended several times for smoke-gate behavior, making it the riskiest remaining expression in the repo.  
  **Recommended fix** — Extract the wait-review logic into an external script under `scripts/` and pass only small env vars/args from YAML. That is safer than further splitting ad hoc conditionals into the same block.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/validate.yml:183-476`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Fetch workflow support files` block is about **16,485 characters**, leaving **4,515 characters** of headroom. It combines support-ref checkout, file staging, template copying, schema bootstrapping, and fallback logic in one interpolated `run:` body, so ordinary maintenance can push it over the limit.  
  **Recommended fix** — Move this bootstrap flow into `scripts/bootstrap_workflow_support.sh` and call that script from YAML. This also resolves DUP-002.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/review_autofix.yml:1251-1573`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Collect PR metadata` is about **16,437 characters**, leaving **4,563 characters** of headroom. The step mixes retry wrapper code, PR-context hydration, linked-issue fetching, Python heredocs, and diff generation, so it is both large and still actively edited.  
  **Recommended fix** — Extract the metadata collection into an external shell/Python helper, ideally one that uses `gh_pr_with_all_comments()` from `scripts/gh_helpers.sh` so both expression size and API duplication fall together.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:813-1096`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Parse and post answer` is about **15,140 characters**, leaving **5,860 characters** of headroom. The block embeds claim/loop-guard logic, Telegram escalation, comment construction, and processed-command completion handling in one interpolated shell step.  
  **Recommended fix** — Extract the whole answer-posting/loop-guard sequence into a dedicated script, leaving the workflow step as argument wiring plus environment setup.

Repository-wide note: no audited workflow exceeds the **800 KB** early-warning threshold for the 1 MB workflow file limit. The largest files are `review_autofix.yml` (**266,619 bytes**) and `test-and-mark-stable.yml` (**222,438 bytes**).

### Section 5: Cross-Cutting Concerns

- **ID** — CONSIST-002  
  **File path** — `scripts/tg_helpers.sh:346-350,417-424`  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — Telegram cleanup deletes GitHub tracking comments with raw `curl -s -X DELETE` calls instead of the repo’s standard `curl_gh_api`/`gh_retry` helpers. Unlike the rest of the codebase, these DELETEs have no retry, no rate-limit handling, and silently `|| true` away failures, so transient GitHub failures can leave stale cleanup comments behind without any consistent diagnostic path.  
  **Recommended fix** — Route these deletions through a shared helper in `scripts/gh_helpers.sh` (for example, `curl_gh_api -X DELETE ...` or a tiny `gh_delete_issue_comment <repo> <comment_id>` wrapper) so cleanup behavior matches the rest of the repository’s GitHub API contract.

Cross-cutting note: repository-wide grep over `.github/workflows/*.yml`, `scripts/*.sh`, and `scripts/*.py` found **no TODO/FIXME/HACK/XXX markers**. I am not re-raising the already-documented standalone-state dead-code helper because prior report content already covers that area.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, CONSIST-001, EXPR-001 |
| Medium | 9 | API-001, API-002, API-003, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004 |
| Low | 1 | CONSIST-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2-3 | Medium |
| API call optimization | 3-4 | Medium |
| Code modularization | 7-9 | Large |
| Expression size reduction | 4-8 | Large |
| Medium/Low fixes | 1-2 | Small |
