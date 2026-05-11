## Executive Summary

- **`review_autofix` is the dominant speed/cost hotspot.** In `shubhodeep1/coding-workflows`, `review_autofix` ran **121** times with **p50 1319s** and **p95 4886s**; its **21 failures** consumed **26.85h** of runtime, and **16 canceled runs lasting >=300s** consumed another **3.64h**. Sample failures `25640363259`, `25642391680`, `25646458592`, `25648463010`, and `25648623581` all failed in `review / codex-agent` → `Run Codex resolver, validate, stage, commit` after very large consolidator/editor contexts and the same empty-editor signature. **Estimated impact:** very high. **Confidence:** high.

- **Check-run polling is adding pure wait on broken or non-actionable paths.** Runs `25640363259`, `25646458592`, and `25648623581` all logged `CHECK_RUNS_AUTOFIX head SHA missing from PR payload` and then sat until `CHECK_RUNS_WAIT_TIMEOUT` at **1200s**; `workflow_log_analysis` run `25646148103` observed **13 poll iterations** in about **4m07s** for one sampled review run, with config allowing up to **60 polls/run**. **Estimated impact:** high. **Confidence:** high.

- **CI reliability is being dragged down by one Semble contract drift, not generalized flakiness.** `ci` had **49 runs**, **19 failures** (**38.8%**). Of those, **18** failed in `lint` → `Review Semble contract test`, and **1** failed in `lint` → `Judge Semble prefetch contract test`. Runs `25649168089`, `25647671798`, and `25646358426` all failed on the same assertion expecting the default `SEMBLE_ENABLED` contract to be `false`. **Estimated impact:** high. **Confidence:** high.

- **Semble currently looks like startup overhead without measured production benefit.** Recent `review_autofix` run `25655637517` and `orchestrate_poll` run `25656704251` both show `SEMBLE_ENABLED=true` while `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`; the index step immediately logged that it was skipping the build. In sampled production deep dives, I found **no structured production `SEMBLE_QUERY target=... bytes=...` telemetry** outside the analysis workflow, so prompt-reduction benefit is unmeasured. **Estimated impact:** medium. **Confidence:** high.

- **AI memory and prompt-cache instrumentation are emitting, but not helping yet.** I found **60** `AI_MEMORY_TELEMETRY` events, but all **15** `retrieve` operations returned **0** records, with `keyword_method=none` and average `estimated_tokens=0.0`. Separately, all locally observed `review_autofix_cache_probe` lines reported `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` as `na`. **Estimated impact:** medium. **Confidence:** high.

## Speed Optimizations

### 1. [Critical path] Create a lighter policy for comment-only Claude-branch reviews

- **Evidence:** Recent `review_autofix` run summaries show that `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` is still expensive even when no edit path runs:
  - `25650970646` succeeded in **1864s** on a **comment-only** path.
  - `25653540251` succeeded in **1417s** on a **comment-only** path.
  - `25655902814` was **canceled after 1235s**; its summary says `review / codex-agent (claude-branch-review)` dominated the run while `editor/commit/judge/auto-merge` were skipped.
  - Local log `25655902814`, job `review / codex-agent (claude-branch-review)`, shows **6 reviewer models**, `ENABLE_REVIEWER_TWO_PASS=true`, `REVIEWER_REASONING_EFFORT=xhigh`, `EDITOR_REASONING_EFFORT=xhigh`, `XPOLL_SUMMARISER_MODEL=openai/gpt-5.4-mini`, `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`, and `CHECK_RUNS_POLL_INTERVAL_SECS=20`.

- **Root cause:** The workflow is reusing a heavyweight reviewer/summarizer policy for paths that only need comments, not edits or commits.

- **Exact change:** Add a dedicated **comment-only review profile** for `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` / `editor/commit/judge/auto-merge skipped` paths:
  - reduce reviewers from 6 to 2-3,
  - disable reviewer pass 2 unless diff size or disagreement crosses a threshold,
  - lower reviewer reasoning from `xhigh` to `medium` or `high`,
  - cap summarizer evidence volume for comment-only runs.

- **Estimated time savings:** **8-15 minutes** on long comment-only runs like `25650970646`, `25653540251`, and `25655902814`.

- **Implementation risk:** **Medium.** Fewer reviewer perspectives may reduce edge-case recall. Keep the full 6-model/two-pass path for workflow files, conflict-resolution runs, or larger diffs.

### 2. [Critical path] Short-circuit check-run polling when the workflow cannot act on the result

- **Evidence:**
  - `review_autofix` failures `25640363259`, `25646458592`, `25648463010`, and `25648623581` all logged `CHECK_RUNS_AUTOFIX head SHA missing from PR payload.`
  - `25640363259` then logged `CHECK_RUNS_WAIT_TIMEOUT reached after 1200s with 1 check-run(s) still queued/in_progress`.
  - `25646458592`, `25648463010`, and `25648623581` each timed out after **1200s** with **2** checks still in progress.
  - `workflow_log_analysis` deep-audit run `25646148103` reported a sampled `review_autofix` run spending **02:07:07 to 02:11:14** in `Collect PR check-run failures (CI/lint autofix context)` with **13 observed poll iterations**, while config allowed up to **60 polls/run**.

- **Root cause:** The workflow polls Actions state even when the head SHA is unavailable, the path is comment-only, the run is already superseded/canceled, or the result is not needed for the next decision.

- **Exact change:**
  1. If PR payload lacks head SHA, **skip the check-run wait path immediately** and mark the state as `head_sha_missing`.
  2. Disable `CHECK_RUNS_AUTOFIX` on comment-only/no-edit paths.
  3. Stop polling as soon as the run is canceled or a newer review run exists for the same PR/head ref.
  4. Restrict polling to actionable contexts only.

- **Estimated time savings:** **2-20 minutes per affected run**, plus fewer repeated Actions API reads.

- **Implementation risk:** **Low.** This is a guardrail around obviously non-actionable states, not a behavior change on healthy paths.

### 3. [Critical path] Self-abort superseded or canceled `review_autofix` runs before second-pass review/summarization

- **Evidence:**
  - `review_autofix` had **44 canceled runs**.
  - **19** lasted **>=60s**, **16** lasted **>=300s**, **7** lasted **>=600s**, and **6** lasted **>=1000s**.
  - Canceled runs `>=300s` burned **3.64h** total.
  - Longest examples: `25645576688` (**2184s**), `25643308631` (**1454s**), `25643309591` (**1431s**), `25646357179` (**1343s**), `25646358487` (**1319s**), `25655902814` (**1235s**).
  - Recent canceled run `25655902814` spent essentially the whole run inside `review / codex-agent (claude-branch-review)` before cancellation.

- **Root cause:** Superseded review runs continue burning reviewer/summarizer time after a newer event has already made them irrelevant.

- **Exact change:** Add a **latest-run guard** at the boundaries before:
  - reviewer pass 2,
  - summarizer/consolidator,
  - editor/commit stage.
  
  If the run is no longer the newest for that PR/head ref, or if it has been canceled/superseded, exit cleanly with a neutral/superseded outcome.

- **Estimated time savings:** Recovers much of the **3.64h** currently lost in long canceled runs, with **10-30 minutes** saved on the worst individual cases.

