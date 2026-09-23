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
