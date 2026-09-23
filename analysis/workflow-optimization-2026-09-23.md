## Executive Summary

- **CI is failing late and expensively:** 26/31 CI runs failed at `Script-workflow cross-reference`; nine deep dives found the same missing `scripts/helper.sh` only after 37.3–42.9 minutes. Moving this check first saves an estimated **16–18 runner-hours/window**. **Confidence: high.**
- **Review credential-contract regression caused six verified long failures:** runs `35737368348`, `35755050146`, `35768346658`, `35779370870`, `35790039807`, and `35795320252` failed after reviewer work because editor isolation required `OPENROUTER_API_KEY` after `opencode_helpers.sh` had unset it. They consumed **97,123,290 OpenRouter tokens** and 8.2 runner-hours. **Confidence: high.**
- **Review/autofix dominates AI cost:** 143.95M of 144.11M OpenRouter tokens—**99.9%**—came from this family; p95 duration was 5,948 seconds. **Confidence: high.**
- **Prompt caching is valuable but context is near capacity:** derived weighted cache-read rate is **80.55%**, while eight `CONTEXT_BUDGET_WARN` events occurred exclusively in review/autofix, reaching 97.7% of one model’s context window. **Confidence: high.**
- **Metadata artifact creation is another deterministic failure:** three semantic-agent runs failed at `Collect PR metadata`; run `35798932027` fetched a 170,637-byte diff, then failed because `linked_issue_metadata.json` was never produced. **Confidence: high for latest run; medium for earlier two.**
- **Orchestration is reliable but repeatedly processes stalled state:** 83/85 poll runs succeeded, yet issue `#4290` remained `ai:done` for 142–150 minutes. Fresh-push suppression correctly prevented duplicate recovery. **Confidence: high.**

## Speed Optimizations

1. **Critical path — run workflow/script reference validation first**
   - **Evidence:** all 26 CI failures ended at `Script-workflow cross-reference`; nine logs reported `implement.yml: references scripts/helper.sh which does not exist` after a median 42.2 minutes.
   - **Root cause:** a deterministic, sub-second repository-integrity check runs after the full test suite.
   - **Exact change:** move `python3 scripts/check_workflow_script_refs.py` immediately after checkout/setup in `.github/workflows/ci.yml` and equivalent release validation.
   - **Savings:** 37–43 minutes per affected run.
   - **Risk:** low.

2. **Critical path — preflight editor provider credentials before reviewer fan-out**
   - **Evidence:** six long runs reached the editor only to fail immediately at historical `review_apply_fixes.sh:168`.
   - **Root cause:** `opencode_helpers.sh` captured then unset the raw key; historical isolation subsequently required the unset variable instead of the broker token.
   - **Exact change:** validate `provider_key_captured=true`, `broker_ready=true`, and `broker_token_present=true` before reviewers. Never log values. Isolation must consume `MODEL_PROVIDER_BROKER_TOKEN`, not the raw key.
   - **Savings:** 56–99 minutes and millions of tokens per affected run.
   - **Risk:** low. HEAD removed the historical path, but runtime regression coverage is still needed.

3. **Critical path — bound unhealthy reviewer slots**
   - **Evidence:** 14 structured deep runs recorded eight 600-second stall kills. `minimax/minimax-m3` repeatedly stalled and retried/fell back; the circuit breaker was disabled.
   - **Exact change:** open the existing circuit after one stall plus one retryable failure for the same model, with the existing TTL. Emit model, slot, elapsed time, and circuit state.
   - **Savings:** approximately 10–30 minutes on affected review runs.
   - **Risk:** medium; retain minimum successful-reviewer quorum.

4. **Recurring path — lazily initialize Semble in pollers**
   - **Evidence:** orchestrate-poll ran 85 times but recorded only one Semble query; recent runs spent roughly a minute installing/building Semble before no-query poll cycles.
   - **Exact change:** defer Semble setup until a judge or context-expansion path actually requests it.
   - **Savings:** estimated 30–60 seconds on most poll runs.
   - **Risk:** low.

5. **Micro-optimization — circuit-break repeated live-log fetches**
   - **Evidence:** stable-release run `35768550507` made **236 failed live-log fetches** for job `106891727076` over roughly 11 minutes.
   - **Exact change:** after three consecutive failures, suppress live-log calls for 60 seconds while retaining status polling.
   - **Savings:** up to 233 API calls; small direct latency reduction.
   - **Risk:** low.

## Cost Optimizations

1. **Eliminate reviewer spend before deterministic editor failure**
   - The six verified provider-contract failures consumed **97.12M tokens**, 67.5% of review/autofix’s total.
   - Add the early provider preflight above and classify failures as `credential_contract`, not `editor_empty_noop`.
   - **Estimated savings:** up to 97M tokens for this observed recurrence.
   - **Quality risk:** none.

