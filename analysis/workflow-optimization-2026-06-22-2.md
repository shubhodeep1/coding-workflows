## Executive Summary

- **Fix CI contract fixtures before tuning anything else.** `ci` failed in `12/14` runs (`85.7%`), with repeated failures in `lint / Review autofix review-pipeline plumbing contract test`; run `27924497867` shows `test_reviewer_failback_harness_reuses_cached_open_state_and_skips_unmapped_models` ending in `subprocess.CalledProcessError`. **Estimated impact:** cut CI red-rate by `50–70%`. **Confidence:** high.
- **The largest speed win is early-exit on `review_autofix` no-op paths.** `review_autofix` is the slowest family (`74` runs, `1095.6s` avg, `3241.9s` p95); run `27924692438` was `skipped` after `882s` with only false gate checks in `log_summary`, while run `27924931472` finished a deterministic `small_diff` skip in `29s`. **Estimated impact:** save `10–18 min` on skipped/no-PR review flows and reduce `review_autofix` p95 by `20–35%`. **Confidence:** high.
- **`test_and_mark_stable` spends most of its time before it knows it has failed.** Run `27922954497` ran `3871s`, executed `57 gh api + 2 gh run` in `e2e-smoke-test`, looped `31` times on `Waiting for ...`, then failed in `validate-scripts / Unit tests` on resolver fingerprint drift. **Estimated impact:** save `5–12 min` on healthy smoke runs and `40+ min` on drift regressions. **Confidence:** high.
- **Primary cost is wasted analysis/context work, not Codex token volume.** Repo totals show only `12,156` Codex tokens across `6` calls, but `workflow_log_analysis` run `27922969841` alone consumed `31/52` Semble queries, `272,899/508,132` Semble bytes, and an `AI_MEMORY_TELEMETRY summarize_unselected_runs` pass with `174,490` tokens before failing with a `SyntaxError`. **Estimated impact:** cut audit-path model/context cost by `40–60%`. **Confidence:** medium-high.
- **Memory and prompt-cache systems are instrumented but not effective yet.** Real AI-memory telemetry shows `8/8` real retrieve ops returned `0` records, `avg estimated_tokens=0`, and repo `cache_hit_rate` is `null` with `or_calls=116` but `or_total_tokens=0`. **Estimated impact:** medium once fixed, but first priority is instrumentation and retrieval quality, not broad model downgrades. **Confidence:** high.
- **Orchestrator core flow is reliable, but control-plane noise is high.** `orchestrate_poll` went `36/36` successful (`183s` p50, `537s` p95), yet recent run `27926182132` flips `SEMBLE_AVAILABLE`/`SEMBLE_INDEX_AVAILABLE` within one poll, and `clarify`/`plan`/`implement` are dominated by 1-second no-op runs (`plan`: `9` successes vs `188` other). **Estimated impact:** lower queue contention and more deterministic context application. **Confidence:** medium.

## Speed Optimizations

1. **[Critical path] Exit `review_autofix` before reviewer bootstrap on deterministic no-op paths**
   - **Evidence:** `review_autofix` has `74` runs with `1095.6s` average and `3241.9s` p95. Run `27924692438` (`log_summary`) was `skipped` after `882s` with only false condition-evaluation steps. Run `27924914012` (`log_summary`) still spent `1086s` on `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR pr=none ... running reviewer panel + commit-comment path`. By contrast, run `27924931472` (`log_summary`) completed a deterministic `small_diff` skip in `29s`.
   - **Root cause:** skip/no-PR decisions are not consistently resolved before heavyweight review bootstrap, MCP setup, and reviewer orchestration.
   - **Exact change:** move `small_diff`, `pr=none`, closed/merged, and all-false gate checks to the earliest possible point in `review_autofix`, before reviewer/editor model bootstrap, Semble queries, and memory retrieval. Keep the heavyweight reviewer panel only behind an explicit “needs review despite no PR” branch.
   - **Estimated time savings:** `10–18 min` on skipped/no-PR runs; `20–35%` reduction in `review_autofix` p95.
   - **Implementation risk:** low-medium, depending on how conservatively the no-PR lane is gated.

2. **[Critical path] Collapse smoke-test polling and fail fast on resolver drift**
   - **Evidence:** run `27922954497` (`Test & Mark Stable Release`) lasted `3871s`; `step-007-e2e-smoke-test.log` contains `57 gh api + 2 gh run` and repeated `Waiting for ...` loops; the full run contains `31` `Waiting for` lines. The run then fails in `step-008-validate-scripts.log` with `Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent`, including `issue #2523 (PR #2524)` reverting a required `scripts/render_prompt.sh` pattern and `issue #2969 (PR #2970)` reintroducing deleted files.
   - **Root cause:** late invariant detection plus repeated serial polling of run status.
   - **Exact change:** run fingerprint verification immediately after resolver/merge output is produced, before phase waits; replace repeated per-phase status calls with one batched status fetch per poll iteration and exponential backoff.
   - **Estimated time savings:** `5–12 min` on healthy smoke runs; `40–60 min` on regression runs that would otherwise fail late.
   - **Implementation risk:** low.

3. **[Critical path] Remove the extra wait introduced by failed `workflow_dispatch` fallback**
   - **Evidence:** run `27924329656` (`review_autofix`, `3202s`) logs `Could not dispatch review workflow via workflow_dispatch; the synchronize event from the push will trigger the next review run instead.` The same family had `14` cancellations out of `74` runs (`18.9%`).
   - **Root cause:** review progression sometimes depends on asynchronous event fallback instead of a deterministic dispatch path.
   - **Exact change:** either restore a stable `workflow_dispatch` path for the downstream review workflow or explicitly stop waiting for dispatch when unsupported and let the synchronize-triggered run take ownership immediately.
   - **Estimated time savings:** `1–5 min` on affected reruns; lower cancel churn.
   - **Implementation risk:** low.

4. **[Micro] Deduplicate PR/file lookups in `review_gate`**
   - **Evidence:** run `27924329656` `step-002-review_gate.log` fetches `/pulls/{PR}`, GraphQL linked issues, commit metadata, and paginated `/pulls/{PR}/files`; the log’s own comments note that `/pulls/{n}` already exposes some gate fields and `/files` should be conditional. The step contains `6 gh api` calls and duplicated `/pulls/{PR}/files` fetch logic.
   - **Root cause:** gate logic refetches data instead of reusing one materialized PR snapshot.
   - **Exact change:** fetch PR JSON once, write it to a temp file, and share one optional `/files` result between doc-only/materiality decisions and later review steps.
   - **Estimated time savings:** `0.5–2s` per run and `3–5` fewer API calls per run.
   - **Implementation risk:** low.

