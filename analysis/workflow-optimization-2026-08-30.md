## Executive Summary

- **Review/autofix is the dominant critical path.** Five detailed active runs averaged **6,393s (106.5 min)**; workflow-family p95 was **6,110.6s**. Every detailed active run had a `stall_guard`; circuit breaking was disabled. Enabling the existing breaker should save roughly **10–15 minutes per affected review**. **Confidence: high.**
- **The 5-minute poller is near saturation.** `orchestrate_poll` p50 was **279.5s (93% of cadence)** and p95 **525.75s**, with 6 cancellations. Run `33325127600` spent **37.6s** fetching all refs and **85.2s** writing two memory events. Target: **80–100s saved per ordinary poll**. **Confidence: high.**
- **Duplicate review launches produce extreme queueing.** Runs `33298304735`, `33298506770`, and `33300215575` waited **4,995–6,007s** before requesting a runner, then discovered merged PRs and performed no model work. **Confidence: high.**
- **AI cost is concentrated in review panels:** **84 OpenRouter calls and 27.08M total tokens**. Five detailed runs made **71 calls versus a 60-call two-pass baseline**, indicating **15.5% retry/failback overhead**. **Confidence: high.**
- **Cache reuse is useful but uneven.** Derived aggregate hit rate was **66.2%**; detailed DeepSeek usage reached **85.9%**, while Mistral reached only **0.65%** despite logging `prompt_reused=true`. **Confidence: medium** because provider accounting differs.
- **Operational success is high once skipped runs are excluded:** 236 successes, 1 failure, and 12 cancellations among 249 non-skipped runs—**94.8% successful**. The sole failure was security audit run `33301196549`. **Confidence: high.**

## Speed Optimizations

1. **Critical path: enable reviewer circuit breaking**
   - **Evidence:** Active review runs `33298314768`, `33298515312`, `33300999483`, `33304922307`, and `33305241958` all recorded `stall_guard`; Kimi was affected in four, Minimax in two. Three runs used failback models.
   - **Root cause:** `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`, so unhealthy slots continue receiving work across runs.
   - **Exact change:** Enable the existing breaker with threshold 3 and 30-minute TTL. Add `elapsed_ms`, `first_token_ms`, and `termination_reason` to `REVIEWER_SLOT_STATE`.
   - **Savings:** Target **10–15 minutes/run**, or **10–15%** of active review latency.
   - **Risk:** Low–medium; failback preserves panel coverage. Roll back by setting the flag to `0`.

2. **Critical path: remove poller fixed overhead**
   - **Evidence:** Run `33325127600`:
     - Full-ref checkout: **37.6s**
     - `poll_started` memory write: **43.1s**
     - `poll_completed` memory write: **42.1s**
     - Semble install/index phase: approximately **26s**, with no `SEMBLE_QUERY`.
   - **Root cause:** Every cycle fetches all branches/tags, independently clones/pushes memory twice, and eagerly prepares Semble.
   - **Exact change:**
     - Fetch default/state/integration refs explicitly instead of `+refs/heads/*` and all tags.
     - Reuse one `ai-memory` checkout across start/end events, or batch both events into the final push.
     - Initialize Semble lazily on first judge/conflict query.
   - **Savings:** **80–100s/poll**.
   - **Risk:** Medium for scoped ref fetching; low for clone reuse and lazy Semble.

3. **Critical-path control plane: deduplicate review successors**
   - **Evidence:** Wrapper runs waited **83–100 minutes** before runner allocation and then no-op’d because PRs `#3910`, `#3911`, and `#3913` were already merged. Runs `33300996427` and `33305238818` were cancelled before their first agent step after 5,766s and 6,099s.
   - **Root cause:** PR-event and explicit dispatch paths both launch the same `(PR, head SHA)` review. Continuation dispatch explicitly bypasses peer detection.
   - **Exact change:** Before every explicit dispatch, query queued/in-progress runs using `(PR, head_sha)` and skip only when a peer is confirmed to have `should_run=true`; remain fail-open on API errors.
   - **Savings:** Removes **83–100 minute reported waits** and roughly **2–4 minutes** of eventual no-op work.
   - **Risk:** Low if keyed by exact head SHA.

4. **Micro-optimization: suppress terminal projects earlier**
   - **Evidence:** Poller run `33325127600` spent 6.4s processing tracking issue `#3845` only to report “Project already complete.”
   - **Change:** Remove the active tracking label or exclude terminal state during discovery.
   - **Savings:** About **6s plus at least two API reads per poll** while such projects remain active.
   - **Risk:** Low.