2. **Enable risk-tiered reviewer selection**
   - `REVIEW_TIER_RESOLVER_ENABLED=false`, `REVIEWER_RISK_TIER_ENABLED=0`, while reviews routinely invoke six slots at `xhigh`.
   - Enable the existing tier resolver for low-risk diffs, preserving full review for `.github/workflows/`, `scripts/`, prompts, migrations, and contracts.
   - **Estimated savings:** 10–25% of review prompt/completion tokens.
   - **Quality risk:** medium; deploy in observe-only mode first.

3. **Reduce context duplication**
   - Eight warnings occurred at 141K–165K tokens on 200K models; run `35701080313` reached 125,098/128,000.
   - Deduplicate repeated workflow instructions, cap historical comment bodies, and pass unchanged static context only once.
   - **Estimated savings:** 5–15% of uncached prompt tokens, approximately 1.4M–4.2M tokens.
   - **Quality risk:** low if hashes and omission summaries are logged.

4. **Keep Semble enabled**
   - Runtime queries typically returned 12 chunks, 13–15KB, in 348–581ms. There were zero runtime fallbacks; all 40 fallbacks were explicit contract tests.
   - Semble appears to add compact, low-latency context rather than noisy expansion.
   - Add `source_bytes`, `selected_bytes`, `dedup_bytes`, and estimated prompt bytes avoided to prove net benefit.

5. **Serena cannot yet be evaluated**
   - Queries, tool calls, response bytes, fallbacks, and probes were all zero; deep logs show it disabled.
   - Emit a skipped probe when disabled so “intentionally off” is distinguishable from missing instrumentation.

## Reliability Improvements

1. **Fix and regression-test the provider credential contract**
   - **Impact:** prevents the six verified editor failures.
   - Add `EDITOR_PROVIDER_CONTRACT status=ok|failed raw_key_captured=<bool> broker_ready=<bool> isolation_mode=<mode>`.
   - Fail before reviewers when the contract is unsatisfied.
   - **Rollback:** fail closed for editing while preserving reviewer artifacts.

2. **Restore linked-issue metadata artifact ownership**
   - `scripts/review_collect_pr_metadata.sh` neither requires nor writes `LINKED_ISSUE_METADATA_FILE`, while the workflow immediately hashes it.
   - Atomically write the artifact and emit:
     `PR_METADATA_ARTIFACT status=ok bytes=<n> sha256=<digest> linked=<n> fallback=<n>`.
   - **Expected impact:** remove the repeated 265–359 second metadata failures.
   - **Fail-open:** linked context may be empty, but artifact schema and digest must still exist.

3. **Fix the missing workflow reference and fail fast**
   - Remove the stale `scripts/helper.sh` reference or restore the intended helper.
   - Log the workflow path, reference type, source line, and missing path as structured JSON.
   - **Expected impact:** eliminate the CI family’s observed 83.9% failure rate.

4. **Improve AI-memory failure diagnostics**
   - Twelve unique fail-open events contained only `source=shell`: six `record-candidate`, six `record-run-event`.
   - Add operation, helper, exit code, failure class, push attempt, and sanitized stderr category.
   - Continue fail-open, but alert only after repeated failure for the same operation.

5. **Separate contract-test MCP fallbacks from operational availability**
   - All 40 Semble fallbacks were `context=contract-test`, not production failures.
   - Exclude them from availability SLOs while retaining separate test coverage totals.

## AI Memory Health

- **Retrieval:** 12/12 retrieves selected records: **100% hit rate**.
- **Budget:** average estimated tokens **1,387.2 / 1,400**—99.1% utilization.
- **Keyword method:** `llm` 12, `plain` 0, `none` 0.
- **Zero-record retrieves:** 0.
- **Disabled retrieves:** 0.
- **Fail-open events:** 12 unique—six candidate writes and six run-event writes.
- **Push retries above one:** two events: run `35717770409` phase start and run `35772092715` phase failure.
- **Gap:** no `promote`, `compact`, or `finalize-task` telemetry appeared in the deep sample. Verify those operations emit telemetry, and log why 17 `write_lessons_learned` operations all wrote zero records.

## GH API Call Audit

- **Material data gap:** no dynamic per-endpoint call totals were collected. Static code contains many API call sites, but that is not evidence of runtime volume.
- **Confirmed hotspot:** release run `35768550507` repeated the same failed job-log lookup 236 times.
- **Existing positive hygiene:** `gh_helpers.sh` provides rate-aware retries, permanent-failure classification, and several GraphQL/batched helpers. No rate-limit events were observed.
- **Required instrumentation:** emit one sanitized line per wrapper invocation:
  `GH_API_CALL helper=<name> method=<method> endpoint=<normalized> attempt=<n> result=<result> status=<code> duration_ms=<n> cache=<state> rate_remaining=<n>`.
