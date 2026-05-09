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

## Deep Audit — Workflows & Scripts (2026-05-08)

### Section 1: Bug & Correctness Sweep

I did not repeat the already-appended findings on `review_autofix` check-run polling or `test-and-mark-stable` child-workflow polling. The new high-confidence correctness findings from the workflow/script audit are below.

- **ID** — BUG-001  
  **File path** — `.github/workflows/issue_pr_status.yml:253-350,501-516`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The `sync-status` step classifies linked issues using both labels and body text: lines 280-349 build `TRACKING_ISSUES` and `MANAGED_ISSUES` from `ai:orchestrator-tracking`, `ai:orchestrator-managed`, and the `Managed by: AI Orchestrator` body marker. The later `send-merged-alert` step re-checks orchestration status with a different rule: lines 503-512 loop `LINKED_ISSUE_NUMBERS`, fetch each issue body, and only grep for the body marker. **Inference:** PRs linked to tracking issues, or to managed issues identified only by label, can bypass the intended “skip PR merged alert” guard and still emit Telegram merge alerts.  
  **Recommended fix** — Export a single truthy output such as `HAS_ORCHESTRATED_LINKED_ISSUE=true` from the `sync-status` step, or export the computed `TRACKING_ISSUES` / `MANAGED_ISSUES` lists via `$GITHUB_OUTPUT` / `$GITHUB_ENV`, and have `send-merged-alert` consume that state instead of re-implementing detection. If a fresh lookup is required, reuse the same batched GraphQL classifier shape already used in lines 280-349 rather than the body-only loop.

- **ID** — SHELL-001  
  **File path** — `.github/workflows/mark-stable.yml:452-490`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — The workflow reads repo slugs with `REPOS=$(jq -r '.[]' "$CONSUMER_FILE" ...)` and then iterates with `for REPO in $REPOS; do`. That relies on shell word-splitting and pathname expansion instead of preserving one JSON element per iteration. A malformed entry, embedded whitespace, or a glob character would split a single repo slug into multiple loop iterations or expand against the workspace.  
  **Recommended fix** — Read the JSON array into an actual Bash array or a line-safe loop, e.g. `mapfile -t REPOS < <(jq -r '.[]' "$CONSUMER_FILE")` and then iterate `for REPO in "${REPOS[@]}"`; alternatively use `while IFS= read -r REPO; do ... done`.

### Section 2: GitHub API Call Redundancy Audit

The in-progress report already covers the major long-polling hotspots (`review_autofix` check-runs and `test-and-mark-stable` nested workflow watchers), so I did not duplicate those. The additional code-level API hygiene findings are:

- **ID** — API-001  
  **File path** — `.github/workflows/review_autofix.yml:1455-1485`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Current call count** — Up to **N REST calls** on the fallback path, capped at **20** (`1` call per `_fb_num`).  
  **Proposed call count after fix** — **1 GraphQL batch call** for the capped fallback issue set.  
  **Description** — When `closingIssuesReferences` comes back empty and the PR-body regex fallback finds linked issue numbers, the workflow initializes `_fallback_json='[]'` and then loops `_fallback_numbers`, calling `gh_retry ... api "repos/${{ github.repository }}/issues/${_fb_num}"` once per issue. This is a direct `gh api`-inside-a-loop pattern.  
  **Recommended fix** — Extract fallback issue hydration into a shared helper that builds one aliased GraphQL query for `{ number, title, body }` across all fallback issue numbers, using the same alias-fragment batching pattern as `scripts/orchestrate_poll_process.sh:_fetch_issue_labels_batch_graphql` (`1241-1307`) or `_fetch_candidate_issue_details_graphql` (`5866-5921`). The workflow should consume one JSON array result instead of incrementally `jq`-appending N REST responses.

- **ID** — API-002  
  **File path** — `.github/workflows/issue_pr_status.yml:280-349,503-512`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Current call count** — **1 batched GraphQL call** in `sync-status`, then up to **N extra REST calls** in `send-merged-alert`.  
  **Proposed call count after fix** — **1 total batched call**; **0 extra calls** in `send-merged-alert`.  
  **Description** — The `sync-status` step already batches `number`, `labels`, and `body` for every linked issue via one GraphQL alias query (lines 295-297) and classifies them into tracking vs managed buckets (lines 304-349). The later `send-merged-alert` step discards that work and loops over `LINKED_ISSUE_NUMBERS`, re-fetching each body with `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""'`.  
  **Recommended fix** — Write a boolean or serialized classification result to `$GITHUB_OUTPUT` in `sync-status` and consume it directly in `send-merged-alert`. If step-local recomputation is required, lift the existing alias-batch query into `scripts/gh_helpers.sh` or extend the `_fetch_candidate_issue_details_graphql` batching pattern so both steps share one canonical batched implementation.

