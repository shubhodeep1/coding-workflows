## Executive Summary

- **Restore the static reviewer prompt file before model invocation.** In four `shubhodeep1/coding-workflows` review/autofix runs—36072616228, 36076221005, 36076920721, and 36077126289—`Run reviewer models` ended with `cat: ./pre_assembled_static.txt: No such file or directory`, after a successful Semble query. Together, those failed runs occupied **1,738 runner-seconds**. Preserve the file outside the mutable checkout and check it immediately before use. **Impact:** prevents this observed failure class and its rerun cost; **confidence: high**.
- **Bound repeated memory-substate waits.** The deep-dive logs contain **1,154** `ledger_emit_substate` warnings for 120-second `record-run-event` timeouts across **16** review runs; run 36052022235 alone has 88. Calls can overlap, so multiplying warnings by 120 seconds would overstate savings. Shorten the fail-open timeout and suppress repeated attempts briefly after a timeout. **Impact:** up to 110 seconds less waiting *per affected synchronous attempt* if reduced to 10 seconds; **confidence: high** for the waits, **medium** for end-to-end savings.
- **Fix and front-load the CI prompt contract.** All **11/11 CI runs failed**; 10 failed at `Review/judge/conflict strict-render contract test`. Logs for 36058812894, 36068219202, and 36069685528 confirm an assembled-versus-legacy prompt byte assertion. Report the first mismatch and run this test earlier. **Impact:** removes a demonstrated merge blocker once corrected and exposes regressions earlier; **confidence: high** for the three inspected assertions, **medium** for the other seven same-step failures.
- **Separate reviewer failure classes in diagnostics.** Four inspected runs produced **24 reviewer-slot failures with exit 226** and “no text events”; other runs show a rejected combination of `reasoning.effort` and `reasoning.max_tokens`, or editor-manifest validation failures. Emit a bounded, credential-free failure category per attempt rather than treating these as one provider problem. **Impact:** fewer blind reruns and faster routing to the correct fix; **confidence: high**.
- **Do not read the overall 29.7% success share as task success rate.** Of 1,000 runs, **654 were skipped**. Review/autofix is the expensive tail: 171 runs, 31 failures, p95 **5,232 seconds**. Log skip and terminal reasons with a PR/issue correlation key before changing dispatch policy. **Impact:** more reliable throughput measurement; **confidence: high**.

## Speed Optimizations

1. **Critical path — stop repeated substate timeouts.** Run 36058839944 logged 78 timeout warnings; 36052022235 logged 88. `scripts/ledger_emit_substate.sh` bounds each memory write at 120 seconds and fails open, but a timed-out attempt leaves its dedupe key available for another attempt. Set a shorter tested `LEDGER_EMIT_TIMEOUT_SECONDS` (initially 10–15 seconds), add a per-run cooldown after failure, and log `attempts_suppressed` and cumulative wait milliseconds. **Estimated saving:** up to roughly 105–110 seconds per affected synchronous attempt; overlapping calls make run-level savings unknown. **Risk:** low to medium—substate freshness may fall, so retain final run-event writes and compare coverage.
2. **Critical path — prevent missing-file review attempts.** The four static-file failures lasted **314, 338, 352, and 734 seconds**. The file is assembled before preflight, while `scripts/review_run_reviewers.sh` later reads `./pre_assembled_static.txt`; its disappearance between checks is a **lifecycle/path inference**, not a proven single cleanup command. Put the generated file in the existing runtime directory, pass an absolute path, and recheck presence/size after the merge-topology gate. Log only path class, byte count, and checksum—not contents. **Estimated saving:** avoids up to the **1,738 observed failed runner-seconds** if these attempts need not recur. **Risk:** low with a compatibility fallback to the current path.
3. **Critical path — fail CI earlier.** CI p50 is **1,046 seconds**; run 36069685528 reached the prompt assertion near the end of its 901-second run. Move the strict-render/assembly contract ahead of the longer lint suite, and print the template name, first differing byte offset, lengths, and hashes on failure. Correct the template or golden text only after reviewing the semantic difference. **Estimated saving:** potentially minutes per failing CI run; exact amount requires step-duration collection. **Risk:** low; test semantics stay unchanged.
4. **Smaller opportunity — measure poller waits before tuning cadence.** `orchestrate_poll` ran 44 times with p50 **349.5 seconds**; run 36073795815’s `poll` step occupied its 331-second run. Emit per-tick API wait, model compute, sleep, and state-write milliseconds before reducing polling or parallelizing it. **Estimated saving:** unquantified; **risk:** low for logging, higher for cadence changes. By comparison, 62 observed Semble queries took about **34 seconds total** across deep-dive logs, so optimizing those first would be a micro-optimization.

## Cost Optimizations

