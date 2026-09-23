## Executive Summary

- **Repeated same-head editor no-ops are the largest avoidable cost and latency source — high confidence.** PR `#4323`, head `e16fdfd…`, repeated the same `editor_empty_noop` fingerprint in runs `35846260189`, `35871101502`, `35878367998`, and `35892683442`. The final three consumed **15,171 seconds (~4.2 hours)** and **25.52M cache-weighted tokens** after the streak had already reached three.
- **Review/autofix dominates failures and tail latency — high confidence.** It had 142 runs, 27 failures (**19.0%**), 15 cancellations, and p95 duration **5,315s**, versus repository-wide p50 **7s**.
- **Reviewer infrastructure is degraded — high confidence.** Five of eleven full review logs rejected two reviewer models as missing/duplicated in the staged catalog; eight of eleven had `stall_guard` events. Every full log also emitted **43–82 ledger substate timeouts**.
- **Two completed edit cycles failed only during push — high confidence.** Runs `35843269131` and `35868679924` committed successfully, then failed in `Push all pending commits`; the latter was a GitHub `Internal Server Error` that was not retried.
- **Caching is valuable but prompt size remains dangerous — high confidence.** OpenRouter recorded 76.86M cache-read tokens versus 24.78M prompt tokens, a computed **75.6% cache-read share**. Nevertheless, six context-budget warnings occurred, including one prompt at **98.74%** of its context window.
- **CI is the second major critical path — high confidence.** CI p50 was **2,497s**; runs `35897751073` and `35898180125` spent approximately 42–43 minutes in the monolithic `lint` job.

## Speed Optimizations

1. **Stop repeated same-head failures before model invocation — critical path**
   - **Evidence:** Runs `35846260189`, `35871101502`, `35878367998`, and `35892683442` repeated the same fingerprint; only later run `35903666305` tripped the cap and completed in 62s.
   - **Root cause:** Comment-backed fingerprint state diverged from the workflow-heal streak; `pr_comments_unavailable` later forced “counting this run only.”
   - **Exact change:** Persist the fingerprint counter in the existing quarantine/ledger state and use comments only as a secondary source. Pass the gate’s comment snapshot to the cap job instead of refetching it.
   - **Estimated saving:** Up to **4.2 hours and 25.52M tokens** in the selected runs.
   - **Risk:** Low; fail open when persisted state is unreadable.
   - **Logging:** `AUTOFIX_REPEAT_GUARD head=… fp=… ledger_count=… comment_count=… decision=skip|run source=…`.

2. **Add a run-local reviewer health circuit breaker — critical path**
   - **Evidence:** Eight of eleven full runs encountered stalls; `minimax/minimax-m3` often consumed multiple 600-second stall windows before failback.
   - **Exact change:** After one `stall_guard`, route later passes directly to the configured failback. Do not retry a model after a deterministic config-generation failure.
   - **Estimated saving:** **10–25 minutes** per affected run.
   - **Risk:** Medium; may reduce one reviewer’s diversity during provider degradation.
   - **Logging:** Include per-attempt elapsed time, first-token latency, output bytes, kill reason, and selected failback.

3. **Shard the CI `lint` job — critical path**
   - **Evidence:** CI runs `35897751073` and `35898180125` took 2,573–2,575s, almost entirely in `lint`.
   - **Exact change:** Split static workflow checks, contract tests, unit-test shards, and coverage gates into parallel jobs. Retain the same required-check aggregate.
   - **Estimated saving:** **15–25 minutes** on the CI critical path.
   - **Risk:** Medium; requires preserving current coverage thresholds.

4. **Reduce reviewer/editor prompt payloads — critical path**
   - **Evidence:** Run `35878367998` assembled a **562,286-byte** editor prompt; context warnings reached 150K–197K tokens.
   - **Exact change:** Keep immutable instructions first, then append only consensus findings, changed symbols, and targeted snippets. Avoid duplicating the 286KB static context in editor-specific sections.
   - **Estimated saving:** **5–15 minutes** and fewer context overflows per large review.
   - **Risk:** Medium; validate finding recall before rollout.

5. **Circuit-break ledger substate emission — secondary**
   - **Evidence:** Every full review log had 43–82 `record-run-event` timeouts spanning 34–74 minutes.
   - **Exact change:** After the first timeout, emit one `LEDGER_SUBSTATE_DISABLED` warning and aggregate remaining substates into one final event.
   - **Estimated saving:** Direct saving uncertain, likely **0–10 minutes**; substantial reduction in process churn and log noise.
   - **Risk:** Low; primary workflow behavior already fails open.

