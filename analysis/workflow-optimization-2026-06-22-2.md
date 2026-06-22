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