1. **Avoid failed full review/editor passes before changing models.** Review run 36052022235 failed after **5,981 seconds** with **12,326,437 reported OpenRouter total tokens**; run 36065459446 failed in `Apply fixes with editor model` after **4,037 seconds**, following manifest-validation trouble and a workspace-guard rejection. Fix the deterministic file and format failures, and classify empty reviewer output before any same-head retry. **Estimated saving:** the token and runner spend of prevented attempts; not a defensible dollar estimate without prices and complete usage. **Quality risk:** low if failed reviews still never count as approval.
2. **Keep model/reasoning changes evidence-gated.** Review logs show six reviewer slots, a primary `openai/gpt-6-sol` editor and, in run 36063391601, a final-attempt `openai/gpt-5.6-sol` fallback. Run 36076664580’s security audit used `openai/gpt-6-sol` at `xhigh` and took 427 seconds, but has no token breakdown. First log role/model/reasoning, outcome, elapsed time and available usage per attempt; trial lower reasoning only on a defined retry class with quality comparison. **Estimated saving:** unknown until measured. **Quality risk:** material if reasoning is reduced globally.
3. **Measure prompt expansion and cache fragmentation.** Wider telemetry reports **45,511,239 prompt**, **147,276,416 cache-read**, and **2,573,173 completion tokens** across 281 logged OpenRouter calls; 35 calls lack usage. Aggregate `cache_hit_rate` is **null**, not zero. Individual runs range from **0.464443** (36058813471) to **0.776629** (36075609726). Preserve the existing static-prefix-first assembly, log a prefix hash/byte length and dynamic-section sizes, and move run-specific noise after the reusable prefix. **Estimated saving:** unknown; compare measured uncached-input tokens and latency before/after. **Quality risk:** low if ordering alone changes.
4. **Bound Semble overflow context rather than disabling Semble.** Wider telemetry records **89 `SEMBLE_QUERY` calls and 912,298 logged bytes**. In the deep-dive logs, 41 `target=overflow` calls returned **335,641 bytes**; run 36063391601 made five overflow queries after a reviewer-context query. Deduplicate repeated chunks and fetch overflow only for relevant changed files or explicit need, retaining a fail-open path. **Estimated saving:** at most the removed response bytes and associated prompt expansion; bytes are *not* measured billable tokens. Semble may be replacing larger context, but no baseline proves that here. **Quality risk:** medium if useful evidence is elided; compare review findings.
5. **Serena is not an established saving or cost in this window.** There are zero logged Serena queries, response bytes, tool calls, fallbacks, or probes; a recent review summary reports `SERENA_ENABLED: false` (36077107599). Do not claim it replaced downstream work or produced noisy bytes. **Estimated saving:** none evidenced; **quality risk:** none from leaving it unchanged.

## Reliability Improvements

1. **Missing static file — deterministic input failure.** The four cited `Run reviewer models` failures reached Semble successfully, then failed reading `./pre_assembled_static.txt`. Revalidate after checkout/merge cleanup and fail with a distinct `review_input_missing` event before retrieval or model work; retain the existing preflight. **Expected impact:** eliminate this confirmed four-run failure class. **Rollback:** restore the relative-path fallback, never synthesize an empty prompt.
2. **Memory-substate timeout storm — telemetry dependency.** The **1,154** warnings are failed substate emissions, not 1,154 failed workflow runs. Add short timeout/cooldown telemetry with `phase`, `elapsed_ms`, `reason=timeout`, and suppression count; investigate memory-store write latency separately. **Expected impact:** fewer repeated waits and warnings. **Fail-open:** keep review execution independent of substate writes.
3. **CI contract drift — validation failure.** Three inspected CI logs fail `tests/test_assemble_prompt.py` at `test_assembled_templates_match_legacy_prompt_bytes`; 10 CI runs point to that step. Surface a safe first-difference diagnostic, then repair the mismatch without weakening byte equality. **Expected impact:** restore this CI gate once the regression is corrected. **Rollback:** revert the template change, not the assertion.
4. **Reviewer/provider and editor failures — distinct categories.** Runs 36055775446, 36058701173, 36064965623, and 36067920051 each have six exit-226 reviewer failures with no text events. Run 36065459446 also records the incompatible pair `reasoning.effort`/`reasoning.max_tokens`, an editor-manifest failure, and rejected generated `tests/__pycache__` files. Ensure model configuration supplies only one reasoning control; prevent bytecode/cache artifacts in the guarded worktree; emit separate `empty_structured_output`, `provider_request_invalid`, `manifest_invalid`, and `workspace_guard_rejected` counters. **Expected impact:** fewer avoidable retry loops; **rollback:** retain strict manifest and workspace guards—never treat missing reviewer output as clean.
5. **Incomplete failure attribution.** Several review failures have no job/step and zero-second duration, including 36050108797; workflow-validation annotations are unavailable in the collector payload. Fetch annotations **only for startup failures**, and log the dispatch/ref validation outcome. A successful run, 36076968467, also warns that no standalone validation workflow could be dispatched for merged PR #4408: log attempted workflow names and a bounded reason, and keep validation status explicitly unknown rather than silently passing. **Expected impact:** faster diagnosis; no change to merge gates.