- **Batch/reuse recommendation:** cache poll-tick PR/issue metadata by number and head SHA, then reuse it across stall, conflict, and noop-suspicious sweeps.
- **Expected reduction:** over 95% for the E2E failed-log hot loop; broader poller savings require the missing runtime call telemetry.

## Prompt Cache & Memory System

- **Reported aggregate `cache_hit_rate`:** unavailable (`null`).
- **Derived weighted cache-read rate:** **80.55%**, using cache-read divided by prompt-plus-cache-read tokens.
- Individual review runs ranged from 62.0% to 88.2%; stable-release run `35768550507` reported 0%.
- Cache reads were **114.98M tokens**, versus only 158,932 cache-write tokens. Review/autofix reported no cache writes, suggesting incomplete provider creation telemetry.
- Twenty-five of 166 OpenRouter calls—15.1%—had unavailable usage.
- Preserve stable instructions and repository context at the prompt prefix; place timestamps, run IDs, comments, and volatile diffs afterward.
- Emit `prompt_prefix_sha256`, static/dynamic byte counts, model, and cache status per call to identify fragmentation.
- Memory retrieval is effective, but the near-full 1,400-token budget leaves little room for ranking variance; log candidate count before and after truncation.

## Orchestrator Health

- Orchestrate-poll: **85 runs, 83 successes, 0 failures, 2 cancellations; p50 305s, p95 608.4s**.
- Issue `#4290` was repeatedly identified as stalled in `ai:done`, but runs `35799020783` and `35799657783` safely skipped retriggering because PR `#4291` had a fresh push.
- Autofix sweeps correctly suppressed duplicate active runs for PRs `#4291` and `#4259`.
- Cancel-on-close runs warned that three active runs lacked PR linkage. Add one structured event containing run IDs, workflow paths, head branches, and attempted lineage sources.
- Track:
  - `ORCH_TICK_SUMMARY issues_scanned transitions dispatches skips api_calls`
  - `STALL_DECISION issue phase age action outcome`
  - `WAVE_PROGRESS tracking_issue wave active terminal deferred`
  - `CONFLICT_SWEEP prs_scanned fixed skipped duration_ms`

## Pipeline Flow Bottlenecks

| Stage | Observed behavior | Dominant overhead |
|---|---|---|
| Clarify | p50 1s; most runs skipped/no-op | Event filtering |
| Plan | p50 1s; occasional 701–1,398s runs | Compute outliers |
| Implement | p95 835.5s; active run `35782284543` took 2,377s | Model work |
| Review/autofix | p95 5,948s; six verified editor-contract failures | Reviewer compute, stalls, late failure |
| CI | p50 2,356s; 26/31 failures | Late deterministic gate |
| Validate | One 512s run | Sparse evidence |
| Orchestrate poll | p50 305s | Setup plus repeated state/API sweeps |
| Stable release | 6,093s | E2E waits and failed live-log polling |

Queueing is also material: several review runs were cancelled before their first step after 3,161–4,613 seconds. Add `RUN_QUEUE_TELEMETRY queued_ms runner_wait_ms concurrency_wait_ms cancelled_before_first_step superseded_by_run_id`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Review/autofix reviewer fan-out and 600-second stall detection.
- CI’s cross-reference check runs after approximately 40 minutes.
- Pollers pay repeated setup costs despite rarely using Semble.

**Top failure modes**
- Historical editor provider-key/isolation contract failure.
- Missing `linked_issue_metadata.json`.
- Missing `scripts/helper.sh` workflow reference.

**Highest-cost drivers**
- Review/autofix: 143.95M OpenRouter tokens.
- Six verified failed editor cycles: 97.12M tokens.
- Full six-reviewer `xhigh` configuration with disabled tiering.

**Top three actions**
1. Add and enforce the provider/broker credential contract before reviewer invocation.
2. Fix metadata artifact generation and move script-reference validation to the start of CI.
3. Enable bounded reviewer circuit breaking and lazy Semble setup.

## Metrics Appendix

### Outcomes

| Scope | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 345 | 43 | 16 | 596 | 7s | 2,386s |
| CI | 31 | 5 | 26 | 0 | 0 | 2,356s | 2,571s |
| Review/autofix | 105 | 74 | 16 | 11 | 4 | 287s | 5,948.4s |
| Orchestrate poll | 85 | 83 | 0 | 2 | 0 | 305s | 608.4s |
| Implement | 132 | 7 | 1 | 3 | 121 | 1s | 835.5s |
| Test and mark stable | 1 | 1 | 0 | 0 | 0 | 6,093s | 6,093s |

