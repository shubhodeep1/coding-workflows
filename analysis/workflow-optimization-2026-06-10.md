## Executive Summary

- **`review_autofix` is the dominant latency tail, and the fixed two-pass six-reviewer fan-out is the main culprit.** Across 64 `review_autofix` runs, p50 was 17s but p95 was 3,079.45s. In run `27237778158`, job `review_codex-agent`, step `Run reviewer models` took 1,717s; in run `27239919056` the same step took 1,973s, with six reviewers succeeding in both pass 1 and pass 2. Making pass 2 conditional should cut heavy-review time by roughly 14–17 minutes per affected run. **Impact: high. Confidence: high.**

- **CI `lint` time is long enough to delay downstream autofix decisions.** `ci` p50 was 1,658.5s and p95 was 1,697.55s across 10/10 successful runs; evidence-grade summaries for runs `27242130838` (1,636s) and `27241936336` (1,394s) both show `lint` dominating runtime. `review_autofix` then waited 141s (`27237778158`) and 301s with timeout (`27239919056`) in `Collect PR check-run failures CI lint autofix context`. **Impact: high. Confidence: high.**

- **The only true workflow failure in the window was AI-memory branch contention, and a fix appears to already be landing.** Failed run `27233531187` (`plan / plan` → `Check and claim /answer command`) logged `AI_MEMORY_ERROR: Failed to push memory branch after 5 attempts` and `HEAD -> ai-memory (fetch first)`. A later successful `clarify` run `27242172592` checked out commit `24d3ddb` with message `Raise ai-memory push retry budget 5→8...`. **Impact: medium-high. Confidence: high.**

- **Token spend is almost entirely concentrated in one `implement` execution.** Run `27242652607` used `2,652,382` Codex tokens across `26` calls; repo total was `2,660,489`, so that single run was **99.7%** of observed Codex-token spend. Its `log_summary` also shows `WORKSPACE_CACHE_RESTORE_STATE: disabled`, suggesting repeated context/workspace rebuilds are the first cost target before any model changes. **Impact: high. Confidence: high.**

- **Prompt-cache and Semble telemetry need cleanup before aggressive tuning.** `cache_hit_rate` was null repo-wide, `or_cache_read_tokens`/`or_cache_write_tokens` were zero despite sampled review logs showing `cache_enabled=true`, and sampled `SEMBLE_QUERY` metadata appears doubled when both job-level and per-step logs are parsed: run `27237778158` metadata shows 4 queries / 46,352 bytes, but raw unique lines show 2 queries totaling 23,176 bytes; run `27239919056` shows 8 / 78,296 vs 4 unique queries totaling 39,148 bytes. **Impact: medium. Confidence: high.**

## Speed Optimizations

### Critical-path wins

1. **Make `review_autofix` pass 2 conditional instead of always running six reviewers twice**
   - **Evidence:** `review_autofix` had `64` runs, `p50=17s`, `p95=3079.45s`, `avg=1044.4s`. In run `27237778158`, `review_codex-agent / Run reviewer models` took `1717.35s`; in run `27239919056`, the same step took `1973.41s`. Both runs logged `Pass 1 complete: 6 reviewers successful.` and `Pass 2 complete: 6 reviewers successful.` The block from pass-1 completion to pass-2 summariser completion still added `872.8s` (`27237778158`) and `1042.7s` (`27239919056`).
   - **Root cause:** fixed two-pass, six-model reviewer fan-out even when pass 1 already converged.
   - **Exact change:** only run pass 2 when pass 1 shows reviewer disagreement, high-risk paths, or large diffs; otherwise promote pass-1 consensus directly to the editor. Lowest-risk variant: keep six reviewers in pass 1, but run only the 2–3 dissenting/high-signal reviewers in pass 2.
   - **Estimated time savings:** ~14.5–17.4 minutes on heavy `review_autofix` runs; likely the single biggest reduction in `review_autofix` p95.
   - **Implementation risk:** medium. Keep full pass 2 for risky paths (`scripts/`, `.github/workflows/`, `ai-memory/`, etc.) to fail open on safety.

