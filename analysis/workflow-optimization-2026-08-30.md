## Executive Summary

- **Review/autofix is the dominant critical path.** Five detailed active runs averaged **6,393s (106.5 min)**; workflow-family p95 was **6,110.6s**. Every detailed active run had a `stall_guard`; circuit breaking was disabled. Enabling the existing breaker should save roughly **10–15 minutes per affected review**. **Confidence: high.**
- **The 5-minute poller is near saturation.** `orchestrate_poll` p50 was **279.5s (93% of cadence)** and p95 **525.75s**, with 6 cancellations. Run `33325127600` spent **37.6s** fetching all refs and **85.2s** writing two memory events. Target: **80–100s saved per ordinary poll**. **Confidence: high.**
- **Duplicate review launches produce extreme queueing.** Runs `33298304735`, `33298506770`, and `33300215575` waited **4,995–6,007s** before requesting a runner, then discovered merged PRs and performed no model work. **Confidence: high.**
- **AI cost is concentrated in review panels:** **84 OpenRouter calls and 27.08M total tokens**. Five detailed runs made **71 calls versus a 60-call two-pass baseline**, indicating **15.5% retry/failback overhead**. **Confidence: high.**
- **Cache reuse is useful but uneven.** Derived aggregate hit rate was **66.2%**; detailed DeepSeek usage reached **85.9%**, while Mistral reached only **0.65%** despite logging `prompt_reused=true`. **Confidence: medium** because provider accounting differs.
- **Operational success is high once skipped runs are excluded:** 236 successes, 1 failure, and 12 cancellations among 249 non-skipped runs—**94.8% successful**. The sole failure was security audit run `33301196549`. **Confidence: high.**

## Speed Optimizations

1. **Critical path: enable reviewer circuit breaking**
   - **Evidence:** Active review runs `33298314768`, `33298515312`, `33300999483`, `33304922307`, and `33305241958` all recorded `stall_guard`; Kimi was affected in four, Minimax in two. Three runs used failback models.
   - **Root cause:** `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`, so unhealthy slots continue receiving work across runs.
   - **Exact change:** Enable the existing breaker with threshold 3 and 30-minute TTL. Add `elapsed_ms`, `first_token_ms`, and `termination_reason` to `REVIEWER_SLOT_STATE`.
   - **Savings:** Target **10–15 minutes/run**, or **10–15%** of active review latency.
   - **Risk:** Low–medium; failback preserves panel coverage. Roll back by setting the flag to `0`.

2. **Critical path: remove poller fixed overhead**
   - **Evidence:** Run `33325127600`:
     - Full-ref checkout: **37.6s**
     - `poll_started` memory write: **43.1s**
     - `poll_completed` memory write: **42.1s**
     - Semble install/index phase: approximately **26s**, with no `SEMBLE_QUERY`.
   - **Root cause:** Every cycle fetches all branches/tags, independently clones/pushes memory twice, and eagerly prepares Semble.
   - **Exact change:**
     - Fetch default/state/integration refs explicitly instead of `+refs/heads/*` and all tags.
     - Reuse one `ai-memory` checkout across start/end events, or batch both events into the final push.
     - Initialize Semble lazily on first judge/conflict query.
   - **Savings:** **80–100s/poll**.
   - **Risk:** Medium for scoped ref fetching; low for clone reuse and lazy Semble.

3. **Critical-path control plane: deduplicate review successors**
   - **Evidence:** Wrapper runs waited **83–100 minutes** before runner allocation and then no-op’d because PRs `#3910`, `#3911`, and `#3913` were already merged. Runs `33300996427` and `33305238818` were cancelled before their first agent step after 5,766s and 6,099s.
   - **Root cause:** PR-event and explicit dispatch paths both launch the same `(PR, head SHA)` review. Continuation dispatch explicitly bypasses peer detection.
   - **Exact change:** Before every explicit dispatch, query queued/in-progress runs using `(PR, head_sha)` and skip only when a peer is confirmed to have `should_run=true`; remain fail-open on API errors.
   - **Savings:** Removes **83–100 minute reported waits** and roughly **2–4 minutes** of eventual no-op work.
   - **Risk:** Low if keyed by exact head SHA.

