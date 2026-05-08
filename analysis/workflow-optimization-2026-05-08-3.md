## Executive Summary

- **`review_autofix` is the dominant latency and spend hotspot in the observed window.** In `shubhodeep1/coding-workflows` run `25564101139`, `review / codex-agent / Run reviewer models` took `1643.7s`, `Apply fixes with editor model` took `1079.3s`, and pre-review check-run waiting added another `144.9s`; the family’s `p95` is `2408.8s` across `109` runs, even though its `p50` is only `46s` because many runs skip early. **Estimated impact:** `6–18 min` faster on heavy review runs. **Confidence:** high.

- **Comment-only Claude-branch review is over-provisioned for the work it does.** Evidence-grade run summaries show `review_autofix` run `25563830468` succeeded in `1165s` and run `25565928148` was cancelled at `343s`, both on `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.` **Estimated impact:** `35–60%` lower latency and materially lower model spend on that path. **Confidence:** high.

- **Preventable bad retries are wasting both time and tokens.** In `review_autofix` run `25564101139`, reviewer `qwen/qwen3.6-plus` failed attempt 1 with `No usable temporary directory found ...` after logging `tokens used = 1,838,914`, then succeeded on attempt 2. **Estimated impact:** remove a known retry class and avoid up to `1.84M` tokens per avoided bad retry. **Confidence:** high.

- **Stable release smoke is bottlenecked by nested workflow watching, not by the local watcher timeout anymore.** Failed `test_and_mark_stable` run `25558853263` failed because child `workflow-log-analysis` run `25558885206` concluded `cancelled`, while successful stable run `25548305105` spent `4094.8s` in `orphan-workflows-test` and `4141.7s` in `e2e-smoke-test`. **Estimated impact:** `20–48 min` faster stable-smoke runs with a lighter analysis mode plus less aggressive polling. **Confidence:** medium-high.

- **CI is healthy but structurally serial.** `ci` is `51/51` successful with `p50 625s` and `p95 654.5s`; recent runs `25565927939` (`607s`) and `25566161868` (`640s`) were dominated by one `lint` job in `.github/workflows/ci.yml`. **Estimated impact:** `3–5 min` lower wall time on every CI run by splitting independent checks into parallel jobs. **Confidence:** high.