### AI Cost and Cache

| Metric | Value |
|---|---:|
| Codex tokens / calls | 1,326,191 / 13 |
| OpenRouter prompt tokens | 27,756,326 |
| OpenRouter completion tokens | 1,386,822 |
| OpenRouter cache-read tokens | 114,976,240 |
| OpenRouter cache-write tokens | 158,932 |
| OpenRouter total tokens | 144,112,594 |
| OpenRouter calls | 166 |
| Usage available / unavailable | 141 / 25 |
| Reported aggregate cache hit rate | unavailable |
| Derived weighted cache-read rate | 80.55% |
| Wall-clock p50 / p99 | 14,000ms / 6,668,400ms |
| Wall-clock samples | 116 |
| `BREAK_GLASS` | 0 |
| `CONTEXT_BUDGET_WARN` | 8 |

### MCP

| System | Queries | Bytes | Fallbacks | Runtime fallback | Contract-test fallback |
|---|---:|---:|---:|---:|---:|
| Semble | 28 | 280,975 | 40 | 0 | 40 |
| Serena | 0 | 0 | 0 | 0 | 0 |

| Target/system | Probe OK | Probe failed | Probe skipped | Note |
|---|---:|---:|---:|---|
| Serena, all targets | 0 | 0 | 0 | Disabled in deep logs; skipped probes not emitted |
| Semble, reviewer-context/overflow | N/A | N/A | N/A | Semble telemetry has query/fallback events, not probes |

**Other MCP servers observed:** none.

### GH API

| Signal | Value |
|---|---:|
| Dynamic total call count | unavailable |
| Observed rate-limit events | 0 |
| Repeated failed live-log calls, run `35768550507` | 236 |
| Runtime endpoint breakdown | unavailable |

**Coverage gaps:** one repository only; success sampling was 7%; deep folders covered 38 unique runs; dynamic GH API totals and full Serena availability telemetry were absent.

## Deep Audit — Workflows & Scripts (2026-09-23)

### Section 1: Bug & Correctness Sweep

Audit coverage: 50 workflows, 85 shell scripts, and 57 Python scripts. All YAML parsed, all Python files passed AST parsing, and all shell scripts passed `bash -n`.

#### SEC-001 — Untrusted issue body can inject job environment variables

- **File path and line range:** `.github/workflows/implement.yml:1590-1606`
- **Severity:** High
- **Category tag:** `security`
- **Description:** `ISSUE_BODY` and `ISSUE_TITLE` are user-controlled but are written to `$GITHUB_ENV` using the fixed delimiter `EOF`. A body containing `EOF` on its own line can terminate the value and inject additional environment assignments. The following comment incorrectly claims this prevents injection.
- **Recommended fix:** Prefer exporting only `ISSUE_BODY_FILE`. If the body must remain in the environment, generate a collision-checked delimiter, following `.github/workflows/plan.yml:461-472`, and add a regression test containing `EOF` plus a forged assignment.

#### BUG-001 — Workflow-reference validator treats comments as executable references

- **File path and line range:** `scripts/check_workflow_script_refs.py:43-52,78-94`; `.github/workflows/implement.yml:2681-2688`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** `EXPLICIT_REF.findall(text)` scans raw YAML, including comments. The sole reported missing path, `scripts/helper.sh`, appears only in an incident comment at `implement.yml:2685`; it is not executed or staged. This refines Reliability Improvement 3 in the existing report: the failing contract check is a false positive, not a missing runtime dependency.
- **Recommended fix:** Parse YAML scalar nodes and inspect only executable `run`, `uses`, and relevant `with` values, or strip YAML comments before regex matching. Add tests proving comments are ignored while real missing references still fail.

#### BUG-002 — Failure handler reads stale labels and can leave issues stuck in `ai:implementing`

- **File path and line range:** `.github/workflows/implement.yml:5300-5317,5352-5358`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** The cached `ISSUE_META_FILE` was captured before `ai:awaiting-approval` is replaced with `ai:implementing` at lines 1774-1777. The failure handler reuses that stale snapshot, so its `index("ai:implementing")` check is normally false and the issue is not restored to `ai:awaiting-approval`.
- **Recommended fix:** Refresh labels immediately before the failure-state transition and restore only when the live label set contains `ai:implementing`. On read failure, warn and leave labels unchanged. Add a test with stale cached labels and live `ai:implementing`.

#### SEC-002 — Almost all external actions use mutable tags or branches

