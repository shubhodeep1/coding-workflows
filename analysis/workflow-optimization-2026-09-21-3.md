## Executive Summary

- **Review commit preconditions are the largest avoidable loss.** Runs `35579063461`, `35586406493`, and `35594596407` spent **3.34 hours and 31.2M tokens** on PR `#4174`, then commit policy rejected ten staged edits because linked-issue metadata was unavailable. Preflight metadata before reviewers/editor; estimated impact: eliminate nearly all repeated cost and ~65 minutes per affected run. **Confidence: high.**
- **CI fails too late.** Seven runs failed after 18–20 minutes on the same `300 != 180` budget-contract assertion. Moving that 4-second test near workflow start would have saved about **2.2 hours** in this window. **Confidence: high.**
- **Release authorization is checked after the 85-minute E2E gate.** Run `35570966035` failed tagging `v1.29.7` because workflow-write permission could not be established. Add an early dry-run/attested permission preflight; potential saving: **~88 minutes per misconfigured release**. **Confidence: high.**
- **Review/autofix dominates AI cost:** **138.0M of 138.5M tokens (99.66%)**. Adaptive reviewer fan-out and context reduction offer an estimated **20–40M token reduction**, with full review retained for sensitive files. **Confidence: medium.**
- **Queue churn is material:** 25 review runs were cancelled, including 15 before their first step, representing **14.9 hours of elapsed queue/run time**. Same-head failure-signature suppression is safer than additional concurrency. **Confidence: high.**
- **Telemetry needs source authentication.** The collector reported 13 context warnings, but only six were direct emissions; seven were quoted inside an analysis report and recursively counted. **Confidence: high.**

## Speed Optimizations

1. **Critical path: validate review metadata before model startup**
   - **Evidence:** In run `35594596407`, the editor produced a 58,142-byte diff over ten files, all ten were staged, then `Commit changes` emitted `Refusing to commit: linked-issue metadata is unavailable.` Runs `35579063461` and `35586406493` ended identically.
   - **Root cause:** A deterministic commit-policy precondition is evaluated only after reviewers and editor execution.
   - **Exact change:** Immediately after `Collect PR metadata`, validate file existence, JSON validity, collection status, and required fields. Emit `REVIEW_METADATA_PREFLIGHT result=blocked reason=linked_issue_metadata_unavailable`; skip model execution.
   - **Savings:** Observed **3.34 hours and 31.2M tokens** across three runs.
   - **Risk:** Low; fail closed because metadata is correctness-critical.

2. **Critical path: move the CI budget contract forward**
   - **Evidence:** Seven CI runs (`35555535156`, `35556427518`, `35556433871`, `35556445315`, `35556456140`, `35558279036`, `35559614802`) failed on `tests/test_ci_poll_test_sharding.py` after most lint work. The failing test took 3.5–4.6 seconds.
   - **Root cause:** The contract test currently runs near the end of `.github/workflows/ci.yml`.
   - **Exact change:** Run only `tests/test_ci_poll_test_sharding.py` immediately after the test-subset derivation step.
   - **Savings:** Approximately **18–20 minutes per incompatible run; 2.2 hours observed**.
   - **Risk:** Low.

3. **Critical path: release permission preflight**
   - **Evidence:** `35570966035` completed E2E validation, then tag push was rejected because workflow-update permission could not be determined.
   - **Exact change:** Before E2E fan-out, test release-token capability using a non-mutating dry-run and emit workflow-files-changed/token-capability state. Fail before testing when required permission is known absent.
   - **Savings:** Up to **88 minutes** per configuration failure.
   - **Risk:** Low if the probe is non-mutating.

4. **Critical path: quarantine repeated deterministic review failures**
   - **Evidence:** Three PR `#4174` failures had the same commit guard and iteration metric; sweep run `35600880415` subsequently dispatched PR `#4174` again once no active run remained.
   - **Exact change:** Persist a fingerprint of `head_sha + root_failure_class + root_failure_reason`; suppress model reruns until the head or metadata changes.
   - **Savings:** One full review run per duplicate, typically **60–74 minutes** here.
   - **Risk:** Low with fail-open behavior if fingerprint state is unavailable.