- **AI memory retrieval and prompt-cache telemetry are not delivering usable value yet.** Across parsed deep-dive telemetry, `retrieve` hit rate was `0%` (`0/8`), every retrieve returned `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`, and all sampled prompt-cache probes reported `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. **Estimated impact:** medium token/latency savings once retrieval and cache observability are fixed. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Right-size `review_autofix` for Claude-branch/comment-only reviews**

   - **Evidence:** Evidence-grade summaries for `review_autofix` runs `25563830468` (`1165s`, success) and `25565928148` (`343s`, cancelled) both show `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.` Another cancelled run, `25563551926`, ended at `397s` while `codex-agent (claude-branch-review)` was still running.
   - **Root cause:** The comment-only path is still paying for the expensive reviewer panel even though downstream edit/judge/merge work is intentionally skipped.
   - **Exact change:** In `.github/workflows/review_autofix.yml`, add a `claude_branch_review`-specific budget: set `ENABLE_REVIEWER_TWO_PASS=false`, reuse the existing smoke-style lower reasoning override pattern, and define a smaller dedicated reviewer set for comment-only review paths.
   - **Estimated time savings:** `400–700s` per long Claude-branch-review run, with additional waste avoided on runs that currently cancel after several minutes.
   - **Implementation risk:** **Low-medium.** This path already produces comment-only output, so narrowing depth is less risky than changing the main autofix path.

2. **Lower default second-pass reviewer cost on the general heavy `review_autofix` path**

   - **Evidence:** In run `25564101139`, `review / codex-agent / Run reviewer models` took `1643.7s`. Source in `.github/workflows/review_autofix.yml` shows `ENABLE_REVIEWER_TWO_PASS=true`, `REVIEWER_REASONING_EFFORT=xhigh`, `REVIEWER_PASS2_REASONING_SMALL=xhigh`, and `REVIEWER_PASS2_REASONING_LARGE=xhigh` by default. The same run’s gate logged `AUTOFIX_GATE_DET_SKIP_EVAL pr=2310 files=3 additions=0 deletions=? ... skip=false`, so the expensive path proceeded.
   - **Root cause:** Executed `review_autofix` runs default to a six-model, two-pass, high-reasoning panel unless skipped by a gate.
   - **Exact change:** Keep the existing self-trigger and deterministic skip gates, but reduce second-pass defaults to something like `REVIEWER_PASS2_REASONING_SMALL=low` and `REVIEWER_PASS2_REASONING_LARGE=medium`; optionally disable pass 2 when no workflow files are touched and there is no failing CI context to incorporate.
   - **Estimated time savings:** `300–600s` on heavy runs, depending on how often pass 2 is suppressed.
   - **Implementation risk:** **Medium.** Review breadth may drop slightly, but the first-pass reviewer panel still runs and human review remains the safety net.

3. **Skip the editor entirely when the consolidator produced no actionable markers**

   - **Evidence:** In run `25564101139`, `review / codex-agent / Apply fixes with editor model` took `1079.3s`. That step logged `stage=parser event=no_issue_markers failopen=1`, `parse_failed=1`, `parsed_blocks=0`, while still assembling `pre_assembled_static.txt bytes: 88003`, `editor_prompt_body.txt bytes: 152150`, and `Editor prompt bytes: 240547`.
   - **Root cause:** The editor still runs a full autofix attempt even when the parser says there are no parseable issue markers to act on.
   - **Exact change:** If the parser reports `parsed_blocks=0` / `parse_failed=1` / `no_issue_markers`, skip the editor model and post the reviewer summary only. For cases that still need the editor, reduce `TARGETED_FILE_CONTEXT_MAX_BYTES` on comment-only or no-CI-failure paths.
   - **Estimated time savings:** Up to the full `1079.3s` on similar no-marker runs; smaller but still useful savings on editor runs with trimmed context.
   - **Implementation risk:** **Low** for the skip-on-no-marker condition; **medium** for broader context trimming.

4. **Split CI into parallel jobs instead of one long serial `lint` job**

   - **Evidence:** The `ci` family is `51/51` successful with `avg 622.94s`, `p50 625s`, and `p95 654.5s`. Recent runs `25565927939` (`607s`), `25566161868` (`640s`), and `25564041478` (`635s`) were all dominated by the single `lint` job. Source in `.github/workflows/ci.yml` shows one `lint` job containing independent checks: unit tests, coverage gates, and `check_workflow_script_refs.py`.
   - **Root cause:** Independent checks are serialized inside one job.
   - **Exact change:** Split CI into separate jobs such as `workflow-lint`, `python-tests`, `coverage-gates`, and `script-ref-check`, then keep branch protection on the set of jobs rather than a monolithic job.
   - **Estimated time savings:** `180–300s` per CI run, depending on runner queue time.
   - **Implementation risk:** **Low-medium.** The behavior does not change; only job topology changes.

5. **Add a light/smoke mode for `workflow-log-analysis` when called from stable smoke workflows**

   - **Evidence:** Successful `workflow_log_analysis` run `25548339583` took `3738s`, with `collect-logs` at `85.3s`, `analyze-commit-notify` at `714.1s`, `deep-audit` at `1364.7s`, and `api-redundancy` at `1539.2s`. Source confirms these are intentionally serialized today: `analyze-commit-notify` needs `collect-logs`, `deep-audit` needs `analyze-commit-notify`, and `api-redundancy` needs both `analyze-commit-notify` and `deep-audit`. Stable run `25548305105` then spent `4094.8s` in `orphan-workflows-test`.
   - **Root cause:** Stable smoke is exercising the full long-running analysis chain even when the smoke objective is likely “dispatch works and core analysis runs,” not “all audits complete.”
   - **Exact change:** Add a `smoke_mode` or `analysis_depth=light` input to `workflow-log-analysis` that runs only `collect-logs` plus `analyze-commit-notify`; reserve `deep-audit` and `api-redundancy` for scheduled or full-analysis paths.
   - **Estimated time savings:** `1364–2900s` per smoke invocation, depending on whether one or both audit jobs are skipped.
   - **Implementation risk:** **Medium.** This is safe if smoke intent is dispatch/health validation rather than full audit correctness. If not, keep full mode for nightly/mainline jobs.

### Micro-optimizations

6. **Stop waiting on sibling check-runs just to enrich review context**

   - **Evidence:** In `review_autofix` run `25564101139`, `review / codex-agent / Collect PR check-run failures (CI/lint autofix context)` spent `144.9s` polling `repos/.../commits/${HEAD_SHA}/check-runs?per_page=100` eight times. The log shows `Waiting for 1 in-progress/queued check-run(s) ...` every `20s`.
   - **Root cause:** A context-gathering step blocks on sibling checks instead of taking a best-effort snapshot.
   - **Exact change:** Snapshot once and start review immediately; optionally refresh the check-run context only before editor/judge. If polling remains necessary, back off from `20s` to `60s` after the first minute.
   - **Estimated time savings:** `90–145s` on affected heavy review runs.
   - **Implementation risk:** **Low.** The first reviewer pass may occasionally see slightly stale CI context, but the job no longer idles waiting to begin.

## Cost Optimizations

1. **Shrink the reviewer footprint on comment-only / Claude-branch-review paths**

   - **Evidence:** Runs `25563830468` (`1165s`) and `25565928148` (`343s`) both ran the comment-only Claude-branch-review path. The `review_autofix` workflow is configured for six reviewer models plus two-pass review by default, and the comment-only path explicitly skips editor/commit/judge/auto-merge.
   - **Root cause:** The path is paying for broad multi-model review depth even though the output is advisory only.
   - **Exact change:** For `claude_branch_review`, use a smaller dedicated reviewer set, single-pass review, and the lower-reasoning override pattern already used for smoke cases.
   - **Estimated savings:** **High.** Exact token totals were not emitted for the whole panel, but eliminating one pass and reducing the model count should cut spend by roughly one-third to two-thirds on that path (**inference** based on the current 6-model, 2-pass shape).
   - **Quality-risk notes:** **Low-medium.** Because no code is pushed on this path, lower reviewer depth is safer than on the main autofix path.

2. **Eliminate bad retries before remote model calls begin**

   - **Evidence:** In run `25564101139`, reviewer `qwen/qwen3.6-plus` failed attempt 1 with `No usable temporary directory found ...` after logging `tokens used = 1,838,914`, then succeeded on attempt 2.
   - **Root cause:** Environment preflight is happening too late; the workflow discovers tempdir issues after invoking expensive model work.
   - **Exact change:** Create and validate a writable `TMPDIR` before reviewer/editor subprocesses start; if validation fails, skip the affected model immediately rather than retrying after spend has already occurred.
   - **Estimated savings:** Up to `1,838,914` tokens per avoided bad retry on that model in the observed failure mode, plus retry latency.
   - **Quality-risk notes:** **None to positive.** This removes waste without reducing review quality.

3. **Skip editor spends on no-op cases**

   - **Evidence:** In run `25564101139`, the editor step built a `240547`-byte prompt and then logged `no_issue_markers` / `parse_failed=1`.
   - **Root cause:** The workflow still pays for an editor call even when the parser indicates nothing actionable was produced upstream.
   - **Exact change:** On `parsed_blocks=0` / `no_issue_markers`, stop after posting the reviewer summary.
   - **Estimated savings:** One full editor-model call per such run. Exact token savings cannot be quantified from this window because editor token telemetry was not emitted, but the `240547`-byte prompt indicates the avoided input is substantial.
   - **Quality-risk notes:** **Low** if restricted to explicit no-action cases.

4. **Tighten `workflow_log_analysis` widened-coverage summarization scope**

   - **Evidence:** In `workflow_log_analysis` run `25548339583`, `summarize_unselected_runs` used model `openai/gpt-5.4-mini`, summarized `97` of `100` targeted runs, and reported `tokens_used = 187077`. The same step logged `WORKFLOW_LOG_SUMMARY_MAX_RUNS: 100` and `WORKFLOW_LOG_SUMMARY_TOKEN_BUDGET: 1500000`.
   - **Root cause:** Widening coverage is already using a cheaper model, but it is still summarizing a large set of runs each time.
   - **Exact change:** Lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS` to `40–60`, or target only workflow families lacking deep-dive logs in the current collection window.
   - **Estimated savings:** Roughly `77k–110k` tokens per `workflow_log_analysis` run if the scope is reduced from `97` summaries to about `60` or `40` (**linear inference** from the observed `187077` tokens).
   - **Quality-risk notes:** **Low.** Deep-dive runs and explicit `log_summary` fields still provide broad coverage.

