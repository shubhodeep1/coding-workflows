## Executive Summary

- **Terminal poll loop is the largest avoidable latency source — high confidence.** Nine consecutive poller runs, including `33998420918`, `34003454596`, and `34005163391`, spent 206–265s rediscovering tracker `#3965` before logging `Project already failed, skipping`. Early terminal-state classification should save **150–210s per affected tick**.
- **Review/autofix dominates AI cost and tail latency — high confidence.** It consumed **55.23M OpenRouter tokens across 93 calls** and has a **6,306s p95**. Second-pass calls account for **29.53M tokens (53.5%)**.
- **Memory bookkeeping is unexpectedly expensive — high confidence.** Poller memory start/end writes each take **30–41s** because every operation clones the repository afresh. A memory-branch-only fetch or batched write should save **50–80s per poll**.
- **Reviewer stalls are systemic — high confidence.** Twenty-one backoff events concentrated on `moonshotai/kimi-k3` and `minimax/minimax-m3`; 17/93 calls lacked usage data. Carrying slot health across passes should remove **15–45 minutes** from affected reviews.
- **CI failures are poorly diagnosable — high confidence.** Runs `33962767376`, `33962791612`, and `33963148976` all failed after 398–489s with only an `AssertionError` from prompt byte parity. Earlier execution plus byte-diff logging would save **6–8 minutes per regression**.
- **Overall completion reliability is good but obscured by expected skips/cancellations — high confidence.** There were 318 successes, 5 failures, 20 cancellations, and 202 skipped/other outcomes. No `BREAK_GLASS` events occurred; 10 context-budget warnings did.

## Speed Optimizations

| Rank | Finding and evidence | Root cause | Exact change | Estimated saving | Risk |
|---:|---|---|---|---:|---|
| 1 | **Critical path:** Nine recent pollers processed terminal tracker `#3965`; poller p50 is 251s and p95 371.5s, exceeding its 300s schedule interval. | Terminal status is read only after checkout, setup, global prefetches, memory write, and Semble installation. | Move the existing state-comment read before heavy setup. Emit `ORCH_POLL_CLASSIFY active=N actionable=N terminal=N`; gate checkout, memory and Semble on `actionable>0`, failing open to the existing path. | 150–210s/tick | Low–medium |
| 2 | **Critical path:** Memory start/end steps took 64–82s combined in runs `34003454596`, `34004452327`, `34005163391`. | `_clone_for_memory_branch()` performs a full clone before fetching `ai-memory`, once per operation. | Replace with `git init` plus a depth-1 fetch of only `refs/heads/ai-memory`; optionally batch start/end events into one final transaction. Retain the current clone path as fail-open fallback. | 50–80s/tick | Low |
| 3 | **Critical path:** 21 reviewer backoffs and up to four stall kills per run; `33942849047` lasted 8,754s. | Unhealthy reviewer slots are retried independently in each pass. | Persist a run-local slot circuit breaker. After the first stall/server error, skip retries of that primary in later passes and go directly to its configured fallback. | 15–45 min on affected reviews | Low–medium |
| 4 | **Feedback path:** Three identical CI prompt-parity failures appeared only after 398–489s. | `test_assemble_prompt.py` runs late in a long serial job. | Run prompt parity immediately after syntax/lint checks, before broad contract tests. | 6–8 min on these failures | Low |
| 5 | **Actionable poll path:** Checkout fetched **2,240 branches and 200 tags**, taking 26–37s in three sampled polls. | `fetch-depth: 0` fetches every repository ref. | After validating ref requirements, use a shallow/filtered checkout and explicitly fetch only active integration/PR refs. First pilot `filter: blob:none`. | 20–30s/tick | Medium |
| 6 | **Micro-optimization:** Semble installation took 7.8–9.3s despite no judge invocation. | Setup is eager whenever any tracking issue exists. | Install/build Semble lazily at the first judge or targeted-context request. | 8–10s/tick | Low |

## Cost Optimizations