- **File path and line range:** `.github/workflows/ci.yml:23`; `.github/workflows/clarify.yml:651`; `.github/workflows/review_autofix.yml:1661`
- **Severity:** Medium
- **Category tag:** `security`
- **Description:** Repository-wide inspection found 188 of 189 non-local `uses:` directives are not pinned to a 40-character commit SHA. Examples include `actions/checkout@v5`, `astral-sh/setup-uv@v7`, and `jlumbroso/free-disk-space@v1.3.1`. Mutable tags create supply-chain drift.
- **Recommended fix:** Pin third-party and official actions to full commit SHAs with version comments. Replace same-repository `@main` reusable-workflow calls with relative workflow references where supported. Automate updates through the existing release process.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — JSON fetch helper retries permanent failures and sleeps after the final attempt

- **File path and line range:** `scripts/gh_helpers.sh:610-663`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** Unlike `gh_retry` and `gh_retry_to_file`, `gh_api_json_to_file` never calls `_is_gh_permanent_failure`. A 404, 422, or permission failure therefore consumes up to five identical requests and all backoff sleeps; it also sleeps after the final failed attempt.
- **Current call count:** Up to 5 calls for a permanent error.
- **Proposed call count:** 1 call for permanent errors; retain the configured retry count for transient failures.
- **Recommended fix:** Reuse `_is_gh_permanent_failure` from `gh_helpers.sh:91-94` and guard every sleep with `attempt < max_attempts`.

#### API-002 — Clarify fetches the same comments twice

- **File path and line range:** `.github/workflows/clarify.yml:468-499`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** When semantic caching is enabled, the step first fetches 50 comments into `ISSUE_COMMENTS_FILE`, then performs a second paginated fetch of the same endpoint for `THREAD_HISTORY_FILE`.
- **Current call count:** 2 logical snapshot calls, with the second potentially issuing multiple pagination requests.
- **Proposed call count:** 1 paginated snapshot, locally deriving both the bounded 50-comment context and full history.
- **Recommended fix:** Fetch once through `gh_retry_to_file`, flatten the pages, then generate both artifacts from the cached JSON.

#### BATCH-001 — Body-fallback linked issues trigger one label read per issue

- **File path and line range:** `.github/workflows/review_autofix.yml:1151-1209,1213-1247`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** When linked issues come from PR body/title fallback, their `labels` field is `null`. The subsequent loop calls `gh issue view` once per issue. `[NEEDS VERIFICATION]`
- **Current call count:** N label reads for N fallback issues.
- **Proposed call count:** `ceil(N / 25)` GraphQL calls.
- **Recommended fix:** Batch-hydrate fallback issue labels using the alias pattern in `scripts/orchestrate_poll_process.sh:14483-14613`, then preserve per-issue REST only as a cache-miss fallback.

#### API-003 — No-op ancestry walks perform two sequential reads per hop

- **File path and line range:** `.github/workflows/implement.yml:4314-4332`; `scripts/orchestrate_poll_process.sh:12845-12897`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** Each hop fetches the current issue body and then the parent issue’s comments. The parent payload can include both its body and comments, allowing the next hop to reuse the body.
- **Current call count:** `2D` calls for depth D; defaults are 4 calls in implement and 6 in the poller.
- **Proposed call count:** `D+1`, or D when the initial issue body is already cached; defaults become 3/4 or 2/3.
- **Recommended fix:** Carry the fetched parent body into the next iteration and accept an optional initial-body cache. Extend the cycle-local cache pattern used by `_fetch_candidate_issue_details_graphql`.

#### API-004 — Generic retries can duplicate non-idempotent writes

- **File path and line range:** `scripts/gh_helpers.sh:430-494`; `scripts/security_audit.sh:1763-1774`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** `gh_retry` retries arbitrary commands, including issue creation and comment POSTs. If GitHub accepts a write but the response is lost, the wrapper can submit it again, creating duplicates. This is an inference from the retry semantics. `[NEEDS VERIFICATION]`
- **Current call count:** Up to 5 POST attempts for one intended write.
- **Proposed call count:** 1 POST, followed by at most 1 marker-based reconciliation GET on an ambiguous result.
- **Recommended fix:** Separate read/idempotent retries from non-idempotent creates. For issue/comment creation, use stable body markers and the upsert pattern already present in `scripts/review_merge_train.sh:254-291`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — No-op ancestry logic is duplicated

- **File path and line range:** `.github/workflows/implement.yml:4293-4332`; `scripts/orchestrate_poll_process.sh:12845-12897`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Both paths independently parse `Re-issued from #N`, inspect the same comment marker, apply the same depth validation, and fail open. Their defaults already differ, increasing drift risk.
- **Recommended fix:** Add `gh_count_reissued_noop_ancestors <repo> <issue> <max-depth> <marker> [initial-body-file]` to `scripts/gh_helpers.sh`. Replace both callers and implement API-003 within that helper.