5. **Fix prompt-cache observability before trying to tune cache economics**

   - **Evidence:** Across sampled slow review runs, `OPENROUTER_PROMPT_CACHE_DISABLED=false` and `cache_enabled=true`, but every emitted `review_autofix_cache_probe` line reported `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. I found `16` such probe lines in the sampled deep-dive logs, including duplicated whole-job logs.
   - **Root cause:** The system is trying to expose cache behavior but is not logging usable provider counters.
   - **Exact change:** Emit actual prompt/completion/cache counters for reviewer and editor calls, not only the probe. Add a stable phase/prompt-prefix hash so cacheability can be compared across runs.
   - **Estimated savings:** Not quantifiable today, which is itself the problem. This is the prerequisite to proving or improving cache ROI.
   - **Quality-risk notes:** **None.** Observability-only change.

## Reliability Improvements

1. **Preflight and pin a writable temp directory before reviewer/editor execution**

   - **Failure evidence:** In `review_autofix` run `25564101139`, reviewer `qwen/qwen3.6-plus` failed attempt 1 with `No usable temporary directory found in ['/tmp', '/var/tmp', '/usr/tmp', '/home/runner/work/coding-workflows/coding-workflows']`, then succeeded on attempt 2 after the retry.
   - **Root cause category:** Execution environment / temp-space preflight gap.
   - **Exact fix:** Create and export a dedicated `TMPDIR` under `${RUNNER_TEMP}` or a known writable workspace temp directory at job start; verify write access before any reviewer/editor subprocess is invoked.
   - **Expected reliability impact:** Removes one concrete retry class already observed in production and avoids “expensive failure then retry” behavior.
   - **Rollback / fail-open:** If `TMPDIR` creation fails, skip only the affected model and continue with the remaining reviewers rather than failing the whole run.

2. **Treat child workflow `cancelled` as a bounded retryable outcome in stable smoke**

   - **Failure evidence:** Failed `test_and_mark_stable` run `25558853263` ended in `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` because child run `25558885206` concluded `cancelled`. Source already shows the watcher timeouts in `.github/workflows/test-and-mark-stable.yml` were increased, so “raise timeout again” is no longer the right first fix.
   - **Root cause category:** Downstream orchestration / nested workflow lifecycle.
   - **Exact fix:** When the child workflow concludes `cancelled`, retry dispatch once or allow a soft replacement attempt before hard-failing the parent smoke job; keep hard failure for explicit child `failure`.
   - **Expected reliability impact:** Reduces smoke failures caused by transient child cancellations. The family sample is small (`2` runs), but half the observed runs hit this class.
   - **Rollback / fail-open:** Bound the retry to one additional dispatch; if the second child also cancels, preserve the current hard failure.

3. **Remove fallback-to-`main` drift in `issue_pr_status` support checkout**

   - **Failure evidence:** Recent `issue_pr_status` run `25567900180` logged `::warning::Support checkout ref 2b536b38770d41dafa1204c3c293563da71f6076 is unavailable; using main.` multiple times inside `sync-status / sync-issue-status`.
   - **Root cause category:** Dependency pin drift / support-source mismatch.
   - **Exact fix:** Pass the expected support ref explicitly from caller to callee, validate it once up front, and either fail early or emit a single explicit downgrade path that records the actual fallback ref used.
   - **Expected reliability impact:** Reduces non-deterministic behavior where the workflow body and support scripts silently diverge.
   - **Rollback / fail-open:** Keep the current fallback behavior behind a flag during rollout.

4. **Health-check Semble/index availability once per job and choose a deterministic fallback**

   - **Failure evidence:** In `review_autofix` run `25564101139`, reviewer and editor steps logged `SEMBLE_FALLBACK target=Reviewer Context reason=index_unavailable`, `SEMBLE_FALLBACK target=Consolidator Context reason=index_unavailable`, and `SEMBLE_FALLBACK target=Editor Referenced Context reason=index_unavailable`. Across the sampled deep-dive files, I found `161` `SEMBLE_FALLBACK` lines, though that total includes duplicated whole-job logs.
   - **Root cause category:** Local context-index availability / repeated degraded retrieval attempts.
   - **Exact fix:** Add one preflight health check that sets a single `SEMBLE_AVAILABLE` decision for the job, then go directly to the configured `marker` or `read` fallback if the index is unavailable.
   - **Expected reliability impact:** Fewer repeated retrieval failures, more stable prompt construction, and less noisy degraded behavior.
   - **Rollback / fail-open:** Preserve current fallback modes; only skip the failing index query path.

5. **Emit minimal AI memory telemetry even on gate-only/skip paths**

   - **Failure evidence:** Recent `review_autofix` gate-only run `25566526952` had no AI memory telemetry in the provided logs, even though other deep-dive review runs did emit it.
   - **Root cause category:** Observability gap on short-circuit paths.
   - **Exact fix:** Emit a tiny `path=gate_only` / `retrieve_skipped=true` / `reason=...` event on every execution path, including self-trigger skips and deterministic skips.
   - **Expected reliability impact:** Improves diagnosis and rollback safety when future regressions occur on short-circuit paths.
   - **Rollback / fail-open:** No behavioral change; telemetry-only.

## AI Memory Health

Telemetry was present in the deep-dive logs, but it is not yet very useful operationally.

- **Machine-parsable telemetry found:** `33` JSON records across the sampled `errors/`, `slow/`, and `recent/` logs.
- **Additional non-JSON/truncated telemetry lines:** `29`. These included fragments such as callback payloads in `copilot_pull_request_reviewer` run `25565930306`, which were not machine-parsable.
- **Retrieve operations:** `8`
  - **Hit rate:** `0%` (`0/8` had `records_selected > 0`)
  - **Average `estimated_tokens`:** `0`
  - **Budget comparison:** the sampled retrieve JSON did **not** include a budget field, so `estimated_tokens` vs budget cannot be quantified from this window
  - **`keyword_method` distribution:** `none = 8`, `plain = 0`, `llm = 0`
  - **`enabled: false` entries:** `0`
  - **`fail_open: true` retrieve entries:** `0` observed; the field was usually absent rather than explicitly `false`
  - **Zero-record retrieves:** `8/8`
- **Write-path operations:**
  - `record-run-event`: `16` JSON ops; `push_attempts=1` in `14`, `push_attempts=2` in `2`
  - `record-candidate`: `8` JSON ops; `push_attempts=1` in `7`, `push_attempts=2` in `1`
- **Ops not machine-parsed in this sampled JSON set:** no parsed `promote`, `compact`, `finalize-task`, or `processed-command-complete` JSON ops were present in the deep-dive JSON sample. Some may exist in other runs or in truncated/non-JSON lines, but they were not quantifiable here.

**What this means**

- Memory retrieval is effectively inert on the sampled review runs. For example, retrieve payloads in `review_autofix` run `25564101139` reported `enabled=true`, `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`, `role=reviewer`.
- The write path is healthier than the read path: most `record-run-event` and `record-candidate` operations pushed successfully on the first attempt, with only three observed one-step retries.
- Emission is inconsistent. Recent run `25566526952` had no AI memory telemetry in the provided logs, so short-circuit paths are not consistently observable.

**Recommended next telemetry additions**

1. Add a `budget_tokens` field to `retrieve` telemetry so the “estimated tokens vs budget” question can be answered directly.
2. Emit why `keyword_method=none` occurred, and fall back to `plain` keyword extraction when LLM keywording is unavailable.
3. Track a per-family retrieve hit-rate SLI, with alerting if `review_autofix` stays below `10%`.

## GH API Call Audit

Below are the highest-value API hygiene findings from direct runtime logs. Where I reference call counts, they are sampled step-log observations rather than repo-wide totals.

1. **Nested workflow watchers in `test_and_mark_stable` are the clearest API hotspot**

   - **Evidence:** In successful `test_and_mark_stable` run `25548305105`, `orphan-workflows-test` lasted `4094.8s`. Its step log shows the pattern:
     - discover prior run: `actions/workflows/${WF_FILE}/runs?per_page=1`
     - discover newly dispatched run: `actions/workflows/${WF_FILE}/runs?per_page=10`
     - then repeated `actions/runs/${NEW_ID}` status checks every ~`15s` until completion
   - **Observed redundancy:** Constant-interval polling for very long child runs. Over a ~`68 min` watch, this implies **hundreds** of status checks for a single child run (**inference from the log cadence**).
   - **Concrete change:** After the first few minutes, back off polling from `15s` to `60s`, or poll a broader `runs` list once per sweep rather than a per-run endpoint each time.
   - **Estimated reduction:** `50–75%` fewer status-check API calls on long child runs, plus lower rate-limit risk.
   - **Repo-specific hygiene cross-check:** This repository already uses rate-limit-aware wrappers in smoke scripts; extend that same discipline to long-watch polling frequency.

2. **`e2e-smoke-test` is doing repeated per-run status lookups that should be reused**

   - **Evidence:** In the same successful stable run `25548305105`, `e2e-smoke-test` lasted `4141.7s`, and its step log contained `74` visible `gh api` invocations. The log repeatedly shows:
     - `STATUS=$(gh api "repos/${REPO}/actions/runs/${RID}" ...)`
     - `FINAL_STATUS=$(gh api "repos/${REPO}/actions/runs/${RID}" ...)`
     - `EXISTING_STATUS=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" ...)`
   - **Observed redundancy:** The same run object is fetched multiple times to derive both `status` and `conclusion`, and multiple child workflows are polled independently.
   - **Concrete change:** Fetch each run once per sweep and derive both `status` and `conclusion` from the same response; where multiple child runs are active, poll them via one list query and join in shell/`jq`.
   - **Estimated reduction:** `30–60` API calls per smoke run, depending on the number of child workflows active.
   - **Rate-limit risk reduction:** Moderate. This is exactly the kind of loop that becomes visible under higher queue or fan-out conditions.