- **ID** — API-003  
  **File path** — `scripts/gh_helpers.sh:916-932,1028-1050`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Current call count** — **1 timeline REST call + N PR REST calls** on the REST fallback path.  
  **Proposed call count after fix** — **2 total calls** on eligible fallback paths: **1 REST timeline call + 1 aliased GraphQL PR enrichment batch**.  
  **Description** — `_gh_issue_timeline_with_cross_refs_rest` fetches the issue timeline once, extracts unique same-repo PR URLs, and then loops each URL through `gh_retry gh api "${pr_url}"` to discover PR state and merge status. The GraphQL-first wrapper `gh_issue_timeline_with_cross_refs` already proves that this enrichment data can come from GraphQL when the primary path succeeds. **[NEEDS VERIFICATION]** — some fallback cases are triggered by pagination (`hasNextPage=true`) or may involve cross-repo PR URLs, so not every fallback can be collapsed identically.  
  **Recommended fix** — Keep the REST timeline fetch for fallback reasons, but on non-pagination, same-repo fallback paths batch-enrich the unique PR numbers in one aliased GraphQL query before stitching results back into the timeline JSON. Extend the existing `gh_issue_timeline_with_cross_refs` transform for that enrichment phase, and keep the current per-URL REST loop only for cross-repo or paginated edge cases.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/issue_pr_status.yml:61-131,466-499`; `.github/workflows/validate.yml:206-280`; `.github/workflows/validation-improvements-intake.yml:68-134`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Three workflows duplicate the same support-checkout bootstrap: temp staging dirs, `checkout_support_ref`, `resolved_script_ref`, optional `main` fallback clone, and a copy/fetch helper (`fetch_from_ref_or_local` vs `copy_from_ref_or_local`). `issue_pr_status.yml` then duplicates a smaller variant again in the merged-alert step just to fetch `tg_helpers.sh`. The code is already drifting in naming and fallback semantics across these copies.  
  **Recommended fix** — Create `scripts/support_checkout_helpers.sh` and make it the shared owner of:  
  `checkout_workflow_support_ref <wf_source> <script_ref> <stage_root>`  
  `copy_support_file <support_primary_root> <support_main_root> <repo_path> <target_path> [require_remote] [allow_main_fallback]`  
  Update callers in `issue_pr_status.yml`, `validate.yml`, and `validation-improvements-intake.yml`, and replace the alert-step inline clone in `issue_pr_status.yml` with the same helper.

- **ID** — DUP-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3343-3405,3421-3464,3479-3529,3544-3588,3894-3941`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `test-and-mark-stable.yml` inlines the same dispatch-and-watch control flow at least five times: fetch `PRE`, dispatch `gh workflow run`, poll `runs?per_page=10` until the new run appears, then poll `actions/runs/${NEW_ID}` until completion. Only `WF_FILE`, deadline, input fields, polling interval, and accepted conclusions vary. This duplication makes timeout/backoff changes error-prone and directly contributes to the file’s size and expression-risk footprint.  
  **Recommended fix** — Extract a shared script such as `scripts/dispatch_and_watch_workflow.sh` with a signature like:  
  `dispatch_and_watch_workflow <repo> <workflow_file> <deadline_secs> <accepted_conclusions_csv> [field key=value ...]`  
  Update the repeated callers in `test-and-mark-stable.yml` to pass only per-workflow parameters and keep the soft-error-analyzer steps in YAML.

