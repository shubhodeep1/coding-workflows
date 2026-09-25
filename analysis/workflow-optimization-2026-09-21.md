## Executive Summary

- **Implement preflight failures wasted ~100 runner-minutes and 3.97M Codex tokens** across runs `35488007595`, `35494288283`, and `35499660092`; all failed only after model execution on `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. A pre-model support-ledger check would save **31–36 minutes and ~1.324M tokens per affected run**. **Impact: very high; confidence: high.**
- **Review/autofix is the dominant bottleneck:** p95 `8,145.6s` (136 minutes), with 55–108 memory-ledger failures per sampled slow run. Eight completed slow runs produced **666 synchronous substate-write failures**, commonly timing out after 120 seconds. **Estimated saving from local batching/circuit breaking: 10–30 minutes per long run; confidence: medium.**
- **Reviewer-provider degradation is systemic:** `mistralai/mistral-small-2603` failed in all eight structured slow-review samples; one request in run `35511425396` attempted ~269,522 tokens against a 262,144-token context. **Impact: 5–25 minutes and failed calls per affected review; confidence: high.**
- **CI regressions fail too late:** five CI runs (`35495539377`, `35498989364`, `35503818432`, `35508264536`, `35510947848`) spent 15–20 minutes before detecting the same stale budget assertion, `300 != 180`. **Impact: 15–20 minutes faster feedback per regression; confidence: high.**
- **Prompt caching is valuable but incompletely measured:** 168.33M cache-read tokens were 84.3% of 199.58M reported OpenRouter tokens, yet `cache_hit_rate` is null and 30.6% of calls lack usage. There were 18 context-budget warnings. **Impact from safer prompt pruning: estimated 10–20% prompt-token reduction; confidence: medium.**
- **GitHub API hygiene exists in code, but runtime auditing is absent.** No real GitHub rate-limit event appeared in selected full logs, but there are no trustworthy endpoint/call/latency counters. **Impact: diagnostic rather than immediate savings; confidence: high.**

## Speed Optimizations

| Rank | Type | Evidence and root cause | Exact change | Estimated saving | Risk |
|---|---|---|---|---|---|
| 1 | Critical path | Three implement failures ran 1,873–2,185s before discovering missing `scripts/helper.sh` support baseline. | Immediately after support staging, validate every ledger entry against `STAGED_SUPPORT_BASE_DIR`. Emit `IMPLEMENT_SUPPORT_PREFLIGHT entries=N missing=N paths=... support_ref=...`; abort before memory retrieval or Codex. | 31–36 min/run; ~100 min observed | Low |
| 2 | Critical path | Eight slow reviews emitted 666 `ledger_emit_substate` failures; 55–108 per run. `review_run_reviewers.sh` invokes the helper synchronously per lane. | Append substates to run-local JSONL, then flush once per phase. On the first auth/bootstrap failure, open a run-local circuit and retain local telemetry only. Emit `AI_MEMORY_SUBSTATE_SUMMARY attempted=N flushed=N dropped=N reason=... ms=...`. | **Inference:** 10–30 min from review p95 | Low–medium |
| 3 | Critical path | All eight structured slow reviews used six reviewers and had the Mistral slot fail. Runs included 17 retryable failures and four stall kills. | Canary `REVIEWER_CIRCUIT_BREAKER_ENABLED=1`; retain threshold `3` and TTL `1800`. Log `REVIEWER_SLOT_HEALTH model=... failures=N state=open|closed reason=...`. | **Inference:** 5–25 min on degraded runs | Low |
| 4 | Feedback path | Five CI runs surfaced the `300 != 180` budget assertion only after 944–1,223s. | Update the stale test to use the same `E2E_JOB_TIMEOUT_MINUTES` source as the workflow, and run this contract before the large CI suite. Emit budget components in `CI_BUDGET_CONTRACT`. | 15–20 min faster failures | Low |
| 5 | Recurring overhead | Poll run `35546180313` took 293s; start and completion memory pushes each consumed about 47–48s. | Reuse one memory checkout and flush buffered start/completion events in one final push; failure trap should flush `phase_started` plus `phase_failed`. | ~45–50s per poll | Medium |
| 6 | Queue/dispatch | Twelve long cancelled `Internal: AI Review & Autofix` runs were paired with successful semantic-agent runs created 14–19s later; paired durations total 65,831s. | Correlate dispatches by PR/head SHA and wait for both wrapper and engine visibility before fallback dispatch. Emit `REVIEW_RUN_LINK parent_run=... child_run=... pr=... head_sha=... source=...`. | Reduced pending-run replacement and queue noise | Low |

Micro-optimization: Copilot review is healthy—49/49 successful, p50 `385s`—so optimize it only after the review/autofix and CI critical paths.

## Cost Optimizations

1. **Prevent invalid implement executions.**  
   The three identical staged-support failures consumed `3,972,495` Codex tokens—**60.0% of all 6,620,824 Codex tokens** in the assembled window. The preflight above is the largest low-risk cost reduction.

2. **Batch AI-memory writes.**  
   Long reviews repeatedly attempted remote substate pushes that timed out or failed authentication. In runs `35511425396` and `35519204369`, final writes retried 16 times before failing with missing Git credentials. Batch to one push and stop retries after an authentication-class error. This saves runner minutes rather than model tokens.

3. **Enforce model-aware prompt limits.**  
   Eighteen `CONTEXT_BUDGET_WARN` events occurred, with sampled prompts at 186K–233K tokens. Run `35511425396` requested approximately 237,522 input plus 32,000 output tokens. Compute:
   `maximum_input = context_window - requested_output - safety_margin`, then prune oldest comments/check history before invocation. Emit component counts via `PROMPT_COMPONENT_BUDGET phase=... static=... diff=... comments=... memory=... semble=... total=... limit=...`.

   **Estimated saving:** 10–20% of 29.61M direct prompt tokens, or roughly 3–6M tokens.  
   **Quality risk:** medium; never trim the current diff, issue requirements, or latest review findings.

4. **Use reviewer tiers for eligible, non-sensitive changes.**  
   `REVIEW_TIER_RESOLVER_ENABLED=false` and `REVIEWER_RISK_TIER_ENABLED=0` caused six-model review panels. Enable the resolver for non-sensitive paths; the existing always-full regex preserves full review for `scripts/`, workflows, prompts, and contracts. Eligible runs can reduce reviewer calls from six to three—up to 50%—without weakening sensitive-path reviews.

5. **Preserve cache-friendly prefixes.**  
   Cache-read tokens represent 84.3% of reported OpenRouter tokens, a strong positive signal. Avoid moving volatile PR metadata into the static prefix. Add a stable-prefix hash and per-call `cache_status=hit|miss|unknown`; do not infer hit rate from token totals.

6. **Semble should remain enabled, with yield measurement.**  
   It made 63 queries returning 469,210 bytes—about 7.45KB/query—with four runtime budget fallbacks. It is likely reducing raw file expansion, but context warnings show it does not control total prompt growth. Add `raw_bytes`, `selected_bytes`, and `prompt_bytes_avoided` to `SEMBLE_QUERY`. Do not enable Serena solely for cost reduction: it was disabled and emitted no runtime telemetry.

## Reliability Improvements

| Rank | Failure evidence | Category | Exact fix and logging | Expected impact / rollback |
|---|---|---|---|---|
| 1 | Three of four implement failures shared `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING`. | Contract/version skew | Fix the staged-support manifest and add pre-model ledger validation. | Could eliminate 75% of observed implement failures. Rollback by retaining the old post-model guard. |
| 2 | Eight slow reviews logged 666 memory substate failures; four runs emitted fail-open memory telemetry. | Dependency/authentication | Authenticate memory remote once, batch events, and circuit-break on auth errors. Include `reason`, `rc`, and attempts in telemetry. | Removes a masked broken write path while retaining fail-open execution. |
| 3 | Run `35546298657` failed after 312s because `LINKED_ISSUE_METADATA_FILE` was unset. | Workflow/helper interface mismatch | Introduce a versioned required-env manifest shared by workflow and script; validate immediately after runtime initialization. | Prevents late setup failures. Fail closed before expensive work. |
| 4 | Mistral failed in 8/8 slow-review summaries; sampled failures included upstream rate limiting and context overflow. | Provider/context | Enable the health breaker and classify `context_length_exceeded` separately from retryable server errors. | Fewer doomed retries; disable through the existing variable if quality regresses. |
| 5 | Five CI runs repeated the same stale `180` expectation against a 300-minute workflow timeout. | Test/config drift | Derive both validation and test expectations from one budget constant and run it first. | Removes deterministic rerun churn. |
| 6 | Security audit run `35499174216` failed with `captured_path_error=No such file or directory`, then only reported `Codex exited nonzero`. | Missing diagnostic context | Validate all generated prompt/input paths before Codex and log the exact missing path and argv role. | Faster root-cause isolation; only one run, so confidence is low. |

`BREAK_GLASS` count was zero, so there is no observed policy-override pressure. The 18 context warnings instead indicate prompt-size risk.

Semble’s 24 contract-test fallbacks are expected test behavior. Four runtime fallbacks out of 63 queries (6.3%), all budget-related, look like healthy rare fail-open behavior rather than a broken rollout. Add whether the raw-content fallback was actually injected.

Serena recorded no query, fallback, or probe event. Logs show `SERENA_ENABLED=false`; this is not a rollout failure, but an explicit `SERENA_PROBE result=skipped reason=disabled target=...` would remove ambiguity.

## AI Memory Health

- **Retrieval effectiveness, selected full logs:** 12/12 retrieves returned records—**100% hit rate**.
- Average retrieved size was **1,448 tokens against a 1,467-token average budget**, or 98.7% occupancy.
- Keyword extraction: `llm=8`, `plain=4`, `none=0`.
- No retrieve returned zero records; no `enabled:false` retrieve was observed.
- Four review runs (`35486128483`, `35504713879`, `35511425396`, `35519204369`) emitted eight fail-open record events.
- Successful memory events required at most two push attempts; four events used two attempts.
- The more serious issue is substate emission: 55–108 failures per completed slow review. Inspected failures include both 120-second timeouts and 16-attempt unauthenticated pushes.

**Recommendation:** preserve retrieval, but decouple remote writes from the reviewer critical path. Add `reason`, `failure_class`, `elapsed_ms`, `push_attempts`, and `circuit_state` to failed memory telemetry.

No `promote`, `compact`, or `finalize-task` telemetry appeared in selected logs, so retention/compaction health cannot be assessed. Verify those operations emit telemetry when scheduled.

## GH API Call Audit

Runtime call counts are **not collected**, so any raw count derived from log text would be unreliable because Actions logs echo scripts and documentation.

- Repository policy correctly requires batched GraphQL and cycle-local caches in `agents.md`, `CLAUDE.md §15`, and `unattended_system_instructions.md §14`.
- `scripts/orchestrate_poll_process.sh` uses `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, `ACTIVE_WORKFLOW_ISSUES`, and `STALL_MANAGED_LINKED_PR_CACHE`; these are good patterns.
- No actual `GitHub API rate limit hit` event appeared in selected full logs.
- The review sweep statically performs separate queued/in-progress/pending snapshots for both `internal-review.yml` and `review_autofix.yml`. **Inference:** fetching unfiltered recent runs once per workflow and filtering locally could reduce six status calls to two per tick—a 67% reduction, approximately 192 calls/day on the 30-minute schedule. Retain per-status fallback if the unfiltered page is saturated.