2. **Stop paying 2–5 minutes for check-run context that often adds <1 KB**
   - **Evidence:** In run `27237778158`, `review_codex-agent / Collect PR check-run failures CI lint autofix context` waited with sleeps of `20s`, `40s`, `80s` and produced only `219` bytes of context in `141.27s`. In run `27239919056`, the same step slept `20s`, `40s`, `80s`, `80s`, `78s`, hit `CHECK_RUNS_WAIT_TIMEOUT` after `300s`, and produced `928` bytes.
   - **Root cause:** the workflow blocks on queued/in-progress check runs before continuing, even when the resulting context is tiny.
   - **Exact change:** lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` from `300` to `90–120` for autofix; stop after two polls when only queued/in-progress checks remain; continue with the latest snapshot instead of waiting for completion.
   - **Estimated time savings:** ~2–5 minutes on affected heavy review runs.
   - **Implementation risk:** low-medium. The step already fails open with a snapshot, so shortening the wait is backward-compatible.

3. **Parallelize or split the long `ci` lint path**
   - **Evidence:** `ci` was consistently long: `p50=1658.5s`, `p95=1697.55s`, `10/10` successful. Evidence-grade summaries for runs `27242130838` and `27241936336` both say `lint` dominated runtime. In the same window, `review_autofix` spent up to `300s` waiting for check-run context from CI.
   - **Root cause:** a single long required `lint` path is gating downstream review/autofix.
   - **Exact change:** split `lint` into parallel required jobs (for example: workflow linting, YAML lint, Python lint, tests/reference validation) and preserve one top-level required status if needed for branch protection.
   - **Estimated time savings:** inference: ~5–12 minutes on CI wall time, plus indirect savings from less `review_autofix` waiting.
   - **Implementation risk:** medium. Main risk is required-check renaming; keep a stable umbrella check if branch protection depends on current names.

4. **Enable safe workspace reuse on the long AI paths**
   - **Evidence:** `WORKSPACE_CACHE_RESTORE_STATE: disabled` appeared repeatedly in slow `review_autofix` runs `27237778158` and `27239919056`, and in `implement` run `27242652607` (which also consumed `2,652,382` tokens).
   - **Root cause:** expensive paths rebuild context/workspace from scratch.
   - **Exact change:** enable reuse keyed by repository + PR + head SHA + workflow-support ref; restore only read-only artifacts (diff summaries, Semble index, previous reviews, metadata), and invalidate on head-SHA change.
   - **Estimated time savings:** ~30–120s per full AI run, with likely additional token savings on `implement`.
   - **Implementation risk:** medium. Mitigate stale-state risk by strict keying and by never reusing mutable git state across head SHAs.

### Micro-optimizations

5. **Reduce event fan-out that ends in immediate skip**
   - **Evidence:** `clarify` had `101` runs with `94` in `other`; `plan` had `93` runs with `84` in `other`; `implement` had `93` with `87` in `other`; `orchestrate_clarify_respond` had `93` with `93` in `other`. Recent skipped runs show the exact false conditions:
     - `plan` run `27243305045`: comment body did not start with `/answer`.
     - `implement` run `27243305003`: comment body did not start with `/approved`.
     - `clarify` run `27243305015`: comment body did not start with `/reclarify`.
     - `orchestrate_clarify_respond` run `27243476472`: comment body did not contain `Clarification required`.
   - **Root cause:** multiple workflows still start on orchestration/status comments that are known non-matches.
   - **Exact change:** move more filtering to trigger-time or a single lightweight dispatcher, so non-command comments do not spawn separate workflow runs.
   - **Estimated time savings:** small per run (mostly 1–8s), but meaningful queue-noise reduction across `358` skipped/no-op runs in the window.
   - **Implementation risk:** low.

## Cost Optimizations

**Important caveat:** prompt-cache effectiveness is not measurable in this window. Repo-wide `cache_hit_rate` is null, and `or_cache_read_tokens` / `or_cache_write_tokens` are zero, even though sampled `review_autofix` logs show `cache_enabled=true` on OpenRouter review calls.

1. **Target `implement` first; it is almost the entire Codex-token bill**
   - **Evidence:** `implement` family telemetry shows `2,652,382` Codex tokens across `26` calls. Repo total is `2,660,489`, so `implement` is **99.7%** of observed Codex-token spend. The sampled run with telemetry, `27242652607`, lasted `912s` and logged `WORKSPACE_CACHE_RESTORE_STATE: disabled`.
   - **Root cause:** inference: `implement` is likely re-sending or rebuilding large unchanged context because no workspace reuse is active.
   - **Exact change:** add per-phase prompt-byte telemetry to `implement`, reuse same-PR same-head workspace/context artifacts, and stop replaying unchanged repository scaffolding across retries/continuations.
   - **Estimated savings:** inference: if repeated context is reduced by even `30–60%`, comparable windows would save roughly `0.8M–1.6M` Codex tokens.
   - **Quality-risk notes:** low, if the change is limited to context dedup/reuse and not model downgrades.

2. **Cut `review_autofix` OpenRouter cost by making pass 2 adaptive**
   - **Evidence:** repo-wide `or_calls=122`, all concentrated in `review_autofix`. Sampled slow runs `27237778158` and `27239919056` each executed `12` reviewer-model calls (`6` pass 1 + `6` pass 2) inside `Run reviewer models`.
   - **Root cause:** every full review pays for a second six-model pass regardless of pass-1 convergence.
   - **Exact change:** only run pass 2 when pass 1 produces disagreement/high-risk findings; otherwise stop after pass-1 consensus or run a smaller second-pass subset.
   - **Estimated savings:** roughly `30–50%` of `review_autofix` OpenRouter call volume; on a repo window like this one, that is about `36–61` fewer OR calls.
   - **Quality-risk notes:** medium. Keep the full second pass for high-risk path patterns and high-disagreement cases.

3. **Shrink editor prompt assembly before considering weaker models**
   - **Evidence:** editor prompt sizes were large:
     - run `27237279915`: `Editor prompt bytes: 223648`
     - run `27237778158`: `Editor prompt bytes: 339327`
     - run `27239919056`: `Editor prompt bytes: 353914`
     - run `27237778158` also logged `PR comments context bytes: 22457`
   - **Root cause:** editor prompts are carrying large volatile blocks: previous reviewer outputs, comments, check-run context, and overflow file excerpts.
   - **Exact change:** pass only deduplicated consensus findings plus touched-file context by default; lower or cap PR comment / log-tail inclusion; keep Semble overflow on-demand instead of embedding broad extra context every time.
   - **Estimated savings:** bounded estimate: likely `10–20%` lower editor prompt size on heavy reviews, with matching latency savings.
   - **Quality-risk notes:** low-medium if consensus + touched-file context remains intact.

4. **Keep Semble on review flows; it looks targeted, not noisy—but fix the accounting**
   - **Evidence:** raw `SEMBLE_QUERY` lines in sampled `review_autofix` runs were targeted:
     - `27237279915`: `target=reviewer-context`, `13702` bytes
     - `27237778158`: `target=reviewer-context`, `14975` bytes; `target=overflow`, `8201` bytes
     - `27239919056`: `target=reviewer-context`, `15524` bytes; three `overflow` queries totaling `23624` bytes
     These are small relative to the `223–354 KB` editor prompts in the same runs. By contrast, CI had `35` Semble fallbacks, all `contract_test` fallbacks, with `0` query bytes and `0` runtime fallbacks.
   - **Root cause:** Semble is being used mainly for targeted reviewer/overflow context in review flows; CI fallback volume is test-mode noise, not runtime load.
   - **Exact change:** do **not** cut Semble first on `review_autofix`; instead, dedupe collector accounting and reduce CI contract-test fallback logging to one summary per run.  
   - **Estimated savings:** small direct dollar savings; higher value is avoiding the wrong optimization target.
   - **Quality-risk notes:** low.  
   - **Semble vs Serena assessment:** inference: Semble appears to be reducing full-file prompt expansion rather than adding low-value noise. Serena had `0` query calls, `0` response bytes, `0` tool calls, and `0` fallbacks/probes, so it neither replaced downstream work nor added noise in this window.

5. **Eliminate avoidable no-op workflow spend**
   - **Evidence:** `358` runs finished in `other/skipped` states across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`.
   - **Root cause:** status comments still fan out into AI workflows that immediately evaluate to false.
   - **Exact change:** route comments through a single command-dispatch gate.
   - **Estimated savings:** small per run, but real runner-minute and queue-pressure reduction.
   - **Quality-risk notes:** low.