- **ID** — DUP-003  
  **File path** — `scripts/label_helpers.sh:102-197`; `scripts/validate_process.sh:523-557,583-631`; `scripts/orchestrate_poll_process.sh:1090-1145,1178-1218,1840-1868`; `.github/workflows/review_autofix.yml:610-624`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Label creation and phase-label mutation logic is reimplemented in four places. The implementations already differ: `orchestrate_poll_process.sh` caches ensured labels, `validate_process.sh` reads the contract file and emits Telegram warnings, and `review_autofix.yml` hardcodes label metadata inline. `review_autofix.yml:615-620` explicitly notes that the inline definitions “must stay in lockstep” with `scripts/label_helpers.sh`, which is direct evidence that the duplication is intentional but brittle.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner of:  
  `ensure_label_exists <label_name> [repo]`  
  `set_issue_phase_label_resilient <issue_number> <target_label> [repo]`  
  `build_issue_label_edit_args <current_labels_json> <phase_changes_json>`  
  Then source that helper from `validate_process.sh` and `orchestrate_poll_process.sh`, and have `review_autofix.yml` fetch/source it via the support-checkout helper from DUP-001 instead of hardcoding a partial clone.

### Section 4: Expression Size Limit Risk Assessment

Static counts below are **estimated current character counts** using the interpolated `run:` body text as a heuristic for blocks that contain at least one `${{ }}` interpolation. Those are the blocks that can trip GitHub’s 21,000-character expression ceiling in practice.

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1202-1586`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Estimated current character count** — ~**19,899**  
  **Estimated headroom remaining** — ~**1,101** characters  
  **Description** — The “wait-review” step contains `${{ steps.create-issue.outputs.issue_number }}` at line 1264, so the large polling/log-analysis body is expression-bearing. At ~19.9 KB, it is already within ~1.1 KB of GitHub’s hard failure point. The block mixes run discovery, activity tracking, log scraping, idle-timeout logic, and review-count heuristics, so even a modest future edit could push it over the limit.  
  **Recommended fix** — Preferred: extract the entire wait loop to `scripts/wait_review_run.sh` and keep only env wiring in YAML. Acceptable alternative: split it into separate steps such as “discover review run”, “monitor activity”, and “evaluate completion/failure”.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1672-2077`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Estimated current character count** — ~**17,408**  
  **Estimated headroom remaining** — ~**3,592** characters  
  **Description** — The “Verify editor restored canary” step contains `${{ steps.wait-review.outputs.review_run_id }}` at line 1958 and packs bootstrap diagnostics, package installs, git fetch/retry logic, pytest invocation, and review-run correlation into one interpolated `run:` block. It is below the hard ceiling today, but already over the 15 KB medium-risk threshold.  
  **Recommended fix** — Extract this block to `scripts/verify_editor_restored_canary.sh`, or split bootstrap/diagnostics away from the canary-verification logic so the GitHub expression-bearing step stays small.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/review_autofix.yml:1284-1673`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Estimated current character count** — ~**17,408**  
  **Estimated headroom remaining** — ~**3,592** characters  
  **Description** — The “Collect PR metadata” step contains many interpolations, including `${{ github.repository }}` and `${{ github.repository_owner }}` at lines 1341, 1351, 1364, 1369-1374, 1400-1401, 1458, 1473, and 1482. The block combines an inline retry helper, multiple REST/GraphQL fetches, PR diff handling, fallback linked-issue resolution, and base-branch detection. It is already above the 15 KB reporting threshold.  
  **Recommended fix** — Extract the step to `scripts/review_collect_pr_metadata.sh` and move the fallback linked-issue hydration into a shared helper in `scripts/gh_helpers.sh`. If a full extraction is not feasible, split the block into distinct steps for metadata fetch, linked-issue resolution, and diff snapshotting.

**Workflow file-size note:** no workflow is near the 800 KB warning threshold. The largest audited files were `review_autofix.yml` at **285,829 bytes**, `test-and-mark-stable.yml` at **272,275 bytes**, and `implement.yml` at **187,107 bytes**.

### Section 5: Cross-Cutting Concerns

No literal `TODO`, `FIXME`, or `HACK` markers were present in the audited `.github/workflows/*.yml` and `scripts/*.{sh,py}` targets.

- **ID** — DEAD-001  
  **File path** — `scripts/validate_changed_files_syntax.sh:70-74`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — In the redaction denylist `case "${file},${basename_lc}"`, the early `*.env*` alternative already matches inputs that the later `*,*.envrc` and `*,.env*` alternatives are trying to catch. That makes the later `.env`-specific alternatives unreachable in practice; ShellCheck reports this pattern overlap as SC2221/SC2222. Because this block governs whether file contents are suppressed from diagnostics, dead alternatives make a sensitive branch harder to audit.  
  **Recommended fix** — Remove the redundant `.env`-specific alternatives, or split the path and basename checks into separate `case` statements with comments explaining the intended coverage for `.envrc` and `.env*`.

- **ID** — DEBT-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:241-247,3144-3147,3238-3241,4585-4588,4696-4699`  
  **Severity** — Low  
  **Category tag** — `tech-debt`  
  **Description** — The workflow defines a temporary `&git-checkout-diag` anchor explicitly annotated “Remove after root-cause is identified and a targeted fix lands,” then reuses it in four later jobs. Leaving temporary checkout-probe scaffolding permanently embedded increases log noise, adds `always()` post-checkout work in unrelated jobs, and makes future checkout cleanup harder to reason about.  
  **Recommended fix** — Either delete the anchor once the checkout exit-128 root cause is fixed, or gate it behind an explicit debug input / repo variable such as `ENABLE_CHECKOUT_GIT_DIAG=false` by default. If the probe must stay long-term, move it into a reusable composite action so the troubleshooting logic is isolated from the release-test workflow.

**Additional cross-cutting note:** ShellCheck also emitted lower-priority advisories such as SC2034 unused variables in `scripts/memory_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_issue_ledger.sh`, and `scripts/review_run_reviewers.sh`. I did not elevate those because they appeared more likely to be intentional scaffolding or debug residue than active correctness defects.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 8 | BUG-001, API-001, API-002, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003 |
| Low | 4 | SHELL-001, API-003, DEAD-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 2 workflows | Medium |
| API call optimization | 2 workflows + 1 shared script | Medium |
| Code modularization | 5 workflows + 3 shared scripts | Large |
| Expression size reduction | 2 workflows (+ 2–3 extracted scripts) | Medium |
| Medium/Low fixes | 3 files | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-08)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is local enough and structurally identical enough that the implement stage can consolidate it directly without further review. `NEEDS_VERIFICATION` means the overlap is real, but at least one safety precondition is not fully provable from static reading alone, so a human or follow-on analysis must confirm parity first. `RISKY_SKIP` means the duplication is visible but sits in a retry/pagination/race-defense/log-contract-sensitive path, so it must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:443-446`, `.github/workflows/test-and-mark-stable.yml:448-448`  
  **Current call count** — `2`  
  **Proposed call count** — `1`  
  **Endpoint(s)** — `POST /repos/{owner}/{repo}/issues`, `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — The same step creates the issue, extracts `.number`, then immediately re-reads the created issue only to extract `.html_url`:
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  The second call is a strict subset read of the object returned by the first call.  
  **Proposed fix** — In the `Create issue` step, capture the `POST /issues` response once into a temp file or variable, then parse both `.number` and `.html_url` from that single payload; keep the existing hard-fail behavior by explicitly checking that both extracted fields are non-empty.  
  **Safety rationale** — Same step, no intervening mutation, no pagination, same auth, and the second call only re-reads fields that are part of the newly created issue object.  
  **Downstream signal** — Replace the create+read pair with one `gh api "repos/${TEST_REPO}/issues"` capture and derive both `ISSUE_NUMBER` and `ISSUE_URL` from that response, failing if either parsed field is empty.

- **ID** — `MERGE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/review_autofix.yml:511-528`, `.github/workflows/review_autofix.yml:536-543`  
  **Current call count** — On the regex-fallback path, `1 + N` API lookups (`1` PR-body fetch plus up to `N` per-issue label lookups).  
  **Proposed call count** — On the regex-fallback path, `2` total (`1` PR-body fetch plus `1` batched linked-issue label hydration).  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{pr_number}`; per-issue label lookups via `gh issue view ... --json labels`; proposed replacement is one batched GraphQL issue-label query.  
  **Evidence** — When `closingIssuesReferences` is empty, the step synthesizes `issue_nodes_json` with `labels: null`, then falls back to one label lookup per issue:
  ```bash
  if [ -z "${issue_nodes_json}" ] || [ "${issue_nodes_json}" = "[]" ]; then
    pr_data="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
    ...
    issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0)) | map({number: tonumber, labels: null})')"
  fi

  ...

  if [ "${labels_known}" != "true" ]; then
    issue_labels="$(gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null || true)"
    if echo "${issue_labels}" | grep -Fxq 'ai:orchestrator-validate-required'; then
      has_validate_label="true"
    else
      has_validate_label="false"
    fi
  fi
  ```
  This is a direct per-item metadata fetch after a nearby discovery call has already materialized the full fallback issue set.  
  **Proposed fix** — Extend the fallback branch so that after `issue_numbers` is derived, it batch-hydrates `{number, labels}` for that issue set in one aliased GraphQL call, following the batching pattern already used by `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`; keep the current per-issue `gh issue view` loop only as a fail-open fallback when the batch query fails.  
  **Safety rationale** — The overlap is local and in one step, but the current path mixes REST regex fallback with CLI label lookups, so parity of the fallback issue set and fail-open behavior is not fully provable without runtime verification.  
  **Downstream signal** — Verify on merged PRs that hit both the normal `closingIssuesReferences` path and the regex-fallback path that a batched label map yields the same `ai:orchestrator-validate-required` decisions as the current per-issue `gh issue view` loop before replacing it.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/review_autofix.yml:1399-1526`, `.github/workflows/review_autofix.yml:1917-1921`  
  **Current call count** — `1` early linked-issue GraphQL fetch plus up to `1` later issue-title GET in the smoke-detector step.  
  **Proposed call count** — `1` early fetch only.  
  **Endpoint(s)** — GraphQL `repository.pullRequest(number){closingIssuesReferences(first:50){nodes{number title body}}}`; `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — `Collect PR metadata` already fetches linked issue `number`, `title`, and `body`, then renders that data into `LINKED_ISSUE_CONTEXT_FILE`:
  ```bash
  if gh_retry "${_linked_tmp}" api graphql \
    ...
    -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
  ...
  lines.append(f"Issue #{num}: {title}")
  if body:
      lines.append(body)
  ```
  Later, the smoke-detector step re-fetches the linked issue title from the API:
  ```bash
  if [ "$IS_SMOKE" = "false" ]; then
    ISSUE_NUM=$(echo "${PR_BODY}" | grep -oiPm1 '...' || true)
    if [ -n "${ISSUE_NUM:-}" ]; then
      ISSUE_TITLE=$(_safe_gh_jq "repos/${{ github.repository }}/issues/${ISSUE_NUM}" --jq '.title // ""' || echo "")
      if echo "${ISSUE_TITLE}" | grep -qi '\[E2E Smoke Test\]'; then
        IS_SMOKE=true
      fi
    fi
  fi
  ```
  The title data is already available earlier in the same job; only a keyed local lookup is missing.  
  **Proposed fix** — In `Collect PR metadata`, emit a structured cache such as `LINKED_ISSUE_TITLES_JSON='[{number,title}]'` or a temp JSON file keyed by issue number, then have `Detect smoke test and tune LLM settings` resolve `ISSUE_NUM` from that cache instead of calling `_safe_gh_jq`. If the cache is absent because the early fetch failed, preserve the current API fallback.  
  **Safety rationale** — Same job and no intervening mutation are visible, but the current smoke-detector selects one issue number via regex while the cached linked-issue data is built in an earlier step with both GraphQL and fallback paths, so keyed parity needs verification.  
  **Downstream signal** — Verify that, for both default-branch-linked PRs and non-default-base fallback PRs, the new local title cache contains the same `ISSUE_NUM` chosen by the smoke-detector regex and preserves the current fail-open behavior when the early linked-issue fetch fails.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:76-78`, `.github/workflows/implement.yml:543-548`  
  **Current call count** — `2`  
  **Proposed call count** — `1`  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — The workflow fetches the same issue once in `Precheck approval phase label` and again in `Fetch issue metadata`:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ISSUE_STATE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.state')"
  ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -c '.labels')"
  ```
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"

  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ISSUE_NUMBER_JSON="$(jq -r '.number' "${ISSUE_META_FILE}")"
  ISSUE_URL_JSON="$(jq -r '.html_url' "${ISSUE_META_FILE}")"
  ```
  No issue mutation occurs between these two reads; the second read is only widening the field set.  
  **Proposed fix** — Persist the full issue payload from the precheck into a temporary JSON file under `${RUNNER_TEMP}` (or move runtime-workspace creation earlier and write it directly to `ISSUE_META_FILE`), then let `Fetch issue metadata` reuse that file when present instead of re-calling the API; later reuse sites that already read `ISSUE_META_FILE` can stay unchanged.  
  **Safety rationale** — The overlap is obvious and local, but the first fetch is plain `gh api` while the second is `gh_retry gh api`, so consolidating them changes retry/error semantics unless intentionally preserved.  
  **Downstream signal** — Verify that no step between precheck and metadata consumption intentionally relies on a fresh post-precheck read, and decide whether the later `gh_retry` semantics must be preserved before replacing the second fetch with the cached precheck payload.

- **ID** — `REUSE-003`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:64-80`, `.github/workflows/orchestrate_clarify_respond.yml:402-413`  
  **Current call count** — `2` child-issue reads plus up to `2` tracking-issue reads (`4` total when a tracking parent exists).  
  **Proposed call count** — `1` child-issue read plus up to `1` tracking-issue read (`2` total max).  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}` for the child issue; `GET /repos/{owner}/{repo}/issues/{tracking_number}` for the tracking issue.  
  **Evidence** — The first step fetches the child issue and, when present, the tracking issue title:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.title // ""')"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ```
  Later, `Fetch issue and tracking context` fetches the same child issue again and then re-fetches the tracking issue body:
  ```bash
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_META}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_META}" | jq -r '.title // ""')"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  The second step is widening the field set, not querying a different resource.  
  **Proposed fix** — Introduce a shared temp-file cache for the child issue payload and, when `TRACKING_NUM` is present, fetch/store a full tracking issue payload once; then have both steps read from that cache. If the preferred cache write fails, keep the existing late `gh_retry` reads as fail-open fallbacks.  
  **Safety rationale** — Same job and no visible issue mutation are satisfied, but the existing early reads are unretied while the later reads use `gh_retry`, and moving/reusing them may affect early orchestrator-gating and alert-suppression behavior.  
  **Downstream signal** — Verify that `is_orchestrator` gating and smoke-parent alert suppression still behave correctly when both steps consume a shared cached issue/tracking payload instead of re-fetching them later with `gh_retry`.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `scripts/orchestrate_poll_process.sh:4748-4754`, `scripts/orchestrate_poll_process.sh:6398-6399`  
  **Current call count** — `0` live in-repo invocations; the dead helper contains `1` paginated comments fetch per hypothetical invocation.  
  **Proposed call count** — `0`  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_num}/comments?sort=created&direction=desc&per_page=100` with `--paginate`  
  **Evidence** — The helper performs a paginated API read:
  ```bash
  read_standalone_state_json() {
    local issue_num="$1"
    local comments_json
    if ! comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null)"; then
      comments_json='[]'
    fi
    _extract_standalone_state_json_from_comments "${comments_json}"
  }
  ```
  But the live standalone-state path in the same script reads from already-fetched `comments_json` via the pure parsing helpers instead:
  ```bash
  state_comment_id="$(_extract_standalone_state_comment_id_from_comments "${comments_json}")"
  state_json="$(_extract_standalone_state_json_from_comments "${comments_json}")"
  ```
  I also verified, via repository-wide symbol search excluding `analysis/`, that `read_standalone_state_json` has no in-repo call sites beyond its own definition.  
  **Proposed fix** — If no out-of-repo sourcing contract depends on it, remove `read_standalone_state_json()` entirely; if the wrapper is meant to remain public, convert one real caller to it and document the pagination/fail-open contract above the function so the API call stops being dead surface area.  
  **Safety rationale** — Static repo reading proves no in-repo consumers, but the function lives in a sourced shell script and external/manual consumers cannot be ruled out from repository contents alone.  
  **Downstream signal** — Run `rg -n --glob '!analysis/**' '\bread_standalone_state_json\b' .` and check any documented/manual sourcing paths outside the repo before deleting the helper; only remove it if that verification is clean.

### Cross-References to Deep Audit Section

- `API-001`: `NEEDS_VERIFICATION` — The batching opportunity is real, but it replaces per-issue REST hydration with GraphQL and needs parity checks for truncation, fail-open behavior, and downstream JSON shape.
- `API-002`: `NEEDS_VERIFICATION` — The later `send-merged-alert` body re-fetch should reuse earlier classification, but cross-step output wiring and orchestrator-skip semantics need to be verified before removal.
- `API-003`: `RISKY_SKIP` — The fallback path paginates and exists specifically to preserve fail-open timeline semantics, so auto-merging it would risk page-boundary behavior and fallback correctness.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | `MERGE-001` |
| NEEDS_VERIFICATION | 5 | `MERGE-002`, `REUSE-001`, `REUSE-002`, `REUSE-003`, `DEAD-API-001` |
| RISKY_SKIP | 0 | — |

### Implement-Stage Handoff

- `MERGE-001`