Add instrumentation inside `gh_retry`, `gh_retry_to_file`, `_safe_gh_jq`, `gh_api_json_to_file`, and `curl_gh_api`:

- `GH_API_CALL helper=... method=... endpoint_class=... attempt=N result=ok|error|rate_limited ms=N cache=hit|miss`
- `GH_API_SUMMARY calls=N retries=N rate_limits=N errors=N by_endpoint=...`

Normalize paths and omit query values, bodies, tokens, repository secrets, and PII.

## Prompt Cache & Memory System

- `cache_hit_rate`: **unavailable**
- OpenRouter cache-read tokens: `168,325,878`
- Cache-write tokens: `0`
- OpenRouter total tokens: `199,582,371`
- Usage available: `136/196` calls (69.4%); unavailable: `60/196` (30.6%)
- Context warnings: `18`

The large cache-read share indicates useful prefix reuse, but it is not equivalent to a hit rate. Zero cache-write tokens may mean creation usage is unreported rather than absent.

Recommended changes:

1. Emit `prefix_hash`, `cache_status`, `cache_read_tokens`, `cache_creation_tokens`, and `usage_unavailable_reason` per call.
2. Keep system instructions, stable rubrics, and tool contracts before volatile PR metadata.
3. Place comments, timestamps, run IDs, and retry diagnostics in a dynamic suffix.
4. Enforce the model-aware hard input limit before requests, not only the existing 70% warning.
5. Record Semble’s `raw_bytes_saved`; retain raw fallback when query output is empty or budget-exhausted.
6. Reduce memory retrieval budgets only as a micro-optimization: 1.4–1.6K memory tokens are small relative to 186K–233K review prompts.