**Micro-optimization:** Gate irrelevant phase workflows before dispatch. Clarify, plan, implement, and orchestrate-respond produced **543 “other”/mostly skipped runs**; this saves seconds per event but little critical-path time.

## Cost Optimizations

1. **Enforce the repeat-fingerprint cap earlier:** selected avoidable runs consumed **25.52M tokens** after streak three, with no quality benefit.
2. **Short-circuit stalled reviewer retries:** sampled Minimax calls accounted for **23.02M tokens** over 29 calls. A skipped retry can save roughly **0.2–1.6M tokens** per affected slot. Quality risk: medium.
3. **Target a 15–30% uncached-prompt reduction:** applied to 24.78M prompt tokens, this is approximately **3.7–7.4M prompt tokens**. Preserve all findings while removing duplicated repository context.
4. **Use expensive `xhigh` classification conditionally:** workflow-heal intake run `35902789231` used 241,807 tokens for an ultimately deterministic `workflow-defect` classification. Run deterministic signature checks first, escalating only ambiguous cases. Quality risk: medium.
5. **Trim memory retrieval modestly:** retrieval averaged 1,388.5 of 1,400 tokens. Reducing to 900–1,100 tokens is low risk but only a micro-saving.

**Semble:** 31 queries logged 319,758 bytes, averaging 10.3KB/query, with no runtime fallback. This is small relative to 500KB-class prompts and appears to constrain rather than expand context. Three overflow queries in run `35860987963` added only 23,811 bytes.

**Serena:** zero queries, responses, fallbacks, or probes. It is neither saving downstream work nor adding token cost; its rollout is effectively unobservable/inactive.

## Reliability Improvements

1. **Stage the model catalog in lockstep with its writer**
   - **Evidence:** Five of eleven runs logged four catalog errors each for Gemini and GLM across two passes.
   - **Root cause:** `scripts/stage_workflow_support.sh:194-199` prefers the branch catalog, while `write_opencode_config.sh` is main-primary. This can combine a new writer with an old catalog.
   - **Fix:** Make `codex_model_catalog.json` main-primary alongside the writer, and emit both source SHAs/hashes.
   - **Impact:** Restores two missing reviewer slots in affected runs.
   - **Rollback:** Revert both files as one compatibility unit.

2. **Retry transient push failures**
   - **Evidence:** Run `35868679924` received a GitHub `Internal Server Error`; run `35843269131` received a generic remote rejection. Neither matched the non-fast-forward-only retry classifier.
   - **Fix:** Retry boundedly on remote 5xx, `Internal Server Error`, and generic `remote rejected … (failed)` after verifying the remote tip.
   - **Impact:** Could recover one or both sampled push-tail failures.
   - **Fail-safe:** Maximum two retries; never force-push.

3. **Expose editor failure details**
   - **Evidence:** Four editor failures ended with opaque exit code `226`; the step log exposed prompt size and status but not the editor stderr classification.
   - **Fix:** Emit `EDITOR_ATTEMPT_RESULT` with model, attempt, duration, exit code, stall state, stdout/stderr bytes, summary bytes, and a sanitized failure class.
   - **Impact:** Lower MTTR for the repeated `editor_empty_noop` class.
   - **Safety:** Log only a bounded sanitized stderr tail or hash.

4. **Diagnose zero-second failures**
   - **Evidence:** Eleven run archives returned HTTP 404, including run `35901820512`; most had no jobs or failure point.
   - **Fix:** On missing archive, record workflow path, event, jobs count, run timestamps, workflow-validation annotations status, and classify `startup_failure` versus expired/deleted archive.
   - **Impact:** Converts currently opaque failures into actionable telemetry.

5. **Validate staged support-source provenance**
   - **Evidence:** Validate run `35839322615` failed at `Fetch workflow support files`.
   - **Fix:** Emit requested ref, primary/fallback outcomes, resolved SHA, and staged-manifest hash in clarify, plan, review, and validate.
   - **Rollback:** Existing stable→main fail-open behavior remains unchanged.

`BREAK_GLASS` count was zero, so no evidence of rubric/policy pressure. The six `CONTEXT_BUDGET_WARN` events instead indicate prompt-size risk.

