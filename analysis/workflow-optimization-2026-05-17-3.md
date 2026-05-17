## Executive Summary

- `CI` was the clearest reliability problem in this window: 29 of 39 `ci` runs failed (74.4%), all at `lint` → `Validation self-test unit tests`, and sampled failed logs all ended with `AssertionError: implement.yml missing resolved-ref log output` (for example runs `25996044702`, `25996003957`, `25995388895`, `25995213924`, `25994699778`). That burst alone consumed 18,592s (5.16h) of runner time on May 17, 2026. Current `HEAD` now contains the expected `implement.yml` strings, so this looks like a repeated historical regression that likely needs guardrails more than a fresh fix. **Estimated impact:** remove most CI-family failures and recover ~5.2 runner-hours in a similar window. **Confidence:** high.

- `review_autofix` is the dominant end-to-end latency center. Family metrics were 120 runs, 88 success, 27 cancelled, p50 `677.5s`, p95 `2699.1s`, avg `1028.9s`. In slow runs `25990356358` and `25991911686`, runner queueing alone consumed `2044.9s` (42.0%) and `1334.3s` (46.8%) before compute started; run `25983337193` then spent `3026.4s` inside the main review step. **Estimated impact:** 20–35 minutes off p95 review latency if stale runs, queue backlog, and long review passes are reduced. **Confidence:** high.

- A single recent `review_autofix` success run (`25996049912`) shows three concrete step bottlenecks that are easy to trim: `Collect PR check-run failures...` = `120.8s`, `Free disk space` = `44.7s`, and `Review-blocked judge decision` = `255.2s`; together they were `420.7s` of a `552s` run (76.2%). **Estimated impact:** 2–7 minutes saved per affected review run. **Confidence:** high.

- Cancelled and likely superseded `review_autofix` runs are materially wasteful. The family burned `29,209s` (8.11h) in 27 cancelled runs; 11 exact cancelled→success pairs landed within 20s of each other and wasted `23,491s` (6.53h). Because `.github/workflows/review_autofix.yml` already contains a preflight dedup block and PR-backed `cancel-in-progress=false`, the remaining waste is best described as a residual duplicate-dispatch gap, not obviously intended behavior. **Estimated impact:** recover 6–8 runner-hours per similar day. **Confidence:** medium (pairing is exact; supersession cause is inference).

- Semble looks useful and cheap in the sampled real `review_autofix` runs; Serena is effectively off. Across 8 sampled actual review runs, Semble emitted 22 deduped `SEMBLE_QUERY` events totaling `197,288` bytes and only `10.6s` of logged lookup latency, with 0 fallbacks. Serena emitted 0 `SERENA_QUERY`, 0 `SERENA_FALLBACK`, and 0 `SERENA_PROBE` lines, while sampled logs showed `SERENA_ENABLED=false` and `SERENA_AVAILABLE=false`. **Estimated impact:** keep Semble, tighten overflow queries, and defer Serena optimization work. **Confidence:** high.

- AI memory is healthy on writes but not helping retrieval. In 8 sampled actual `review_autofix` runs, memory retrieval hit `0/8` times, average `estimated_tokens` was `0`, and `keyword_method` was `none` in 100% of retrieves; all 23 push-bearing memory writes succeeded on the first push attempt. **Estimated impact:** modest direct speed savings, but high leverage for future cost tuning once retrieval works. **Confidence:** high.

- `workflow_log_analysis` is a standalone cost outlier. Run `25981605517` took `3842s` and logged `1,684,201` total tokens across three main steps: `172,181`, `554,911`, and `957,109`. The same run’s widening summarizer telemetry showed `summarized=77`, `targeted=100`, `tokens_used=164,297` on `openai/gpt-5.4-mini`. **Estimated impact:** save ~300k–700k tokens and 15–25 minutes per analysis run with model/reasoning downshift and tighter summarizer scope. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Add a stale-run self-abort at the start of `review_autofix`**
   - **Evidence:** Slow runs `25990356358` and `25991911686` spent `2044.9s` and `1334.3s` waiting for `ubuntu-latest` before compute. Separately, 11 cancelled `review_autofix` runs were followed by a successful peer within 20s.
   - **Root cause:** residual duplicate-dispatch / successor-handoff overlap keeps obsolete runs queued long enough to steal runner slots.
   - **Exact change:** keep the existing preflight dedup in `.github/workflows/review_autofix.yml`, but add a second guard at heavy-job start: if a newer same-PR/same-branch `review_autofix` run already exists, exit neutral before Semble install, diff/context assembly, and model work.
   - **Estimated time savings:** up to 22–34 minutes on queued outliers; up to 6.53h/day in the observed cancelled→successor pairs.
   - **Implementation risk:** low-medium. Make the new guard fail open if the Actions API lookup fails.

2. **Make reviewer pass 2 conditional, and downshift small-diff pass-2 reasoning**
   - **Evidence:** In run `25983337193`, the workflow logged `ENABLE_REVIEWER_TWO_PASS: true`, then `=== PASS 2: Deep review (xhigh reasoning — diff is 194 LOC < 200 threshold ...) ===`. The same run also logged `Reviewer x-ai/grok-4.1-fast failed after 3 attempts.` twice, and `Reviewer deepseek/deepseek-v4-pro produced empty output on attempt 1.` before succeeding on attempt 2.
   - **Root cause:** unconditional second-pass review plus repeated provider retries on a small diff.
   - **Exact change:** only run pass 2 when pass 1 finds blocking issues, cross-model disagreement, or a diff above the existing threshold; reduce `REVIEWER_PASS2_REASONING_SMALL` from `xhigh` to `high` for sub-200-LOC diffs; do not retry a provider in pass 2 if it already exhausted retries in pass 1.
   - **Estimated time savings:** 5–15 minutes on heavy review runs.
   - **Implementation risk:** medium. Preserve an override for high-risk labels / large diffs.