## Orchestrator Health

- Poller reliability is generally healthy: 108 successes, zero failures, and 11 cancellations across 119 runs; p50 `285s`.
- Recent run `35546180313` completed successfully, but two memory-event pushes consumed about 95 seconds combined.
- Stall recovery worked in implement run `35538013085`, which succeeded after logging that implementation stalled and was re-triggered.
- Sweep deduplication worked: recent sweeps repeatedly skipped PRs `#4174` and `#4176` because active runs existed.
- However, 12 long cancelled wrapper runs paired with successful engine runs strongly suggest duplicate-entrypoint/concurrency replacement. This also keeps sweeps in no-op `active_run` cycles.
- Semble availability changed from false during early setup to true after installation in run `35546180313`; final availability should be summarized once to prevent misleading partial snapshots.

Track these indicators:

- `ORCHESTRATOR_CYCLE_SUMMARY issues=N actions=N noops=N api_calls=N memory_ms=N process_ms=N queue_ms=N`
- active-run age and status by PR
- parent/child run correlation
- stall-recovery attempts and outcomes
- final MCP availability, not only early environment snapshots
- merge-train examined/released counts

## Pipeline Flow Bottlenecks

| Stage | Observed state | Dominant bottleneck | Priority fix |
|---|---|---|---|
| Clarify | p50 `1s`; 134/139 other/skipped | Gate-heavy, not compute-bound | No optimization needed |
| Plan | p50 `1s`; 125/130 other/skipped | Gate-heavy | No optimization needed |
| Implement | Active runs reach 879–2,205s; three late identical failures | Post-model staged-support validation | Pre-model support preflight |
| Review/autofix | p95 `8,145.6s`; six-reviewer panels; memory timeouts; provider failures | Compute, retry, prompt growth, memory writes | Memory batching + provider breaker + prompt cap |
| Copilot review | p50 `385s`, 49/49 successful | Secondary model latency | Observe only |
| Validate/CI | CI p50 `2,116s`, failure rate 25.7% | Large serial suite and late contract checks | Fast-fail deterministic contracts |
| Orchestrate | Poll p50 `285s`; frequent no-op/active-run cycles | Setup, memory pushes, dispatch dedup | Stage timing and single memory flush |
| Merge/conflict | Recent merge-train summary examined `0`, released `0` | Insufficient evidence | Add cycle summary before tuning |