1. **Adaptively gate the second reviewer pass.**  
   - **Evidence:** 45 second-pass calls consumed **29.53M tokens**, versus 48 pass1 calls consuming 25.70M. Run `33970549395` spent **6.64M tokens and 6,015s** before concluding `clean_review_no_commit`.
   - **Change:** Require a second full panel only for high-risk files, unresolved major findings, low consensus, or large diffs. Otherwise run one targeted verifier.
   - **Savings:** Skipping 25–50% of second passes would save approximately **7.4–14.8M tokens**, or **13–27%** of aggregate OpenRouter usage.
   - **Quality risk:** Medium; retain full panels for workflow, security, migration, and conflict changes.

2. **Circuit-break unhealthy models before repeated attempts.**  
   - `kimi-k3` and `minimax-m3` each had eight usage-unavailable calls. Immediate fallback preserves model diversity while avoiding long dead periods.
   - Token savings are unquantifiable because failed calls lacked usage data; latency and potentially billed failed-request savings are material.

3. **Enforce model-specific prompt budgets before fan-out.**  
   - Ten warnings occurred across runs `33940574300`, `33946546308`, and `33956395737`; one prompt had **200,579 tokens against a 200,000-token window**.
   - Trim or summarize dynamic comments, logs and duplicated context until prompts remain below 70–80% of each model’s context window.
   - Estimated saving: **1.7–3.4M prompt tokens** if aggregate prompt volume drops 10–20%. Quality risk is medium.

4. **Keep Semble enabled, but measure its reduction ratio.**  
   - Semble produced 20 successful queries and 314,867 logged bytes. Bounded 6/12-chunk outputs suggest it is reducing unbounded expansion rather than adding broad noise.
   - Three runtime fallbacks were `reason=budget-exhausted`; these are healthy bounded fail-open events, not evidence of a broken rollout.
   - Add `source_bytes`, `selected_bytes`, `budget_before`, and `budget_after` fields. Do not disable Semble without evidence of negative reduction.

5. **Do not prioritize memory-token reduction.**  
   - Retrieval used 1,399/1,400 tokens on average, but this is below 1% of the 185k–201k warning prompts. Optimize its git persistence latency first.

Dollar savings cannot be calculated because model pricing was not included.

## Reliability Improvements

1. **Make prompt-parity failures self-diagnosing.**
   - **Failure evidence:** CI runs `33962767376`, `33962791612`, `33963148976`.
   - **Root cause category:** Template/legacy prompt drift.
   - **Fix:** Replace the bare equality assertion with prompt name, expected/actual byte lengths, SHA-256 values, first differing byte/line, and a bounded unified diff.
   - **Impact:** Prevents repeated blind reruns and reduces diagnosis from minutes to one log inspection.
   - **Rollback:** Diagnostics are additive.

2. **Reject or reroute over-window reviewer prompts.**
   - Add `PROMPT_MODEL_FIT model=... tokens=... window=... action=run|trim|fallback`.
   - If trimming fails, skip only the incompatible slot or use its larger-window fallback; never fail the entire review panel.
   - Expected impact: eliminate context-overflow failures and reduce stall probability.

3. **Classify concurrency cancellations.**
   - Review/autofix had **17 cancellations**. Runs `33940565611` and `33949206491` were cancelled before the child `codex-agent` began after 7,999s and 5,917s.
   - Emit `AUTOFIX_QUEUE_STATE` with PR, head SHA, trigger time, child-start time and superseding run ID. Enhance the collector to label probable concurrency supersession instead of generic cancellation.
   - Fail open: classification must not affect execution.

4. **Preserve Semble fail-open behavior but improve its diagnostics.**
   - Runtime fallback rate was 3 events alongside 20 successful queries; all three were budget exhaustion.
   - Log requested file bytes, remaining global budget and whether marker/read fallback was selected.

5. **Keep the release ref guard; improve operator telemetry.**
   - Run `34005801483` correctly rejected dispatch from `main` rather than `stable` in 18s.
   - Add a structured `RELEASE_DISPATCH_REJECT reason=wrong_ref expected=stable actual=main` line and surface it in the job summary.