5. **Diagnostic prerequisite: decompose CI**
   - **Evidence:** CI p50 was **1,223s**, p95 **1,292.5s**, but no CI deep-dive logs were collected.
   - **Change:** Emit step-level timing and cache-status summaries before changing CI behavior.
   - **Risk:** None; latency savings cannot yet be estimated safely.

## Cost Optimizations

1. **Eliminate reviewer retry/failback waste**
   - **Evidence:** Five detailed runs used 71 calls against a 60-call baseline; total window usage was **27.08M tokens**.
   - **Change:** Enable circuit breaking and emit per-attempt duration/token data.
   - **Estimated savings:** **8–15% of review calls**, potentially **2–4M total-token equivalents/window**.
   - **Quality risk:** Low because failed slots are replaced rather than removed.

2. **Make second-pass review selective**
   - **Evidence:** Six reviewers generally run twice. `REVIEWER_RISK_TIER_ENABLED=0`; risk logs also report `loc=0 files=0` despite gates observing 3–14 changed files.
   - **Change:** First fix LOC/file telemetry, then canary a three-model second pass for non-critical paths. Preserve six models for workflows, scripts, migrations, and memory code.
   - **Estimated savings:** Up to **25% of panel calls**, approximately **6–7M tokens/window** at current volume.
   - **Quality risk:** Medium; require disagreement and defect-escape monitoring.

3. **Fix model-specific cache effectiveness**
   - **Evidence from five detailed runs:**

     | Model | Calls | Derived cache hit |
     |---|---:|---:|
     | DeepSeek v4 Pro | 11 | 85.9% |
     | Kimi k3 | 15 | 68.7% |
     | Minimax m3 | 12 | 69.2% |
     | Qwen 3.7 Plus | 10 | 62.9% |
     | Grok 4.6 | 10 | 36.9% |
     | Mistral Small | 10 | 0.65% |

   - **Change:** Keep immutable policy/system text before dynamic PR data; emit an observed cache result after usage rather than `status=supported`.
   - **Estimated savings:** Potentially reclassifies hundreds of thousands of Mistral/Grok input tokens to discounted cache reads.
   - **Quality risk:** None. Dollar savings are unavailable because pricing telemetry was not collected.

4. **Semble appears cost-positive**
   - **Evidence:** 10 queries, **117,716 bytes**, zero fallbacks. Eight detailed calls were targeted reviewer, overflow-file, or conflict-resolver retrievals.
   - **Assessment:** Context volume—**11.8KB/query average**—is small relative to approximately 100K-token review prompts.
   - **Change:** Add `source_bytes_considered` and `bytes_avoided` to quantify prompt reduction. Avoid eager poller setup when no query occurs.

5. **Reasoning selection**
   - Reviewer reasoning defaults to `xhigh`; second-pass small diffs already use `high`. No observed judge execution supports changing judge reasoning.
   - Do not lower reasoning for critical workflow/script changes until risk-tier telemetry is corrected.

## Reliability Improvements

1. **Security audit path failure**
   - **Evidence:** Run `33301196549` failed in `Run security audit` with `captured_path_error=Error: No such file or directory`, but did not identify the missing path.
   - **Root cause category:** Configuration/path resolution.
   - **Fix:** Log sanitized stderr head/tail, executable resolution, all file paths passed to Codex, and path-valued `config.toml` entries. Emit:
     `SECURITY_AUDIT_FAILURE phase=codex-execution rc=... stderr_bytes=... missing_path=...`
   - **Impact:** Converts an opaque deterministic failure into an actionable one.
   - **Rollback/fail-open:** Logging-only; retain fail-closed audit behavior.

2. **Reviewer provider instability**
   - **Evidence:** `stall_guard` occurred in all five detailed active reviews; one DeepSeek `server_error`; three failback states.
   - **Fix:** Enable circuit breaking and retain fallback slots.
   - **Impact:** Lower timeout and rerun risk without reducing reviewer count.
   - **Rollback:** Environment flag.

3. **Unattributed poller API failure**
   - **Evidence:** Run `33325127600` emitted `gh: Not Found (HTTP 404)` while processing `#3898`, but no endpoint or operation.
   - **Fix:** Log sanitized method, endpoint class, attempt, status, elapsed time, and fail-open decision.
   - **Impact:** Separates expected optional-probe 404s from broken state lookups.