3. **Cut the advisory check-run wait budget**
   - **Evidence:** In run `25996049912`, step `Collect PR check-run failures CI lint autofix context` took `120.8s`; it logged four waits (`16:16:59`, `16:17:30`, `16:18:00`, `16:18:31`) and then `CHECK_RUNS_WAIT_TIMEOUT reached after 120s with 1 check-run(s) still queued/in_progress`. The final context was only `511` bytes.
   - **Root cause:** blocking on queued CI state to gather advisory context.
   - **Exact change:** lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` from `120` to `30` or `45`, keep `CHECK_RUNS_POLL_INTERVAL_SECS` short, and snapshot immediately once at least one usable check-run view exists. Let the next autofix iteration refresh the context if CI is still moving.
   - **Estimated time savings:** 75–120s per affected run.
   - **Implementation risk:** low-medium. Existing behavior already fails open after timeout, so this is a bounded change.

4. **Invoke the review-blocked judge later and with a smaller dossier**
   - **Evidence:** In run `25996049912`, `Review-blocked judge decision` took `255.2s` (46.2% of the run). Its Semble lookup was only `561ms` and `5,771` bytes, so almost all of the time was in the downstream judge/model work.
   - **Root cause:** an expensive LLM decision path is still reached after deterministic gating has already done most of the cheap filtering.
   - **Exact change:** call the judge only after deterministic mergeability / label / check-state rules fail to resolve the outcome; pass a narrow decision packet instead of the full accumulated review context.
   - **Estimated time savings:** 2–4 minutes on blocked-review runs.
   - **Implementation risk:** medium. Roll out behind a flag and compare decision parity.

### Micro-optimizations

5. **Guard `Free disk space` behind a headroom check**
   - **Evidence:** In run `25996049912`, `Free disk space` took `44.7s` (8.1% of total runtime).
   - **Root cause:** unconditional cleanup even when the runner may already have enough space.
   - **Exact change:** add a fast `df`/workspace-size precheck; only run the cleanup when free space drops below a fixed threshold.
   - **Estimated time savings:** ~45s on eligible runs.
   - **Implementation risk:** low.

### Non-critical admin flow

6. **Downshift `workflow_log_analysis` runtime**
   - **Evidence:** Run `25981605517` took `3842s`; step runtimes were `688.4s`, `1307.8s`, and `1748.2s`.
   - **Root cause:** a low-frequency but very heavy analysis workflow is doing three long full-model passes.
   - **Exact change:** keep the collector and fail-open summarizer, but gate the most expensive analysis/report-polish path on meaningful report deltas or failure spikes; otherwise reduce `THINKING_LEVEL_ANALYSIS` first.
   - **Estimated time savings:** 15–25 minutes per analysis run.
   - **Implementation risk:** medium.

## Cost Optimizations

> Cost estimates on `review_autofix` are partly proxy-based because sampled operational review logs did **not** emit raw `prompt_tokens` / `completion_tokens` / `total_tokens` / prompt-cache counters.

1. **Reduce aggregate `review_autofix` model spend by collapsing low-value second passes and retries**
   - **Evidence:** Run `25983337193` ran a second `xhigh` review pass on a `194 LOC` diff and retried `x-ai/grok-4.1-fast` to exhaustion twice.
   - **Root cause:** model-heavy two-pass policy plus repeated provider retries on the same run.
   - **Exact change:** conditional pass 2, smaller pass-2 reasoning on small diffs, and per-run provider health memory so a failed provider is not re-tried in the next pass.
   - **Estimated savings:** likely 15–30% of `review_autofix` model spend on comparable runs.
   - **Quality-risk notes:** medium. Keep pass 2 for large diffs, risky labels, or reviewer disagreement.

2. **Downshift the full-model `workflow_log_analysis` pass**
   - **Evidence:** Run `25981605517` logged `1,684,201` total tokens: `172,181` (`api-redundancy`), `554,911` (`deep-audit`), and `957,109` (`analyze-commit-notify`). The workflow config defaults to `openai/gpt-5.4` with `THINKING_LEVEL_ANALYSIS=xhigh`.
   - **Root cause:** high-reasoning full-model usage on every major analysis step.
   - **Exact change:** keep `gpt-5.4` only where reasoning quality is actually load-bearing; downshift packaging / commit-notify first, then `api-redundancy` if result quality remains acceptable.
   - **Estimated savings:** ~300k–700k tokens per analysis run.
   - **Quality-risk notes:** low-medium if the downshift starts with the report-packaging path rather than the deepest audit.

3. **Tighten the unselected-run summarizer budget**
   - **Evidence:** Current run `25981605517` emitted `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` with `targeted=100`, `summarized=77`, `tokens_used=164297`, model `openai/gpt-5.4-mini`. Workflow defaults are `WORKFLOW_LOG_SUMMARY_MAX_RUNS=100` and `WORKFLOW_LOG_SUMMARY_TOKEN_BUDGET=1500000`.
   - **Root cause:** coverage widening is generous for a one-repo window that already has deep-dive `errors/slow/recent` folders.
   - **Exact change:** summarize only families not already represented in deep-dive coverage, or cut max runs to 30–50.
   - **Estimated savings:** roughly 60k–100k mini tokens per run (inference from 164,297 tokens for 77 summaries).
   - **Quality-risk notes:** low if deep-dive gaps stay covered.

4. **Trim raw context expansion before the model sees it**
   - **Evidence:** In run `25996049912`, `Collect PR metadata` built `76,521` bytes of PR comments context and `39,022` bytes of diff context (`115,543` bytes total before check-run/judge inputs). Across 8 sampled actual review runs, Semble `reviewer-context` queries totaled `89,585` bytes across 7 queries (avg `12,798` bytes each), but `overflow` queries still added `81,926` bytes across 11 calls concentrated in just two runs (`25981932582`, `25991055127`).
   - **Root cause:** large dynamic blobs are still being assembled even when Semble already provides focused context.
   - **Exact change:** trim comments to recent/high-signal threads, summarize unchanged diff regions, and only trigger overflow for changed files or explicit reviewer asks.
   - **Estimated savings:** tens of KB of prompt context per run, plus likely 30–90s less model latency on spill-heavy runs.
   - **Quality-risk notes:** low-medium. Keep an override for pathological PRs.

5. **Eliminate cancelled/superseded `review_autofix` compute**
   - **Evidence:** `review_autofix` burned `29,209s` in cancelled runs, and `23,491s` of that sat in 11 cancelled→successor pairs inside 20s.
   - **Root cause:** obsolete runs still get far enough to consume setup/queue/compute.
   - **Exact change:** same stale-run guard as the speed section, plus stronger continuation/synchronize ownership.
   - **Estimated savings:** meaningful runner and model cost, though exact token savings are not measurable from current review logs.
   - **Quality-risk notes:** low.

**Semble / Serena evaluation**
- **Semble:** net-positive in the sampled real runs. It produced 22 deduped queries, `197,288` total bytes, only `10.6s` total logged lookup time, and **0 fallbacks**. That is a good trade if it is replacing larger prompt expansion. The problem is the `overflow` slice: 11 queries, `81,926` bytes, 41.5% of all Semble bytes.
- **Serena:** neutral because it is inactive. There were **0** `SERENA_QUERY`, `SERENA_FALLBACK`, and `SERENA_PROBE` lines, with `SERENA_ENABLED=false` / `SERENA_AVAILABLE=false` in all 8 sampled actual review runs. It is not currently replacing downstream tool/model work, but it is also not adding noisy response bytes today.

## Reliability Improvements

> Across all 1,000 runs in `workflow_log_report.json`, there were **0** workflow reruns (`retries > 0`). Reliability waste is coming from regression bursts and duplicate dispatches, not GitHub rerun storms.

1. **Prevent checkout-ref contract drift from reaching full CI**
   - **Failure evidence:** All 29 failed `ci` runs between `2026-05-17T03:34:51Z` and `2026-05-17T16:14:36Z` failed at the same step with the same assertion. Sampled failed logs (`25996044702`, `25996003957`, `25995388895`, `25995213924`, `25994699778`) all contain `AssertionError: implement.yml missing resolved-ref log output`.
   - **Root cause category:** workflow/test contract drift, likely across workflow-support or branch-ref boundaries.
   - **Exact fix:** add a fast contract audit that runs before the slow CI phases and validates the exact checkout-ref markers on the workflow file actually used by CI; ideally source those required strings from one shared helper/test fixture so the workflow and test cannot drift independently.
   - **Expected reliability impact:** if this regression recurs, it should remove the majority of `ci` failures in similar windows.
   - **Rollback / fail-open:** additive guard only; keep the existing test as a backstop.

2. **Close the remaining duplicate-dispatch gap in `review_autofix`**
   - **Failure evidence:** 27 cancelled `review_autofix` runs; 11 exact cancelled→success pairs within 20s (`25981096149→25981100308`, `25993094478→25993100190`, `25994699791→25994705386`, etc.).
   - **Root cause category:** duplicate/superseded dispatch overlap. Cause is an inference, but the pairing and wasted time are exact.
   - **Exact fix:** extend the existing preflight dedup with a job-start stale-peer abort and a “newest run owns the branch” check before heavy setup.
   - **Expected reliability impact:** lower the family’s 22.5% cancel rate and reduce partial-work churn.
   - **Rollback / fail-open:** if peer detection fails, fall through to current behavior.

3. **Make reviewer-provider fallback smarter**
   - **Failure evidence:** Run `25983337193` retried `x-ai/grok-4.1-fast` to exhaustion twice in the same run and got an empty output from `deepseek/deepseek-v4-pro` before succeeding on the next attempt.
   - **Root cause category:** provider instability plus retry policy that repeats already-failed choices.
   - **Exact fix:** once a provider exhausts retries in a run, mark it degraded for the rest of that run; when a provider returns empty output, skip directly to the next known-good fallback instead of reusing the same ladder.
   - **Expected reliability impact:** fewer blocked reviews and fewer long retry chains.
   - **Rollback / fail-open:** keep the current full provider list behind a flag.

4. **Treat check-run context as advisory data, not a blocking prerequisite**
   - **Failure evidence:** Run `25996049912` timed out after `120s` with one check-run still queued/in progress and proceeded anyway.
   - **Root cause category:** waiting on an external dependency that is not required for safe continuation.
   - **Exact fix:** shorter wait, explicit freshness stamp, and refresh on the next loop instead of blocking the current one.
   - **Expected reliability impact:** fewer timeout warnings and less chance that queued CI delays the autofix loop.
   - **Rollback / fail-open:** keep the timeout env-configurable.

5. **Keep the good fail-open behavior for non-critical integrations**
   - **Failure evidence:** `issue_pr_status` run `25996254467` succeeded in `9s` while logging `Support checkout ref ... is unavailable; using main.`, `Telegram send failed`, and TG cleanup warnings.
   - **Root cause category:** peripheral helper / notification availability.
   - **Exact fix:** do not make these paths blocking; instead emit structured counters so operators can see if the warning rate climbs.
   - **Expected reliability impact:** maintains core pipeline reliability while improving observability.
   - **Rollback / fail-open:** already healthy.

**MCP fallback / probe status**
- **Semble:** 22 deduped `SEMBLE_QUERY` events, **0** `SEMBLE_FALLBACK`, **0** `SEMBLE_PROBE` in the sampled actual review runs. That looks like healthy active usage, not a broken rollout.
- **Serena:** **0** query/fallback/probe events, with `SERENA_ENABLED=false` and `SERENA_AVAILABLE=false` in all 8 sampled actual review runs. This looks intentionally inactive, not like a masked failure. The smallest safe mitigation is simply to keep it off until probe telemetry exists.

## AI Memory Health

- **Sample basis:** 8 sampled actual `review_autofix` deep-dive runs with real memory telemetry (`25981932582`, `25983337193`, `25986904692`, `25987785204`, `25990356358`, `25991055127`, `25991911686`, `25996049912`).

- **Retrieve effectiveness is currently poor**
  - `retrieve` ops: **8**
  - hit rate: **0/8 = 0%**
  - average `estimated_tokens`: **0**
  - `keyword_method` distribution: **`none`: 8/8**
  - `fail_open: true`: **0**
  - `enabled: false`: **0**
  - Example: run `25996049912`, step `Retrieve reviewer memory context fail-open`, logged `records_selected: 0`.

- **Write path health is good**
  - `record-run-event`: **16**
  - `record-candidate`: **7**
  - push-attempt histogram on push-bearing ops: **`1`: 23**
  - No high push-retry counts were observed.

- **Coverage gaps**
  - No sampled actual deep-dive logs showed `finalize-task`, `promote`, `compact`, `processed-command-claim`, or `processed-command-complete`.
  - Outside `review_autofix`, sampled deep-dive logs did not show useful operational retrieval telemetry.

- **Recommendation**
  - Fix retrieval before expanding memory usage. The smallest safe move is to add a deterministic fallback keyword extractor (PR title + changed files + failing step) so retrieves stop defaulting to `keyword_method=none`.
  - Keep the write path as-is; it is not the problem.

## GH API Call Audit

The repo already codifies batching/cycle-local caching discipline in `agents.md` (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, `_candidate_details_json`). The highest-value remaining reductions are in `review_autofix` and a few small housekeeping flows.

1. **`review_autofix` / `Collect PR check-run failures...`**
   - **Evidence:** Run `25996049912` step implementation calls `repos/.../commits/${HEAD_SHA}/check-runs?per_page=100`, then logged four 30s wait iterations before timing out.
   - **High-redundancy pattern:** repeated snapshots of the same head SHA while nothing materially changes.
   - **Concrete change:** one initial snapshot, one short retry window, then stop. Reuse the first stable snapshot for the rest of the run.
   - **Estimated call-count reduction:** 3–4 REST calls per affected run (inference from the four wait loops plus initial snapshot).
   - **Rate-limit risk reduction:** medium.

2. **`review_autofix` / `Collect PR metadata`**
   - **Evidence:** Run `25996049912` step implementation includes a default-branch lookup, paginated issue comments, paginated review comments, a batched `closingIssuesReferences` GraphQL query, up to 20 fallback `issues/{n}` GETs, and `gh pr diff`. The same run later printed `Linked issues already cached from early fetch.` in step `Cache linked issues references`.
   - **High-redundancy pattern:** good early caching exists, but the fallback path can still fan out into per-issue REST calls and the same PR metadata is re-derived in multiple places.
   - **Concrete change:** persist repo default-branch and PR title/body from the first fetch, aggressively bound the body-text fallback, and reuse cached linked-issue metadata across continuation runs.
   - **Estimated call-count reduction:** 2–20 calls on fallback-heavy PRs.
   - **Rate-limit risk reduction:** medium.

3. **`issue_pr_status` / `Update linked issue labels when PR closes`**
   - **Evidence:** Run `25996254467` step implementation uses a single GraphQL `closingIssuesReferences` query, one REST PR fetch for title/body, per-issue label POSTs, and a GraphQL batch / REST fallback path for orchestrator-linked issue detection.
   - **High-redundancy pattern:** batching is already good; remaining waste is fallback GETs and any duplicated issue-number handling.
   - **Concrete change:** preserve the current batched GraphQL path, dedupe issue numbers before label POSTs, and only invoke per-issue REST fallback when the batch truly fails.
   - **Estimated call-count reduction:** 1–N GETs per PR close.
   - **Rate-limit risk reduction:** low-medium.

4. **`cancel_on_pr_close` / `Cancel queued/in-progress runs for closed PR branch`**
   - **Evidence:** Run `25996254464` step implementation contains two `repos/${REPOSITORY}/actions/runs` list calls and still concluded `No matching queued/in-progress pull_request workflow runs found...`.
   - **High-redundancy pattern:** no-op PR closures still pay for multiple list calls before learning there is nothing to cancel.
   - **Concrete change:** consolidate to one paginated fetch with client-side filtering, or skip the second query when the first already returns empty.
   - **Estimated call-count reduction:** 1 list call on no-op closes.
   - **Rate-limit risk reduction:** low.

**Observed rate-limit status:** no concrete rate-limit breach was observed in sampled actual deep-dive logs. The wrappers are already rate-limit-aware; the priority is reducing unnecessary calls, not emergency rate-limit handling.

## Prompt Cache & Memory System

- **Prompt cache is clearly intended**
  - `.github/workflows/review_autofix.yml` sets `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - In run `25996049912`, step `Pre-assemble static context cacheable across runs` explicitly says the stable prefix is being assembled so the provider can cache it across runs.