#### DUP-002 — `_safe_gh_jq` is repeatedly reimplemented inline

- **File path and line range:** `.github/workflows/plan.yml:2048-2067`; `.github/workflows/implement.yml:5165-5179`; `.github/workflows/orchestrate.yml:1078-1095`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Three workflow blocks reproduce the temp-file/error-suppression implementation already owned by `scripts/gh_helpers.sh:564-594`.
- **Recommended fix:** Ensure the shared helper is staged before these steps and call existing `_safe_gh_jq <endpoint> [args...]`. Retain only a minimal fail-closed stub when helper staging genuinely fails.

#### DUP-003 — Support-file staging remains duplicated across large workflow blocks

- **File path and line range:** `.github/workflows/implement.yml:979-1336`; `.github/workflows/clarify.yml:224`; `.github/workflows/plan.yml:286`; `.github/workflows/orchestrate_poll.yml:373-584`; `scripts/stage_workflow_support.sh:714-983`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Multiple workflows maintain independent required/optional file lists and nearly identical source/fallback/install loops despite the existing manifest-driven staging helper.
- **Recommended fix:** Extend `stage_workflow_support.sh` with `implement --manifest <json> --workspace-root <path> --runtime-dir <path>`. Move phase-specific file lists into manifests and update implement, clarify, plan, orchestrate, orchestrate-poll, and clarify-respond callers.

No workflow pair exceeded the evidence threshold for a safe “more than 70% identical” consolidation; the internal wrappers differ primarily by contract and trigger predicate.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging expression is above the medium-risk threshold

- **File path and line range:** `.github/workflows/implement.yml:981-1336`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The interpolated `Stage workflow support files` run block is approximately **16,985 characters**, leaving **4,015 characters** before GitHub’s 21,000-character hard limit.
- **Recommended fix:** Extract the block through the manifest-driven `stage_workflow_support.sh implement` entrypoint proposed in DUP-003. Keep workflow expressions limited to environment wiring and one script invocation.

No interpolated run block exceeded 18,000 characters, and no large `if:` condition approached the limit. No workflow exceeded 800 KB; the largest was `review_autofix.yml` at 505,283 characters.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Critical implementation guard bypasses shared retry and notification helpers

- **File path and line range:** `scripts/implement_handle_guard_block.sh:13-77,86-182,212-313`; `.github/workflows/implement.yml:1028-1031`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The guard performs critical latch writes through raw `gh` and sends three near-identical raw Telegram requests. The workflow preserves only the guard script after cleanup, even though `gh_helpers.sh`, `label_helpers.sh`, and `tg_helpers.sh` were staged earlier. Transient API failures can therefore leave a failed issue without its intended blocking label.
- **Recommended fix:** Preserve the three helper files beside the runtime guard, source them, and use `gh_retry`, `ensure_label_exists`, and `tg_send_msg`. Keep the existing post-write verification.

#### DEAD-001 — Deprecated workflow inputs have no runtime consumers

- **File path and line range:** `.github/workflows/orchestrate_poll.yml:7-20`; `.github/workflows/workflow-log-analysis.yml:16-20`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `caller_workflow` and `codex_mode` are explicitly documented no-op compatibility inputs and are never read.
- **Recommended fix:** Preserve them until consumer wrappers are migrated, but record a deprecation version, emit a notice when callers explicitly pass them, and remove them only in a documented breaking release.

#### DEAD-002 — Several assignments are never consumed

