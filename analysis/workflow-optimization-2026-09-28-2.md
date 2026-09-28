## Executive Summary

- **CI is the dominant operational failure pattern.** Nine of 14 CI runs were cancelled after 2,707–2,719 seconds; run 36383013856 ended during `lint / Orchestrate poll process unit tests`. Those nine runs account for approximately **6.8 runner-hours**. Prioritize test-progress diagnostics and enough runtime reduction to create headroom below the 45-minute limit. **Impact: high; confidence: high.**
- **Successful CI has almost no margin.** Run 36382913289 finished in 2,697 seconds, three seconds below the configured 45-minute limit. Its poll-process test step took 1,306 seconds; the promote-cycle test group took 326 seconds. Measure per-test and subprocess time before changing coverage. **Potential impact: minutes per CI run; confidence: high on the bottleneck, low on achievable savings.**
- **The sole reported failure was an authorization rejection, not a validation-test failure.** Validate run 36378523375 found a missing or ambiguous explicit-target PR binding. Subsequent cleanup steps also failed because checkout and runtime setup had not occurred. Preserve the fail-closed authorization check; make post-failure reporting safe before setup. **Impact: clearer diagnosis and fewer secondary errors; confidence: high.**
- **AI cost and cache savings cannot be priced from this window.** The assembled context reports 115 runs with log telemetry, but no measured model calls or tokens and no `cache_hit_rate`; only 15 logs were downloaded for deep dives. Instrument actual AI invocations before changing models or prompts. **Impact: enables cost decisions, savings unquantifiable; confidence: high.**
- **Semble fallback totals need qualification.** The collector reports 48 `SEMBLE_FALLBACK` occurrences, all CI contract-test fallbacks targeting `overflow`, with zero runtime fallbacks. Two runs contain both whole-job and per-step views of the same lines; the selected logs show **40 distinct test events**. Deduplicate before alerting. **Impact: fewer false rollout alarms; confidence: high.**

## Speed Optimizations

1. **Critical path — diagnose and shorten CI poll tests.** CI run 36382913289 spent 1,306 seconds in `lint / Orchestrate poll process unit tests`; run 36383013856 spent 1,274 seconds there before cancellation. Four-way sharding is **already enabled**, so adding it is not a new fix. Emit shard start, periodic completed-test count, slow subprocess/test timings, and a cancellation-safe final progress line. Then target measured slow fixtures without removing assertions. A **10% reduction in this step is about 2.1 minutes per run**; feasibility is unverified. Implementation risk: low for logging, medium for fixture changes.
2. **Critical-path cushion — profile the next test groups.** In successful CI run 36382913289, `Promote cycle and blocked-verdict unit tests` took 326 seconds and `Integration-ahead-by gate regression tests` took 175 seconds. Add per-test timing to the former and inspect repeated real waits before substituting controllable test clocks. A 25% reduction in the 326-second group would save approximately **82 seconds**, conditionally; risk: medium until timing-dependent behavior is verified.
3. **Workflow latency — separate wait from work.** `orchestrate_poll` had p50/p95 durations of 368/641 seconds across 10 runs; `review_autofix` had 13/1,412 seconds across 157. There are no selected execution traces for the long review runs, so their cause is unknown. Log queue, setup, model, retry, and merge/conflict spans before changing scheduling. Savings: **not estimable**; logging risk: low. Do not optimize the one-second skipped runs as though they were critical-path work.

## Cost Optimizations