5. **Micro-optimization: Semble query parallelism**
   - **Evidence:** Deep-dive overflow queries commonly took ~0.57–0.60 seconds each; run `35594596407` issued eight sequential overflow queries.
   - **Change:** Parallelize independent overflow-file queries with a small bounded worker count.
   - **Savings:** Roughly 3–4 seconds on similar runs.
   - **Risk:** Low; not a priority versus model and CI work.

## Cost Optimizations

1. **Eliminate post-model commit-policy failures**
   - **Evidence:** The three PR `#4174` failures consumed **31,203,707 tokens**, including 23,719,017 cache-read tokens.
   - **Change:** Apply the metadata preflight above and retry metadata collection, not the entire review ensemble.
   - **Estimated savings:** Nearly all 31.2M observed tokens.
   - **Quality risk:** None; models receive no useful work when commit is impossible.

2. **Enable adaptive reviewer quorum and health circuit breaking**
   - **Evidence:** Review/autofix used **144 calls and 138.0M tokens**. Selected runs used six reviewer slots; the same Gemini and GLM slots failed with unknown classification in all three commit-guard failures. Other slow runs repeatedly retried a Mistral slot for rate limits/server errors. Logs show `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`.
   - **Change:** Start with three reviewers for low-risk changes; expand on disagreement, parser failure, or sensitive paths. Enable the existing health circuit breaker in shadow mode first.
   - **Estimated savings:** **15–30% of review tokens, approximately 20.7–41.4M tokens**.
   - **Quality risk:** Medium; always retain full fan-out for workflows, security, contracts, migrations, and high-disagreement reviews.

3. **Compact before context reaches 70%**
   - **Evidence:** Six direct warnings in runs `35555531553`, `35560591197`, and `35571926267` reported 227K–234K prompt tokens and ratios of **86.7–89.2%**.
   - **Change:** Deduplicate prior-review, memory, and Semble text; enable conservative uninteresting-file filtering; trigger reduction at 70% rather than warning only.
   - **Estimated savings:** **10–20% of 24.7M review prompt tokens: 2.5–4.9M tokens**.
   - **Quality risk:** Low-to-medium; retain all changed executable and policy-sensitive files.

4. **Keep Semble, instrument usefulness**
   - **Evidence:** Semble made **82 queries / 703,565 bytes**, averaging 8.6KB. Runtime fallbacks were zero. In the deep-dive sample, 55/65 queries targeted overflow and ten reviewer context.
   - **Assessment:** Semble appears to bound full-file expansion rather than add large noisy context, but cannot be proven without replacement metrics.
   - **Change:** Add `selected_chunks`, `deduplicated_bytes`, `prompt_bytes_replaced`, `included`, and `latency_ms`.
   - **Savings:** Enables removal of low-value queries; present overhead is modest.
   - **Quality risk:** Low.

5. **Close usage-accounting gaps before price-based routing**
   - **Evidence:** 23/211 OpenRouter calls lacked usage data; review accounts for all 23 missing calls.
   - **Change:** Emit provider, model, latency, attempts, terminal class, and `usage_missing_reason`.
   - **Impact:** Makes dollar/model comparisons reliable.
   - **Quality risk:** None.

Serena recorded no queries, probes, or fallbacks and was explicitly disabled in observed review runs; no efficiency conclusion is possible.

## Reliability Improvements

1. **Preserve the first/root failure**
   - **Evidence:** Run `35555067924` first failed because `LINKED_ISSUE_METADATA_FILE` was unset, but its final structured summary classified the run as `editor_empty_noop`. The later always-run editor step masked the root cause.
   - **Fix:** Set an immutable `ROOT_FAILURE` record on the first non-zero step: phase, step, class, reason, exit code, timestamp. Include it in `REVIEW_AUTOFIX_RUN_SUMMARY_V1`.
   - **Impact:** Prevents incorrect automated remediation and alert wording.
   - **Rollback:** Logging-only.