## Reliability Improvements

**Window summary:** repo failure rate was `1 / 543 = 0.18%`. `break_glass_count=0` and `context_budget_warn_count=0` repo-wide, so there is no evidence of rubric-pressure overrides or actual prompt-budget alerting in this sample.

1. **Harden AI-memory claim writes on the shared `ai-memory` branch**
   - **Failure evidence:** failed run `27233531187`, workflow `plan`, job `plan / plan`, step `Check and claim /answer command` logged `AI_MEMORY_ERROR: Failed to push memory branch after 5 attempts` and `HEAD -> ai-memory (fetch first)`.
   - **Root cause category:** shared-state concurrency / branch contention.
   - **Exact fix:** keep the retry-budget increase already visible in commit `24d3ddb` (`Raise ai-memory push retry budget 5→8...` in run `27242172592`), and add a smallest-safe fail-open path for claim collisions: fetch/rebase/retry, then re-check whether the command was already claimed remotely before failing the workflow.
   - **Expected reliability impact:** should remove the only observed hard failure in the window.
   - **Rollback / fail-open:** keep duplicate-claim detection authoritative; if the remote already shows a valid claim, skip rather than fail.

2. **Fix provider-error classification so 400 `invalid-argument` does not masquerade as `rate_limit`**
   - **Failure evidence:** in run `27239919056`, `review_codex-agent / Run reviewer models`, reviewer slot `x-ai/grok-4.20` failed on attempt 1 with provider raw code `invalid-argument` (`Invalid tool arguments received... EOF while parsing a string...`) but was classified as `retryable (rate_limit)`. The same slot only succeeded on attempt 2 at `23:07:01`, about `5m36s` later.
   - **Root cause category:** retry taxonomy / provider error parsing.
   - **Exact fix:** map explicit provider `400 invalid-argument` (and similar request-shape errors) to non-retryable; reserve retry logic for 429/5xx/network errors.
   - **Expected reliability impact:** fewer masked request-shape bugs and fewer long pointless retries.
   - **Rollback / fail-open:** keep one generic retry only for clearly transient classes.

3. **Make editor no-summary / no-commit exits explicit and cheap**
   - **Failure evidence:** both slow review runs hit the same warning chain:
     - `27237778158` and `27239919056` `Apply fixes with editor model`: `Editor produced no summary on first iteration — retrying once...`
     - `Detect editor-claimed-but-uncommitted changes`: `Editor claimed changes but no commit was produced...`
     - `Validate editor no-op disposition`: `Editor summary contains failure/fallback markers — editor never completed a validated review.`
   - **Root cause category:** ambiguous fail-open/no-op handling after editor execution.
   - **Exact fix:** after one retry, emit a structured terminal reason (`editor_no_summary`, `editor_no_commit`, `editor_fallback_marker`) and skip downstream validation/comment churn; post one manual-review/no-op comment instead.
   - **Expected reliability impact:** reduces ambiguous “success but suspicious no-op” outcomes and makes reruns/manual triage cleaner.
   - **Rollback / fail-open:** retain the current behavior behind a flag if teams want the extra post-checks.

4. **Treat Semble CI fallbacks as rollout-noise unless runtime fallbacks appear**
   - **Failure evidence:** repo aggregate shows `35` Semble fallbacks, all `contract_test` fallbacks, all in `ci`; `semble_runtime_fallbacks=0`. No sampled `review_autofix` run showed runtime Semble fallbacks. Run `27242131098` shows Semble-enabled review-gate targets succeeding, while `post-merge-validate-dispatch` simply resolved `false`, not as a runtime fallback.
   - **Root cause category:** test/availability noise, not production-path failure.
   - **Exact fix:** keep fail-open behavior; collapse CI contract-test fallback logging to once per run; separately verify CI’s Semble contract-test environment so it does not mask a broken rollout.
   - **Expected reliability impact:** low functional impact, medium alert-fatigue reduction.
   - **Rollback / fail-open:** current fail-open behavior is the correct default; only reduce log volume.