- **But prompt-cache economics are not observable today**
  - In sampled actual `review_autofix` deep-dive logs, there were **0** occurrences of `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.
  - That means hit rate, miss rate, create/read mix, and real savings are all unknown.

- **There is one good reuse signal**
  - Run `25996049912`, step `Cache linked issues references`, printed `Linked issues already cached from early fetch.`
  - That is exactly the right pattern: fetch once, reuse later, keep the later step cheap.

- **Likely cache-fragmentation causes**
  - Large dynamic blobs are being inserted every run: in `25996049912`, PR comments context was `76,521` bytes and diff context was `39,022` bytes.
  - Semble helps keep codebase retrieval smaller, but `overflow` queries still consumed `81,926` bytes across 11 calls.
  - Inference: volatile comments/diff content is probably weakening prefix stability and forcing more prompt variance than necessary.

- **Concrete recommendations**
  1. Keep the static system/runbook/code-policy prefix first and stable; move volatile PR-specific context later.
  2. Reuse hashed/summarized comment and diff digests across continuation runs on the same PR.
  3. Emit raw prompt/cache counters in the `Log token usage` step so prompt-cache tuning becomes evidence-driven.
  4. Treat AI memory as write-only until retrieval stops returning 0 records.

- **Estimated impact**
  - **Tokens/latency:** currently unquantifiable because counters are missing.
  - **Reliability:** high, because observability is the prerequisite for safe cache/model tuning.

## Orchestrator Health

- **Healthy gating on idle paths**
  - Recent runs `25996255244` (`clarify`), `25996255226` (`orchestrate_clarify_respond`), `25996255248` (`plan`), and `25996255235` (`implement`) all skipped in `0–1s` because their `if` conditions evaluated false.
  - Family skip rates are also very high: `clarify` 94.9%, `plan` 95.1%, `implement` 91.9%, `orchestrate_clarify_respond` 99.5%.
  - That is good health, not waste: the orchestrator is cheaply deciding when no work is needed.

- **Standing wait still exists in the poller**
  - `orchestrate_poll` had 21/21 successes, but avg `206.1s`, p50 `134s`, p95 `495s`.
  - Recent run `25991682725` still took `743s`, so the tail is meaningful.
  - **Smallest safe mitigation:** adaptive poll backoff keyed to “time since last state change,” and immediate exit once downstream state is terminal.

- **Main orchestrator pain point is handoff duplication into `review_autofix`**
  - The cancelled→successor pairs point to overlap between continuation dispatches and the `pull_request.synchronize` path.
  - This is where orchestration is leaking the most expensive waste today.

- **Telemetry coverage is not yet good enough for tool-routing decisions**
  - Semble: active and observable.
  - Serena: inactive and unprobed.
  - Prompt cache: intended but unmeasured.
  - Memory retrieval: measured, but ineffective.

- **Observable indicators to track**
  1. `review_autofix` cancel-with-successor rate
  2. `review_autofix` queue time before runner assignment
  3. `orchestrate_poll` p95 duration
  4. `SEMBLE_QUERY target=overflow` per run
  5. AI-memory retrieve hit rate
  6. CI contract-guard failures

## Pipeline Flow Bottlenecks

| Stage | What the data says | Bottleneck type | Ordered fix |
|---|---|---|---|
| Clarify → Plan → Respond | Mostly cheap skips: `clarify` p50 `1s`, `plan` p50 `1s`, `orchestrate_clarify_respond` p50 `1s` | Not a current bottleneck | Keep as-is |
| Implement | Mostly skipped, but active work can still be long (recent success `25987545937` took `803s`) | Compute when active; insufficient deep-dive evidence to isolate | Watch, but lower priority than review/CI |
| CI / Validate | `ci` failed 29/39 times from one repeated contract regression | Failure-loop bottleneck | Add fast contract guard first |
| Review / Autofix | Avg `1028.9s`, p95 `2699.1s`; queueing can consume 42–47% of outlier runs; provider retries and judge calls add more | Queue + compute + retry | Stale-run abort, conditional pass 2, shorter check-run wait, narrower judge |
| Merge / Conflict handling | Present but not dominant: only 3 `conflict-resolver-context` Semble queries (`20,006` bytes total) in sampled actual review runs | Merge/conflict overhead exists, but not the top issue | Do not over-optimize yet |
| Orchestrate / Poll | Family p95 `495s`; recent run `25991682725` took `743s` | Wait / poll overhead | Adaptive backoff + terminal-state short-circuit |
| Admin analysis | `workflow_log_analysis` `3842s`; `test_and_mark_stable` `3910s` | Separate long-tail admin compute | Downshift after PR-path fixes |

**End-to-end priority order**
1. Stop the CI regression loop so the expensive self-healing path is triggered less often.
2. Kill stale/superseded `review_autofix` runs before they queue.
3. Shorten advisory check-run waiting and conditionalize pass 2 / judge work.
4. Reduce `orchestrate_poll` standing wait.
5. Downshift the admin analysis workflow.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` queueing and long review compute (`avg 1028.9s`, `p95 2699.1s`)
  - CI regression burst (`29/39` failures in `ci`)
  - `orchestrate_poll` tail latency (`p95 495s`, recent `743s` outlier)