## AI Memory Health

- **Retrieval hit rate:** 8/8 sampled retrieves selected records: **100%**.
- **Average size:** **1,399 estimated tokens** against a 1,400-token budget; 30–31 records per retrieval.
- **Keyword methods:** `llm=8`, `plain=0`, `none=0`.
- **Failures:** No zero-record retrieves, `fail_open:true`, or `enabled:false` events were observed.
- **Push health:** Two review phase-start events required two push attempts; recent poller start/end events otherwise succeeded in one attempt.
- **Lifecycle gap:** No `promote`, `compact`, or `finalize-task` telemetry appeared in the sampled logs. `record-candidate` worked, while sampled lessons-learned writes commonly reported `count=0`.

**Recommendation:** Add clone/fetch/push duration fields and retrieval score distributions. A 100% hit rate at 99.9% budget utilization may represent useful recall, but currently cannot distinguish relevance from saturation.

## GH API Call Audit

Exact API-call counts are unavailable because `gh_retry` does not emit structured per-call telemetry.

| Pattern | Evidence | Assessment and change |
|---|---|---|
| Poller tracker discovery | One `gh issue list` per poll by workflow design; 123 poller runs in the window. | Extend this existing call to include labels rather than adding a new lookup. Move terminal-state preflight ahead of heavy setup. |
| Opaque poll prefetch | Run `34005163391` spent ~55s inside `Process each tracking issue` before its first issue log line. | Instrument every `gh_retry` call with endpoint class, method, duration, attempts, result and rate-limit remaining. Emit one `GH_API_SUMMARY` per step. |
| Idle review sweep | Runs `33998843290`, `34000188705`, `34003137051`, `34004473823`, and `34005355349` all found zero candidates. | Healthy: the zero-candidate fast exit avoids the two workflow-run status snapshots. Preserve it. |
| Review PR metadata | PR state must be refreshed after long concurrency waits. | Reuse immutable title/body/files where safe, but retain a live state/head-SHA read immediately before reviewer spend. |

No production HTTP 429 or rate-limit event was observed. `DRIFT_SCAN_RETRY` messages in CI were test fixtures and should not be counted as runtime incidents.

Recommended structured marker:

`GH_API_CALL caller=... endpoint_class=... method=... attempt=... duration_ms=... result=ok|error cache=hit|miss rate_remaining=...`

## Prompt Cache & Memory System

- Official `cache_hit_rate` is unavailable despite cache-read telemetry.
- A derived proxy, `cache_read / (prompt + cache_read)`, is **68.5%**.
- Usage coverage was **76/93 calls**; 17 calls lacked token data.
- Cache reads totaled **37.44M tokens**, while cache writes reported zero. Zero writes may reflect provider reporting semantics rather than no cache creation.
- Cache use is uneven: Kimi, DeepSeek, and Grok account for most read tokens, while Mistral and several Qwen calls showed little or no reuse.

**Inference:** Six model prefixes, two reviewer passes, and dynamic memory/Semble/run data likely fragment reusable prefixes.

**Changes:**
1. Put stable rubric/persona text first and dynamic PR metadata last.
2. Log a non-sensitive `static_prefix_hash`, static-token count and dynamic-token count per call.
3. Compute `cache_hit_rate` only across usage-available calls and publish coverage beside it.
4. Trigger context compaction before `CONTEXT_BUDGET_WARN`, not after a provider rejects the prompt.

Expected impact: **5–15% prompt-token and latency reduction**, with low risk if prompt content remains unchanged.

## Orchestrator Health

- **Terminal-state churn:** Nine recent runs repeatedly treated failed tracker `#3965` as active.
- **Cadence pressure:** Poller p95 is **371.5s**, longer than its five-minute schedule. `cancel-in-progress:false` therefore permits pending-run replacement; three poller runs were cancelled.
- **Queueing:** Run `33988567699` waited approximately 19 minutes for a hosted runner; run `33987631244` lasted 2,295s.
- **Wave/judge health:** Recent terminal-tracker polls invoked no judge. No conflict-heal retry or clarification-loop telemetry was available for this window.
- **State hygiene:** The poller detects `failed` only after loading and parsing the orchestrator state comment.