3. **`review_autofix` is re-polling check-runs for context instead of treating them as a snapshot**

   - **Evidence:** In `review_autofix` run `25564101139`, `Collect PR check-run failures (CI/lint autofix context)` hit `repos/.../commits/${HEAD_SHA}/check-runs?per_page=100` eight times over `144.9s`, waiting for one in-progress sibling check-run.
   - **Observed redundancy:** Context gathering blocks and repolls even though the review can often begin from the first snapshot.
   - **Concrete change:** Cache the first response and proceed; if freshness matters, refresh once just before editor/judge. If polling must remain, slow down after the first minute.
   - **Estimated reduction:** From `8` calls to `1–2` on similar runs, or about `75–87%` fewer check-run API calls for that step.
   - **Repo-specific hygiene cross-check:** The `review_autofix` gate already follows a good cheap-first rule by using cheap PR metadata before paginating `/pulls/{n}/files`; apply the same “pay only when needed” rule to check-run context collection.

4. **`issue_pr_status` still makes a multi-call linked-issue pass even when there are no linked issues**

   - **Evidence:** Recent run `25567900180` (`10s`) logged `5` visible `gh api` calls in `sync-status / sync-issue-status`, including GraphQL linked-issue lookup, PR fetch, labels POST scaffolding, and orchestrator metadata fetch, even though the run ended with `No linked issues found for PR #2314.`
   - **Observed redundancy:** The job does several discovery calls before proving there is no linked-issue work to do.
   - **Concrete change:** Return all required PR title/body plus linked-issue metadata in one GraphQL query, and short-circuit before follow-up issue metadata requests when the link set is empty.
   - **Estimated reduction:** `1–3` API calls per no-link run, plus clearer control flow.
   - **Rate-limit risk reduction:** Low individually, but worthwhile because this is a frequent utility workflow.