5. **Verify Serena rollout intent instead of troubleshooting absent data**
   - **Failure evidence:** repo aggregate shows `serena_query_calls=0`, `serena_fallbacks=0`, `serena_probe_ok=0`, `serena_probe_failed=0`, `serena_probe_skipped=0`; sampled review summaries also show `SERENA_ENABLED: false`.
   - **Root cause category:** disabled or non-participating feature, not runtime failure.
   - **Exact fix:** if Serena is expected, verify enablement and probe emission; if not expected, remove disabled-status noise from these workflows.
   - **Expected reliability impact:** low direct failure reduction, but clearer rollout state.
   - **Rollback / fail-open:** none needed.

## AI Memory Health

- **Telemetry presence:** deep-dive logs contained `46` raw `AI_MEMORY_TELEMETRY` lines across sampled runs.
- **Observed ops:** `record-run-event=25`, `retrieve=10`, `record-candidate=10`, `processed-command-check=1`.
- **Retrieval effectiveness:** `0 / 10` retrieve lines selected any records (`0%` hit rate). Average `estimated_tokens` was `0`, and `keyword_method` was `llm` on `100%` of retrieve lines. Example retrieve misses appeared in runs `27237279915`, `27237778158`, `27235127456`, `27234325520`, `27236561729`, `27234631103`, `27237790963`, and `27239919056`.
- **Flags:** no sampled `enabled:false` entries; no actual sampled `fail_open:true` entries.
- **Push health:** most successful writes pushed in `1–2` attempts; the highest successful `push_attempts` observed was `3` (for example in `27237279915`, `27236561729`, `27234325520`, `27237790963`). The only hard failure was the plan-claim collision in `27233531187`, which exhausted `5` attempts.
- **Coverage gaps:** no sampled `finalize-task`, `promote`, `compact`, or `processed-command-complete` telemetry was present in the selected deep-dive set.

**Assessment:** the memory write path is active, but the retrieval path is currently ineffective. Right now it adds operational complexity and a small amount of latency without contributing records.

**Recommendation:** validate the retry-budget fix first, then audit why reviewer retrieval returns zero records. If the next window still shows `0%` retrieve hits, either disable review-time retrieval or downgrade keyword-generation effort until the corpus is demonstrably useful.

## GH API Call Audit

No sampled run showed HTTP `429` or secondary rate-limit failures. The issue is **redundancy**, not hard rate limiting.

1. **`review_autofix` re-fetches PR metadata/files multiple times**
   - **Evidence:** run `27237778158`, job `review / gate`:
     - `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"`
     - `gh api "repos/${REPOSITORY}/commits/${PR_HEAD_SHA}"`
     - paginated `pulls/${PR_NUMBER}/files` at two separate code paths
   - The same run later re-fetches labels and PR metadata in `review_codex-agent / Enable auto-merge on PR`:
     - `issues/${PR_NUMBER}/labels?per_page=100`
     - `pulls/${PR_NUMBER}`
   - **Recommendation:** fetch PR metadata once in the gate, persist it as job output/artifact, and reuse it in later steps; memoize the `/files` result instead of calling it twice.
   - **Estimated reduction:** ~2–4 GH API calls per full `review_autofix` run, plus fewer paginated `/files` calls on large PRs.
   - **Rate-limit benefit:** medium; this is the highest-redundancy GH API pattern in the sample.

2. **Check-run polling is an API loop with diminishing returns**
   - **Evidence:** `review_codex-agent / Collect PR check-run failures CI lint autofix context` in run `27239919056` performed at least five wait cycles before timing out after `300s`.
   - **Recommendation:** stop polling after a smaller bounded number of snapshots, or poll only required check names instead of the full check-run surface.
   - **Estimated reduction:** ~3–5 GH API calls on slow/autofix-tailed runs.
   - **Rate-limit benefit:** low-medium.

3. **`cancel_on_pr_close` does rate-limit-aware plumbing even when there is nothing to cancel**
   - **Evidence:** runs `27241798904` and `27241936418` both reported no matching queued/in-progress runs, yet still hit `gh api -i /rate_limit` and entered cancel logic scaffolding.
   - **Recommendation:** only invoke rate-limit/cancel plumbing after a non-empty candidate list is found.
   - **Estimated reduction:** ~1–2 calls per `cancel_on_pr_close` run.
   - **Rate-limit benefit:** low, but easy.

4. **Copilot reviewer is chatty with session-log/progress APIs**
   - **Evidence:** runs `27241806819` and `27243182062` both logged repeated `PUT /agents/sessions/.../logs` and `POST /agents/swe/agent/jobs/.../progress`.
   - **Recommendation:** if the wrapper allows it, batch progress/log updates at a lower frequency.
   - **Estimated reduction:** unquantified from current logs, but likely the biggest GH-API chatter source outside native `gh api` usage.
   - **Rate-limit benefit:** medium if Copilot review volume grows.

## Prompt Cache & Memory System

