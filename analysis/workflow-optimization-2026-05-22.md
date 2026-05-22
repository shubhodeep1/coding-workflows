## Executive Summary

- **`review_autofix` is the main critical-path bottleneck.** It consumed **132,292s / 36.7h** of the **189,394s** sampled runtime (**69.9%**), with **p50 1,278.5s** and **p95 4,061.1s** across **100** runs. The biggest safe wins are shortening the PR check-run wait loop and restoring a real small-diff pass-2 gate. **Estimated impact:** ~10–20 minutes off slow review runs. **Confidence:** high.
- **Comment-triggered wrapper fan-out is creating massive no-op noise.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced **697 skipped runs**. At **2026-05-22 04:37:04 UTC**, one comment spawned **plan 26268681311**, **implement 26268681263**, **clarify 26268681281**, and **respond 26268681308**; the first three have deep-dive evidence of false gate evaluation. **Estimated impact:** big reduction in queue/control-plane noise; modest direct runtime savings. **Confidence:** high.
- **Current hard failures are real regressions, not infra noise.** `CI` run **26211942062** failed in `lint / Orchestrate poll process unit tests` with **209 passed, 13 failed, 222 total**; `CI` run **26242204999** failed in `lint / Python lint (ruff)` on two `F841` unused variables; `plan` run **26268304639** failed in `Check and claim /answer command` after an AI-memory branch push race. **Estimated impact:** removing all 3 known hard failures in this window. **Confidence:** high.
- **AI memory is low-recall and occasionally high-risk.** Deep-dive telemetry shows **9 retrieves**, only **1 hit (11.1%)**, **8 zero-hit retrieves**, and **3.11 average estimated tokens** used versus configured **1,400–1,600 token budgets**. Yet `plan` run **26268304639** still failed after **5** memory-branch push attempts (`cannot lock ref 'refs/heads/ai-memory'`). **Estimated impact:** medium reliability gain if contention is fixed before memory usage is expanded. **Confidence:** high.
- **GitHub API pressure is concentrated in one hotspot.** `.github/workflows/review_autofix.yml:1888-2013` polls `check-runs` in a loop; sampled runs show **>=48** logical snapshots in **26246026439** and **26242205286**, **>=44** in **26215796301**, and **>=26** in **26211944838**. **Estimated impact:** large API reduction and several minutes saved per slow review run. **Confidence:** high.
- **Semble looks net-useful; Serena is absent.** I found **39 `SEMBLE_QUERY`** events totaling **374,886 bytes** and **5 `SEMBLE_FALLBACK`** events, all from CI contract-test run **26242204999**. I found **no operational `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE`** lines. **Estimated impact:** keep Semble, but cap overflow lookups on huge PRs; Serena is not yet an optimization lever in this sample. **Confidence:** medium-high.

## Speed Optimizations

### 1) Shrink `review_autofix` check-run waiting — **critical-path win**

- **Evidence:** `.github/workflows/review_autofix.yml:1888-2013` polls `repos/{repo}/commits/{sha}/check-runs?per_page=100` every **20s** with a **1200s** wait budget. Sampled `review_autofix / codex-agent` logs show:
  - run **26246026439**: **47** wait lines = **940s** minimum sleep, implying **>=48** snapshots.
  - run **26242205286**: **47** wait lines = **940s** minimum sleep, implying **>=48** snapshots.
  - run **26215796301**: **43** wait lines = **860s** minimum sleep, implying **>=44** snapshots.
  - run **26211944838**: **25** wait lines = **500s** minimum sleep, implying **>=26** snapshots.
  - run **26226389847**: **9** wait lines = **180s** minimum sleep, implying **>=10** snapshots.
- **Root cause:** the review path blocks on sibling check-runs before building autofix context.
- **Exact change:** reduce default `CHECK_RUNS_WAIT_TIMEOUT_SECS` from **1200** to **300** for normal review runs, and back off polling after the first few unchanged snapshots (for example, 20s → 60s). Keep the existing fail-open `timeout` snapshot behavior.
- **Estimated time savings:** **3–11 minutes per slow `review_autofix` run** in this sample.
- **Implementation risk:** **low-medium**; reviewers may sometimes receive a slightly staler CI snapshot, but the step already fail-opens by design.