## AI Memory Health

- **Retrieve hit rate:** 11/11 (**100%**) with 31–33 records selected.
- **Budget use:** average **1,388.5/1,400 tokens (99.2%)**.
- **Keyword method:** `llm` 11/11; `plain` and `none` 0.
- **Health:** no zero-record retrieves, `fail_open:true`, or `enabled:false` entries.
- **Writes:** 22 `record-run-event`, 11 `record-candidate`, and 16 `write_lessons_learned` operations. Nine writes needed two push attempts; one needed four.
- **Concern:** Successful top-level memory events coexist with 43–82 substate timeouts per full run. Add operation-duration fields for clone, lock wait, commit, fetch, push, and retry reason.
- **Coverage gap:** No `finalize-task`, `promote`, `compact`, or processed-command telemetry appeared. Memory-maintenance run `35830638126` had no log telemetry; verify emission there.

## GH API Call Audit

- Runtime call counts were **not collected**, so reductions below are design estimates.
- `.github/workflows/review_autofix.yml` contains roughly **41 `gh api` call sites**. Common-path duplicates include repeated PR metadata, linked-issue GraphQL, comments, and PR-file lookups.
- The gate already shares one comment snapshot, but the fingerprint-cap job refetches comments and later reported `pr_comments_unavailable`. Pass the snapshot/output forward.
- Fetch `pulls/{PR}` once and reuse title, body, labels, mergeability, head SHA, and state.
- Fetch `pulls/{PR}/files` once; two gate branches currently contain equivalent paginated calls.
- Reuse the initial `closingIssuesReferences` GraphQL result instead of later live refetches.
- The sweep workflow is already well optimized: run `35903646283` snapshotted active runs once per workflow and processed three PRs in about seven seconds.

**Estimated reduction:** **30–50% of review-path read calls**, with lower rate-limit exposure. No rate-limit event was observed.

Add `GH_API_SUMMARY endpoint_group=… calls=… retries=… cache_hits=… rate_remaining_start=… rate_remaining_end=…`.

## Prompt Cache & Memory System

- Aggregate `cache_hit_rate` is unavailable. Run-level values were **0.8775** (`35868679924`) and **0.7617** (`35871101502`).
- Computed cache-read share was **75.6%**, but cache-write tokens were zero and 19/144 usage records were unavailable.
- Model-level sampled cache-read shares varied widely: GLM **87.8%**, DeepSeek **83.6%**, Minimax **81.3%**, Grok **69.8%**, Qwen **54.5%**, Gemini **0%/unsupported**.
- Place volatile diff, memory, timestamps, run IDs, and issue metadata after the stable prompt prefix. Keep model-independent instructions byte-identical between passes.
- Memory retrieval is effective but budget-saturated; place it after the cacheable prefix and reduce low-ranked records.
- Six context warnings show cache value is being eroded by prompt growth even where cache reads are high.
- **Semble:** healthy production behavior—31 queries, no runtime fallback.
- **Serena:** no query/probe telemetry; emit `SERENA_PROBE target=review result=ok|failed|skipped reason=…` before claiming availability.

## Orchestrator Health

- **Healthy controls:** run `35903662496` skipped terminal same-head work in 16s; run `35903666305` blocked the repeated fingerprint in 62s.
- **Pain point:** those controls activated only after multiple hour-long repeats because comment-derived state was unavailable or inconsistent.
- **Poller:** 52 runs, p50 **325.5s**, p95 **534.6s**, with two cancellations. Run `35897188734` explicitly waited for a hosted runner.
- **No-op fanout:** 543 mostly skipped/other runs across clarify, plan, implement, and clarify-response indicate dispatch filtering happens too late.
- **Validation state:** merged PRs `#4330` and `#4335` skipped standalone validation because their issues lacked `ai:orchestrator-validate-required`.