5. **[Micro] Stabilize Semble availability once per poll cycle**
   - **Evidence:** recent `orchestrate_poll` run `27926182132` shows `SEMBLE_AVAILABLE: false` / `SEMBLE_INDEX_AVAILABLE: false` in `Find active tracking issues`, then `Optional Semble installer is unavailable ... leaving Semble disabled`, then `install_semble: Semble 0.1.3 is available`, then `SEMBLE_AVAILABLE: true` but `SEMBLE_INDEX_AVAILABLE: false`, and later both `true` in `Process each tracking issue`.
   - **Root cause:** capability state is allowed to change mid-run.
   - **Exact change:** compute one post-install/post-index capability snapshot and have downstream steps consume that snapshot only.
   - **Estimated time savings:** small (`seconds to tens of seconds`) but reduces variance and wasted retries.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Trim `workflow_log_analysis` breadth before tuning model size**
   - **Evidence:** repo-wide Codex volume is small (`12,156` tokens across `6` calls), but run `27922969841` consumed `31` Semble queries and `272,899` Semble bytes—`59.6%` of repo Semble queries and `53.7%` of repo Semble bytes—then failed with `SyntaxError: unterminated string literal (detected at line 8)`. Its `AI_MEMORY_TELEMETRY` summary step reports `summarized=97`, `targeted=100`, `tokens_used=174490`.
   - **Root cause:** the audit path is doing broad summarization/context assembly on a fragile inline-script path.
   - **Exact change:** move the inline Python into a checked-in script; cap `summarize_unselected_runs` once enough representative coverage is reached; reuse fetched run summaries between `deep-audit` and `analyze-commit-notify` instead of rebuilding context twice.
   - **Estimated savings:** `40–60%` less Semble byte volume in this workflow and elimination of `174k` summarizer-token waste on failing runs.
   - **Quality-risk notes:** low if the sample remains representative (e.g., enough failing + slow + recent coverage).

2. **Keep targeted `reviewer-context` Semble queries; prune noisy overflow queries**
   - **Evidence:** slow `review_autofix` runs `27911188250`, `27906809873`, and `27924329656` each show one targeted `SEMBLE_QUERY target=reviewer-context` around `14.4–15.2 KB` in `506–552ms`. That looks like useful prompt-expansion replacement. But run `27909309905` added overflow on `README.md` and `scripts/ai_memory_lib.py`—`12,912` bytes, `47.2%` of that run’s `27,347` Semble bytes. Run `27912850883` added overflow on `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`, and `scripts/validate_process.sh`—`19,368` bytes, `58.0%` of that run’s `33,404` Semble bytes.
   - **Root cause:** overflow retrieval is pulling large side files after the primary context query, even when the added files are not clearly first-order review inputs.
   - **Exact change:** allow overflow only for changed files or files named by tool errors/test failures; cap overflow to one file or a byte ceiling unless the primary query explicitly signals truncation pressure.
   - **Estimated savings:** `25–60%` Semble-byte reduction on overflow-heavy review runs, plus `0.5–1.5s` latency savings.
   - **Quality-risk notes:** low if changed/error-referenced files remain whitelisted.  
   - **Semble efficiency call:** `reviewer-context` appears efficient; overflow is where low-value byte expansion shows up.

3. **Defer high-reasoning model setup until skip gates pass**
   - **Evidence:** run `27924931472` (`review_autofix`, `29s`, `log_summary`) took a deterministic `small_diff` skip, but still exported `ENABLE_REVIEWER_TWO_PASS=true`, `REVIEWER_PASS2_REASONING_LARGE: xhigh`, and `EDITOR_REASONING_EFFORT: xhigh`. Validate run `27923692562` also ran `MODEL_EDITOR: openai/gpt-5.4` with `MODEL_REASONING_EFFORT: xhigh` before ending `VALIDATION_RAW_STATUS: needs_fixes`.
   - **Root cause:** heavyweight reasoning configuration is applied before deterministic skip/fail-open branches finish evaluating.
   - **Exact change:** delay reviewer/editor reasoning env setup until after materiality/small-diff/no-PR checks; optionally lower only the first-pass validate diagnose branch from `xhigh` to `high` when Serena is disabled and Semble context is tiny.
   - **Estimated savings:** modest direct token savings and small latency savings across skip-heavy branches.
   - **Quality-risk notes:** medium; only safe on deterministic skip branches or explicitly degraded/fail-open paths.

4. **Treat OR/prompt-cache telemetry as a blocker for broader model-cost tuning**
   - **Evidence:** repo totals show `or_calls=116` but `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null`.
   - **Root cause:** prompt-cache/token telemetry for the orchestrator path is incomplete.
   - **Exact change:** emit OR prompt/completion/cache counters and `cache_hit_rate` from the actual model client for every call; do not make broad model-downshift decisions until this is populated.
   - **Estimated savings:** indirect, but it prevents blind tuning and reveals the real prompt-cost hotspots.
   - **Quality-risk notes:** none.

5. **Serena is not yet a proven cost reducer in this window**
   - **Evidence:** repo totals show `serena_query_calls=1`, `serena_query_response_bytes=0`, `serena_query_tool_calls=0`, `serena_fallbacks=3`, and `serena_probe_skipped=1`. Validate run `27923692562` logged two `SERENA_FALLBACK target=validate phase=diagnose reason=disabled` lines.
   - **Root cause:** Serena is mostly disabled or bypassed here; there is not enough response-byte/tool-call evidence to show that it is replacing downstream tool/model work.
   - **Exact change:** bypass Serena wrapper logic entirely when `SERENA_ENABLED=false`; re-evaluate only after response-byte/tool-call telemetry is populated.
   - **Estimated savings:** tiny; mainly reduces log noise and control-path overhead.
   - **Quality-risk notes:** none.  
   - **Serena efficiency call:** insufficient evidence to claim efficient replacement yet.

## Reliability Improvements

1. **Repair the review-pipeline plumbing contract-test harness**
   - **Failure evidence:** `ci` failed in `12/14` runs. In run `27924497867`, `step-035-lint_Review_autofix_review-pipeline_plumbing_contract_test.log` fails inside `tests/test_review_autofix_review_pipeline_contract.py` at `test_reviewer_failback_harness_reuses_cached_open_state_and_skips_unmapped_models` with `subprocess.CalledProcessError`.
   - **Root cause category:** contract-test fixture drift / shell harness mismatch.
   - **Exact fix:** make the contract test use a deterministic stubbed helper path and assert failback behavior from controlled fixture output rather than executing a missing command path.
   - **Expected reliability impact:** removes a recurring CI red path and likely clears a large fraction of `lint` failures.
   - **Rollback / fail-open:** keep the old path behind an env flag until the fixture is stable.