### 2) Restore a real small-diff pass-2 gate in `review_autofix` — **critical-path win**

- **Evidence:** `.github/workflows/review_autofix.yml:109-118` says the pass-2 LOC gate is effectively a no-op because both small and large diff branches default to `xhigh`. In slow run **26226389847** (`review / codex-agent`), the log explicitly says: **“diff is 66 LOC < 200 threshold”** yet still ran **PASS 2**. That run spent:
  - **467s** in pass 2 (`13:34:06` → `13:41:53`)
  - **141s** in the final review summariser (`13:41:53` → `13:44:14`)
  - total **608s** after pass-2 start.
- **Root cause:** low-risk diffs are still paying full second-pass review cost.
- **Exact change:** set `REVIEWER_PASS2_REASONING_SMALL` to `medium` or `low`, or disable pass 2 entirely unless the PR touches high-risk paths (`.github/workflows/`, `scripts/orchestrate_*`, `scripts/review_*`, memory helpers) or CI is already red.
- **Estimated time savings:** about **10 minutes** on small-diff review runs like **26226389847**.
- **Implementation risk:** **medium**; reduce depth only on low-risk diffs and keep the current policy for orchestrator/workflow changes.

### 3) Move `ruff` near the top of CI — **fast-fail win**

- **Evidence:** `.github/workflows/ci.yml:501-505` runs `Python lint (ruff)` after a long serial test list. Failure run **26242204999** took **1,099s** before failing on:
  - `scripts/verify_integration_fingerprints.py:633`
  - `scripts/verify_integration_fingerprints.py:719`
- **Root cause:** a cheap static check sits late in a long single-job pipeline.
- **Exact change:** move `Python lint (ruff)` to immediately after Python setup / syntax check.
- **Estimated time savings:** up to **~18 minutes** on future lint-failing runs.
- **Implementation risk:** **low**.

### 4) Replace the four comment wrappers with one router workflow — **operational/queue win**

- **Evidence:** the thin wrappers all trigger on `issue_comment.created`:
  - `.github/workflows/internal-plan.yml:3-16`
  - `.github/workflows/internal-implement.yml:3-17`
  - `.github/workflows/internal-clarify.yml:3-15`
  - `.github/workflows/internal-orchestrate-clarify-respond.yml:3-13`
  Recent evidence:
  - **2026-05-22 04:37:04 UTC:** plan **26268681311**, implement **26268681263**, clarify **26268681281**, respond **26268681308** created together.
  - Deep-dive logs prove false gates for plan / implement / clarify on that burst.
  - Across the sample, these families produced **697 skipped runs**.
- **Root cause:** one comment event fans out into four independent workflow runs.
- **Exact change:** replace the four wrappers with a single `issue_comment` router workflow containing four conditional reusable jobs.
- **Estimated time savings:** small direct runtime savings (**975s** total skipped duration across all four families), but a **large reduction in queue churn, telemetry noise, and control-plane overhead**.
- **Implementation risk:** **low**.

## Cost Optimizations

> Direct token/dollar totals were **not emitted** in sampled deep-dive logs, so the estimates below use reviewer breadth, prompt-byte proxies, Semble byte counts, and runtime.

### 1) Cut low-value second-pass review spend on small diffs

- **Evidence:** `review_autofix` defines **6 reviewer models** in `.github/workflows/review_autofix.yml:96-102`, uses `ENABLE_REVIEWER_TWO_PASS=true` at line **140**, and uses `openai/gpt-5.4-mini` as the summariser at lines **146-149**. In run **26226389847**:
  - **6 reviewers successful**
  - pass-1 summariser prompt: **15,766 bytes**
  - pass-2 summariser prompt: **25,296 bytes**
  - pass-2 consensus output: **15,127 bytes**
  - total post-pass2 work: **608s**