- **Implementation risk:** **Low.** This is a classic supersession check and is backward-compatible.

### 4. [Micro-optimization] Stop bootstrapping Semble on the default-unavailable path

- **Evidence:**
  - `review_autofix` run `25655637517` logged `SEMBLE_ENABLED: true`, `SEMBLE_AVAILABLE: false`, `SEMBLE_INDEX_AVAILABLE: false`, then `Semble unavailable; skipping index build.`
  - `orchestrate_poll` run `25656704251` repeatedly logged `SEMBLE_ENABLED: true`, `SEMBLE_AVAILABLE: false`, `SEMBLE_INDEX_AVAILABLE: false`.
  - `workflow_log_analysis` deep-audit `25646148103` reported a sampled production run (`25646512616`) spending about **13s** in `Setup uv for Semble` + `Install semble` before still reporting unavailability.

- **Root cause:** Setup/install/index logic still runs even when Semble is not available and no production usage telemetry proves value.

- **Exact change:** Default `SEMBLE_ENABLED` back to `false`, and guard setup/install/index behind explicit enablement plus availability.

- **Estimated time savings:** About **10-15s per affected run**.

- **Implementation risk:** **Low.** Current sampled production behavior is already falling back.

### 5. [Micro-optimization / queue hygiene] Stop dispatching command workflows that immediately skip

- **Evidence:**
  - `clarify`: **193 total**, **183 skipped**
  - `plan`: **179 total**, **171 skipped**
  - `implement`: **179 total**, **167 skipped**
  - `orchestrate_clarify_respond`: **179 total**, **177 skipped**
  - Recent runs `25654368271` (`clarify`), `25654368247` (`plan`), and `25654368263` (`implement`) were all created at **2026-05-11T06:36:32Z** and ended in **1-2s** after `*.if` evaluated false.

- **Root cause:** Eligibility/prefix filtering happens after workflow launch rather than before dispatch.

- **Exact change:** Move command-prefix and eligibility checks into the parent dispatcher so these workflows are not launched unless the downstream `if` will evaluate true.

- **Estimated time savings:** Only **seconds per run**, but it reduces queue churn and log noise across hundreds of launches.

- **Implementation risk:** **Low.**

## Cost Optimizations

> **Important telemetry gap:** numeric prompt/completion/total token counts were **not** emitted in the sampled deep dives. All token/dollar estimates below are therefore based on observed durations and logged **prompt bytes**, and are labeled accordingly where they are inferential.

### 1. Cut model spend on comment-only review paths

- **Evidence:** Comment-only `review_autofix` runs `25650970646` (**1864s**), `25653540251` (**1417s**), and `25655902814` (**1235s**, canceled) still ran the heavyweight reviewer path. Local `25655902814` logs show 6 reviewer models, two-pass review, and `xhigh` reasoning.

- **Root cause:** The same high-cost reviewer policy is used whether the workflow is going to edit code or only post review comments.

- **Exact change:** Introduce a lower-cost **comment-only policy**:
  - 2-3 reviewers instead of 6,
  - no pass 2 by default,
  - lower reviewer reasoning effort,
  - lower summarizer input limits.

- **Estimated savings:** **Inference:** roughly **35-55%** model spend on comment-only `review_autofix` runs.

- **Quality-risk notes:** **Medium.** Use escalation rules:
  - keep full panel for workflow files, merge-conflict branches, or large diffs,
  - rerun full panel only when reviewer disagreement crosses a threshold.

### 2. Reduce prompt/context expansion before consolidator and editor

- **Evidence:** Five failed `review_autofix` runs logged extremely large byte counts in `review / codex-agent`:
  - `25640363259`: pass1 **27,373B**, review **31,426B**, consolidator **120,206B**, editor **323,972B**
  - `25642391680`: **36,597B**, **36,491B**, **126,568B**, **405,174B**
  - `25646458592`: **21,831B**, **35,617B**, **125,700B**, **410,199B**
  - `25648463010`: **25,951B**, **29,103B**, **119,185B**, **410,039B**
  - `25648623581`: **28,636B**, **44,958B**, **135,040B**, **427,230B**
  
  Across these five runs, averages were about **28,078B** (pass1), **35,519B** (review), **125,340B** (consolidator), and **395,323B** (editor).

- **Root cause:** Large raw reviewer outputs and diff context are flowing downstream with limited deduplication or structured compression.

- **Exact change:**
  - dedupe overlapping reviewer findings before consolidator,
  - lower `XPOLL_SUMMARISER_LINES_PER_REVIEWER` from the current `160` on comment-only/medium-risk paths,
  - pass structured findings manifests instead of broad raw text where possible,
  - feed the editor only touched files plus high-confidence findings, not the entire reviewer surface area.

- **Estimated savings:** **Inference:** **20-35%** token/dollar reduction on large `review_autofix` runs.

- **Quality-risk notes:** **Low-medium.** Keep the full-context fallback for reviewer disagreement, workflow-file changes, or unresolved CI context.

### 3. Stop paying for superseded review runs

- **Evidence:** Long canceled `review_autofix` runs were materially expensive:
  - **44** canceled total,
  - **16** canceled runs at **>=300s**,
  - **3.64h** burned in those long cancellations alone,
  - examples up to **2184s** (`25645576688`) and **1235s** (`25655902814`).

- **Root cause:** The workflow continues into later review phases after a newer event has already made the run obsolete.

- **Exact change:** Add supersession checks before pass 2, summarizer, and editor, and stop immediately when a newer run exists for the same PR/head ref.

- **Estimated savings:** High relative spend reduction on canceled review runs; exact token/dollar total is unavailable because numeric token counters are missing.

- **Quality-risk notes:** **Low.** Only obsolete runs exit early.

### 4. Default Semble off until production telemetry proves it reduces prompt expansion

- **Evidence:**
  - Sampled production deep dives outside `workflow_log_analysis` contained **no structured `SEMBLE_QUERY target=... bytes=...` lines**, so no prompt-reduction benefit is measurable.
  - Recent production runs still show `SEMBLE_ENABLED=true` while unavailable.
  - In CI deep dives, the Semble-specific lines were almost entirely `SEMBLE_FALLBACK target=overflow ... ms=0` contract-test behavior, not production usage.

- **Root cause:** A feature that is supposed to be opt-in is being bootstrapped as if it were ready by default.

- **Exact change:** Revert the default to `false`; emit structured `SEMBLE_QUERY` telemetry only when Semble is actually used, and emit an explicit skipped/unavailable marker otherwise.

- **Estimated savings:** Small per run, but high confidence and very low risk.

- **Quality-risk notes:** **Low.** Current sampled production behavior already falls back.

### 5. Fix prompt-cache observability before trying to optimize cache hit rates