There were **zero observed `BREAK_GLASS` events** and **one `CONTEXT_BUDGET_WARN`**, in failed review run 36052022235: **202,624 prompt tokens against a 200,000-token window** on a fallback attempt. This points to prompt-size risk, not demonstrated rubric-pressure break-glass use. Emit assembled-size and truncation decisions before each attempt and enforce the existing budget without dropping required instructions.

Semble’s **12 fallbacks** are four apiece in CI contract-test runs 36058812894, 36068219202, and 36069685528 (`target=overflow`, `context=contract-test`); **runtime fallbacks are zero**. Do not diagnose a broken runtime rollout from these fixtures. Separate probe availability from runtime fallback in new events: poller summaries for 36074523151 and 36075193900 report Semble enabled but unavailable, without a counted probe result.

## AI Memory Health

Across **26 downloaded deep-dive logs**, there are **22 `AI_MEMORY_TELEMETRY` retrieves: 22/22 hits (100%)**, averaging **1,397 estimated tokens against a 1,400-token budget**; all 22 use `keyword_method=llm`. No sampled retrieve selected zero records or reported `enabled=false` or `fail_open=true`. The logs also contain 44 `record-run-event` and 13 `record-candidate` telemetry entries; run 36076968424’s unselected-run summary confirms a successful `finalize-task` for issue #4407. No `promote`, `compact`, or processed-command telemetry was established from this sample.

Retrieval is effective but leaves about **3 tokens of average headroom**. Log records considered, selected, truncated, and budget-limited by role; inspect relevance before increasing the budget. Among observed operations with `push_attempts`, **10 took two attempts and two took three**. Record retry cause and latency, and correlate them with the separate substate-timeout storm; successful retrieval does not establish healthy writes.

## GH API Call Audit

**Endpoint request counts, pagination totals, retry counts, and GitHub 403/429 counts are not present.** No high-volume endpoint can therefore be *measured* from this window; OpenRouter rate-limit classifications must not be counted as GitHub rate limits.

