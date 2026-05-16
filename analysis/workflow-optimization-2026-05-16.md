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