- **Root cause:** small diffs still invoke a full reviewer panel plus a full second-pass summarisation cycle.
- **Exact change:** keep the current panel only for high-risk diffs; otherwise reduce pass-2 reasoning or skip pass 2. If further cost trimming is needed, shrink the low-risk reviewer list before changing the editor model.
- **Estimated savings:** largest model-cost opportunity in the pipeline; removes **one summariser call plus six pass-2 reviewer calls** on low-risk runs.
- **Quality-risk notes:** **medium**; apply only to low-risk diffs and retain the full panel for workflow/orchestrator/memory changes.

### 2) Keep Semble, but cap overflow lookups on large review runs

- **Evidence:** operational telemetry found **39 `SEMBLE_QUERY`** events and **5 `SEMBLE_FALLBACK`** events:
  - total query bytes: **374,886**
  - `overflow`: **31** queries / **252,230** bytes / **5** fallbacks
  - `reviewer-context`: **7** queries / **111,205** bytes
  - `conflict-resolver-context`: **1** query / **11,451** bytes
  - heaviest run: **26209179205** with **17** queries / **157,020** bytes
- **Root cause:** large PRs trigger many per-file overflow fetches.
- **Exact change:** dedupe repeated overflow files across reviewer/editor/resolver phases, cap overflow fetches per iteration, and prefer one `reviewer-context` bundle before falling back to many per-file lookups.
- **Estimated savings:** tens of KB on typical large reviews and **>150KB** on worst sampled runs like **26209179205**.
- **Quality-risk notes:** **low** if dedupe happens before any hard cap.
- **Semble verdict:** **inference:** Semble is probably reducing raw prompt expansion overall because the sampled context arrives as bounded chunk bundles (`chunks=6` / `12`) instead of full-file inlining. The cost problem is **overflow volume**, not Semble itself.

### 3) Remove avoidable no-op wrapper runs

- **Evidence:** `clarify` **184/197** skipped, `plan` **167/185**, `implement` **167/180**, `orchestrate_clarify_respond` **179/181**; combined **697 skipped runs**.
- **Root cause:** four separate wrappers react to every comment.
- **Exact change:** use one router workflow.
- **Estimated savings:** low direct model savings (skipped runs do not appear to call models), but lower GitHub Actions overhead and less queue churn.
- **Quality-risk notes:** **low**.

### 4) Reduce canceled `review_autofix` spend with a stale-run preflight

- **Evidence:** `review_autofix` had **18 canceled runs** consuming **28,945s (~8.0h)**; max canceled duration was **8,151s** in run **26209737833**.
- **Root cause:** not fully visible in sampled logs. **Inference:** at least some canceled work is stale/superseded review effort.
- **Exact change:** add an early stale-run check before `codex-agent` starts expensive work, and emit cancel reason / superseded-by data to logs.
- **Estimated savings:** potentially multiple runner-hours per day if even a fraction of canceled work is superseded.
- **Quality-risk notes:** **low**; the recommendation is to skip only when a newer same-PR run is already known to exist.

### 5) Fix observability before tuning prompt-cache dollars

