## Executive Summary

- **Tiny PRs are missing the deterministic small-diff skip.** In `shubhodeep1/coding-workflows`, run **25954929371** (`review_autofix`, `review / gate / Evaluate review gate`) logged `pr=2633 files=2 additions=1 deletions=? ... small_diff=false skip=false`, and cancelled follow-up run **25956428376** logged the same failure mode with `files=3 additions=2 deletions=?`. That PR then paid for a full **2427s** review/autofix run. **Estimated impact:** ~38-40 minutes saved per affected trivial PR, plus near-total reviewer/editor cost avoidance. **Confidence:** high.

- **`review_autofix` is the dominant bottleneck and cost center.** It consumed **78,692s / 114,128s = 69.0%** of all observed wall time. In run **25954929371**, `review / codex-agent / Run reviewer models` took **1363.9s** and `Apply fixes with editor model` took **754.2s**. **Estimated impact:** biggest speed + cost win comes from shrinking or skipping this path. **Confidence:** high.

- **CI failures are deterministic and quick to fix.** The `ci` family failed **5/17 runs (29.4%)**. Three failures were the same ShellCheck parse error in `scripts/review_apply_fixes.sh` (runs **25938975842**, **25938980466**, **25954929349**). Two were the same validation contract failure (`implement.yml missing resolved-ref log output`) in runs **25955702244** and **25956422446**. **Estimated impact:** removes ~**3425s** (~57 min) of failed CI wall time in this window and cuts reruns. **Confidence:** high.

- **Late failure detection is wasting CI time.** In run **25956422446**, `CI / lint / Orchestrate poll process unit tests` ran **636.6s**, then `Validation self-test unit tests` failed in **3.5s**. Run **25955702244** showed the same pattern (**516.2s** + **2.9s**). **Estimated impact:** save ~9-11 minutes on each similar failing CI run by reordering checks. **Confidence:** high.

- **AI memory is adding overhead without recall value.** Across 8 deep-dive `review_autofix` runs, memory `retrieve` hit rate was **0/8 = 0%**, `keyword_method` was **none` 8/8**, and `estimated_tokens` averaged **0**. In run **25954929371**, memory steps alone cost ~**53.2s**. **Estimated impact:** modest latency win and cleaner memory signal if retrieval is tuned or temporarily gated. **Confidence:** high.

- **Operational metrics are being distorted by skipped runs.** Raw conclusions were **203 success / 6 failure / 14 cancelled / 484 skipped**, but the analyzer buckets skipped into `other_count`, so repo-level failure rates look artificially low. The skipped control plane is mostly cheap (~**650s** total across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`), but it obscures real bottlenecks. **Estimated impact:** better triage and SLA accuracy, not raw latency. **Confidence:** high.

## Speed Optimizations

**Critical-path wins first.**

1. **Fix the tiny-PR deterministic skip gate.**  
   - **Evidence:** Run **25954929371** (`review_autofix`, `review / gate / Evaluate review gate`) logged `AUTOFIX_GATE_DET_SKIP_EVAL pr=2633 files=2 additions=1 deletions=? max_add=10 max_del=10 doc_only=false small_diff=false skip=false`. Cancelled run **25956428376** logged the same pattern for the same PR family with `files=3 additions=2 deletions=?`.  
   - **Root cause:** the gate fail-opens to `small_diff=false` when deletion totals are missing/non-numeric, so trivial PRs never reach the skip path.  
   - **Exact change:** in `.github/workflows/review_autofix.yml` (gate logic around lines **245-250** and the `AUTOFIX_GATE_DET_SKIP_EVAL` log at line **469**), add a one-time fallback that computes totals from `/pulls/{pr}/files` if either `pr_additions` or `pr_deletions` is empty/non-numeric, instead of forcing `small_diff=false`. Keep the existing `force-review` override.  
   - **Estimated time savings:** **2250-2427s** per affected tiny-PR run (~**37.5-40.5 min**).  
   - **Implementation risk:** **low-medium**; safe if the fallback only triggers on malformed diffstat input.

2. **Move fast-failing CI checks ahead of long unit suites.**  
   - **Evidence:** Run **25956422446**: `CI / lint / Orchestrate poll process unit tests` ran **636.6s**, then `Validation self-test unit tests` failed in **3.5s**. Run **25955702244**: same pattern, **516.2s** then **2.9s**. ShellCheck failures in **25938975842**, **25938980466**, and **25954929349** happened after **729-752s** total runtime. In `.github/workflows/ci.yml`, `Orchestrate poll process unit tests` is at line **133**, `Validation self-test unit tests` at **153**, and `ShellCheck static analysis` much later at **431**.  
   - **Root cause:** deterministic syntax/contract checks are placed after expensive tests in one serial `lint` job.  
   - **Exact change:** split out a required `lint-fast` job (YAML/actionlint/python syntax/validation self-test/ShellCheck), or at minimum move `Validation self-test unit tests` and `ShellCheck static analysis` above `Orchestrate poll process unit tests`.  
   - **Estimated time savings:** **516-637s** per validation-self-test failure and **729-752s** per ShellCheck failure.  
   - **Implementation risk:** **low**.

3. **Shorten or decouple check-run polling in `review_autofix`.**  
   - **Evidence:** Run **25954929371**, step `review / codex-agent / Collect PR check-run failures (CI/lint autofix context)`, lasted **120.8s** and logged four waits before `CHECK_RUNS_WAIT_TIMEOUT reached after 120s with 1 check-run(s) still queued/in_progress; proceeding with snapshot.` The workflow default is `CHECK_RUNS_WAIT_TIMEOUT_SECS: 120` in `.github/workflows/review_autofix.yml` line **157**.  
   - **Root cause:** optional check-run context collection blocks the critical path while waiting for sibling CI to finish.  
   - **Exact change:** reduce the default timeout from **120s** to **30-45s**, or do one immediate snapshot plus one short retry only when the snapshot is empty and checks are still in flight. Because the step already fail-opens, correctness risk is low.  
   - **Estimated time savings:** **60-121s** on affected `review_autofix` runs.  
   - **Implementation risk:** **low**.

4. **Remove artifact cleanup from the Copilot review success path.**  
   - **Evidence:** Run **25954929829** (`copilot_pull_request_reviewer`) reported `Cleanup artifacts` at ~**155s** inside a **195s** run. In run **25956423941**, the same workflow used separate jobs for `Prepare` (**2.4s**), `Agent` (**116.8s**), `Upload results` (**8.8s**), and cleanup, with runner-wait system logs on Prepare/Upload/Cleanup.  
   - **Root cause:** multi-job orchestration reacquires runners and performs artifact list/delete work on the foreground path.  
   - **Exact change:** make cleanup best-effort `if: always()` after user-visible results are posted, or skip immediate deletion when artifact count is tiny and retention policy is acceptable. Also reuse PR metadata/base SHA between jobs instead of re-fetching it.  
   - **Estimated time savings:** **60-155s** on affected Copilot review runs.  
   - **Implementation risk:** **low-medium**.