Track: repeat fingerprints per head, gate skips before runner allocation, poll queue seconds, active-run dedup rate, validation-dispatch skips, conflict-heal attempts, and terminal-state age.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Type | Priority fix |
|---|---|---|---|
| Clarify | Invoked run `35903144534` took 290s; cache reservation collision | Compute/cache | Prevent duplicate cache writers |
| Plan | Run `35897713103` took 587s at `xhigh` | Model compute | Deterministic pre-triage and lower reasoning for simple plans |
| Implement | Slow runs reached 1,271–1,457s | Model/validation | Emit phase timing and reuse deterministic context |
| Review/autofix | p95 5,315s; stalls, no-ops, push failures | Retry/model/Git | Repeat guard, reviewer circuit breaker, push retries |
| CI | p50 2,497s; monolithic lint | Compute | Parallel shards |
| Validate | Sole run failed after 372s fetching support files | Setup/reliability | Provenance and manifest checks before long setup |
| Orchestrate poll | p50 325.5s; runner wait and unavailable Semble | Queue/compute | Queue telemetry and early no-work exit |
| Merge/status | Manual conflict PR and ~100s status sync | Conflict/API | Emit conflict class and batch status lookups |

The end-to-end critical path is **review/autofix plus CI**, not clarify/plan dispatch overhead.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 5,315s; CI p50 2,497s; orchestrate poll p50 325.5s.
- **Top failure modes:** repeated editor no-op fingerprint, mixed-version model catalog, non-retried push rejection, missing log archives.
- **Highest-cost drivers:** repeated six-reviewer passes, Minimax stall retries, 150K–197K-token prompts, avoidable same-head reruns.
- **Top actions:**
  1. Enforce persisted fingerprint caps before reviewer invocation.
  2. Stage catalog/writer atomically and add reviewer health circuit breaking.
  3. Retry transient pushes and shard CI.

## Metrics Appendix

### Overall

| Metric | Value |
|---|---:|
| Total runs | 1,000 |
| Success / failure / cancelled / other | 349 / 28 / 17 / 606 |
| Success rate / failure rate | 34.9% / 2.8% |
| Duration p50 / p95 / average | 7s / 2,518s / 310.6s |
| Success-log sample rate | 7% |

### Key workflow families

| Family | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 142 | 96 | 27 | 15 | 4 | 40s | 5,315s |
| ci | 38 | 38 | 0 | 0 | 0 | 2,497s | 2,612s |
| orchestrate_poll | 52 | 50 | 0 | 2 | 0 | 325.5s | 534.6s |
| clarify | 145 | 5 | 0 | 0 | 140 | 1s | 11s |
| plan | 138 | 5 | 0 | 0 | 133 | 1s | 11.5s |
| implement | 139 | 3 | 0 | 0 | 136 | 1s | 10s |
| validate | 1 | 0 | 1 | 0 | 0 | 372s | 372s |

### Cost and cache

| Metric | Value |
|---|---:|
| OpenRouter calls | 144 |
| Usage available / unavailable | 125 / 19 |
| Prompt / completion / total tokens | 24,777,990 / 1,256,816 / 102,891,035 |
| Cache read / write tokens | 76,862,502 / 0 |
| Computed cache-read share | 75.6% |
| Aggregate `cache_hit_rate` | unavailable |
| Codex calls / tokens | 16 / 12,160 |
| `wall_clock_p50_ms` / `p99_ms` | 9,000 / 6,860,500 |
| Break-glass / context warnings | 0 / 6 |

### MCP telemetry

| Server | Queries | Bytes | Fallbacks | Runtime fallbacks | Probe ok/failed/skipped |
|---|---:|---:|---:|---:|---:|
| Semble | 31 | 319,758 | 16 contract-test | 0 | not emitted |
| Serena | 0 | 0 | 0 | 0 | 0 / 0 / 0 |

Deep-dive Semble targets: `reviewer-context` 11 calls/169,377 bytes; `overflow` 3 calls/23,811 bytes. No other MCP servers were observed.

### Material data gaps

- Eleven missing log archives, mostly zero-second failures.
- No runtime GH API call counters.
- No Serena probe telemetry despite `SERENA_AVAILABLE:false` in sampled CI summaries.
- Deep-dive archives are concentrated in review/autofix; conclusions for other families rely partly on sampled `log_summary` evidence.
- Top-level raw `summary.json` contained 15 telemetry runs, while the assembled widened context contained 109; report both coverage tiers separately in future collector output.

## Deep Audit — Workflows & Scripts (2026-09-23)

### Section 1: Bug & Correctness Sweep

All scoped YAML, Bash, and Python files parsed successfully. No direct secret logging or unsafe issue/PR-body interpolation was found.

#### BUG-001