- **Top failure modes**
  - Historical checkout-ref contract mismatch in `implement.yml` vs `tests/test_workflow_checkout_integration_ref_audit.py`
  - Duplicate/superseded `review_autofix` dispatches causing cancellations
  - Reviewer-provider instability / empty-output retries in long review runs

- **Highest-cost drivers**
  - `workflow_log_analysis` run `25981605517`: `1,684,201` tokens
  - `review_autofix` two-pass review plus provider retries
  - Large dynamic prompt/context assembly (`76,521`-byte comment context + `39,022`-byte diff in run `25996049912`)
  - Widening summarizer budget (`164,297` mini tokens in current analysis run)

- **Top 3 prioritized actions**
  1. Add a fast checkout-ref contract guard and verify the workflow-support/ref path used by CI.
  2. Add stale-run self-abort to `review_autofix`, cut `CHECK_RUNS_WAIT_TIMEOUT_SECS`, and make pass 2 conditional.
  3. Downshift `workflow_log_analysis` reasoning/model usage and cut `WORKFLOW_LOG_SUMMARY_MAX_RUNS`.

## Metrics Appendix

### Run summary

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 1000 | 216 | 29 | 31 | 724 | 175.5 | 1.0 | 1236.0 |
| `ci` | 39 | 10 | 29 | 0 | 0 | 672.1 | 652.0 | 794.0 |
| `review_autofix` | 120 | 88 | 0 | 27 | 5 | 1028.9 | 677.5 | 2699.1 |
| `clarify` | 198 | 10 | 0 | 0 | 188 | 6.8 | 1.0 | 18.5 |
| `plan` | 185 | 9 | 0 | 0 | 176 | 10.8 | 1.0 | 10.8 |
| `implement` | 185 | 11 | 0 | 4 | 170 | 21.7 | 1.0 | 241.6 |
| `orchestrate_clarify_respond` | 185 | 1 | 0 | 0 | 184 | 1.1 | 1.0 | 2.0 |
| `orchestrate_poll` | 21 | 21 | 0 | 0 | 0 | 206.1 | 134.0 | 495.0 |
| `workflow_log_analysis` | 1 | 1 | 0 | 0 | 0 | 3842.0 | 3842.0 | 3842.0 |

### Review/autofix waste and retry summary

| Metric | Value |
|---|---:|
| Cancelled `review_autofix` runs | 27 |
| Cancelled `review_autofix` runtime | 29,209s (8.11h) |
| Cancelled→success pairs within 20s | 11 |
| Runtime in those 11 superseded cancelled runs | 23,491s (6.53h) |
| Workflow reruns (`retries > 0`) across all 1000 runs | 0 |

### Representative latency outliers

| Run | Workflow | Total s | Key evidence |
|---|---|---:|---|
| `25990356358` | `Internal: AI Review & Autofix` | 4867 | queue before runner `2044.9s` (42.0%); main review step `2774.6s` |
| `25991911686` | `Internal: AI Review & Autofix` | 2853 | queue before runner `1334.3s` (46.8%); main review step `1508.5s` |
| `25983337193` | `Internal: AI Review & Autofix` | 3066 | queue only `5.5s`; main review step `3026.4s`; two-pass + retry-heavy |
| `25996049912` | `Codex PR Self-Healing Semantic Agent` | 552 | judge `255.2s`; check-run wait `120.8s`; free disk `44.7s` |

### `workflow_log_analysis` token outlier (`25981605517`)