**Micro-optimizations to defer:** the skipped control plane (`clarify`, `plan`, `implement`, `orchestrate_clarify_respond`) generated **482 skipped runs**, but only ~**650s** total wall time, so suppressing those runs is not the first speed priority.

## Cost Optimizations

**Exact token/dollar telemetry was not emitted in this window, so ranking is based on observed model usage, runtime, and fan-out.**

1. **Stop spending full reviewer/editor budget on tiny PRs.**  
   - **Evidence:** The same PR (#2633) missed deterministic skip in run **25954929371** and paid for a full **2427s** `review_autofix` pass.  
   - **Root cause:** the small-diff gate is broken when deletion totals are missing.  
   - **Exact change:** same gate fix as in Speed #1.  
   - **Estimated savings:** **very large**; likely **80-95%** of AI spend on affected trivial PRs *(inference)* because it avoids the reviewer panel, consolidator, and editor entirely.  
   - **Quality-risk note:** low, because the existing `force-review` override can preserve full review on any PR that needs it.

2. **Right-size the `review_autofix` reviewer/editor stack by risk.**  
   - **Evidence:** Run **25954929371** logged **6 reviewer models**, `MODEL_EDITOR: openai/gpt-5.4`, `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: xhigh`, and `REVIEW_CONSOLIDATOR_MODEL: openai/gpt-5.4`. That single run spent **1363.9s** in `Run reviewer models` and **754.2s** in `Apply fixes with editor model`.  
   - **Root cause:** the same expensive fan-out profile is used for ordinary code PRs and higher-risk workflow/infrastructure changes.  
   - **Exact change:** keep the full 6-reviewer/xhigh profile for workflow edits, CI-breakage PRs, or reviewer disagreement; otherwise use **2-3 reviewers** and lower pass-1 reasoning to `high` or `medium`, escalating only when reviewers disagree or `force-review` is present.  
   - **Estimated savings:** **30-60%** of `review_autofix` AI spend on eligible runs *(inference)*.  
   - **Quality-risk note:** medium; ship behind repo variables and compare issue quality before widening rollout.

3. **Keep Semble; it looks bounded and useful, not noisy.**  
   - **Evidence:** Deep-dive runtime logs contained **12 actual `SEMBLE_QUERY` events** across 8 `review_autofix` runs: **8** `target=reviewer-context` and **4** `target=overflow`, totaling **148,957 bytes** with **471.6ms** average latency. Example: run **25954929371**, `review / codex-agent / Run reviewer models`, `SEMBLE_QUERY target=reviewer-context chunks=12 bytes=15339 ms=455`.  
   - **Root cause / interpretation:** Semble is supplying targeted context in small payloads relative to multi-minute reviewer/editor work. That suggests it is probably reducing raw prompt expansion rather than adding noisy low-value bytes *(inference)*.  
   - **Exact change:** keep Semble enabled for `reviewer-context` and `overflow`, but add token accounting on the downstream AI calls that consume its output so the prompt-size benefit becomes measurable.  
   - **Estimated savings:** likely positive/neutral on token cost; exact dollar savings unavailable because prompt/completion metrics were not emitted.  
   - **Quality-risk note:** low.

4. **Do not spend time tuning Serena yet; it is not active in the sampled runtime.**  
   - **Evidence:** No runtime `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed. Review/autofix run **25954929371** logged `SERENA_ENABLED: false`; recent implement/plan summaries also showed Serena absent or unavailable.  
   - **Root cause:** Serena is currently disabled or not bootstrapped in the sampled paths.  
   - **Exact change:** defer Serena-specific optimization until runtime telemetry exists; when re-enabled, require query/fallback/probe emission with response-byte logging.  
   - **Estimated savings:** **0 right now**.  
   - **Quality-risk note:** none.

5. **Turn prompt-cache from a design assumption into a measured control.**  
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` was logged in review/autofix run **25954929371** and plan run **25954591397**. The repo already pre-assembles static prompt prefixes (`review_autofix.yml:1383`, `implement.yml:1024`, `plan.yml:794`; see also `scripts/build_static_context.sh:7`). `scripts/review_apply_fixes.sh:411-425` explicitly documents the prompt-prefix cache break and accepts ~**420** non-cached tokens/run for a quality reason. But no sampled runtime emitted `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.  
   - **Root cause:** usage normalization exists in `scripts/review_run_reviewers.sh:105-113`, but no real-call telemetry was emitted in this window.  
   - **Exact change:** emit normalized usage for actual reviewer/editor/plan/implement Codex calls, not only optional cache probes. Then tune only the unstable prompt regions whose cache-read rate is actually poor.  
   - **Estimated savings:** unknown until telemetry exists.  
   - **Quality-risk note:** low.

## Reliability Improvements

1. **Fix the ShellCheck parse regression in `scripts/review_apply_fixes.sh`.**  
   - **Failure evidence:** CI failures **25938975842** (**752s**), **25938980466** (**729s**), and **25954929349** (**740s**) all failed in `CI / lint / ShellCheck static analysis`. Each tail log ends with `scripts/review_apply_fixes.sh` line **854**, `SC1073 Couldn't parse this redirection`, `SC1072 Fix any mentioned problems and try again`.  
   - **Root cause category:** shell template / heredoc parse error.  
   - **Exact fix:** make the long prompt text around `scripts/review_apply_fixes.sh:854` shell-literal safe (single-quoted heredoc or escaped backticks) so ShellCheck parses it as text instead of syntax.  
   - **Expected reliability impact:** removes **3/5** recent CI failures.  
   - **Rollback / fail-open:** trivial revert; runtime behavior is unchanged except for the prompt literal.

2. **Keep workflow contract tests and workflow edits in lockstep.**  
   - **Failure evidence:** CI failures **25955702244** (**542s**) and **25956422446** (**662s**) both failed in `CI / lint / Validation self-test unit tests` with `AssertionError: implement.yml missing resolved-ref log output`.  
   - **Root cause category:** workflow/test contract drift.  
   - **Exact fix:** merge/rebase branches so `.github/workflows/implement.yml` includes the expected line (current main already has `echo "Resolved ref: ..."` at line **361**), and require workflow + contract test updates to land together.  
   - **Expected reliability impact:** removes the remaining **2/5** recent CI failures.  
   - **Rollback / fail-open:** if the log wording must change, update the single contract string instead of disabling the audit.

3. **Mark test-only Semble fallbacks so they do not look like rollout failures.**  
   - **Failure evidence:** **15** actual `SEMBLE_FALLBACK` lines were observed, all in CI runs **25938975842**, **25938980466**, and **25954929349**, all `target=overflow`, all `ms=0`, and all inside passing Semble fail-open contract tests. No production fallback lines were observed.  
   - **Root cause category:** observability ambiguity, not runtime breakage.  
   - **Exact fix:** tag these lines with `test_fixture=true` (or equivalent analyzer-side step filtering) so fail-open tests stay visible without looking like a broken Semble rollout.  
   - **Expected reliability impact:** lowers false-positive incident noise and makes real rollout failures easier to detect.  
   - **Rollback / fail-open:** none; actual fail-open behavior remains intact.

4. **Guarantee log export for failed `review_autofix` runs.**  
   - **Failure evidence:** `review_autofix` failure **25907085670** (**942s**) has only `metadata.json`; there are no step logs, excerpts, or step-level failure point.  
   - **Root cause category:** log collection gap.  
   - **Exact fix:** always include failed `review_autofix` runs in deep-dive export; if download fails, emit an explicit collector error entry instead of silently leaving metadata-only evidence.  
   - **Expected reliability impact:** faster RCA and fewer blind reruns after failures.  
   - **Rollback / fail-open:** analysis-only change.

5. **Pay down the Node 20 action-runtime deprecation now.**  
   - **Failure evidence:** Node 20 deprecation warnings appeared in multiple sampled runs, including review/autofix deep dives and recent summaries (**25955121169**, **25954591397**, **25954786564**). The repo still references `astral-sh/setup-uv@v3` in multiple workflows (for example `.github/workflows/implement.yml:627`, `.github/workflows/plan.yml:733`).  
   - **Root cause category:** upstream action runtime deprecation.  
   - **Exact fix:** move Node 20-targeting actions to Node 24-compatible versions, starting with `setup-uv` and any cache actions still being forced forward.  
   - **Expected reliability impact:** preventative; reduces risk of a future hard break.  
   - **Rollback / fail-open:** pin back only if upstream regressions appear.

## AI Memory Health

- **Observed coverage:** only `review_autofix` deep-dive logs emitted runtime `AI_MEMORY_TELEMETRY:` lines. No sampled `plan`, `implement`, `orchestrate`, `orchestrate_poll`, or Copilot review deep-dive logs emitted memory telemetry.

- **Observed runs:** **8** `review_autofix` runs emitted deduped runtime telemetry: **25919301321**, **25927682586**, **25931256122**, **25951655677**, **25951672388**, **25953204787**, **25953446431**, **25954929371**.

- **Operation mix:** **32** deduped events total: **16** `record-run-event`, **8** `retrieve`, **8** `record-candidate`. No `finalize-task`, `promote`, `compact`, or `processed-command-*` operations were observed.

- **Retrieve effectiveness:**  
  - Hit rate: **0/8 = 0%** (`records_selected > 0` never occurred).  
  - Average `estimated_tokens`: **0**.  
  - `keyword_method` distribution: **none 8/8**, **plain 0**, **llm 0**.  
  - `enabled: false` retrieves: **0**.  
  - `fail_open: true` retrieves: **0**.  
  - Every retrieve returned **0 records**.

- **Measured overhead:** in run **25954929371**, memory-specific steps consumed ~**53.2s** total:
  - `Record review run start in memory` **16.5s**
  - `Retrieve reviewer memory context` **5.3s**
  - `Record reviewer consensus candidate in memory` **15.6s**
  - `Record review run completion in memory` **15.8s**

- **Push retry pressure:** high retry counts appeared on writes:
  - run **25951672388**: `record-run-event` push attempts **3**
  - run **25951655677**: `record-run-event` push attempts **2**

- **Recommendation:** do not widen memory usage until retrieval is giving recall. First fix retrieval seeding (keywords are effectively absent today), then keep memory only where hit rate becomes non-zero. If memory is intended outside `review_autofix`, verify emission there because the sampled deep dives did not show it.

## GH API Call Audit

1. **`review_autofix` check-run polling is the biggest sampled API redundancy.**  
   - **Evidence:** Run **25954929371**, `review / codex-agent / Collect PR check-run failures (CI/lint autofix context)`, repeatedly queried the same `commits/{sha}/check-runs?per_page=100` endpoint and waited the full **120.8s** before timing out. The workflow default is at `.github/workflows/review_autofix.yml:157`; the loop itself is at **1881-1950**.  
   - **Pattern:** repeated snapshot polling on the same SHA inside one run.  
   - **Concrete change:** reduce the timeout and snapshot count as described in Speed #3, or reuse a previously captured snapshot when the SHA has not changed.  
   - **Estimated call-count reduction:** about **40-80%** for this step on runs that currently hit the timeout.  
   - **Rate-limit risk reduction:** moderate.

2. **Copilot PR review duplicates PR metadata across jobs/steps.**  
   - **Evidence:** Run **25956423941**:
     - `Prepare` uses `github.rest.pulls.get` plus paginated `github.rest.pulls.listFiles`.
     - `Agent` later calls `gh api .../pulls/{PR}` for the full diff and another `gh api .../pulls/{PR} --jq '.base.sha'`.
     - Cleanup then lists artifacts and deletes them individually.  
   - **Pattern:** same PR is re-read in separate jobs instead of sharing outputs; artifact cleanup is one-list-plus-N-delete calls.  
   - **Concrete change:** pass base SHA/file list/diff metadata as job outputs or a lightweight artifact, and batch/skip immediate artifact deletion when safe.  
   - **Estimated call-count reduction:** at least **2 redundant PR lookups** per run plus **N** artifact delete calls.  
   - **Rate-limit risk reduction:** low-moderate.

3. **The repo already contains one good batching pattern; reuse it.**  
   - **Evidence:** `.github/workflows/review_autofix_sweep.yml:107-181` snapshots active review runs once per workflow into `active_review_runs[...]` and then reuses that local snapshot per PR instead of doing N×2 active-run lookups. Recent sweep run **25955874879** then skipped PR **#2633** with `reason=active_run`.  
   - **Pattern:** batch once, reuse locally.  
   - **Concrete change:** copy this pattern to any per-item GH API loops, especially `review_autofix` check-run collection and multi-job Copilot review prep.  
   - **Estimated call-count reduction:** depends on adoption area, but the pattern is already proven in-repo.  
   - **Rate-limit risk reduction:** high where applied.

4. **No sampled rate-limit incidents were observed.**  
   - **Evidence:** no deep-dive logs showed HTTP 429s or secondary rate-limit messages.  
   - **Implication:** API hygiene is not failing today, but the two redundant patterns above are the likeliest future pressure points.

## Prompt Cache & Memory System

- **Prompt-cache architecture is already in place.** `review_autofix.yml:1383`, `plan.yml:794`, `implement.yml:1024`, and `scripts/build_static_context.sh:7` all explicitly separate a stable, cacheable prompt prefix from dynamic run-specific context.

- **One important non-cacheable prompt tail is intentional.** `scripts/review_apply_fixes.sh:411-425` documents that the provider cache breaks at the first PR-specific embed and that keeping a tail-positioned edit-discipline copy costs about **420 tokens/run** but prevents a much larger empty-output failure mode. That tradeoff looks correct; do **not** blindly hoist it for cache-hit reasons.

- **What is missing is measurement.** No sampled runtime emitted:
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
  - `INFO: openrouter usage ...`  
  even though `scripts/review_run_reviewers.sh:105-113` can normalize those fields.

- **Likely cache-fragmentation sources (inference):**
  - per-PR metadata and diffs entering immediately after the static prefix,
  - repeating near-identical dynamic context across six reviewer calls plus an editor call,
  - run-specific noise such as reordered overflow snippets or changing reviewer scope material.

- **Memory retrieval is currently the weaker part of the system.** It is writing events and candidates, but retrieval is returning nothing (0% hit rate), so memory is not improving prompt quality in the sampled runs.

- **Concrete improvements:**
  1. Emit usage/cache stats for **real** Codex calls, not only optional probes.  
  2. Keep the current static-prefix preassembly; it is directionally right.  
  3. Keep volatile material after the cacheable prefix and make overflow/context ordering deterministic where possible.  
  4. If memory hit rate stays at 0, gate review-memory retrieval off after an N-run streak or fall back to a plain keyword seed before paying write/read overhead.

- **Expected impact:** token savings are **unquantified** until usage emission exists; latency savings from better memory gating are probably **small-to-moderate** (~5-50s on affected runs); reliability impact is positive.

## Orchestrator Health

- **The control plane looks noisy, not stuck.**  
  - `clarify`: **134** runs, **126** skipped  
  - `plan`: **124** runs, **116** skipped  
  - `implement`: **124** runs, **116** skipped  
  - `orchestrate_clarify_respond`: **124** runs, **124** skipped  
  Average skipped-run duration was only ~**1.1-1.5s**, so these are mostly healthy guard-condition no-ops, not broken loops. Example: runs **25956421162** and **25956139113** skipped because `contains(..., 'Clarification required')` was false.

- **But observability is weak.** The analyzer reports those skipped runs as `other_count`, so the repo-level failure rate looks much cleaner than the user experience actually is.

- **Runner queueing is common.** Hosted-runner wait messages appeared in **19 unique deep-dive runs** across `review_autofix`, `ci`, `copilot_pull_request_reviewer`, `forward_merge_stable_to_main`, and `promote_main_to_stable`. This is a cross-pipeline latency tax, not a single-workflow bug.

- **`review_autofix` has meaningful cancel churn.** There were **14 cancelled** `review_autofix` runs totaling **16,581s** of wall time. The worst were **25953441853** (**2421s**) and **25953200338** (**2392s**). Deep-dive logs do not show an explicit failing step for those runs, so the most likely explanation is stale queued/abandoned work rather than compute failure *(inference)*. The current concurrency rules in `.github/workflows/review_autofix.yml:741-742` keep PR-backed runs queued (`cancel-in-progress: false`).

- **MCP availability is opaque.** In `orchestrate_poll` run **25956119498**, the workflow logged `SEMBLE_ENABLED: true` but `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`; the run still succeeded, so fail-open behavior looks healthy, but there were no structured probe lines to quantify availability.

- **Smallest safe mitigations:**
  1. Add explicit `skipped_count` to the workflow-log summaries and dashboards.  
  2. Track `review_autofix` cancel wall time and queue age separately from compute time.  
  3. Emit one structured Semble/Serena availability line per run so “enabled but unavailable” is visible without log scraping.  
  4. Alert only on repeated cancelled `review_autofix` runs where `codex-agent` never starts, not on all cancellations.

- **Track these indicators:** `review_autofix` cancel wall time, skipped/control-plane ratio, runner-wait run count, Semble available-false rate, AI memory retrieve hit rate.

## Pipeline Flow Bottlenecks

1. **Compute bottleneck — `review_autofix`.**  
   This is the dominant end-to-end cost center: **69.0%** of all observed wall time. The critical path is reviewer fan-out + editor, not the surrounding orchestration. Example: run **25954929371** spent **1363.9s** in reviewer models and **754.2s** in the editor.

2. **Retry/rerun bottleneck — deterministic CI failures.**  
   `ci` consumed **12,369s** total and failed **29.4%** of the time (**5/17 runs**). Those failures are not flaky: they are two repeatable defects. Until they are fixed, merge feedback remains expensive and slow.

3. **Queueing bottleneck — hosted-runner waits and multi-job workflows.**  
   Runner wait appeared across 19 deep-dive runs. Copilot review is especially exposed because it splits work across Prepare/Agent/Upload/Cleanup jobs, each of which can reacquire a runner.

4. **Coordination bottleneck — check-run polling.**  
   The `review_autofix` check-run context step waits on sibling CI for up to **120s** even though it is optional and already fail-open. This is coordination overhead, not useful compute.

5. **Merge/conflict overhead — not dominant in this sample.**  
   No recurring merge-conflict resolver or conflict-heal retry pattern dominated the sampled logs. I would not optimize here first without a broader collection window.

6. **Control-plane fan-out — high count, low runtime.**  
   The clarify/plan/implement/respond fan-out creates lots of skipped runs and metric noise, but only ~**650s** total wall time. It is an observability problem before it is a speed problem.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` dominates wall time (**78,692s**, **69.0%** share).
  - `ci` is the main merge blocker (**5/17 failures**, **29.4%**).
  - Runner waits and multi-job orchestration add tail latency across review and release workflows.

- **Top failure modes**
  - ShellCheck parse regression in `scripts/review_apply_fixes.sh` (runs **25938975842**, **25938980466**, **25954929349**).
  - Validation contract drift around `implement.yml` resolved-ref logging (runs **25955702244**, **25956422446**).
  - One `review_autofix` failure with missing exported logs (**25907085670**), so root cause is unknown.

- **Highest-cost drivers**
  - Tiny PRs missing deterministic skip (`deletions=?` in gate logs for PR #2633).
  - Six-reviewer `xhigh` review panel plus `gpt-5.4` editor on `review_autofix`.
  - Long cancelled `review_autofix` runs that add wall-clock churn without clear useful work.

- **Top 3 prioritized actions**
  1. **Fix the small-diff deterministic skip gate** so tiny PRs do not pay full review/autofix cost.
  2. **Split or reorder CI into fast-fail vs long-test lanes** so ShellCheck and validation self-tests fail early.
  3. **Shorten `review_autofix` check-run waiting and reuse PR metadata across jobs** to reduce GH API churn and tail latency.

## Metrics Appendix

| Scope | Runs | Success | Failure | Cancelled | Skipped/Other | Failure rate | p50 s | p95 s | Wall time s | Share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Overall | 707 | 203 | 6 | 14 | 484 | 0.85%* | 1.0 | 994.0 | 114,128 | 100.0% |
| `review_autofix` | 100 | 83 | 1 | 14 | 2 | 1.0% | 174.0 | 2420.0 | 78,692 | 69.0% |
| `ci` | 17 | 12 | 5 | 0 | 0 | 29.4% | 740.0 | 784.6 | 12,369 | 10.8% |
| `implement` | 124 | 8 | 0 | 0 | 116 | 0.0% | 1.0 | 523.9 | 6,476 | 5.7% |
| `plan` | 124 | 8 | 0 | 0 | 116 | 0.0% | 1.0 | 533.9 | 4,829 | 4.2% |
| `clarify` | 134 | 8 | 0 | 0 | 126 | 0.0% | 1.0 | 76.7 | 851 | 0.7% |
| `orchestrate_clarify_respond` | 124 | 0 | 0 | 0 | 124 | 0.0% | 1.0 | 2.0 | 171 | 0.1% |
| `orchestrate_poll` | 32 | 32 | 0 | 0 | 0 | 0.0% | 111.0 | 159.45 | 4,210 | 3.7% |
| `copilot_pull_request_reviewer` | 21 | 21 | 0 | 0 | 0 | 0.0% | 197.0 | 283.0 | 4,237 | 3.7% |

\* Raw run conclusions were **203 success / 6 failure / 14 cancelled / 484 skipped**. In the current analyzer, skipped conclusions are rolled into `other_count`, so the repo-level failure rate is misleading.

| Deterministic failing runs | Workflow / job / step | Duration s | Evidence |
| --- | --- | ---: | --- |
| 25938975842 | `CI / lint / ShellCheck static analysis` | 752 | `scripts/review_apply_fixes.sh` line 854 -> `SC1073` / `SC1072` |
| 25938980466 | `CI / lint / ShellCheck static analysis` | 729 | same ShellCheck parse failure |
| 25954929349 | `CI / lint / ShellCheck static analysis` | 740 | same ShellCheck parse failure |
| 25955702244 | `CI / lint / Validation self-test unit tests` | 542 | `AssertionError: implement.yml missing resolved-ref log output` |
| 25956422446 | `CI / lint / Validation self-test unit tests` | 662 | same validation contract failure |
| 25907085670 | `review_autofix / unknown` | 942 | metadata only; no step logs exported |

| Token / cache metric | Value | Notes |
| --- | --- | --- |
| Prompt tokens total | Not emitted | No sampled runtime logged normalized OpenRouter usage |
| Completion tokens total | Not emitted | Same gap |
| Total tokens total | Not emitted | Same gap |
| `cache_creation_input_tokens` | Not emitted | `scripts/review_run_reviewers.sh:105-113` can emit this, but no runtime lines were present |
| `cache_read_input_tokens` | Not emitted | Same gap |
| Prompt cache disabled flag | `false` observed in review/autofix **25954929371** and plan **25954591397** | Indicates prompt cache was intended to be active |
| GitHub Actions Codex CLI cache | Cache hit on **25954591397** (`codex-v0.114.0`) | Infra cache, not model prompt cache |

| MCP server | Event | Count | Bytes / response_bytes | Avg bytes | Avg ms | Notes |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Semble | `QUERY` | 12 | 148,957 | 12,413 | 471.6 | Runtime deep dives only |
| Semble | `FALLBACK` | 15 | n/a | n/a | 0.0 | All `target=overflow`; all in CI contract-test runs **25938975842**, **25938980466**, **25954929349** |
| Semble | `PROBE` | 0 | n/a | n/a | n/a | No runtime probe lines emitted |
| Serena | `QUERY` | 0 | n/a | n/a | n/a | No runtime telemetry |
| Serena | `FALLBACK` | 0 | n/a | n/a | n/a | No runtime telemetry |
| Serena | `PROBE` | 0 | n/a | n/a | n/a | No runtime telemetry |
| Other MCP servers observed | any | 0 | n/a | n/a | n/a | None observed |

| MCP server | Target | Query count | Bytes | Avg ms |
| --- | --- | ---: | ---: | ---: |
| Semble | `reviewer-context` | 8 | 119,397 | 460.4 |
| Semble | `overflow` | 4 | 29,560 | 494.0 |

| MCP availability | Target | probe_ok | probe_failed | probe_skipped | Notes |
| --- | --- | ---: | ---: | ---: | --- |
| Semble | `reviewer-context` | 0 | 0 | 0 | No `SEMBLE_PROBE` lines emitted |
| Semble | `overflow` | 0 | 0 | 0 | No `SEMBLE_PROBE` lines emitted |
| Serena | `all` | 0 | 0 | 0 | No runtime `SERENA_PROBE` lines emitted; sampled runs often had Serena disabled/unavailable |

| AI memory metric | Value |
| --- | --- |
| Workflow families with telemetry | `review_autofix` only |
| Observed runs | 8 |
| Deduped telemetry events | 32 |
| `record-run-event` | 16 |
| `retrieve` | 8 |
| `record-candidate` | 8 |
| Retrieve hit rate | 0 / 8 = 0% |
| Avg `estimated_tokens` | 0 |
| `keyword_method` distribution | `none` 8, `plain` 0, `llm` 0 |
| `enabled: false` retrieves | 0 |
| `fail_open: true` retrieves | 0 |
| Push retries > 1 | run **25951672388** = 3; run **25951655677** = 2 |

| GH API hotspot | Evidence | Estimated avoidable calls / run | Recommendation |
| --- | --- | ---: | --- |
| `review_autofix / codex-agent / Collect PR check-run failures` | Run **25954929371** waited **120.8s** polling the same check-run endpoint | ~2-4 paginated snapshots | Shorten timeout or snapshot once + retry once |
| `copilot_pull_request_reviewer / Prepare + Agent` | Run **25956423941** fetched PR details/files, then re-fetched diff and base SHA | >=2 PR lookups | Share PR metadata/base SHA across jobs |
| `copilot_pull_request_reviewer / Cleanup artifacts` | Run **25954929829** spent ~**155s** in cleanup; current flow lists then deletes artifacts individually | 1 list + N deletes | Make cleanup asynchronous / skip when low value |
| `review_autofix_sweep / sweep` | `.github/workflows/review_autofix_sweep.yml:107-181` snapshots active runs once and reuses locally | already optimized | Reuse this batching pattern elsewhere |

## Deep Audit — Workflows & Scripts (2026-05-16)

### Section 1: Bug & Correctness Sweep

- **BUG-001**  
  **File:** `.github/workflows/review_autofix.yml:514-566`  
  **Severity:** High  
  **Category:** `bug`  
  **Description:** When `closingIssuesReferences` comes back empty, the fallback regex at line 524 accepts bare `issues/123` and `issue #123` mentions from the PR title/body, not just closing keywords. The same block then looks up labels, dispatches standalone validate, and removes `ai:orchestrator-validate-required` at lines 536-563. That means a PR that merely mentions an unrelated issue can trigger validation and clear that issue’s label. This is inconsistent with the hardened fallback in `.github/workflows/issue_pr_status.yml:196-210`, which explicitly excludes bare prose mentions after issue #1469.  
  **Recommended fix:** Reuse the stricter `issue_pr_status.yml` fallback rule here: only accept closing-keyword references or repo-scoped issue URLs/paths, then only remove labels for issues confirmed by GraphQL or that stricter fallback.

### Section 2: GitHub API Call Redundancy Audit

- **API-001**  
  **File:** `.github/workflows/review_autofix.yml:1530-1799`  
  **Severity:** Medium  
  **Category:** `api-redundancy`  
  **Description:** `Collect PR metadata` fans out into four separate PR-context fetches: `pulls/{pr}`, `issues/{pr}/comments`, `pulls/{pr}/reviews`, and `pulls/{pr}/comments`, then rebuilds a flattened context file locally. **Current call count:** 4 logical fetches in the common path, plus extra requests when comments/reviews paginate. The repo already has a GraphQL-first consolidator in `scripts/gh_helpers.sh:735-899` (`gh_pr_with_all_comments`), and that helper is already consumed elsewhere (`scripts/review_rb_judge.sh:303-308`, `scripts/orchestrate_poll_process.sh:9677-9682`).  
  **Recommended fix:** Extend `gh_pr_with_all_comments owner repo pr_number [preloaded_meta_json]` to include review body/state/submittedAt so `review_autofix` can source the helper and build `PR_META_FILE`/comment context from one payload. **Proposed call count:** 1 helper call in the common case, with REST fallback only when the helper detects pagination/GraphQL failure. **Batching pattern to extend:** `scripts/gh_helpers.sh:735-899`.

- **BATCH-001**  
  **File:** `.github/workflows/review_autofix.yml:1616-1646`  
  **Severity:** Medium  
  **Category:** `api-batching`  
  **Description:** When linked-issue GraphQL resolution returns `[]`, the body-text fallback loops over every matched issue number and calls `gh api "repos/.../issues/${_fb_num}"` once per issue. **Current call count:** 1 GraphQL lookup + N REST issue lookups, with N capped at 20 by `_FALLBACK_MAX_ISSUES`. This is a textbook per-item REST loop on a hot path.  
  **Recommended fix:** Batch the fallback issue hydration with one alias-based GraphQL request that returns `{number,title,body}` for all fallback numbers. **Proposed call count:** 2 total in the common case, or `1 + ceil(N/25)` if implemented as a reusable batch helper. **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh:6393-6510` (`_fetch_candidate_issue_details_graphql`).

- **BATCH-002**  
  **File:** `.github/workflows/review_autofix.yml:514-566`  
  **Severity:** Low  
  **Category:** `api-batching`  
  **Description:** The same fallback path stores regex-matched issues as `{number, labels: null}` at line 531, then calls `gh issue view ... --json labels` once per issue at lines 539-546 before deciding whether to dispatch validate/remove labels. **Current call count:** 2 fixed lookups (`closingIssuesReferences` + PR title/body fetch) + N per-issue label fetches on fallback.  
  **Recommended fix:** Once fallback numbers are known, batch-fetch `number + labels` into `issue_nodes_json` instead of leaving `labels: null`. **Proposed call count:** 3 total instead of `2 + N`. **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh:6393-6510` (`_fetch_candidate_issue_details_graphql`).

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**  
  **File:** `.github/workflows/validate.yml:201-577`; `.github/workflows/implement.yml:434-623`; `.github/workflows/review_autofix.yml:907-1153`  
  **Severity:** Medium  
  **Category:** `duplication`  
  **Description:** Large “stage/fetch workflow support files” blocks are duplicated across eight workflows. The same copy/chmod/fallback/schema/prompt staging pattern also appears in `.github/workflows/clarify.yml:214-286`, `.github/workflows/plan.yml:245-316`, `.github/workflows/orchestrate.yml:335-421`, `.github/workflows/orchestrate_poll.yml:282-392`, and `.github/workflows/orchestrate_clarify_respond.yml:257-348`. This is now both a maintenance hotspot and a direct contributor to the expression-size risk in `validate.yml`.  
  **Recommended fix:** Move the staging logic into a new shared module such as `scripts/stage_workflow_support.sh` with an entrypoint like `stage_workflow_support <mode> <script_ref> <wf_source> <manifest_path>`. Shared helpers should own `checkout_support_ref` and `copy_from_ref_or_local`. Update callers in clarify, plan, orchestrate, orchestrate_poll, orchestrate_clarify_respond, review_autofix, validate, and implement.

- **CONSIST-001**  
  **File:** `scripts/gh_helpers.sh:391-568`; `.github/workflows/review_autofix.yml:1448-1486`  
  **Severity:** Medium  
  **Category:** `consistency`  
  **Description:** The repo already ships centralized GitHub API retry helpers with permanent-failure detection and file/JSON variants in `scripts/gh_helpers.sh`, but multiple workflows still carry bespoke retry wrappers: `review_autofix.yml:1448-1486`, `test-and-mark-stable.yml:468-482,593-605,786-798,1233-1255,1728-1751,2387-2398,4642-4667`, `cancel_on_pr_close.yml:26-52`, `mark-stable.yml:326-352,475-501`, `orchestrate_poll.yml:79-112`, and `comprehensive-test-and-release.yml:72-98,315-341`. These copies drift semantically: they do not use `_is_gh_permanent_failure`, so they can burn retry budget on deterministic 404/422/scope failures, and they emit inconsistent diagnostics.  
  **Recommended fix:** Standardize on `scripts/gh_helpers.sh` as the single owner of retry behavior. Existing call signatures are already sufficient: `gh_retry gh api ...`, `gh_retry_to_file <out> gh api ...`, and `gh_api_json_to_file <out> gh api ...`. For jobs that currently avoid checkout, add a shallow checkout/bootstrap step so they can source the same helper.

### Section 4: Expression Size Limit Risk Assessment

- **EXPR-001**  
  **File:** `.github/workflows/test-and-mark-stable.yml:1203-1587`  
  **Severity:** High  
  **Category:** `expression-limit`  
  **Description:** `Phase 4: Wait for review & autofix to complete` contains `${{ }}` and its deindented `run:` body is about **19,899** characters. That leaves only about **1,101** characters of headroom before GitHub’s 21,000-character template-expression ceiling.  
  **Recommended fix:** Extract the wait loop into `scripts/wait_for_review_run.sh` and pass state via env (`PR_NUMBER`, `TEST_REPO`, `BAIT_SHA`, `POLL_INTERVAL`, etc.), or split live-log shortcut logic into a separate step.

- **EXPR-002**  
  **File:** `.github/workflows/review_autofix.yml:1445-1834`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** `Collect PR metadata` deindents to about **17,408** characters, leaving about **3,592** characters of headroom. It combines inline retry helpers, PR hydration, linked-issue fallback, Python flattening, and diff capture in one interpolated block.  
  **Recommended fix:** Move the step into a helper script such as `scripts/review_collect_pr_metadata.sh`, and source `scripts/gh_helpers.sh` instead of embedding the retry wrapper.

- **EXPR-003**  
  **File:** `.github/workflows/validate.yml:204-577`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** `Fetch workflow support files` deindents to about **17,416** characters, leaving about **3,584** characters of headroom. This is already above the 15 KB warning threshold and is also duplicated across seven other workflows.  
  **Recommended fix:** Extract the block to shared `scripts/stage_workflow_support.sh` or a composite action, leaving the workflow step as a thin env wrapper.

- **EXPR-004**  
  **File:** `.github/workflows/test-and-mark-stable.yml:1673-2078`  
  **Severity:** Medium  
  **Category:** `expression-limit`  
  **Description:** `Phase 4b: Verify editor restored canary (pytest + retry)` deindents to about **17,408** characters, leaving about **3,592** characters of headroom. The inline API retry helper, fetch helpers, pytest classifier, and retry poll loop all live in one interpolated block.  
  **Recommended fix:** Move the verification/retry logic into `scripts/verify_e2e_editor_canary.sh` and keep the workflow step focused on env plumbing and outputs.

- **Note:** No workflow file exceeded the 800 KB early-warning threshold. Largest files inspected: `review_autofix.yml` **332,706 B**, `test-and-mark-stable.yml` **281,253 B**, `implement.yml` **221,263 B**.

### Section 5: Cross-Cutting Concerns

- **SHELL-001**  
  **File:** `scripts/review_commit_changes.sh:489-489`  
  **Severity:** Low  
  **Category:** `shellcheck`  
  **Description:** ShellCheck SC2086 flags the unquoted URL in `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. Quoting is safer and avoids accidental word-splitting/globbing in tests or future host/token variations.  
  **Recommended fix:** Quote the full URL argument.

- **SHELL-002**  
  **File:** `scripts/review_conflict_resolve.sh:1460-1460`  
  **Severity:** Low  
  **Category:** `shellcheck`  
  **Description:** ShellCheck SC2086 flags the same unquoted `git remote set-url` pattern here. It should be kept consistent with the fix in `review_commit_changes.sh`.  
  **Recommended fix:** Quote the full URL argument here too.

- **DEAD-001**  
  **File:** `scripts/review_run_reviewers.sh:142-147`  
  **Severity:** Low  
  **Category:** `dead-code`  
  **Description:** `probe_prompt` is declared in the local-variable list but never assigned or read. ShellCheck SC2034 already flags it.  
  **Recommended fix:** Remove `probe_prompt` from the declaration, or wire it into the cache-probe path if it was intended for future use.

- **DEBT-001**  
  **File:** `.github/workflows/orchestrate_poll.yml:7-20`; `.github/workflows/internal-orchestrate-poll.yml:16-18`  
  **Severity:** Low  
  **Category:** `tech-debt`  
  **Description:** `caller_workflow` remains in the reusable workflow interface as a documented no-op, and the internal wrapper still passes it. That preserves a dead public input surface and keeps callers reasoning about behavior that no longer exists.  
  **Recommended fix:** Stop passing `caller_workflow` from `internal-orchestrate-poll.yml`, announce a deprecation window, then remove the ignored input from `orchestrate_poll.yml`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
| --- | ---: | --- |
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 7 | API-001, BATCH-001, CONSIST-001, DUP-001, EXPR-002, EXPR-003, EXPR-004 |
| Low | 5 | BATCH-002, DEAD-001, DEBT-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
| --- | ---: | --- |
| Critical/High bug fixes | 1 | Small |
| API call optimization | 1-2 | Medium |
| Code modularization | 8-9 | Large |
| Expression size reduction | 3-4 | Medium |
| Medium/Low fixes | 5 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-16)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is proven in-place and can be consolidated without changing retries, filters, pagination, concurrency, or observable behavior. `NEEDS_VERIFICATION` means the overlap is real but one or more safety preconditions still need a human/parity check. `RISKY_SKIP` means the redundancy is visible, but it sits in a guarded/polling/race-sensitive path (or otherwise fails the auto-merge safety bar) and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — NEEDS_VERIFICATION**
  - **Files:** `.github/workflows/clarify.yml:397-402`
  - **Current call count:** 2 logical comment fetches when `SEMANTIC_CACHE_BACKEND != 'none'`
  - **Proposed call count:** 1 logical fetch on the semantic-cache-enabled success path
  - **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}/comments?sort=created&direction=asc`
  - **Evidence:**
    ```sh
    gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

    if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
      if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
        | jq -r 'add // [] | .[] | "[" + (.created_at // "") + "] @" + (.user.login // "unknown") + ":\n" + (.body // "") + "\n"' > "${THREAD_HISTORY_FILE}"; then
    ```
    The second call is a strict superset of the first call’s data when semantic cache is enabled; only the output shaping differs.
  - **Proposed fix:** In `Fetch issue comments`, fetch the paginated comment list once into a temp JSON blob when semantic cache is enabled, derive `ISSUE_COMMENTS_FILE` via `jq 'add // [] | .[:50]'`, and derive `THREAD_HISTORY_FILE` from the same merged array; keep the current single-page fetch as the fail-open fallback if the full fetch fails.
  - **Safety rationale:** The overlap is real, but the current two-call design has different pagination and fail-open semantics, so response-order and failure-parity must be checked before collapsing it.
  - **Downstream signal:** Verify on an issue with `>100` comments that `ISSUE_COMMENTS_FILE` still matches the current first-50 ordering, and simulate a full-fetch failure to confirm the step still preserves bounded prompt context plus the existing semantic-cache bypass behavior.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — SAFE_TO_MERGE**
  - **Files:** `scripts/review_rb_judge.sh:210-227`, `scripts/review_rb_judge.sh:242-245`
  - **Current call count:** 2 common-path `pulls/{pr}` fetches in the `closingIssuesReferences == empty` branch
  - **Proposed call count:** 1 common-path fetch; retain the second fetch only as fallback if the reused payload is empty/invalid
  - **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pr_number}`
  - **Evidence:**
    ```sh
    _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
    ...
    unset _pr_meta _pr_state _pr_merged
    ...
    if [ -z "${ISSUE_NUMBERS}" ]; then
      PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    fi
    ```
    The second fetch is only used to recover `title + body`, which is already present in `_pr_meta`.
  - **Proposed fix:** Delay `unset _pr_meta` until after the linked-issue fallback, compute `PR_DATA` from `_pr_meta` first, and keep the current `_safe_gh_jq` fetch only as a fallback when `_pr_meta` is empty or unparsable.
  - **Safety rationale:** Same endpoint, same script, and no response-affecting mutation occurs between the two reads; keeping the current fallback preserves the existing fail-open/error-handling behavior.
  - **Downstream signal:** Reuse `_pr_meta` for `PR_DATA` in `scripts/review_rb_judge.sh`, and fall back to the existing `_safe_gh_jq` pull fetch only when `_pr_meta` is unusable.

- **REUSE-002 — NEEDS_VERIFICATION**
  - **Files:** `.github/workflows/internal-review.yml:98-101`
  - **Current call count:** 2 logical API calls
  - **Proposed call count:** 1 logical API call
  - **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls?state=open&head={owner}:{branch}`, `GET /repos/{owner}/{repo}`
  - **Evidence:**
    ```sh
    existing_pr="$(gh api \
      "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
      --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
    base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
    ```
    `base_ref` is repo metadata that is typically already present on the push event payload as `github.event.repository.default_branch`.
  - **Proposed fix:** Replace the `/repos/${REPOSITORY}` fetch with `${{ github.event.repository.default_branch }}` (or inject it once into env before the shell step) and keep the open-PR lookup unchanged.
  - **Safety rationale:** The data appears to already exist in workflow context, but reusable-workflow push-context parity should be verified before removing the API fallback.
  - **Downstream signal:** Confirm `github.event.repository.default_branch` is always populated for the `push`-triggered `internal-review.yml` path and matches the current API response on representative branches/repos before deleting the repo lookup.