2. **Fix missing Semble contract-test fixtures instead of masking them with repeated fallbacks**
   - **Failure evidence:** repo totals show `71` Semble fallbacks, `67` of them contract-test (`94.4%`). CI runs `27922954670`, `27923218357`, `27923226092`, `27923287456`, and `27923312145` each emitted `5` `SEMBLE_FALLBACK ... context=contract-test` lines; run `27924497867` emitted `10`; `test_and_mark_stable` run `27922954497` emitted `5`. Example line: `SEMBLE_FALLBACK target=overflow file=src/big.py reason=[Errno 2] No such file or directory: '/tmp/.../missing_semble' ms=0 context=contract-test`.
   - **Root cause category:** fixture/config miswiring, not production MCP instability.
   - **Exact fix:** ship a deterministic fake Semble binary for contract tests, or explicitly test the fail-open path without making missing-binary fallback a hard failure in `lint`.
   - **Expected reliability impact:** removes most Semble fallback noise and likely cuts CI failure volume substantially.
   - **Rollback / fail-open:** if the stub cannot be provisioned, assert the fallback marker and continue instead of failing the whole job.

3. **Move fingerprint verification ahead of the long smoke-test path**
   - **Failure evidence:** run `27922954497` failed after `3871s` in `validate-scripts / Unit tests` with `Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent`, including issue/PR pairs `#2523/#2524` and `#2969/#2970`.
   - **Root cause category:** late invariant enforcement.
   - **Exact fix:** run fingerprint verification immediately after resolver output is created and before clarify/plan/implement/review smoke waits begin.
   - **Expected reliability impact:** similar regressions fail in minutes instead of after most of the workflow wall clock.
   - **Rollback / fail-open:** none; this is a stricter preflight of an existing invariant.

4. **Fix the analysis job’s inline Python quoting**
   - **Failure evidence:** run `27922969841` `step-001-deep-audit.log` fails with `SyntaxError: unterminated string literal (detected at line 8)`, followed by `The runner has received a shutdown signal` and `The operation was canceled.`
   - **Root cause category:** generated-script defect.
   - **Exact fix:** move the inline Python snippet into a checked-in script and run a lightweight syntax check before invoking the full audit.
   - **Expected reliability impact:** restores a `1/1` failing workflow family and prevents long audit-path waste.
   - **Rollback / fail-open:** none.

5. **Make Semble capability fail-open deterministic inside `orchestrate_poll`**
   - **Failure evidence:** recent run `27926182132` flips from `SEMBLE_AVAILABLE=false`/`SEMBLE_INDEX_AVAILABLE=false` to `true/false` to `true/true` within one poll cycle.
   - **Root cause category:** rollout/bootstrap state inconsistency.
   - **Exact fix:** set one capability snapshot after install/index and keep all downstream decisions pinned to it for the rest of the run.
   - **Expected reliability impact:** fewer masked context-path differences between early and late poll steps.
   - **Rollback / fail-open:** if setup is unavailable, stay on the current no-Semble path for the whole run.

6. **Treat Serena disabled fallbacks as healthy rare fail-open behavior, not a broken rollout**
   - **Failure evidence:** validate run `27923692562` logged two `SERENA_FALLBACK target=validate phase=diagnose reason=disabled`; repo totals show `3` Serena fallbacks, `0` probe failures, and `1` probe skipped.
   - **Root cause category:** feature disabled / not bootstrapped in this path.
   - **Exact fix:** log one run-level disabled reason and bypass Serena-specific branches entirely when disabled, instead of emitting repeated per-step fallback noise.
   - **Expected reliability impact:** small, but it clarifies operator signal and reduces confusion.
   - **Rollback / fail-open:** none; this preserves today’s fail-open behavior.

**`BREAK_GLASS` / `CONTEXT_BUDGET_WARN` status:** none observed in this window (`break_glass_count=0`, `context_budget_warn_count=0` repo-wide). Current failures point to harness/config drift, not policy pressure or prompt-size exhaustion.

## AI Memory Health

- **Telemetry is present.** I found `44` real `AI_MEMORY_TELEMETRY` events after excluding duplicated deep-dive copies and copied report text.
- **Retrieve effectiveness is currently poor.** There were `8` real `retrieve` operations, `0` with `records_selected > 0` (`0%` hit rate), `avg estimated_tokens=0`, and `keyword_method=llm` on all `8`. No real `retrieve` had `enabled=false`.
- **Zero-hit retrieves are recurring, not isolated.** Real zero-hit retrieves appeared across `review_autofix` runs including `27901454796`, `27906809873`, `27909309905`, `27911188250`, `27912850883`, `27921899203`, and `27924329656`.
- **Fail-open behavior exists, but only in shell tick helpers.** Validate run `27923692562` logged `2` `force-tick-get` and `2` `force-tick-put` events with `ok:false` and `fail_open:true`. That looks like safe continuity fallback, not a hard failure.
- **Push retries are rare.** One `record-candidate` in run `27921899203` needed `push_attempts: 2`; everything else observed succeeded on first push.
- **The analysis workflow is also using the memory system heavily.** Run `27922969841` logged `summarize_unselected_runs` with `summarized=97`, `targeted=100`, `tokens_used=174490`.

**Recommendation:** when the same repo/role sees repeated zero-hit retrieves, short-circuit the LLM-keyword retrieval path on the next run and fall back to `plain` keywords or no retrieval until new memory candidates exist. That is the smallest safe fix because today’s retrieval path adds latency and complexity but contributes `0` selected records.

## GH API Call Audit

The repo already encodes this hygiene standard in code comments: `scripts/lint_pr_body_auto_close.py:243` says the same issue should make one GH call, not N, and `scripts/orchestrate_poll_process.sh` has repeated `CLAUDE.md §15` cache/reuse notes (for example at lines `3238`, `3552`, and `11545`).

1. **`test_and_mark_stable` e2e smoke loop is the biggest GH API hotspot**
   - **Evidence:** run `27922954497`, `step-007-e2e-smoke-test.log`: `57 gh api + 2 gh run`.
   - **Pattern:** repeated `actions/runs/{RID}` status polling and phase-by-phase issue/PR rechecks in loops.
   - **Concrete change:** fetch all needed run/job state once per poll iteration, store it in a temp JSON blob, and have each phase read from that blob; widen polling interval with backoff after the first few checks.
   - **Estimated reduction:** `30–40` GH calls on this run shape; materially lower rate-limit exposure.

2. **`workflow_log_analysis` is re-querying too much for a job that already has a summary bundle**
   - **Evidence:** run `27922969841`, `step-001-deep-audit.log`: `23 gh api + 3 gh issue list + 2 gh run`; `step-002-analyze-commit-notify.log`: `33 gh api + 13 gh issue list`.
   - **Pattern:** both steps appear to hydrate overlapping run/issue context.
   - **Concrete change:** prefetch once, persist the fetched run metadata to an artifact/temp file, and let both steps consume that snapshot.
   - **Estimated reduction:** `20–30` GH calls per audit run; lower chance of secondary throttling.