- **Check-run polling is the first candidate to instrument.** `review_autofix`’s `Collect PR check-run failures` uses one logical paginated snapshot per poll iteration, potentially multiple HTTP pages/retries; its documented wait budget is 300 seconds. Log `endpoint=check-runs`, head-key hash, iterations, pages, retries, unchanged snapshots, elapsed milliseconds, and terminal reason. Reuse the last confirmed same-head snapshot where safe; **estimated reduction:** one or more requests per redundant snapshot, conditional on measured duplication. Preserve the current `api_error` sentinel so unknown does not become “passing.”
- **Preserve existing batching.** The repository’s API hygiene rules prefer `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and cycle-local caches; the poller also shares an actions-runs cache. Emit batched item counts, cache hits/misses, fallback reasons, and underlying request counts before proposing per-item rewrites. Logs for 36065459446 and 36067920051 show linked-issue *body-text* fallback when GraphQL closing references were empty; that line alone does **not** prove an extra API call.
- **Measure dispatch and reconciliation, not just requests.** Sweep run 36075594994 examined eight candidates, dispatched five and skipped three active peers. Log endpoint-template request totals and correlation IDs alongside `AUTOFIX_PEER_CHECK`/dispatch outcomes; retain the three skips. Close-cleanup run 36075407860 preserved three active runs without PR linkage: log why linkage was unavailable before considering any more aggressive cancellation. **Estimated call/rate-limit reduction:** unquantifiable until request counters exist; no new service is needed.

## Prompt Cache & Memory System

Cache-read tokens are substantial, while reported cache-write tokens are **0** and overall `cache_hit_rate` is **unavailable**. Zero reported writes does not establish that no provider-side cache was created. The differing per-run hit rates above warrant a prefix-hash comparison, not an assumption of fragmentation. Log cache metadata accepted/rejected, breakpoint fallback, prefix hash, prefix bytes, and uncached dynamic bytes per model call; keep prompts and credentials out of logs. Expected effect is measurable lower uncached-input tokens and latency, with no intended reliability change.

Run 36052022235’s context warning and the **1,397/1,400-token** average memory retrieve show little growth room. Cap optional overflow before the provider window, place dynamic PR text after the stable prefix, and report what was elided. Neither a prompt-cache fail-open event nor a cache-metadata rejection was established in these deep dives; add counters to verify both behaviors.

## Orchestrator Health

`clarify`, `plan`, and `implement` have **152/160**, **145/152**, and **146/152** other/skipped outcomes respectively; recent runs such as 36077469185, 36077469351, and 36077469155 skipped within the same fan-out. This is **not evidence that all three phases are stuck**: their gate reasons are absent. Add `skip_reason`, project/issue correlation, wave, and next eligible phase to each gate outcome.

The poller completed **44/44** runs, but its p50 is **349.5 seconds**. Run 36073914917 found PRs #4354 and #4415 still queued in the merge-train release scan. Track wave entry/exit, deferral reason/count, judge decision, validation-dispatch outcome, and oldest pending PR age; avoid changing merge ordering without that evidence. Review run 36075618027 shows an autofix fingerprint cap for PR #4348 at **3/3** repeated `reviewers_failed` outcomes. The cap contains repetition but does not resolve the PR: log a terminal handoff and alert status, not another automatic identical-head pass. Conflict-heal retry and clarification-loop counts were not supplied.

## Pipeline Flow Bottlenecks

| Segment | Evidence | Bottleneck type and next measurement |
|---|---|---|
| Clarify → plan → implement | Mostly skipped fan-out; successful implement outliers include runs 36045021044 (**1,038 s**) and 36016290312 (**1,037 s**). | Gate versus compute unknown; emit skip reasons and active-step timings. |
| Review/autofix | p50 **337 s**, p95 **5,232 s**; run 36052022235 **5,981 s**. | Dominant compute/retry tail plus substate waits; fix deterministic failures and timeout repetition first. |
| CI and validate | CI **11/11 failed**, p50 **1,046 s**; merged PR #4408 had a validation-dispatch warning in run 36076968467. | Validation blocker and unknown dispatch result; front-load contract test and log dispatch outcome. |
| Orchestrate/merge | Poll p50 **349.5 s**; PRs #4354/#4415 remained queued in run 36073914917. | Poll compute versus sleep and merge deferral unresolved; emit component timings and queue age. |

The collector supplies no usable **job-level queue latency**: its run-level `run_started_at` equals `created_at` across the 1,000 rows. Do not conclude queueing is zero; collect first-job-start and dispatch-to-start timestamps.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** review/autofix’s long tail and repeated memory-substate waits. **Top failure modes:** missing reviewer static file; strict-render CI mismatch; empty reviewer output and editor-manifest/workspace-guard failures. **Highest measured cost driver:** review/autofix accounts for all **195,346,505** reported OpenRouter total tokens in the wider assembled telemetry, whose coverage is partial.

**Top three actions, in order:** (1) preserve and recheck the static reviewer file after mutable-worktree operations; (2) shorten and circuit-break fail-open substate writes while counting suppressed events; (3) diagnose and fix the prompt-byte contract, then move that test earlier. Keep reviewer and workspace safety gates intact.

## Metrics Appendix

**Window:** September 24, 2026 14:50:43 UTC–September 25, 2026 00:25:31 UTC. One repository; GitHub Actions API source.

| Workflow family | Runs | Success | Failure | Cancelled | Other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 297 | 42 (**4.2% of all**) | 6 | 655, including 654 skipped | 2 / 976 s |
| Review/autofix | 171 | 134 | 31 (**18.1% of family**) | 5 | 1 | 337 / 5,232 s |
| CI | 11 | 0 | 11 | 0 | 0 | 1,046 / 1,090.5 s |
| Orchestrate poll | 44 | 44 | 0 | 0 | 0 | 349.5 / 399.25 s |
| Clarify / plan / implement | 160 / 152 / 152 | 8 / 7 / 6 | 0 / 0 / 0 | 0 / 0 / 0 | 152 / 145 / 146 | 1 / 25.15; 1 / 11; 1 / 11 s |

| Cost and diagnostic coverage | Wider assembled telemetry | Downloaded-log collector |
|---|---:|---:|
| Runs with log telemetry | 125 | 26; includes 9 successes, 16 failures, 1 cancellation |
| OpenRouter calls; usage available / unavailable | 281; 246 / 35 | 203; 168 / 35 |
| Prompt / completion / reported total tokens | 45,511,239 / 2,573,173 / **195,346,505** | 33,421,175 / 1,645,355 / 141,619,615 |
| Cache read / write tokens; aggregate `cache_hit_rate` | 147,276,416 / 0; **null** | 106,563,872 / 0; **null** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 9,000 / 6,046,520; **123 samples** | 4,071,000 / 6,482,750; **26 biased deep-dive samples** |
| `break_glass_count` / `context_budget_warn_count` | 0 / 1 | 0 / 1 |
| Codex calls / tokens | 0 / 0 reported; **not** a measure of all model work | 0 / 0 reported |

The wider totals and deep-dive totals have **different coverage**; do not add them together or interpret the deep-dive wall-clock percentile as a population percentile. In particular, 35 OpenRouter calls lack usage in both views.

| MCP and API signal | Wider total | Target/availability detail from deep dives |
|---|---:|---|
| Semble queries / logged bytes | **89 / 912,298** | 62 directly located: `reviewer-context` **21 / 308,704 bytes**; `overflow` **41 / 335,641 bytes**. Remaining wider calls are not assigned a target here. |
| Semble fallbacks | **12** | `overflow` contract-test fixtures **12**; runtime **0**. |
| Semble probe `probe_ok` / `probe_failed` / `probe_skipped` | Not reported | No counted probe events; two poll summaries report unavailable state, not a probe result. |
| Serena queries / response bytes / tool calls / fallbacks | **0 / 0 / 0 / 0** | No per-tool breakdown exists; recent configuration reports disabled. |
| Serena probe `probe_ok` / `probe_failed` / `probe_skipped` | **0 / 0 / 0** | No target-level availability event; absence is not a successful probe. |
| GH endpoint calls / pages / retries / 403–429 | **Not collected** | Sweep 36075594994 reports **5 dispatches and 3 active-peer skips**, not underlying request counts. |
| Other MCP servers observed | **None established** | No other `<NAME>_QUERY`, `_FALLBACK`, or `_PROBE` events found in the inspected logs. |

The next collection should retain per-step durations and job start times; emit bounded, structured failure categories, API request/page/retry counters, and explicit MCP probe outcomes. That would make the savings estimates testable without adding infrastructure or changing review safety behavior.

## Deep Audit — Workflows & Scripts (2026-09-25)

### Section 1: Bug & Correctness Sweep

The read-only sweep covered 50 workflow files, 92 shell scripts, and 59 Python scripts. The existing report already covers the missing static reviewer file, substate timeouts, and CI prompt-byte mismatch; those are not repeated as findings here.

- **BUG-001** — **File:** `scripts/collect_pr_check_runs_context.py:150-168,395-414`. **Severity:** High. **Category:** `bug`. **Description:** `_parse_pages` converts empty, malformed, or unexpected successful API output into an empty run list. The caller then sets `final_status="ready"` and emits “No failed or incomplete check-runs detected” (`scripts/collect_pr_check_runs_context.py:269-292`). A read-only call to these functions confirmed that result for malformed text and an unexpected JSON object. **Recommended fix:** Validate the page shape before interpreting check-runs; write the existing `api_error` sentinel when a successful command returns an invalid snapshot, and test malformed and empty responses.
- **BUG-002** — **File:** `.github/workflows/review_autofix_sweep.yml:159-210`. **Severity:** High. **Category:** `bug`. **Description:** Each active-run status request ends in `|| true`; a failed request contributes no runs, yet the snapshot can parse as an empty active cache. The per-PR loop then dispatches without its active-peer guard (`.github/workflows/review_autofix_sweep.yml:250-297`). **Recommended fix:** Require every status snapshot to succeed and validate its JSON; if any fails, skip dispatch for that tick with a distinct diagnostic. Use `gh_retry` from `scripts/gh_helpers.sh` for transient failures.
- **BUG-003** — **File:** `scripts/orchestrate_poll_process.sh:1053-1076,1111-1127`. **Severity:** High. **Category:** `bug`. **Description:** A failed sibling-PR lookup becomes `[]` and is marked fetched for the cycle. The conflict probe then has no siblings to examine and returns success (`scripts/orchestrate_poll_process.sh:1183-1188`), allowing the ready-to-merge path to continue (`scripts/orchestrate_poll_process.sh:19761-19770`). **Inference:** an API outage can bypass this sibling-conflict safeguard when siblings exist. **Recommended fix:** Cache *unknown* separately from an authoritative empty list; defer that merge until the lookup succeeds, while retaining the next-tick retry path.
- **BUG-004** — **File:** `.github/workflows/review_autofix_sweep.yml:250-297`. **Severity:** Medium. **Category:** `bug`. **Description:** The active-run cache is keyed by PR head ref, but fork PRs deliberately dispatch on the default branch without `--ref`. A fork dispatch therefore cannot match its own head-ref cache key on the next sweep. **Inference:** repeated sweeps can queue redundant fork reviews. [NEEDS VERIFICATION] **Recommended fix:** correlate fork dispatches by PR number using a durable dispatch record or a PR-scoped run lookup; retain the existing head-ref fast path for same-repository PRs.
- **BUG-005** — **File:** `scripts/review_run_reviewers.sh:4928-4959`. **Severity:** High. **Category:** `bug`. **Description:** Pass 1 invokes the summariser without checking `pass1_successful`. With zero usable inputs, that script writes a “No findings reported” ledger (`scripts/summarize_reviewer_consensus.sh:110-143`) rather than stopping before cross-pollination. This is the pass-1 zero-success case also raised in the supplied September 24 optimization context. **Recommended fix:** require at least one successful pass-1 reviewer before summarisation; preserve failure artifacts and fail the review closed. Keep the existing pass-2 success guard (`scripts/review_run_reviewers.sh:5048-5068`).
- **BUG-006** — **File:** `scripts/review_merge_train.sh:202-233`. **Severity:** Medium. **Category:** `bug`. **Description:** `_mt_blockers_for_into` stops after `MERGE_TRAIN_MAX_OLDER_PRS` eligible older PRs and returns its accumulated blockers as a complete result. If the first 20 do not overlap but a later older PR does, the gate can report `unblocked` (`scripts/review_merge_train.sh:323-335`). Whether this bounded exception is acceptable under the train’s ordering contract needs confirmation. [NEEDS VERIFICATION] **Recommended fix:** return an explicit `incomplete` result at the cap and distinguish it from “no blockers”; obtain another batch before declaring the PR unblocked.
- **BUG-007** — **File:** `scripts/review_collect_pr_metadata.sh:176-205`. **Severity:** Medium. **Category:** `bug`. **Description:** In no-PR review mode, an absent base override and a failed repository lookup silently select `main`. **Inference:** repositories whose actual default branch is not `main` can receive a synthesized PR payload with the wrong base. **Recommended fix:** reuse the caller’s default-branch value when available; otherwise fail this metadata path with a distinct error instead of inventing a base.
- **BUG-008** — **File:** `scripts/security_audit.sh:1665-1682,1767-1774`. **Severity:** Medium. **Category:** `bug`. **Description:** Existing-finding deduplication runs before follow-up creation, while each `gh issue create` uses retrying `gh_retry`. If GitHub accepts a creation but its response is lost, an immediate retry can create the same follow-up again without rechecking its body marker. [NEEDS VERIFICATION] **Recommended fix:** use one creation attempt, then reconcile the finding marker against issues before retrying an uncertain result; preserve the weekly-cap accounting.
- **SEC-001** — **File:** `scripts/gh_helpers.sh:458-462,474-490`. **Severity:** High. **Category:** `security`. **Description:** `gh_retry` prints the full command arguments on permanent or exhausted failures. Callers pass author- or model-controlled comment text as `-f body=...`, including `.github/workflows/plan.yml:1861-1862`; that text can reach logs on failure. **Recommended fix:** log only an operation and endpoint class, attempt, and bounded error category; never print request-body arguments. Keep workflow-command escaping for the remaining diagnostic fields.
- **SEC-002** — **File:** `scripts/gh_helpers.sh:621-633`. **Severity:** Medium. **Category:** `security`. **Description:** `gh_api_json_to_file` prints the first 50 lines of a response whenever JSON validation fails. A truncated response from an issue or other private endpoint could place its raw contents in workflow logs. [NEEDS VERIFICATION] **Recommended fix:** log byte count, checksum, endpoint class, and parse-error category instead of response content.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **logical call-path estimates**, not measured HTTP totals; pagination and retries can add requests.

- **API-001** — **File:** `scripts/review_merge_train.sh:257-290`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** For an existing marker, `_mt_find_marker_comment_id` fetches all PR comments, then `_mt_upsert_comment` fetches the selected comment again solely for its body. **Current → proposed:** 2 reads → 1 read per marked PR, before any conditional write. **Recommended fix:** return the latest marker’s ID *and body* from the paginated comments snapshot. This follows the snapshot-reuse pattern in `scripts/workflow_retro_fanout.sh:301-308`; retain a freshness check if a concurrent edit would change the decision.
- **BATCH-001** — **File:** `scripts/review_merge_train.sh:124-136,202-230`. **Severity:** Medium. **Category:** `api-batching`. **Description:** Each distinct older PR receives its own paginated `/pulls/{n}/files` read inside the blocker loop, despite the run-local cache. **Current → proposed:** 1 open-PR list + *N* file-list lookups → 1 list + approximately `ceil(N/20)` aliased file lookups for the no-pagination case; retain per-PR fallback for incomplete results. GraphQL file-connection pagination and cost need validation. [NEEDS VERIFICATION] **Recommended fix:** extend the aliased-batch approach used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh` to prefetch older PR file paths, verify completeness per alias, and keep `_MT_FILES_CACHE`.
- **BATCH-002** — **File:** `scripts/orchestrate_poll_process.sh:3897-3909,3942-4007`. **Severity:** Medium. **Category:** `api-batching`. **Description:** `close_merged_issues_sweep` fetches a cross-reference timeline per issue and PR JSON per merged candidate inside nested loops. **Current → proposed:** 2 labeled-issue lists + *N* timeline reads + up to *M* candidate-PR reads → 2 lists + approximately `ceil(N/20) + ceil(M/20)` aliased reads, with per-item fallback for incomplete pages. Completeness and strict implementation-PR verification must be preserved. [NEEDS VERIFICATION] **Recommended fix:** extend `gh_issue_timeline_with_cross_refs` (`scripts/gh_helpers.sh:1030-1098`) with the poller’s existing aliased-batch pattern; batch candidate PR head/body fields and retain the current fail-closed verification.
- **API-002** — **File:** `scripts/gh_helpers.sh:91-94,439-493,693-725`. **Severity:** Low. **Category:** `api-redundancy`. **Description:** `curl_gh_api` retries every non-rate-limit HTTP failure, and `_is_gh_permanent_failure` does not identify ordinary 400/401 responses for `gh_retry`. **Current → proposed:** up to 5 requests → 1 for a classified permanent failure; transient-error behavior stays unchanged. **Recommended fix:** share an explicit permanent-status classifier across the helpers, retaining reset-aware backoff for rate limits. No batching pattern applies.
- **API-003** — **File:** `.github/workflows/review_autofix_sweep.yml:145-165`. **Severity:** Medium. **Category:** `api-redundancy`. **Description:** The sweep makes separate `queued`, `in_progress`, and `pending` run-list calls for each of two workflows. **Current → proposed:** 6 logical status snapshots → 1 paginated repository-wide active-run snapshot filtered locally. Underlying page count could erase that saving on a busy repository. [NEEDS VERIFICATION] **Recommended fix:** extend the cycle-local actions-runs pattern in `_mt_inflight_review_branches` (`scripts/review_merge_train.sh:402-407`) with complete pagination and all three statuses; do not apply this optimization without the failure handling in BUG-002.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — **File:** `scripts/review_run_reviewers.sh:1759-1768`, `scripts/review_conflict_prepare.sh:610-619`, `scripts/review_apply_fixes.sh:998-1007`. **Severity:** Low. **Category:** `duplication`. **Description:** The three `append_semble_query_section` function bodies are identical. **Recommended fix:** make `scripts/semble_helpers.sh` own `append_semble_query_section <label> <path> [max_bytes]`; source it from all three callers, preserving the current output and missing-file behavior.
- **DUP-002** — **File:** `.github/workflows/workflow-log-analysis.yml:938-1003,1549-1624,2077-2128`. **Severity:** Medium. **Category:** `duplication`. **Description:** Analysis, deep-audit, and API-redundancy steps repeat the `AI_PHASE_FAILURE_V1` payload, comment, event, and label logic with different step names. **Recommended fix:** move it to a staged `scripts/workflow_log_analysis_failure_helpers.sh` function `emit_log_analysis_failure <failed_step> <failure_mode> <attempt_count> <summary>`; update all three steps while retaining their existing `if:`, environment, and marker schema.
- **DUP-003** — **File:** `scripts/workflow_failure_heal_report.sh:54-63`, `scripts/workflow_failure_heal_intake.sh:94-107`, `scripts/check_failure_triage.sh:56-78`. **Severity:** Low. **Category:** `duplication`. **Description:** Three scripts repeat source-or-fallback definitions for `gh_retry` and `gh_api_json_to_file`; one repeats `_safe_gh_jq` as well. **Recommended fix:** stage a shared `scripts/workflow_heal_helpers.sh` initializer, `source_workflow_api_helpers <scripts_dir>`, for these callers, retaining their current missing-helper behavior and existing function names.