- **Evidence:**
  - Locally available production deep dives show **16 unique `review_autofix_cache_probe` lines across 8 runs**.
  - The repo’s own `workflow_log_analysis` deep-audit run `25646148103` reports **18 cache-probe lines across 9 sampled runs**.
  - In both sets, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` were all `na`.
  - I found **0 numeric prompt/completion/total token lines** anywhere in the deep-dive tree.

- **Root cause:** Cache probes are present, but the extraction/plumbing is not producing usable counters.

- **Exact change:** Either:
  1. fix the probe extraction so counters are numeric and labeled by phase/model, or
  2. disable the probes until they become trustworthy.
  
  Then normalize stable prompt prefixes for comment-only versus edit paths.

- **Estimated savings:** Unquantifiable today, because cache hit/miss data is unusable.

- **Quality-risk notes:** **None** for telemetry repair; **low** for prompt-prefix normalization if semantics are preserved.

## Reliability Improvements

### 1. Align the Semble default with the tested contract

- **Failure evidence:** `ci` had **19 failures / 49 runs**. **18** failed in `lint` → `Review Semble contract test`, and **1** failed in `lint` → `Judge Semble prefetch contract test`. Failed runs `25649168089`, `25647671798`, and `25646358426` all show the same assertion:
  - expected `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}`
  - got a workflow state inconsistent with that contract.

- **Root cause category:** **Configuration drift / contract mismatch**

- **Exact fix:** Set the workflow default for Semble back to `false`, keep explicit override support, and keep the overflow fallback tests intact.

- **Expected reliability impact:** This should remove the dominant CI failure signature in the current window and materially improve the current **38.8% CI failure rate**.

- **Rollback / fail-open considerations:** Re-enable Semble explicitly via repo/workflow vars if a targeted branch needs it.

- **`SEMBLE_FALLBACK` note:** In **9** deep-dive CI error runs, I counted **45** `SEMBLE_FALLBACK target=overflow` lines:
  - **5 per run**
  - **4× `src/big.py`**
  - **1× `src/small.py`**
  - all with **`ms=0`**
  
  This is **healthy fail-open contract-test behavior**, not evidence of a production outage. The smallest safe mitigation is **not** to remove fallback logic; it is to stop default-enabling an unavailable feature in production workflows.

### 2. Add typed handling for the empty-editor / no-clean-entrypoint failure signature

- **Failure evidence:** Every `review_autofix` failure in this window failed at the same step: `review / codex-agent` → `Run Codex resolver, validate, stage, commit` (**21 failures total**). In the five deep-dive failures inspected in detail (`25640363259`, `25642391680`, `25646458592`, `25648463010`, `25648623581`), the workflow logged:
  - consolidator wall time **300s** with `failopen=1`,
  - editor prompts **323,972B - 427,230B**,
  - `The editor stage completed without a structured summary and without committing any file changes.`,
  - sometimes `No clean resolver entry-point available`,
  - then `Process completed with exit code 2.`

- **Root cause category:** **Transient editor/bootstrap/resolver handoff failure**  
  **Inference:** because the same signature appears across unrelated runs after very large prompts, this looks more like a tool/bootstrap failure mode than a deterministic PR-content problem.

- **Exact fix:**
  1. Detect the exact signature `{no structured summary + no file changes}`.
  2. Perform **one** controlled bootstrap retry for that signature only.
  3. If it repeats, emit a typed terminal status such as `editor_no_summary` and stop before resolver/check-run wait logic.
  4. If `No clean resolver entry-point available` is present, skip the resolver stage entirely and fail fast with artifacts.

- **Expected reliability impact:** Medium-high. It directly targets the signature shared by the sampled `review_autofix` failures and should reduce both failure tail length and rerun churn.

- **Rollback / fail-open considerations:** Keep the current path behind a feature flag/repo var for quick rollback; preserve artifact uploads and summary comments so operators still get diagnostic evidence.

### 3. Standardize GitHub API retry semantics and stop silent default-branch fallbacks

- **Failure evidence:** `workflow_log_analysis` run `25646148103` identified that `resolve_integration_ref.sh` uses bare `gh api` calls and that callers can fall back to the default branch when a non-404 API error occurs. The same deep-audit also noted inconsistent retry wrappers across workflows, where some helpers retry only `rate limit` while others also handle `secondary rate`, `HTTP 429`, and abuse-detection patterns. Recent `issue_pr_status` run summary `25656767372` also recorded a warning: support checkout ref unavailable, falling back to `main`.

- **Root cause category:** **Inconsistent API error handling / fail-open ref resolution**

- **Exact fix:** Route integration-ref resolution and workflow watchers through the repo’s canonical helpers in `scripts/gh_helpers.sh` (`gh_retry`, `_safe_gh_jq`, `gh_api_json_to_file`), and distinguish:
  - `branch_missing`
  - `api_error`
  
  instead of collapsing both into a silent fallback to the default branch.

- **Expected reliability impact:** Medium. This is lower-frequency than the Semble/empty-editor issues, but it is a higher-consequence correctness risk for orchestrator-managed flows.

- **Rollback / fail-open considerations:** Preserve the current `404 => branch missing` behavior; only harden non-404 API failures.

### 4. Clear the Node 20 action deprecation before the runner cutoff changes behavior

- **Failure evidence:**
  - `orchestrate_poll` run `25656704251` logged a hosted-runner warning that `astral-sh/setup-uv@v3` is still Node 20-based and that Node 24 becomes the default on **June 2, 2026**.
  - Recent `review_autofix` run `25655902814` ended with the same warning.
  - `test_and_mark_stable` run `25646134059` shows `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` in parts of the workflow, which suggests partial mitigation but not consistent coverage.

- **Root cause category:** **Action runtime deprecation**

- **Exact fix:** Upgrade to Node 24-compatible action versions where available, or apply `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` consistently in workflows that still depend on Node 20-based JavaScript actions.

- **Expected reliability impact:** Medium now, high after GitHub’s runner-side cutoff dates.

- **Rollback / fail-open considerations:** Easy rollback if an upgraded action regresses; the safer immediate step is consistent opt-in to Node 24 before the platform forces it.

## AI Memory Health

- **Telemetry emission exists.** I found **60** `AI_MEMORY_TELEMETRY` events across deep-dive logs:
  - `record-run-event`: **31**
  - `retrieve`: **15**
  - `record-candidate`: **9**
  - `summarize_unselected_runs`: **5**

- **Retrieval effectiveness is currently zero.**
  - `retrieve` operations: **15**
  - `records_selected > 0`: **0**
  - **Hit rate:** **0%**
  - `records_selected = 0`: **15**
  - average `estimated_tokens`: **0.0**
  - `keyword_method` distribution: **`none` in 15/15**
  - sample retrieve: `review_autofix` run `25646458592` emitted `enabled=true`, `records_selected=0`, `keyword_method=none`, `role=reviewer`

- **Budget comparison gap:** the sampled `retrieve` telemetry did **not** include a retrieval budget field, so I can report `estimated_tokens=0.0` but **cannot** compare it to budget.

- **Flags:**
  - `fail_open: true` entries: **0**
  - `enabled: false` entries: **0**
  - zero-record retrieves: **15/15**

- **Write-side health is mostly good.**
  - `record-run-event` push attempts: **31/31** completed with `push_attempts=1`
  - `record-candidate` push attempts: **8/9** at `1`, **1/9** at `2`  
    The only observed retry was in run `25642396253`.
  - I did **not** observe high push-retry pressure.

- **What this means:** memory emission is working, but retrieval query generation is effectively non-functional in this sample. Because every retrieve used `keyword_method=none` and selected zero records, the memory system is not currently shrinking prompts or improving decisions.

- **Recommended next step:** make retrieval choose a real keyword path (`plain` or `llm`) instead of `none`, and log a budget field plus a stable query hash. Until retrieve hit rate is above zero, memory should be treated as **observability-only**, not as an optimization layer.

## GH API Call Audit

> I did **not** see sampled `429` / secondary-rate-limit incidents in the recent `review_autofix`, `ci`, or `copilot_pull_request_reviewer` deep dives I inspected. The problem is **redundancy and burstiness**, not a currently failing rate-limit path.

| Workflow / job / step | Evidence | Redundant pattern | Recommended change | Estimated reduction / rate-limit benefit |
|---|---|---|---|---|
| `review_autofix` / `review / codex-agent` / check-run collection | Runs `25640363259`, `25646458592`, `25648463010`, `25648623581`; deep-audit `25646148103` saw **13 polls** in ~4m07s and config allowing **60 polls/run** | Repeated Actions status polling even when head SHA is missing or the path is non-actionable | Skip polling when head SHA is missing, on comment-only paths, and on superseded/canceled runs; cache one poll result per tick | **13-60 Actions reads** avoided per affected run; **medium** rate-limit risk reduction |
| `issue_pr_status` / `sync-status / sync-issue-status` / `Update linked issue labels when PR closes` + `Send PR merged Telegram alert` | Recent run `25656767372` uses GraphQL `closingIssuesReferences(first: 50)`, batched `ORCH_RESP`, then per-issue REST fetches such as `repos/${REPOSITORY}/issues/${issue_number}` | GraphQL batch work is followed by per-issue REST reads in loops | Extend the batched GraphQL payload to include the fields later steps need, and reuse `ORCH_RESP`; follow the repo’s `_fetch_candidate_issue_details_graphql` / `_fetch_issue_labels_batch_graphql` pattern | **O(N) → O(1-2)** issue lookups per run; **medium** rate-limit benefit |
| `review_autofix` / `review / post-merge-validate-dispatch` | Recent run `25656767378` queries `closingIssuesReferences(first: 50)` then falls back to `gh issue view` per issue before dispatch/edit | One GraphQL fetch, then per-issue label reads in a loop | Expand the initial query or batch-fetch missing issue labels once, then dispatch/edit only the qualifying issues | Avoids up to **N** issue reads per run; **medium** benefit on bursty PRs |
| `cancel_on_pr_close` / `cancel / cancel-active-runs` | Recent run `25656767344` unconditionally calls `gh api -i /rate_limit`, then two `_gh_retry gh api` list calls before concluding there are no matching runs | Unconditional rate-limit probe and duplicate list reads on a very short code path | Move `/rate_limit` probing into the retry/error path only; short-circuit earlier when there are no candidate runs | Saves **1-3 API calls** per run; **low** but safe benefit |
| `copilot_pull_request_reviewer` / `Cleanup artifacts` | Recent run `25655641046` lists `/actions/runs/25655641046/artifacts` then loops over `/actions/artifacts/$artifact_id` deletes | Enumeration + delete loop even when artifacts are few or predictable | Skip cleanup when no upload step ran, or reuse artifact IDs from the upload phase | Saves **1 GET + N DELETEs** on no-artifact/known-artifact paths; **low** benefit |
| `test_and_mark_stable` / `workflow-log-analysis-test` watcher steps | Failed run `25646134059` repeatedly hits `/actions/workflows/.../runs`, `/actions/runs/$RID`, `/jobs?per_page=10`, and `/actions/jobs/$JOB_ID/logs` in dispatch/watch loops | Same watch pattern is reimplemented several times and re-fetches the same state within a polling tick | Extract a shared watch helper, cache fetched JSON within each tick, and route calls through canonical retry helpers | Likely **dozens** of calls removed over 2h+ harness runs; **medium** rate-limit benefit |

**Repo-specific API hygiene to reuse:** the deep-audit already points to the right local patterns: `_fetch_issue_labels_batch_graphql`, `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and the canonical wrappers in `scripts/gh_helpers.sh`.