**What I did not find**

- I did **not** find direct runtime evidence of GitHub secondary rate-limit or `429` failures in the inspected deep-dive runtime steps.
- I intentionally did **not** use the AI-generated audit prose embedded inside `workflow_log_analysis` deep-audit logs as primary evidence for exact API call counts.

## Prompt Cache & Memory System

### Current state

1. **Prompt cache is enabled but effectively unauditable**
   - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in heavy and recent `review_autofix` runs such as `25564101139`, `25566526952`, and `25567900185`.
   - Cache probe lines in the sampled slow-review logs show `cache_enabled=true`, but every useful counter was `na`: `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`.
   - Result: I cannot tell whether the cache is hitting, missing, or fragmenting.

2. **Memory retrieval is enabled but not retrieving anything useful**
   - `retrieve` hit rate was `0/8`.
   - Every sampled retrieve used `keyword_method=none`.
   - No sampled retrieve emitted a token budget, so there is no way to tell whether queries were too small, too restricted, or simply never formed.

3. **Context assembly is likely too dynamic for good cache reuse** **(inference)**
   - The editor path in run `25564101139` assembled `88003` bytes of `pre_assembled_static.txt` and `152150` bytes of `editor_prompt_body.txt`, for a total prompt of `240547` bytes.
   - The reviewer path is explicitly two-pass, and pass 2 depends on pass-1 cross-pollination, which makes its prompt content inherently run-specific.
   - Because the cache counters are missing, I cannot prove cache fragmentation directly, but the current prompt shape strongly suggests limited prefix reuse across runs.