- **File:** `scripts/orchestrate_poll_process.sh:18911-18918,19545-19616,20423-20438,20484-20493,20775-20784,23295-23450`
- **Severity:** High
- **Category:** `bug`
- **Description:** Six merge paths validate a specific PR head through variables such as `_pw_head_sha`, `_rtm_head_sha`, `_rb_merge_sha`, and `N_HEAD_SHA`, but subsequently invoke `gh pr merge` without `--match-head-commit`. A concurrent push between validation and merging can therefore land an unevaluated head. Other repository merge paths explicitly guard this race.
- **Recommended fix:** Add `--match-head-commit "$validated_sha"` to both auto and synchronous merge attempts. Fail closed when the SHA is missing. Extract a shared `merge_pr_at_head <pr> <sha> <mode> [--auto]` helper and add poller contract tests mirroring `review_rb_judge.sh`.

#### BUG-002

- **File:** `scripts/audit_consumer_drift.py:202-219,482-509,576-594` (contract: `agents.md:614-619`)
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The drift auditor loads every `ai-*.yml` template for every consumer, ignoring the documented `core`, `standard`, and `full` profiles. Legitimate core/standard omissions can be reported as drift. Conversely, `review_rb_judge_dispatch.yml` belongs to the standard/full manifests but is excluded by the `ai-*.yml` glob and is never audited. The latter omission is certain; whether current registered consumers receive false positives depends on their configured profiles. **[NEEDS VERIFICATION]**
- **Recommended fix:** Resolve each consumer’s `WORKFLOW_PROFILE`, load the corresponding `workflow-templates/profiles/<profile>.txt`, and audit exactly that manifest. Use `full` only when the profile is unavailable, matching the documented default. Add tests for all three profiles and the non-`ai-*` judge wrapper.

### Section 2: GitHub API Call Redundancy Audit

The prior report already covers repeated PR metadata, comments, linked-issue, and file reads in `review_autofix.yml`; those findings are not duplicated here.

#### API-001

- **File:** `scripts/orchestrate_poll_process.sh:10230-10242`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** When `final_pr_json_snapshot` does not match, the same `pulls/{final_pr}` endpoint is fetched twice consecutively—once for state and once for merged status. **Current:** 2 calls. **Proposed:** 1 call.
- **Recommended fix:** Fetch once through `_fetch_pr_json`, then derive both fields with `_jq_field`. Extend the existing cycle-local PR JSON reuse pattern.

#### BATCH-001

- **File:** `scripts/audit_consumer_drift.py:408-480,482-539`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** For 13 registered consumers and 16 currently selected templates, the auditor performs one directory listing plus one REST content fetch per present wrapper. **Current worst case:** `13 × (1 + 16) = 221` calls. **Proposed:** 13 GraphQL calls—one aliased blob query per repository. **[NEEDS VERIFICATION]** GraphQL query complexity and large-blob behavior require fixture validation.
- **Recommended fix:** Extend the alias-building pattern from `_fetch_candidate_issue_details_graphql`; return a filename-keyed cache containing existence and text for all profile-selected wrappers.

#### BATCH-002

- **File:** `scripts/orchestrate_poll_process.sh:12866-12894`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** `count_noop_ancestors` fetches one issue body and one comments collection per ancestry hop. **Current:** up to 6 calls at default depth 3. **Proposed:** 3 calls with a cached seed body, or 4 on cache miss.
- **Recommended fix:** Accept the current issue body from `_candidate_details_json`, then fetch each parent’s body and comments together through one GraphQL query. Extend `_fetch_candidate_issue_details_graphql`.

#### BATCH-003

