## Executive Summary

- **Fix the Semble default contract drift first.** In `shubhodeep1/coding-workflows`, all **15 CI failures** in the current window hit the same point: `CI` → job `lint` → step `Review Semble contract test`. In failed run **25646358426**, `tests/test_review_semble_contract.py` asserted `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}`, but `.github/workflows/review_autofix.yml:154` currently sets it to `'true'`. **Estimated impact:** remove most or all of the observed **30.6% CI failure rate** (`15/49`) in this window. **Confidence:** **high**.

- **`review_autofix` is the dominant latency and failure sink.** `review_autofix` logged **122 runs**, **22 failures**, **50 cancellations**, **p50 1008s**, and **p95 4808.55s**. The 22 failing runs alone consumed **93,192s total** (**4,236s average**). Two sampled failures, **25642396253** (**5360s**) and **25642397953** (**4121s**), completed the expensive AI work (`REVIEWERS_SUCCESSFUL: 6`, `DID_COMMIT: true`, `EDITOR_SUMMARY_POSTED: true`) and still ended with `MERGE_CONFLICT: true`, `CONFLICT_RESOLVED: false`. **Estimated impact:** the largest end-to-end savings opportunity in the system. **Confidence:** **high**.

- **The check-run polling loop is a major avoidable critical-path delay and GH API hotspot.** In `review_autofix` run **25646512616**, step `Collect PR check-run failures (CI/lint autofix context)` waited from **02:07:07** to **02:11:14** with **13 observed poll iterations**, while the workflow is configured for **`CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`** and **`CHECK_RUNS_POLL_INTERVAL_SECS=20`**. **Estimated impact:** save **2–20 minutes per affected run** and sharply reduce repeated Actions API reads. **Confidence:** **high**.

- **Semble is currently costing bootstrap time without measurable production prompt benefit.** Sampled production `review_autofix` runs such as **25646512616**, **25642396253**, and **25642397953** all ended with `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`; run **25646512616** spent about **13 seconds** in `Setup uv for Semble` + `Install semble` before logging `Semble 0.1.3 is unavailable`. I found **no structured production `SEMBLE_QUERY target=... bytes=...` lines** in `errors/`, `slow/`, or `recent/` deep dives outside the analysis workflow, so prompt-reduction benefit is currently unmeasurable. **Estimated impact:** low-to-medium per run, but very low risk to fix. **Confidence:** **high**.

- **AI memory retrieval is present but not helping the expensive path yet.** Across **10 unique `review_autofix` runs** with deep-dive `AI_MEMORY_TELEMETRY`, there were **10 unique `retrieve` events**, **0 hits**, **0 selected records**, **0 average estimated tokens**, and `keyword_method: none` every time. The memory system is writing (`record-run-event`, `record-candidate`) but retrieval is inert on the review path. **Estimated impact:** medium for quality/observability, low immediate cost savings. **Confidence:** **high**.

- **Prompt-cache observability is effectively broken.** I found **18 unique cache-probe usage lines across 9 sampled `review_autofix` runs**, but every probe reported `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. The system appears to pay probe overhead without getting actionable hit/miss data. **Estimated impact:** medium cost/telemetry improvement, low implementation risk. **Confidence:** **high**.

## Speed Optimizations

### 1. Short-circuit the `review_autofix` check-run wait loop for prompt context
**Type:** Critical-path win

- **Evidence:** In `review_autofix` run **25646512616**, job `review / codex-agent (claude-branch-review)`, step `Collect PR check-run failures (CI/lint autofix context)`, the workflow logged **13 waits** between **02:07:07** and **02:11:14**, first for **2** in-flight checks and then for **1**, before the run was canceled. The workflow file also hard-codes **`CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`** and **`CHECK_RUNS_POLL_INTERVAL_SECS: 20`** in `.github/workflows/review_autofix.yml:152-153`, and the polling loop is implemented at `.github/workflows/review_autofix.yml:1785-1854`.
- **Root cause:** A **prompt-context collection step** is behaving like a **hard merge gate**, repeatedly polling `/commits/{sha}/check-runs` even though the step already documents itself as fail-open and the prompt logic treats non-`ready` status as “no signal.”
- **Exact change:** Convert the step to **immediate snapshot + optional one refresh**, or at minimum reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` from **1200** to **60–120** seconds and `CHECK_RUNS_POLL_INTERVAL_SECS` from **20** to **30–60** seconds. If you want to preserve some waiting behavior, wait only for a narrow allowlist of contexts actually used in the prompt instead of all queued/in-progress check-runs on the SHA.
- **Estimated time savings:** **~257s in the sampled canceled run**; up to **18 minutes** on runs that would otherwise hit the full 1200s ceiling.
- **Implementation risk:** **Low.** The step already fails open and writes `collection_status` sentinels instead of failing the workflow.

### 2. Move merge-topology/conflict prechecks ahead of the 6-reviewer + editor spend
**Type:** Critical-path win