- **Evidence:** sampled runs show `OPENROUTER_PROMPT_CACHE_DISABLED=false` (for example, plan **26268304639** and review **26226389847**), but I found **no** emitted `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.
- **Root cause:** prompt-cache and token telemetry are not reaching the log-analysis surface.
- **Exact change:** emit per-call token and cache counters into workflow-log telemetry before doing further cache tuning.
- **Estimated savings:** indirect; this unlocks precise tuning instead of guesswork.
- **Quality-risk notes:** **none**.

**Serena note:** I found **no operational `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE`** lines, so Serena is neither saving cost nor adding response-byte noise in this sample. Verify whether that is intentional before optimizing it.

## Reliability Improvements

### 1) Repair the `orchestrate_poll_process` regression cluster

- **Failure evidence:** CI run **26211942062** failed in `lint / Orchestrate poll process unit tests` with **209 passed, 13 failed, 222 total**. Failing tests included:
  - `test_external_finalize_detect_marks_project_complete_when_final_pr_already_merged`
  - `test_external_finalize_detect_preempts_sync_branch_missing_failure`
  - `test_validation_completion_preempts_sync_branch_missing_failure_when_final_pr_already_merged`
  - `test_validation_fixing_completion_preempts_sync_branch_missing_failure_when_final_pr_already_merged`
  - `test_verify_integration_fingerprints_partial_removal_regressions`
  - plus 8 additional failures in the same path.
- **Root cause category:** orchestrator logic regression / forward-merge conflict resolution regression.
- **Exact fix:** restore the removed/failing behaviors in `scripts/orchestrate_poll_process.sh` and related fingerprint logic until this test group is fully green again.
- **Expected reliability impact:** removes the most serious current failure cluster and protects core orchestrator correctness.
- **Rollback / fail-open:** prefer minimal revert or targeted restoration; if a full fix is risky, temporarily gate the new behavior behind an existing flag rather than shipping a red CI path.

### 2) Fix the trivial `ruff` regression immediately

- **Failure evidence:** CI run **26242204999** failed in `lint / Python lint (ruff)` on:
  - `scripts/verify_integration_fingerprints.py:633`
  - `scripts/verify_integration_fingerprints.py:719`
- **Root cause category:** code hygiene / lint regression.
- **Exact fix:** remove or use the unused `exc` variables.
- **Expected reliability impact:** eliminates one of the three hard failures in this sample window.
- **Rollback / fail-open:** none needed.

### 3) Harden AI-memory command claiming against branch-push races

- **Failure evidence:** plan run **26268304639** failed in `plan / plan / Check and claim /answer command` with:
  - `AI_MEMORY_TELEMETRY: {"enabled": true, "exists": false, "ok": true, "op": "processed-command-check"}`
  - `AI_MEMORY_ERROR: Failed to push memory branch after 5 attempts`
  - remote reject: `cannot lock ref 'refs/heads/ai-memory'`
- **Root cause category:** shared-state concurrency / optimistic-lock conflict.
- **Exact fix:** add jittered backoff between push retries in `scripts/ai_memory_lib.py:1860-1892`, and after the last conflict do one final re-read of the claimed entry before failing. Keep command-claim semantics strict; do not fail-open duplicate prevention itself.
- **Expected reliability impact:** removes a real `/answer` hard-failure mode without weakening dedup semantics.
- **Rollback / fail-open:** fail-open only for append-only memory events; keep command claims strict.

### 4) Treat observed Semble fallbacks as healthy test coverage, not rollout failure

- **Failure evidence:** I found **5 `SEMBLE_FALLBACK`** events, all in CI run **26242204999**, all `target=overflow`, and all pointing at test tmp paths like `/tmp/.../missing_semble`. I found **no `SERENA_FALLBACK`** and **no `SERENA_PROBE`** events.
- **Root cause category:** test telemetry pollution, not production instability.
- **Exact fix:** tag contract-test fallback telemetry separately from production fallback telemetry so alerts and dashboards do not treat it as a broken rollout.
- **Expected reliability impact:** lower false-positive ops noise; no evidence of a masked Semble/Serena production outage in this sample.
- **Rollback / fail-open:** none.

## AI Memory Health

- **Telemetry present:** yes — **35 deduped `AI_MEMORY_TELEMETRY` events** across deep-dive logs.
- **Op mix:** `record-run-event=19`, `retrieve=9`, `processed-command-check=2`, `record-candidate=2`, `processed-command-claim=1`, `processed-command-complete=1`, `finalize-task=1`. I saw **no** sampled `promote` or `compact` telemetry.
- **Retrieve hit rate:** **1/9 = 11.1%**.
  - **Reviewer role:** **8** retrieves, **0 hits**, **0.0 average estimated tokens** against the configured **1,400-token** reviewer budget in `ai-memory/config/retrieval_profiles.v1.json`.
  - **Implementation role:** **1** retrieve, **1 hit**, **28 estimated tokens** against the configured **1,600-token** implementation budget.
- **Keyword extraction method:** `none=8`, `plain=1`, `llm=0`.
- **Flags:** `enabled:false=0`, `fail_open:true=0`, telemetry events with `push_attempts > 1 = 0`.
- **Problem:** retrieval is not budget-constrained; it is **recall-constrained**. Reviewer retrieval is effectively cold in the sampled deep dives.
- **Important gap:** telemetry did **not** surface the real branch-contention severity in run **26268304639`; the hard failure showed **5 push attempts** in stderr, but no telemetry event recorded `push_attempts > 1`.
- **Recommendation:** add `token_budget`, `push_attempts`, and contention result fields to claim/retrieve telemetry, and track a daily reviewer retrieve hit-rate KPI. Right now the memory system is cheap in prompt terms but weak in recall and occasionally risky in write contention.