4. **Micro-optimization: suppress terminal projects earlier**
   - **Evidence:** Poller run `33325127600` spent 6.4s processing tracking issue `#3845` only to report “Project already complete.”
   - **Change:** Remove the active tracking label or exclude terminal state during discovery.
   - **Savings:** About **6s plus at least two API reads per poll** while such projects remain active.
   - **Risk:** Low.

5. **Diagnostic prerequisite: decompose CI**
   - **Evidence:** CI p50 was **1,223s**, p95 **1,292.5s**, but no CI deep-dive logs were collected.
   - **Change:** Emit step-level timing and cache-status summaries before changing CI behavior.
   - **Risk:** None; latency savings cannot yet be estimated safely.

## Cost Optimizations

1. **Eliminate reviewer retry/failback waste**
   - **Evidence:** Five detailed runs used 71 calls against a 60-call baseline; total window usage was **27.08M tokens**.
   - **Change:** Enable circuit breaking and emit per-attempt duration/token data.
   - **Estimated savings:** **8–15% of review calls**, potentially **2–4M total-token equivalents/window**.
   - **Quality risk:** Low because failed slots are replaced rather than removed.

2. **Make second-pass review selective**
   - **Evidence:** Six reviewers generally run twice. `REVIEWER_RISK_TIER_ENABLED=0`; risk logs also report `loc=0 files=0` despite gates observing 3–14 changed files.
   - **Change:** First fix LOC/file telemetry, then canary a three-model second pass for non-critical paths. Preserve six models for workflows, scripts, migrations, and memory code.
   - **Estimated savings:** Up to **25% of panel calls**, approximately **6–7M tokens/window** at current volume.
   - **Quality risk:** Medium; require disagreement and defect-escape monitoring.

3. **Fix model-specific cache effectiveness**
   - **Evidence from five detailed runs:**

     | Model | Calls | Derived cache hit |
     |---|---:|---:|
     | DeepSeek v4 Pro | 11 | 85.9% |
     | Kimi k3 | 15 | 68.7% |
     | Minimax m3 | 12 | 69.2% |
     | Qwen 3.7 Plus | 10 | 62.9% |
     | Grok 4.6 | 10 | 36.9% |
     | Mistral Small | 10 | 0.65% |

   - **Change:** Keep immutable policy/system text before dynamic PR data; emit an observed cache result after usage rather than `status=supported`.
   - **Estimated savings:** Potentially reclassifies hundreds of thousands of Mistral/Grok input tokens to discounted cache reads.
   - **Quality risk:** None. Dollar savings are unavailable because pricing telemetry was not collected.

4. **Semble appears cost-positive**
   - **Evidence:** 10 queries, **117,716 bytes**, zero fallbacks. Eight detailed calls were targeted reviewer, overflow-file, or conflict-resolver retrievals.
   - **Assessment:** Context volume—**11.8KB/query average**—is small relative to approximately 100K-token review prompts.
   - **Change:** Add `source_bytes_considered` and `bytes_avoided` to quantify prompt reduction. Avoid eager poller setup when no query occurs.

5. **Reasoning selection**
   - Reviewer reasoning defaults to `xhigh`; second-pass small diffs already use `high`. No observed judge execution supports changing judge reasoning.
   - Do not lower reasoning for critical workflow/script changes until risk-tier telemetry is corrected.

## Reliability Improvements

1. **Security audit path failure**
   - **Evidence:** Run `33301196549` failed in `Run security audit` with `captured_path_error=Error: No such file or directory`, but did not identify the missing path.
   - **Root cause category:** Configuration/path resolution.
   - **Fix:** Log sanitized stderr head/tail, executable resolution, all file paths passed to Codex, and path-valued `config.toml` entries. Emit:
     `SECURITY_AUDIT_FAILURE phase=codex-execution rc=... stderr_bytes=... missing_path=...`
   - **Impact:** Converts an opaque deterministic failure into an actionable one.
   - **Rollback/fail-open:** Logging-only; retain fail-closed audit behavior.

2. **Reviewer provider instability**
   - **Evidence:** `stall_guard` occurred in all five detailed active reviews; one DeepSeek `server_error`; three failback states.
   - **Fix:** Enable circuit breaking and retain fallback slots.
   - **Impact:** Lower timeout and rerun risk without reducing reviewer count.
   - **Rollback:** Environment flag.

