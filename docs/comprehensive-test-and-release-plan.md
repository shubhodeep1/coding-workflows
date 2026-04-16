# Plan: Comprehensive Pre-Release Testing Workflow

## Architecture Overview

```
comprehensive-test-and-release.yml (new)
  │
  ├─ Phase 1: Trigger test-and-mark-stable (dry_run: true)
  │    └─ Poll for completion
  │    └─ FAIL → admin alert, exit
  │
  ├─ Phase 2: Trigger workflow-log-analysis (dispatch)
  │    └─ Poll for completion
  │    └─ Save collection timestamp
  │
  ├─ Phase 3: Trigger internal-orchestrate with report link
  │    └─ Exit workflow
  │
  └─ [CALLBACK from orchestrate_poll]
       │
       ├─ Phase 4: Orchestrator project completes
       │    └─ ai:complete → dispatch test-and-mark-stable (dry_run: false)
       │    └─ ai:failed → admin alert, no release
       │
       └─ Phase 5: test-and-mark-stable runs for real
            └─ SUCCESS → stable released
            └─ FAIL → admin alert
```

## Files Changed / Created

### 1. NEW: `.github/workflows/comprehensive-test-and-release.yml`

The main orchestrating workflow. `workflow_dispatch` only. Contains:

- **Job 1: `first-pass-test`** — Dispatches `test-and-mark-stable.yml` with `dry_run: true`
  via `gh workflow run`, then polls the dispatched run for completion (using the same
  `gh_api_safe` + inactivity-timeout pattern from test-and-mark-stable). On failure →
  Telegram alert + exit.

- **Job 2: `collect-and-analyze-logs`** (needs: first-pass-test) — Dispatches
  `workflow-log-analysis.yml` with `BATCH_API_DISABLED=true` (forced sync). Passes
  `--since` from the last saved timestamp (read from
  `analysis/last_collection_timestamp.txt` on main, or fallback to 7 days). Polls for the
  workflow run to complete. On success, saves the current timestamp to
  `analysis/last_collection_timestamp.txt` and commits it.

- **Job 3: `dispatch-orchestrator`** (needs: collect-and-analyze-logs) — Reads the latest
  analysis report path from the `analysis/` directory. Dispatches `internal-orchestrate.yml`
  with a `project_description` that includes a link to the report in the repo and
  instructions to implement the recommendations. Tags the resulting tracking issue with a
  marker label `ai:comprehensive-test-pending` so the poller can identify it for the
  callback. Sends a Telegram notification. Exits.

#### Inputs

```yaml
inputs:
  version_tag:
    description: Semantic version tag (vX.Y.Z). Leave blank to auto-increment.
    required: false
    type: string
    default: ""
  test_repo:
    description: Repository to run E2E smoke test on (owner/repo).
    required: false
    type: string
    default: ""
  phase_timeout:
    description: Max inactivity minutes per polling phase.
    required: false
    type: number
    default: 30
  lookback_days_fallback:
    description: Fallback lookback days if no saved timestamp exists.
    required: false
    type: number
    default: 7
```

### 2. MODIFY: `scripts/collect_workflow_logs.py`

Changes:

- **Expand `CORE_WORKFLOW_FAMILIES`** to include all workflows:

  ```python
  CORE_WORKFLOW_FAMILIES = (
      "clarify", "plan", "implement", "review_autofix",
      "orchestrate", "orchestrate_poll", "orchestrate_clarify_respond",
      "validate", "workflow_log_analysis", "memory_maintenance",
      "test_and_mark_stable", "mark_stable", "issue_pr_status",
      "update_workflows", "cancel_on_pr_close", "ci",
  )
  ```

- **Remove the exclusion logic** in `normalize_workflow_family()` that skips
  orchestrate/validate/clarify_respond.

- **Add classification rules** for the new families (matching on workflow name/path
  patterns).

- **Add `--log-output-dir` argument** — when set, dumps full log archives to a folder
  structure instead of (or in addition to) embedding excerpts in JSON.

- **Folder structure:**

  ```
  {log-output-dir}/
    summary.json              # All run metadata + computed stats
    errors/
      {repo_slug}/{family}/{run_id}/
        metadata.json         # Run info, failure_point, duration
        step-{N}-{name}.log  # Full step logs (no char limit)
    slow/
      ...                     # Same structure, runs above p95
    recent/
      ...                     # Latest N runs
  ```

- **Only download full logs for error + slow + recent categories** (Q13: A). Other runs
  get metadata only in `summary.json`.

- **Remove `LOG_EXCERPT_MAX_CHARS` usage** when `--log-output-dir` is set (full logs dumped
  to disk).

- **Keep the existing JSON output path** (`--output`) working for backward compatibility —
  it still uses the char limit for the embedded excerpts.

### 3. MODIFY: `scripts/analyze_workflow_logs.py`

Changes:

- **Add `--codex-mode` flag** (default: `false`). When true:
  - Skips the OpenRouter direct API call.
  - Writes the preprocessed `analysis_context.json` (stats, categorization, NO truncation)
    to the log output dir.
  - Exits with code 0, printing the analysis context path.
  - The calling workflow then invokes Codex CLI against the folder.