1. **Avoid CI work that cannot finish.** The nine cancelled CI runs consumed approximately 6.8 runner-hours, though the logs do not establish why each was cancelled. Preserve the full gate, add progress/cancellation diagnostics, and investigate the measured test hotspots above. Recoverable runner time: **up to the affected work, not a guaranteed 6.8 hours**; quality risk: low for instrumentation, potentially high if tests are dropped.
2. **Measure model spend before selecting a cheaper model or reasoning level.** Review summaries for runs 36389722025 and 36389697549 show `openai/gpt-6-luna` configured for materiality and summarization, but recorded model calls and tokens are zero. Emit per-invocation model, reasoning level, phase, prompt/completion/cache tokens, and usage-availability status. Dollar savings and quality trade-offs **cannot yet be estimated**; do not step down models on configuration evidence alone.
3. **Measure context tools against downstream work.** Review run 36389722025 reports Semble enabled and Serena disabled, but this window has **zero measured `SEMBLE_QUERY` calls or logged query bytes**, and no Serena queries or response bytes. Thus neither reduced prompt expansion nor noisy low-value context can be established. Log target, request/response bytes, latency, whether the result entered the prompt, and downstream tool/model calls avoided. Savings: unknown; quality risk: low for bounded, redacted telemetry.

Repeated prompt expansion, cache-hit savings, context-budget pressure, and avoidable AI reruns likewise have no measured denominator here. Release run 36389862083 reports dispatching `test-and-mark-stable.yml` again after one failed attempt, despite its own `run_attempt=1`; log parent and dispatched-run IDs to price that cross-workflow retry rather than counting it as a GitHub run retry.

## Reliability Improvements

1. **CI timeout/cancellation — execution-budget category.** Nine CI cancellations cluster near 45 minutes, including runs 36383013856 and 36381450632 at 2,719 seconds; run 36382913289 succeeded at 2,697 seconds. Emit a warning when remaining job budget falls below the measured test-step p95, plus shard progress and cancellation cause. Optimize only verified slow tests; retain the 45-minute setting and full suite as the rollback baseline. Expected impact: potentially fewer incomplete CI gates, **rate reduction unmeasured**.
2. **Validate failure cascade — pre-setup cleanup category.** In run 36378523375, `Authorize explicit validation target` rejected a missing/ambiguous PR binding. Later steps reported absent `validate_process.sh` and memory helpers, an empty runtime path, and artifact scanning under `/boot/efi`. Keep authorization fail-closed. For `always()` steps, distinguish `not_initialized` from validation failure, require the runtime directory before writing status, and upload only a known prepared artifact path. Expected impact: removal of misleading secondary errors; rollback: restore existing reporting steps without relaxing authorization.
3. **Test-only MCP fallback noise — classification category.** CI run 36383013856 logged four distinct `SEMBLE_FALLBACK target=overflow` events from `lint / Targeted file context contract tests`, deliberately using a missing executable; the collector counted eight through duplicate log views. Classify by `context=contract-test` and deduplicate by run, step, and event identity. Alert separately on runtime fallbacks. Expected impact: more trustworthy rollout alarms; retain the existing fail-open behavior. **No probe-availability failure or broken runtime rollout is established.**

Measured `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are zero in parsed coverage. That is **not** proof of absent policy pressure or prompt-size risk in unselected AI runs.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` entries were found in the selected deep-dive logs. Retrieve hit rate, average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, `fail_open`, `enabled=false`, and memory push-retry rates are therefore **unavailable**, not zero. The summary of clarify run 36389844224 reports ledger `push_attempts: 3`, but does not identify a memory operation or cause. Verify emission for `retrieve`, record, finalize, promote, compact, and processed-command operations on **executing** AI runs; retain operation, budget, selected-record count, keyword method, enabled/fail-open flags, and push attempts without record contents.

## GH API Call Audit

- **Measured calls are incomplete.** Validate run 36378523375 executed one *paginated PR-list operation* in `Authorize explicit validation target`; the number of HTTP pages is unavailable. Record sanitized endpoint template, page count, latency, status class, retry count, and rate-limit category. Do not cache away its independent authorization binding.
- **Instrument a plausible N-call hotspot before redesigning it.** `scripts/orchestrate_poll_process.sh` lists standalone open PRs, then conditionally reads each candidate’s full PR payload for mergeability. If N candidates are inspected, that path entails N per-PR reads in addition to the list; **N was not observed in poller logs**. Log candidate count and actual reads. If field and freshness parity can be verified, reuse a cycle-local batched result; theoretical reduction is **N − batched-call count**, with no defensible window-wide estimate yet.
- **Preserve existing hygiene.** The poller already prefetches stalled linked-PR state in batches of 25 and uses cycle-local caches. Follow `CLAUDE.md` §15: extend an existing fetch before adding calls, and retain the smallest safe fallback on cache misses. Log batch hit/miss and fallback reads rather than recommending another unmeasured prefetch. Rate-limit events and endpoint totals were not supplied.

