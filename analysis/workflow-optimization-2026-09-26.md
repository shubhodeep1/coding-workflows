## Executive Summary

- **CI is the clearest repeatable failure.** Seven of ten CI runs failed; five spent 558–630 seconds before reaching the same thread-reuse assertion in `lint / Validation bootstrap and family direct-run tests` (for example, runs 36170477132 and 36186609211). Move that contract check ahead of the long test suite. **Estimated impact:** about 50 minutes saved across those five observed failures if caught near startup. **Confidence: high.**
- **Successful review runs can conceal expensive recovery or a blocked PR.** Run 36203196657 lasted 2,038 seconds, used 26 OpenRouter calls, and reported `editor_changes_lost` followed by redispatch. Recent successful review gates for PRs 4443 and 4450 reported dirty merge state and a fingerprint cap of 3/3. Emit an explicit terminal *work outcome* distinct from the GitHub run conclusion. **Impact:** fewer repeated reviews and faster escalation; savings cannot yet be measured. **Confidence: high.**
- **Two review/autofix runs failed safely, but late:** runs 36176625636 and 36196360602 rejected an incomplete security-sensitive support set after 323 and 430 seconds. Preserve that refusal; preflight the verified bundle before expensive setup and report which required component is absent. **Impact:** potentially several minutes sooner per affected run. **Confidence: high.**
- **Synchronous memory work is visible on the poll critical path.** In poll run 36203394070, the start and completion memory-event steps each took about 55 seconds of a 362-second run. Instrument clone, write, and push time separately before changing persistence behavior. **Potential impact:** up to roughly 110 seconds per comparable poll if that overhead proves reusable; **confidence: medium** on savings, high on measured delay.
- **The headline median hides active work.** Of 1,000 runs, 685 were skipped; overall p50 was 2 seconds, versus 343 seconds for the 47 poll runs. Publish active-run and skipped-run metrics separately. **Impact:** better prioritization rather than direct runtime savings. **Confidence: high.**

## Speed Optimizations

1. **Review/autofix critical path — highest potential.** Review runs 36172003252 and 36179301062 took 4,237 and 3,588 seconds. Their `codex-agent` logs contain 86 and 79 distinct ledger-substate timeout warnings, respectively; those counts **must not** be multiplied by the 120-second timeout because calls may overlap. Root cause category: auxiliary event emission repeatedly reaching its timeout during model work. In `scripts/ledger_emit_substate.sh`, emit a once-per-run summary of attempts, timeouts, skipped emissions, and elapsed time; then test a per-run circuit breaker for *auxiliary* substate emissions after the first timeout, retaining required memory writes and existing fail-open behavior. **Estimated savings:** one or more 120-second waits where timeouts are sequential; measure before claiming a run-wide gain. **Risk: medium.**
2. **Fail CI contracts early.** The same `tests/test_codex_thread_reuse_core.py` assertion—expecting the validation workflow’s `stage_workflow_support.sh` bootstrap—ended five CI runs totaling 3,033 seconds. Run this contract and the shared shell-block guard before family direct-run tests; correct either the workflow or the contract to the intended trusted bootstrap, without weakening it. **Estimated savings:** most of each 558–630-second failed run on recurrence. **Risk: low** for ordering, medium for any contract change.
3. **Expose and then shorten poll memory overhead.** Poll run 36203394070 spent approximately 55.8 seconds recording `poll_started` and 54.7 seconds recording `poll_completed`; `Process each tracking issue` occupied about 109 seconds. Add `clone_ms`, `write_ms`, `push_ms`, and `attempts` to memory-operation telemetry. If clone time dominates, reuse a safely refreshed run-local checkout with the existing conflict/retry path. **Conditional savings:** roughly 45–55 seconds on a subsequent write; not yet established. **Risk: low** for logging, medium for checkout reuse.
4. **Instrument a CI blind interval before tuning shards.** Three successful CI runs took 2,326–2,624 seconds and each had an approximately 1,002–1,155-second gap before the next `orchestrate-poll shard 0` output. Emit flushed shard start/end, current test, and periodic elapsed-time events. Four shards were already configured in run 36168535223, so adding shards without this evidence is premature. **Immediate savings: none;** enables a targeted critical-path change. **Risk: low.** Semble queries taking roughly 0.4–0.7 seconds are micro-optimizations by comparison.