## GH API Call Audit

### 1) Critical hotspot: `review_autofix` check-run polling

- **Evidence:** `.github/workflows/review_autofix.yml:1888-2013` runs `gh_retry gh api --paginate --slurp "repos/${repo}/commits/${HEAD_SHA}/check-runs?per_page=100"` inside `while :; do`.
- **Sampled volume:** at least **176 logical snapshots** across just five sampled slow runs (**48 + 48 + 44 + 26 + 10**).
- **Why this matters:** `README.md:65-67` explicitly notes each snapshot costs **>=1** underlying GitHub API requests; `CLAUDE.md:435-439` says per-iteration `gh api` inside loops is a review-blocker unless cached/prefetched.
- **Concrete fix:** shorten the wait budget, back off polling after unchanged snapshots, and reuse the last snapshot instead of polling at a fixed 20s cadence for up to 20 minutes.
- **Estimated reduction:** **60–90% fewer check-run API snapshots** on the slow sampled review runs.
- **Rate-limit risk reduction:** high.

### 2) Moderate redundancy: `plan` still needs two issue-scope API calls on active runs

- **Evidence:** `plan.yml:355-361` fetches issue metadata, then `plan.yml:450-469` separately hits the issue timeline to detect linked PRs.
- **What is already good:** `plan.yml:415-428` reuses `ISSUE_META_FILE` for label/state checks instead of refetching the issue.
- **Concrete fix:** keep the current reuse, but if active plan volume grows, fold linked-PR detection into a batched GraphQL helper or defer the timeline call until after all cheaper skip gates.
- **Estimated reduction:** small in this sample because only **18** plan runs were active.

### 3) Low-volume but wasteful-on-no-op: `orchestrate_clarify_respond` metadata lookups

- **Evidence:** `orchestrate_clarify_respond.yml:62-88` calls `gh api issues/${ISSUE_NUMBER}` and sometimes `issues/${TRACKING_NUM}` before discovering it should skip. `log_summary` for run **26268561854** says: `respond / Check orchestrator metadata` → **“Issue #2874 is not orchestrator-managed. Skipping.”**
- **Concrete fix:** if `github.event.issue.body` is reliably present, move the orchestrator-marker check to a job `if` or pre-run shell check to avoid the API call and runner start.
- **Estimated reduction:** low.

### 4) `cancel_on_pr_close` is not a current API problem

- **Evidence:** recent run **26268655372** used the cancel path but found **“No matching queued/in-progress…”** and showed no 429/secondary-rate-limit behavior.
- **Recommendation:** leave this path alone for now.

## Prompt Cache & Memory System

- **Prompt cache state:** configured on, but unmeasured. `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in sampled plan/review/implement logs, but no cache hit/miss/create/read counters reached the log-analysis layer.
- **Token telemetry state:** absent. I found **zero** emitted `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.
- **Memory retrieval state:** weak recall, not budget pressure. Reviewer retrieves used **0 / 1,400** estimated tokens on average and hit **0/8** times; implementation used **28 / 1,600** once and hit.
- **Cache-fragmentation risk (inference):** prompt variance is likely high in `review_autofix` because:
  - pass-1 summariser prompt in run **26226389847** was **15,766 bytes**
  - pass-2/review summariser prompt was **25,296 bytes**
  - Semble context in run **26209179205** reached **157,020 bytes**
  This kind of per-run dynamic context likely hurts cache-prefix reuse.