## Prompt Cache & Memory System

`cache_hit_rate` is **unavailable**; recorded prompt, cache-write, and cache-read tokens are all zero with no measured model calls. There is no basis to diagnose unstable-prefix fragmentation, semantic-cache effectiveness, or token savings. Add per-invocation cache-read/write and prompt-token telemetry plus a non-content prefix/version identifier; compare misses by phase and prefix before moving dynamic context after stable instructions. Log `CONTEXT_BUDGET_WARN` with phase and prompt/window ratio when it occurs. Keep cache misses and retrieval failures fail-open. Token, latency, and reliability gains remain **unquantifiable until covered AI invocations are sampled**.

## Orchestrator Health

Across the window, `clarify`, `plan`, and `implement` respectively had 170/178, 172/178, and 167/174 skipped runs; `orchestrate_clarify_respond` was skipped in all 175. Recent plan run 36390064979 has an empty log archive, so skips cannot be classified as healthy event filtering versus lost work. Emit a low-cardinality skip reason and correlation ID **before job-level gates where possible**.

Review summaries show `AUTOFIX_GATE_SKIP reason=claude_fixer_awaiting_session` for PRs 4546 (run 36389722025) and 4604 (run 36389714300). Record wait age and the event that resumes each PR; a successful gate run is not evidence that review advanced. Clarify run 36389844224’s three ledger push attempts warrant retry-cause logging. Wave progression, deferrals, conflict-heal attempts, and terminal-state ages were not provided; track counts and age by state before declaring a stuck-loop pattern.

## Pipeline Flow Bottlenecks

| Stage or overhead | Observed signal | Next diagnostic or fix |
|---|---|---|
| Clarify → plan → implement | Predominantly skipped; successful clarify examples 36389846114/36389844224 took 131/124 seconds; slow plan run 36375309681 took 538 seconds; implement run 36375721209 took 1,022 seconds. | Correlate issue and dispatch IDs; log gate reason, queue/start, and productive-stage timings. |
| Review/autofix | p95 **1,412 seconds**, versus p50 **13 seconds**; awaiting-session gate skips are observed. | Separate short gate exits, active review/model time, session wait, and conflict/merge time. |
| Validate | Run 36378523375 failed authorization in **10 seconds**, before checkout. | Keep the guard; log sanitized binding result and suppress setup-dependent cleanup failures. |
| CI/compute | CI p50/p95 **2,717/2,719 seconds**; nine cancellations. | Highest end-to-end priority: shard heartbeat and measured test-runtime reduction without losing coverage. |
| Queue/retry/merge | No queue-time or merge-conflict totals; one cross-workflow release redispatch is summarized in run 36389862083. | Emit linked-run IDs and separate queued, retry-backoff, conflict-heal, and compute spans. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** CI’s 45-minute boundary, with a roughly 21–22-minute poll-process test step in runs 36383013856 and 36382913289. **Top failure modes:** nine CI cancellations and one fail-closed validate authorization rejection with noisy post-failure cleanup. **Highest-cost drivers:** cancelled runner time; AI token spend is unmeasured.

**Prioritized actions:** (1) add CI shard heartbeats and per-test timings, then reduce verified slow fixtures; (2) make pre-checkout validate diagnostics and artifact handling safe while preserving authorization; (3) instrument actual AI usage, memory operations, API calls, and gate-skip reasons before altering model, cache, or dispatch behavior.