End-to-end priority: **implement preflight → memory batching/auth circuit → reviewer provider/context controls → CI fast-fail → dispatch correlation**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 136 minutes; CI p50 35 minutes; poller p50 4.75 minutes.
- **Top failure modes:** staged-support baseline missing in three implement runs; repeated stale CI budget contract; memory-ledger timeouts/auth failures; one missing review metadata env variable.
- **Highest-cost drivers:** 199.58M OpenRouter tokens in review/autofix and 3.97M Codex tokens wasted by three deterministic implement failures.
- **Top actions:**
  1. Add and enforce `IMPLEMENT_SUPPORT_PREFLIGHT`.
  2. Batch reviewer substates locally and circuit-break memory writes after the first authentication-class failure.
  3. Enable reviewer health breaking and enforce model-aware prompt limits before request dispatch.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 402 (40.2%) | 15 (1.5%) | 45 (4.5%) | 538 | 8s | 4,081s |
| CI | 35 | 26 | 9 (25.7%) | 0 | 0 | 2,116s | 2,441.9s |
| Implement | 130 | 5 | 4 (3.1%) | 0 | 121 | 1s | 1,193.4s |
| Review/autofix | 153 | 117 | 1 (0.7%) | 34 (22.2%) | 1 | 34s | 8,145.6s |
| Orchestrate poll | 119 | 108 | 0 | 11 (9.2%) | 0 | 285s | 514.5s |
| Copilot reviewer | 49 | 49 | 0 | 0 | 0 | 385s | 579s |
| Validation refresh | 1 | 1 | 0 | 0 | 0 | 1,250s | 1,250s |
| Security audit | 1 | 0 | 1 | 0 | 0 | 87s | 87s |

Terminal-outcome success rate excluding skipped/other runs: **87.0%**.

### AI cost and latency

| Metric | Value |
|---|---:|
| Codex calls / tokens | 58 / 6,620,824 |
| OpenRouter calls | 196 |
| OR prompt / completion / total | 29,613,344 / 1,651,208 / 199,582,371 |
| OR cache read / write | 168,325,878 / 0 |
| OR usage available / unavailable | 136 / 60 |
| Cache hit rate | Not collected |
| Wall-clock samples | 122 |
| Wall-clock p50 / p99 | 267,500ms / 8,957,700ms |
| Break-glass count | 0 |
| Context-budget warnings | 18 |

### MCP telemetry

| System / target | Queries | Bytes | Fallbacks | Notes |
|---|---:|---:|---:|---|
| Semble, all assembled runs | 63 | 469,210 | 28 | 24 contract-test; 4 runtime |
| Semble `overflow`, selected deep logs | 35 | Not split by target | 27 | 24 contract; 3 runtime |
| Semble `reviewer-context`, selected deep logs | 8 | Not split by target | 0 | No observed fallback |
| Serena | 0 | 0 | 0 | Disabled; no runtime evaluation possible |
| Other MCP servers observed | 0 | 0 | 0 | None |

| MCP target availability | probe_ok | probe_failed | probe_skipped | Gap |
|---|---:|---:|---:|---|
| Serena, target not emitted | 0 | 0 | 0 | Emit explicit skipped probe when disabled |
| Semble | N/A | N/A | N/A | Current schema has queries/fallbacks but no probe counters |

### AI memory, selected full logs

| Metric | Value |
|---|---:|
| Retrieve operations | 12 |
| Retrieves with records | 12 (100%) |
| Average estimated tokens / budget | 1,448 / 1,467 |
| Keyword method | `llm=8`, `plain=4`, `none=0` |
| Zero-record retrieves | 0 |
| Fail-open record events | 8 across 4 runs |
| Maximum successful push attempts | 2 |
| Slow-review substate failures | 666 across 8 runs |

### GH API telemetry