4. **Semble/index unavailability is degrading both memory quality and prompt stability**
   - In `25564101139`, the reviewer/editor path repeatedly fell back with `SEMBLE_FALLBACK ... reason=index_unavailable`.
   - Across sampled deep-dive logs, `SEMBLE_FALLBACK` appeared many times, though the raw total includes duplicate whole-job logs.

### Concrete improvements

1. **Emit real per-call cache counters**
   - Log actual `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` for reviewer and editor calls, not just the probe.
   - **Expected impact:** unlocks measurable token and latency tuning; no direct savings until this exists.
   - **Reliability impact:** higher confidence in future optimization work.

2. **Stabilize prompt prefixes**
   - Keep policy/instruction blocks and static repo guidance at the front, and append dynamic PR diffs, check-run status, linked-issue text, and pass-1 summaries later.
   - The existing `pre_assembled_static.txt` artifact suggests the workflow already has a natural static block; make sure dynamic noise does not precede it.
   - **Expected impact:** moderate token and latency savings if the provider cache keys on prompt prefixes; exact savings unavailable because counters are missing.
   - **Reliability impact:** more reproducible prompts.

3. **Bypass Semble query attempts when the index is known unavailable**
   - After a one-time availability check, go directly to `marker`/`read` fallback.
   - **Expected impact:** lower latency and smaller prompt variance on degraded runs.
   - **Reliability impact:** fewer repeated context-retrieval failures.

4. **Make memory retrieval query formation non-empty by default**
   - If LLM keywording is unavailable, use a plain deterministic keyword path rather than `keyword_method=none`.
   - Emit `query_terms_count` and `budget_tokens` in telemetry.
   - **Expected impact:** improved retrieval hit rate and better reuse of historical context.
   - **Reliability impact:** less “memory system enabled but inert” behavior.

## Orchestrator Health

### What looks healthy

- The clarify/plan/respond control plane is **not** the current bottleneck.
  - `clarify`: `201` runs, `15` success, `186` skipped, `p50 1s`, `p95 87s`
  - `plan`: `181` runs, `13` success, `168` skipped, `p50 1s`, `p95 141s`
  - `orchestrate_clarify_respond`: `182` runs, only `2` successful executions, most others skipped/other, `avg 1.36s`, `p95 2s`
  - `implement`: `182` runs, `167` skipped, `p50 1s`
- This is good news: irrelevant triggers are mostly being short-circuited quickly.

### Operational pain points

1. **Heavy review cancellation churn**
   - `review_autofix` has `39` cancelled runs out of `109` total (`35.8%`), with several cancellations occurring after meaningful work had already begun: `25565928148` at `343s`, `25563551926` at `397s`, `25559260254` at `1191s`, `25559262955` at `1165s`.
   - Smallest safe mitigation: reduce work before first durable output, especially on comment-only review paths.

2. **Stable smoke depends on long child workflow chains**
   - The release/stable path is operationally fragile because a child workflow cancellation propagates up to a long parent run, as seen in `25558853263`.
   - Smallest safe mitigation: light/smoke mode for child workflows plus one bounded retry on child `cancelled`.

3. **Poller jobs are stable but runner-bound**
   - `orchestrate_poll` is `15/15` successful with `avg 128.7s`, `p50 110s`, `p95 207.4s`.
   - Recent runs `25564828849` (`102s`) and `25566402327` (`110s`) both noted hosted-runner wait before doing modest work.
   - Smallest safe mitigation: keep the poller lightweight and avoid spawning it when there is no active orchestration state to refresh.

### Observability gaps

- I did **not** find explicit wave-progression, deferral, or conflict-heal retry telemetry in the sampled deep-dive logs, so those aspects cannot be scored confidently from this window.
- AI memory telemetry is inconsistent on gate-only paths.
- Prompt-cache effectiveness is currently opaque.

### Indicators to track next