## Prompt Cache & Memory System

- **Prompt-cache probes exist but are operationally useless right now.**
  - Locally available production deep dives contain **16 unique `review_autofix_cache_probe` lines across 8 runs**.
  - The repo’s own `workflow_log_analysis` deep-audit run `25646148103` reports **18 lines across 9 sampled production runs**.
  - In both cases, all counters were `na`:
    - `prompt_tokens`
    - `completion_tokens`
    - `total_tokens`
    - `cache_creation_input_tokens`
    - `cache_read_input_tokens`
  - I found **0 numeric token-counter lines** anywhere in the deep-dive tree.

- **So current cache behavior cannot be measured.** There is no trustworthy hit/miss, read/create, or token-saved signal to optimize against.

- **Memory retrieval is also not reducing prompt size yet.** Because AI memory retrieves were **0/15 hits**, the memory layer is currently acting like a no-op from a prompt-shrinking perspective.

- **Inference: cache fragmentation is probably high.** The sampled `review_autofix` prompts vary materially:
  - summarizer pass1: **21.8KB - 36.6KB**
  - summarizer review: **29.1KB - 45.0KB**
  - consolidator input: **119KB - 135KB**
  - editor prompt: **324KB - 427KB**
  
  With six reviewer outputs, two-pass logic, per-PR diffs, run-specific metadata, and check-run state mixed into the prompt, stable cache prefixes are likely being polluted by dynamic noise.

- **Concrete improvements:**
  1. **Repair cache telemetry first.** Emit numeric prompt/completion/total and cache read/create counters by phase (`reviewer`, `summarizer`, `editor`).
  2. **Stabilize prompt prefixes.** Put invariant instructions first; move volatile data such as head SHA, run IDs, timestamps, and live Actions state to the tail or external files.
  3. **Split prompt families.** Comment-only review prompts and edit prompts should not share the same template/prefix.
  4. **Emit structured Semble telemetry only on real use.** Right now Semble adds ambiguity because availability/skip logs exist, but production query-byte telemetry does not.

- **Expected impact:**
  - **Tokens/latency:** currently unquantifiable until counters are numeric.
  - **Reliability/observability:** high immediate value, because teams can finally tell whether cache changes helped or hurt.

## Orchestrator Health

- **Core orchestration is mostly stable.**
  - `orchestrate_poll`: **22/22 success**, avg **137.4s**, p95 **198.6s**
  - `issue_pr_status`: **13/13 success**
  - `cancel_on_pr_close`: **13/13 success**
  
  So the orchestrator is not failing broadly.

- **The biggest orchestration pain point is launch noise, not clarify-loop failure.**
  - `clarify`: **183/193 skipped** (**94.8%**)
  - `plan`: **171/179 skipped** (**95.5%**)
  - `implement`: **167/179 skipped** (**93.3%**)
  - `orchestrate_clarify_respond`: **177/179 skipped** (**98.9%**)
  
  Recent runs `25654368271`, `25654368247`, and `25654368263` all launched and then immediately skipped on `if` evaluation.