## Metrics Appendix

Window: **September 28, 2026, 03:50:42–07:08:55 UTC**, one repository. Collector summary has 1,000 rows but **998 unique run IDs**; runs 36383769890 and 36376203750 appear twice. Rates and duration percentiles below use the supplied row-based aggregates, not an invented deduplicated percentile.

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Success / failure rate, all rows | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---|---|
| All workflows | 1,000 | 262 | 1 | 16 | 721 | 26.2% / 0.1% | 1 / 437 s |
| CI | 14 | 5 | 0 | 9 | 0 | 35.7% / 0% | 2,717 / 2,719 s |
| Review/autofix | 157 | 153 | 0 | 2 | 2 | 97.5% / 0% | 13 / 1,412 s |
| Orchestrate poll | 10 | 10 | 0 | 0 | 0 | 100% / 0% | 368 / 641 s |
| Validate | 1 | 0 | 1 | 0 | 0 | 0% / 100% | 10 / 10 s |

| Telemetry, assembled context | Value | Coverage caveat |
|---|---:|---|
| Runs with parsed log telemetry | 115/1,000 | Source `summary.json` has **15 downloaded/parsed deep-dive logs**; assembled context includes additional parsed recent runs. |
| Codex calls / tokens; OpenRouter calls / prompt / completion / total tokens | 0 / 0; 0 / 0 / 0 / 0 | Observed in covered logs; **not** a spend estimate for all runs. |
| OpenRouter cache-write / cache-read tokens; `cache_hit_rate` | 0 / 0; **N/A** | No measured invocation denominator. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 2,719,000 | 113 assembled samples; **run-wall samples, not model-call latency**. Deep-dive-only 15-sample p50 is 2,717,000 ms. |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | Parsed coverage only. |
| GH API calls / rate-limit events | **N/A / N/A** | One paginated validation PR-list operation observed; page and aggregate call counts unavailable. |
| Semble queries / logged query bytes | 0 / 0 | No prompt-reduction rate measurable. |
| Semble fallbacks | 48 collector occurrences; **40 distinct selected-log events** | All `context=contract-test`, target `overflow`; runtime fallbacks 0. Query-based fallback rate undefined. |
| Serena queries / response bytes / tool calls / query ms | 0 / 0 / 0 / 0 | No per-tool usage breakdown beyond **none observed**. |
| Serena fallbacks; probe OK / failed / skipped | 0; 0 / 0 / 0 | Availability was not probed in selected logs. |

| MCP target availability in selected logs | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Semble `overflow` | 0 | 0 | 0 | Contract-test fallback target; **no probe result**. |
| Serena — no target observed | 0 | 0 | 0 | No availability result; review summaries report Serena disabled. |

**Other MCP servers observed:** none in the selected logs or supplied summaries. Tool-byte efficiency, memory retrieval effectiveness, queue-time distribution, actual GH API volume, and AI-token totals require collection from executing runs.

## Deep Audit — Workflows & Scripts (2026-09-28)

### Section 1: Bug & Correctness Sweep

The audit covered all 52 `.github/workflows/*.yml` files and 159 top-level shell/Python scripts. All workflows parsed as YAML. The findings below concern paths not already covered by the report’s CI-timing, validate-cleanup, and standalone-poller API findings.

- **BUG-001** — `.github/workflows/review_autofix_sweep.yml:163-214,254-301` · **Severity:** High · **Category:** `bug`. **Description:** Each active-run API request ends in `|| true`. A failed request is therefore indistinguishable from an empty status, and the sweep can dispatch another review for a branch with an active run. **Recommended fix:** Preserve a success flag for *each* status/workflow snapshot; skip dispatch for affected branches when any required snapshot is incomplete, and log the failed status.