These are the most useful leading indicators to operationalize:

- `review_autofix` runs cancelled after `>300s`
- `claude_branch_review` runs lasting `>600s`
- child workflow conclusions of `cancelled` inside `test_and_mark_stable`
- AI memory `retrieve` hit rate by workflow family
- count of `SEMBLE_FALLBACK ... index_unavailable`
- count of `Support checkout ref ... unavailable; using main`
- runner-wait incidence on short utility workflows

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact:

1. **Review / Autofix compute dominates the executed path**
   - **Compute overhead:** `review_autofix` run `25564101139` spent `1643.7s` in reviewer models and `1079.3s` in the editor.
   - **Retry overhead:** the same run retried one reviewer after a tempdir failure, with `1,838,914` tokens spent on the failed attempt.
   - **Fix first:** reduce Claude-branch-review depth, lower second-pass cost, skip no-marker editor, and preflight `TMPDIR`.

2. **Stable validation is slowed by nested workflow dispatch-and-watch loops**
   - **Compute + polling overhead:** successful stable run `25548305105` spent `4094.8s` in `orphan-workflows-test` and `4141.7s` in `e2e-smoke-test`.
   - **Retry/failure overhead:** failed stable run `25558853263` failed because child `workflow-log-analysis` run `25558885206` ended `cancelled`.
   - **Fix second:** create a lighter analysis mode for smoke and reduce constant polling.

3. **CI is serial, not flaky**
   - **Compute overhead:** CI runs are reliably around `10–11 min` because independent checks are packed into one `lint` job.
   - **Queueing overhead:** runner wait is visible in some runs, but the dominant cost is still serial compute.
   - **Fix third:** split the `lint` job into parallel jobs.

4. **Queueing exists, but it is usually secondary to compute**
   - Runner wait appears in multiple families, including `ci` (`25566161868`, `25564041478`), `orchestrate_poll` (`25566402327`), and even short utility workflows like `cancel_on_pr_close` run `25567900173`.
   - Because the worst bottlenecks are still multi-minute compute phases, I would not make runner-queue mitigation the first optimization target.

5. **Clarify → plan → implement is mostly healthy and skip-gated**
   - The control-plane workflows mostly complete in `1–2s` when they do not need to act.
   - This stage should be monitored, but it is not where end-to-end latency is currently being lost.

6. **Merge/conflict-heal overhead is not directly observable in this sample**
   - I did not find direct conflict-heal telemetry in the sampled logs.
   - The observed “rerun waste” is cancellation/re-review churn rather than explicit merge-conflict repair loops.
   - Next collection step: emit explicit conflict-heal counters before changing merge-handling behavior.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**

- `review_autofix` heavy path: run `25564101139` spent `1643.7s` in reviewer models and `1079.3s` in editor work.
- Stable smoke / release validation: run `25548305105` spent `4094.8s` in `orphan-workflows-test` and `4141.7s` in `e2e-smoke-test`.
- CI serial wall time: `51/51` successful runs but `p50 625s` because `.github/workflows/ci.yml` is a single serial job.

**Top failure modes**

- Child workflow cancellation bubbling up to stable smoke failure: `test_and_mark_stable` run `25558853263` failed because child `workflow-log-analysis` run `25558885206` concluded `cancelled`.
- Environment-driven reviewer retry: `review_autofix` run `25564101139` had a tempdir failure before succeeding on retry.
- Support-source drift: `issue_pr_status` run `25567900180` warned `Support checkout ref ... is unavailable; using main.`

**Highest-cost drivers**

- Six-reviewer, two-pass, high-reasoning review policy on executed `review_autofix` runs
- Large editor prompt assembly (`240547` bytes) even when parser found `no_issue_markers`
- `workflow_log_analysis` widened coverage summarization (`97` summaries, `187077` tokens on `gpt-5.4-mini`) plus the full long audit chain

**Top 3 prioritized actions**

1. **Introduce a reduced-cost profile for Claude-branch/comment-only `review_autofix`**  
   Single-pass, lower reasoning, smaller reviewer set.

2. **Skip no-marker editor runs and preflight `TMPDIR`**  
   This is the best combined speed + reliability + cost win on the main heavy review path.

3. **Add a smoke/light mode for `workflow-log-analysis`, then split CI into parallel jobs**  
   The former reduces multi-hour stable smoke paths; the latter shaves several minutes off every CI run.

## Metrics Appendix

### Overall and key workflow-family metrics