| Step | Duration s | Share of run | Tokens | Share of step-token total |
|---|---:|---:|---:|---:|
| `step-001 api-redundancy` | 688.4 | 17.9% | 172,181 | 10.2% |
| `step-002 deep-audit` | 1307.8 | 34.0% | 554,911 | 32.9% |
| `step-003 analyze-commit-notify` | 1748.2 | 45.5% | 957,109 | 56.8% |
| **Observed total** | — | — | **1,684,201** | **100%** |

### Unselected-run summarizer telemetry (current analysis run)

| Run | Op | Model | Targeted | Summarized | Tokens used |
|---|---|---|---:|---:|---:|
| `25981605517` | `summarize_unselected_runs` | `openai/gpt-5.4-mini` | 100 | 77 | 164,297 |

### Semble / Serena MCP summary (sampled actual `review_autofix` deep-dive runs only)

| System | Sample basis | Queries | Fallbacks | Probes | Logged bytes | Logged response bytes | Total logged lookup time | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | 8 runs | 22 | 0 | 0 | 197,288 | n/a | 10.6s | Active; overflow was 41.5% of bytes |
| Serena | 8 runs | 0 | 0 | 0 | 0 | 0 | 0 | `SERENA_ENABLED=false`, `SERENA_AVAILABLE=false` |

### Semble target breakdown

| Target | Queries | Bytes | Avg bytes/query | Notes |
|---|---:|---:|---:|---|
| `reviewer-context` | 7 | 89,585 | 12,798 | Healthy core retrieval |
| `overflow` | 11 | 81,926 | 7,448 | Concentrated in runs `25981932582` and `25991055127` |
| `conflict-resolver-context` | 3 | 20,006 | 6,669 | Present but not dominant |
| `review-blocked-judge-context` | 1 | 5,771 | 5,771 | Very small relative to judge runtime |

**Other MCP servers observed:** none in sampled actual deep-dive logs.

### MCP availability rows

| Server | Sampled runs | probe_ok | probe_failed | probe_skipped | Notes |
|---|---:|---:|---:|---:|---|
| Semble | 8 | 0 | 0 | 8 | No `SEMBLE_PROBE` lines; availability inferred from `SEMBLE_ENABLED=true`, `SEMBLE_AVAILABLE=true` |
| Serena | 8 | 0 | 0 | 8 | No `SERENA_PROBE` lines; `SERENA_ENABLED=false`, `SERENA_AVAILABLE=false` |

### AI memory telemetry (sampled actual `review_autofix` deep-dive runs only)

| Metric | Value |
|---|---|
| Retrieve ops | 8 |
| Retrieve hit rate | 0/8 = 0% |
| Avg `estimated_tokens` per retrieve | 0 |
| `keyword_method` distribution | `none`: 8/8 |
| Retrieve `fail_open:true` | 0 |
| Retrieve `enabled:false` | 0 |
| `record-run-event` ops | 16 |
| `record-candidate` ops | 7 |
| Push-attempt histogram (push-bearing ops) | `1`: 23 |
| Unobserved in sample | `finalize-task`, `promote`, `compact`, `processed-command-*` |

### Prompt cache / observability

| Signal | Observation |
|---|---|
| Prompt cache intended? | Yes — `OPENROUTER_PROMPT_CACHE_DISABLED=false` in config/logs |
| Stable cacheable prefix intent | Yes — step `Pre-assemble static context cacheable across runs` says provider can cache stable prefix |
| Raw prompt/completion/total token counters | Not observed in sampled actual `review_autofix` deep-dive logs |
| Prompt-cache create/read counters | Not observed |
| Explicit reuse signal | `Linked issues already cached from early fetch.` in run `25996049912` |
| Context-size sample | comments `76,521` bytes; diff `39,022` bytes; check-run snapshot `511` bytes |

### GH API hotspot summary

| Workflow / job / step | Observed pattern | Current good hygiene | Concrete reduction opportunity |
|---|---|---|---|
| `review_autofix` / `codex-agent` / `Collect PR check-run failures...` (`25996049912`) | repeated polling on the same `HEAD_SHA` until timeout | bounded timeout already exists | snapshot sooner; reuse first stable view |
| `review_autofix` / `codex-agent` / `Collect PR metadata` (`25996049912`) | default-branch lookup, paginated comments, GraphQL linked issues, fallback per-issue GETs, `gh pr diff` | later linked-issue cache reuse exists | reuse initial PR metadata and tighten fallback fan-out |
| `issue_pr_status` / `sync-issue-status` / `Update linked issue labels...` (`25996254467`) | batched GraphQL + REST PR fetch + per-issue label POSTs + REST fallback | batching already present | dedupe issue list and skip fallback GETs when batch succeeds |
| `cancel_on_pr_close` / `cancel-active-runs` / `Cancel queued/in-progress...` (`25996254464`) | two list calls before concluding nothing to cancel | retry wrapper exists | consolidate no-op close handling into one fetch |

*Note:* successful-path tuning is based on targeted `slow/` + `recent/` deep dives and `log_summary` rows, not a random raw-telemetry sample of all 216 successful runs.

## Deep Audit — Workflows & Scripts (2026-05-17)

### Section 1: Bug & Correctness Sweep

Audit notes: all `.github/workflows/*.yml` parsed with `yaml.safe_load`, all `scripts/*.sh` passed `bash -n`, and all `scripts/*.py` passed `py_compile`.

- **BUG-001**
  - **File path:** `.github/workflows/plan.yml:393-399,646-684`
  - **Severity:** High
  - **Category tag:** `bug`
  - **Description:** `Fetch issue comments` writes the raw `gh api --paginate` stream straight to `ISSUE_COMMENTS_FILE`. That file is then treated as one array by the later `jq` that finds the latest `/answer` and preceding clarification block. With 2+ pages, the file becomes a stream of per-page arrays, so `sort_by(...) | last` and the reverse-scan logic run page-by-page instead of across the full thread. Long issue threads can therefore feed the planner the wrong answer/question pair.
  - **Recommended fix:** Merge pages before writing the file, e.g. `gh_retry gh api --paginate ... | jq -s 'add // []' > "${ISSUE_COMMENTS_FILE}"`, or switch the step to `gh_api_json_to_file` plus a slurp merge. This repo already uses the merged-page pattern in `.github/workflows/implement.yml:1164`.

- **BUG-002**
  - **File path:** `.github/workflows/plan.yml:461-470`
  - **Severity:** Medium
  - **Category tag:** `bug`
  - **Description:** `LINKED_PR_COUNT` is computed with `gh api --paginate ... --jq '[...] | length'`. `--jq` runs once per page, so a multi-page timeline can yield a multiline value like `0\n1`. The later `[ "${LINKED_PR_COUNT}" -gt 0 ]` test then becomes non-numeric and falls through false inside the `if`, which can let planning continue even though an open linked PR exists.
  - **Recommended fix:** Slurp pages before counting (`... --paginate | jq -s 'add // [] | map(...) | length'`) or replace the REST timeline walk with one GraphQL query that returns the open cross-referenced PR count once.

- **BUG-003**
  - **File path:** `scripts/validate_process.sh:2836-2840`
  - **Severity:** Medium
  - **Category tag:** `bug`
  - **Description:** Prior validation-failure context is supposed to be capped to the latest 3 comments, but the code uses `gh api --paginate --jq '[...] | .[-3:] | .[].body'`. Because `--jq` executes per page, the `[-3:]` cap is applied page-by-page, not across the merged history. A multi-page tracking issue can therefore inject more than 3 old failures into the next validation prompt.
  - **Recommended fix:** Fetch the paginated comments once and slice after slurping (`... --paginate | jq -s 'add // [] | map(select(...)) | .[-3:] | .[].body'`), or switch to a bounded GraphQL `comments(last: 3)` query.

### Section 2: GitHub API Call Redundancy Audit