- **BUG-002** — `scripts/review_merge_train.sh:125-136,148-163` · **Severity:** High · **Category:** `bug`. **Description:** `_mt_pr_files_into` records only `.[].filename`. Its diff-based path records both sides of a rename, but the REST fallback and older-PR lookup omit `previous_filename`; an edit to the old path can escape the overlap gate. **Recommended fix:** Include both non-null filenames in the cached REST result and test rename-versus-edit blocking.

- **SEC-001** — `scripts/gh_helpers.sh:458-464,485-490,525-530,552-558`; caller `.github/workflows/review_autofix.yml:5053-5069` · **Severity:** High · **Category:** `security`. **Description:** On failure, `gh_retry` prints its entire argument list, including a `-f body=` argument. The review caller builds that body from `EDITOR_SUMMARY_FILE`; a rejected POST can consequently copy its contents into logs. **Inference:** Sensitive or multiline content in that summary could be exposed or interpreted as separate workflow-command lines. **Recommended fix:** Log the operation and sanitized endpoint, never body-bearing argument values; pass large comment bodies with `--input` or `-F body=@file`. Apply workflow-command escaping to remaining untrusted diagnostics. `[NEEDS VERIFICATION]`

- **BUG-003** — `scripts/tg_helpers.sh:314-374,383-445` · **Severity:** Medium · **Category:** `bug`. **Description:** Both cleanup functions delete comments while incrementing offset-based `page`. After deletion, comments originally on page 2 can move to page 1 and be skipped. **Recommended fix:** Collect all matching comment IDs from a complete paginated snapshot before deleting any, then process that snapshot; test more than 100 matching comments.

- **BUG-004** — `scripts/claude_issue_intake.sh:260-280`; `scripts/claude_pr_sweep.py:146-166`; `scripts/claude_issue_queue_watchdog.sh:59-79`; `scripts/claude_issue_route.py:1107-1127` · **Severity:** Medium · **Category:** `bug`. **Description:** Four open-queue reads take only `per_page=100`. If the labeled queue exceeds one page, intake and sweep can miss an existing item, while pickup and watchdog can miss pending or stale items. The supplied evidence does not establish that this threshold was reached. **Recommended fix:** Put complete pagination and array validation in `fetch_open_queue`, reuse it across callers, and refuse deduplication decisions when the read is incomplete. `[NEEDS VERIFICATION]`

- **BUG-005** — `scripts/review_merge_train.sh:402-407,423-449` · **Severity:** Medium · **Category:** `bug`. **Description:** Release deduplication checks only the first 100 repository-wide Actions runs. An older still-active review outside that page is absent from `inflight_review_branches`, permitting a second dispatch; a failed lookup also explicitly clears the guard. **Recommended fix:** Read complete status-filtered review-run pages and retain an “unknown” result on failure that defers release dispatch. Preserve the cycle-local snapshot rather than querying per PR. `[NEEDS VERIFICATION]`

- **BUG-006** — `scripts/tg_helpers.sh:135-145,349-369,418-440` · **Severity:** Medium · **Category:** `bug`. **Description:** `tg_delete_msg` suppresses every deletion failure; cleanup then deletes the GitHub tracking comment regardless. A failed Telegram deletion therefore loses the ID needed for retry. **Recommended fix:** Check HTTP and Telegram `ok` results, retain the corresponding tracking marker on failure, and emit a bounded warning while keeping the workflow fail-soft.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **logical calls per execution path**, not measured HTTP totals; pagination and retries can add requests. The existing report already identifies standalone-poller per-PR mergeability reads, so they are not repeated here.

- **API-001** — `scripts/gh_helpers.sh:610-656,678-725` · **Severity:** Medium · **Category:** `api-redundancy`. **Description:** `gh_api_json_to_file` does not use the permanent-error check present in `gh_retry`; `curl_gh_api` likewise retries non-rate-limited HTTP errors, including permanent 404s. Both also sleep after the final failed attempt. **Current → proposed:** up to **5 → 1** requests for a permanent failure. **Recommended fix:** Extend `_is_gh_permanent_failure`/`gh_retry`’s classification to these helpers, classify curl responses by status, and sleep only when another attempt remains.