| Metric | Value |
|---|---|
| Runtime call count | Not collected |
| Endpoint latency | Not collected |
| Cache hit/miss | Not collected |
| Actual GitHub rate-limit events in selected logs | 0 |
| Confirmed batching controls | GraphQL candidate/linked-PR helpers and cycle-local caches |
| Highest-priority collection step | Add `GH_API_CALL` and `GH_API_SUMMARY` markers |

**Coverage gap:** the assembled context has telemetry or summaries for 124 runs, while `summary.json` contains full deep-log telemetry for 29 selected runs. Successful-run sampling is 7%, so latency/provider conclusions based on slow-run logs are high-confidence for outliers but not population-wide distributions.

## Deep Audit — Workflows & Scripts (2026-09-21)

### Section 1: Bug & Correctness Sweep

Repository-wide checks found no Bash syntax errors across 84 scripts, no Python AST errors across 57 scripts, and no YAML lint errors across 49 workflows.

#### BUG-001 — Critical guard latches use non-retrying GitHub mutations

- **File path and line range:** `scripts/implement_handle_guard_block.sh:13-58`, `scripts/implement_handle_guard_block.sh:212-289`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** The staged-support and destructive-commit handlers call `gh label`, `gh issue edit`, `gh issue view`, and `gh issue comment` directly. Most writes are followed by `|| true`; a transient GitHub failure can therefore leave `ai:needs-human` or `ai:destructive-blocked` absent while the handler exits. Lines 21-37 and 246-264 explicitly acknowledge that an unverified latch does not block redispatch.
- **Recommended fix:** Source `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/gh_helpers.sh` and route every GitHub call through `gh_retry`. Fail closed when the final verification read confirms the latch is absent; preserve Telegram fail-open behavior.

#### BUG-002 — One-time validation dispatch bypasses the available retry helper

- **File path and line range:** `.github/workflows/review_autofix.yml:1204-1243`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** The step defines `gh_retry` at lines 1123-1137 and uses it for label reads, but lines 1229 and 1237 invoke raw `gh workflow run` and `gh issue edit`. A transient dispatch failure merely warns and continues; because this is a post-merge event, there may be no later event to initiate standalone validation. A transient label-removal failure can also permit duplicate dispatch on a rerun. [NEEDS VERIFICATION]
- **Recommended fix:** Wrap both workflow candidates and the label removal with `gh_retry`. Leave `ai:orchestrator-validate-required` intact when dispatch fails and emit a force-tick/retry marker.

#### SEC-001 — PAT is embedded directly in executable step source and Git remote configuration

- **File path and line range:** `.github/workflows/implement.yml:4328-4340`, `.github/workflows/review_autofix.yml:6005-6010`
- **Severity:** High
- **Category tag:** `security`
- **Description:** Both steps already export `GH_TOKEN`, but interpolate `${{ secrets.GH_PAT }}` directly into the `run:` body and save it in `origin` via `git remote set-url`. This materializes the PAT in the generated step script and persists it in `.git/config`; masking protects ordinary logs but not local file or process inspection.
- **Recommended fix:** Keep a credential-free origin URL and authenticate individual fetch/push operations with an ephemeral `http.extraheader` or credential helper derived from `GH_TOKEN`. Never place `${{ secrets.* }}` directly in shell source.

#### BUG-003 — “Unreadable file” contract is implemented as existence-only

- **File path and line range:** `scripts/validate_editor_audit.sh:43-78`
- **Severity:** Low
- **Category tag:** `bug`
- **Description:** Exit code 3 is documented for a missing or unreadable summary, but the guard checks only `-f`. An existing unreadable file reaches `awk`; because the script intentionally lacks `set -e`, the failure becomes an empty audit and returns exit code 1 instead of 3.
- **Recommended fix:** Check `[ -r "${summary_file}" ]` and explicitly test the `awk` return code before classifying the audit contents.

### Section 2: GitHub API Call Redundancy Audit

The six-to-two review-sweep status-call optimization is already covered by the existing report’s **GH API Call Audit** and is not duplicated below.

#### BATCH-001 — Merge-train file discovery performs one REST request per PR

- **File path and line range:** `scripts/review_merge_train.sh:41-60`, `scripts/review_merge_train.sh:110-137`, `scripts/review_merge_train.sh:194-230`, `scripts/review_merge_train.sh:423-457`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The gate performs one open-PR request plus one paginated `/pulls/{n}/files` request for each older PR, up to `MERGE_TRAIN_MAX_OLDER_PRS=20`. Current logical count is `1 + N`; the release path is `2 + U`, where `U` is the number of distinct PR file lists needed. The cycle-local cache prevents duplicate fetches but does not batch the first fetch.
- **Recommended fix:** Add an aliased GraphQL file-list prefetch modeled on `_fetch_linked_pr_status_graphql`, in batches of approximately 20 PRs. Projected gate count: `1 + ceil(N/20)`—21 calls become 2 at the configured cap. Retain REST fallback for PRs whose file connection exceeds the GraphQL page.