- **No recurring wave-progression or conflict-heal storm was obvious in this window.** The more concrete operational issue is **fail-open ref drift**:
  - recent `issue_pr_status` run summary `25656767372` recorded `Support checkout ref ${script_ref} is unavailable; using main.`
  - `workflow_log_analysis` run `25646148103` identified default-branch fallback risk in integration-ref resolution on API errors

- **Smallest safe mitigations:**
  1. Pre-filter command workflows before dispatch.
  2. Standardize ref resolution on the canonical GH retry helpers.
  3. Track check-run timeout signatures as first-class operational alerts.

- **Observable indicators to track:**

| Indicator | Current signal | Why it matters |
|---|---|---|
| `CHECK_RUNS_WAIT_TIMEOUT` count | Present in at least **6** `review_autofix` deep-dive runs | Best leading indicator of pure wait waste |
| `Support checkout ref ... unavailable; using main` warnings | Seen in recent run summary `25656767372` | Indicates support-script drift from triggering ref |
| Launched-but-skipped ratio for command families | **93-99%** skipped in `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` | Measures queue noise and unnecessary workflow churn |
| `review_autofix` empty-editor signature count | Present in all 5 sampled failing deep dives | Tracks the dominant current failure mode |
| AI memory retrieve hit rate | **0%** in sampled window | Shows whether memory is providing any value |

## Pipeline Flow Bottlenecks

### End-to-end bottleneck map

| Stage | Bottleneck type | Evidence | Recommendation order |
|---|---|---|---|
| **Clarify / Plan / Implement** | Queue noise, not compute | Most runs are skipped in **1-2s** (`25654368271`, `25654368247`, `25654368263`) | 5th: pre-dispatch gating |
| **Review / Autofix** | **Primary compute bottleneck** | `review_autofix` p50 **1319s**, p95 **4886s**; recent comment-only runs `25650970646` (**1864s**) and `25653540251` (**1417s**) still used heavyweight review paths | **1st:** lighter comment-only policy |
| **Review / Autofix** | **Primary retry/poll bottleneck** | `CHECK_RUNS_WAIT_TIMEOUT` at **1200s** in `25640363259`, `25646458592`, `25648463010`, `25648623581`; deep-audit `25646148103` saw **13 polls** in ~4m07s | **2nd:** short-circuit polling |
| **Review / Autofix** | Failure-tail overhead | Empty-editor/no-change signature after large prompts in `25640363259`, `25642391680`, `25646458592`, `25648463010`, `25648623581` | **3rd:** typed fail-fast / one guarded retry |
| **Orchestrate / Validate harnesses** | Watch-loop overhead | `workflow_log_analysis` avg **8644.5s** across 2 runs; `test_and_mark_stable` avg **7301s** across 2 runs, with repeated dispatch/watch API loops in `25646134059` | 4th: shared watcher + JSON reuse |
| **CI** | Lint runtime plus avoidable failure | `ci` p50 **645s** and **19** failures; most failures are one Semble contract assertion, not infra | 2nd reliability fix: restore Semble default contract |
| **Merge / conflict handling** | Secondary overhead | `forward_merge_stable_to_main` run `25656766995` opened a fallback PR for conflict in **26s**; merge/conflict handling exists but is not the repo-wide dominant latency source | Lower priority than review/polling fixes |

### Ordered fix list by end-to-end impact

1. **Reduce `review_autofix` model cost on comment-only paths.**
2. **Stop 1200s check-run waits on missing-head/non-actionable paths.**
3. **Fail fast on the empty-editor/no-clean-entrypoint signature.**
4. **Unify long watcher loops in `workflow_log_analysis` / `test_and_mark_stable`.**
5. **Remove launch noise from skipped command workflows.**

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  1. `review_autofix` AI compute and polling tail
  2. CI lint duration plus Semble contract failures
  3. Low-volume but very long workflow-watch harnesses (`workflow_log_analysis`, `test_and_mark_stable`)

- **Top failure modes**
  1. `review / codex-agent` → `Run Codex resolver, validate, stage, commit` exit-code-2 failures after empty editor output (**21 failures**, all `review_autofix`)
  2. `lint` → `Review Semble contract test` assertion failures (**18 CI failures**) plus one Semble prefetch contract failure
  3. Long dispatch/watch workflow failures (`workflow_log_analysis` run `25646148103`, `test_and_mark_stable` run `25646134059`)

- **Highest-cost drivers**
  1. Six-model, two-pass reviewer policy with `xhigh` reasoning on comment-only review paths
  2. 300s consolidator fail-open plus 1200s check-run waits
  3. Superseded/canceled `review_autofix` runs that continue into expensive phases

- **Top 3 prioritized actions**
  1. **Split `review_autofix` into lightweight comment-only and heavyweight edit/conflict profiles.**
  2. **Short-circuit check-run waits and add supersession checks before later review phases.**
  3. **Restore the Semble default contract to `false` and skip Semble bootstrap when unavailable.**

## Metrics Appendix

### Overall repo metrics

| Scope | Runs | Success | Failure | Cancelled | Skipped/Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 203 | 43 | 49 | 705 | 263.5 | 1.0 | 1583.8 |

### Key workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Skipped/Other | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 121 | 51 | 21 | 44 | 5 | 17.4% | 1528.8 | 1319.0 | 4886.0 |
| `ci` | 49 | 30 | 19 | 0 | 0 | 38.8% | 640.3 | 645.0 | 699.8 |
| `workflow_log_analysis` | 2 | 1 | 1 | 0 | 0 | 50.0% | 8644.5 | 8644.5 | 9403.7 |
| `test_and_mark_stable` | 2 | 0 | 1 | 1 | 0 | 50.0% | 7301.0 | 7301.0 | 7800.5 |
| `orchestrate_poll` | 22 | 22 | 0 | 0 | 0 | 0.0% | 137.4 | 111.5 | 198.5 |
| `clarify` | 193 | 10 | 0 | 0 | 183 | 0.0% | 7.2 | 1.0 | 37.8 |
| `plan` | 179 | 8 | 0 | 0 | 171 | 0.0% | 5.7 | 1.0 | 9.1 |
| `implement` | 179 | 8 | 0 | 4 | 167 | 0.0% | 12.0 | 1.0 | 23.0 |
| `orchestrate_clarify_respond` | 179 | 2 | 0 | 0 | 177 | 0.0% | 1.2 | 1.0 | 2.0 |
| `issue_pr_status` | 13 | 13 | 0 | 0 | 0 | 0.0% | 28.1 | 14.0 | 63.8 |
| `cancel_on_pr_close` | 13 | 13 | 0 | 0 | 0 | 0.0% | 7.4 | 7.0 | 9.6 |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 0 | 100.0% | 97.0 | 97.0 | 97.0 |

### `review_autofix` outcome breakdown

| Conclusion | Runs | Avg s | p50 s | p95 s | Total runtime h |
|---|---:|---:|---:|---:|---:|
| Success | 51 | 1451.7 | 1559.0 | 3352.5 | 20.57 |
| Failure | 21 | 4602.8 | 4404.0 | 7577.0 | 26.85 |
| Cancelled | 44 | 313.2 | 31.0 | 1417.8 | 3.83 |
| Skipped | 5 | 100.6 | 2.0 | 383.6 | 0.14 |