- **API-001**
  - **File path:** `scripts/orchestrate_poll_process.sh:3875-3880,3931-3936,3985-3987`
  - **Severity:** High
  - **Category tag:** `api-redundancy`
  - **Description:** The final-merge path fetches the same PR from `repos/${GITHUB_REPOSITORY}/pulls/${final_pr}` eight times on the existing-PR path: 2 calls before the first merged check, 3 more before mergeability gating, and 3 more after a failed merge attempt. Besides cost, `state`, `mergeable`, and `merged_at` are read from different snapshots, so the branch can make merge/heal/retry decisions on mixed PR state.
  - **Current call count:** **8** `GET /pulls/{final_pr}` calls on the hot existing-PR path.
  - **Proposed call count:** **2** calls total: one snapshot before decision-making, one refresh only after a failed merge attempt.
  - **Existing pattern to extend:** `gh_api_json_to_file` in `scripts/gh_helpers.sh` plus the poller’s existing cycle-local JSON reuse style (`_candidate_details_json`).
  - **Recommended fix:** Fetch the PR JSON once per decision point, parse `state`, `mergeable`, and `merged_at` locally, and carry that snapshot through the branch logic instead of reissuing field-specific `_safe_gh_jq` calls.

- **BATCH-001**
  - **File path:** `.github/workflows/review_autofix.yml:521-540,566`
  - **Severity:** Medium
  - **Category tag:** `api-batching`
  - **Description:** When `closingIssuesReferences` is empty, the standalone-validate dispatch falls back to parsing issue numbers from the PR body/title and then calls `gh issue view ... --json labels` once per linked issue when `labels_known != 'true'`. That is an avoidable per-item REST loop on data that can be fetched in one GraphQL batch.
  - **Current call count:** **N** per-issue REST calls for **N** fallback-linked issues.
  - **Proposed call count:** **1** batched GraphQL call for up to 25 issues (`ceil(N/25)` if needed).
  - **Existing pattern to extend:** `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.
  - **Recommended fix:** After building fallback `issue_nodes_json`, batch-enrich it with labels once, then keep the loop API-free except for the actual validate dispatch and label removal.

- **API-002**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:1728-1750`
  - **Severity:** Medium
  - **Category tag:** `api-redundancy`
  - **Description:** `gh_api_with_retry()` retries every `gh api` failure 3 times but does not classify permanent failures (`404`, `422`, token-scope/auth errors). That burns 2 extra calls on deterministic failures in a long release-gate workflow.
  - **Current call count:** Up to **3** attempts for a permanent failure.
  - **Proposed call count:** **1** attempt for permanent failures; retries only for transient failures.
  - **Existing pattern to extend:** `gh_retry` and `_is_gh_permanent_failure` in `scripts/gh_helpers.sh`.
  - **Recommended fix:** Source `scripts/gh_helpers.sh` in this step and replace the local wrapper with `gh_retry gh api` / `gh_retry_to_file`, or port the permanent-failure branch into the local wrapper.

- **API-003**
  - **File path:** `.github/workflows/clarify.yml:387-402`
  - **Severity:** Low
  - **Category tag:** `api-redundancy`
  - **Description:** `Fetch issue comments` always fetches the first 50 comments into `ISSUE_COMMENTS_FILE`, then fetches the full thread again when semantic cache is enabled to build `THREAD_HISTORY_FILE`. The second fetch subsumes the first unless the exact server-side 50-comment snapshot is a deliberate compatibility contract. [NEEDS VERIFICATION]
  - **Current call count:** **2** logical comment fetches.
  - **Proposed call count:** **1** logical fetch, with the 50-comment slice derived locally.
  - **Existing pattern to extend:** The single-fetch JSON-file reuse pattern in `scripts/gh_helpers.sh` (`gh_api_json_to_file`).
  - **Recommended fix:** Fetch the full comments array once, write it to a temp JSON file, derive `ISSUE_COMMENTS_FILE` with a local `jq '.[0:50]'`, and render `THREAD_HISTORY_FILE` from the same cached payload.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**
  - **File path:** `.github/workflows/clarify.yml:174-286; .github/workflows/plan.yml:225-336; .github/workflows/orchestrate.yml:166-198,299-425; .github/workflows/orchestrate_clarify_respond.yml:215-348; .github/workflows/orchestrate_poll.yml:245-397; .github/workflows/review_autofix.yml:876-1184; .github/workflows/validate.yml:207-583`
  - **Severity:** Medium
  - **Category tag:** `duplication`
  - **Description:** The workflow-support bootstrap logic is duplicated across the main AI workflows: self-repo short-circuit, support-ref checkout, fallback checkout, main-branch fallback, and file staging/copy. The copies are already structurally drifting (`validate.yml` inlines one huge step, others split into 4 steps), which increases regression risk on the checkout-ref contract.
  - **Recommended fix:** Move this into a shared owner such as `scripts/fetch_workflow_support.sh` with a signature like `fetch_workflow_support <support_repo> <script_ref> <stage_root> <manifest_file>`. Update `clarify.yml`, `plan.yml`, `orchestrate*.yml`, `review_autofix.yml`, and `validate.yml` to pass only workflow-specific manifests.

- **DUP-002**
  - **File path:** `scripts/review_run_reviewers.sh:348-357; scripts/review_conflict_prepare.sh:448-457; scripts/review_apply_fixes.sh:435-444`
  - **Severity:** Low
  - **Category tag:** `duplication`
  - **Description:** `append_semble_query_section()` is duplicated byte-for-byte in 3 review-side scripts. Any future change to truncation, newline behavior, or empty-file handling now requires 3 synchronized edits.
  - **Recommended fix:** Move it into `scripts/semble_helpers.sh` as `append_semble_query_section <label> <path> [max_bytes]`, then source that helper from all 3 callers.

- **DUP-003**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:455-565,580-736,766-943,1182-1593,1663-2079`
  - **Severity:** Medium
  - **Category tag:** `duplication`
  - **Description:** `test-and-mark-stable.yml` repeats the same wait/poll shell pattern across clarify, plan, implement, review, and canary-verification phases: capture prior run state, poll for a matching run, watch status/conclusion, enforce inactivity/time budgets, and emit phase-specific diagnostics. The copies are already drifting in retry and API-wrapper behavior.
  - **Recommended fix:** Extract a shared watcher such as `scripts/e2e_wait_phase.sh` with a signature like `e2e_wait_phase <phase> <repo> <issue_number> <created_after> [--pr <n>] [--head-sha <sha>] [--timeout-mins <n>] [--accept <csv>]`, and keep the workflow YAML limited to phase-specific inputs.

### Section 4: Expression Size Limit Risk Assessment

Static counts below are approximate `run:` scalar sizes for blocks containing `${{ }}`.

- **EXPR-001**
  - **File path:** `.github/workflows/review_autofix.yml:1477-1865`
  - **Severity:** High
  - **Category tag:** `expression-limit`
  - **Description:** Approximate interpolated `run:` size is **21,048** characters for `Collect PR metadata`, leaving **-48** characters of headroom against the 21,000-character limit. [NEEDS VERIFICATION]
  - **Recommended fix:** Extract the whole step to `scripts/review_collect_pr_metadata.sh`.

- **EXPR-002**
  - **File path:** `.github/workflows/validate.yml:211-583`
  - **Severity:** High
  - **Category tag:** `expression-limit`
  - **Description:** Approximate interpolated `run:` size is **20,816** characters for `Fetch workflow support files`, leaving only **184** characters of headroom. [NEEDS VERIFICATION]
  - **Recommended fix:** Extract the support bootstrap to `scripts/fetch_workflow_support.sh`.

- **EXPR-003**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:1204-1587`
  - **Severity:** High
  - **Category tag:** `expression-limit`
  - **Description:** Approximate interpolated `run:` size is **23,499** characters for `Phase 4: Wait for review & autofix to complete`, leaving **-2,499** characters of headroom. [NEEDS VERIFICATION]
  - **Recommended fix:** Extract this wait loop to `scripts/e2e_wait_review.sh` or the shared `scripts/e2e_wait_phase.sh`.