No greater-than-70% near-duplicate workflow structure was established by the small-workflow comparison; the repeated failure blocks above are the supported extraction candidate.

### Section 4: Expression Size Limit Risk Assessment

Parsed YAML `run` scalars—not indentation-inflated source slices—were measured across all 50 workflows. Runtime substitutions are unavailable to a static audit, so headroom below is **before expansion**.

- **EXPR-001** — **File:** `.github/workflows/implement.yml:979-1336`. **Severity:** Medium. **Category:** `expression-limit`. **Description:** “Stage workflow support files” contains three executable `${{ }}` interpolations in a **16,985-character** `run` scalar: **4,015 characters** of source-text headroom to the stated 21,000-character threshold. Variable expansion can change the final length. [NEEDS VERIFICATION] **Recommended fix:** extract the staging body to a script under `scripts/`, pass the two repository variables through step `env:`, and retain the step’s present gate and outputs.

No parsed interpolated `run` scalar exceeds 18,000 characters. The next largest is the 14,392-character “Preflight destructive-commit guard” in `.github/workflows/implement.yml:3218-3523`, below the requested 15,000-character flag threshold. No large `if:` approached it. No workflow exceeds 800 KiB or the stated 1 MB assessment threshold. The largest is `review_autofix.yml` at **432,955 bytes**—47,045 bytes below this repository’s stricter 480,000-byte guard documented in `CLAUDE.md` §27. Non-interpolated `run` blocks were excluded.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — **File:** `scripts/review_run_reviewers.sh:752-760,3532-3548`. **Severity:** Low. **Category:** `dead-code`. **Description:** ShellCheck reports `SC2034` for `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, and `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`; a repository reference check found assignments but no reads of those names. **Recommended fix:** use the health timestamp in transition diagnostics and determine whether the two `RAW_REVIEWER_*` aliases have an external compatibility contract before retiring them; obtain the identifier-change approval required by `CLAUDE.md` §6 before removal.
- **SHELL-001** — **File:** `.github/workflows/ci.yml:1013-1017`. **Severity:** Low. **Category:** `shellcheck`. **Description:** CI runs ShellCheck at `--severity=error`, so warning-level diagnostics do not fail this gate. The read-only warning-level scan reported 40 diagnostics across `scripts/*.sh`, including the unused assignments in DEAD-001; some, such as the `CDPATH= cd` idiom at `scripts/review_collect_pr_metadata.sh:27`, require triage rather than mechanical edits. **Recommended fix:** triage warning codes, add narrow suppressions for intentional constructs, then raise this gate to warning severity.

No actionable `TODO`, `FIXME`, or `HACK` marker was found in the scoped files. The direct `gh` calls in BUG-002 and the retry-helper differences in API-002 are the identified consistency gaps; they are not counted again here.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 5 | BUG-001, BUG-002, BUG-003, BUG-005, SEC-001 |
| Medium | 11 | BUG-004, BUG-006, BUG-007, BUG-008, SEC-002, API-001, API-003, BATCH-001, BATCH-002, DUP-002, EXPR-001 |
| Low | 5 | API-002, DUP-001, DUP-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 5 principal workflow/script files, plus focused tests | Large |
| API call optimization | 4 principal workflow/script files, plus pagination and fallback tests | Large |
| Code modularization | 2 workflows and approximately 7 scripts, including proposed shared helpers | Large |
| Expression size reduction | `implement.yml` and one staged script, plus contract tests | Medium |
| Medium/Low fixes | Approximately 7 existing files, plus focused tests | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-25)

### Safety Tag Legend

`SAFE_TO_MERGE` meets the static safety conditions; `NEEDS_VERIFICATION` requires the stated checks; `RISKY_SKIP` must not be auto-implemented because it touches a protected pagination, retry, polling, or race-sensitive path.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — `NEEDS_VERIFICATION`.** **Calls:** `scripts/auto_release_stable.sh:162`, `scripts/auto_release_stable.sh:170`, and `scripts/auto_release_stable.sh:176`. **Current → proposed:** 3 workflow-run reads → 1 repository-wide run read *only if that snapshot is complete for all three workflows*; otherwise retain the scoped reads. **Endpoints:** `GET /repos/{repo}/actions/workflows/{workflow}/runs` → `GET /repos/{repo}/actions/runs`. **Evidence:** The three results are filtered for the same active statuses at `scripts/auto_release_stable.sh:166-178`; the first also supplies the failed-attempt guard at `scripts/auto_release_stable.sh:182-193`. **Proposed fix:** Replace the three reads in the release guard with one validated run snapshot, filter it by workflow, and retain scoped fallback whenever it cannot supply the currently inspected 30 runs for *each* workflow. **Safety rationale:** The broader endpoint and its ordering do not statically prove equivalent coverage to three separately limited snapshots. **Downstream signal:** Verify workflow identity, ordering, permissions, and coverage of each workflow’s first 30 runs on a busy repository; compare active and failed-attempt decisions before changing the guard.

- **MERGE-002 — `RISKY_SKIP`.** **Calls:** `.github/workflows/clarify.yml:512` and `.github/workflows/clarify.yml:515-517`. **Current → proposed:** 2 reads when semantic caching is enabled → 1 on successful full-history retrieval; retain 2 on full-history failure. **Endpoint:** `GET /repos/{repo}/issues/{issue}/comments`, currently with different `per_page` values. **Evidence:** The step first saves 50 comments for the prompt, then fetches the same ordered thread with `--paginate` for the semantic cache. **Proposed fix:** Subject to manual review, have the “Fetch issue comments” step derive its first 50 prompt comments from the complete history; on full-history failure, still fetch the bounded prompt input and write the existing cache-bypass sentinel. **Safety rationale:** The second call paginates, and changing which snapshot supplies the prompt can change page-boundary, failure, and concurrent-comment behavior. **Downstream signal:** Do not auto-implement; manually test multi-page ordering, a comment arriving between reads, and full-history failure while preserving the prompt and cache-bypass behavior.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — `NEEDS_VERIFICATION`.** **Calls:** `.github/workflows/review_autofix.yml:535-543` and `.github/workflows/review_autofix.yml:1425-1437`; the first result crosses jobs through `.github/workflows/review_autofix.yml:1345,1369`. **Current → proposed:** 2 GraphQL reads when the gate successfully returns `[]` and PR text is nonempty → 1 for that case; other paths unchanged. **Endpoint:** GraphQL `repository.pullRequest.closingIssuesReferences(first: 50)`. **Evidence:** The gate records `post_merge_linked_issues_cache_known="true"` on success, but exports only the array. The dispatch job treats `[]` as a reason to fetch again, so it cannot distinguish a confirmed empty result from a failed gate lookup. **Proposed fix:** Export the gate’s cache-known flag alongside `post_merge_linked_issues_json`; update the “Dispatch standalone validate” step to reuse a *confirmed* empty array, while retaining its live lookup for unknown results. **Safety rationale:** The reads cross a workflow-job boundary, and static reading cannot establish whether a later closing reference must be observed before dispatch. **Downstream signal:** Verify the intended freshness contract across the two jobs, including a closing reference added after the gate; test successful-empty and failed-lookup outputs separately before suppressing the second read.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — its proposed reuse removes a read after a paginated comment snapshot; concurrent edits need manual review.
- BATCH-001: `RISKY_SKIP` — older-PR file reads paginate, so aliased batching must prove per-PR completeness.
- BATCH-002: `RISKY_SKIP` — this is a poller path with paginated timeline fallback and strict PR verification.
- API-002: `RISKY_SKIP` — changing permanent-failure classification changes retry behavior.
- API-003: `RISKY_SKIP` — the active-run guard paginates and precedes dispatch; incomplete coverage could permit duplicate runs.

### Summary Counts

*Counts include net-new findings only; cross-references are not recounted.*

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | MERGE-001, REUSE-001 |
| RISKY_SKIP | 1 | MERGE-002 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