### Long canceled `review_autofix` tail

| Canceled duration threshold | Runs | Total runtime h |
|---|---:|---:|
| `>=60s` | 19 | 3.76 |
| `>=300s` | 16 | 3.64 |
| `>=600s` | 7 | 2.72 |
| `>=1000s` | 6 | 2.49 |

### Sampled prompt/context size proxies for failing `review_autofix` runs

> Numeric token totals were **not** available; these are the directly logged **byte counts**.

| Run ID | Summariser pass1 bytes | Summariser review bytes | Consolidator input bytes | Editor prompt bytes |
|---|---:|---:|---:|---:|
| `25640363259` | 27,373 | 31,426 | 120,206 | 323,972 |
| `25642391680` | 36,597 | 36,491 | 126,568 | 405,174 |
| `25646458592` | 21,831 | 35,617 | 125,700 | 410,199 |
| `25648463010` | 25,951 | 29,103 | 119,185 | 410,039 |
| `25648623581` | 28,636 | 44,958 | 135,040 | 427,230 |
| **Average** | **28,078** | **35,519** | **125,340** | **395,323** |

### Token / cache telemetry status

| Metric | Value |
|---|---|
| Numeric prompt/completion/total token lines found in deep-dive tree | **0** |
| Unique local `review_autofix_cache_probe` lines | **16** across **8** production runs |
| Repo deep-audit cache-probe count | **18** lines across **9** sampled production runs |
| `prompt_tokens` / `completion_tokens` / `total_tokens` in probes | all **`na`** |
| `cache_creation_input_tokens` / `cache_read_input_tokens` in probes | all **`na`** |

### AI memory metrics

| Metric | Value |
|---|---|
| Total `AI_MEMORY_TELEMETRY` events | **60** |
| `retrieve` ops | **15** |
| Retrieve hit rate (`records_selected > 0`) | **0 / 15 = 0%** |
| Retrieve zero-result count | **15 / 15** |
| Average `estimated_tokens` on retrieve | **0.0** |
| `keyword_method=none` on retrieve | **15 / 15** |
| `fail_open=true` on retrieve | **0** |
| `enabled=false` on retrieve | **0** |
| `record-run-event` push attempts | **31** at `1` attempt |
| `record-candidate` push attempts | **8** at `1`, **1** at `2` |

### Semble telemetry summary

| Context | Metric | Value | Notes |
|---|---|---:|---|
| Sampled production deep dives | Structured `SEMBLE_QUERY target=... bytes=...` lines | **0** | Excluding analysis-workflow prompt text |
| CI deep-dive error runs | `SEMBLE_FALLBACK` lines | **45** across **9** runs | All `target=overflow`, all `ms=0` |
| CI deep-dive error runs | Per-run fallback distribution | **5/run** | **4× `src/big.py`**, **1× `src/small.py`** |
| Production bootstrap | Unavailable examples | `25655637517`, `25656704251` | `SEMBLE_AVAILABLE=false`, `SEMBLE_INDEX_AVAILABLE=false` |
| Logged Semble query bytes in production | Availability | **Unavailable** | No structured production query-byte telemetry found |

### GH API hotspot summary

| Workflow / step | Observed API pattern | Approx. calls per observed execution | Main opportunity |
|---|---|---|---|
| `review_autofix` check-run polling | Repeated Actions polling; one sampled run had **13** observed polls, config allows **60** | **13-60** poll reads on affected runs | Skip non-actionable polling |
| `issue_pr_status` linked-issue handling | GraphQL batch + per-issue REST lookups | **2 batched calls + N per-issue calls** | Extend/reuse batched payloads |
| `review_autofix` post-merge validate dispatch | GraphQL linked-issue query + per-issue label reads + dispatch/edit loop | **1 batched call + N per-issue reads/mutations** | Batch label availability upfront |
| `cancel_on_pr_close` | `/rate_limit` probe + list queries + optional cancel POST | **1-3 base calls** before any cancel | Move rate-limit probe to retry path |
| `copilot_pull_request_reviewer` artifact cleanup | List artifacts then delete one-by-one | **1 GET + N DELETEs** | Skip or reuse artifact IDs |
| `test_and_mark_stable` watcher steps | Repeated `/actions/workflows`, `/actions/runs`, `/jobs`, `/logs` | **Dozens** over long watch loops | Shared watch helper + JSON reuse |

If you want, I can turn this report into a prioritized implementation checklist next, with exact workflow/script files to touch and a low-risk rollout order.

## Deep Audit — Workflows & Scripts (2026-05-11)

Scope reviewed: all workflow files under `.github/workflows/` and all shell/Python helpers under `scripts/`. To avoid duplicating the in-progress report already on disk, the findings below focus on additional high-confidence issues that were not already covered there.

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Standalone issue close gate is effectively bypassed for non-merged PR closes
- **ID** — `BUG-001`
- **File path(s)** — `.github/workflows/issue_pr_status.yml:253-279,353-379`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — The policy comment at lines 275-278 says standalone issues should keep the original `PR_BASE_REF == main` close gate. The actual close condition at line 369 is `PR_MERGED != true || PR_BASE_REF == main || is_managed_child == true`. That first `PR_MERGED != true` clause means **every non-merged PR close event** closes linked standalone issues, even when the PR targeted a non-`main` branch. In practice, the `gh issue close` call at lines 375-377 runs for any non-merged close, which contradicts the documented standalone-policy branch immediately above it.
- **Recommended fix** — Split the close decision into explicit branches instead of one broad `||` condition:
  1. close orchestrator-managed child issues only when `PR_MERGED == true`,
  2. close standalone issues only when `PR_BASE_REF == main`,
  3. otherwise leave the issue open and only sync the phase label.  
  Keep `FINAL_LABEL` assignment separate from the close decision so `ai:closed` / `ai:merged` labeling does not implicitly force issue closure.

#### CONSIST-001 — Linked-issue lineage finalization is fail-closed, not fail-open
- **ID** — `CONSIST-001`
- **File path(s)** — `.github/workflows/issue_pr_status.yml:41-171,388-445`; `scripts/memory_helpers.sh:216-224`
- **Severity** — Medium
- **Category tag** — `consistency`
- **Description** — `README.md:25-29` documents AI memory as fail-open: “a memory error never fails the workflow.” The PR-close workflow does the opposite in two places. First, lines 412-419 hard-fail the job when `MEMORY_HELPERS_READY` is not `1` or `scripts/memory_helpers.sh` is missing. Second, lines 435-444 call `memory_finalize_task` inside a `set -euo pipefail` loop, and `memory_finalize_task()` at lines 216-224 is a bare Python invocation with no fail-open wrapper. Any `finalize-task` error therefore aborts the workflow, even though other memory helpers in the same file are explicitly wrapped fail-open.
- **Recommended fix** — Make lineage finalization match the existing memory contract:
  - downgrade lines 412-419 to warnings plus `exit 0`,
  - wrap `memory_finalize_task` the same way `memory_record_run_event` is wrapped earlier in `scripts/memory_helpers.sh`,
  - or, at minimum, guard each `memory_finalize_task` call with `|| { echo "::warning::..."; continue; }`.  
  The existing `memory_record_run_event` fail-open pattern in `scripts/memory_helpers.sh` is the right implementation model to reuse.