#### BATCH-002 — Deterministic skip applies linked-issue labels one REST mutation at a time

- **File path and line range:** `.github/workflows/review_autofix.yml:1454-1493`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** One GraphQL query resolves up to 50 closing issues, followed by one REST label mutation per issue and one label-creation attempt. Current count is `N + 2`; with 50 linked issues it reaches 52 calls.
- **Recommended fix:** Return issue node IDs and the `ai:ready-to-merge` label ID in the initial query, then issue one aliased `addLabelsToLabelable` mutation. Projected count: 2 calls, or 3 only when the label must first be created. Follow the alias construction used by `_fetch_candidate_issue_details_graphql`.

#### BATCH-003 — Weekly consumer retro fan-out repeats repository metadata reads

- **File path and line range:** `scripts/workflow_retro_fanout.sh:96-115`, `scripts/workflow_retro_fanout.sh:202-223`, `scripts/workflow_retro_fanout.sh:275-323`, `scripts/workflow_retro_fanout.sh:328-346`
- **Severity:** Low
- **Category tag:** `api-batching`
- **Description:** For each active consumer, the loop performs a variable GET, a label-creation attempt, a tracker issue list, and a current-window comment list before necessary upsert mutations. With the current 13-consumer roster, that is 52 calls before tracker/comment writes. [NEEDS VERIFICATION]
- **Recommended fix:** Batch label existence, tracker selection, and recent tracker comments across repositories using GraphQL aliases based on `_fetch_candidate_issue_details_graphql`. Keep the unbatchable Actions-variable GET per repository. Projected pre-write count for 13 consumers: approximately 14 calls, subject to GraphQL pagination limits.

#### API-001 — Inline retry shims retry permanent client errors

- **File path and line range:** `.github/workflows/review_autofix.yml:1123-1137`, `.github/workflows/review_autofix.yml:1326-1337`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** Both inline `gh_retry` functions retry every error four times. Unlike `scripts/gh_helpers.sh:439-465`, they do not stop on authentication, permission, validation, or missing-resource failures. Current count for a permanent failure is 4 calls; the canonical helper would issue 1.
- **Recommended fix:** Stage/source the canonical helper or extract its permanent-failure classifier into a lightweight shared module suitable for these jobs.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Context-budget telemetry function is copied byte-for-byte