- **Concrete improvements:**
  1. emit cache hit/miss/create/read + token counters before further tuning;
  2. keep static policy/instructions stable and place dynamic Semble/context blocks at the end;
  3. dedupe repeated overflow files across reviewer/editor/resolver phases;
  4. add `token_budget` to AI-memory retrieve telemetry so “estimated_tokens vs budget” is visible without repo inspection.
- **Estimated impact:** currently **unquantified** due missing cache metrics; likely **medium** on cost/latency once instrumented.
- **Reliability impact:** high on diagnosability; medium on runtime once measured.

## Orchestrator Health

- **Clarify/plan/implement/respond gating is mostly healthy but operationally noisy.** The system is skipping far more than it is executing:
  - `clarify`: **184 skipped / 197 total**
  - `plan`: **167 skipped / 185 total**
  - `implement`: **167 skipped / 180 total**
  - `orchestrate_clarify_respond`: **179 skipped / 181 total**
- **No evidence of a runaway clarify loop** in the sampled deep dives; the issue is fan-out/no-op dispatch, not repeated failing clarification cycles.
- **`orchestrate_poll` itself looks healthy in this window:** **26/26 success**, **avg 174.2s**, **p95 458.8s**. The bigger orchestrator risk is that current CI regressions sit directly in this code path (`26211942062`).
- **AI-memory command claiming is a live orchestrator reliability issue:** `plan` run **26268304639** hard-failed before planning due to branch push contention.
- **Forward-merge health is degraded:** recent run **26268655190** opened fallback conflict **PR #2879** with **17 conflicting files**, meaning stable fixes do not reach `main` automatically until manual merge.
- **Evidence gap:** only **1** `orchestrate` family run is in the sample (**26267728598**, **1261s**), and there is no deep-dive folder for it; keep conclusions about full orchestrator wave progression bounded.
- **Track these indicators going forward:**
  - skipped-wrapper ratio
  - `review_autofix` canceled seconds
  - check-run wait seconds per review run
  - AI-memory claim failures / push-attempt tails
  - forward-merge conflict PR count
  - CI failures in `test_orchestrate_poll_process.py`

## Pipeline Flow Bottlenecks

| Phase | Bottleneck type | Evidence | End-to-end effect | Ordered fix |
|---|---:|---|---|---|
| Comment dispatch (`clarify` / `plan` / `implement` / `respond`) | Queue/control-plane | **697 skipped runs**; same comment created 4 runs at **04:37:04 UTC** | Noisy telemetry, queue churn, confusing phase visibility | **1.** Single router workflow |
| `plan` | Reliability gate | Active `plan` runs average **293.5s**; run **26268304639** failed before planning on AI-memory claim | Can block the whole issue before implementation starts | **2.** Harden AI-memory claim path |
| `implement` | Compute | Active `implement` runs average **797.3s**, with outlier **4622s** in run **26218075393** | Moderate contributor to long issue lifecycles | **3.** Keep current closed-issue skip, but fix upstream plan noise first |
| `review_autofix` | Dominant compute | **69.9%** of sampled runtime; active `p95` **4097.4s** | Main source of long PR turnaround | **4.** Shorten check-run wait + re-enable small-diff pass-2 gate |
| `review_autofix` | Retry/poll overhead | **500–940s** minimum wait just in check-run polling on sampled runs | Burns time before reviewers/editor even start | **5.** Lower wait budget and back off polling |
| CI | Serial compute / mean-time-to-fail | `ci` avg **985.1s**; `ruff` failure **26242204999** arrived after **1099s** | Slow feedback on regressions | **6.** Move ruff earlier; fix failing test cluster |
| Merge/finalize | Conflict overhead | forward-merge run **26268655190** opened fallback **PR #2879** with **17** conflicting files | Stable fixes stall before reaching `main` | **7.** Reduce `stable`/`main` drift on high-churn workflow/script files |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` dominates runtime: **100 runs**, **132,292s**, **69.9%** of sampled runtime.
  - CI is long and serial: **28 runs**, **avg 985.1s**, **2 failures**.
  - Comment wrappers create heavy no-op fan-out: **697 skipped runs** across four workflow families.