| Scope / Family | Total runs | Success | Failure | Cancelled | Other / skipped | Success rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall repo (`shubhodeep1/coding-workflows`) | 1000 | 251 | 1 | 42 | 706 | 25.1% | 130.96 | 1.0 | 648.0 |
| `review_autofix` | 109 | 67 | 0 | 39 | 3 | 61.5% | 587.60 | 46.0 | 2408.8 |
| `ci` | 51 | 51 | 0 | 0 | 0 | 100.0% | 622.94 | 625.0 | 654.5 |
| `workflow_log_analysis` | 2 | 1 | 0 | 1 | 0 | 50.0% | 2821.50 | 2821.5 | 3646.35 |
| `test_and_mark_stable` | 2 | 1 | 1 | 0 | 0 | 50.0% | 4637.50 | 4637.5 | 4979.05 |
| `orchestrate_poll` | 15 | 15 | 0 | 0 | 0 | 100.0% | 128.67 | 110.0 | 207.4 |
| `copilot_pull_request_reviewer` | 26 | 26 | 0 | 0 | 0 | 100.0% | 223.81 | 219.0 | 366.0 |
| `issue_pr_status` | 17 | 17 | 0 | 0 | 0 | 100.0% | 46.71 | 63.0 | 70.6 |

**Note:** Overall `p50=1.0s` is heavily distorted by `706` skipped/other runs; the real executed hot paths are `review_autofix`, `ci`, `workflow_log_analysis`, and `test_and_mark_stable`.

### Deep-dive critical step timings

| Run ID | Workflow | Job / Step | Duration (s) | Key observation |
|---|---|---|---:|---|
| `25564101139` | `review_autofix` | `review / codex-agent / Collect PR check-run failures (CI/lint autofix context)` | 144.9 | Waited on one sibling check-run with 8 polls |
| `25564101139` | `review_autofix` | `review / codex-agent / Run reviewer models` | 1643.7 | Dominant heavy-review step |
| `25564101139` | `review_autofix` | `review / codex-agent / Apply fixes with editor model` | 1079.3 | Large editor prompt, no actionable markers |
| `25548339583` | `workflow_log_analysis` | `collect-logs` | 85.3 | Small compared with downstream audit work |
| `25548339583` | `workflow_log_analysis` | `analyze-commit-notify` | 714.1 | Moderate analysis stage |
| `25548339583` | `workflow_log_analysis` | `deep-audit` | 1364.7 | Long serial audit stage |
| `25548339583` | `workflow_log_analysis` | `api-redundancy` | 1539.2 | Longest stage in the chain |
| `25548305105` | `test_and_mark_stable` | `orphan-workflows-test` | 4094.8 | Long child-workflow watch chain |
| `25548305105` | `test_and_mark_stable` | `e2e-smoke-test` | 4141.7 | Multiple dispatch-and-watch loops |
| `25558853263` | `test_and_mark_stable` | `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` | 1945.1 | Failed because child run `25558885206` concluded `cancelled` |

### Partial token, cache, and memory telemetry

| Source | Metric | Value | Notes |
|---|---|---:|---|
| `review_autofix` run `25564101139`, reviewer retry | `tokens used` on failed `qwen/qwen3.6-plus` attempt | `1,838,914` | Directly observed in step log before attempt 2 succeeded |
| `workflow_log_analysis` run `25548339583`, `summarize_unselected_runs` | `tokens_used` | `187,077` | Model `openai/gpt-5.4-mini`, `97` summaries out of `100` targeted |
| `review_autofix` run `25564101139`, editor prompt | `Editor prompt bytes` | `240,547` | `88,003` static + `152,150` body bytes |
| Sampled prompt-cache probe lines | `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` | `na` | Cache is enabled but not measurable from current logs |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| JSON telemetry records parsed | 33 |
| Non-JSON / truncated telemetry lines | 29 |
| `retrieve` ops | 8 |
| `retrieve` hit rate (`records_selected > 0`) | 0% |
| Avg `estimated_tokens` across retrieves | 0 |
| `keyword_method=none` | 8 / 8 retrieves |
| `keyword_method=plain` | 0 / 8 retrieves |
| `keyword_method=llm` | 0 / 8 retrieves |
| `enabled=false` retrieve entries | 0 |
| `fail_open=true` retrieve entries | 0 observed |
| `record-run-event` with `push_attempts=2` | 2 / 16 |
| `record-candidate` with `push_attempts=2` | 1 / 8 |

### Sampled GH API call summary

| Run ID | Workflow / Step | Observed pattern | Sampled call count / cadence | Optimization target |
|---|---|---|---|---|
| `25548305105` | `test_and_mark_stable / e2e-smoke-test` | Repeated `STATUS`, `FINAL_STATUS`, `EXISTING_STATUS` polling | `74` visible `gh api` invocations in the step log | Reuse one run response per sweep; batch multi-run polling |
| `25548305105` | `test_and_mark_stable / orphan-workflows-test` | Dispatch + long constant-interval child-run polling | `20` visible `gh api` lines plus ~`15s` polling cadence over ~`68 min` | Exponential/backoff polling or list-runs polling |
| `25564101139` | `review_autofix / Collect PR check-run failures` | Repeated `check-runs?per_page=100` polling | `8` polls in `144.9s` | Snapshot once, refresh later only if needed |
| `25567900180` | `issue_pr_status / sync-status` | GraphQL + PR + issue follow-ups even on no-link case | `5` visible `gh api` invocations | Enrich single GraphQL call and short-circuit earlier |

**Note:** These API counts come from sampled runtime logs and should be treated as step-level observations, not repo-wide totals.