## Cost Optimizations

1. **Prevent avoidable editor recovery first.** Run 36203196657 reported `editor_changes_lost` and redispatch after a 2,038-second review using 26 calls and 44.21 million OpenRouter usage tokens, including cache reads. Validate the on-disk diff and claimed changes immediately after editor execution, before downstream work; log the loss point and preserve the existing redispatch as fallback. **Savings:** potentially a comparable repeated attempt, *not* a claim that all 44.21 million tokens in this run were wasted. **Quality risk: low** if this only detects loss.
2. **Investigate low cache reuse without changing review content.** Across the assembled 88 OpenRouter calls, cache-read tokens were 73.79 million and noncached prompt tokens 14.73 million; calculated `cache_hit_rate` was 83.36%. Review run 36179301062 was lower at 47.73% (12 calls). Log stable-prefix hash, model, phase, and dynamic-context placement per call; keep invariant instructions before run-specific material. **Conditional savings:** shifting roughly 0.81 million tokens from noncached prompt to cached reads in a run with 36179301062’s input volume if its hit rate reached 75%; dollar savings require provider prices and billing data. **Quality risk: low** for ordering-only changes.
3. **Measure Semble’s net context value.** The assembled window records 32 `SEMBLE_QUERY` calls and 224,162 logged bytes: 25 `target=overflow` calls/121,690 bytes in implement and seven `target=reviewer-context` calls/102,472 bytes in review/autofix. Run 36191760929 alone made eight overflow queries/30,366 bytes. Log source bytes that would otherwise enter the prompt alongside selected chunks and bytes actually injected. **Savings:** unquantifiable until paired measurements show whether Semble replaces expansion or adds low-value context. **Quality risk: medium** for changing selection; logging is low-risk.
4. **Do not downgrade models or reasoning on configuration alone.** Review logs show `REVIEW_CONSOLIDATOR_REASONING: xhigh`, and run 36173134609 switched its final editor attempt from capacity-limited `openai/gpt-6-sol` to `openai/gpt-5.6-sol`. Record actual model, reasoning level, calls, tokens, latency, and accepted outcome by phase; trial cheaper settings only on a bounded, quality-checked cohort. **Dollar estimate: unavailable. Quality risk: high** without outcome comparison. Serena recorded zero real queries and zero response bytes, so there is no evidence yet that it replaces downstream tool/model work—or adds noisy responses.

## Reliability Improvements