- **Top failure modes**
  - `CI` run **26211942062**: `lint / Orchestrate poll process unit tests` regression cluster.
  - `CI` run **26242204999**: `lint / Python lint (ruff)` failed on two `F841` unused vars.
  - `plan` run **26268304639**: AI-memory branch lock during `/answer` claim.
  - `forward_merge_stable_to_main` run **26268655190**: fallback conflict PR **#2879** opened.

- **Highest-cost drivers**
  - `review_autofix` two-pass, six-reviewer setup and large summariser prompts.
  - `review_autofix` PR check-run polling loop.
  - Large-review Semble overflow context (for example, run **26209179205** at **157,020 bytes**).
  - Canceled `review_autofix` work: **28,945s** across **18** runs.

- **Top 3 prioritized actions**
  1. **Shorten `review_autofix` check-run waiting and restore a real small-diff pass-2 gate.**
  2. **Fix the current CI regressions (`orchestrate_poll_process` tests + `ruff`) and harden AI-memory claim retries.**
  3. **Replace the four comment wrappers with one router workflow and add missing token/cache/cancel telemetry.**

## Metrics Appendix

### Overall repository summary

| Repo | Total runs | Success | Failure | Cancelled | Skipped | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 275 | 3 | 22 | 700 | 189.4 | 2.0 | 1383.0 |

*Note: collector `other_count=700`; run-level conclusions show these are effectively skipped runs.*

### Major workflow-family summary

| Family | Total | Success | Failure | Cancelled | Skipped | Avg s | p50 s | p95 s | Runtime share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 100 | 80 | 0 | 18 | 2 | 1322.9 | 1278.5 | 4061.1 | 69.9% |
| ci | 28 | 26 | 2 | 0 | 0 | 985.1 | 988.0 | 1115.2 | 14.6% |
| implement | 180 | 11 | 0 | 2 | 167 | 59.0 | 1.0 | 258.9 | 5.6% |
| plan | 185 | 17 | 1 | 0 | 167 | 29.8 | 1.0 | 202.0 | 2.9% |
| orchestrate_poll | 26 | 26 | 0 | 0 | 0 | 174.2 | 136.0 | 458.8 | 2.4% |
| clarify | 197 | 13 | 0 | 0 | 184 | 8.1 | 1.0 | 85.0 | 0.8% |
| orchestrate | 1 | 1 | 0 | 0 | 0 | 1261.0 | 1261.0 | 1261.0 | 0.7% |
| orchestrate_clarify_respond | 181 | 2 | 0 | 0 | 179 | 1.4 | 1.0 | 2.0 | 0.1% |
| forward_merge_stable_to_main | 2 | 2 | 0 | 0 | 0 | 27.0 | 27.0 | 30.6 | 0.0% |

### Active-only latency (skips removed)

| Family | Active runs | Avg s | p50 s | p95 s | Max s |
|---|---:|---:|---:|---:|---:|
| review_autofix | 98 | 1340.1 | 1345.5 | 4097.4 | 8151 |
| ci | 28 | 985.1 | 988.0 | 1115.2 | 1122 |
| implement | 13 | 797.3 | 289.0 | 2958.8 | 4622 |
| plan | 18 | 293.5 | 244.0 | 812.9 | 875 |
| orchestrate_poll | 26 | 174.2 | 136.0 | 458.8 | 633 |
| clarify | 13 | 103.0 | 89.0 | 159.8 | 245 |

### GH API hotspot samples