- **File path and line range:** `scripts/review_run_reviewers.sh:755-761,3533-3549,4120-4128`; `scripts/orchestrate_poll_process.sh:19270-19291`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`, `REVIEWER_ATTEMPT_WD_REASON`, and `LINKED_PR_NUM` are assigned but have no repository consumer. Sourceable-script consumers remain possible. `[NEEDS VERIFICATION]`
- **Recommended fix:** Confirm no external sourced-call contract relies on them, then either remove the assignments or wire them into the intended telemetry/state output.

No `TODO`, `FIXME`, `HACK`, or `XXX` markers were found. Shellcheck found no SC2086, SC2046, SC2006, or SC2015 violations; remaining warnings were intentional glob matching, dynamic output variables, or the dead assignments above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | SEC-001, BUG-001, BUG-002 |
| Medium | 8 | SEC-002, API-001, BATCH-001, API-004, DUP-001, DUP-003, EXPR-001, CONSIST-001 |
| Low | 5 | API-002, API-003, DUP-002, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3–5 | Medium |
| API call optimization | 6–9 | Large |
| Code modularization | 7–10 | Large |
| Expression size reduction | 2–3 | Medium |
| Medium/Low fixes | 15–50 | Large |

## API Call Consolidation & Dead-Call Analysis (2026-09-23)

### Safety Tag Legend

`SAFE_TO_MERGE` is directly implementable; `NEEDS_VERIFICATION` requires the stated checks; `RISKY_SKIP` must not be automated because it touches pagination, polling, retry, race-defense, or orchestrator recovery semantics.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Consolidate review-blocked PR metadata into the existing GraphQL query

- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `scripts/review_rb_judge.sh:908-925`; `scripts/review_rb_judge.sh:933-966`
- **Current call count:** 2.
- **Proposed call count:** 1 on the normal path; retain one REST fallback only when GraphQL fails or is malformed.
- **Endpoints:** `GET /repos/{repo}/pulls/{pr}`; `POST /graphql` querying `repository.pullRequest`.
- **Evidence:**
  ```bash
  _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" ...)"
  ...
  RB_LINKED_ISSUES_GRAPHQL_JSON="$(gh_retry gh api graphql \
    ... pullRequest(number:$number) { baseRefName closingIssuesReferences(...) ... } ...)"
  ```
  The REST payload supplies state, merged status, title, body, and base ref; the immediately following GraphQL query reads the same PR for base ref and linked issues.
- **Proposed fix:** Extend the query assigned to `RB_LINKED_ISSUES_GRAPHQL_JSON` with `state`, `merged`, `mergedAt`, `title`, and `body`; derive the early guard, `PR_BASE_REF`, and fallback `PR_DATA` from that response. Keep the existing REST lookup as a fail-open fallback for absent or malformed GraphQL fields.
- **Safety rationale:** `NEEDS_VERIFICATION` because this replaces a REST read with GraphQL fields whose state vocabulary and failure handling differ, so identical error semantics are not statically proven.
- **Downstream signal:** Verify REST-to-GraphQL state/merged mapping, malformed-response fallback, the 50-linked-issue boundary, and behavior when repository-label creation between the current calls fails.

#### MERGE-002 — Post one clear-to-proceed auto-answer comment instead of two

- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/clarify.yml:1214-1218`
- **Current call count:** 2 POSTs.
- **Proposed call count:** 1 POST.
- **Endpoint:** `POST /repos/{repo}/issues/{issue}/comments`.
- **Evidence:**
  ```bash
  gh_retry gh api ".../comments" \
    -f body=$'The task appears clear...'

  gh_retry gh api ".../comments" \
    -f body=$'/answer [auto-answered-by-clarify]...'
  ```
- **Proposed fix:** Replace both calls with one body beginning `/answer [auto-answered-by-clarify]`, followed by the existing clear-to-proceed explanation.
- **Safety rationale:** `NEEDS_VERIFICATION` because consolidating two non-idempotent writes changes observable comment count and partial-failure behavior even though endpoint, auth, retry wrapper, and workflow step are identical.
- **Downstream signal:** Verify plan wrapper predicates still trigger, no comment scanner expects two separate comments, and simulated POST failure preserves the intended workflow failure behavior.

#### MERGE-003 — Fetch final-PR state and merged status together

- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/orchestrate_poll_process.sh:10230-10242` in `finalize_integration_merge_if_needed`
- **Current call count:** 2 on snapshot miss.
- **Proposed call count:** 1.
- **Endpoint:** `GET /repos/{repo}/pulls/{pr}`.
- **Evidence:**
  ```bash
  existing_pr_state="$(gh_retry _safe_gh_jq ".../pulls/${final_pr}" --jq '.state' ...)"
  existing_pr_merged="$(gh_retry _safe_gh_jq ".../pulls/${final_pr}" --jq '.merged_at != null' ...)"
  ```
- **Proposed fix:** Capture one PR JSON object and locally derive both fields.
- **Safety rationale:** `RISKY_SKIP` because the calls are inside `orchestrate_poll_process.sh` and protect a race-sensitive final-merge path; combining them also changes partial-read failure behavior.
- **Downstream signal:** Do not auto-implement; manually test snapshot mismatch, first-read-only failure, second-read-only failure, concurrent merge, and integration-branch drift behavior.

#### MERGE-004 — Fetch issue title and body together in reissue paths

- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/orchestrate_poll_process.sh:13731-13733` in `execute_stall_recovery_action`; `scripts/orchestrate_poll_process.sh:16413-16421` in `run_standalone_stall_recovery`; `scripts/orchestrate_poll_process.sh:21397-21399` in the implementation-failed sweep
- **Current call count:** 2 per reissue path; 6 if all three paths execute once.
- **Proposed call count:** 1 per path; 3 total.
- **Endpoint:** `GET /repos/{repo}/issues/{issue}`.
- **Evidence:**
  ```bash
  orig_title="$(gh_retry _safe_gh_jq ".../issues/${issue_num}" --jq '.title ...')"
  orig_body="$(gh_retry _safe_gh_jq ".../issues/${issue_num}" --jq '.body ...')"
  ```