3. **`review_autofix` gate logic has redundant per-PR lookups**
   - **Evidence:** run `27924329656` `step-002-review_gate.log` has `6 gh api` calls and duplicated `/pulls/{PR}/files` fetches; `step-001-review_codex-agent.log` has another `6 gh api`.
   - **Pattern:** PR metadata, linked issues, commit metadata, and filenames are fetched separately and not reused across steps.
   - **Concrete change:** materialize one PR context file with PR JSON, linked issues, head commit metadata, and optional files list, then pass it into both gate and review steps.
   - **Estimated reduction:** `3–5` API calls per review run; lower PR-loop redundancy.

4. **`validate` is not the main hotspot, but it still duplicates run/issue state**
   - **Evidence:** run `27923692562` `step-001-validate_validate.log` contains `8 gh api + 1 gh issue list + 4 gh run`.
   - **Pattern:** diagnose/enforce stages are re-reading workflow state.
   - **Concrete change:** reuse one current-run and issue-state snapshot across diagnose and final status emission.
   - **Estimated reduction:** `3–5` calls per validate run.

**Rate-limit risk assessment:** no deep-dive run showed a hard 429 or secondary rate-limit failure, but the current hotspots are exactly the kind of repeated-loop patterns that eventually cause them.

## Prompt Cache & Memory System