- **Remove the 24k token budget default** — change `--max-prompt-tokens` default to `0`
  (0 = no truncation). When `--codex-mode` is set, truncation is skipped entirely
  regardless.

- **Keep the direct API path** working for backward compatibility (manual
  `workflow_dispatch` without `--codex-mode` still works, but now with no truncation by
  default).

### 4. MODIFY: `prompts/mode-workflow-analysis.txt`

Rewrite to be comprehensive. The prompt becomes the Codex CLI instructions. Key sections:

```text
You are a workflow optimization analyst for an AI-powered GitHub Actions pipeline.

You have access to a structured log directory. Start by reading summary.json
for the big picture, then drill into errors/, slow/, and recent/ folders.

Your task is to produce a comprehensive optimization report.

## Analysis Dimensions

Analyze ALL of the following:

### 1. Speed Optimizations
- Identify slow workflows, jobs, and steps
- Find unnecessary sequential operations that could be parallelized
- Detect redundant setup steps (checkout, install, cache misses)

### 2. Cost Optimizations (Token Usage)
- Token usage breakdown: cache_creation vs cache_read vs prompt tokens
- Identify phases using unnecessarily high thinking levels
- Find avoidable token waste (repeated context, redundant searches)
- Model selection efficiency (are expensive models used where cheaper ones suffice?)

### 3. Reliability Improvements
- Failure patterns and root causes
- Flaky steps that need retry logic
- Timeout tuning opportunities

### 4. GitHub API Call Efficiency
- Identify workflows making excessive or redundant API calls
- Look for per-item REST calls that could be batched GraphQL
- Rate-limit incidents and their impact
- Calls inside loops that should use cycle-local caches

### 5. MCP Tool Call Patterns
- Redundant MCP tool invocations
- Failed MCP calls and their fallback behavior
- Tool call chains that could be simplified

### 6. Serena Efficiency
- Unnecessary full-file reads (should use symbol lookups)
- Missed find_symbol / find_referencing_symbols opportunities
- Onboarding triggered unnecessarily during non-exploration phases
- replace_symbol_body vs full-file rewrites

### 7. Prompt Cache Effectiveness
- Cache hit/miss rates across phases
- Prompt assembly patterns that break cache (dynamic prefix)
- cache_creation_input_tokens vs cache_read_input_tokens ratios

### 8. Memory System Effectiveness
- Memory retrieval adding value vs noise
- Candidate record quality
- Lineage tracking completeness

### 9. Orchestrator Health
- Judge cycle counts and outcomes
- Stall recovery frequency and success rates
- Merge conflict rates on integration branches
- Wave completion times

### 10. Pipeline Flow Bottlenecks
- Phase-level timing (clarify → plan → implement → review)
- Phases that are systemic bottlenecks across repos
- Auto-approval vs manual approval wait times

## Output Format

Use these section headings exactly, in this exact order:
- ## Executive Summary
- ## Speed Optimizations
- ## Cost Optimizations
- ## Reliability Improvements
- ## GH API Call Audit
- ## MCP & Serena Efficiency
- ## Prompt Cache & Memory System
- ## Orchestrator Health
- ## Pipeline Flow Bottlenecks
- ## Per-Repo Breakdown
- ## Metrics Appendix

## Section Requirements

### Executive Summary
- Provide 3-5 bullets with the highest-impact findings.
- Include estimated impact in each bullet when possible.

### Speed Optimizations
- Rank recommendations by estimated time savings.
- For each: evidence, root cause, exact change, estimated impact.

### Cost Optimizations
- Rank by estimated token and/or dollar savings.
- For each: evidence, root cause, exact change, estimated impact.
- Include current vs. proposed prompt/model/thinking-level behavior.
- Call out avoidable token waste patterns.

### Reliability Improvements
- Rank by expected failure-rate reduction.
- For each: evidence (failure pattern), root cause, exact change, expected impact.

### GH API Call Audit
- Flag workflows making excessive or redundant API calls.
- Identify per-item REST calls that could be batched via GraphQL.
- Report rate-limit incidents with timestamps and affected workflows.
- Identify API calls inside loops that should use cycle-local caches.

### MCP & Serena Efficiency
- Identify redundant MCP tool invocations (same call repeated, failed retries).
- Flag full-file reads where symbol lookups (find_symbol, get_symbols_overview)
  would suffice.
- Detect onboarding triggered during non-exploration phases.
- Recommend replace_symbol_body over full-file rewrites where applicable.

### Prompt Cache & Memory System
- Report cache hit/miss rates per phase.
- Identify prompt assembly patterns that break caching.
- Report cache_creation_input_tokens vs cache_read_input_tokens ratios.
- Assess memory retrieval quality (value added vs context noise).

### Orchestrator Health
- Judge cycle counts and verdict distribution.
- Stall recovery frequency, trigger counts, and success rates.
- Merge conflict rates on integration branches.
- Wave completion times and bottleneck issues.

### Pipeline Flow Bottlenecks
- Phase-level timing across the pipeline.
- Identify systemic bottleneck phases across consumer repos.
- Auto-approval vs manual approval wait time analysis.

### Per-Repo Breakdown
- Add a subsection per repository with repo-specific findings and targeted actions.

### Metrics Appendix
- Include concise summary tables for key metrics used in the analysis.

## Rules

- Be specific: cite repo names, run IDs, workflow/job/step names, and measured values.
- Be actionable: every finding must include a concrete recommendation with code blocks.
- Estimate impact using percentages, absolute time/token deltas, or failure-rate deltas.
- Maintain backward compatibility. Do not recommend breaking changes.
- Do not recommend adding external services or new infrastructure.
- Do not invent missing data. If data is insufficient, state the gap.
- Avoid generic advice; tie each recommendation to observed metrics or logs.
- Do not include secrets, credentials, or sensitive PII in the report.
- In cost recommendations, explicitly identify where lower-cost models or lower thinking
  levels can be used without sacrificing required quality.
- Read files from the log directory selectively — start with summary.json, then drill into
  the most important logs (errors first, then slow, then recent).
```