- **Prompt-cache observability is currently insufficient.** Repo-wide `cache_hit_rate` is null, and `or_cache_read_tokens` / `or_cache_write_tokens` are zero, even though slow `review_autofix` runs `27237778158` and `27239919056` logged `cache_enabled=true` on OpenRouter review calls.
- **Dependency cache works; workspace/prompt cache does not show value yet.** Several runs (`27242059225`, `27241830585`, `27243228068`, `27243397818`, `27242652607`) show `setup-uv` cache hits, so the missing win is not general caching—it is AI/workspace reuse and AI-cache observability.
- **Prompts are large and probably unstable.** Evidence:
  - `Editor prompt bytes`: `223648`, `339327`, `353914`
  - `PR comments context bytes`: `22457` in `27237778158`
  - dynamic additions include reviewer consensus, prior review files, check-run context, and Semble overflow files
- **Cache fragmentation risk (inference):** those volatile sections are likely changing prompt prefixes run-to-run, which erodes prompt-cache reuse even when caching is enabled.
- **Context-budget warning is not firing early enough to be useful.** `CONTEXT_BUDGET_WARN_RATIO: 0.7` is present in sampled logs, but actual `CONTEXT_BUDGET_WARN` count is `0` repo-wide despite multi-hundred-kilobyte prompts.

**Concrete improvements**
1. Emit prompt fingerprint + actual cache read/write tokens per phase (`review pass1`, `review pass2`, `editor`, `implement`) so cache wins are measurable.
2. Freeze stable instructions at the front of prompts; move volatile blocks (reviewer outputs, comments, check-run tails, Semble overflow) to the end.
3. Cap volatile sections more aggressively on retries / pass 2.
4. Since AI-memory retrieval currently returns zero records, do not let empty retrieval scaffolding vary the cacheable prompt prefix.

**Expected impact:** medium on token and latency efficiency once instrumented; low implementation risk because these are formatting/observability changes, not model-behavior changes.

## Orchestrator Health