Track these indicators:

- `scheduled_to_runner_ms`
- `runner_to_actionable_classification_ms`
- actionable/terminal tracker counts
- poll overlap and pending replacement count
- per-issue state transitions
- judge invocations and repeat-fingerprint count
- conflict-heal attempts and outcomes

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant bottleneck | Priority fix |
|---|---|---|---|
| Clarify | 52 runs; 51 skipped; p50 1s | Trigger noise, not compute | Add collector-side skip classification. |
| Plan | 51 runs; 50 skipped; one 440s run | Active-run evidence unavailable | Collect phase/model timings for the active run. |
| Implement | 51 runs; 50 skipped; one 2,917s run | Deep-dive log absent | Ensure longest active implement run is always selected. |
| Review/autofix | p95 6,306s; 93 OR calls; 21 backoffs | Model compute and retries | Adaptive pass2 and slot circuit breaker. |
| Validate/CI | CI p50 1,379s; three late parity failures | Serial test ordering | Move high-signal contracts earlier and add diffs. |
| Orchestrate | p50 251s; terminal tracker repeated | Setup, git fetch, memory writes, opaque prefetch | Early actionability gate and memory-branch-only fetch. |
| Merge/conflict | No measured conflict-heal events | Data gap | Emit conflict attempt/result/duration markers. |

Queueing, compute and retry overhead are evidenced separately; merge/conflict overhead cannot be quantified from this window.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Review/autofix p95 of 6,306s and 55.23M OpenRouter tokens.
- Poller p50 of 251s despite repeatedly finding only terminal tracker `#3965`.
- CI p50 of 1,379s with critical prompt-parity checks late in the job.

**Top failure modes**
- Prompt template byte drift: runs `33962767376`, `33962791612`, `33963148976`.
- Mixed whitespace in `scripts/targeted_file_context.py`: run `33988647787`.
- Incorrect release workflow ref: run `34005801483`.
- Concurrency supersession: 17 review cancellations.

**Highest cost drivers**
- Second reviewer pass: 29.53M tokens.
- Kimi/Minimax stalls and fallback attempts.
- Prompts near or beyond model context limits.

**Top three actions**
1. Move terminal-state classification before poller checkout/setup and log actionable counts.
2. Replace full memory clones with depth-1 memory-branch fetches; batch poll events.
3. Add adaptive second-pass gating, persistent reviewer slot health, and prompt/model-fit telemetry.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| All | 545 | 318 | 5 | 20 | 202 | 9s | 3,350s |
| CI | 22 | 18 | 4 | 0 | 0 | 1,379s | 1,479.5s |
| Review/autofix | 112 | 95 | 0 | 17 | 0 | 10s | 6,306.4s |
| Orchestrate poll | 123 | 120 | 0 | 3 | 0 | 251s | 371.5s |
| Copilot reviewer | 29 | 29 | 0 | 0 | 0 | 225s | 326.6s |

### Cost and review telemetry

| Metric | Value |
|---|---:|
| Codex calls / tokens | 1 / 2,026 |
| OpenRouter calls | 93 |
| Prompt / completion / total tokens | 17,187,740 / 615,373 / 55,233,833 |
| Cache read / write tokens | 37,437,177 / 0 |
| Usage available / unavailable | 76 / 17 |
| Official cache hit rate | unavailable |
| Derived cache-read share | 68.5% |
| `BREAK_GLASS` | 0 |
| `CONTEXT_BUDGET_WARN` | 10 |
| `wall_clock_p50_ms` / `p99_ms` | 9,000 / 8,017,780 |
| Wall-clock samples | 112 |

### MCP telemetry