2. **Classify commit guard failures separately from lost edits**
   - **Evidence:** Run `35594596407` still had ten modified/staged files when commit refused metadata; the later detector labeled this `changes_lost`.
   - **Fix:** Emit `COMMIT_GUARD_BLOCKED=true` and `COMMIT_BLOCK_REASON`; skip `EDITOR_CHANGES_LOST` classification unless the post-editor diff actually disappeared.
   - **Impact:** Avoids unnecessary re-dispatch and false human-intervention alerts.
   - **Rollback:** Preserve existing hard failure while changing only classification.

3. **Harden release push diagnostics**
   - **Evidence:** Run `35570966035` retried the deterministic `dst refspec stable matches more than one` error four times, then failed on tag permission.
   - **Fix:** Classify git push errors as permanent/transient and emit exact source/destination refs. The current checkout already uses `HEAD:refs/heads/${SOURCE_BRANCH}`; verify that fix is deployed to the stable workflow and retain its regression test.
   - **Impact:** Removes useless retries and isolates authorization from ref ambiguity.
   - **Rollback:** Fall back to existing retry path for unknown errors.

4. **Stop recursive telemetry ingestion**
   - **Evidence:** Seven `CONTEXT_BUDGET_WARN` lines in audit run `35571055909` were quoted evidence from older runs, yet were counted as current warnings.
   - **Fix:** Require telemetry immediately after the timestamp or a dedicated marker such as `TELEMETRY_V1`; reject lines with report text before the marker.
   - **Impact:** Correct warning/fallback rates and avoids self-amplifying reports.
   - **Rollback:** Maintain a temporary legacy-parser counter for comparison.

5. **Repair AI-memory authentication while retaining fail-open**
   - **Evidence:** Ten sampled memory events were fail-open. Six were `force-tick-get/put` failures across the three review failures. Run `35572408424` retried a memory push 16 times before an authentication error, but telemetry omitted retry count and reason.
   - **Fix:** Configure authenticated memory remotes before writes; emit `reason`, `exit_code`, `push_attempts`, `duration_ms`, repository/ref, and stderr category.
   - **Impact:** Converts masked systematic degradation into actionable data.
   - **Rollback:** Continue fail-open operation.

6. **Cancellation provenance**
   - **Evidence:** Audit run `35571055909` ended with a shutdown signal; its failure point had no step. The parent E2E path only captured an in-progress snapshot.
   - **Fix:** Trap TERM/INT and emit parent run, watcher run, active phase, elapsed time, deadline, and last checkpoint. Collector should classify `cancelled_by_signal`.
   - **Impact:** Distinguishes infrastructure cancellation from model failure.
   - **Rollback:** Logging-only.

`BREAK_GLASS` count was zero. The six validated context warnings indicate prompt-size pressure, not policy/rubric pressure. All 32 Semble fallbacks were contract-test events; runtime fallback count was zero, indicating healthy fail-open behavior rather than a broken rollout.

## AI Memory Health

- **Sample:** 66 telemetry events across 11 deep-dive runs.
- **Retrieve hit rate:** **10/10, 100%** with `records_selected > 0`.
- **Token use:** average **1,392.8 / 1,400 tokens (99.5%)**.
- **Keyword method:** `llm=10`, `plain=0`, `none=0`.
- **Zero-record retrieves:** none.
- **Disabled retrieves:** none.
- **Fail-open events:** **10/66 (15.2%)**—six force-tick reads/writes and four candidate/run-event writes.
- **Push retries:** successful telemetry reported mostly one attempt and at most two, but failure logs show up to 16 attempts; retry telemetry is incomplete.

**Recommendation:** Retain retrieval, but target 80–90% of budget through deduplication and reserve headroom for unusual tasks. Add complete error/retry fields to every fail-open event.