- **Overall health is good, but noisy.** Only `1` failure across `543` total runs, and active-run dedupe is working: `review_autofix` sweep run `27243525062` ended with `AUTOFIX_SWEEP_END dispatched=0 skipped_active=1 ... candidates=1`.
- **The command/workflow fan-out is much noisier than the actual work.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` generated `371` runs combined, but only `21` were successful; the rest were intentional `other/skipped` paths.
- **Poller setup overhead is material.** In recent run `27243397818`, `poll/poll` started at `23:51:38` but did not record `AI_MEMORY_TELEMETRY ... poll_started` until `23:52:50`, so roughly `72.3s` elapsed before poll work was formally underway.
- **Runner wait is a recurring operational drag.** It appears in `plan` (`27242205281`), `clarify` (`27242172592`), `review_autofix` (`27241936497`, `27242131098`), `ci` (`27241936336`, `27242130838`), and `orchestrate_poll` (`27242059225`, `27243397818`).
- **Semble status emission is operationally confusing in `orchestrate_poll`.** Evidence-grade `log_summary` for `27243397818` says `SEMBLE_AVAILABLE: false`, but the deep-dive log later shows `SEMBLE_AVAILABLE: true` and `SEMBLE_INDEX_AVAILABLE: true` after install/build. Emit one final post-setup tool-status block instead of mixing pre-install and post-install states.

**Smallest safe mitigations**
- reduce workflow fan-out before adding new orchestration logic
- emit final tool availability after setup, not during setup
- track:
  - skip-run ratio by family
  - `orchestrate_poll` setup seconds before `poll_started`
  - `review_autofix` pass-2 rate
  - AI-memory `push_attempts` p95
  - CI check-run wait-timeout count

## Pipeline Flow Bottlenecks

1. **Queueing overhead**
   - Evidence comes from logs, not collector timestamps: many workflows explicitly logged `Waiting for a runner to pick up this job...`.
   - Biggest visible queue/noise contributors: `plan` run `27242205281`, `ci` runs `27241936336` and `27242130838`, `review_autofix` run `27241936497`, and `orchestrate_poll` runs `27242059225` / `27243397818`.
   - **Fix:** reduce skip-trigger fan-out and split only the truly long jobs.

2. **AI compute overhead**
   - `review_autofix` is the dominant compute bottleneck. In the main `review_codex-agent` job:
     - run `27237778158`: reviewer + check-run wait + editor = `2166s`, or **91.7%** of the main job’s `2361s`
     - run `27239919056`: same trio = `2610s`, or **93.2%** of the main job’s `2801s`
   - **Fix:** adaptive pass 2, shorter check-run waits, slimmer editor prompts.

3. **Validation / CI overhead**
   - `ci` `lint` takes ~23–28 minutes and directly affects autofix readiness.
   - **Fix:** parallelize `lint` and reduce required-check latency.

4. **Retry / timeout overhead**
   - xAI invalid-argument retry added ~5.5 minutes in `27239919056`.
   - AI-memory push retries reached `5` attempts in the only failed plan run.
   - Check-run waiting reached the full `300s` timeout in `27239919056`.
   - **Fix:** tighten retry classification and shorten bounded waits.

5. **Merge / admin overhead**
   - GH API refetches for labels, PR metadata, and files are smaller than compute bottlenecks, but they are frequent and easy to cut.
   - **Fix:** memoize PR metadata/files across steps.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: `64` runs, `p95=3079.45s`, `122` OR calls, dominant heavy-tail workflow
  - `ci`: `10` runs, `p95=1697.55s`, `lint` dominates
  - `implement`: sampled run `27242652607` lasted `912s` and consumed `2,652,382` Codex tokens

- **Top failure modes**
  - AI-memory branch contention on shared `ai-memory` branch (`27233531187`)
  - misclassified xAI `400 invalid-argument` retried as rate-limit (`27239919056`)
  - editor no-summary / no-commit ambiguous no-op path (`27237778158`, `27239919056`)

- **Highest-cost drivers**
  - `implement` = **99.7%** of observed Codex-token spend
  - `review_autofix` = all observed OpenRouter call volume (`122` calls)
  - Semble traffic is concentrated in `review_autofix` (`215,077` counted bytes / `18` counted queries), but sampled raw logs suggest those counters are upper bounds due to duplicate parsing

- **Top 3 prioritized actions**
  1. Make `review_autofix` pass 2 conditional and shrink editor prompt assembly
  2. Validate the AI-memory retry-budget fix (`24d3ddb`) and add a fail-open claim-collision path
  3. Enable safe workspace reuse on `implement`/`review_autofix` and add real prompt-cache telemetry

## Metrics Appendix

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s | Codex tokens | Codex calls | OR calls | Semble queries* | Semble bytes* | Semble fallbacks | Serena queries | Cache hit rate | OR cache read/write | break_glass | context warns | wall p50 ms | wall p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 543 | 174 | 1 | 10 | 358 | 0.18% | 2.0 | 1681.0 | 2,660,489 | 36 | 122 | 22* | 246,993* | 35 (35 contract / 0 runtime) | 0 | n/a | 0 / 0 | 0 | 0 | 2,000 | 3,254,230 |

### Key workflow-family metrics

| Workflow family | Runs | S/F/C/O | p50 s | p95 s | Codex tokens | OR calls | Semble q/bytes | cache_hit_rate | wall p50/p99 ms |
|---|---:|---|---:|---:|---:|---:|---|---|---|
| review_autofix | 64 | 55 / 0 / 9 / 0 | 17.0 | 3079.45 | 4,052 | 122 | 18* / 215,077* | n/a | 2,851,500 / 4,987,300 |
| implement | 93 | 6 / 0 / 0 / 87 | 1.0 | 465.4 | 2,652,382 | 0 | 4 / 31,916 | n/a | 1,000 / 757,300 |
| ci | 10 | 10 / 0 / 0 / 0 | 1658.5 | 1697.55 | 0 | 0 | 0 / 0 | n/a | 1,578,000 / 1,695,170 |
| plan | 93 | 8 / 1 / 0 / 84 | 1.0 | 464.6 | 4,055 | 0 | 0 / 0 | n/a | 1,000 / 588,080 |
| clarify | 101 | 7 / 0 / 0 / 94 | 1.0 | 89.0 | 0 | 0 | 0 / 0 | n/a | 1,000 / 72,400 |
| orchestrate_clarify_respond | 93 | 0 / 0 / 0 / 93 | 1.0 | 8.4 | 0 | 0 | 0 / 0 | n/a | 1,000 / 9,520 |
| orchestrate_poll | 36 | 35 / 0 / 1 / 0 | 86.5 | 252.25 | 0 | 0 | 0 / 0 | n/a | 155,000 / 201,720 |
| copilot_pull_request_reviewer | 15 | 15 / 0 / 0 / 0 | 208.0 | 291.2 | 0 | 0 | 0 / 0 | n/a | 268,000 / 307,200 |
| cancel_on_pr_close | 6 | 6 / 0 / 0 / 0 | 6.0 | 8.5 | 0 | 0 | 0 / 0 | n/a | 6,000 / 6,000 |

### Sampled outlier runs used in the analysis

| Run ID | Workflow family | Duration s | Key evidence |
|---|---|---:|---|
| 27233531187 | plan | 110 | only failure; AI-memory push contention on `ai-memory` branch |
| 27237778158 | review_autofix | 5293 | reviewer step 1717s; check-run wait 141s; editor 307s; large prompt and Semble usage |
| 27239919056 | review_autofix | 2852 | reviewer step 1973s; check-run timeout 300s; xAI invalid-argument retried; editor 336s |
| 27242652607 | implement | 912 | 2,652,382 Codex tokens; workspace cache restore disabled |
| 27242130838 | ci | 1636 | `lint` dominated runtime |
| 27241936336 | ci | 1394 | `lint` dominated runtime |
| 27243397818 | orchestrate_poll | 154 | ~72s setup before `poll_started`; runner wait noted |
| 27243525062 | review_autofix | 7 | active-run dedupe prevented duplicate dispatch |
| 27241806819 | copilot_pull_request_reviewer | 308 | repeated Copilot session-log/progress API calls |
| 27243182062 | copilot_pull_request_reviewer | 228 | repeated Copilot session-log/progress API calls |

### GH API hotspot summary

| Workflow / run(s) | Hotspot | Evidence | Suggested reduction |
|---|---|---|---|
| review_autofix / 27237778158 | duplicate PR metadata and `/files` fetches | `review / gate` fetches PR + commit + paginated `/files`; later `Enable auto-merge on PR` fetches labels + PR metadata again | memoize PR metadata/files across jobs/steps |
| review_autofix / slow runs | repeated check-run polling | `Collect PR check-run failures...` waited 141s and 300s with multiple polls | shorten timeout and poll only required checks |
| cancel_on_pr_close / 27241798904, 27241936418 | rate-limit/cancel plumbing with no targets | both runs reported no matching active runs | skip `/rate_limit` and cancel scaffolding when candidate list is empty |
| copilot_pull_request_reviewer / 27241806819, 27243182062 | repeated session log/progress updates | repeated `PUT /agents/sessions/.../logs` and `POST .../progress` | batch or reduce heartbeat frequency if wrapper supports it |

### Semble / Serena / MCP summary

| Scope | Semble queries* | Semble bytes* | Semble fallbacks | Contract-test fallbacks | Runtime fallbacks | Serena queries | Serena response bytes | Serena tool calls | Serena fallbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 22* | 246,993* | 35 | 35 | 0 | 0 | 0 | 0 | 0 |
| review_autofix | 18* | 215,077* | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| implement | 4 | 31,916 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ci | 0 | 0 | 35 | 35 | 0 | 0 | 0 | 0 | 0 |

### Per-target MCP availability

| MCP target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---:|---:|---:|---|
| Serena (all targets) | 0 | 0 | 0 | No `SERENA_PROBE` activity observed in this window |
| Other MCP servers observed | 0 | 0 | 0 | No non-Semble `<NAME>_PROBE` telemetry observed |

\* **Semble-count caveat:** sampled runs `27237778158` and `27239919056` show metadata counts that are exactly double the unique raw `SEMBLE_QUERY` lines because both job-level and per-step logs contain the same events. Treat current Semble query/byte aggregates as **upper bounds** until collector dedupe is added.

## Deep Audit — Workflows & Scripts (2026-06-10)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `scripts/tg_helpers.sh:312-356,381-426`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — Both cleanup loops fetch comment pages, delete matching comments in-place, then increment `page`. Because GitHub paginates the live comment collection, deleting items from page 1 shifts later comments forward; the subsequent `page=2` fetch can skip still-unprocessed tracking comments. The skip can leave stale `<!-- tg_phase:... -->` / `<!-- tg_cleanup:... -->` markers and their Telegram message IDs behind.  
  **Recommended fix** — Stop deleting while paginating forward. Either: (a) fetch all matching comment IDs first with one full paginated read, then delete afterward; or (b) keep refetching page 1 until no tracking markers remain. Reuse `curl_gh_api` from `scripts/gh_helpers.sh` for the read pass so the cleanup path stays rate-limit aware.

- **ID** — BUG-002  
  **File path** — `scripts/tg_helpers.sh:169-205,241-276,346-350,417-421`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — GitHub writes in the Telegram tracking helpers use raw `curl -s -X POST/PATCH/DELETE ... || true` instead of the repo’s retry/status helpers. Because `curl` is not run with `-f`, HTTP 4xx/5xx responses still exit successfully, and the trailing `|| true` suppresses network failures too. Result: marker comments can fail to create/update/delete silently, while the caller thinks tracking or cleanup succeeded.  
  **Recommended fix** — Route all GitHub comment writes through `curl_gh_api`/`gh_retry_to_file` from `scripts/gh_helpers.sh`, check the HTTP status explicitly, and return/log a non-success outcome when the marker mutation did not actually happen.

Sweep note: `bash -n` passed on all `scripts/*.sh`, `python -m py_compile` passed on all `scripts/*.py`, and `yamllint .github/workflows` was clean; I did not find additional high-signal correctness failures beyond the items above.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `.github/workflows/implement.yml:96-105,1254-1255,1396-1401`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The common implement path reads the same issue multiple times: once in the precheck (`GET /issues/{ISSUE_NUMBER}`), again when populating `ISSUE_META_FILE`, and a third time in the label-validation fallback if the cached file is missing/unparseable. **Current call count:** 2 reads on the normal path, 3 on the cache-miss fallback. **Proposed call count:** 1 read total by writing the full precheck payload into `ISSUE_META_FILE` and reusing it downstream. **Existing pattern to extend:** the job-local `ISSUE_META_FILE` cache already present in this workflow.  
  **Recommended fix** — During the precheck step, persist the full issue JSON to `ISSUE_META_FILE` (not just state/labels in a shell variable), then have later steps read body/title/labels from that file and fall back to the API only if the file is absent or invalid.

- **ID** — API-002  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:68-85,438-449`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The workflow refetches both the child issue and the tracking issue across steps: the gate step reads the child issue once and the tracking issue title once; the later context step reads the child issue again and the tracking issue body again. **Current call count:** 3-4 reads on the common path. **Proposed call count:** 1 aliased GraphQL read total, or 2 reads if the child payload is cached and only the tracking issue is fetched once. **Existing batching pattern to extend:** `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — Replace the split reads with a single aliased `repository { issue(number: ...) ... }` GraphQL query for child + tracking issue fields, or persist the first child-issue payload and only fetch the tracking issue once.

- **ID** — API-003  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2879-2888`  
  **Severity** — Low  
  **Category tag** — `api-redundancy`  
  **Description** — The cancel-on-close polling loop fetches `/actions/runs/{id}` twice per iteration: once for `.status` and once for `.conclusion`. **Current call count:** 2 reads per poll iteration. **Proposed call count:** 1 read per iteration by fetching both fields in one JSON object. **Existing batching pattern to extend:** not a batching case; use the same single-response JSON read pattern already used elsewhere in this workflow.  
  **Recommended fix** — Replace the two `gh api` calls with one `gh api ... --jq '{status, conclusion}'`, then split the JSON locally with `jq -r`.

- **ID** — BATCH-001  
  **File path** — `.github/workflows/review_autofix.yml:778-805`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — The post-merge validate-dispatch step starts with one GraphQL read of linked issues, but if `closingIssuesReferences` is empty it falls back to PR-title/body parsing and then does `gh issue view ... --json labels` once per linked issue inside the loop. **Current worst-case read count:** 1 GraphQL + 1 PR read + N issue-label reads. **Proposed read count:** 1 GraphQL + 1 PR read + 1 aliased GraphQL issue batch. **Existing batching pattern to extend:** `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — When the fallback regex produces issue numbers, batch-fetch those issues’ labels with one aliased GraphQL query before entering the loop, then use the cached label set for dispatch/removal decisions.

- **ID** — BATCH-002  
  **File path** — `scripts/review_collect_pr_metadata.sh:129-191`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — Linked-issue context is fetched efficiently via one GraphQL call first, but the fallback path parses issue numbers from the PR body and then loops over them with `GET /issues/{n}` one by one. The code caps the list at 20, so the fallback still does up to 20 separate REST reads. **Current worst-case read count:** 1 GraphQL + up to 20 REST reads. **Proposed read count:** 2 GraphQL reads total. **Existing batching pattern to extend:** `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — After parsing fallback issue numbers, issue one aliased GraphQL batch over those numbers and build `_linked_context_raw` from that payload instead of looping over `gh api repos/.../issues/{n}`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `scripts/tg_helpers.sh:154-278,300-428`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `tg_store_msg_id` and `tg_store_phase_msg_id` are near-identical upsert flows, and `tg_cleanup_phase_msgs` and `tg_cleanup_msgs` are near-identical cleanup flows. The marker syntax changes, but the fetch/parse/update/delete mechanics are duplicated twice.  
  **Recommended fix** — Keep ownership in `scripts/tg_helpers.sh`, but factor the shared logic into helpers such as `tg_store_marker_msg_id <issue_num> <marker_kind> <marker_key> <msg_id>` and `tg_cleanup_marker_msgs <issue_num> <marker_kind> [marker_key]`. Update callers: `tg_store_msg_id`, `tg_store_phase_msg_id`, `tg_cleanup_phase_msgs`, and `tg_cleanup_msgs`.

- **ID** — DUP-002  
  **File path** — `scripts/review_collect_pr_metadata.sh:103-113`; `scripts/gh_helpers.sh:735-760`; `scripts/review_rb_judge.sh:852-860`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `scripts/review_collect_pr_metadata.sh` still open-codes PR payload + issue comments + reviews + review comments hydration, even though `scripts/gh_helpers.sh` already exposes `gh_pr_with_all_comments` for that exact shape, and `scripts/review_rb_judge.sh` already prefers that helper. This leaves two review surfaces to keep in sync.  
  **Recommended fix** — Make `scripts/gh_helpers.sh::gh_pr_with_all_comments <owner> <repo> <pr_number> [preloaded_meta_json]` the sole owner of this data shape. Update `scripts/review_collect_pr_metadata.sh` to call it and write its `meta/comments/review_comments` outputs into the existing artifact files; keep `scripts/review_rb_judge.sh` on the same helper path.

### Section 4: Expression Size Limit Risk Assessment

No current findings above the requested thresholds.

- I did **not** find any single `${{ ... }}` template expression above 2,000 characters in `.github/workflows/*.yml`.
- The largest workflow files are `review_autofix.yml` (371,703 chars) and `implement.yml` (287,888 chars), both well below the 800 KB warning threshold.
- Some interpolated `run:` blocks are large literals, but the embedded `${{ }}` fragments inside them are small; I am not elevating those as current `Exceeded max expression length 21000` risks.

### Section 5: Cross-Cutting Concerns

- **ID** — CONSIST-001  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53`; `.github/workflows/orchestrate_poll.yml:84-118`; `.github/workflows/mark-stable.yml:401-428`; `.github/workflows/test-and-mark-stable.yml:475-489`  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — These workflows each re-implement their own rate-limit/backoff wrapper (`_gh_retry` or `gh_api_safe`) with different failure semantics: some return non-zero, some emit empty strings, some write breaker files, some do neither. That drifts away from the canonical retry contract in `scripts/gh_helpers.sh` and makes API-failure handling workflow-specific.  
  **Recommended fix** — Treat `scripts/gh_helpers.sh::gh_retry` / `curl_gh_api` as the single canonical implementation, and bootstrap that helper through one shared module/composite before these steps so all four workflows converge on the same retry behavior.

- **ID** — DEAD-001  
  **File path** — `scripts/orchestrate_poll_process.sh:9279-9286`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is definition-only in repo runtime code; repo-local search found no call site using it. It keeps a second paginated `/issues/{n}/comments` path alive without any active consumer.  
  **Recommended fix** — Remove the function, or route a real caller through it so the helper has an exercised contract. If it is kept intentionally, add a targeted test that invokes it directly.

- **ID** — DEAD-002  
  **File path** — `scripts/collect_workflow_logs.py:798-805`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `list_run_log_excerpts()` is not called by production code; repo-local search only found the definition (plus a test-local fake). It is a thin wrapper over `_fetch_run_log_archive()` and `extract_log_excerpts()` that currently adds maintenance surface without live use.  
  **Recommended fix** — Remove the wrapper, or switch a real caller to use it so the function’s interface is actually exercised.

Cross-cutting note: I found **no** `TODO` / `FIXME` / `HACK` markers under `.github/workflows` or `scripts`. ShellCheck did report a few low-signal warnings (mostly unused locals / naming reuse), but none rose above the findings listed here.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 5 | BUG-001, BUG-002, API-001, API-002, DUP-002 |
| Low | 7 | API-003, BATCH-001, BATCH-002, DUP-001, CONSIST-001, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 5 | Medium |
| Code modularization | 5 | Medium |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 3 | Small |