| Server/target | Queries | Logged bytes | Runtime fallbacks | Contract fallbacks | probe_ok | probe_failed | probe_skipped |
|---|---:|---:|---:|---:|---:|---:|---:|
| Semble `reviewer-context` | 6 | 150,457 | 0 | 0 | 0 | 0 | 0 |
| Semble `overflow` | 12 | 143,709 | 3 | 15 | 0 | 0 | 0 |
| Semble target unavailable in summary-only evidence | 2 | 20,701 | 0 | 0 | 0 | 0 | 0 |
| **Semble total** | **20** | **314,867** | **3** | **15** | **0** | **0** | **0** |
| Serena, no target observed | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

- Serena per-tool breakdown: none observed.
- Other MCP servers observed: none.
- Runtime Semble fallback rate: **3/23 attempts, 13.0%**.
- Serena’s all-zero probes make disabled versus missing emission indistinguishable; emit `SERENA_PROBE result=skipped reason=disabled` when appropriate.

### AI memory sample

| Metric | Value |
|---|---:|
| Retrieves | 8 |
| Retrieves selecting records | 8 (100%) |
| Average estimated tokens / budget | 1,399 / 1,400 |
| Records selected | 30–31 |
| Keyword method | `llm`: 8 |
| Zero-record / fail-open / disabled retrieves | 0 / 0 / 0 |
| Start events requiring >1 push attempt | 2 |
| Recent poll event writes | 18/18 successful, one attempt |

## Deep Audit — Workflows & Scripts (2026-09-06)

### Section 1: Bug & Correctness Sweep

#### SEC-001 — Updater sources a helper from the caller owner’s repository

- **ID:** `SEC-001`
- **File path and line range:** `.github/workflows/update_workflows.yml:629-645`
- **Severity:** High
- **Category tag:** `security`
- **Description:** The workflow fetches and sources `tg_helpers.sh` from `${{ github.repository_owner }}/coding-workflows@stable`, while its canonical template source is explicitly `shubhodeep1/coding-workflows`. In a consumer organization, a sibling repository named `coding-workflows` can therefore supply shell code executed with `GH_PAT` present.
- **Recommended fix:** Reuse `scripts/tg_helpers.sh` from the already-fetched canonical upstream checkout at `steps.fetch.outputs.upstream_dir`, pinned to `steps.fetch.outputs.upstream_sha`. Remove the second `gh api` fetch entirely.

#### SEC-002 — Attachment downloader permits SSRF through untrusted issue URLs

- **ID:** `SEC-002`
- **File path and line range:** `scripts/issue_attachment_bundle.py:129-174`
- **Severity:** Medium
- **Category tag:** `security`
- **Description:** `_download()` accepts both HTTP and HTTPS, performs no private, loopback, link-local, or metadata-address validation, and lets `urllib` follow redirects automatically. The CLI feeds it URLs extracted from issue-body text. No current workflow invocation was found, limiting present reachability. [NEEDS VERIFICATION]
- **Recommended fix:** Require HTTPS, reject user-info URLs, resolve and reject every non-public address, and install a redirect handler that validates each `Location` before following it. Add SSRF tests covering `127.0.0.1`, `::1`, RFC1918, link-local, and public-to-private redirects.

#### BUG-001 — Forward pagination skips comments after deletion

- **ID:** `BUG-001`
- **File path and line range:** `scripts/tg_helpers.sh:330-374,399-445`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** Both cleanup functions fetch page N, delete matching comments from that issue, then increment to page N+1. Deletions shift later comments toward earlier pages, so comments originally on page 2 can move to page 1 and never be visited.
- **Recommended fix:** Fetch and snapshot all matching comment IDs before deleting anything, then delete from the snapshot. Alternatively, repeatedly fetch page 1 until no matching markers remain.

#### BUG-002 — Telegram marker updates can lose message IDs

- **ID:** `BUG-002`
- **File path and line range:** `scripts/tg_helpers.sh:155-206,227-277`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** `tg_store_msg_id()` and `tg_store_phase_msg_id()` use an unlocked GET–modify–PATCH sequence. Inference: concurrent phase workflows can read the same old body and overwrite each other, permanently dropping one message ID from later cleanup.
- **Recommended fix:** Use one append-only marker comment per message ID. If retaining aggregation, re-read and verify after PATCH, then merge and retry with a bounded backoff when the submitted ID is absent.