#### CONSIST-002 — PR-merged Telegram fallback bypasses `ALERT_MSG_LEVEL`
- **ID** — `CONSIST-002`
- **File path(s)** — `.github/workflows/issue_pr_status.yml:447-538`; `scripts/tg_helpers.sh:103-129`
- **Severity** — Low
- **Category tag** — `consistency`
- **Description** — The merged-alert step exports `ALERT_MSG_LEVEL` at line 453 and tries to source `scripts/tg_helpers.sh`. When that succeeds, `tg_send_msg()` honors the alert threshold via `_tg_should_send` at lines 107-109 of `scripts/tg_helpers.sh`. But if the helper is unavailable, the fallback branch at lines 534-537 sends the message with raw `curl` and **does not check `ALERT_MSG_LEVEL` at all**. That means a DEBUG-level merged alert can still be emitted even when operators intentionally configured `ERROR`, `CRITICAL`, or `SILENT`.
- **Recommended fix** — Do not let the raw fallback bypass policy. Either:
  - skip the alert entirely when `tg_helpers.sh` is unavailable, or
  - inline the same threshold check before the raw `curl` send.  
  Reusing the same alert-threshold logic already implemented in `tg_helpers.sh` is the safest path.

### Section 2: GitHub API Call Redundancy Audit

Distinct from the already-documented polling/watch-loop hotspots, I found the following additional API candidates.

#### API-001 — `clarify.yml` fetches issue comments twice when semantic cache is enabled
- **ID** — `API-001`
- **File path(s)** — `.github/workflows/clarify.yml:384-414`
- **Severity** — Low
- **Category tag** — `api-redundancy`
- **Current call count** — **2** comment-fetch calls per cache-enabled run:
  - 1 bounded REST call at line 394 for `ISSUE_COMMENTS_FILE`
  - 1 paginated REST call at lines 398-410 for `THREAD_HISTORY_FILE`
- **Proposed call count** — **1** paginated fetch, with both artifacts derived locally
- **Description** — The step fetches `repos/{repo}/issues/{ISSUE_NUMBER}/comments` twice. The second, paginated fetch already subsumes the first page used by `ISSUE_COMMENTS_FILE`, so the first request becomes redundant whenever `SEMANTIC_CACHE_BACKEND != none`.
- **Recommended fix** — Fetch paginated comment JSON once into a temp file using the existing `scripts/gh_helpers.sh` file-oriented helpers (prefer `gh_api_json_to_file` or `gh_retry_to_file`), then:
  - derive the bounded JSON written to `ISSUE_COMMENTS_FILE` locally,
  - derive the rendered thread-history text for `THREAD_HISTORY_FILE` locally with `jq`.  
  This reduces the path from 2 calls to 1 without changing output semantics.

#### BATCH-001 — `review_rb_judge.sh` does a GraphQL number lookup, then re-fetches issue details one-by-one
- **ID** — `BATCH-001`
- **File path(s)** — `scripts/review_rb_judge.sh:206-244`
- **Severity** — Medium
- **Category tag** — `api-batching`
- **Current call count** — **1 GraphQL + 1..N REST** calls:
  - 1 GraphQL call at lines 206-211 to get linked issue numbers
  - up to N REST issue fetches at lines 227-243 while searching for the first issue/body and capturing labels
- **Proposed call count** — **1 GraphQL** call
- **Description** — The script already knows it needs linked issue metadata, but the first call only asks for `number`. It then loops over those numbers and calls `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"` until it finds the first useful body, while also extracting labels for `FIRST_ISSUE`. That is a textbook N+1 pattern on a hot review-blocked path.
- **Recommended fix** — Extend the initial GraphQL selection set so the first request returns the fields the loop actually consumes for `FIRST_ISSUE`: at minimum `number`, `body`, and labels. The existing batching pattern to extend is `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`; it already returns issue metadata keyed by issue number and matches the shape this script needs. That would collapse the path from `1 + N` to `1`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Integration-ref staging logic is duplicated across five workflows
- **ID** — `DUP-001`
- **File path(s)** — `.github/workflows/clarify.yml:53-125`; `.github/workflows/plan.yml:84-159`; `.github/workflows/implement.yml:259-332`; `.github/workflows/orchestrate_clarify_respond.yml:87-160`; `.github/workflows/validate.yml:72-145`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — These five workflows all embed the same long shell block to:
  - validate an issue number,
  - stage a checkout of `shubhodeep1/coding-workflows`,
  - sanitize clone logs,
  - run `scripts/resolve_integration_ref.sh`,
  - and fail open to the default branch on any staging/resolution error.  
  The copies are already drifting: `plan.yml` writes an extra `integration_branch_meta` output at lines 153-155 while the others do not.