- **File:** `scripts/orchestrate_poll_process.sh:15321-15417`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The staged-support latch sweep issues comments, label-event, live-label, and repeated label-event reads per candidate. **Current:** `1 + 4N` reads for `N` candidates. **Proposed:** `1 + 2×ceil(N/50)` reads using one initial and one pre-mutation revalidation batch. **[NEEDS VERIFICATION]** Pagination must preserve the current fail-closed behavior.
- **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql` with labels, trusted comments, and labeled/unlabeled timeline events. Preserve the second live batch immediately before mutation.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001

- **File:** `scripts/assemble_prompt.sh:12-93`; `scripts/render_prompt.sh:12-41,95-159`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** `resolve_prompt_file`, `resolve_render_prompt_py`, and `resolve_assembly_source_path` are duplicated across both prompt entrypoints. Drift in fallback ordering would make assembly and rendering resolve different assets.
- **Recommended fix:** Create `scripts/prompt_path_helpers.sh` owning:
  - `resolve_prompt_file <path>`
  - `resolve_render_prompt_py`
  - `resolve_assembly_source_path <path>`
  
  Source it from both callers.

#### DUP-002

- **File:** `scripts/apply_analysis_on_main.sh:126-150`; `scripts/auto_release_stable.sh:76-99`; `scripts/promote_main_cycle.sh:113-144`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Identical GitHub-output emission and truthy parsing functions appear in three release-cycle scripts.
- **Recommended fix:** Create `scripts/release_cycle_helpers.sh` with `emit_github_output <key> <value>` and `is_truthy <value>`, then source it from all three scripts.

#### DUP-003

- **File:** `scripts/build_semble_wrapper.sh:31-42`; `scripts/install_semble.sh:15-26`; `scripts/setup_serena.sh:81-92`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The same fail-soft `GITHUB_ENV` writer is independently maintained in three bootstrap scripts.
- **Recommended fix:** Create `scripts/github_env_helpers.sh` with `write_github_env <key> <value> <log-prefix>` and update all three callers.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001

- **File:** `.github/workflows/implement.yml:981-1337`
- **Severity:** High
- **Category:** `expression-limit`
- **Description:** The interpolated `Stage workflow support files` block is approximately **20,326 characters**, leaving only **674 characters** before GitHub’s 21,000-character limit.
- **Recommended fix:** Move the block into `scripts/stage_workflow_support.sh`, adding an implement-specific mode for the staged-support ledger and immutable runtime-copy behavior.

#### EXPR-002

- **File:** `.github/workflows/implement.yml:3218-3524`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `Preflight destructive-commit guard` block is approximately **17,313 characters**, leaving **3,687 characters** of headroom.
- **Recommended fix:** Extract it to `scripts/implement_preflight_commit_guard.sh`, or add a `preflight` mode to `implement_commit_changes.sh` so preflight and commit-time guard logic share one implementation.

No workflow exceeds the 800 KB warning threshold. The largest, `review_autofix.yml`, is approximately 541 KB.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001

- **File:** `scripts/resolve_integration_ref.sh:51-69,72-104`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** Issue-body and branch-existence reads use raw `gh api` with no bounded retry or shared rate-limit handling. Five phase workflows depend on this resolver, so a transient API error fails checkout-ref resolution while surrounding GitHub reads use `gh_retry`.
- **Recommended fix:** Stage and source `gh_helpers.sh` with the resolver, then use `gh_retry _safe_gh_jq`. Preserve the current fail-closed response for non-404 branch lookup failures.

#### DEAD-001

- **File:** `scripts/orchestrate_poll_process.sh:9179-9264,9361-9376`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `_check_branch_rebuild_threshold` assigns multiple `BRANCH_REBUILD_SKIP_REASON` values and `BRANCH_REBUILD_LAST_REBUILD_AT`, but neither variable is read. Disabled, cooldown, threshold, and audit-unavailable outcomes therefore lose their structured reason.
- **Recommended fix:** Emit `BRANCH_REBUILD_SKIPPED reason=… last_rebuild_at=…` before returning and include the fields in the rebuild audit record; otherwise remove the unused assignments.

ShellCheck found no actionable SC2086, SC2046, SC2006, or SC2015 violations after GitHub expressions were neutralized. No active TODO/FIXME/HACK markers were found in scoped files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 6 | BUG-002, BATCH-001, BATCH-003, DUP-001, EXPR-002, CONSIST-001 |
| Low | 5 | API-001, BATCH-002, DUP-002, DUP-003, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Medium |
| API call optimization | 2 | Medium |
| Code modularization | 8 existing + 3 new | Medium |
| Expression size reduction | 1 workflow + 2 helpers | Large |
| Medium/Low fixes | 3 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-23)

### Safety Tag Legend

`SAFE_TO_MERGE` permits direct implementation; `NEEDS_VERIFICATION` requires specified validation first; `RISKY_SKIP` identifies opportunities that must not be automated because they involve pagination, races, retries, or poller recovery paths.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — RISKY_SKIP

- **ID:** `MERGE-001`
- **Safety tag:** `RISKY_SKIP`
- **Files/calls:** `scripts/gh_helpers.sh:1238-1244`, `scripts/gh_helpers.sh:1358-1364`; callers at `.github/workflows/review_autofix.yml:7398-7417`.
- **Current call count:** 2 on the editor-changes-lost no-peer path.
- **Proposed call count:** 1 after a successful first snapshot; retain 2 when the first probe fails.
- **Endpoint:** `GET /repos/{repo}/actions/runs?branch={head_branch}&per_page=30`.
- **Evidence:**
  ```bash
  response=$(gh_retry gh api -X GET \
    "/repos/${GITHUB_REPOSITORY}/actions/runs" \
    -f "branch=${head_branch}" -f "per_page=30")
  ```
  Both helpers issue this identical request back-to-back: first to find an in-flight peer, then to count completed runs on the reviewed head.
- **Proposed fix:** Let `autofix_retrigger_has_inflight_peer` optionally persist its validated response to a snapshot file. Extend `autofix_changes_lost_head_retry_consumed` to consume that file, falling back to its existing live request when the file is missing or invalid. Preserve `AUTOFIX_PEER_*` and `AUTOFIX_CHANGES_LOST_BUDGET_*` log keys.
- **Safety rationale:** This explicitly defends against concurrent workflow-run races; reusing stale run state could incorrectly dispatch or suppress recovery.
- **Downstream signal:** Do not auto-implement; manually test peer appearance/disappearance between probes and the case where the first request fails but the second succeeds.

#### MERGE-002 — RISKY_SKIP

- **ID:** `MERGE-002`
- **Safety tag:** `RISKY_SKIP`
- **Files/calls:** `.github/workflows/clarify.yml:479-484`.
- **Current call count:** 2 logical calls when semantic caching is enabled.
- **Proposed call count:** 1 logical paginated call.
- **Endpoint:** `GET /repos/{repo}/issues/{issue}/comments`, ordered ascending.
- **Evidence:**
  ```bash
  gh_retry gh api ".../comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  gh_retry gh api --paginate --slurp \
    ".../comments?sort=created&direction=asc&per_page=100"
  ```
- **Proposed fix:** Fetch one flattened full-comments snapshot, derive `ISSUE_COMMENTS_FILE` from its first 50 entries, and render `THREAD_HISTORY_FILE` from the complete array.
- **Safety rationale:** The second call paginates and currently fails open only for semantic caching, whereas the first call is required prompt context; consolidation could alter partial-page failure behavior.
- **Downstream signal:** Do not auto-implement; validate issues with more than 100 comments, an injected later-page failure, and both enabled and disabled semantic-cache modes.

#### MERGE-003 — RISKY_SKIP

- **ID:** `MERGE-003`
- **Safety tag:** `RISKY_SKIP`
- **Files/calls:** `scripts/orchestrate_poll_process.sh:13727-13733`.
- **Current call count:** 2.
- **Proposed call count:** 1.
- **Endpoint:** `GET /repos/{repo}/issues/{issue}`.
- **Evidence:**
  ```bash
  orig_title="$(gh_retry _safe_gh_jq ".../issues/${issue_num}" --jq '.title // ""' || echo "")"
  orig_body="$(gh_retry _safe_gh_jq ".../issues/${issue_num}" --jq '.body // ""' || echo "")"
  ```
- **Proposed fix:** In `execute_stall_recovery_action`, fetch `{title, body}` once and derive both variables locally.
- **Safety rationale:** The calls are inside `orchestrate_poll_process.sh`’s stall-recovery close-and-reissue path, which is an explicit `RISKY_SKIP` trigger.
- **Downstream signal:** Do not auto-implement; manually verify independent fail-open behavior and freshness after `close_linked_pr`.

#### MERGE-004 — RISKY_SKIP

- **ID:** `MERGE-004`
- **Safety tag:** `RISKY_SKIP`
- **Files/calls:** `scripts/orchestrate_poll_process.sh:14483-14605`, `15527-15528`, `16413-16421`, `19222-19228`, `21397-21399`.
- **Current call count:** 4 incremental REST reads beyond the two existing GraphQL batch fetches.
- **Proposed call count:** 0 incremental REST reads on successful prefetch; one REST fallback per cache miss.
- **Endpoints:** GraphQL `repository.issue` aliases; REST `GET /repos/{repo}/issues/{issue}`.
- **Evidence:**
  ```bash
  _candidate_details_json="$(_fetch_candidate_issue_details_graphql ...)"
  orig_title="$(gh_retry _safe_gh_jq ".../issues/${issue_num}" --jq '.title' || echo "")"
  orig_body="$(gh_retry _safe_gh_jq ".../issues/${issue_num}" --jq '.body // ""' || echo "")"
  ```
  The same pattern follows `_current_wave_details_json` before `IF_TITLE` and `IF_BODY` are re-fetched.
- **Proposed fix:** Add issue-level `title` and `body` fields to `_fetch_candidate_issue_details_graphql` and its transformed cache. Update `run_standalone_stall_recovery` and the implementation-failed sweep to read them cache-first, with one combined REST fallback returning both fields.
- **Safety rationale:** This changes a documented cycle-local cache inside the poller and may alter freshness and fail-open behavior.
- **Downstream signal:** Do not auto-implement; manually test GraphQL batch failure, missing issue nodes, batches over 25 issues, and an issue edited during the poll tick.

#### MERGE-005 — RISKY_SKIP

- **ID:** `MERGE-005`
- **Safety tag:** `RISKY_SKIP`
- **Files/calls:** `scripts/review_merge_train.sh:257-261`, `275-287`, with the shared caller at `354-381`.
- **Current call count:** 2 reads when a marker comment exists.
- **Proposed call count:** 1 read.
- **Endpoints:** `GET /repos/{repo}/issues/{pr}/comments` and `GET /repos/{repo}/issues/comments/{comment_id}`.
- **Evidence:**
  ```bash
  _mt_find_marker_comment_id   # paginated collection lookup
  existing_body="$(gh_retry gh api ".../issues/comments/${existing_id}" --jq '.body')"
  ```
  The paginated collection already contains both comment IDs and bodies.
- **Proposed fix:** Replace `_mt_find_marker_comment_id` with `_mt_find_marker_comment` returning a compact `{id, body}` snapshot, and pass that snapshot into `_mt_upsert_comment`.
- **Safety rationale:** The collection call paginates and participates in merge-train claim/bypass race handling.
- **Downstream signal:** Do not auto-implement; manually verify multi-page ordering, API-failure versus not-found distinction, and concurrent queue-marker replacement.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — RISKY_SKIP

- **ID:** `REUSE-001`
- **Safety tag:** `RISKY_SKIP`
- **Files/calls:** `scripts/orchestrate_poll_process.sh:2523-2525`, `4134-4140`, `11687-11702`, `17466-17481`, `17640-17641`, `17931-17932`, `17974-17975`, `18018-18020`, `18418-18419`, `18488-18489`, `18561-18563`, `22040-22041`, `22358-22360`, `22981-22982`.
- **Current call count:** 14 static repository-metadata read sites; execution is path-dependent, but several can run in one tick.
- **Proposed call count:** 1 successful run-local fetch, with legacy retries on cache miss.
- **Endpoint:** `GET /repos/{repo}` for `.default_branch`.
- **Evidence:**
  ```bash
  CWS_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "")"
  DEFAULT_BRANCH_TRACKING="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  ```