- **File path and line range:** `scripts/review_run_reviewers.sh:69-107`, `scripts/review_apply_fixes.sh:164-202`, `scripts/review_rb_judge.sh:256-294`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` has the same 39-line body in all three scripts. Any telemetry schema, import path, or fail-open behavior change must be synchronized manually.
- **Recommended fix:** Move it into `scripts/watchdog_helpers.sh` or a new `scripts/prompt_budget_helpers.sh` with signature `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`. Source that module from all three callers.

#### DUP-002 — Workflow support staging is independently maintained in ten workflows

- **File path and line range:** `.github/workflows/check_failure_triage.yml:263-324`, `.github/workflows/clarify.yml:218-353`, `.github/workflows/implement.yml:932-1288`, `.github/workflows/orchestrate.yml:358-486`, `.github/workflows/orchestrate_clarify_respond.yml:281-414`, `.github/workflows/orchestrate_poll.yml:373-585`, `.github/workflows/plan.yml:281-428`, `.github/workflows/review_autofix.yml:1765-1849`, `.github/workflows/validate.yml:214-433`, `.github/workflows/workflow_failure_heal.yml:131-160`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** These blocks independently maintain script manifests, main fallbacks, prompt/schema copying, optional assets, and environment exports. `review_autofix.yml` and `validate.yml` already delegate most work to `scripts/stage_workflow_support.sh`, while the other workflows repeat comparable logic inline.
- **Recommended fix:** Generalize the helper to `stage_workflow_support.sh <phase> --manifest <path> --layout in-tree|runtime [--self-repo-ledger]`. Store per-phase manifests as data. Keep implement’s staged-support ledger as a phase callback rather than duplicating the common staging engine.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Support-staging run body has only 674 characters of expression headroom

- **File path and line range:** `.github/workflows/implement.yml:932-1288`
- **Severity:** High
- **Category tag:** `expression-limit`
- **Description:** The interpolated `Stage workflow support files` run body is approximately 20,326 characters, leaving about 674 characters before the 21,000-character runner limit. It exceeds the requested 18,000-character high-risk threshold.
- **Recommended fix:** Extract the body to `scripts/stage_workflow_support.sh` with an implement manifest and self-repository ledger mode. Keep only environment setup and one script invocation in YAML.

#### EXPR-002 — Destructive preflight is above the medium-risk threshold

- **File path and line range:** `.github/workflows/implement.yml:3143-3449`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The interpolated `Preflight destructive-commit guard` body is approximately 17,313 characters, leaving about 3,687 characters of headroom. It exceeds the 15,000-character medium-risk threshold.
- **Recommended fix:** Extract it to `scripts/implement_preflight_guard.sh`, with explicit paths for `GITHUB_OUTPUT`, the staged-support ledger, issue-body file, and scope helper.

No individual `${{ ... }}` expression exceeds 234 characters, and the largest `if:` value is approximately 793 characters. No workflow exceeds the 800 KB warning threshold; the largest is `review_autofix.yml` at approximately 497,787 characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Three poller functions have no repository call sites

- **File path and line range:** `scripts/orchestrate_poll_process.sh:11348-11356`, `scripts/orchestrate_poll_process.sh:12958-12977`, `scripts/orchestrate_poll_process.sh:13087-13097`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `get_last_validation_run_conclusion`, `read_standalone_state_json`, and `stall_recovery_action_is_terminal` are definition-only. The dead `read_standalone_state_json` path also retains a latent paginated comments API call.
- **Recommended fix:** Remove the functions after confirming no externally sourced consumer depends on them, or mark them as a documented compatibility API with direct tests and callers.

#### DEAD-002 — Reviewer cutover left unused compatibility functions

- **File path and line range:** `scripts/review_run_reviewers.sh:664-692`, `scripts/review_run_reviewers.sh:3562-3583`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `is_mcp_incompatible_model` and `strip_all_mcp_server_blocks` are explicit no-ops after the OpenCode cutover. `reviewer_patch_reasoning_config_file` is also definition-only, while `reviewer_prepare_reasoning_configs` states that reasoning now uses `--variant`.
- **Recommended fix:** Document these as a public compatibility surface if consumers may call them. Otherwise deprecate them and remove them in a versioned cleanup consistent with identifier-immutability rules.

#### SHELL-001 — Reviewer state assignments are never consumed

- **File path and line range:** `scripts/review_run_reviewers.sh:753-761`, `scripts/review_run_reviewers.sh:3533-3548`, `scripts/review_run_reviewers.sh:3772-3775`, `scripts/review_run_reviewers.sh:4047-4048`, `scripts/review_run_reviewers.sh:4120-4128`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** ShellCheck reports unused assignments including `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE` and `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`. Repository search also finds `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH` and `REVIEWER_ATTEMPT_WD_REASON` written but never read.
- **Recommended fix:** Remove obsolete assignments, or wire `open_until_epoch` and watchdog reason into `REVIEWER_SLOT_STATE`/`REVIEW_AUTOFIX_RUN_SUMMARY_V1` if they are intended telemetry.

#### CONSIST-001 — Inline label metadata duplicates the authoritative contract

- **File path and line range:** `.github/workflows/review_autofix.yml:1340-1343`, `.github/workflows/review_autofix.yml:1442-1448`
- **Severity:** Low
- **Category tag:** `consistency`
- **Description:** The deterministic-skip job hardcodes label colors and descriptions while comments require them to remain synchronized with `scripts/label_helpers.sh` and `.github/ai/label_contract.v1.json`. There is no shared data source in this job.
- **Recommended fix:** Stage a lightweight generated label helper or generate the inline constants from `label_contract.v1.json` during CI, with a contract check preventing drift.

No real `TODO`, `FIXME`, or `HACK` markers were found. ShellCheck found no repository-wide SC2086/SC2046 unquoted-expansion class requiring a separate finding.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, SEC-001, EXPR-001 |
| Medium | 6 | BUG-002, BATCH-001, BATCH-002, DUP-001, DUP-002, EXPR-002 |
| Low | 7 | BUG-003, API-001, BATCH-003, DEAD-001, DEAD-002, SHELL-001, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 4 | Medium |
| Code modularization | ~12 | Large |
| Expression size reduction | 3 | Medium |
| Medium/Low fixes | 5 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-21)

### Safety Tag Legend

`SAFE_TO_MERGE` is directly actionable; `NEEDS_VERIFICATION` requires specified checks; `RISKY_SKIP` must not be automated because pagination, retries, polling, or race defenses are involved.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Reuse the branch-runs snapshot across adjacent autofix probes

- **Safety tag:** `SAFE_TO_MERGE`
- **Files:** `scripts/gh_helpers.sh:1238-1244`, `scripts/gh_helpers.sh:1358-1364`; caller `.github/workflows/review_autofix.yml:6851-6870`
- **Current call count:** 2 on the successful no-peer path.
- **Proposed call count:** 1.
- **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs?branch={head_branch}&per_page=30`
- **Evidence:** Both helpers issue identical requests and run consecutively without an intervening GitHub mutation.

```bash
"/repos/${GITHUB_REPOSITORY}/actions/runs"
-f "branch=${head_branch}"
-f "per_page=30"
```

```bash
autofix_retrigger_has_inflight_peer ...
REVIEWED_HEAD_SHA="$(git rev-parse HEAD ...)"
autofix_changes_lost_head_retry_consumed ...
```