| Workflow / step | Endpoint / pattern | Sample evidence | Est. logical calls in sample |
|---|---|---|---:|
| `review_autofix / codex-agent / Collect PR check-run failures` | `commits/{sha}/check-runs?per_page=100` in a loop | run **26246026439**: **47** waits / **940s** min sleep / **>=48** snapshots; run **26242205286**: same; run **26215796301**: **>=44**; run **26211944838**: **>=26**; run **26226389847**: **>=10** | **>=176** across 5 sampled runs |
| `plan / Skip when issue already has a PR` | `issues/{ISSUE_NUMBER}/timeline` | active plan path only; metadata is already reused from earlier issue fetch | **<=18** active-plan calls in sample window |
| `orchestrate_clarify_respond / Check orchestrator metadata` | `issues/{ISSUE_NUMBER}` and optional tracking issue lookup | `log_summary` for **26268561854**: non-orchestrator issue discovered after runner start | low |
| `cancel_on_pr_close / Cancel queued in-progress runs` | `actions/runs/{run_id}/cancel` | recent run **26268655372** found no matching runs, no rate-limit/backoff evidence | low |

### AI memory telemetry: op counts

| Operation | Count |
|---|---:|
| record-run-event | 19 |
| retrieve | 9 |
| processed-command-check | 2 |
| record-candidate | 2 |
| processed-command-claim | 1 |
| processed-command-complete | 1 |
| finalize-task | 1 |

### AI memory retrieve health

| Metric | Value |
|---|---|
| Retrieve events | 9 |
| Hit rate | 1 / 9 = **11.1%** |
| Zero-hit retrieves | 8 |
| Avg estimated tokens | **3.11** |
| Reviewer retrieves | 8 events, **0 hits**, **0.0 / 1400** avg tokens vs budget |
| Implementation retrieves | 1 event, **1 hit**, **28 / 1600** tokens vs budget |
| Keyword method distribution | `none=8`, `plain=1`, `llm=0` |
| `enabled:false` events | 0 |
| `fail_open:true` events | 0 |
| Telemetry events with `push_attempts > 1` | 0 |
| Non-telemetry contention failure | run **26268304639** hit **5** push attempts before failure |

### MCP / Semble / Serena summary

| Server / target | Query count | Query bytes | Fallback count | Fallback rate | Probe count | Notes |
|---|---:|---:|---:|---:|---:|---|
| Semble / all | 39 | 374,886 | 5 | 11.4% of observed Semble events | 0 | No `SEMBLE_PROBE` lines observed |
| Semble / overflow | 31 | 252,230 | 5 | 13.9% of overflow events | 0 | All fallbacks came from CI run **26242204999** |
| Semble / reviewer-context | 7 | 111,205 | 0 | 0.0% | 0 | Net-positive context bundling signal |
| Semble / conflict-resolver-context | 1 | 11,451 | 0 | 0.0% | 0 | Single sampled use |
| Serena / all | 0 | n/a | 0 | n/a | 0 | No operational `SERENA_*` telemetry observed |

**Other MCP servers observed:** none.

### MCP availability rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Note |
|---|---|---:|---:|---:|---|
| Semble | overflow | 0 | 0 | 0 | No probe telemetry emitted |
| Semble | reviewer-context | 0 | 0 | 0 | No probe telemetry emitted |
| Semble | conflict-resolver-context | 0 | 0 | 0 | No probe telemetry emitted |
| Serena | all | 0 | 0 | 0 | No `SERENA_PROBE` telemetry emitted |

### Prompt-cache / token telemetry availability

| Metric | Observed in sampled deep-dive logs? |
|---|---|
| `prompt_tokens` | No |
| `completion_tokens` | No |
| `total_tokens` | No |
| `cache_creation_input_tokens` | No |
| `cache_read_input_tokens` | No |
| Prompt-cache hit / miss counters | No |
| `OPENROUTER_PROMPT_CACHE_DISABLED` config flag | Yes (`false` in sampled plan/review/implement runs) |