- **Proposed fix:** Add `_repo_default_branch_cached <fallback>` that caches only a validated non-empty successful response. Preserve each caller’s current `""` versus `"main"` fallback and do not cache failures, allowing later callers to retry.
- **Safety rationale:** Consolidation crosses many poller functions and completion/recovery branches with different fail-open and fail-closed contracts.
- **Downstream signal:** Do not auto-implement; manually exercise validation dispatch, security-pass reset, judge, final merge, revalidate, and standalone conflict-sweep paths under both successful and failed metadata reads.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — It is inside `orchestrate_poll_process.sh`; independent fail-open results must be reviewed manually.
- BATCH-001: NEEDS_VERIFICATION — Validate GraphQL query complexity and large blob behavior against fixtures before replacing REST reads.
- BATCH-002: RISKY_SKIP — The ancestry walk is in the poller and includes paginated comment reads.
- BATCH-003: RISKY_SKIP — Pagination and the required pre-mutation revalidation batch protect against upstream races.

### Summary Counts

Counts include new findings and reviewed Deep Audit cross-references.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | BATCH-001 |
| RISKY_SKIP | 9 | MERGE-001, MERGE-002, MERGE-003, MERGE-004, MERGE-005, REUSE-001, API-001, BATCH-002, BATCH-003 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