- **Recommended fix** — Move the staging/orchestration wrapper into a shared shell module, for example `scripts/integration_ref_helpers.sh`, and expose a single function such as:
  - `resolve_integration_ref_with_stage <issue_number> <repository> <gh_token> [script_ref]`  
  returning the resolved ref on stdout (and optional metadata via a temp output file if needed).  
  **Callers to update:** `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, and `validate.yml`. Keep `scripts/resolve_integration_ref.sh` as the core resolver and make the helper own only the checkout/staging/fallback wrapper.

#### DUP-002 — Support-repo checkout/copy helpers are reimplemented in multiple workflows
- **ID** — `DUP-002`
- **File path(s)** — `.github/workflows/issue_pr_status.yml:41-171,466-499,555-593`; `.github/workflows/validate.yml:185-512`; `.github/workflows/validation-improvements-intake.yml:48-140`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The repository has several near-identical implementations of:
  - `wf_source` / `script_ref` selection,
  - support-repo staging under `${RUNNER_TEMP}`,
  - `checkout_support_ref`,
  - `fetch_from_ref_or_local` or `copy_from_ref_or_local`,
  - fallback-to-`main` logging,
  - copy-into-workspace logic.  
  `issue_pr_status.yml` duplicates this pattern twice more for `tg_helpers.sh` in its merged-alert and cleanup steps, which means future changes to support checkout semantics must be edited in several places.
- **Recommended fix** — Create a shared shell module such as `scripts/support_checkout_helpers.sh` with functions like:
  - `stage_support_repo <wf_source> <script_ref> <stage_root>`
  - `copy_from_staged_support <repo_path> <target_path> [require_remote=false] [allow_main_fallback=true]`  
  **Callers to update:** the `issue_pr_status.yml` support fetch step, both `issue_pr_status.yml` Telegram helper fetch blocks, `validate.yml`, and `validation-improvements-intake.yml`. This centralizes fallback semantics and removes the copy/paste surface.

#### DUP-003 — `review_autofix.yml` redefines label-helper fallbacks instead of reusing the canonical helper
- **ID** — `DUP-003`
- **File path(s)** — `.github/workflows/review_autofix.yml:588-614,4014-4056,4145-4176,4891-4908`; `scripts/label_helpers.sh:110-196`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — `review_autofix.yml` carries four separate inline fallback implementations of `ensure_label_exists` / `set_issue_phase_label_resilient`. The canonical implementation already exists in `scripts/label_helpers.sh` and includes central label catalogs plus phase-exclusive `PUT` semantics. The inline copies only `POST` labels and hardcode special cases (`ai:review-skipped`, `ai:ready-to-merge`, `ai:closed`, `ai:review-blocked`), so behavior can diverge if support helpers go missing late in the workflow.
- **Recommended fix** — Make `scripts/label_helpers.sh` the only owner of label mutation behavior and call its existing signatures directly:
  - `ensure_label_exists <label_name> [repo]`
  - `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`  
  **Callers to update:** deterministic-skip merge labeling, the late-stage linked-issue relabel steps, and the terminal review-blocked failure path. If a minimal emergency fallback is still required, implement that fallback once inside `scripts/label_helpers.sh` instead of redefining it inline in YAML.

### Section 4: Expression Size Limit Risk Assessment

Static scan of interpolated `run:` bodies flagged four blocks above the repo’s requested thresholds. Counts below are estimated from the checked-in `run:` body text that contains `${{ }}` interpolation.

#### EXPR-001 — `test-and-mark-stable.yml` Phase 4 wait loop is one edit away from the 21 KB ceiling
- **ID** — `EXPR-001`
- **File path(s)** — `.github/workflows/test-and-mark-stable.yml:1203-1587`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Estimated current character count** — **19,899**
- **Headroom remaining** — **1,101**
- **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block is already at ~94.8% of the hard 21,000-character limit. This step embeds a large polling loop, retry/backoff wrapper, live-log shortcuts, and several `${{ }}` interpolations. The repo has already hit this GitHub Actions ceiling multiple times; this block is now in the same risk band.
- **Recommended fix** — Extract the wait-loop body into an external script such as `scripts/test_and_mark_wait_review.sh` and pass the few required values via environment variables. If extraction is not possible immediately, split the step into smaller phases: run discovery, run-state polling, live-log shortcuts, and timeout diagnostics.

#### EXPR-002 — `review_autofix.yml` PR-metadata collection block is already above the 15 KB warning band
- **ID** — `EXPR-002`
- **File path(s)** — `.github/workflows/review_autofix.yml:1370-1759`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Estimated current character count** — **17,408**
- **Headroom remaining** — **3,592**
- **Description** — The `Collect PR metadata` step combines a custom retry wrapper, PR payload fetches, linked-issue GraphQL fetch/fallback logic, large inline Python for comments-context construction, and multiple `${{ }}` interpolations. It is already past the repo’s 15 KB medium-risk threshold and has limited headroom for future edits.
- **Recommended fix** — Move this logic into a dedicated script such as `scripts/review_collect_pr_metadata.sh`, or split it into separate steps for:
  - PR/review/comment fetch,
  - linked-issue context construction,
  - comments-context materialization,
  - diff snapshot capture.  
  Externalizing the inline Python blocks to `scripts/*.py` would reduce the expression body quickly.

#### EXPR-003 — `test-and-mark-stable.yml` canary verification/retry step is also in the medium-risk band
- **ID** — `EXPR-003`
- **File path(s)** — `.github/workflows/test-and-mark-stable.yml:1673-2078`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Estimated current character count** — **17,408**
- **Headroom remaining** — **3,592**
- **Description** — The `Phase 4b: Verify editor restored canary (pytest + retry)` step contains helper functions for API retry, canary fetching, PR-state polling, pytest classification, retry dispatch, and retry-run monitoring, all inline inside one interpolated `run:` block. This is exactly the kind of growing operational script that tends to tip over the Actions parser limit during future maintenance.
- **Recommended fix** — Extract the full flow into `scripts/test_and_mark_verify_canary.sh`. A second-best option is to split “attempt 1”, “retry dispatch/poll”, and “attempt 2 verification” into separate steps so each template body stays comfortably below the ceiling.

#### EXPR-004 — `validate.yml` support-fetch step is already past the medium-risk threshold
- **ID** — `EXPR-004`
- **File path(s)** — `.github/workflows/validate.yml:188-512`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Estimated current character count** — **15,134**
- **Headroom remaining** — **5,866**
- **Description** — The `Fetch workflow support files` step includes support-repo staging, generalized file-copy helpers, multiple optional fetch branches, schema bootstrapping, and large here-doc file lists in one interpolated `run:` body. It is only barely over the medium threshold today, but it is structurally similar to earlier blocks that have already had to be extracted elsewhere in this repo.
- **Recommended fix** — Move the support-fetch implementation into a shared script such as `scripts/fetch_workflow_support.sh` and keep YAML responsible only for inputs/outputs. This would also address `DUP-002`.

**Workflow file-size note:** no workflow exceeded the 800 KB early-warning threshold. The largest files measured were `review_autofix.yml` at **297,527** bytes and `test-and-mark-stable.yml` at **274,158** bytes.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Reserved label-repair evidence helpers are shipped but not wired into the active poller
- **ID** — `DEAD-001`
- **File path(s)** — `scripts/orchestrate_lib.py:988-1425`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `scripts/orchestrate_lib.py` defines a full contradiction-evidence chain:
  - `parse_phase_failure_markers`
  - `evaluate_phase_failure_resume`
  - `resolve_label_repair_evidence`
  - `choose_most_advanced_conclusive_evidence`  
  A repository-wide search found no call sites outside this module, and both `agents.md:126-133` and `README.md:1092-1093` explicitly describe this path as “contract/reserved” and “not yet wired.” That means the code is currently inert in production while still carrying maintenance and behavioral surface area.
- **Recommended fix** — Either:
  - wire `resolve_label_repair_evidence(labels, comments, linked_pr)` into the active label-repair path in `scripts/orchestrate_poll_process.sh` behind an explicit feature gate, or
  - move the reserved implementation behind a narrower module boundary with rollout tests so it is not shipped as apparently-live helper code without callers.

#### SHELL-001 — Secret-file case pattern has an unreachable branch
- **ID** — `SHELL-001`
- **File path(s)** — `scripts/validate_changed_files_syntax.sh:70-73`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — The case arm starting with `*.env*` at line 71 already matches `.envrc` and `.env*`, so the later `*,*.envrc|*,.env*` pattern at line 73 can never be reached. ShellCheck reports this as `SC2221` / `SC2222`. The current behavior still sets `skip_dump=1`, but the redundant arm is misleading and makes future edits riskier because it looks like `.envrc` has a distinct handling path when it does not.
- **Recommended fix** — Collapse these patterns into one canonical arm, or move any genuinely distinct `.envrc` handling before the broad `*.env*` glob. If `.envrc` is meant to behave identically, delete the later pattern entirely.

**Repository-wide note:** I did not find any `TODO`, `FIXME`, or `HACK` markers under `.github/workflows/` or `scripts/` during the audit scan.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 7 | CONSIST-001, BATCH-001, DUP-001, DUP-002, EXPR-002, EXPR-003, EXPR-004 |
| Low | 5 | CONSIST-002, API-001, DUP-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1-2 | Small |
| API call optimization | 2-3 | Medium |
| Code modularization | 8-10 | Large |
| Expression size reduction | 3-6 | Large |
| Medium/Low fixes | 4-6 | Medium |