- **Proposed fix:** At each site, fetch `{title, body}` once and parse both values locally.
- **Safety rationale:** `RISKY_SKIP` because every site is an orchestrator stall/recovery path explicitly covered by the race-defense exclusion.
- **Downstream signal:** Do not auto-implement; manually verify fail-open behavior when only one field is missing, concurrent issue edits, and every managed, standalone, and implementation-failed reissue branch.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Reuse cached issue payloads during integration-ref resolution

- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `scripts/resolve_integration_ref.sh:51-53,72-92`; `.github/workflows/implement.yml:127-130,169-174,498-502`; `.github/workflows/orchestrate_clarify_respond.yml:69-78,92-97,173-177`
- **Current call count:** Child resolution is 2 issue GETs instead of 1; tracking fallback can be 4 GETs instead of 2 when both payloads were already fetched.
- **Proposed call count:** 1 child GET and, when needed, 1 tracking GET.
- **Endpoint:** `GET /repos/{repo}/issues/{issue}`.
- **Evidence:**
  ```bash
  ISSUE_PAYLOAD="$(gh api ".../issues/${ISSUE_NUMBER}")"
  ```
  is cached before:
  ```bash
  child_body="$(get_issue_body "${ISSUE}")"
  tracking_body="$(get_issue_body "${tracking_issue}")"
  ```
- **Proposed fix:** Extend `resolve_integration_ref.sh` to accept optional `ISSUE_PAYLOAD_FILE` and `TRACKING_PAYLOAD_FILE`, validate each payload’s `.number`, and fall back to `get_issue_body` on absent, stale, or malformed input. Pass the existing files from `orchestrate_clarify_respond.yml`; persist and pass the precheck payloads from `implement.yml`.
- **Safety rationale:** `NEEDS_VERIFICATION` because reuse crosses workflow steps and could replace resolver-time live metadata with an earlier snapshot.
- **Downstream signal:** Define the accepted freshness point, then test matching caches, number mismatch, malformed files, missing files, tracking fallback, and preservation of the live branch-existence probe.

#### REUSE-002 — Reuse marker-comment bodies from the paginated merge-train scan

- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/review_merge_train.sh:255-283`; `scripts/review_merge_train.sh:354-381`; `scripts/review_merge_train.sh:464-486`
- **Current call count:** General existing-marker upsert uses 2 reads; the blocked gate path can use 3 reads.
- **Proposed call count:** 1 paginated read.
- **Endpoints:** `GET /repos/{repo}/issues/{pr}/comments?per_page=100`; `GET /repos/{repo}/issues/comments/{comment_id}`.
- **Evidence:**
  ```bash
  ... --paginate ".../issues/${pr}/comments?per_page=100" \
    --jq '... | .id'
  ...
  existing_body="$(gh_retry gh api ".../issues/comments/${existing_id}" --jq '.body' ...)"
  ```
  The blocked path also scans once before invoking `_mt_upsert_comment`, which scans again.
- **Proposed fix:** Replace `_mt_find_marker_comment_id` with a snapshot helper returning the latest `{id, body}`; let `_mt_upsert_comment` accept that snapshot and update the blocked-path caller to pass it through.
- **Safety rationale:** `RISKY_SKIP` because the source read is paginated and participates in concurrency-sensitive merge-train marker and label-claim logic.
- **Downstream signal:** Do not auto-implement; manually validate multi-page ordering, latest-marker selection, human label-removal bypasses, and concurrent gate/release invocations.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — modifies retry, permanent-failure, backoff, and rate-limit-sensitive wrapper semantics.
- API-002: `RISKY_SKIP` — the proposed source snapshot is paginated, requiring manual page-boundary and bounded-context review.
- BATCH-001: `NEEDS_VERIFICATION` — batching is sound, but label pagination completeness and REST fallback behavior require verification.
- API-003: `RISKY_SKIP` — includes pagination and an orchestrator recovery path that explicitly defends against races.
- API-004: `RISKY_SKIP` — directly changes retry behavior for non-idempotent writes and ambiguous accepted-response failures.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| `SAFE_TO_MERGE` | 0 | — |
| `NEEDS_VERIFICATION` | 3 | MERGE-001, MERGE-002, REUSE-001 |
| `RISKY_SKIP` | 3 | MERGE-003, MERGE-004, REUSE-002 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