## GH API Call Audit

| Pattern | Evidence | Assessment / action |
|---|---|---|
| Review sweep | Run `35600880415`: four PRs, three active skips, one dispatch. Implementation uses one paginated PR snapshot plus three status snapshots for each of two workflows. | Good batching: seven baseline logical reads instead of per-PR queries. Keep. |
| Workflow-analysis E2E polling | Parent run `35570966035` polled child `35571055909` every five seconds for 120 seconds: 23 wait reads plus one final status read. | Reduce from ~24 to 5–8 calls using an initial delay and 15–30-second intervals; estimated 67–79% reduction. |
| Linked-issue collection | Review metadata is designed around one GraphQL call; downstream cache step reuses even an empty `[]` result. | Good API hygiene. Preserve and log cache reuse. |
| Failure handlers | PR state, comments, labels, force-tick, and notifications execute after failures. | Exact call count unavailable; consolidate around a shared failure-context JSON object. |
| Rate limiting | No GitHub rate-limit event appeared in selected logs. | Current risk appears low, but coverage is incomplete. |

**Required logging:** instrument `gh_retry`, `gh_retry_to_file`, `_safe_gh_jq`, and direct high-volume calls with `GH_API_CALL workflow=… step=… endpoint_class=… method=… attempt=… status=… ms=… items=… pages=… rate_remaining=… retry_class=…`. Emit one per-step `GH_API_SUMMARY`.

## Prompt Cache & Memory System

- Cache-read traffic was **112.1M of 138.5M total tokens (81.0%)**, showing substantial reuse.
- Aggregate `cache_hit_rate` is unavailable. Observed run-level values ranged from **46.6% to 84.8%**; audit/release runs reported 0%.
- Review cache-write tokens were zero; this may reflect provider reporting rather than absent cache creation.
- Same-PR failed runs improved from 46.6% to 84.8%, suggesting increasing prefix reuse, but still repeated large dynamic contexts.
- **Inference:** dynamic PR data, prior-review artifacts, run-specific metadata, and full file context are eroding otherwise useful stable prefixes.

**Changes:**
1. Keep policy, tools, rubric, and output schema in a stable prefix.
2. Append PR/run-specific data after the stable block.
3. Emit `prefix_hash`, static/dynamic bytes, cache-read/write tokens, model context, and miss reason per call.
4. Deduplicate memory and Semble content before prompt assembly.
5. Automatically compact at the 70% threshold.

Expected impact: **2.5–4.9M fewer prompt tokens**, lower latency, and fewer context-limit failures.

## Orchestrator Health

- `orchestrate_poll`: **39/39 successful**, p50 **295s**, p95 **427s**—reliable but long-running.
- Review sweep deduplication worked in `35600880415`, skipping three active branches and dispatching one.
- Clarify, plan, implement, and clarify-response produced many intentional 0–11-second skips. This is cheap but creates noisy run volume.
- Failure-heal recorded 61 skipped/other runs with no successful heal, making healthy ineligibility indistinguishable from never-eligible behavior.
- Forward merge run `35598962239` safely opened fallback PR `#4215` after conflicts in three workflow files.
- Repeated PR `#4174` commit-guard failures show that active-run dedup prevents concurrency but not deterministic reprocessing.