#### BUG-003 — Invalid alert levels are silently classified as DEBUG

- **ID:** `BUG-003`
- **File path and line range:** `.github/workflows/review_autofix.yml:6391-6430`; `scripts/orchestrate_poll_process.sh:19453`; `scripts/tg_helpers.sh:53-74`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** Callers pass `WARN` and `INFO`, but the helper only recognizes `DEBUG`, `WARNING`, `ERROR`, `CRITICAL`, and `SILENT`. Unknown values receive numeric level 0, so these alerts disappear when `ALERT_MSG_LEVEL` is `WARNING` or higher and receive an incorrect critical icon otherwise.
- **Recommended fix:** Replace `WARN` with `WARNING` and classify the `INFO` call explicitly as `DEBUG` or `WARNING`. Also normalize `WARN` as an alias or reject unknown levels with a warning.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Final PR metadata is fetched twice

- **ID:** `API-001`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:8361-8373`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** When `final_pr_json_snapshot` is unavailable, the same `/pulls/{final_pr}` endpoint is called separately for `.state` and `.merged_at`. Current count: 2 calls. Proposed count: 1 call.
- **Recommended fix:** Fetch one PR object and derive both fields locally. Extend the poller’s existing `_fetch_pr_json`/cycle-local snapshot pattern.

#### API-002 — Reissue path fetches one issue separately for title and body

- **ID:** `API-002`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:11211-11213`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** Two consecutive `/issues/{issue_num}` calls extract title and body independently. Current count: 2 calls. Proposed count: 1 call.
- **Recommended fix:** Fetch `{title,body}` once and parse both values locally, following the poller’s existing cached JSON-object pattern.

#### API-003 — Review sweep uses six fixed active-run snapshots

- **ID:** `API-003`
- **File path and line range:** `.github/workflows/review_autofix_sweep.yml:123-224`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** Each non-empty sweep queries three statuses for each of two workflow files. Current count: 6 logical paginated calls per tick. Proposed count: 1 repository-wide paginated run snapshot, filtered locally by workflow path, status, and head branch. [NEEDS VERIFICATION]
- **Recommended fix:** Port the `_load_actions_runs_cached` repository-snapshot pattern from `orchestrate_poll_process.sh`. Validate page-boundary behavior before removing the workflow-scoped fallback.

#### API-004 — Merge-conflict path re-reads the default branch

- **ID:** `API-004`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:14893-14916`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** When `DEFAULT_BRANCH_TRACKING` starts empty, line 14894 populates it, but the immediately following merge-conflict branch fetches the same repository field again. Current count: up to 2 calls. Proposed count: 1 call.
- **Recommended fix:** Set `FINAL_DEFAULT_BRANCH="${DEFAULT_BRANCH_TRACKING}"` and retain the existing fallback only when the cached value is empty.

#### BATCH-001 — Tracking comments are fetched once per tracking issue

- **ID:** `BATCH-001`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:14443-14470`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The main tracking loop performs one paginated comments request for every tracking issue. Current count: N logical calls, potentially multiple underlying pages each. Proposed common-path count: `ceil(N/10)` aliased GraphQL calls, with REST fallback for nodes whose comment connection reports additional pages. [NEEDS VERIFICATION]
- **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql` with latest-comment nodes and pagination metadata. Preserve the existing REST path for oversized histories and incomplete GraphQL responses.

#### BATCH-002 — Commit attribution is fetched per advancing commit

- **ID:** `BATCH-002`
- **File path and line range:** `scripts/check_external_branch_advance.sh:163-202`
- **Severity:** Low
- **Category tag:** `api-batching`
- **Description:** Each self-subject commit triggers an individual `/commits/{sha}` request. Current count: K calls. Proposed count: `ceil(K/50)` GraphQL alias batches. [NEEDS VERIFICATION]
- **Recommended fix:** Extend the aliased-query pattern used by `_fetch_candidate_issue_details_graphql`, requesting commit author and committer user logins. Retain REST fallback if GraphQL attribution differs.

#### BATCH-003 — Merged and ready issue discovery uses two list calls

- **ID:** `BATCH-003`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:3437-3467`
- **Severity:** Low
- **Category tag:** `api-batching`
- **Description:** `close_merged_issues_sweep()` independently lists `ai:merged` and `ai:ready-to-merge` issues. Current count: 2 calls. Proposed count: 1 aliased GraphQL query. [NEEDS VERIFICATION]
- **Recommended fix:** Query both label searches as GraphQL aliases and preserve the current deduplication/origin policy. Follow the poller’s documented batched-query and fail-open REST pattern.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Integration-ref bootstrap is copied across five workflows