3. **Unattributed poller API failure**
   - **Evidence:** Run `33325127600` emitted `gh: Not Found (HTTP 404)` while processing `#3898`, but no endpoint or operation.
   - **Fix:** Log sanitized method, endpoint class, attempt, status, elapsed time, and fail-open decision.
   - **Impact:** Separates expected optional-probe 404s from broken state lookups.

4. **Collector consistency**
   - **Evidence:** Supplied aggregate reports 115 telemetry runs with wall-clock p50 1,000ms; on-disk `summary.json` reports 30 with p50 41,000ms.
   - **Fix:** Emit collector source counts, dedup key counts, selected/deep-dive counts, and schema version at every aggregation stage.
   - **Impact:** Prevents contradictory SLO conclusions.

5. **Policy-pressure signals**
   - `BREAK_GLASS`: **0**
   - `CONTEXT_BUDGET_WARN`: **0**
   - No evidence of rubric pressure or context-window risk in the collected window.

## AI Memory Health

- **Retrievals:** 8/8 selected records—**100% hit rate**.
- **Budget:** Average **1,371/1,400 tokens (97.9%)**; all used `keyword_method=llm`.
- **Failures:** Zero zero-record, fail-open, or disabled retrievals.
- **Writes:** 18 deduplicated run events, 5 record candidates, and 6 `write_lessons_learned` operations; all lesson writes reported `count=0`.
- **Contention:** Four write operations required more than one push attempt; maximum was 3.
- **Assessment:** Availability is healthy, but retrieval is consistently saturated near the cap and relevance cannot be evaluated.
- **Recommendation:** Reuse a single memory clone per workflow and add `elapsed_ms`, clone/fetch/push timing, score distribution, records considered, and truncation count. No `promote`, `compact`, or `finalize-task` telemetry was observed.

## GH API Call Audit

Runtime API call counts were not emitted, so exact high-volume totals are **not auditable**.

- **Terminal project redundancy:** Issue `#3845` required comments and label reads before being skipped as complete. Remove it from active discovery.
- **Recurring sweep overhead:** Run `33325127600` spent approximately 10.2s on stall, merged-issue, conflict, and noop sweeps. Logs show zero merged issues and one PR scanned. Running these sweeps every third poll would reduce their API reads by roughly **67%**, with at most a 10-minute recovery delay.
- **Review sweep hygiene:** Runs `33322643747`, `33324021573`, and `33325437231` each found one candidate, recognized PR `#3915` had an active run, and dispatched zero—healthy deduplication.
- **404 observability:** The poller’s 404 lacked endpoint context.
- **Rate limits/retries:** No explicit runtime summaries were available.

Recommended wrapper output:

```text
GH_API_CALL method=GET target=issue-comments attempt=1 status=200 elapsed_ms=412 cached=false
GH_API_CALL method=GET target=workflow-probe attempt=1 status=404 elapsed_ms=190 fail_open=true
GH_API_SUMMARY calls=... retries=... cache_hits=... rate_limit_wait_ms=...
```

## Prompt Cache & Memory System

- Aggregate `cache_hit_rate` is null, but token counters imply **66.2%** cache reuse: 17.65M cached versus 9.00M uncached prompt tokens.
- Three explicitly reported run rates were **62.6%, 66.0%, and 69.4%**.
- Cache writes were reported as zero while reads were substantial; this likely reflects provider accounting rather than proof that no cache was created.
- `REVIEWER_CACHE status=supported prompt_reused=true` does not correspond to observed hits, especially for Mistral.
- Zero context-budget warnings indicate prompt growth did not cross the configured 70% threshold.
- Validate run `33321954636` hit its setup-uv cache but warned that configured save paths did not exist.

**Changes:**
1. Emit observed per-call cache ratios after usage.
2. Stabilize prompt prefixes and place PR SHA, timestamps, and diff data after cacheable policy text.
3. Add cache-key component hashes to diagnose fragmentation.
4. Guard dependency-cache save steps on directory existence.

## Orchestrator Health