- **Evidence:** Failed `review_autofix` runs **25642396253** (**5360s**) and **25642397953** (**4121s**) both reached the end of the expensive path with:
  - `REVIEWERS_SUCCESSFUL: 6`
  - `DID_COMMIT: true`
  - `EDITOR_SUMMARY_POSTED: true`
  - `MERGE_CONFLICT: true`
  - `CONFLICT_RESOLVED: false`
  
  In **25642397953**, the same step also logged a deterministic merge-precheck failure: `HEAD (...) and origin/main (...) have no common ancestor... Manual repair required: rebase...`.
- **Root cause:** Deterministic branch-topology / mergeability failures are being discovered **after** reviewer fan-out, editor work, and commit validation.
- **Exact change:** Run the existing merge precheck logic immediately after checkout and before reviewer fan-out:
  - `git merge-base` / no-common-ancestor detection
  - lightweight `git merge-tree` preflight for obvious textual conflicts
  - if deterministic failure is found, jump directly to conflict-resolver dispatch or fail fast with a “manual rebase required” outcome
- **Estimated time savings:** **68–89 minutes per affected run** in the two sampled failures.
- **Implementation risk:** **Low to medium.** Keep the new precheck **fail-open on unknown** and only short-circuit on deterministic states.

### 3. Debounce `claude/**` branch-review runs before launching the full reviewer panel
**Type:** Critical-path win

- **Evidence:** The wrapper workflow `.github/workflows/internal-review.yml:48-50` sets `cancel-in-progress` for `claude/` branches, and the reusable job `.github/workflows/review_autofix.yml:705-732` also sets `cancel-in-progress` when `claude_branch_review` is true. Recent run **25646512616** (`Internal: AI Review & Autofix`) was **canceled after 445s** while the `claude-branch-review` path was doing real work. By contrast, recent run **25646929515** completed in **41s** because the gate recognized `AUTOFIX_GATE_SKIP reason=self_triggered_autofix`.
- **Root cause:** Successive pushes to `claude/**` can cancel in-progress review runs **after** those runs have already spent several minutes in reviewer/comment-only work.
- **Exact change:** Add a **quiet-period / no-new-SHA** guard at the start of `claude_branch_review` mode, before reviewer fan-out. A simple pattern is:
  - wait 60–90 seconds
  - verify the head SHA is still current
  - only then launch reviewers
- **Estimated time savings:** **7–22 minutes** on the expensive canceled tail; concrete observed examples include **445s** canceled run **25646512616** and **1343s** canceled run **25646357179** from the recent-run evidence.
- **Implementation risk:** **Medium.** It slightly delays first feedback on active branches, but it preserves latest-only behavior and should reduce wasted compute.

### 4. Stop bootstrapping Semble on the default-unavailable path
**Type:** Medium win

- **Evidence:** In `review_autofix` run **25646512616**:
  - `Setup uv for Semble` ran from **02:06:29.506** to **02:06:30.883** (~**1.4s**)
  - `Install semble` ran from **02:06:30.892** to **02:06:42.924** (~**12.0s**)
  - then logged `install_semble: Semble 0.1.3 is unavailable; callers should use fallback paths`
  - `Build semble index` immediately logged `Semble unavailable; skipping index build`
- **Root cause:** The workflow enables and bootstraps Semble even when the binary/index path is unavailable and no measurable production query telemetry is emitted.
- **Exact change:** Restore the workflow default to opt-in (`SEMBLE_ENABLED=false` unless explicitly enabled via repo var) and gate the uv/install/index steps behind that opt-in.
- **Estimated time savings:** **~13–15s per `review_autofix` run**.
- **Implementation risk:** **Low.** Explicit opt-in remains available via repo vars.

### 5. Micro-optimization: target single-artifact cleanup in Copilot review runs
**Type:** Micro-optimization

- **Evidence:** Recent `copilot_pull_request_reviewer` runs **25646930564** and **25646513314** each listed exactly one artifact, `results-agent`, then performed:
  - one `GET /actions/runs/{run_id}/artifacts`
  - one `DELETE /actions/artifacts/{artifact_id}`
- **Root cause:** A generic “list all then delete in a loop” cleanup path is used even when there is only one artifact.
- **Exact change:** If the workflow can reuse a known artifact ID from the upload step, delete that directly; otherwise keep the current listing path as fallback.
- **Estimated time savings:** **Sub-second** per run.
- **Implementation risk:** **Low**, but the impact is small.

## Cost Optimizations

Before the ranked items: **I could not compute total token spend or dollar spend directly** from this window because the sampled production logs do **not** emit usable prompt/completion totals for the actual reviewer/editor calls. The only token/cache-related production lines I found were cache probes, and those reported all counters as `na`. So the savings estimates below are bounded and, where applicable, explicitly marked as inference.