- **API-002** — `scripts/gh_helpers.sh:1219-1247,1338-1367`; caller `.github/workflows/review_autofix.yml:6523-6542` · **Severity:** Medium · **Category:** `api-redundancy`. **Description:** On the editor-changes-lost path with no peer, two branch-scoped reads of the same Actions-runs endpoint occur after the same wait: one for the peer check and one for the retry budget. **Current → proposed:** **2 → 1** snapshot reads. **Recommended fix:** Return the snapshot and read-status from a shared helper, then apply the two existing predicates locally, preserving their different failure decisions (peer check fail-open; budget check fail-closed). Verify freshness requirements before reuse. `[NEEDS VERIFICATION]`

- **BATCH-001** — `scripts/review_merge_train.sh:123-136,190-230` · **Severity:** Medium · **Category:** `api-batching`. **Description:** `_mt_blockers_for_into` calls `_mt_pr_files_into` inside its older-PR loop. The cache avoids repeats, but up to `MERGE_TRAIN_MAX_OLDER_PRS` (default 20) distinct PRs still require individual paginated file reads. **Current → proposed:** **1 PR-list + K file reads → 1 PR-list + `ceil(K/25)` batched reads**, plus fallbacks, for K distinct inspected PRs. **Recommended fix:** Extend the aliased GraphQL pattern used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`; populate `_MT_FILES_CACHE` once and retain REST fallback for incomplete file lists or rename-field parity. `[NEEDS VERIFICATION]`

- **BATCH-002** — `scripts/workflow_failure_heal_pr_reconcile.sh:109-110,239-274` · **Severity:** Medium · **Category:** `api-batching`. **Description:** After one heal-issue list, the reconciliation loop issues a separate open-PR lookup for each matching heal issue. **Current → proposed:** **1 + H → 1 + `ceil(H/25)`** reads for H matching issues, plus misses. **Recommended fix:** Batch head-branch lookups using aliases following `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`; retain the existing per-issue REST lookup on missing or incomplete batch entries. Verify head-repository and base-ref parity before acting. `[NEEDS VERIFICATION]`

- **BATCH-003** — `scripts/claude_issue_queue_watchdog.sh:59-80` · **Severity:** Medium · **Category:** `api-batching`. **Description:** The stale-item loop makes one label POST per item. **Current → proposed:** **1 queue read + 1 label-ensure attempt + N label writes → 1 queue read + 1 label-ID read + `ceil(N/25)` aliased mutations**, with per-item fallbacks. **Recommended fix:** Carry issue node IDs through `queue_stale`, batch `addLabelsToLabelable` mutations using the alias pattern in `scripts/orchestrate_poll_process.sh`, and retain individual writes for failed aliases. Validate permissions and partial-mutation behavior first. `[NEEDS VERIFICATION]`

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — `.github/workflows/mark-stable.yml:665-714`; `.github/workflows/test-and-mark-stable.yml:5650-5699` · **Severity:** Medium · **Category:** `duplication`. **Description:** Both release workflows define the same tag-publication retry and remote-verification function inline. **Recommended fix:** Move it to `scripts/release_tag_helpers.sh` as `publish_tag_with_remote_verification <tag_ref> <immutable|moving>`, preserving the existing caller variables and verification behavior; update both workflow steps to source the helper.

- **DUP-002** — `scripts/tg_helpers.sh:155-205,227-278,314-445` · **Severity:** Medium · **Category:** `duplication`. **Description:** General and phase-tagged message tracking duplicate comment lookup/upsert logic; their cleanup functions duplicate pagination and deletion logic, including BUG-003. **Recommended fix:** Keep public entrypoints, but delegate to `_tg_store_tracking_id <issue> <marker> <msg_id>` and `_tg_cleanup_tracking_comments <issue> <phase-or-empty>` in `tg_helpers.sh`. Update all four callers and test both marker formats.

- **DUP-003** — `.github/workflows/workflow-log-analysis.yml:230-264,718-747,1405-1433,1917-1945` · **Severity:** Medium · **Category:** `duplication`. **Description:** Four jobs repeat Codex package persistence and cache restoration shell blocks. **Recommended fix:** Add `scripts/codex_cache_helpers.sh` with `codex_cache_persist <tool_cache_dir>` and `codex_cache_restore <tool_cache_dir>`; update the four jobs’ shell callers while retaining their existing cache actions, keys, and install gates.

### Section 4: Expression Size Limit Risk Assessment

The 767 parsed `run` scalars include 225 containing `${{ }}`. Counts below use the **YAML-parsed run body**, excluding indentation removed by YAML parsing; runtime substitutions can change the final length.

- **EXPR-001** — `.github/workflows/implement.yml:986-1342` · **Severity:** Medium · **Category:** `expression-limit`. **Description:** “Stage workflow support files” has a parsed body of approximately **16,985 characters** with three interpolations: approximately **4,015 characters** of headroom against the specified 21,000-character limit. Its indented source occupies approximately 20,341 characters, which is *not* the parsed body length. **Recommended fix:** Extract the staging body to a staged `scripts/` helper, pass the three expression values through step `env:`, and retain the self-repository support-ledger behavior.

No parsed expression-bearing `run` body exceeds 18,000 characters; the next largest is `.github/workflows/implement.yml:3223-3529` at approximately 14,392. No workflow exceeds the requested 800 KB warning threshold. **A stricter repository contract also applies:** `review_autofix.yml` is 451,634 bytes, leaving 28,366 bytes before the documented 480,000-byte CI guard (`agents.md:583-605`); the documentation states a 512,000-byte runtime limit rather than the request’s 1 MB figure. No large `if:` expression approached the specified 21,000-character threshold in this sweep.

### Section 5: Cross-Cutting Concerns

- **CONSIST-001** — `scripts/tg_helpers.sh:169-179,194-205,240-250,262-278` · **Severity:** Medium · **Category:** `consistency`. **Description:** Tracking-comment GETs use `curl_gh_api`, but their POST/PATCH writes use raw `curl -s ... || true`. An HTTP error can therefore be treated as a successful transport command without recording the message ID, unlike the helper’s status-aware path. **Recommended fix:** Route those writes through `curl_gh_api`, check the result, and log a bounded fail-soft warning on unsuccessful tracking.

- **DEAD-001** — `scripts/orchestrate_poll_process.sh:19483-19512` · **Severity:** Low · **Category:** `dead-code`. **Description:** `LINKED_PR_NUM` is initialized and assigned during linked-PR reconciliation but is not read elsewhere in the script; the subsequent JSON uses `PR_STATE` and `PR_MERGED`. **Recommended fix:** Confirm there is no sourced-caller contract, then remove the write-only assignments or put the number into the intended diagnostic output.

- **SHELL-001** — `scripts/orchestrate_poll_process.sh:9262-9267` · **Severity:** Low · **Category:** `shellcheck`. **Description:** ShellCheck flags `local now_epoch="$(date +%s)"` as SC2155: `local` masks a failed command substitution, leaving later arithmetic with an invalid timestamp. **Recommended fix:** Declare `now_epoch` separately, check the `date` assignment, and take the function’s documented fail-open path if it fails.

- **DEBT-001** — `.github/workflows/ci.yml:1058-1063` · **Severity:** Low · **Category:** `tech-debt`. **Description:** CI invokes ShellCheck at `--severity=error`, so warning-level diagnostics—including SC2155 above—do not gate changes. **Recommended fix:** Triage the current warnings, explicitly suppress intentional patterns at their sites, then raise this step to `--severity=warning`.

No literal `TODO`, `FIXME`, or `HACK` markers were found in the scoped workflow and script files. The validate pre-setup cleanup cascade and the standalone-poller N-read candidate are intentionally left to their existing report sections.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, BUG-002, SEC-001 |
| Medium | 14 | BUG-003, BUG-004, BUG-005, BUG-006, API-001, API-002, BATCH-001, BATCH-002, BATCH-003, DUP-001, DUP-002, DUP-003, EXPR-001, CONSIST-001 |
| Low | 3 | DEAD-001, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3–5: sweep workflow, merge-train script, API helper, review caller, tests | Medium |
| API call optimization | 5–8: API helpers, review caller, batching scripts, tests | Large |
| Code modularization | 4–6: release workflows, Telegram helper, analysis workflow, new helpers | Medium |
| Expression size reduction | 2–3: implement workflow, staged script, contract tests | Medium |
| Medium/Low fixes | 6–9: queue readers, cleanup helper, poller, CI gate, tests | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-28)

### Safety Tag Legend

`SAFE_TO_MERGE` is ready for implementation without further review. `NEEDS_VERIFICATION` requires the stated checks first. `RISKY_SKIP` identifies a possible saving in a protected path and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — `RISKY_SKIP`** — `scripts/orchestrate_poll_process.sh:13934-13936` (`execute_stall_recovery_action`) and `scripts/orchestrate_poll_process.sh:21610-21612` (implementation-failed reissue loop). **Current → proposed:** two reads → one successful read per reissue path. **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}`. **Evidence:** each path reads the same issue twice, once for `.title` and once for `.body`, with no mutation between those reads. **Proposed fix:** capture one full issue response in each path, then extract both fields locally; retain a per-field fallback if preserving the present independent fail-open results requires it. **Safety rationale:** both paths perform recovery or reissue work inside `orchestrate_poll_process.sh`, an explicit `RISKY_SKIP` trigger; a single failed read could otherwise erase a field that the second read would have recovered. **Downstream signal:** Do not auto-implement. Manually review reissue behavior under independently failed title/body reads and concurrent issue edits before changing either path.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — `RISKY_SKIP`** — `scripts/review_merge_train.sh:257-261,275-287`; the no-prelooked-ID caller is at `scripts/review_merge_train.sh:482-486`. **Current → proposed:** two reads → one successful paginated read when `_mt_upsert_comment` performs its own lookup; calls that supply only an ID retain their existing behavior. **Endpoints:** `GET /repos/{owner}/{repo}/issues/{pr}/comments` and `GET /repos/{owner}/{repo}/issues/comments/{comment_id}`. **Evidence:** `_mt_find_marker_comment_id` filters comment `.body` to return its ID, after which `_mt_upsert_comment` fetches that comment’s `.body` again to decide whether a PATCH is needed. **Proposed fix:** extend `_mt_find_marker_comment_id` to return the selected ID *and body* from its existing response, and let `_mt_upsert_comment` use the body when available; preserve the point GET for ID-only callers. **Safety rationale:** the first read uses `--paginate`, an explicit `RISKY_SKIP` trigger, and replacing the later read also changes the freshness of the body comparison. **Downstream signal:** Do not auto-implement. Manually verify page-boundary selection, multiline-body transport, lookup-failure behavior, and concurrent comment edits before reusing the listed body.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — The proposed saving changes retry/backoff behavior, not overlapping data reads; review permanent-error classification manually.
- API-002: `NEEDS_VERIFICATION` — Confirm snapshot freshness and preserve the peer check’s fail-open versus budget check’s fail-closed decisions.
- BATCH-001: `RISKY_SKIP` — The file reads use `--paginate`; manually establish complete file-list and rename parity before replacing them.
- BATCH-002: `NEEDS_VERIFICATION` — Keep the paginated issue list intact; verify batched head-repository and base-ref matching against each REST lookup.
- BATCH-003: `NEEDS_VERIFICATION` — Verify permissions and per-alias partial-failure handling before replacing individual label writes.

### Summary Counts

Counts cover **net-new findings only**, not Deep Audit cross-references.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 2 | MERGE-001, REUSE-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