- `orchestrate_poll`: 76 runs, 70 successes, 6 cancellations; p50 **279.5s**, p95 **525.75s** against a 300s cron.
- Run `33325127600` behaved safely: it deferred PR `#3915` because one required check was blocking and explicitly left retry budget unchanged.
- The same run found no conflicts or noop-suspicious recoveries, indicating clean fail-open sweep behavior.
- A hidden **56s pre-issue phase** occurred between process start and the first issue banner; likely API snapshot/setup work, but no stage timing exists.
- Semble availability changed from initialization defaults (`false`) to usable index state (`true`), while the supplied summary classified it as unavailable. Emit a final probe rather than interpreting early environment defaults.
- No clarification loops, judge cycles, conflict-heal retries, or terminalization loops were observed in deep logs.

Track:

```text
ORCH_STAGE_TIMING stage=pre_issue_snapshot elapsed_ms=... api_calls=...
ORCH_ISSUE_RESULT issue=3898 state=ready_to_merge action=defer reason=blocking_check elapsed_ms=...
SEMBLE_PROBE target=index result=ok elapsed_ms=...
```

## Pipeline Flow Bottlenecks

| Stage | Evidence | Bottleneck type |
|---|---|---|
| Clarify | 190/193 skipped | Trigger/control-plane noise |
| Plan | 186/190 skipped; successful outliers 371–464s | Compute when active |
| Implement | 186/189 skipped; successful outliers 658–799s | Model/compute |
| Review/autofix | Active detailed average 6,393s | Model stalls, retries, two-pass panel |
| CI | p50 1,223s, p95 1,292.5s | Unknown compute; logs missing |
| Validate | Run `33321954636`: 397s | Validation plus cache warnings |
| Orchestrate poll | p50 279.5s on 300s cadence | Fixed setup, API polling, memory writes |
| Promote/forward merge | 44s / 38s | Minor |

The four phase families generated **751 skipped runs out of 762 (98.6%)**. These are mostly one-second records, so they are a control-plane and observability issue rather than the primary compute cost. Add event and skip-reason fields to the collector before narrowing triggers.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** 100+ minute active review runs; 20-minute CI; near-continuous 5-minute poller.
- **Top failure modes:** Reviewer stall guards, duplicate queued review runs, opaque security-audit path failure, unattributed poller 404.
- **Highest cost driver:** Review/autofix—84 OpenRouter calls and 27.08M tokens.
- **Top actions:**
  1. Enable reviewer circuit breaking and per-attempt timing.
  2. Reuse memory checkout, scope poller fetches, and lazily initialize Semble.
  3. Deduplicate review dispatches by PR and exact head SHA.

## Metrics Appendix

### Overall

| Metric | Value |
|---|---:|
| Window | 2026-08-30 05:54–17:34 UTC |
| Total runs | 1,000 |
| Success | 236 |
| Failure | 1 |
| Cancelled | 12 |
| Skipped | 751 |
| Strict success rate | 23.6% |
| Non-skipped success rate | 94.8% |
| p50 / p95 duration | 2s / 359s |
| Average duration | 169.3s |

### Major workflow families

| Family | Runs | Success | Failure | Cancelled | Skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Review/autofix | 73 | 67 | 0 | 6 | 0 | 18s | 6,110.6s |
| Orchestrate poll | 76 | 70 | 0 | 6 | 0 | 279.5s | 525.75s |
| CI | 11 | 11 | 0 | 0 | 0 | 1,223s | 1,292.5s |
| Clarify | 193 | 3 | 0 | 0 | 190 | 1s | 11s |
| Plan | 190 | 4 | 0 | 0 | 186 | 1s | 10s |
| Implement | 189 | 3 | 0 | 0 | 186 | 1s | 10.6s |
| Security audit | 1 | 0 | 1 | 0 | 0 | 86s | 86s |
| Validate | 1 | 1 | 0 | 0 | 0 | 397s | 397s |

### AI and cache telemetry

| Metric | Value |
|---|---:|
| OpenRouter calls | 84 |
| Usage available/unavailable | 79 / 5 |
| Prompt tokens | 9,001,455 |
| Completion tokens | 429,097 |
| Cache-read tokens | 17,651,549 |
| Cache-write tokens | 0 |
| Total tokens | 27,075,004 |
| Derived cache hit rate | 66.2% |
| Collector `cache_hit_rate` | null |
| Break-glass count | 0 |
| Context-budget warnings | 0 |

### Wall-clock telemetry discrepancy