### 1. Eliminate full reviewer/editor spend on runs that are merge-impossible
- **Evidence:** `review_autofix` failures **25642396253** and **25642397953** both burned **4121–5360s** and still ended with unresolved conflicts after `REVIEWERS_SUCCESSFUL: 6`, `DID_COMMIT: true`, and `EDITOR_SUMMARY_POSTED: true`. Across the whole window, the **22 failing `review_autofix` runs** consumed **93,192s total**.
- **Root cause:** Expensive AI reasoning is being spent before deterministic merge-topology failures are screened out.
- **Exact change:** Move the merge precheck / merge-tree conflict probe ahead of the reviewer/editor stages and immediately route deterministic failures to conflict resolution or manual repair.
- **Estimated savings:** **Largest in the system.** On the sampled failures alone, this avoids nearly the entire 6-reviewer + editor spend and **68–89 minutes** of runner time per run.
- **Quality-risk notes:** **Low** if the precheck short-circuits only on deterministic failures and **fails open** on ambiguous states.

### 2. Downshift reviewer breadth and reasoning on lower-risk PRs
- **Evidence:** Successful `review_autofix` run **25646298616** (**1490s**) logged:
  - `REVIEWER_MODELS: minimax/minimax-m2.5 moonshotai/kimi-k2.5 deepseek/deepseek-v4-pro z-ai/glm-5 qwen/qwen3.6-plus x-ai/grok-4.1-fast`
  - `MODEL_EDITOR: openai/gpt-5.4`
  - `REVIEWER_REASONING_EFFORT: xhigh`
  - `EDITOR_REASONING_EFFORT: xhigh`
  
  This is an expensive default posture for every non-skipped review.
- **Root cause:** Premium review configuration is applied broadly, not just to the hardest PRs.
- **Exact change:** **Inference:** keep the full six-reviewer / `xhigh` stack only for:
  - `force_review_marker`
  - merge-conflict resolution
  - very large/high-churn diffs
  - orchestrator final-merge / integration-conflict paths
  
  For ordinary non-conflict PRs, reduce reviewer breadth to **2–3 models** and/or lower reviewer reasoning from `xhigh` to `high`.
- **Estimated savings:** **Inference:** cutting the reviewer pool from **6 → 3** should reduce reviewer-side token spend by roughly **~50%** on downgraded runs.
- **Quality-risk notes:** **Medium.** Use repo vars and start with a canary subset; preserve the current full stack for conflict-heavy and force-review paths.