Add `ORCHESTRATOR_CYCLE_SUMMARY` with active projects, transitions, skip reasons, failure fingerprints, blockers and age, dispatches, API calls, sleep time, and next wake reason. Add `HEAL_SKIP reason=… source_run_id=… eligibility=…`.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify → plan → implement | Trigger/no-op fan-out | Most runs complete as skips in 1–11s | Add structured skip reasons; low optimization priority |
| Review/autofix | Model compute, deterministic reruns, queueing | p50 573s, p95 5,325s; 25 cancellations; 138.0M tokens | Metadata preflight, failure fingerprinting, adaptive quorum |
| CI | Expensive work before contract failure | Seven identical failures totaling 2.29h | Move budget contract to workflow start |
| Validate/release | Long E2E gate before permission check | `35570966035` failed after 5,327s | Release-token preflight |
| Orchestrator polling | Fixed multi-minute cycles | p50 295s | Emit cycle state, then short-circuit provably idle cycles |
| Merge/conflict | Workflow-file conflict resolution | Fallback PR `#4215` | Track blocker age and conflict-file recurrence |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix model work, 38-minute median CI, and release E2E.
- **Top failure modes:** linked-issue metadata commit guard, late CI timeout contract, release token permission, and cancellation ambiguity.
- **Highest-cost driver:** review/autofix—99.66% of OpenRouter tokens.
- **Top three actions:**
  1. Validate linked-issue metadata before reviewer/editor execution and preserve root-failure classification.
  2. Move the CI budget contract to an early fail-fast step.
  3. Add release authorization preflight plus structured GH API, cache, memory, and cancellation telemetry.

## Metrics Appendix

### Outcomes

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Failure rate | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1,000 | 395 | 13 | 28 | 564 | 1.3% | 7s | 3,098s |
| Review/autofix | 142 | 111 | 4 | 25 | 2 | 2.8% | 573s | 5,325s |
| CI | 46 | 39 | 7 | 0 | 0 | 15.2% | 2,270s | 2,536s |
| Orchestrate poll | 39 | 39 | 0 | 0 | 0 | 0% | 295s | 427s |
| Workflow analysis | 2 | 1 | 1 | 0 | 0 | 50% | 1,184s | 1,353s |
| Stable release | 1 | 0 | 1 | 0 | 0 | 100% | 5,327s | 5,327s |

### AI and Cache Telemetry

| Metric | Value |
|---|---:|
| Codex calls / tokens | 7 / 6,080 |
| OpenRouter calls | 211 |
| Usage available / unavailable | 188 / 23 |
| Prompt tokens | 25,192,932 |
| Completion tokens | 1,160,291 |
| Total tokens | 138,485,193 |
| Cache-read tokens | 112,138,668 |
| Cache-write tokens | 445,882 |
| Effective cache-read share | 81.0% |
| Aggregate `cache_hit_rate` | unavailable |
| Wall-clock p50 / p99 | 6,000ms / 7,370,000ms |
| Wall-clock samples | 117 |
| Break-glass | 0 |
| Context warnings, raw / validated direct | 13 / 6 |

### MCP Telemetry

| Server / target | Queries | Logged bytes | Fallbacks | Probe OK / failed / skipped | Notes |
|---|---:|---:|---:|---:|---|
| Semble, aggregate | 82 | 703,565 | 32 | n/a | 32 contract-test, 0 runtime |
| Semble, overflow deep-dive sample | 55 | sampled within 559,101 | 28 | n/a | All sampled fallbacks were contract tests |
| Semble, reviewer-context sample | 10 | sampled within 559,101 | 0 | n/a | Bounded retrieval |
| Serena, observed workflows | 0 | 0 | 0 | 0 / 0 / 0 | Disabled; availability not emitted |
| Other MCP servers observed | 0 | 0 | 0 | — | None |

### Material Data Gaps

- Full log telemetry exists for **121/1,000 runs**; successful-run sampling was 7%.
- Exact completed GH API call counts, pagination counts, and rate-limit remaining values are not emitted.
- Aggregate cache-hit rate and cache-miss reasons are unavailable.
- Serena-disabled runs emit no explicit skipped probe.
- Cancellation actor/superseding run is unavailable.
- Telemetry markers are not source-anchored, allowing recursive counting.

## Deep Audit — Workflows & Scripts (2026-09-21)

### Section 1: Bug & Correctness Sweep

Audit coverage: 50 workflows and 142 scripts. All workflow YAML parsed without duplicate keys; all shell scripts passed `bash -n`; all 57 Python scripts parsed successfully. No direct issue/PR title or body interpolation was found inside `run:` bodies.