- **REUSE-003 — NEEDS_VERIFICATION**
  - **Files:** `.github/workflows/implement.yml:69-80`, `.github/workflows/implement.yml:898-907`
  - **Current call count:** 2 common-path issue fetches in one job
  - **Proposed call count:** 1 common-path issue fetch
  - **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence:**
    ```sh
    ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
    ```
    later:
    ```sh
    gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
    ```
    The job later standardizes on `ISSUE_META_FILE`, and other steps already reuse that file instead of re-fetching labels.
  - **Proposed fix:** Have `Precheck approval phase label` fetch/store the full issue JSON in a temp file (for example under `$RUNNER_TEMP`), then let `Fetch issue metadata` validate/reuse that file as `ISSUE_META_FILE`, keeping the current `gh_retry gh api` as fallback only if the temp snapshot is missing or invalid.
  - **Safety rationale:** This crosses step boundaries and changes which fetch becomes the authoritative metadata source, so retry/failure parity and cache-lifetime assumptions must be verified.
  - **Downstream signal:** Validate both `SKIP_IMPLEMENT=true` and normal implement paths to ensure the precheck snapshot survives across steps, preserves the current gating decisions, and still falls back cleanly when the cached file is absent or malformed.

### Dead Calls (DEAD-API-###)

- **DEAD-API-001 — RISKY_SKIP**
  - **Files:** `scripts/orchestrate_poll_process.sh:5231-5237`
  - **Current call count:** 1 embedded fetch site inside an otherwise unreferenced helper
  - **Proposed call count:** 0 embedded fetch sites
  - **Endpoint(s):** `GET /repos/{owner}/{repo}/issues/{issue_number}/comments?sort=created&direction=desc&per_page=100`
  - **Evidence:**
    ```sh
    read_standalone_state_json() {
      local issue_num="$1"
      local comments_json
      if ! comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null)"; then
        comments_json='[]'
      fi
      _extract_standalone_state_json_from_comments "${comments_json}"
    }
    ```
    Repo-wide symbol search finds no in-repo call sites for `read_standalone_state_json`; only the definition exists.
  - **Proposed fix:** Remove `read_standalone_state_json`, or replace it with a documented wrapper around `_extract_standalone_state_json_from_comments` only if an external consumer is confirmed to source this script.
  - **Safety rationale:** Even though the helper appears dead, it lives inside `scripts/orchestrate_poll_process.sh`, which this audit contract treats as a manual-review-only path for API removals.
  - **Downstream signal:** Do not auto-delete this helper; first confirm no external shell sourcing, test harness, or operational runbook depends on `read_standalone_state_json`, then remove it manually if truly unused.

### Cross-References to Deep Audit Section

- API-001: NEEDS_VERIFICATION — directionally correct, but `gh_pr_with_all_comments` must be field-for-field/pagination-parity checked before replacing `review_autofix`’s current REST hydration.
- BATCH-001: NEEDS_VERIFICATION — batching the fallback issue hydration is correct in principle, but the replacement must preserve `_FALLBACK_MAX_ISSUES`, fail-open behavior, and existing warning logs.
- BATCH-002: NEEDS_VERIFICATION — label batching is a good fit, but the fallback path still feeds live label-removal side effects and needs parity verification before swapping out the per-issue lookups.

### Summary Counts

| Tag | Count | IDs |
| --- | ---: | --- |
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 1 | DEAD-API-001 |

### Implement-Stage Handoff

- REUSE-001