### 3. Stop paying for cache probes that return no usable cache counters
- **Evidence:** I found **18 unique `INFO: openrouter usage phase=review_autofix_cache_probe` lines across 9 sampled `review_autofix` runs**, and every one reported:
  - `prompt_tokens=na`
  - `completion_tokens=na`
  - `total_tokens=na`
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`
- **Root cause:** The system is making extra model calls for cache observability, but the provider telemetry is not actionable.
- **Exact change:** Sample cache probes on a small fraction of runs, or move cache logging to the actual reviewer/editor calls where it would be decision-useful.
- **Estimated savings:** **Two model calls per `review_autofix` run** plus cleaner logs.
- **Quality-risk notes:** **Low.** This changes telemetry collection, not review quality.

### 4. Restore Semble to explicit opt-in until it proves prompt-shrinking value
- **Evidence:** `.github/workflows/review_autofix.yml:154` defaults `SEMBLE_ENABLED` to `true`, but `probably_unnecessary_but_read_if_stuck.md:689` says the default is intentionally `false`, and `tests/test_review_semble_contract.py` enforces the same contract. In sampled production runs, Semble was unavailable (`SEMBLE_AVAILABLE=false`, `SEMBLE_INDEX_AVAILABLE=false`) and I found **no structured production `SEMBLE_QUERY target=... bytes=...` lines**.
- **Root cause:** A feature that is supposed to be opt-in is wired as opt-out in the workflow, while still not demonstrating production usage savings.
- **Exact change:** Set the workflow default back to `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}` and keep explicit repo-var opt-in for repos ready to soak it.
- **Estimated savings:** Small direct cost savings, plus removal of a low-value setup path and lower prompt-path complexity.
- **Quality-risk notes:** **Low.** Any repo that truly wants Semble can still set `vars.SEMBLE_ENABLED=true`.

### 5. Treat AI memory retrieval as a quality improvement, not a present-day cost reducer
- **Evidence:** Across **10 unique `review_autofix` runs** with deep-dive memory telemetry, there were **10 unique `retrieve` events**, and **all 10** had `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: none`.
- **Root cause:** The memory store is being written to, but the retrieval side is not surfacing relevant records for reviewer prompts.
- **Exact change:** Suppress retrieval on run classes with a repeated 0-hit streak until retrieval quality improves, or improve keyword extraction / indexing first.
- **Estimated savings:** Low in the current data, because retrieved token volume is already zero; the benefit is mainly avoiding dead work and prompt variance.
- **Quality-risk notes:** **Low** if suppression is conditional and reversible.

**Semble query volume / logged bytes assessment:** I found **no production `SEMBLE_QUERY target=... bytes=...` telemetry** in the deep-dive logs, so I cannot conclude that Semble is reducing prompt expansion in this window. The only structured Semble telemetry I found was CI-side `SEMBLE_FALLBACK target=overflow ... ms=0` test output, which does **not** indicate production prompt savings or noise.

## Reliability Improvements

### 1. Fix the workflow/test/doc contract drift for `SEMBLE_ENABLED`
- **Failure evidence:** All **15 CI failures** in `analysis_context.json` hit `CI` → `lint` → `Review Semble contract test`. In failed run **25646358426**, `tests/test_review_semble_contract.py` raised `AssertionError` because it expected `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}` while `.github/workflows/review_autofix.yml:154` currently uses `'true'`.
- **Root cause category:** Configuration contract drift.
- **Exact fix:** Change `.github/workflows/review_autofix.yml:154` to `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}` so the workflow matches the test and the repo docs.
- **Expected reliability impact:** Should remove the specific failure mode behind the current **15/49 CI failures**.
- **Rollback / fail-open:** Any caller repo that wants Semble can still set `vars.SEMBLE_ENABLED=true`.

**Related `SEMBLE_FALLBACK` assessment:** I found **75 unique fallback lines across 15 sampled CI runs**, all in `CI` contract-test steps, all `target=overflow`, all `ms=0`, and all referencing temp `missing_semble` paths. This looks like **healthy fail-open test coverage**, not a masked production rollout breakage. The smallest safe mitigation is **not** to suppress these test lines; it is to stop the unrelated default-wiring drift from failing CI.

### 2. Fail fast on deterministic merge-precheck failures instead of failing late after full AI work
- **Failure evidence:** In `review_autofix` failures **25642396253** and **25642397953**, the same final step `Run Codex resolver, validate, stage, commit` burned **5360s** and **4121s** respectively, then ended with `MERGE_CONFLICT: true` and `CONFLICT_RESOLVED: false`. Run **25642397953** explicitly logged `HEAD (...) and origin/main (...) have no common ancestor`.
- **Root cause category:** Late-discovered merge / branch-topology failure.
- **Exact fix:** Move the merge precheck ahead of reviewer/editor fan-out and only spend AI review tokens when the branch topology is sane.
- **Expected reliability impact:** Reduces both outright failures and expensive reruns caused by repeated late conflict discovery.
- **Rollback / fail-open:** Only short-circuit on deterministic failures; continue current behavior on unknown/unstable mergeability states.

### 3. Reduce cancellation exposure on `claude/**` review paths
- **Failure evidence:** `review_autofix` has **50 cancellations in 122 runs**. Some are healthy and cheap (**25646511535** canceled in **3s**, **25646690800** in **4s**), but others are expensive, including **25646512616** (**445s**) and **25646357179** (**1343s**, from log summary).
- **Root cause category:** Concurrency/cancel amplification during bursty branch updates.
- **Exact fix:** Add a quiet-period/no-new-SHA guard before launching `claude_branch_review` reviewers; keep cancel-in-progress semantics, but move expensive work after the branch settles.
- **Expected reliability impact:** Fewer abandoned in-flight reviews and fewer user-visible “run canceled after doing real work” cases.
- **Rollback / fail-open:** If the debounce logic errors, fall back to the current immediate-run behavior.

### 4. Update Node 20–based actions before the forced runtime switch
- **Failure evidence:** Recent and failing runs, including `review_autofix` **25642396253**, **25642397953**, and recent CI **25646512550**, logged warnings that `actions/cache/restore@v4`, `actions/cache/save@v4`, and `astral-sh/setup-uv@v3` are still on **Node.js 20**, with the runner warning that Node 24 becomes the default on **June 2, 2026** and Node 20 is removed on **September 16, 2026**.
- **Root cause category:** Upcoming runtime deprecation.
- **Exact fix:** Upgrade or validate the affected actions on Node 24 now, while the change is still reversible.
- **Expected reliability impact:** Prevents a near-term, date-driven workflow breakage.
- **Rollback / fail-open:** Validate on a branch / PR first; if a specific action regresses, pin or replace only that action.

### 5. Shrink the check-run wait window to reduce stale-context and cancel-risk failures
- **Failure evidence:** Run **25646512616** spent most of its visible codex-agent time in the check-run wait loop, then was canceled while still waiting on sibling checks.
- **Root cause category:** Retry/wait amplification.
- **Exact fix:** Reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` and snapshot sooner.
- **Expected reliability impact:** Fewer runs that are canceled or superseded while passively waiting.
- **Rollback / fail-open:** Revert the timeout via repo var if operators need longer waits temporarily.

## AI Memory Health

### What the deep-dive telemetry shows
Across the sampled deep-dive logs in `errors/`, `slow/`, and `recent/` (excluding the analysis workflow to avoid double-counting quoted telemetry), I found **39 unique `AI_MEMORY_TELEMETRY` events** on the expensive review path:

- **20** `record-run-event`
  - **10** `phase_started`
  - **10** `phase_failed`
- **10** `retrieve`
- **9** `record-candidate`

All of those deep-dive events came from **10 unique `review_autofix` runs**:
`25634376141`, `25636499205`, `25636502765`, `25638352861`, `25640363259`, `25640364808`, `25642391680`, `25642396253`, `25642397953`, `25646512616`.

### Retrieval effectiveness
- **Retrieve hit rate:** **0 / 10 = 0%**
- **Average `estimated_tokens`:** **0**
- **`keyword_method` distribution:** **100% `none`**
- **`records_selected > 0`:** **0 events**
- **`fail_open: true`:** **0 events**
- **`enabled: false`:** **0 events**

That means the memory retrieval path is **enabled and succeeding operationally**, but it is not returning useful records for reviewer prompts in the sampled `review_autofix` runs.

### Push reliability
Only **2 unique memory write events** showed elevated push retries:
- `record-candidate` in `review_autofix` run **25642396253** had `push_attempts: 2`
- `record-run-event phase_started` in `review_autofix` run **25638352861** had `push_attempts: 2`

Everything else in the sampled deep dives pushed in **1 attempt**.

### Lifecycle coverage gaps
In the deep-dive files for the expensive review path, I did **not** find:
- `promote`
- `finalize-task`
- `processed-command-claim`
- `processed-command-complete`

However, evidence-grade `log_summary` entries outside the deep-dive set show those lifecycle stages do exist elsewhere:
- `issue_pr_status` run **25646393403** logged `AI_MEMORY_TELEMETRY: {"op":"finalize-task", ... "did_push": true, "final_state": "closed", "issue_number": 2486}`
- `memory_maintenance` run **25646287867** logged `AI_MEMORY_TELEMETRY: {"op":"compact", "archived_candidates": 2914, ... "did_push": true}`

So the memory system is not absent; it is **partially visible** on the sampled expensive path and **more fully visible** in other workflow families.

### Recommendation
- Keep emitting the current telemetry, but add a **retrieve budget field** so budget utilization can be measured; right now I can report `estimated_tokens=0` but **cannot compute estimated-tokens-vs-budget** because no budget field was emitted.
- Add a simple run-class KPI: **memory retrieve hit rate** by workflow family. Right now the sampled `review_autofix` hit rate is **0%**, which makes it an easy regression/health signal.
- If the 0-hit pattern continues, suppress retrieval on that path until record quality improves.

## GH API Call Audit

The repo’s own API hygiene rules are explicit in `codex.md` §15: **prefer batched GraphQL over per-item REST**, especially inside loops. The biggest observed and code-level GH API opportunities are below.

### 1. `review_autofix` check-run polling is the top observed API hotspot
- **Evidence:** `.github/workflows/review_autofix.yml:1785-1854` polls `GET /repos/{repo}/commits/{HEAD_SHA}/check-runs?per_page=100` every **20s** for up to **1200s**. In sampled run **25646512616**, step `Collect PR check-run failures (CI/lint autofix context)` logged **13 wait iterations** over ~**257s** before cancellation.
- **High-redundancy pattern:** Re-reading the same check-run snapshot in a tight loop for prompt context.
- **Concrete batching/reuse change:** Replace repeated polling with:
  1. one immediate snapshot,
  2. optional one delayed refresh,
  3. or a narrower allowlist of contexts if waiting is truly necessary.
- **Estimated call-count reduction:** From **up to 60 logical calls/run** by configuration to **1–4 calls/run**.
- **Rate-limit risk reduction:** High on synchronize-heavy branches because this endpoint is hit repeatedly for the same SHA.

### 2. `review_autofix` linked-issue fallback still uses per-item REST in loops
- **Evidence:** In `.github/workflows/review_autofix.yml:1488-1549`, when `closingIssuesReferences` comes back empty, the workflow parses the PR body and then loops over distinct issue numbers, calling `GET /repos/{repo}/issues/{n}` for each issue, capped at **20**. In `.github/workflows/review_autofix.yml:537-543`, `post-merge validate dispatch` also calls `gh issue view` per issue when labels are not already known.
- **High-redundancy pattern:** GraphQL first, then per-item REST lookups in loops.
- **Concrete batching/reuse change:** Extend the initial GraphQL fetch to include the fields downstream steps actually need (title/body/labels), or issue a single batched GraphQL query for the referenced issue numbers. That is exactly the pattern the repo’s docs point to (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`).
- **Estimated call-count reduction:** Worst-case **O(N) → O(1–2)** per run; on a 20-issue fallback path this avoids up to **20 REST issue fetches**, plus any follow-on per-issue label reads.
- **Rate-limit risk reduction:** Medium; this path is bursty and concentrated in one workflow family.

### 3. Copilot artifact cleanup is small but still unnecessarily chatty on 1-artifact runs
- **Evidence:** Recent `copilot_pull_request_reviewer` runs **25646930564** and **25646513314** each did:
  - `GET /repos/shubhodeep1/coding-workflows/actions/runs/{run_id}/artifacts`
  - then `DELETE /repos/shubhodeep1/coding-workflows/actions/artifacts/{artifact_id}`
  
  In both sampled runs there was exactly **one artifact** (`results-agent`).
- **High-redundancy pattern:** List-all + loop for a single known artifact.
- **Concrete batching/reuse change:** Reuse a known artifact ID from the upload path if available; otherwise keep the current listing path as fallback.
- **Estimated call-count reduction:** **2 → 1 GH API call/run** on the common 1-artifact case.
- **Rate-limit risk reduction:** Low, but it helps the short tail of these runs.

### 4. Good existing hygiene to keep
Not every GH API path is a problem:
- The `review_autofix` synchronize gate intentionally adds **at most one** `GET /commits/{sha}` call to detect self-triggered autofix pushes and can skip the entire downstream AI chain. That is a **good trade**.
- The repo docs also document several orchestrator-side changes that explicitly avoid new GH API calls; keep following that pattern.

### 5. Current rate-limit posture
- I found **no sampled 429 / secondary rate-limit incidents** in the recent `review_autofix`, `copilot_pull_request_reviewer`, or `ci` logs I inspected.
- The risk is therefore **preventive** rather than reactive: repeated polling and per-item loops are the main places where future scale problems will surface first.

## Prompt Cache & Memory System

### Prompt cache
- **What exists now:** The workflow has a dedicated step, `Pre-assemble static context (cacheable across runs)`, and the repo includes `scripts/build_static_context.sh`, which is the right structural idea for provider-side caching. Sampled runs also consistently set `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **What is missing:** Measurable cache effectiveness. In the sampled `review_autofix` runs, the only cache-related usage lines were `review_autofix_cache_probe` entries, and **all numeric counters were `na`**:
  - `prompt_tokens=na`
  - `completion_tokens=na`
  - `total_tokens=na`
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`
- **Why this matters:** Right now you cannot tell whether the static-prefix design is actually paying off or whether prompt fragmentation is defeating it.

### Likely fragmentation sources
These are **inferences**, but they fit the current prompt assembly design:
- `PR_CHECK_RUNS_CONTEXT`
- linked-issue body fallback content
- memory retrieval output
- any future structured Semble output

If any of those dynamic blocks leak into the cacheable prefix instead of staying after it, provider cache reuse across autofix iterations will collapse.

### Concrete improvements
1. **Log cache metrics on the real reviewer/editor calls, not just on the probe.**
   - **Impact:** turns the cache into something you can tune instead of just assume.
   - **Risk:** low.

2. **Sample or disable the cache probe until counters are numeric.**
   - **Impact:** saves **2 model calls per sampled `review_autofix` run**.
   - **Risk:** low.

3. **Keep all volatile context strictly after the static prefix.**
   - **Impact:** **inference**—best chance of increasing cache hit rate in autofix iteration loops.
   - **Risk:** low.

### Memory retrieval effectiveness
- The memory system is writing events and candidates, but sampled retrieval is **0-hit / 0-token / keyword_method=none**.
- That means the memory system is currently **more useful as a write ledger than as prompt enrichment** on the expensive review path.

### Recommendation
Treat the combined prompt-cache/memory system as a two-step program:
1. **Fix observability first**: numeric cache counters, retrieve budget field, retrieve hit-rate dashboard.
2. **Then tune behavior**: sample probes, suppress dead retrieval paths, and only invest in Semble/measured context compression when the telemetry is trustworthy.

## Orchestrator Health

### What looks healthy
- The front half of the pipeline is successfully avoiding unnecessary work:
  - `clarify` has **195 runs**, **p50 1s**
  - `plan` has **182 runs**, **p50 1s**
  - `implement` has **182 runs**, **p50 1s**
  - `orchestrate_clarify_respond` has **182 runs**, **p50 1s**
  
  Recent runs **25646927883** (`clarify`), **25646927876** (`plan`), **25646927882** (`implement`), and **25646927874** (`orchestrate_clarify_respond`) all completed in **1s** because their conditions evaluated false. That is healthy gate behavior, not waste.

- The orchestrator poller itself is stable:
  - `orchestrate_poll` has **21 runs**, **0 failures**, **avg 114.4s**, **p50 112s**, **p95 133s**
  - Recent run **25646422707** succeeded in **110s**, with the `poll` step accounting for ~**104s** of that runtime.

- Terminal-state handling is working in sampled evidence-grade summaries:
  - `issue_pr_status` run **25646393403** closed issue **2486** / PR **2493** and emitted `finalize-task`
  - `memory_maintenance` run **25646287867** compacted **2914 archived candidates**

### Operational pain points
1. **Review/orchestrator boundary remains the pain point.** The orchestrator-facing stages are relatively cheap; the handoff into `review_autofix` is where long waits, cancellations, and late conflicts accumulate.
2. **Support-source fallback warning needs watching.** `issue_pr_status` run **25646393403** logged `::warning::Support checkout ref ${script_ref} is unavailable; using main.` That is a fail-open behavior, but repeated occurrences would increase drift between the triggering ref and the support scripts actually executed.
3. **`claude/**` cancellation policy is likely over-eager for long reviewer work.** The latest-only semantics are good, but expensive canceled runs show the settle point is too late.

### Smallest safe mitigations
- Add a **quiet-period** before `claude_branch_review` fan-out.
- Add a cheap **early merge precheck** before reviewer/editor work.
- Track how often support-source checkout falls back to `main`; if it becomes frequent, fix the ref resolution path before it becomes a correctness issue.

### Observable indicators to track
These are the most actionable health indicators from the current data:
- `review_autofix` **canceled-after-300s** count
- `review_autofix` **late merge-conflict failure** count (`MERGE_CONFLICT=true` + `CONFLICT_RESOLVED=false`)
- average **check-run polling seconds** per `review_autofix` run
- AI memory **retrieve hit rate**
- prompt-cache **measurable-call ratio** (calls with numeric token/cache counters)
- count of `Support checkout ref ... unavailable; using main` warnings

## Pipeline Flow Bottlenecks

### 1. Clarify → plan → implement gates are not the bottleneck
The aggregate numbers make this clear:
- `clarify` p50 **1s**
- `plan` p50 **1s**
- `implement` p50 **1s**
- `orchestrate_clarify_respond` p50 **1s**

These stages are mostly acting as **cheap dispatch filters**.

### 2. Review/autofix is the dominant end-to-end bottleneck
`review_autofix` is where the pipeline spends most of its time and variance:
- **122 runs**
- **46 success / 22 failure / 50 cancelled / 4 other**
- **avg 1393s**
- **p50 1008s**
- **p95 4808.55s**

Within that family, the dominant sub-bottlenecks are:
- **compute:** 6-reviewer + editor at `xhigh`
- **wait:** check-run polling for prompt context
- **merge/conflict overhead:** late unresolved conflict discovery
- **cancel overhead:** long-running `claude/**` review jobs canceled mid-flight

### 3. CI is the next visible pipeline blocker, but the fix is small
`CI` is much shorter than `review_autofix`, but it is currently noisy:
- **49 runs**
- **34 success / 15 failure**
- **avg 639s**
- **p50 647s**
- **p95 687.6s**

The good news: all 15 failures point to the same small contract drift, so the current CI bottleneck is **highly fixable**.

### 4. Queueing exists, but compute and duplicate work are the better levers
I saw runner-queue messages in recent `CI`, `review_autofix`, and `copilot_pull_request_reviewer` runs, but the logs do **not** emit enough timing detail to quantify queue delay precisely. Since new infrastructure is off-limits anyway, the best queue mitigation is to:
- remove redundant `review_autofix` cancels
- shorten check-run waits
- stop running unavailable Semble bootstrap
- fix CI so reruns are not self-inflicted

### 5. Merge/conflict overhead is the highest-value flow fix
The biggest end-to-end waste is:
1. enter `review_autofix`
2. run reviewers/editor/commit
3. discover a deterministic merge problem late
4. fail or get canceled
5. rerun on the next event

That is the loop to break first.

### Recommended order of attack by end-to-end impact
1. **Fix the Semble contract drift** so CI stops failing for a trivial reason.
2. **Move merge prechecks ahead of reviewer/editor** to prevent 4,000–5,000s late failures.
3. **Cap or redesign the check-run context wait loop**.
4. **Debounce `claude/**` branch reviews** before reviewer fan-out.
5. **Downshift low-risk reviewer breadth/reasoning** once the failure/cancel loops are under control.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` latency and variance: **p50 1008s**, **p95 4808.55s**
- `CI` runtime: steady **~10–11 minutes** even when healthy
- repeated `review_autofix` check-run waiting and late merge/conflict discovery

**Top failure modes**
- CI contract drift around `SEMBLE_ENABLED` defaulting
- late `review_autofix` failures after full AI work (`MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`)
- expensive canceled `claude/**` review runs

**Highest-cost drivers**
- six-reviewer / `xhigh` default review posture
- long check-run polling for non-blocking prompt context
- expensive late failures and cancellations
- cache probes with no usable counters
- Semble bootstrap on runs where Semble is unavailable

**Top 3 prioritized actions**
1. **Change `.github/workflows/review_autofix.yml:154` back to `SEMBLE_ENABLED || 'false'`.**
   - Fastest high-confidence reliability win; should clear the CI failure cluster.

2. **Front-load merge precheck + shorten check-run wait.**
   - Biggest combined speed, cost, and reliability improvement on the critical path.

3. **Gate premium review configuration by risk tier.**
   - After the failure loops are fixed, reduce reviewer breadth/reasoning on ordinary PRs to lower cost without touching high-risk flows.

## Metrics Appendix

### Workflow-family metrics

| Scope | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repository total | 1000 | 193 | 37 | 55 | 715 | 3.7% | 231.7 | 1.0 | 1462.0 |
| review_autofix | 122 | 46 | 22 | 50 | 4 | 18.0% | 1393.3 | 1008.0 | 4808.6 |
| ci | 49 | 34 | 15 | 0 | 0 | 30.6% | 639.4 | 647.0 | 687.6 |
| copilot_pull_request_reviewer | 29 | 29 | 0 | 0 | 0 | 0.0% | 181.8 | 182.0 | 294.2 |
| orchestrate_poll | 21 | 21 | 0 | 0 | 0 | 0.0% | 114.4 | 112.0 | 133.0 |
| clarify | 195 | 10 | 0 | 0 | 185 | 0.0% | 7.1 | 1.0 | 31.1 |
| plan | 182 | 8 | 0 | 0 | 174 | 0.0% | 5.6 | 1.0 | 8.9 |
| implement | 182 | 8 | 0 | 4 | 170 | 0.0% | 11.9 | 1.0 | 22.4 |

**Note:** the repository-wide `p50=1s` is dominated by skipped/other runs, so workflow-family metrics are much more decision-useful than the repo aggregate.

### Failure concentration

| Workflow family | Failure count | Common failure point | Evidence |
|---|---:|---|---|
| review_autofix | 22 | `review / codex-agent` → `Run Codex resolver, validate, stage, commit` | All failing `review_autofix` rows in `analysis_context.json` |
| ci | 15 | `lint` → `Review Semble contract test` | All failing `ci` rows in `analysis_context.json`; deep-dive run `25646358426` confirms assertion |

### Representative outlier runs

| Run ID | Workflow family | Conclusion | Duration (s) | Notable bottleneck / failure evidence |
|---|---|---|---:|---|
| 25642396253 | review_autofix | failure | 5360 | `REVIEWERS_SUCCESSFUL: 6`, `DID_COMMIT: true`, `EDITOR_SUMMARY_POSTED: true`, then `MERGE_CONFLICT: true`, `CONFLICT_RESOLVED: false` |
| 25642397953 | review_autofix | failure | 4121 | Same late conflict pattern; also logged no-common-ancestor merge precheck failure |
| 25646512616 | review_autofix | cancelled | 445 | `Collect PR check-run failures...` waited ~257s with 13 observed poll iterations before cancellation |
| 25646298616 | review_autofix | success | 1490 | Six reviewer models + `xhigh` reviewer/editor reasoning; review gate dominated runtime |
| 25646358426 | ci | failure | 622 | `Review Semble contract test` assertion failure on `SEMBLE_ENABLED` default |
| 25646691686 | ci | success | 711 | Healthy CI still spends ~11.8 min in lint/test work |

### Token / cache / memory telemetry

| Metric | Value | Notes |
|---|---:|---|
| Actual reviewer/editor token totals | Not emitted | No usable prompt/completion totals for sampled production reviewer/editor calls |
| Unique cache-probe usage lines | 18 | Across 9 sampled `review_autofix` runs |
| Cache probes with numeric `prompt_tokens` / `total_tokens` | 0 / 18 | All reported `na` |
| Cache probes with numeric cache read/write counters | 0 / 18 | All reported `na` |
| AI memory unique events | 39 | 20 `record-run-event`, 10 `retrieve`, 9 `record-candidate` |
| AI memory retrieve hit rate | 0 / 10 | 0% |
| Avg memory retrieve `estimated_tokens` | 0.0 | No retrieve budget field emitted |
| Retrieve `keyword_method` | 100% `none` | 10 / 10 sampled retrieves |
| Memory write events with `push_attempts > 1` | 2 | One `record-candidate`, one `record-run-event` |

### GH API call summary

| Workflow / job / step | Pattern | Observed or configured call volume | Optimization opportunity |
|---|---|---|---|
| review_autofix / codex-agent / `Collect PR check-run failures (CI/lint autofix context)` | `GET /commits/{sha}/check-runs` polling loop | **13 observed** polls over ~257s in run `25646512616`; **max 60/run** by config (`1200s / 20s`) | Highest-value API reduction |
| review_autofix / codex-agent / linked-issue fallback | GraphQL then per-issue `GET /issues/{n}` loop | Up to **20 REST calls/run** on fallback path | Batch into GraphQL |
| review_autofix / post-merge validate dispatch | `gh issue view` per issue when labels unknown | O(N) per run | Batch labels in initial fetch |
| copilot_pull_request_reviewer / `Cleanup artifacts` | list artifacts + delete each artifact | **2 calls/run observed** on 1-artifact runs `25646930564`, `25646513314` | Reuse known artifact ID if possible |

### Semble telemetry summary

| Metric | Value | Notes |
|---|---:|---|
| Structured production `SEMBLE_QUERY target=... bytes=...` lines in sampled deep dives | 0 | None found outside the analysis workflow |
| Unique `SEMBLE_FALLBACK` lines | 75 | Across 15 sampled CI runs |
| Workflows emitting structured `SEMBLE_FALLBACK` | 1 | `ci` only |
| Fallback target distribution | 100% `overflow` | 75 / 75 |
| Logged bytes in structured Semble telemetry | Not present | No `bytes=` field on sampled fallback lines |
| Typical fallback latency | `ms=0` | In sampled CI contract-test lines |