- **Proposed fix:** After `autofix_retrigger_has_inflight_peer` successfully validates and parses its response, retain it in a process-local cache keyed by repository and branch. Extend `autofix_changes_lost_head_retry_consumed` to consume that snapshot. Do not cache API or parse failures, so its existing live fallback and fail-closed behavior remain unchanged.
- **Safety rationale:** The endpoint, query filters, auth, retry helper, and workflow step are identical; successful-only shell-local reuse preserves independent failure semantics and does not cross workflow-step boundaries.
- **Downstream signal:** Implement a successful-response-only branch-runs cache in `gh_helpers.sh` and add a test asserting one `gh api` invocation for the adjacent no-peer/budget path.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Final-PR state and merge status use two identical reads

- **Safety tag:** `RISKY_SKIP`
- **File:** `scripts/orchestrate_poll_process.sh:10230-10242`, function `finalize_integration_merge_if_needed`
- **Current call count:** 2 on snapshot miss.
- **Proposed call count:** 1.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{final_pr}`
- **Evidence:**

```bash
existing_pr_state="$(gh_retry _safe_gh_jq ... --jq '.state' ...)"
existing_pr_merged="$(gh_retry _safe_gh_jq ... --jq '.merged_at != null' ...)"
```

- **Proposed fix:** Fetch one full PR object and derive both `.state` and `.merged_at != null` locally.
- **Safety rationale:** Although adjacent and identical, the calls are inside `orchestrate_poll_process.sh`, which explicitly defends against upstream races and therefore triggers mandatory `RISKY_SKIP`.
- **Downstream signal:** Do not auto-implement; manually verify final-merge race tests and preserve cache-miss/failure behavior before consolidating.

#### REUSE-002 — Reissue paths fetch issue title and body separately

- **Safety tag:** `RISKY_SKIP`
- **Files:** `scripts/orchestrate_poll_process.sh:13726-13733` (`execute_stall_recovery_action`), `scripts/orchestrate_poll_process.sh:16413-16421` (`run_standalone_stall_recovery`), `scripts/orchestrate_poll_process.sh:21397-21399` (implementation-failed sweep)
- **Current call count:** 2 per reissue path; up to 6 across the three sites.
- **Proposed call count:** 1 per path; up to 3.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence:**

```bash
orig_title="$(gh_retry _safe_gh_jq ... --jq '.title // ""' ...)"
orig_body="$(gh_retry _safe_gh_jq ... --jq '.body // ""' ...)"
```

- **Proposed fix:** Add a single issue-object fetch at each site and extract both fields locally, preferably through one shared `_fetch_issue_title_body_json` helper.
- **Safety rationale:** These calls are in orchestrator stall/recovery paths, and combining them also changes the current independent partial-failure behavior.
- **Downstream signal:** Do not auto-implement; manually test API failure between field reads and every managed, standalone, and implementation-failed reissue path.

#### REUSE-003 — Merge-train marker lookup discards a body that is immediately re-fetched

- **Safety tag:** `RISKY_SKIP`
- **Files:** `scripts/review_merge_train.sh:257-261`, `scripts/review_merge_train.sh:275-287`, caller `scripts/review_merge_train.sh:354-387`
- **Current call count:** 2 logical calls when a marker exists.
- **Proposed call count:** 1 logical call.
- **Endpoints:** Paginated `GET /repos/{owner}/{repo}/issues/{pr}/comments?per_page=100`; `GET /repos/{owner}/{repo}/issues/comments/{comment_id}`
- **Evidence:**

```bash
--jq ".[] | select(.body | startswith(\"${marker}\")) | .id"
...
existing_body="$(gh_retry gh api "repos/${MT_REPO}/issues/comments/${existing_id}" --jq '.body' ...)"
```

- **Proposed fix:** Extend the marker lookup to return the latest matching comment’s ID and body together, then pass both into `_mt_upsert_comment`.
- **Safety rationale:** The source lookup uses `--paginate`, which mandates `RISKY_SKIP` because page ordering and boundary semantics must remain exact.
- **Downstream signal:** Do not auto-implement; manually verify latest-marker selection across multiple pages and bodies containing tabs, newlines, and Unicode.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- BATCH-001: `RISKY_SKIP` — Its per-PR files reads are paginated, requiring manual GraphQL connection and REST-fallback review.
- BATCH-002: `NEEDS_VERIFICATION` — Verify aliased mutation partial-error handling and label creation/ID races before batching.
- BATCH-003: `RISKY_SKIP` — Cross-repository auth scopes and paginated comment reads prevent automatic consolidation.
- API-001: `RISKY_SKIP` — The calls are inside retry loops, and changing classifiers affects retry and logging semantics.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | MERGE-001 |
| NEEDS_VERIFICATION | 1 | BATCH-002 |
| RISKY_SKIP | 6 | REUSE-001, REUSE-002, REUSE-003, BATCH-001, BATCH-003, API-001 |

### Implement-Stage Handoff

- MERGE-001