#### SHELL-001 — Truncation pipelines can fail under `pipefail`

- **File path / lines:** `.github/workflows/plan.yml:1839-1844,1959-1964`; `scripts/review_conflict_prepare.sh:616-634`; `scripts/workflow_failure_heal_intake.sh:604-623`; `scripts/review_apply_fixes.sh:2251-2266`
- **Severity:** Medium
- **Category tag:** `shellcheck`
- **Description:** These strict-mode paths truncate data with producer-to-`head` pipelines such as `cat ... | head -c 10000` and `printf ... | head`. For sufficiently large input, the producer receives SIGPIPE and the pipeline returns non-zero under `set -euo pipefail`, aborting the step instead of merely truncating its diagnostic or memory payload.
- **Recommended fix:** Read files directly with `head -c N "$file"`. For variables, use `scripts/truncate_to_utf8_byte_cap.py` or write to a temporary file and invoke `head` directly. Use `sed -n '1,10p'` or arrays for line-limited diagnostics.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Review metadata collection bypasses the consolidated GraphQL helper

- **File path / lines:** `scripts/review_collect_pr_metadata.sh:209-226,251-269`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** The normal PR path performs four logical reads: PR metadata, issue comments, review comments, and linked issues. Enabling `REVIEW_BREAK_GLASS_ENABLED` adds a fifth reviews read. Pagination can increase the underlying request count.
- **Current / proposed calls:** 4 normally or 5 with break-glass → 1 primary GraphQL call.
- **Recommended fix:** Extend `gh_pr_with_all_comments` in `scripts/gh_helpers.sh:783-930` to return top-level reviews, `closingIssuesReferences`, and head-repository metadata in addition to its current metadata/comments fields. Preserve its REST fail-open fallback and populate all existing artifact files from the consolidated response.

#### API-002 — Clarify fetches the same issue comments twice

- **File path / lines:** `.github/workflows/clarify.yml:457-487`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** With semantic caching enabled, the step first fetches 50 comments into `ISSUE_COMMENTS_FILE`, then immediately fetches the same endpoint paginated at 100 per page for `THREAD_HISTORY_FILE`.
- **Current / proposed calls:** 2 logical reads → 1.
- **Recommended fix:** Fetch the paginated comments once with `gh_retry_to_file`; derive the first 50 comments for the bounded prompt artifact and render full thread history from the same cached JSON.

#### API-003 — Adjacent poller reads query identical resources twice

- **File path / lines:** `scripts/orchestrate_poll_process.sh:10230-10242,13731-13733,16413-16421,21397-21399`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** Four paths read the same PR or issue twice consecutively to obtain separate fields: state plus merged status, or title plus body.
- **Current / proposed calls:** 2 calls per reached site → 1 call per site.
- **Recommended fix:** Fetch an object once with `_safe_gh_jq`, such as `{state, merged: (.merged_at != null)}` or `{title, body}`, then extract both fields locally. Reuse `_fetch_pr_json` where the PR cache is already available.

#### BATCH-001 — Blocker-state checks perform one REST request per issue

- **File path / lines:** `scripts/orchestrate_poll_process.sh:21222-21243`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** Each implementation-failed item loops over `IF_BLOCKERS_JSON` and reads every blocker issue independently. This is the per-iteration API pattern prohibited by CLAUDE.md §15.
- **Current / proposed calls:** N blocker reads → `ceil(N / 25)` GraphQL calls, normally 1.
- **Recommended fix:** Pass `IF_BLOCKERS_JSON` to `_fetch_candidate_issue_details_graphql` at `scripts/orchestrate_poll_process.sh:14483-14613`, which already returns issue state in batches of 25. Missing cache entries can retain the existing `unknown` fail-open behavior.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Identical Semble query-section helper appears three times