- **EXPR-004**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:1674-2078`
  - **Severity:** High
  - **Category tag:** `expression-limit`
  - **Description:** Approximate interpolated `run:` size is **21,288** characters for `Phase 4b: Verify editor restored canary (pytest + retry)`, leaving **-288** characters of headroom. [NEEDS VERIFICATION]
  - **Recommended fix:** Move the canary fetch / pytest / retry logic into `scripts/e2e_verify_canary.sh`.

- **EXPR-005**
  - **File path:** `.github/workflows/review_autofix.yml:918-1184`
  - **Severity:** Medium
  - **Category tag:** `expression-limit`
  - **Description:** Approximate interpolated `run:` size is **17,427** characters for `Stage workflow support files`, leaving **3,573** characters of headroom. [NEEDS VERIFICATION]
  - **Recommended fix:** Fold this into the same extracted `scripts/fetch_workflow_support.sh` path as `validate.yml`.

- **EXPR-006**
  - **File path:** `.github/workflows/orchestrate_clarify_respond.yml:862-1144`
  - **Severity:** Medium
  - **Category tag:** `expression-limit`
  - **Description:** Approximate interpolated `run:` size is **15,140** characters for `Parse and post answer`, leaving **5,860** characters of headroom. [NEEDS VERIFICATION]
  - **Recommended fix:** Split parsing, loop-detection, and posting into separate steps or extract the step to `scripts/orchestrate_clarify_respond_post_answer.sh`.

No workflow currently exceeds the **800 KB** early-warning threshold; the largest is `.github/workflows/review_autofix.yml` at **338,239** bytes.

### Section 5: Cross-Cutting Concerns

No `TODO` / `FIXME` / `HACK` markers were present in `.github/workflows/*.yml` or `scripts/*.{sh,py}`.

- **DEAD-001**
  - **File path:** `scripts/orchestrate_poll_process.sh:5235-5241`
  - **Severity:** Low
  - **Category tag:** `dead-code`
  - **Description:** `read_standalone_state_json()` has no in-repo callers; repo-wide search finds only its definition. The wrapper still carries its own paginated `/comments` fetch, so it adds dead API-touching surface.
  - **Recommended fix:** Remove `read_standalone_state_json()` if no external consumer sources it, or keep only the pure parser helper `_extract_standalone_state_json_from_comments`.

- **DEAD-002**
  - **File path:** `scripts/review_issue_ledger.sh:862-867,912-918`
  - **Severity:** Low
  - **Category tag:** `dead-code`
  - **Description:** `CURRENT_FLOOR` is declared and populated for each issue ID, but it is never read later in the file. The floor-category associative array is dead state.
  - **Recommended fix:** Delete `CURRENT_FLOOR`, or wire it into later collision-resolution / summary logic if floor metadata is meant to affect ledger behavior.

- **CONSIST-001**
  - **File path:** `.github/workflows/issue_pr_status.yml:235-249; scripts/label_helpers.sh:146-194`
  - **Severity:** Low
  - **Category tag:** `consistency`
  - **Description:** `issue_pr_status.yml` sources `scripts/label_helpers.sh` and then defines a weaker fallback `set_issue_phase_label_resilient()` if the function is missing. The inline fallback only POST-adds the target label, while the canonical helper first reads current labels and replaces the whole phase set with a PUT. Those two paths can leave contradictory phase labels on the same issue.
  - **Recommended fix:** Keep one implementation only: fail fast if `set_issue_phase_label_resilient` is unavailable after sourcing, or move any fallback semantics into `scripts/label_helpers.sh` so every caller uses the same contract.

- **SHELL-001**
  - **File path:** `scripts/validate_process.sh:230-238`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** `local msg="$1$(_tg_link_suffix)"` triggers ShellCheck `SC2155`: the declaration masks the command-substitution exit status. A future `_tg_link_suffix` failure would be hidden by the `local` builtin’s success.
  - **Recommended fix:** Split declaration and assignment, e.g. `local msg` then `msg="$1$(_tg_link_suffix)"`.

- **SHELL-002**
  - **File path:** `scripts/review_commit_changes.sh:482-490; scripts/review_conflict_resolve.sh:1474-1476`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** Both scripts set the authenticated remote with an unquoted credential-bearing URL (`git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`), which is a ShellCheck `SC2086` expansion point and keeps the token on the command line longer than necessary.
  - **Recommended fix:** At minimum, assign the URL to a quoted variable before calling `git remote set-url`. Preferably, reuse the repo’s existing `http.extraheader` pattern from the workflow support-clone steps so pushes do not embed the token in the argv string at all.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 6 | BUG-001, API-001, EXPR-001, EXPR-002, EXPR-003, EXPR-004 |
| Medium | 8 | BUG-002, BUG-003, BATCH-001, API-002, DUP-001, DUP-003, EXPR-005, EXPR-006 |
| Low | 7 | API-003, DUP-002, DEAD-001, DEAD-002, CONSIST-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2-3 | Medium |
| API call optimization | 4 | Medium |
| Code modularization | 9-12 | Large |
| Expression size reduction | 5-8 | Large |
| Medium/Low fixes | 6-8 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-17)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap was proven safe to collapse without changing endpoint/filter/error/concurrency behavior; `NEEDS_VERIFICATION` means the redundancy looks real but at least one safety precondition is not statically proven; `RISKY_SKIP` means the redundancy is visible, but the call lives in pagination/retry/race-defense code and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — NEEDS_VERIFICATION**
  - **File path and lines:** `scripts/review_rb_judge.sh:246-256`, `scripts/review_rb_judge.sh:267-284`
  - **Current call count:** `1` GraphQL call plus either `1` REST PR fetch (empty-linked path) or `1..N` REST issue fetches (linked-issue path)
  - **Proposed call count:** `1`
  - **Endpoint(s):** GraphQL `repository.pullRequest(number).closingIssuesReferences`; REST `GET /repos/{repo}/pulls/{pr_number}`; REST `GET /repos/{repo}/issues/{issue_number}`
  - **Evidence:**
    ```bash
    ISSUE_NUMBERS="$(gh_retry gh api graphql \
      ... pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } ...)"

    if [ -z "${ISSUE_NUMBERS}" ]; then
      PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    fi

    ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
    BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' ...)"
    FIRST_ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_META_JSON}" | jq -c '[(.labels // [])[]?.name]' ...)"
    ```
  - **Proposed fix:** Capture the full GraphQL payload once, widen the selection to `pullRequest { title body closingIssuesReferences(first:50) { nodes { number body labels(first:50){nodes{name}} } } }`, and derive `ISSUE_NUMBERS`, `FIRST_ISSUE_BODY`, `FIRST_ISSUE_LABELS_JSON`, and PR title/body fallback locally from that single payload; use the existing batched-GraphQL style documented in `agents.md` and exemplified by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.
  - **Safety rationale:** `NEEDS_VERIFICATION` because this replaces two REST fail-open branches with one richer GraphQL payload, so empty/error handling and “first usable issue body” behavior must be re-proved.
  - **Downstream signal:** Verify that the widened `closingIssuesReferences` payload exposes `body` and `labels` for the referenced issues and that the single-payload fallback still matches today's empty-linked and first-issue selection semantics before removing the REST calls.

- **MERGE-002 — NEEDS_VERIFICATION**
  - **File path and lines:** `.github/workflows/review_autofix.yml:514-523`
  - **Current call count:** `2` on the `closingIssuesReferences == []` path
  - **Proposed call count:** `1`
  - **Endpoint(s):** GraphQL `repository.pullRequest(number)`; REST `GET /repos/{repo}/pulls/{pr_number}`
  - **Evidence:**
    ```bash
    issue_nodes_json="$(gh api graphql \
      ... pullRequest(number:$number) {
        closingIssuesReferences(first: 50) {
          nodes { number labels(first: 100) { nodes { name } } }
        }
      } ...)"

    if [ -z "${issue_nodes_json}" ] || [ "${issue_nodes_json}" = "[]" ]; then
      pr_data="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...)"
    fi
    ```
  - **Proposed fix:** In step `Dispatch standalone validate for orchestrator short-circuit issues`, stop `--jq`-extracting only `issue_nodes_json`; instead capture the full GraphQL payload once, add `title` and `body` to the `pullRequest` selection, and derive the regex fallback `pr_data` from the same payload. This is separate from Deep Audit `BATCH-001`, which covers the per-issue label enrichment loop.
  - **Safety rationale:** `NEEDS_VERIFICATION` because the merged payload changes the fallback source and requires preserving current behavior when GraphQL succeeds with empty links versus when the GraphQL request itself fails.
  - **Downstream signal:** Verify that merged/closed PRs always return a usable `pullRequest` object in this job, then switch to a single captured GraphQL payload and retain the REST PR fetch only as a GraphQL-request-failure fallback.

- **MERGE-003 — NEEDS_VERIFICATION**
  - **File path and lines:** `.github/workflows/test-and-mark-stable.yml:2797-2807`
  - **Current call count:** `2` per poll iteration after `EXISTING_RUN_ID` is found
  - **Proposed call count:** `1` per poll iteration
  - **Endpoint(s):** REST `GET /repos/{repo}/actions/runs/{run_id}`
  - **Evidence:**
    ```bash
    while [ "${EXISTING_STATUS}" != "completed" ] ...; do
      sleep 5
      EXISTING_STATUS=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
        --jq '.status // ""' ...)
      EXISTING_CONCLUSION=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
        --jq '.conclusion // ""' ...)
    done
    ```
  - **Proposed fix:** Replace the paired field fetches with one `gh api "repos/.../actions/runs/${EXISTING_RUN_ID}"` call per loop iteration, parse `{status, conclusion}` locally, and keep the surrounding timeout/logging unchanged.
  - **Safety rationale:** `NEEDS_VERIFICATION` because a single fetch changes how partial field-read failures surface inside the timeout loop.
  - **Downstream signal:** Verify that one empty/malformed combined fetch produces the same timeout/failure behavior as today's split field fetches before collapsing the two GETs.

- **MERGE-004 — RISKY_SKIP**
  - **File path and lines:** `.github/workflows/cancel_on_pr_close.yml:68-89`
  - **Current call count:** `2` logical paginated list-runs queries
  - **Proposed call count:** `1` logical list-runs query
  - **Endpoint(s):** REST `GET /repos/{repo}/actions/runs`
  - **Evidence:**
    ```bash
    queued_runs_json="$(
      _gh_retry gh api ... "repos/${REPOSITORY}/actions/runs" --paginate -f status=queued ...
    )"
    in_progress_runs_json="$(
      _gh_retry gh api ... "repos/${REPOSITORY}/actions/runs" --paginate -f status=in_progress ...
    )"
    ```
  - **Proposed fix:** If this path is manually reviewed and approved, replace the two status-specific queries with one branch/event-filtered list-runs query and apply the `queued|in_progress` filter client-side.
  - **Safety rationale:** `RISKY_SKIP` because this code is both paginated and on a PR-close cancellation/race path, so changing query shape can alter page-boundary and timing behavior.
  - **Downstream signal:** Do not auto-implement; manually test busy-branch close events to prove one status-agnostic list query returns the exact same cancel set before changing this race-defense step.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — NEEDS_VERIFICATION**
  - **File path and lines:** `.github/workflows/issue_pr_status.yml:284-349`, `.github/workflows/issue_pr_status.yml:383-386`, `.github/workflows/issue_pr_status.yml:503-512`
  - **Current call count:** `1..N` REST issue-body fetches in the merged-alert step
  - **Proposed call count:** `0` extra API calls in the merged-alert step
  - **Endpoint(s):** Earlier GraphQL `repository { issue(number) { labels body } }` batch; later REST `GET /repos/{repo}/issues/{issue_number}`
  - **Evidence:**
    ```bash
    ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" ...)"
    ...
    MANAGED_ISSUES="${_managed_issues}"
    ...
    echo "LINKED_ISSUE_NUMBERS<<EOF" >> "$GITHUB_ENV"
    echo "${ISSUE_NUMBERS}" >> "$GITHUB_ENV"

    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
      IS_ORCHESTRATED="true"
    fi
    ```
  - **Proposed fix:** In `Update linked issue labels when PR closes`, export a boolean such as `HAS_ORCHESTRATED_LINKED_ISSUE` (or export `MANAGED_ISSUES`) alongside `LINKED_ISSUE_NUMBERS`; in `Send PR merged Telegram alert`, consume that exported value instead of re-fetching each issue body.
  - **Safety rationale:** `NEEDS_VERIFICATION` because the alert-step suppression rule must match the earlier managed/tracking classification exactly before replacing the live body scan.
  - **Downstream signal:** Verify whether the alert should suppress on `MANAGED_ISSUES`, `TRACKING_ISSUES`, or both, then export that exact earlier classification result and remove the later per-issue body GET loop.

### Dead Calls (DEAD-API-###)

- **DEAD-API-001 — NEEDS_VERIFICATION**
  - **File path and lines:** `.github/workflows/issue_pr_status.yml:181-207` (call site), `.github/workflows/internal-issue-pr-status.yml:3-12` and `workflow-templates/ai-issue-pr-status.yml:5-12` (in-repo caller evidence)
  - **Current call count:** `0..1` per run
  - **Proposed call count:** `0`
  - **Endpoint(s):** REST `GET /repos/{repo}/pulls/{pr_number}`
  - **Evidence:**
    ```bash
    PR_TITLE: ${{ github.event.pull_request.title }}
    PR_BODY: ${{ github.event.pull_request.body || '' }}
    ...
    PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
    if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
      PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...)"
    fi
    ```

    ```yaml
    on:
      pull_request:
        types: [closed]
    jobs:
      sync-status:
        uses: shubhodeep1/coding-workflows/.github/workflows/issue_pr_status.yml@main
    ```
  - **Proposed fix:** After verifying supported callers always invoke this reusable workflow from `pull_request.closed` (or otherwise always provide non-blank `github.event.pull_request.title/body`), delete the conditional `gh api pulls/{PR_NUMBER}` fallback and rely on the already-injected event payload.
  - **Safety rationale:** `NEEDS_VERIFICATION` because `issue_pr_status.yml` is a reusable `workflow_call` entrypoint, so repo-local wrappers are not proof of every external caller's payload contract.
  - **Downstream signal:** Verify every supported caller invokes `issue_pr_status.yml` from a `pull_request.closed` event with non-blank title/body payloads; only then remove the fallback PR fetch.

### Cross-References to Deep Audit Section

- `API-001`: `RISKY_SKIP` — valid redundancy, but it sits in `scripts/orchestrate_poll_process.sh` final-merge/race-defense code, which this pass must not auto-collapse.
- `BATCH-001`: `NEEDS_VERIFICATION` — batching the fallback issue-label enrichment is directionally correct, but it changes the standalone-validate step’s GraphQL/REST fallback semantics.
- `API-002`: `RISKY_SKIP` — the retry-wrapper change is sound, but the call is inside a backoff/retry path and needs manual review.
- `API-003`: `RISKY_SKIP` — the overlap is real, but both reads are paginated comment fetches, so page-shape semantics must be checked manually.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 5 | MERGE-001, MERGE-002, MERGE-003, REUSE-001, DEAD-API-001 |
| RISKY_SKIP | 1 | MERGE-004 |

### Implement-Stage Handoff

- No SAFE_TO_MERGE findings in this pass.