### 5. MODIFY: `.github/workflows/workflow-log-analysis.yml`

Changes:

- **Remove the `schedule` trigger** (cron). Keep `workflow_dispatch` only.

- **Add new inputs:**
  - `since` (string, optional) — ISO-8601 timestamp for `--since` filtering.
  - `codex_mode` (boolean, default: `true`) — use Codex CLI for analysis.
  - `batch_api_disabled` (string, default: `'true'`) — force sync when not using codex
    mode.

- **Add Codex CLI installation step** (same pattern as implement.yml: npm install,
  config.toml setup with `web_search = "live"`, model provider = openrouter).

- **Add Codex CLI analysis step** (when `codex_mode: true`):

  ```bash
  cat "${ANALYSIS_PROMPT_FILE}" | codex exec \
    --model "${MODEL_EDITOR}" \
    --full-auto \
    > "${CODEX_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2)
  ```

  - Model: `WORKFLOW_LOG_ANALYSIS_MODEL || WORKFLOW_EDITOR_MODEL || 'openai/gpt-5.3-codex'`
  - Reasoning effort: `medium`
  - `web_search = "live"` in config.toml

### 6. MODIFY: `scripts/orchestrate_poll_process.sh`

Changes:

- **Add callback detection** in the project completion handler. When a tracking issue has
  the label `ai:comprehensive-test-pending`:

  - On `ai:complete` verdict: dispatch `test-and-mark-stable.yml` with `dry_run: false`
    and the version tag from the tracking issue metadata. Send Telegram SUCCESS alert.

  - On `ai:failed` verdict: send Telegram CRITICAL alert ("Comprehensive test pipeline
    aborted — orchestrator failed"). Do NOT dispatch test-and-mark-stable.

  - Remove the `ai:comprehensive-test-pending` label after handling.

### 7. NEW: `analysis/last_collection_timestamp.txt`

A single-line file committed to the repo containing the ISO-8601 timestamp of the last
successful log collection run start. Read by the comprehensive test workflow to determine
the `--since` value for the collector.

## GH API Call Budget

The comprehensive test workflow polls external workflow runs. API call budget:

| Phase | Calls per poll cycle | Frequency | Notes |
|---|---|---|---|
| Poll test-and-mark-stable | 1 (run status) | Every 30s | Single run ID lookup |
| Poll workflow-log-analysis | 1 (run status) | Every 30s | Single run ID lookup |
| Find latest report | 0 | Once | `git log` on local checkout |
| Dispatch orchestrator | 1 | Once | `workflow_dispatch` |
| Tag tracking issue | 1 | Once | Add label |

**Total for the workflow: ~2 API calls per poll cycle + 2 one-shots.** The heavy lifting
(E2E test polling, orchestrator polling) happens in the existing workflows, not duplicated
here.

For the orchestrate_poll callback, the label check is piggy-backed on the existing issue
label fetch that the poller already does — **0 additional API calls**.

## What Stays Unchanged

- `test-and-mark-stable.yml` — used as-is (invoked with different `dry_run` values)
- `mark-stable.yml` — untouched
- `orchestrate.yml` — untouched
- `orchestrate_poll.yml` — untouched (changes are in the shell script it calls)
- All consumer workflow templates — untouched
- Existing `--output` JSON path in collector — backward compatible

## Env Vars

### New

| Variable | Default | Used By | Description |
|---|---|---|---|
| `WORKFLOW_LOG_ANALYSIS_MODEL` | falls back to `WORKFLOW_EDITOR_MODEL` → `openai/gpt-5.3-codex` | workflow-log-analysis | Model for Codex CLI log analysis |

No new secrets required.

### Existing (referenced)

| Variable | Default | Used By |
|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` | Fallback for analysis model |
| `TG_BOT_SECRET` | — | Telegram alerts |
| `TG_ADMIN_CHAT_ID` | — | Telegram alerts |
| `ALERT_MSG_LEVEL` | `DEBUG` | Alert severity filter |