- **Prompt-cache observability is currently missing.** Repo telemetry shows `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `or_total_tokens=0` despite `or_calls=116`. That means prompt-cache behavior cannot be evaluated reliably from current counters.
- **Observed cache hits are infra-cache hits, not prompt-cache hits.** Slow `review_autofix` run `27924329656` logged `Cache hit occurred on key ...` for `setup-uv`; that is useful dependency caching, but it says nothing about model prompt caching.
- **No prompt-size pressure alarms fired.** There were no real `CONTEXT_BUDGET_WARN` events and no `BREAK_GLASS` events in sampled logs.
- **Inference: prompt-cache fragmentation is likely coming from volatile prefixes.** Review logs vary in PR metadata, linked issues, Semble overflow file lists, and reasoning-mode flags before the actual task content. Combined with `cache_hit_rate=null`, this is the likeliest reason prompt-cache value is low or unmeasured.
- **Memory retrieval is not currently adding value.** The `retrieve` path is always `enabled=true`, always `llm`, and always returns `0` records in this window.

**Concrete improvements**
1. Keep the stable rubric/system prompt prefix constant; move volatile run metadata, timestamps, status text, and Semble result blobs to the suffix.
2. Sort and cap Semble overflow file lists deterministically so equivalent runs produce equivalent prompt shapes.
3. Skip the AI-memory retrieve call after repeated zero-hit runs until new memory candidates exist.
4. Fix OR token/cache emission first; without that, cache tuning will be guesswork.

**Expected impact:** low immediate impact from prompt-cache tuning alone because the telemetry gap is the main blocker; medium future impact once cache counters are real.

## Orchestrator Health

- **Healthy core signal:** `orchestrate_poll` is reliable in this window (`36/36` success, `183s` p50, `537s` p95).
- **Operational pain point 1 — high no-op workflow fan-out.** `clarify` had `209` runs but only `10` successes; `plan` had `197` runs but only `9` successes; `implement` had `197` runs but only `9` successes and `2` cancellations. **Inference:** most of these are expected skip/no-op dispatches, but they still add queue noise and runner churn.
- **Operational pain point 2 — review handoff is not fully deterministic.** Run `27924329656` fell back from `workflow_dispatch` to waiting for a `synchronize` event, which adds timing variance and likely contributes to the `review_autofix` cancel rate (`18.9%`).
- **Operational pain point 3 — MCP capability is mutable within one run.** Recent poll run `27926182132` changed Semble availability state mid-flight.
- **Operational pain point 4 — validate fail-open behavior is safe but noisy.** Run `27923692562` preserved continuity with `fail_open:true` tick helpers, but still took `695s` to arrive at `VALIDATION_STATUS: fail`.

**Smallest safe mitigations**
1. Reduce no-op dispatches upstream where possible, or consolidate them into a caller-side gate.
2. Make review dispatch deterministic, or explicitly stop waiting when deterministic dispatch is unavailable.
3. Snapshot Semble capability once per poll cycle.
4. Emit one compact validate “disabled/fail-open path” annotation instead of repeated Serena fallback lines.

**Observable indicators to track**
- `review_autofix` cancel rate
- no-op run ratio for `clarify` / `plan` / `implement`
- `orchestrate_poll` p95 duration
- count of `workflow_dispatch` fallback warnings
- Semble capability flips within a single poll run
- validate `raw_status` → final conclusion lag

## Pipeline Flow Bottlenecks

1. **Review / Autofix — compute + control-plane bottleneck**
   - **Evidence:** `review_autofix` is the dominant long-tail stage (`1095.6s` avg, `3241.9s` p95). Run `27924329656` took `3202s`; run `27924692438` (`log_summary`) burned `882s` while skipped; run `27924914012` (`log_summary`) spent `1086s` on a no-PR path.
   - **Fix:** early no-op exits, deterministic review dispatch, and deduped PR metadata fetches.

2. **Smoke / Validate — retry + polling bottleneck**
   - **Evidence:** `test_and_mark_stable` run `27922954497` spent `3871s`, made `57 gh api + 2 gh run`, and only then discovered resolver drift.
   - **Fix:** move drift verification earlier and batch status polling.

3. **Clarify → Plan → Implement — queueing/no-op bottleneck**
   - **Evidence:** `clarify` (`209` runs, `199` other), `plan` (`197` runs, `188` other), and `implement` (`197` runs, `186` other) all have `1s` p50 durations, implying large amounts of immediate no-op evaluation.
   - **Fix:** tighten upstream event routing so obviously false `if` branches do not dispatch full workflows when avoidable.

4. **Poller sweep — steady-state control overhead**
   - **Evidence:** even reliable poller runs take `183s` p50; run `27924586605` (`log_summary`) took `217s` while only finding `2 active tracking issue(s)`, and recent run `27926182132` found `1 active tracking issue(s)` then `2 open PR(s) to scan`.
   - **Fix:** reduce bootstrap duplication and pin capability state per cycle.

5. **Merge / conflict overhead**
   - **Evidence:** no repeated conflict-heal retry signature was prominent in this window.
   - **Fix:** none urgent from current evidence; prioritize review/smoke/poller issues first.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  1. `review_autofix` long tail (`74` runs, `3241.9s` p95)
  2. `test_and_mark_stable` smoke/polling loop (`27922954497`, `3871s`)
  3. control-plane no-op churn in `clarify` / `plan` / `implement`

- **Top failure modes**
  1. CI contract-test harness drift (`12/14` CI failures)
  2. Semble contract-test fixture failures (`67` contract-test fallbacks repo-wide)
  3. audit workflow self-failure (`27922969841` `SyntaxError`)

- **Highest-cost drivers**
  1. `workflow_log_analysis` context assembly (`31` Semble queries, `272,899` bytes, `174,490` summarizer tokens in one run)
  2. `review_autofix` overflow Semble context on large side files
  3. reruns / late failures rather than raw Codex token volume

- **Top 3 prioritized actions**
  1. Repair CI contract fixtures (`review-pipeline plumbing` + Semble stub) before further workflow tuning.
  2. Make `review_autofix` skip/no-PR lanes exit before reviewer/bootstrap work.
  3. Move resolver fingerprint verification ahead of long smoke-test waits and batch GH status polling.

## Metrics Appendix

### Overall repo window

| Repo | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) | Wall p50 (ms) | Wall p99 (ms) | Runs w/ log telemetry | Wall-clock samples | Break glass | Context warns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 197 | 15 | 16 | 772 | 121.707 | 2.0 | 448.0 | 9000 | 3792880 | 124 | 122 | 0 | 0 |

### Workflow-family summary

| Family | Runs | Success | Failure | Cancelled | Other | Success rate | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 74 | 58 | 0 | 14 | 2 | 78.4% | 0.0% | 1095.6 | 60.5 | 3241.9 |
| ci | 14 | 2 | 12 | 0 | 0 | 14.3% | 85.7% | 414.6 | 295.0 | 1691.45 |
| validate | 2 | 1 | 1 | 0 | 0 | 50.0% | 50.0% | 462.0 | 462.0 | 671.7 |
| workflow_log_analysis | 1 | 0 | 1 | 0 | 0 | 0.0% | 100.0% | 1011.0 | 1011.0 | 1011.0 |
| orchestrate_poll | 36 | 36 | 0 | 0 | 0 | 100.0% | 0.0% | 236.6 | 183.0 | 537.0 |
| plan | 197 | 9 | 0 | 0 | 188 | 4.6% | 0.0% | 18.6 | 1.0 | 11.0 |
| implement | 197 | 9 | 0 | 2 | 186 | 4.6% | 0.0% | 32.0 | 1.0 | 13.2 |
| clarify | 209 | 10 | 0 | 0 | 199 | 4.8% | 0.0% | 8.9 | 1.0 | 11.0 |

### Cost / cache / MCP telemetry

| Metric | Value | Notes |
|---|---:|---|
| codex_tokens_used | 12156 | Small compared with workflow wall-clock waste |
| codex_calls | 6 |  |
| or_calls | 116 | But `or_total_tokens=0` => instrumentation gap |
| or_prompt_tokens / completion / total | 0 / 0 / 0 | Not credible as true usage; missing telemetry |
| or_cache_write_tokens / read_tokens | 0 / 0 | Prompt-cache instrumentation gap |
| cache_hit_rate | null | No prompt-cache hit-rate data emitted |
| semble_query_calls | 52 | Avg `9771.8` bytes/query |
| semble_query_bytes | 508132 | `workflow_log_analysis` run `27922969841` used `272899` |
| semble_fallbacks | 71 | `67` contract-test, `4` runtime |
| contract-test fallback share | 94.4% | `67 / 71` |
| runtime fallback share | 5.6% | `4 / 71` |
| serena_query_calls | 1 | Not enough to judge replacement efficiency |
| serena_query_response_bytes | 0 | Telemetry gap or zero-value response |
| serena_query_tool_calls | 0 | No per-tool Serena usage recorded |
| serena_fallbacks | 3 | `2` in validate run `27923692562`; `1` elsewhere aggregate-only |
| serena_probe_ok / failed / skipped | 0 / 0 / 1 | No raw target-level probe line surfaced |
| break_glass_count | 0 | No real events observed |
| context_budget_warn_count | 0 | No real events observed |
| other MCP servers observed | none | No unknown `<NAME>_QUERY/FALLBACK/PROBE` lines observed |

### Notable Semble / Serena runs

| Run | Workflow family | Semble queries | Semble bytes | Semble fallbacks | Serena fallbacks | Notes |
|---|---|---:|---:|---:|---:|---|
| 27922969841 | workflow_log_analysis | 31 | 272899 | 11 | 1 | Also failed with `SyntaxError`; repo’s biggest Semble consumer |
| 27923692562 | validate | 1 | 3729 | 0 | 2 | `SERENA_FALLBACK ... reason=disabled`; `VALIDATION_RAW_STATUS: needs_fixes` |
| 27924329656 | review_autofix | 2 | 28728 | 0 | 0 | Contains targeted `reviewer-context` query |
| 27909309905 | review_autofix | 3 | 27347 | 0 | 0 | Overflow on `README.md` + `scripts/ai_memory_lib.py` = `47.2%` of bytes |
| 27912850883 | review_autofix | 4 | 33404 | 0 | 0 | Overflow on `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`, `scripts/validate_process.sh` = `58.0%` of bytes |
| 27922954497 | test_and_mark_stable | 0 | 0 | 5 | 0 | Contract-test Semble fallbacks during a long smoke/fingerprint failure |

### GH API hotspots

| Run | Workflow / step | gh api | gh issue list | gh run | Key redundancy pattern |
|---|---|---:|---:|---:|---|
| 27922954497 | test_and_mark_stable / `step-007-e2e-smoke-test.log` | 57 | 0 | 2 | repeated run-status polling and phase rechecks |
| 27922969841 | workflow_log_analysis / `step-001-deep-audit.log` | 23 | 3 | 2 | overlapping audit hydration |
| 27922969841 | workflow_log_analysis / `step-002-analyze-commit-notify.log` | 33 | 13 | 0 | repeated issue/run summarization lookups |
| 27924329656 | review_autofix / `step-002-review_gate.log` | 6 | 0 | 0 | duplicate `/pulls/{PR}/files` path |
| 27924329656 | review_autofix / `step-001-review_codex-agent.log` | 6 | 0 | 0 | PR metadata not reused from gate |
| 27923692562 | validate / `step-001-validate_validate.log` | 8 | 1 | 4 | diagnose/enforce state rereads |

### AI memory telemetry

| Metric | Value | Notes |
|---|---:|---|
| Real `AI_MEMORY_TELEMETRY` events | 44 | Excludes duplicated deep-dive copies and copied report text |
| `retrieve` ops | 8 | All real, all zero-hit |
| Retrieve hit rate | 0.0% | `0 / 8` with `records_selected > 0` |
| Avg retrieve `estimated_tokens` | 0 | No selected memory payload |
| `keyword_method` distribution | 100% `llm` | No `plain` / `none` real retrieves observed |
| `enabled:false` retrieves | 0 | None observed |
| `fail_open:true` events | 4 | `2` `force-tick-get` + `2` `force-tick-put` in run `27923692562` |
| Push retries > 1 | 1 | run `27921899203` had `record-candidate push_attempts=2` |
| `summarize_unselected_runs` | 1 | run `27922969841`: `summarized=97`, `targeted=100`, `tokens_used=174490` |

### MCP availability rows

| Server | Target | probe_ok | probe_failed | probe_skipped | Evidence |
|---|---|---:|---:|---:|---|
| Serena | unknown (aggregate only) | 0 | 0 | 1 | Repo aggregate telemetry; no raw target-level `SERENA_PROBE` line exposed in deep-dive logs |
| Semble | n/a | n/a | n/a | n/a | No `SEMBLE_PROBE` telemetry format observed; availability inferred only from `SEMBLE_AVAILABLE` / `SEMBLE_INDEX_AVAILABLE` logs |

## Deep Audit — Workflows & Scripts (2026-06-22)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path:** `.github/workflows/review_autofix.yml:4339-4358, 4528-4548, 5386-5395; .github/workflows/issue_pr_status.yml:318-327; scripts/label_helpers.sh:156-206`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** The fallback `set_issue_phase_label_resilient()` implementations in `review_autofix.yml` and `issue_pr_status.yml` only `POST` the target label. They do **not** remove prior phase labels. The canonical helper in `scripts/label_helpers.sh:156-206` does a GET → phase-label strip → PUT replacement. In `review_autofix.yml`, the fallback path is explicitly reachable when fetched helper artifacts are missing (`4322-4323`, `4510-4515`), so an issue can end a run with conflicting phase labels like `ai:done` plus `ai:ready-to-merge` or `ai:review-blocked`, corrupting the pipeline state machine.
- **Recommended fix:** Stop using POST-only fallbacks for phase swaps. Either always stage/source `scripts/label_helpers.sh`, or copy the canonical `set_issue_phase_label_resilient <issue_number> <target_label> [repo]` logic verbatim into one shared fallback helper and call that from `review_autofix.yml` and `issue_pr_status.yml`.

#### CONSIST-001
- **File path:** `.github/workflows/orchestrate.yml:997-1010; scripts/gh_helpers.sh:516-545`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The fallback `_safe_gh_jq` in `orchestrate.yml` is `gh api "$@"` (`999`) rather than the canonical stdout-suppressing implementation in `scripts/gh_helpers.sh:516-545`. The step immediately uses that fallback in command substitutions for `DEFAULT_BRANCH` (`1001`) and `BASE_SHA` (`1009`). If `gh_helpers.sh` fails to load and GitHub returns a non-2xx response, raw error JSON can be captured into those variables instead of failing cleanly.
- **Recommended fix:** Replace the one-line fallback with the canonical temp-file implementation from `scripts/gh_helpers.sh`, or fail the step immediately when `gh_helpers.sh` cannot be sourced. Do not use raw `gh api` as a drop-in `_safe_gh_jq` substitute.

### Section 2: GitHub API Call Redundancy Audit

_Already-covered hotspots in the in-progress report body (for example the smoke-test polling loop and the review-gate `/pulls/{PR}/files` duplication) are not repeated here._

#### BATCH-001
- **File path:** `scripts/review_rb_judge.sh:720-772`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The judge first fetches linked issue numbers with one GraphQL `closingIssuesReferences` call (`720-725`), then loops over those issue numbers and hydrates each issue again with `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"` (`755-772`) just to read `body` and `labels`. That is an N+1 pattern on top of data that GraphQL can already return.
- **Current call count:** `1 GraphQL + up to N REST issue GETs`
- **Proposed call count after fix:** `1 GraphQL total`
- **Existing batching pattern to extend:** The aliased GraphQL patterns already used in `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql` and `.github/workflows/issue_pr_status.yml:364-393`
- **Recommended fix:** Extend the existing `closingIssuesReferences` GraphQL selection to include `body` and `labels(first: 100) { nodes { name } }`, then pick the first usable issue body locally instead of REST-fetching each linked issue.