| Source | Samples | p50 | p99 |
|---|---:|---:|---:|
| On-disk `summary.json` | 30 | 41,000ms | 7,538,360ms |
| Supplied aggregate context | 115 | 1,000ms | 6,296,760ms |

### MCP telemetry

| System/target | Queries | Bytes | Fallbacks | Probe OK | Failed | Skipped |
|---|---:|---:|---:|---:|---:|---:|
| Semble, all targets | 10 | 117,716 | 0 | — | — | — |
| Serena, target not emitted | 0 | 0 | 0 | 0 | 0 | 0 |

Detailed Semble target coverage was available for 8/10 calls: five reviewer-context, two overflow-file, and one conflict-resolver query. No other MCP servers were observed. Serena probe coverage is insufficient to distinguish disabled from unavailable outside the review workflow.

## Deep Audit — Workflows & Scripts (2026-08-30)

### Section 1: Bug & Correctness Sweep

Audit scope: 46 workflows, 77 shell scripts, and 55 Python scripts. YAML lint, `bash -n`, and Python AST parsing passed.

#### SEC-001 — Fixed delimiter permits GitHub environment injection

- **File:** `.github/workflows/implement.yml:1380-1418`
- **Severity:** High
- **Category:** `security`
- **Description:** User-controlled issue body and title values are written to `$GITHUB_ENV` with the fixed delimiter `EOF`. An issue body containing a standalone `EOF` can terminate the value and inject subsequent environment assignments into privileged implementation steps. The trust-boundary comment at lines 1402-1404 incorrectly describes this as safe.
- **Recommended fix:** Keep issue content in `${ISSUE_BODY_FILE}`, or generate a collision-checked random delimiter. Reuse the `write_multiline_env` pattern from `.github/workflows/plan.yml:735-745`.

#### SEC-002 — Check-run fields are interpolated directly into shell source

- **File:** `.github/workflows/check_failure_triage.yml:110-145,288-295`; `.github/workflows/internal-check-failure-triage.yml:20-27`
- **Severity:** High
- **Category:** `security`
- **Description:** String inputs such as `check_name` and `pr_number` are inserted directly into `run:` source. GitHub expands expressions before Bash parses the script, so a check name containing command substitution syntax can execute commands. The internal wrapper forwards the external check-run name unchanged.
- **Recommended fix:** Bind every input through step `env`, reference only quoted shell variables, and validate `PR_NUMBER` with `^[0-9]+$` before constructing API paths.

#### SEC-003 — Release input is validated after unsafe interpolation

- **File:** `.github/workflows/mark-stable.yml:4-13,71-85`; `.github/workflows/test-and-mark-stable.yml:4-13,176-190`
- **Severity:** Medium
- **Category:** `security`
- **Description:** `version_tag` is a string input assigned through `INPUT_VERSION="${{ inputs.version_tag }}"`. Shell substitutions embedded in the input execute before the subsequent semantic-version regex runs. Exploitation requires access to dispatch or control a trusted caller. [NEEDS VERIFICATION]
- **Recommended fix:** Set `INPUT_VERSION` in the step’s `env:` mapping and retain the existing regex validation against `"${INPUT_VERSION}"`.

#### BUG-001 — Paginated plan data is not merged into one JSON array

- **File:** `.github/workflows/plan.yml:473-480,545-573,711-785,830-849`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** `gh api --paginate` writes one JSON array per page into `ISSUE_COMMENTS_FILE`. Scalar `jq` consumers then emit one result per page, making `LATEST_ANSWER_COMMENT_ID`, answer text, and question selection multiline on issues exceeding 100 comments. The timeline count at lines 546-554 has the same defect and can produce a non-numeric multiline value.
- **Recommended fix:** Merge pages with `--slurp` and `jq 'add // []'`, matching `.github/workflows/implement.yml:1501-1506`. Aggregate the timeline before computing its length.

#### BUG-002 — Review judge phase list has drifted from the label contract

- **File:** `scripts/review_rb_judge.sh:782-814,1628-1637,1702-1710,2302-2314`; `.github/ai/label_contract.v1.json:189-211`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** `_rps_phases` omits seven canonical issue phases: validation states, `ai:needs-human`, and `ai:blocked`. Judge transitions can therefore preserve an omitted phase alongside `ai:ready-to-merge` or `ai:closed`, violating phase exclusivity.
- **Recommended fix:** Resolve additions/removals from `label_contract.v1.json` through `scripts/ai_labels.py`, following `scripts/orchestrate_poll_process.sh:2429-2485`.