- **File path / lines:** `scripts/review_apply_fixes.sh:909-918`; `scripts/review_conflict_prepare.sh:605-614`; `scripts/review_run_reviewers.sh:1760-1769`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** All three files define the same `append_semble_query_section` implementation.
- **Recommended fix:** Move it to `scripts/semble_helpers.sh` as `semble_append_query_section <label> <path> [max_bytes]`, then update the editor, conflict resolver, and reviewer callers.

#### DUP-002 — Workflow-analysis utilities are copied across Python scripts

- **File path / lines:** `scripts/analyze_workflow_logs.py:40-85`; `scripts/workflow_retro.py:50-99`; `scripts/collect_workflow_logs.py:96-108`; `scripts/cost_audit.py:283-295`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** `_parse_iso8601` is duplicated across four scripts; integer coercion, percentile calculation, and JSON loading are also duplicated across analysis and retro code.
- **Recommended fix:** Add `scripts/workflow_analysis_utils.py` with `parse_iso8601(value)`, `coerce_int(value, default=0)`, `percentile(values, pct)`, and `load_json(path)`. Import it from the four callers using the repository’s existing direct/package import fallback pattern.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block exceeds the medium-risk threshold

- **File path / lines:** `.github/workflows/implement.yml:930-1288`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The parsed YAML `run:` scalar is approximately **16,985 characters**, leaving about **4,015 characters** before GitHub’s 21,000-character limit. It contains three `${{ }}` interpolations and therefore falls within the template-expression limit.
- **Recommended fix:** Move this step into an `implement` mode in `scripts/stage_workflow_support.sh`, preserving the staged-support ledger and `$GITHUB_ENV` outputs. Keep the workflow step limited to validated environment setup and one script invocation.

No interpolated `run:` block exceeds 18,000 characters. The next-largest is approximately 14,392 characters. No large `if:` expression approaches the limit; the largest measured about 739 characters. No workflow exceeds 800 KB; the largest is `.github/workflows/review_autofix.yml` at 500,755 bytes.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Ledger variables are assigned but never consumed

- **File path / lines:** `scripts/review_issue_ledger.sh:67-104,866-919`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `line_end` is parsed and repeatedly assigned but signature generation only uses `line_start`. `CURRENT_FLOOR` is declared and populated but never read.
- **Recommended fix:** Remove `line_end` and `CURRENT_FLOOR` assignments to reflect current behavior and prevent future readers from assuming range-end or floor data affects ledger identity.

#### DEAD-002 — Branch-rebuild diagnostic state is discarded

- **File path / lines:** `scripts/orchestrate_poll_process.sh:9183-9259,9707-9711`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `BRANCH_REBUILD_SKIP_REASON` and `BRANCH_REBUILD_LAST_REBUILD_AT` are assigned but never read. Only `BRANCH_REBUILD_ESCALATED_ERROR` reaches the caller’s diagnostics.
- **Recommended fix:** Emit `BRANCH_REBUILD_SKIPPED reason=<...> last_rebuild_at=<...>` when preflight declines a rebuild, then clear the variables after logging.

#### CONSIST-001 — E2E API helper diverges from shared retry policy

- **File path / lines:** `scripts/comprehensive_test_and_release_gh_api.sh:3-47`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** `gh_api_safe` retries only when stderr contains “rate limit.” HTTP 5xx responses, connection resets, and timeouts fail immediately, unlike the reset-aware transient handling in `scripts/gh_helpers.sh`. This helper is widely used by release and dispatch-watcher polling.
- **Recommended fix:** Implement `gh_api_safe` as a compatibility wrapper over `gh_retry_to_file` or `gh_api_json_to_file`, retaining `GH_API_SAFE_OUTPUT` and quiet-mode behavior while using the shared retry classifier and backoff.

No `TODO`, `FIXME`, `HACK`, or `XXX` markers were found in scoped files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 5 | SHELL-001, API-001, BATCH-001, EXPR-001, CONSIST-001 |
| Low | 6 | API-002, API-003, DUP-001, DUP-002, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 4 | Medium |
| Code modularization | 9 | Medium |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 7 | Medium |