- **ID:** `DUP-001`
- **File path and line range:** `.github/workflows/clarify.yml:70-129`; `.github/workflows/plan.yml:133-195`; `.github/workflows/implement.yml:332-391`; `.github/workflows/validate.yml:103-162`; `.github/workflows/orchestrate_clarify_respond.yml:124-183`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** The authenticated clone, fallback checkout, log redaction, cleanup trap, and resolver invocation are nearly identical in five workflows.
- **Recommended fix:** Add `scripts/resolve_integration_ref_bootstrap.sh` with signature `resolve_integration_ref_bootstrap <repo> <issue> <source-repo> <source-ref> <github-output>`. Reduce each workflow to fetching the immutable bootstrap and invoking it.

#### DUP-002 — Prompt-budget warning helpers are triplicated

- **ID:** `DUP-002`
- **File path and line range:** `scripts/review_run_reviewers.sh:17-33,69-106`; `scripts/review_apply_fixes.sh:32-48,164-201`; `scripts/review_rb_judge.sh:247-330`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Prompt-budget lifecycle fallbacks and `emit_context_budget_warn_for_prompt()` are maintained independently across reviewer, editor, and judge paths.
- **Recommended fix:** Move them to `scripts/prompt_budget_helpers.sh`, exposing `prompt_budget_init <bytes>`, `prompt_budget_embed <path> <cap> <mode>`, `prompt_budget_cleanup`, and `emit_context_budget_warn_for_prompt <phase> <path> <model>`.

#### DUP-003 — Phase-label fallbacks duplicate the canonical helper

- **ID:** `DUP-003`
- **File path and line range:** `.github/workflows/issue_pr_status.yml:313-327`; `.github/workflows/review_autofix.yml:5041-5080,5225-5270,6603-6624`; `scripts/label_helpers.sh:128-222`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Multiple inline implementations recreate `ensure_label_exists()` and `set_issue_phase_label_resilient()`, with different colors, return behavior, and label-replacement semantics.
- **Recommended fix:** Make `scripts/label_helpers.sh` a required staged asset for these jobs and use its existing signatures: `ensure_label_exists <label> [repo]` and `set_issue_phase_label_resilient <issue> <label> <repo>`.

No workflow pair exceeded the requested greater-than-70% near-duplicate threshold; the highest measured pair was exactly 70%.

### Section 4: Expression Size Limit Risk Assessment

All 45 workflows parsed successfully. No expression-bearing `run:` block reached the 15,000-character Medium threshold, no `if:` expression approached the limit, and no workflow exceeded 800 KB.

| Workflow | Largest interpolated `run:` block | Headroom to 21,000 |
|---|---:|---:|
| `implement.yml` | 12,608 | 8,392 |
| `validate.yml` | 10,986 | 10,014 |
| `workflow-log-analysis.yml` | 10,884 | 10,116 |
| `orchestrate_poll.yml` | 9,745 | 11,255 |
| `plan.yml` | 9,402 | 11,598 |
| `review_autofix.yml` | 9,203 | 11,797 |
| `test-and-mark-stable.yml` | 8,473 | 12,527 |

Largest workflow: `review_autofix.yml`, approximately 453,925 characters, leaving approximately 594,651 characters before the 1 MB limit. No `EXPR-*` finding is warranted at current sizes.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Documented review thread reuse is intentionally bypassed