#### BATCH-002
- **File path:** `scripts/orchestrate_poll_process.sh:11269-11293`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The standalone-stall sweep builds `labeled_issues` by looping over seven labels and issuing one `gh issue list` call per label (`11272-11275`). It then immediately builds `candidates` and prefetches issue details with `_fetch_candidate_issue_details_graphql` (`11292-11293`). This is a fixed seven-call prepass every cycle before any per-candidate work starts.
- **Current call count:** `7 gh issue list + 1 marker GraphQL + 1 details GraphQL`
- **Proposed call count after fix:** `1 bulk open-issue snapshot + 1 marker GraphQL + 1 details GraphQL`
- **Existing batching pattern to extend:** Cycle-local caches like `ACTIVE_WORKFLOW_ISSUES` and the existing `_fetch_candidate_issue_details_graphql` cache fill
- **Recommended fix:** Replace the seven-label loop with one `gh issue list --state open --json number,labels --limit 1000` snapshot (or equivalent GraphQL search), filter the seven labels locally, then feed that result into the existing candidate-details cache.

#### BATCH-003
- **File path:** `scripts/orchestrate_poll_process.sh:2941-2982`
- **Severity:** Low
- **Category tag:** `api-batching`
- **Description:** `_subissue_closing_pr_number()` documents that after the fast `gh pr list --head` miss, it may do one timeline fetch plus one `GET /pulls/{n}` per cross-referenced PR (`2941-2944`). The loop at `2971-2982` performs exactly that REST-per-PR hydration to inspect merged state and body text. This is small-call-count most of the time, but it still scales linearly with the number of cross-referenced PRs and breaks the repo’s own “N items => aliased GraphQL” rule. [NEEDS VERIFICATION]
- **Current call count:** `1 gh pr list --head + 1 timeline fetch + up to N REST pull GETs`
- **Proposed call count after fix:** `1 gh pr list --head + 1 batched GraphQL fetch over the cross-referenced PR numbers`
- **Existing batching pattern to extend:** `scripts/orchestrate_poll_process.sh:_fetch_linked_pr_status_graphql`
- **Recommended fix:** Reuse the cross-referenced PR number set, then batch-fetch `merged/body` (or equivalent fields) in one GraphQL request instead of looping `GET /pulls/{n}`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path:** `scripts/gh_helpers.sh:516-545; .github/workflows/plan.yml:1771-1785; .github/workflows/implement.yml:4725-4735; scripts/implement_diagnose_post_codex_failure.sh:49-60; .github/workflows/orchestrate.yml:998-999`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `_safe_gh_jq` is duplicated in at least four places outside its canonical home in `scripts/gh_helpers.sh`. One copy has already drifted semantically (`orchestrate.yml:998-999`), which is the root cause of `CONSIST-001`.
- **Shared module owner:** `scripts/gh_helpers.sh`
- **Suggested signature:** `_safe_gh_jq <endpoint> [gh api args...]`
- **Callers to update:** `plan.yml` auto-approve gate, `implement.yml` review-blocked job capture, `implement_diagnose_post_codex_failure.sh`, and `orchestrate.yml` integration-branch bootstrap
- **Recommended fix:** Make `gh_helpers.sh` a guaranteed staged dependency for these call sites and remove the inline helper copies. If a fallback must exist, generate one from a single checked-in snippet instead of hand-copying the function body.