#### BUG-003 — Full-label replacement can lose concurrent labels

- **File:** `scripts/label_helpers.sh:166-216`; `scripts/review_rb_judge.sh:782-814`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** Both helpers read all labels, modify the snapshot, then replace the complete label set with `PUT /labels`. A concurrent label added between GET and PUT—such as `force-review` or an operational marker—is silently removed.
- **Recommended fix:** Mutate only contract-selected phase labels with one `gh issue edit` containing explicit `--remove-label` and `--add-label` arguments, as implemented in `scripts/validate_process.sh:1218-1252`.

### Section 2: GitHub API Call Redundancy Audit

#### BATCH-001 — Poller discards its batch result and re-fetches linked PRs per issue

- **File:** `scripts/orchestrate_poll_process.sh:10853-11019,14722-14799`
- **Severity:** High
- **Category:** `api-batching`
- **Description:** After `_fetch_candidate_issue_details_graphql` batches current-wave data, reconciliation still performs one timeline request per issue and one REST PR request per cross-reference candidate. Current additional calls are `N + P`; proposed additional calls are `0`, retaining only the existing `ceil(N/25)` batch calls.
- **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql` with `linked_pr_candidates[]` containing PR body, head, state, and merge fields, then perform implementation-PR selection locally. Preserve per-issue REST only for batch misses.

#### BATCH-002 — Consumer drift audit can issue 221 logical content requests

- **File:** `scripts/audit_consumer_drift.py:397-484`; `.github/workflows/audit_consumer_drift.yml:34-55`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Each of 13 consumers receives one directory request plus up to 16 individual workflow-content requests: up to 221 logical calls before retries. Proposed count is 13 calls—one aliased GraphQL query per repository.
- **Recommended fix:** Add `fetch_workflow_contents_batch(repository, filenames)` using aliased `object(expression: "HEAD:.github/workflows/<file>")` fields. Extend the alias/chunking pattern used by `_fetch_candidate_issue_details_graphql`.

#### API-001 — Default branch metadata is repeatedly re-fetched

- **File:** `scripts/orchestrate_poll_process.sh:2250-2256,3741-3754,8275-8290,13635-13670,14066-14069,17414-17417,17700-17705,18296-18310`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Nine call sites independently fetch `GET /repos/${GITHUB_REPOSITORY}` solely for `.default_branch`; several occur inside per-project paths. Current count is up to nine possible calls per process; proposed count is one.
- **Recommended fix:** Populate a validated `REPO_DEFAULT_BRANCH` once at startup and reuse it throughout the tick, following the poller’s existing cycle-local cache model.

#### API-002 — Review sweep duplicates status snapshots per workflow

- **File:** `.github/workflows/review_autofix_sweep.yml:123-224`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** Two workflows are queried separately for each of three statuses, producing six active-run requests plus one PR request per tick. Fetching each status repository-wide would reduce the active-run calls from 6 to 3 and total calls from 7 to 4.
- **Recommended fix:** Query repository Actions runs once per status and filter `.path` locally. Extend `_load_actions_runs_cached` from `scripts/orchestrate_poll_process.sh` to include `pending`.

#### API-003 — Editor-changes-lost performs two identical run-list requests

- **File:** `scripts/gh_helpers.sh:1175-1206,1300-1332`; `.github/workflows/review_autofix.yml:6294-6313`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** On the no-peer path, two back-to-back helpers query the same branch-scoped Actions-runs endpoint. Current count is 2; proposed count is 1.
- **Recommended fix:** Fetch one run snapshot and pass it to separate peer and retry-budget evaluators, preserving their distinct fail-open and fail-closed semantics.

#### API-004 — Clarify fetches the same comments twice when caching is enabled

- **File:** `.github/workflows/clarify.yml:455-482`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** The workflow fetches 50 comments for prompt context, then fetches the complete paginated thread for semantic caching. Current count is 2 logical requests; proposed count is 1.
- **Recommended fix:** Fetch and merge the full thread once, derive `ISSUE_COMMENTS_FILE` with `.[0:50]`, and render `THREAD_HISTORY_FILE` from the same snapshot.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Integration-ref bootstrap is copied across three workflows

- **File:** `.github/workflows/clarify.yml:61-130`; `.github/workflows/implement.yml:323-392`; `.github/workflows/orchestrate_clarify_respond.yml:115-184`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** These workflows contain byte-identical 3,583-character bootstrap blocks for cloning, authenticating, locating, and invoking `resolve_integration_ref.sh`.
- **Recommended fix:** Create a shared composite action backed by `scripts/resolve_integration_ref.sh`, with inputs `(issue_number, repository, source_ref, token)` and output `ref`. Replace all three inline blocks.

#### DUP-002 — ISO-8601 parser is duplicated four times

- **File:** `scripts/analyze_workflow_logs.py:40-52`; `scripts/collect_workflow_logs.py:93-105`; `scripts/cost_audit.py:283-295`; `scripts/workflow_retro.py:50-62`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** AST comparison found four identical `_parse_iso8601` implementations.
- **Recommended fix:** Add `scripts/time_utils.py::parse_iso8601_utc(value: str | None) -> datetime | None` and import it from all four callers.

#### DUP-003 — Prompt path-resolution functions are duplicated

- **File:** `scripts/assemble_prompt.sh:12-93`; `scripts/render_prompt.sh:12-41,95-159`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `resolve_prompt_file`, `resolve_render_prompt_py`, and `resolve_assembly_source_path` have identical implementations in both scripts.
- **Recommended fix:** Move them to `scripts/prompt_path_helpers.sh` as `prompt_resolve_file`, `prompt_resolve_renderer`, and `prompt_resolve_assembly_source`; source that module from both callers.

No workflow pair exceeded the requested 70% near-duplicate threshold by step-name structure.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging expression exceeds the medium-risk threshold

- **File:** `.github/workflows/implement.yml:851-1153`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `Stage workflow support files` block is approximately 16,339 characters, or 77.8% of the 21,000-character limit. Estimated headroom is 4,661 characters.
- **Recommended fix:** Move the block into `scripts/stage_workflow_support.sh` under an `implement` mode and leave only environment setup plus one script invocation in YAML.

No workflow exceeds 800 KB. The largest is `.github/workflows/review_autofix.yml` at approximately 453,485 characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Assigned state and telemetry variables are never consumed

- **File:** `scripts/orchestrate_poll_process.sh:6311-6391`; `scripts/review_issue_ledger.sh:862-918`; `scripts/review_run_reviewers.sh:753-761,3533-3548,4120-4128`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** Repository-wide reference analysis found assignments without reads, including `BRANCH_REBUILD_SKIP_REASON`, `BRANCH_REBUILD_LAST_REBUILD_AT`, `CURRENT_FLOOR`, `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`, and `REVIEWER_ATTEMPT_WD_REASON`.
- **Recommended fix:** Remove unused aliases/maps, or wire operationally useful values such as branch-rebuild skip reason and reviewer open-until time into existing structured summaries.

#### CONSIST-001 — Operator-facing repository variables are undocumented

- **File:** `.github/workflows/review_autofix.yml:1304,1349-1356`; `.github/workflows/workflow-log-analysis.yml:826-858`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** Repository variables including `JUDGE_INTERIM_*`, `BEHAVIOURAL_SMOKE_*`, `CONSOLIDATOR_REJECT_SCHEMA_ENABLED`, and `WORKFLOW_LOG_SUMMARY_*` have runtime defaults but no entries in `README.md`, `agents.md`, or `CLAUDE.md`.
- **Recommended fix:** Add variable-table entries documenting defaults, accepted values, consumers, failure behavior, and rollout status. If they are intentionally internal, replace `vars.*` exposure with constants.

No genuine TODO/FIXME/HACK markers were found. Confirmed ShellCheck warnings are represented by the dead-code and correctness findings above; remaining warnings were intentional dynamic exports, glob comparisons, output-variable assignments, or source indirection.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | SEC-001, SEC-002, BATCH-001 |
| Medium | 8 | SEC-003, BUG-001, BUG-002, BUG-003, BATCH-002, API-001, DUP-001, EXPR-001 |
| Low | 7 | API-002, API-003, API-004, DUP-002, DUP-003, DEAD-001, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 6 | Large |
| Code modularization | 12 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 9 | Medium |