- **ID:** `CONSIST-001`
- **File path and line range:** `scripts/review_apply_fixes.sh:697-710,1599-1601`; `scripts/review_conflict_resolve.sh:131-173,1667-1669`; `README.md:120`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The documented `CODEX_THREAD_REUSE_ENABLED` contract includes `review_autofix`, but both OpenCode editor and conflict-resolver paths explicitly log that they always use fresh full prompts. Their thread-reuse helper functions are definition-only.
- **Recommended fix:** Either implement OpenCode session continuation for these paths or remove `review_autofix` from the documented supported phases and delete the inert helper scaffolding.

#### CONSIST-002 — GitHub mutations bypass `curl_gh_api`

- **ID:** `CONSIST-002`
- **File path and line range:** `scripts/tg_helpers.sh:20-29,169-205,241-277,364-368,435-439`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The script loads the reset-aware `curl_gh_api` helper but only uses it for reads. GitHub comment POST, PATCH, and DELETE operations use raw `curl ... || true`, bypassing rate-limit detection, reset-aware backoff, and structured errors.
- **Recommended fix:** Route every `api.github.com` operation through `curl_gh_api`; retain raw curl only for Telegram endpoints.

#### DEAD-001 — Standalone-state reader is unreachable

- **ID:** `DEAD-001`
- **File path and line range:** `scripts/orchestrate_poll_process.sh:10546-10553`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** Repository-wide search finds no caller of `read_standalone_state_json()`. The live loop parses its already-fetched comments payload directly.
- **Recommended fix:** Remove the function and its latent paginated API path, preserving `_extract_standalone_state_json_from_comments()` as the sole reader.

#### DEAD-002 — Reviewer compatibility functions have no callers

- **ID:** `DEAD-002`
- **File path and line range:** `scripts/review_run_reviewers.sh:664-692,3562-3583`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `is_mcp_incompatible_model()`, `strip_all_mcp_server_blocks()`, and `reviewer_patch_reasoning_config_file()` are definition-only after the OpenCode cutover; two are explicit no-ops.
- **Recommended fix:** Remove them and update tests/docs that pin their presence, or move genuine compatibility behavior into a versioned adapter that has an exercised caller.

#### SHELL-001 — Scalar/array identifier reuse fails shellcheck

- **ID:** `SHELL-001`
- **File path and line range:** `scripts/codex_thread_reuse.sh:490-494,806-856`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** `cmd` is an array in `codex_thread_reuse_run_once()` and a scalar command selector in `codex_thread_reuse_main()`. Shellcheck emits SC2178 and SC2128. Function scoping prevents a demonstrated runtime failure, but the collision obscures genuine array misuse.
- **Recommended fix:** Rename the scalar to `subcommand` and keep `cmd` reserved for command arrays.

#### DEBT-001 — Temporary checkout diagnostics remain always enabled

- **ID:** `DEBT-001`
- **File path and line range:** `.github/workflows/test-and-mark-stable.yml:273-282,3462-3465,3754-3757,5017-5020,5186-5189`
- **Severity:** Low
- **Category tag:** `tech-debt`
- **Description:** An `if: always()` diagnostic anchor is instantiated in five jobs, with comments explicitly saying to remove it after diagnosing checkout exit 128. It adds repeated Git commands to every release gate.
- **Recommended fix:** Link the diagnostic to a tracked failure condition or feature flag, then remove all five instances once the checkout fault is resolved.

No exact-word `TODO`, `FIXME`, `HACK`, or `XXX` markers were found.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 12 | BUG-001, BUG-002, BUG-003, SEC-002, API-001, API-003, BATCH-001, DUP-001, DUP-002, DUP-003, CONSIST-001, CONSIST-002 |
| Low | 8 | API-002, API-004, BATCH-002, BATCH-003, DEAD-001, DEAD-002, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Small |
| API call optimization | 3 | Large |
| Code modularization | 13 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 9 | Medium |