1. **Resolve CI’s repeated contract failure.** Five of seven failed CI runs ended at the identical thread-reuse assertion; run 36164629176 instead failed the shared-helper guard, and run 36200909816 failed Ruff with four reported errors. Add a fast, named preflight result (`check`, expected contract, observed contract, source revision) and run Ruff before lengthy tests. **Expected impact:** removal of a five-run repeat failure class once the underlying contract is fixed. **Rollback:** retain the original full checks until preflight parity is demonstrated.
2. **Keep support staging fail-closed, diagnose it earlier.** Runs 36176625636 and 36196360602 refused to mix incomplete security-sensitive support snapshots. Log the verified support identity, missing required filenames, and time spent before staging; guard downstream steps when staging did not establish their variables—the same logs subsequently show `EDITOR_SUMMARY_FILE: unbound variable`. **Expected impact:** fewer secondary errors and faster repair, not permission to use an unverified bundle. **Rollback:** keep the current refusal intact.
3. **Surface blocked reviews as blocked work.** Review-gate runs 36203700569 and 36203694112 succeeded as workflows while PRs 4443 and 4450 were dirty and had reached `workflow_failure` fingerprint cap 3/3. Emit one deduplicated PR/head-scoped terminal event with merge state, cap reason, and handoff owner; suppress redundant sweeps on that unchanged head while preserving a fresh-head recheck. **Expected impact:** fewer no-progress dispatches and shorter time to intervention. **Rollback:** revert suppression, not the cap.
4. **Classify validation failures at their origin.** Validate run 36165111080 failed `Enforce validation outcome` after template renderer exit 14 produced `harness_error`; its summary listed possible causes rather than a confirmed cause. Capture a bounded, redacted renderer stderr excerpt and dependency/config check result at the renderer step. **Expected impact:** fewer diagnostic reruns; one observed run, so rate impact is unknown. **Rollback:** retain the current harness-error status.
5. **Do not mistake synthetic MCP fallbacks for an outage.** CI runs 36168535223, 36179284365, and 36185244321 emitted four `SEMBLE_FALLBACK target=overflow` events each for a deliberately missing executable with `context=contract-test`: **12 test fallbacks, zero recorded runtime fallbacks**. Keep test and runtime counts separate. Serena has zero query, fallback, and probe events; that is an availability-coverage gap, not proof of a broken rollout. Observed `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero, with no measured policy-pressure or prompt-window incident in covered logs.

## AI Memory Health

Seven deep-dive `AI_MEMORY_TELEMETRY` retrieves, from two implement and five review runs, all selected records: **100% observed hit rate**, zero empty retrieves. Average estimated retrieval was **1,451 tokens versus a 1,457-token average budget** (about 99.6%); methods were `llm` five, `plain` two, `none` zero. That near-full budget warrants relevance sampling before increasing it, not an automatic cut.

Other observed operations include 22 `record-run-event`, eight `record-candidate`, two `finalize-task`, and two each of `processed-command-claim` and `processed-command-complete`; `promote` and `compact` were not observed. No retrieval was marked disabled or fail-open. **Eleven force-tick operations were fail-open failures**—six gets and five puts—across validate run 36165111080 and implement runs 36161573118/36191760929; validate logged a memory-clone failure. Two of 22 recorded run events needed two push attempts; none observed needed more. Add operation-scoped timing and a short reason category to these events, keeping failure detail redacted and fail-open behavior unchanged.

## GH API Call Audit

- **Observed write hotspot:** poll run 36203394070, `poll / Process each tracking issue`, attempted `pulls/4450/update-branch` and `pulls/4443/update-branch`; both returned non-retryable HTTP 422 merge conflicts. These are **two observed calls**, not a measured total API budget. Use freshly prefetched merge state to avoid an update known to be dirty, while preserving the conflict-heal path and rechecking unknown state. **Conditional reduction:** up to two futile writes in a comparable poll; no retry or rate-limit reduction is demonstrated for this run.
- **Read-path hygiene:** `.github/workflows/orchestrate_poll.yml` uses one configured active-issue list operation; `scripts/orchestrate_poll_process.sh` already contains batched GraphQL candidate/linked-PR helpers and cycle-local caches. This follows `CLAUDE.md` §15’s prohibition on per-item loop lookups. **No unbatched read pattern is confirmed by these logs.** Do not replace the two distinct PR writes with a purported batch-read optimization.
- **Diagnostic addition:** emit one redacted `GH_API` aggregate per job/step: endpoint *template*, read/write, attempts, status class, elapsed milliseconds, cache hit/miss, batch item count, and rate-limit event count. Instrument `scripts/gh_helpers.sh` and the existing batch helpers without logging request bodies or credentials. This will expose missed reuse and quantify any later call-count reduction. No rate-limit event was found in inspected deep dives; full-window rate-limit incidence is **unknown**.

## Prompt Cache & Memory System

The assembled review/autofix telemetry reports **83.36% `cache_hit_rate`**, 73.79 million cache-read tokens, zero reported cache-write tokens, and usage on all 88 OpenRouter calls. Zero *reported* writes should not be interpreted as zero provider-side cache creation. Hit rates vary markedly—from 47.73% in run 36179301062 to 91.22% in run 36203196657. Stable-prefix hashes and a per-call prompt composition breakdown would distinguish fragmentation from intentionally different work; dynamic PR state should follow invariant instructions when semantics permit.

Retrieval is effective in the seven measured deep dives but almost fills its 1,400–1,600-token caps. Log selected-record IDs as non-sensitive hashes, estimated tokens, budget, and whether each selection was used; keep the current caps pending relevance evidence. No `CONTEXT_BUDGET_WARN` was collected, so prompt-growth pressure is **not observed**, not ruled out across uncollected runs. Preserve memory fail-open on force-tick clone failures and count it separately from successful retrieval.

## Orchestrator Health

All 47 `orchestrate_poll` runs succeeded as workflows, yet their p50/p95 durations were 343/839 seconds. Run 36203394070 processed tracking issues, encountered two dirty-PR update conflicts, and finished successfully; review gates later recorded capped, dirty PRs. Track `issue`, `wave`, previous/next state, deferral reason, conflict-heal attempt, unchanged-head age, and terminal blocked reason in one transition event. This should reveal whether success means progression or merely another poll.

Clarify, plan, implement, and clarify-respond families have 158–159 *other/skipped* outcomes each; these counts do **not** establish clarification loops or stuck waves because skipped runs have no step logs. Add an upstream dispatch/skip-reason event keyed to tracking issue and desired phase. Compare wave dwell time and unchanged-head polls before changing schedules or retry limits.

## Pipeline Flow Bottlenecks

| Flow segment | Evidence | Bottleneck and next action |
|---|---|---|
| Queue | Collector `run_started_at` equals `created_at` for all 1,000 rows, while the summary for cancel-on-close run 36202565954 reports about 21 seconds waiting for a runner. | Queue time is not reliably separated. Collect job queued/start timestamps in the existing run fetch; no queue optimization is justified yet. |
| Clarify → plan | Family p50s are 1 second because most runs skip; active plan run 36201582805 took 537 seconds. | Emit dispatch/skip reason and active-only phase timings before tuning models. |
| Implement | Runs 36161573118 and 36191760929 took 2,117/2,118 seconds. | Separate model, memory-write, and validation time; reuse Semble context only when selected bytes prove useful. |
| Review/autofix | Runs 36172003252/36179301062 took 4,237/3,588 seconds; run 36203196657 reported lost editor changes. | Prioritize ledger-timeout and change-preservation diagnostics on the model critical path. |
| Validate and CI | Validate run 36165111080 had renderer exit 14; five CI runs reached the same assertion after 558–630 seconds. | Move cheap contracts forward and log the renderer’s concrete failure category. |
| Poll/merge | Poll p50 343 seconds; PRs 4443/4450 were dirty and capped. | Track state progression separately from successful execution; avoid known-futile update attempts. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** long review/autofix model paths (up to 4,237 seconds), successful CI runs over 2,300 seconds, and poll memory/event work. **Top failures:** repeated CI thread-reuse contract assertion (five runs), incomplete trusted support staging (two), and one validation renderer harness error. **Highest measured cost drivers:** review/autofix’s 89.36 million OpenRouter usage tokens across 88 calls, plus implement’s 5.31 million separately reported Codex tokens.

**Prioritized actions:** (1) put the failing CI contract and Ruff ahead of long suites and repair the contract; (2) add review outcome, ledger-timeout, and editor-change-preservation diagnostics without weakening safety gates; (3) time memory and GH API operations in poll, then optimize only measured repeated work.

## Metrics Appendix

**Window and coverage.** GitHub Actions collector snapshot generated September 26, 2026, 00:38 UTC; one repository, 1,000 runs. The assembled analysis context includes cost telemetry for **120 runs**, whereas the supplied full-log collector directory has **23 parsed deep-dive runs**. Totals below use the assembled context unless labeled *deep dive*; do not combine these denominators. Twelve recent skipped runs have empty log archives, and 965 runs were not selected for full-log download.

| Runs | Success | Failure | Skipped | Cancelled | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All families | 304/1,000 (30.4%) | 10/1,000 (1.0%) | 685 | 1 | 2 / 421 seconds |
| CI | 3/10 | 7/10 (70%) | 0 | 0 | 620 / 2,614.6 seconds |
| Review/autofix | 163/167 | 2/167 (1.2%) | 1 other | 1 | 13 / 1,910.1 seconds |
| Orchestrate poll | 47/47 | 0 | 0 | 0 | 343 / 839.2 seconds |
| Validate | 0/1 | 1/1 | 0 | 0 | 298 / 298 seconds |

| Cost and timing measure | Assembled value |
|---|---:|
| Codex calls / tokens used | 60 / 5,310,845 |
| OpenRouter calls; usage available / unavailable | 88; 88 / 0 |
| OpenRouter prompt / completion / reported total tokens | 14,730,425 / 845,116 / 89,357,733 |
| OpenRouter cache read / reported cache write tokens | 73,786,507 / 0 |
| `cache_hit_rate` | 83.36% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 3,433,760; 117 samples |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

| MCP target or server | Queries | Logged bytes | Fallbacks | Probe ok / failed / skipped | Interpretation |
|---|---:|---:|---:|---:|---|
| Semble `overflow` | 25 | 121,690 query bytes | 12 **CI contract-test only** | Not emitted | Runtime fallbacks: 0; test fallbacks occurred in three CI runs, four each. |
| Semble `reviewer-context` | 7 | 102,472 query bytes | 0 | Not emitted | Net prompt reduction unmeasured. |
| **Semble total** | **32** | **224,162** | **12 synthetic / 0 runtime** | Not emitted | Query bytes are logged output, not token savings. |
| Serena, target unobserved | 0 | 0 response bytes | 0 | **0 / 0 / 0** | No query, tool-call, or availability-probe evidence; per-tool breakdown empty. |
| Other MCP servers observed | 0 | 0 | 0 | None observed | No unknown server prefix found in inspected deep dives. |

**GH API call summary:** two visible `update-branch` PUT failures (HTTP 422) in poll run 36203394070; total calls, cache-miss calls, and full-window rate-limit counts were not supplied. **Next collection step:** add per-step API aggregates and job queue timestamps to the existing telemetry path, and reconcile the 120-run assembled versus 23-run full-log coverage in future reports.

## Deep Audit — Workflows & Scripts (2026-09-26)

### Section 1: Bug & Correctness Sweep

Static checks covered 50 workflow YAML files, 94 shell scripts, and 60 Python scripts. YAML parsing, `bash -n`, and in-memory Python compilation found no syntax failures. Findings below are source-based; no workflow was executed. The CI bootstrap failure and memory delays already described in the report are not repeated.

**SEC-001 — PR-head files can supply triage model instructions**  
**File:** `.github/workflows/check_failure_triage.yml:219-225,301-313`  
**Severity:** High · **Category:** `security`  
**Description:** The job checks out the PR head, then installs trusted system instructions and the triage prompt *only if those paths are absent*. `scripts/check_failure_triage.sh:281-320` reads the resulting files as instructions; lines 322-331 invoke Codex with `--sandbox danger-full-access`. A same-repository PR can therefore supply instruction files that steer a secret-bearing diagnosis job. The extent to which checkout credentials remain accessible to that process is **an inference**. [NEEDS VERIFICATION]  
**Recommended fix:** Always stage these instruction and prompt files from the verified support source, keeping PR content in the explicitly marked failure-context portion of the prompt. Set `persist-credentials: false` on the PR checkout and assess a tokenless isolated model path, following the repository’s `scripts/clarify_isolated_run.sh` pattern.

**BUG-001 — Failed deduplication read permits a new triage issue**  
**File:** `scripts/check_failure_triage.sh:219-230`  
**Severity:** High · **Category:** `bug`  
**Description:** An exhausted `gh_retry` or failed `jq` becomes `[]` through `|| echo '[]'`. The script then treats “could not list open triage issues” as “none exist” and reaches `gh issue create` at line 391. Job concurrency serializes matching runs but does not correct this failed-read path.  
**Recommended fix:** Capture and validate the list call separately. If uniqueness cannot be confirmed, stop before issue creation, emit a bounded diagnostic, and let a later run retry.

**BUG-002 — Merge-train cap can overlook an older blocker**  
**File:** `scripts/review_merge_train.sh:202-233`  
**Severity:** Medium · **Category:** `bug`  
**Description:** On the 21st eligible older PR with the default `MT_MAX_OLDER=20`, `_mt_blockers_for_into` breaks without checking its files. If the first 20 do not overlap, the gate interprets empty blockers as unblocked (`:327-334`). This contradicts the file’s “every older overlapping PR” queue policy (`:13-16`).  
**Recommended fix:** Return a distinct `cap_exceeded` result rather than an empty blocker list. Keep that PR queued for a later complete evaluation, or fetch the remaining paths through a bounded batch.

**BUG-003 — Candidate PR cache does not enforce repository identity**  
**File:** `scripts/orchestrate_poll_process.sh:14779-14804`  
**Severity:** Medium · **Category:** `bug`  
**Description:** `_fetch_candidate_issue_details_graphql` selects a closing cross-reference by PR type but not repository. Its sibling `_fetch_linked_pr_status_graphql` explicitly filters `repository.nameWithOwner` (`:14905-14916`). The candidate result feeds validation dispatch state (`:2704-2719`) and merged-PR recovery. **Inference:** if GitHub supplies a cross-repository event with `willCloseTarget=true`, those decisions could use the wrong PR’s state. [NEEDS VERIFICATION]  
**Recommended fix:** Request the source PR’s `repository.nameWithOwner` and apply the sibling helper’s same-repository filter; test a cross-repository timeline fixture.

**BUG-004 — Merge-train comment writes report success on failure**  
**File:** `scripts/review_merge_train.sh:275-291`  
**Severity:** Low · **Category:** `bug`  
**Description:** `_mt_upsert_comment` suppresses both PATCH and POST errors with `|| true`; its existing-comment branch then explicitly returns success. The release caller’s warning handler (`:482-486`) cannot detect a missed released-status comment.  
**Recommended fix:** Return the write’s status. Keep queue-label handling fail-open as designed, but let callers emit their existing warning when the comment was not persisted.

### Section 2: GitHub API Call Redundancy Audit

These are static call-count candidates, not measured request totals. Pagination, retries, and fallback calls can increase actual requests. The existing report’s log sample did not establish an unbatched read hotspot.

**BATCH-001 — Merge-train changed-file reads are per PR**  
**File:** `scripts/review_merge_train.sh:123-136,202-230`  
**Severity:** Medium · **Category:** `api-batching`  
**Description:** After one open-PR list, each distinct older PR triggers a paginated `pulls/{n}/files` read; the process cache prevents repeat reads but not the initial N reads. **Current:** `1 + N` logical reads for N distinct older PRs (up to 21 at the default gate cap), plus an optional own-PR fallback. **Proposed:** `1 + ceil(N/10)` with ten aliased PR file connections per GraphQL batch—three for N=20—plus REST fallback for incomplete connections. [NEEDS VERIFICATION]  
**Recommended fix:** Add a prefetch to `scripts/review_merge_train.sh`, following `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`. Preserve `_MT_FILES_CACHE`, check each connection’s `pageInfo`, and use the existing REST path for a failed or incomplete item.

**BATCH-002 — Release repeatedly lists marker comments**  
**File:** `scripts/review_merge_train.sh:255-290,464-486`  
**Severity:** Medium · **Category:** `api-batching`  
**Description:** For N queued PRs and R successfully released PRs, release makes N queued-marker comment-list reads, then R more released-marker list reads; E already-existing released markers add E body GETs. **Current:** `N + R + E` logical comment reads. **Proposed:** `ceil(N/25)` aliased GraphQL comment reads for the queued PR set, with cached marker IDs *and bodies* reused for both operations; retain per-item paginated REST fallback. Writes remain per PR. [NEEDS VERIFICATION]  
**Recommended fix:** Extend the poller’s aliased-issue batching pattern with a merge-train comment prefetch, including pagination checks as in `scripts/gh_helpers.sh::gh_pr_with_all_comments`. Pass cached marker records to `_mt_upsert_comment`.

**API-001 — Known label is created again for every linked issue**  
**File:** `scripts/label_helpers.sh:143-176,179-196`  
**Severity:** Medium · **Category:** `api-redundancy`  
**Description:** `set_issue_phase_label_resilient` calls `ensure_label_exists` every time. The fingerprint-cap workflow first ensures `ai:review-blocked`, then calls that setter for each linked issue (`.github/workflows/review_autofix.yml:2121-2129`). **Current:** `N + 1` label-create attempts for N linked issues. **Proposed:** one confirmed create/existence result reused for that run; retry a later call if confirmation failed.  
**Recommended fix:** Add a run-local, repository-and-label-keyed success cache to `scripts/label_helpers.sh::ensure_label_exists`, following the cycle-local-cache discipline in `scripts/orchestrate_poll_process.sh`. Do not cache an unconfirmed API result.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — Three identical integration-ref bootstrap bodies**  
**File:** `.github/workflows/clarify.yml:61-130`  
**Severity:** Medium · **Category:** `duplication`  
**Description:** The same 70-line resolver clone, fallback, and invocation block appears in `.github/workflows/implement.yml:440-509` and `.github/workflows/orchestrate_clarify_respond.yml:115-184`. Three copies must retain the same trust boundary and fallback behavior.  
**Recommended fix:** Put the body in a new trusted `scripts/resolve_integration_ref_bootstrap.sh <issue_number> <support_ref>`; update all three callers to stage it from a verified support checkout **before** invoking it. Pass GitHub expressions via step `env:`, never by executing a PR-head copy.

**DUP-002 — Release-tag publication routine is copied across workflows**  
**File:** `.github/workflows/mark-stable.yml:665-738`  
**Severity:** Medium · **Category:** `duplication`  
**Description:** The `publish_tag_with_remote_verification` routine and surrounding tag-publication step are identical in `.github/workflows/test-and-mark-stable.yml:5641-5791`. The routine distinguishes immutable from moving tags and verifies a failed push against the remote; drift between copies would affect release safety.  
**Recommended fix:** Move the routine to `scripts/release_tag_helpers.sh::publish_tag_with_remote_verification <tag_ref> <immutable|moving>` and source the verified copy in both workflows. Retain each caller’s tested-tip and immutable-tag checks.

**DUP-003 — Internal plan and implement wrappers share a near-identical shape**  
**File:** `.github/workflows/internal-plan.yml:1-34`  
**Severity:** Low · **Category:** `duplication`  
**Description:** Compared after phase-name and expression normalization, this wrapper and `.github/workflows/internal-implement.yml:1-32` are approximately 80% line-similar. Their `/answer` versus `/approved` predicates and permissions are important differences, so literal YAML deduplication is not safe. [NEEDS VERIFICATION]  
**Recommended fix:** If wrapper drift recurs, extend `scripts/workflow_wrapper_refs.py` with `render_internal_wrapper(phase, predicate, permissions, reusable_ref)` and generate/check both wrappers from explicit phase-specific inputs. Retain the existing phase-wrapper predicate-parity test; do not introduce a dynamic `uses:` target.

### Section 4: Expression Size Limit Risk Assessment

All 50 workflows were parsed. The 224 `run:` bodies containing `${{ }}` were measured by YAML scalar length; runtime expansion of context values is unknown. Blocks without interpolation were excluded. No measured `if:` value exceeds 859 characters, and no workflow exceeds 800 KB.

**EXPR-001 — Implement support-staging block is near the medium-risk threshold**  
**File:** `.github/workflows/implement.yml:981-1337`  
**Severity:** Medium · **Category:** `expression-limit`  
**Description:** This interpolated `run:` body measures approximately **16,985 characters**, leaving **4,015** to the stated 21,000-character limit and only **1,015** to the 18,000-character high-risk threshold. It contains three `${{ }}` substitutions; those values are not included in the static estimate.  
**Recommended fix:** Extract staging into a trusted script under `scripts/`, pass the repository and two feature-flag values through `env:`, and retain the existing staged-support ledger behavior.

**EXPR-002 — Validation embeds its support manifest in an interpolated step**  
**File:** `.github/workflows/validate.yml:257-452`  
**Severity:** Low · **Category:** `expression-limit`  
**Description:** The body measures approximately **10,673 characters**, leaving **10,327** to 21,000. Most of its length is the JSON manifest heredoc (`:289-448`); two expressions follow it. It is below the requested risk thresholds, but manifest growth consumes the same block budget.  
**Recommended fix:** Stage a static manifest file from the verified support source and pass it to the existing `stage_workflow_support.sh validate --manifest` interface.

**File-size contract:** The repository’s measured guard is stricter than the prompt’s 1 MB assessment: `tests/test_workflow_file_size_limit.py:24-27,39-53` records a 480,000-byte CI guard and a 512,000-byte startup limit. The largest workflow is **451,362 bytes**; see DEBT-001.

### Section 5: Cross-Cutting Concerns

**CONSIST-001 — Shared phase-label setter retains a documented terminal-label race**  
**File:** `scripts/label_helpers.sh:193-225`  
**Severity:** High · **Category:** `consistency`  
**Description:** For a nonterminal target, `set_issue_phase_label_resilient` computes a full replacement from an earlier GET and PUTs it without rechecking `ai:merged` or `ai:closed`. `scripts/review_rb_judge.sh:772-849` uses add-first, selective deletion, and terminal reconciliation specifically to avoid a merged label being displaced. **Inference:** a concurrent close between the shared setter’s GET and PUT can recreate that class of phase regression.  
**Recommended fix:** Move the judge’s terminal-aware reconciliation into `scripts/label_helpers.sh` and use it for nonterminal transitions; preserve terminal precedence and add a concurrent-close regression test before updating callers.

**DEAD-001 — Triage defines an unused API fallback**  
**File:** `scripts/check_failure_triage.sh:66-79`  
**Severity:** Low · **Category:** `dead-code`  
**Description:** The local `_safe_gh_jq` fallback creates a temporary file and calls raw `gh api`, but this script has no `_safe_gh_jq` invocation.  
**Recommended fix:** Remove this local definition; keep the sourced `gh_helpers.sh` implementation available for future actual callers.

**SHELL-001 — Singleton loops trigger SC2043**  
**File:** `scripts/stage_workflow_support.sh:138-145,198-208`  
**Severity:** Low · **Category:** `shellcheck`  
**Description:** Focused ShellCheck reports SC2043 for both `for f in <one literal>` loops. The first stages only `transcript_archive.sh`; the second checks only `unattended_system_instructions.md`. Neither needs iteration.  
**Recommended fix:** Replace each loop with a direct assignment and its existing conditional body. CI currently checks shell scripts at `--severity=error` (`.github/workflows/ci.yml:1030-1034`), so these warnings do not fail that step.

**DEBT-001 — Review workflow has limited room beneath its actual size guard**  
**File:** `.github/workflows/review_autofix.yml:6210-6381`  
**Severity:** Medium · **Category:** `tech-debt`  
**Description:** The workflow measures **451,362 bytes**, only **28,638 bytes** below the repository’s 480,000-byte CI guard. This cited interpolated step alone is approximately 9,203 characters; further inline growth could reach the guard before the prompt’s 800 KB warning level.  
**Recommended fix:** Before adding substantial inline logic, extract large steps using the existing `review_autofix_step_*.sh` verified-support pattern and update its required-script and test registries as documented in `agents.md`.

No actionable SC2086 finding or `TODO`/`FIXME`/`HACK` marker was confirmed in the scoped scans. ShellCheck’s SC2016 notes on quoted GraphQL query literals were not treated as defects.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | SEC-001, BUG-001, CONSIST-001 |
| Medium | 9 | BUG-002, BUG-003, BATCH-001, BATCH-002, API-001, DUP-001, DUP-002, EXPR-001, DEBT-001 |
| Low | 5 | BUG-004, DUP-003, EXPR-002, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | ~4–6: triage workflow/script, label helper, judge and tests | Large |
| API call optimization | ~3–5: merge-train and label helpers, callers and tests | Medium |
| Code modularization | ~9–11: five phase/release workflows, two wrappers, shared scripts and tests | Large |
| Expression size reduction | ~3–5: implement/validate workflows, extracted script or manifest and tests | Medium |
| Medium/Low fixes | ~4–6: poller, merge-train, triage and support-staging scripts plus tests | Medium |
