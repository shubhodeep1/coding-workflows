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