#### DUP-002
- **File path:** `scripts/label_helpers.sh:120-206; .github/workflows/review_autofix.yml:953-976, 4324-4358, 4516-4548, 5377-5395; scripts/review_rb_judge.sh:594-646`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Label creation and phase-transition logic is copied inline across `review_autofix.yml` and `review_rb_judge.sh`. The comments in `review_autofix.yml:969-974` already acknowledge that these copies “must stay in lockstep.” Some copies only special-case a subset of labels and fall back to generic color/description defaults, so contract drift in `.github/ai/label_contract.v1.json` will not propagate uniformly.
- **Shared module owner:** `scripts/label_helpers.sh`
- **Suggested signature:** `ensure_label_exists <label_name> [repo]` and `set_issue_phase_label_resilient <issue_number> <target_label> [repo]`
- **Callers to update:** `review_autofix.yml` deterministic-skip merge path, post-merge ready-to-merge / review-blocked paths, and `scripts/review_rb_judge.sh`
- **Recommended fix:** Source `scripts/label_helpers.sh` everywhere instead of inlining label catalogs. If a lightweight fallback is required, generate it from the checked-in label contract during `stage_workflow_support.sh` so the contract has one source of truth.

### Section 4: Expression Size Limit Risk Assessment

- No `run:` or `if:` block exceeded the requested `15,000`-char Medium-risk or `18,000`-char High-risk thresholds.
- The largest interpolated `run:` block I found was `.github/workflows/implement.yml:3767-3994` at about `14,742` characters — `258` chars below the `15,000`-char warning threshold and `6,258` below the `21,000` hard cap.
- No workflow file exceeds the `800 KB` early-warning size. The largest workflow is `.github/workflows/review_autofix.yml` at `342,968` bytes.

### Section 5: Cross-Cutting Concerns

#### DEAD-001
- **File path:** `scripts/orchestrate_poll_process.sh:6161-6235, 15477-15507, 15920-15974`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `BRANCH_REBUILD_LAST_REBUILD_AT`, `RB_FOLLOWUP_REFUSED`, and `IF_BLOCKERS_SOURCE` are assigned, but no read sites exist elsewhere in `scripts/orchestrate_poll_process.sh`. They currently do not affect control flow, persisted state, comments, or telemetry, which makes them dead state plumbing rather than live behavior.
- **Recommended fix:** Either remove these assignments or wire them into a real output path (state JSON, log event, or comment rendering) so the state they represent is observable and testable.

#### SHELL-001
- **File path:** `scripts/review_enable_auto_merge.sh:19; scripts/review_collect_pr_metadata.sh:27`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** Both helpers trigger ShellCheck `SC1007` on `SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"`. The code works, but the assignment form is ambiguous enough that linting flags it.
- **Recommended fix:** Rewrite as `SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"`, or keep the current form with a narrow `# shellcheck disable=SC1007` and an intent comment.

- No material `TODO` / `FIXME` / `HACK` markers were present; the grep hits were `mktemp ... XXXXXX` placeholders, not debt markers.
- I did **not** count ShellCheck `SC2154` on `_bws_effective_threshold`, `_csc_effective_threshold`, or `_rtm_effective_threshold` as findings, because `scripts/orchestrate_poll_process.sh:3633-3638` populates those names intentionally via `printf -v` out-parameter assignment.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 5 | CONSIST-001, BATCH-001, BATCH-002, DUP-001, DUP-002 |
| Low | 3 | BATCH-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3-4 | Medium |
| API call optimization | 2-3 | Medium |
| Code modularization | 6-9 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 3-5 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-22)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is fully proven safe in the current code path. `NEEDS_VERIFICATION` means the overlap is real but pagination, cache freshness, or failure semantics still need a targeted check before implementation. `RISKY_SKIP` means the redundancy is visible, but the call lives in protected/racy control-plane logic and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — NEEDS_VERIFICATION
- **File path and line ranges:** `.github/workflows/clarify.yml:419-449`
- **Current call count / proposed call count:** `2` GH API calls on semantic-cache-enabled runs → `1` common-path call, with the small-call fallback retained only if the full fetch fails
- **Endpoint(s):**
  - `GET /repos/{repo}/issues/{ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50`
  - `GET /repos/{repo}/issues/{ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100` via `--paginate --slurp`