4. **Collector consistency**
   - **Evidence:** Supplied aggregate reports 115 telemetry runs with wall-clock p50 1,000ms; on-disk `summary.json` reports 30 with p50 41,000ms.
   - **Fix:** Emit collector source counts, dedup key counts, selected/deep-dive counts, and schema version at every aggregation stage.
   - **Impact:** Prevents contradictory SLO conclusions.

5. **Policy-pressure signals**
   - `BREAK_GLASS`: **0**
   - `CONTEXT_BUDGET_WARN`: **0**
   - No evidence of rubric pressure or context-window risk in the collected window.

## AI Memory Health

- **Retrievals:** 8/8 selected records—**100% hit rate**.
- **Budget:** Average **1,371/1,400 tokens (97.9%)**; all used `keyword_method=llm`.
- **Failures:** Zero zero-record, fail-open, or disabled retrievals.
- **Writes:** 18 deduplicated run events, 5 record candidates, and 6 `write_lessons_learned` operations; all lesson writes reported `count=0`.
- **Contention:** Four write operations required more than one push attempt; maximum was 3.
- **Assessment:** Availability is healthy, but retrieval is consistently saturated near the cap and relevance cannot be evaluated.
- **Recommendation:** Reuse a single memory clone per workflow and add `elapsed_ms`, clone/fetch/push timing, score distribution, records considered, and truncation count. No `promote`, `compact`, or `finalize-task` telemetry was observed.

## GH API Call Audit

Runtime API call counts were not emitted, so exact high-volume totals are **not auditable**.

- **Terminal project redundancy:** Issue `#3845` required comments and label reads before being skipped as complete. Remove it from active discovery.
- **Recurring sweep overhead:** Run `33325127600` spent approximately 10.2s on stall, merged-issue, conflict, and noop sweeps. Logs show zero merged issues and one PR scanned. Running these sweeps every third poll would reduce their API reads by roughly **67%**, with at most a 10-minute recovery delay.
- **Review sweep hygiene:** Runs `33322643747`, `33324021573`, and `33325437231` each found one candidate, recognized PR `#3915` had an active run, and dispatched zero—healthy deduplication.
- **404 observability:** The poller’s 404 lacked endpoint context.
- **Rate limits/retries:** No explicit runtime summaries were available.

Recommended wrapper output:

```text
GH_API_CALL method=GET target=issue-comments attempt=1 status=200 elapsed_ms=412 cached=false
GH_API_CALL method=GET target=workflow-probe attempt=1 status=404 elapsed_ms=190 fail_open=true
GH_API_SUMMARY calls=... retries=... cache_hits=... rate_limit_wait_ms=...
```

## Prompt Cache & Memory System

- Aggregate `cache_hit_rate` is null, but token counters imply **66.2%** cache reuse: 17.65M cached versus 9.00M uncached prompt tokens.
- Three explicitly reported run rates were **62.6%, 66.0%, and 69.4%**.
- Cache writes were reported as zero while reads were substantial; this likely reflects provider accounting rather than proof that no cache was created.
- `REVIEWER_CACHE status=supported prompt_reused=true` does not correspond to observed hits, especially for Mistral.
- Zero context-budget warnings indicate prompt growth did not cross the configured 70% threshold.
- Validate run `33321954636` hit its setup-uv cache but warned that configured save paths did not exist.

**Changes:**
1. Emit observed per-call cache ratios after usage.
2. Stabilize prompt prefixes and place PR SHA, timestamps, and diff data after cacheable policy text.
3. Add cache-key component hashes to diagnose fragmentation.
4. Guard dependency-cache save steps on directory existence.

## Orchestrator Health

- `orchestrate_poll`: 76 runs, 70 successes, 6 cancellations; p50 **279.5s**, p95 **525.75s** against a 300s cron.
- Run `33325127600` behaved safely: it deferred PR `#3915` because one required check was blocking and explicitly left retry budget unchanged.
- The same run found no conflicts or noop-suspicious recoveries, indicating clean fail-open sweep behavior.
- A hidden **56s pre-issue phase** occurred between process start and the first issue banner; likely API snapshot/setup work, but no stage timing exists.
- Semble availability changed from initialization defaults (`false`) to usable index state (`true`), while the supplied summary classified it as unavailable. Emit a final probe rather than interpreting early environment defaults.
- No clarification loops, judge cycles, conflict-heal retries, or terminalization loops were observed in deep logs.

Track:

```text
ORCH_STAGE_TIMING stage=pre_issue_snapshot elapsed_ms=... api_calls=...
ORCH_ISSUE_RESULT issue=3898 state=ready_to_merge action=defer reason=blocking_check elapsed_ms=...
SEMBLE_PROBE target=index result=ok elapsed_ms=...
```

## Pipeline Flow Bottlenecks

| Stage | Evidence | Bottleneck type |
|---|---|---|
| Clarify | 190/193 skipped | Trigger/control-plane noise |
| Plan | 186/190 skipped; successful outliers 371–464s | Compute when active |
| Implement | 186/189 skipped; successful outliers 658–799s | Model/compute |
| Review/autofix | Active detailed average 6,393s | Model stalls, retries, two-pass panel |
| CI | p50 1,223s, p95 1,292.5s | Unknown compute; logs missing |
| Validate | Run `33321954636`: 397s | Validation plus cache warnings |
| Orchestrate poll | p50 279.5s on 300s cadence | Fixed setup, API polling, memory writes |
| Promote/forward merge | 44s / 38s | Minor |

The four phase families generated **751 skipped runs out of 762 (98.6%)**. These are mostly one-second records, so they are a control-plane and observability issue rather than the primary compute cost. Add event and skip-reason fields to the collector before narrowing triggers.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** 100+ minute active review runs; 20-minute CI; near-continuous 5-minute poller.
- **Top failure modes:** Reviewer stall guards, duplicate queued review runs, opaque security-audit path failure, unattributed poller 404.
- **Highest cost driver:** Review/autofix—84 OpenRouter calls and 27.08M tokens.
- **Top actions:**
  1. Enable reviewer circuit breaking and per-attempt timing.
  2. Reuse memory checkout, scope poller fetches, and lazily initialize Semble.
  3. Deduplicate review dispatches by PR and exact head SHA.

## Metrics Appendix

### Overall

| Metric | Value |
|---|---:|
| Window | 2026-08-30 05:54–17:34 UTC |
| Total runs | 1,000 |
| Success | 236 |
| Failure | 1 |
| Cancelled | 12 |
| Skipped | 751 |
| Strict success rate | 23.6% |
| Non-skipped success rate | 94.8% |
| p50 / p95 duration | 2s / 359s |
| Average duration | 169.3s |

### Major workflow families

| Family | Runs | Success | Failure | Cancelled | Skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Review/autofix | 73 | 67 | 0 | 6 | 0 | 18s | 6,110.6s |
| Orchestrate poll | 76 | 70 | 0 | 6 | 0 | 279.5s | 525.75s |
| CI | 11 | 11 | 0 | 0 | 0 | 1,223s | 1,292.5s |
| Clarify | 193 | 3 | 0 | 0 | 190 | 1s | 11s |
| Plan | 190 | 4 | 0 | 0 | 186 | 1s | 10s |
| Implement | 189 | 3 | 0 | 0 | 186 | 1s | 10.6s |
| Security audit | 1 | 0 | 1 | 0 | 0 | 86s | 86s |
| Validate | 1 | 1 | 0 | 0 | 0 | 397s | 397s |

### AI and cache telemetry

| Metric | Value |
|---|---:|
| OpenRouter calls | 84 |
| Usage available/unavailable | 79 / 5 |
| Prompt tokens | 9,001,455 |
| Completion tokens | 429,097 |
| Cache-read tokens | 17,651,549 |
| Cache-write tokens | 0 |
| Total tokens | 27,075,004 |
| Derived cache hit rate | 66.2% |
| Collector `cache_hit_rate` | null |
| Break-glass count | 0 |
| Context-budget warnings | 0 |

### Wall-clock telemetry discrepancy

| Source | Samples | p50 | p99 |
|---|---:|---:|---:|
| On-disk `summary.json` | 30 | 41,000ms | 7,538,360ms |
| Supplied aggregate context | 115 | 1,000ms | 6,296,760ms |

### MCP telemetry

| System/target | Queries | Bytes | Fallbacks | Probe OK | Failed | Skipped |
|---|---:|---:|---:|---:|---:|---:|
| Semble, all targets | 10 | 117,716 | 0 | — | — | — |
| Serena, target not emitted | 0 | 0 | 0 | 0 | 0 | 0 |

Detailed Semble target coverage was available for 8/10 calls: five reviewer-context, two overflow-file, and one conflict-resolver query. No other MCP servers were observed. Serena probe coverage is insufficient to distinguish disabled from unavailable outside the review workflow.