- **Evidence:**
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
    if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
      | jq -r 'add // [] | .[] | "[" + (.created_at // "") + "] @" + (.user.login // "unknown") + ":\n" + (.body // "") + "\n"' > "${THREAD_HISTORY_FILE}"; then
  ```
  The second call is a strict superset of the first call’s data shape; `ISSUE_COMMENTS_FILE` is later consumed as JSON at `.github/workflows/clarify.yml:534`, while `THREAD_HISTORY_FILE` is built from the full thread.
- **Proposed fix:** In the same step, when `SEMANTIC_CACHE_BACKEND != none`, fetch the full paginated/slurped comment array once to a temp file, write `ISSUE_COMMENTS_FILE` from `.[0:50]`, and derive `THREAD_HISTORY_FILE` from the full array. If that full fetch fails, preserve today’s fail-open behavior by falling back to the current 50-comment call plus the cache-bypass placeholder.
- **Safety rationale:** The two calls hit the same resource in the same step, but they do not have identical pagination or identical failure behavior today.
- **Downstream signal:** Verify that `ISSUE_COMMENTS_FILE` built from the first 50 elements of the slurped full-thread JSON is byte-for-byte acceptable to all downstream parsers, and that a failed full-thread fetch still leaves clarify with the same prompt-context and cache-bypass behavior it has now.

#### MERGE-002 — NEEDS_VERIFICATION
- **File path and line ranges:** `scripts/review_collect_pr_metadata.sh:209-226,228-234`; existing consolidation helper at `scripts/gh_helpers.sh:735-900`; existing helper consumers at `scripts/review_rb_judge.sh:859-867` and `scripts/orchestrate_poll_process.sh:14836-14844`
- **Current call count / proposed call count:** `3` mandatory REST calls + `1` optional REST call (`REVIEW_BREAK_GLASS_ENABLED`) → `1` GraphQL-first helper call on the common path
- **Endpoint(s):**
  - Current:
    - `GET /repos/{repo}/pulls/{PR_NUMBER}`
    - `GET /repos/{repo}/issues/{PR_NUMBER}/comments` with `--paginate`
    - `GET /repos/{repo}/pulls/{PR_NUMBER}/comments` with `--paginate`
    - optional `GET /repos/{repo}/pulls/{PR_NUMBER}/reviews` with `--paginate`
  - Proposed:
    - existing `gh_pr_with_all_comments` GraphQL-first helper in `scripts/gh_helpers.sh`
- **Evidence:**
  ```bash
  gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
  gh_retry "${issue_comments_raw}" api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments"
  ...
  if gh_retry "${reviews_raw}" api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/reviews"; then
  ...
  gh_retry "${review_comments_raw}" api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/comments"
  ```

  ```bash
  # gh_pr_with_all_comments — GraphQL-first consolidated PR context.
  # Emits JSON object:
  # {
  #   "meta": {"title", "body", "head_ref", "base_ref", "head_sha"},
  #   "comments": [...],
  #   "review_comments": [...]
  # }
  ```
  The repo already has a purpose-built consolidation helper, but `review_collect_pr_metadata.sh` still fans out into separate REST calls.
- **Proposed fix:** Extend `gh_pr_with_all_comments` to emit any still-missing fields needed by `review_collect_pr_metadata.sh` (notably top-level `reviews[]`, and any PR payload fields still read downstream), then replace `scripts/review_collect_pr_metadata.sh:209-234` with one helper invocation that fans the returned JSON back out into `PR_PAYLOAD_FILE`, `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEWS_FILE`, and `PR_REVIEW_COMMENTS_FILE`.
- **Safety rationale:** The overlap is real and an existing helper already models it, but the helper is currently fail-open and does not yet prove parity with every field/exit contract that `review_collect_pr_metadata.sh` enforces.
- **Downstream signal:** Verify that a helper-backed payload can supply every field later read from `PR_PAYLOAD_FILE`, `PR_META_FILE`, and `PR_REVIEWS_FILE`, and that helper fallback behavior can preserve the current step’s mandatory-failure contract before replacing the REST fan-out.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — NEEDS_VERIFICATION
- **File path and line ranges:** `scripts/review_enable_auto_merge.sh:64-75,127-139,192-225`; `.github/workflows/review_autofix.yml:1407-1415,1702-1707,4687-4715`; `scripts/review_collect_pr_metadata.sh:209-234`
- **Current call count / proposed call count:** `2` helper-local API calls → `1` common-path API call (`/labels`), with `/pulls/{PR}` retained only as cache-miss fallback
- **Endpoint(s):**
  - `GET /repos/{repo}/issues/{PR_NUMBER}/labels?per_page=100`
  - `GET /repos/{repo}/pulls/{PR_NUMBER}`
- **Evidence:**
  ```bash
  # review_collect_pr_metadata.sh
  gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
  ...
  jq '{ title: (.title // ""), body: (.body // ""), baseRefName: (.base.ref // ""),
        headRefName: (.head.ref // ""), headRepoFullName: (.head.repo.full_name // "") }' \
    "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```

  ```bash
  # review_enable_auto_merge.sh
  if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' ...)"; then
  ...
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" ...)"; then
  ...
  _orch_pr_head_ref="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.head.ref // ""' ...)"
  _orch_pr_body="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.body // ""' ...)"
  ```
  The same `review_autofix` job already materializes PR metadata before the later auto-merge step runs.
- **Proposed fix:** Add a validated fast path in `review_enable_auto_merge.sh` that reads `PR_PAYLOAD_FILE` (or `PR_META_FILE` plus a validated payload companion) for `head.ref` and `body`, and only falls back to live `/pulls/{PR}` when the file is missing or malformed. Keep the paginated labels call for the `e2e-smoke-test` guard unless/until someone proves the embedded label list on `/pulls/{PR}` is complete for >30-label PRs.
- **Safety rationale:** The data is already materialized earlier in the same job, but the reuse crosses workflow steps and must be checked against later body/head updates and cache-validity assumptions.
- **Downstream signal:** Verify that no step between “Collect PR metadata” and “Enable auto-merge on PR” mutates the needed `head.ref`/`body` fields in a way that requires a live read, and that the file-backed fast path still handles malformed/missing cache files by falling back exactly once to `/pulls/{PR}`.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — RISKY_SKIP
- **File path and line ranges:** `scripts/orchestrate_poll_process.sh:17641-17655`
- **Current call count / proposed call count:** `1` → `0`
- **Endpoint(s):** `GET /repos/{repo}` (`.default_branch`)
- **Evidence:**
  ```bash
  STANDALONE_PRS="$(gh_retry gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --json number,headRefName,baseRefName \
    --limit 100 2>/dev/null || echo "[]")"

  STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
  echo "Found ${STANDALONE_COUNT} open PR(s) to scan."

  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  ```
  In this standalone conflict-sweep block, `${DEFAULT_BRANCH}` is assigned and the subsequent sweep uses `S_PR`, `S_HEAD`, and `S_BASE`; there is no read of `${DEFAULT_BRANCH}` after the assignment inside this section.
- **Proposed fix:** Remove the lookup only after a manual reviewer confirms there is no later indirect/global expansion in the remaining top-level script path that depends on this specific post-`17654` assignment.
- **Safety rationale:** The value is statically unused here, but the call sits inside `scripts/orchestrate_poll_process.sh`, which the safety contract explicitly treats as a protected control-plane path.
- **Downstream signal:** Do **not** auto-implement; manual review must confirm that deleting this assignment cannot perturb poller-side race handling, shell-global variable assumptions, or log-scraper expectations inside the standalone conflict sweep.

### Cross-References to Deep Audit Section
- BATCH-001: NEEDS_VERIFICATION — the N+1 pattern is real, but the expanded GraphQL selection must preserve `review_rb_judge.sh`’s first-usable-body and label-propagation behavior before replacing the per-issue REST GETs.
- BATCH-002: RISKY_SKIP — agreed hotspot, but it lives in `scripts/orchestrate_poll_process.sh`’s stall-recovery loop, so it is not an auto-implementable merge under this pass’s safety rules.
- BATCH-003: RISKY_SKIP — agreed N+1 shape, but it is inside `scripts/orchestrate_poll_process.sh` and part of cross-reference resolution that explicitly guards against upstream race conditions.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | MERGE-001, MERGE-002, REUSE-001 |
| RISKY_SKIP | 1 | DEAD-API-001 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
