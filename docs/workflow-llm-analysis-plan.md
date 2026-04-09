# Workflow Log Analysis via LLM — Implementation Plan

**Status:** Draft
**Date:** 2026-04-09

---

## Goal

Build an on-demand workflow that collects GitHub Actions run logs from this repo
and all consumer repos, sends them to an LLM for analysis, and commits a
markdown report to `analysis/` with actionable speed, cost, and reliability
optimization suggestions.

No new external services. Uses existing `GH_PAT` + `OPENROUTER_API_KEY` +
`WORKFLOW_EDITOR_MODEL`.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  workflow_dispatch (manual trigger)                      │
│  Inputs: lookback_days, repos_override, phases_filter   │
└───────────────┬─────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────┐
│  Job 1: collect-logs                                    │
│  ─────────────────                                      │
│  • Read consumer_repos.json + this repo                 │
│  • For each repo, call GitHub API:                      │
│    GET /repos/{owner}/{repo}/actions/runs               │
│      ?created=>=YYYY-MM-DD&status=completed             │
│    → for each run:                                      │
│      GET /repos/{owner}/{repo}/actions/runs/{id}/logs   │
│      (returns zip of step-level logs)                   │
│  • Extract & parse each run into structured JSON:       │
│    {repo, workflow, run_id, conclusion, duration_s,     │
│     phase, steps: [{name, duration_s, conclusion}],     │
│     token_usage, model, thinking_level,                 │
│     serena_tool_calls, file_fallback_ops,               │
│     autofix_iterations, stall_recoveries}               │
│  • Write combined metrics to /tmp/run_metrics.json      │
│  • Write raw log excerpts to /tmp/run_logs/             │
│    (truncated to ~4KB per run to stay within LLM        │
│     context — keep full stderr + last 200 lines stdout) │
│  • Upload as artifact: workflow-analysis-data            │
└───────────────┬─────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────┐
│  Job 2: analyze                                         │
│  ─────────────────                                      │
│  • Download artifact: workflow-analysis-data             │
│  • Build LLM prompt (see §Prompt Design below)          │
│  • Call OpenRouter chat/completions API:                 │
│    model = WORKFLOW_EDITOR_MODEL (or override)           │
│    reasoning_effort = high                               │
│  • Parse LLM response → markdown report                 │
│  • Commit report to analysis/workflow-optimization-      │
│    YYYY-MM-DD.md on current branch                      │
│  • Push branch (non-destructive — new commit only)      │
└─────────────────────────────────────────────────────────┘
```

---

## Deliverables

| # | File | Purpose |
|---|------|---------|
| 1 | `.github/workflows/workflow-log-analysis.yml` | On-demand reusable workflow |
| 2 | `scripts/collect_workflow_logs.py` | Log collection + metrics extraction |
| 3 | `scripts/analyze_workflow_logs.py` | LLM prompt assembly + API call + report generation |
| 4 | `prompts/mode-workflow-analysis.txt` | LLM system prompt for analysis |
| 5 | `analysis/workflow-optimization-YYYY-MM-DD.md` | Output (one per run, git-committed) |

---

## Detailed Design

### 1. Workflow: `.github/workflows/workflow-log-analysis.yml`

```yaml
name: Workflow Log Analysis

on:
  workflow_dispatch:
    inputs:
      lookback_days:
        description: >
          Number of days of workflow run history to analyze.
          Higher = more data but larger context. Max 30.
        required: false
        type: string
        default: "7"
      repos_override:
        description: >
          Comma-separated list of owner/repo to analyze instead of
          consumer_repos.json + this repo. Leave blank for default.
        required: false
        type: string
        default: ""
      phases_filter:
        description: >
          Comma-separated phase names to include (clarify,plan,implement,
          review,validate,orchestrate). Leave blank for all.
        required: false
        type: string
        default: ""

permissions:
  contents: write

jobs:
  collect-logs:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - Checkout this repo
      - Setup Python 3.12
      - Run scripts/collect_workflow_logs.py
        (env: GH_TOKEN, inputs passed as args)
      - Upload artifact: workflow-analysis-data

  analyze:
    needs: collect-logs
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - Checkout this repo
      - Download artifact: workflow-analysis-data
      - Setup Python 3.12
      - Run scripts/analyze_workflow_logs.py
        (env: OPENROUTER_API_KEY, model from vars)
      - Commit + push analysis/workflow-optimization-YYYY-MM-DD.md
      - Send Telegram notification with report link
```

**Key decisions:**

- `workflow_dispatch` only — no schedule. User triggers when they want analysis.
- Two jobs so collection and analysis are independently retriable.
- `timeout-minutes: 30` for collection (GitHub log download API can be slow for
  many runs); `15` for analysis (single LLM call).
- Commits directly to the branch the workflow was dispatched from (typically
  `main`). Uses `GH_PAT` for push so it can write to protected branches if
  configured.
- Sends a Telegram alert with a direct link to the committed report file on
  GitHub after a successful analysis run.

---

### 2. Script: `scripts/collect_workflow_logs.py`

**Responsibilities:**
1. Determine target repos: parse `consumer_repos.json` + add
   `$GITHUB_REPOSITORY` (this repo). Override with `--repos` arg if provided.
2. For each repo, list completed workflow runs in the lookback window via
   `GET /repos/{owner}/{repo}/actions/runs`.
3. Map each run to a pipeline phase by matching workflow name:
   - `AI Clarify` / `clarify` → `clarify`
   - `AI Plan` / `plan` → `plan`
   - `AI Implement` / `implement` → `implement`
   - `AI Review` / `review_autofix` → `review`
   - `AI Validate` / `validate` → `validate`
   - `AI Orchestrate*` / `orchestrate*` → `orchestrate`
   - Everything else → `other`
4. For each run, fetch step timing via
   `GET /repos/{owner}/{repo}/actions/runs/{id}/jobs` (no need to download full
   zip logs for structured metrics — the jobs API returns step-level
   `started_at`/`completed_at` and `conclusion`).
5. For the **top N slowest** and **top N failed** runs (configurable, default
   N=10), also download the full log zip via
   `GET /repos/{owner}/{repo}/actions/runs/{id}/logs` and extract the last
   200 lines + any stderr for deeper LLM analysis.
6. Extract embedded metrics from step logs where available:
   - Token usage: grep for `tokens` patterns already emitted by workflows
   - Model used: grep for `MODEL_EDITOR=` / `model =` in config steps
   - Serena efficiency: grep for the efficiency report table
   - Autofix iteration count: count `[ai-autofix]` / `[judge-fix]` in job names
7. Output:
   - `/tmp/analysis-data/run_metrics.json` — array of structured run records
   - `/tmp/analysis-data/run_logs/` — truncated logs for deep-dive runs
   - `/tmp/analysis-data/summary_stats.json` — pre-computed aggregates:
     - Per-phase: count, avg/p50/p95 duration, failure rate, avg tokens
     - Per-repo: same breakdown
     - Overall: total runs, total estimated cost, failure rate

**Implementation notes:**

- Use `requests` or `urllib3` (available in runner base image) for GitHub API
  calls. Alternatively use `subprocess` + `gh api` to leverage the pre-authed
  `gh` CLI.
- Respect GitHub API rate limits: 5000 req/hr for authenticated requests. With
  ~50 runs × 2 API calls each = ~100 requests per repo, this is well within
  limits even for 10+ repos.
- GitHub's log download endpoint returns a 302 redirect to a zip URL. The zip
  contains one file per job, with step-level log lines timestamped.
- The jobs API (`/actions/runs/{id}/jobs`) is cheaper and returns structured
  step timing without downloading zips. Use this as the primary data source;
  reserve zip downloads for deep-dive logs only.

**CLI interface:**

```
python3 scripts/collect_workflow_logs.py \
  --lookback-days 7 \
  --repos "owner/repo1,owner/repo2"  # optional override \
  --phases "plan,implement,review"    # optional filter \
  --output-dir /tmp/analysis-data \
  --deep-dive-count 10
```

---

### 3. Script: `scripts/analyze_workflow_logs.py`

**Responsibilities:**
1. Load `/tmp/analysis-data/run_metrics.json` and `summary_stats.json`.
2. Load any deep-dive log excerpts from `/tmp/analysis-data/run_logs/`.
3. Read the analysis prompt template from `prompts/mode-workflow-analysis.txt`.
4. Assemble the full LLM prompt:
   - System prompt (from template)
   - Summary statistics (compact JSON or table)
   - Per-phase breakdown tables
   - Deep-dive log excerpts for slowest/failed runs
   - The existing `analysis/plan-workflow-log-analysis.md` as a reference for
     the kind of analysis and recommendations expected
5. Call OpenRouter `POST /api/v1/chat/completions`:
   - Model: `WORKFLOW_EDITOR_MODEL` env var (default `openai/gpt-5.3-codex`)
   - `reasoning_effort`: `high`
   - Temperature: 0 (deterministic analysis)
   - Max tokens: 16000 (analysis reports are detailed)
6. Parse response, write to `analysis/workflow-optimization-YYYY-MM-DD.md`.
7. If a file with today's date already exists, append a run counter suffix:
   `analysis/workflow-optimization-YYYY-MM-DD-2.md`.

**Context budget management:**

GitHub Actions logs can be enormous. The script must respect the model's context
window. Strategy:

- `summary_stats.json` is always included (small — ~2KB).
- `run_metrics.json` is included as a compact table, not raw JSON. Estimated
  ~50 bytes per run × 200 runs = ~10KB.
- Deep-dive logs are capped at ~4KB per run × 10 runs = ~40KB.
- System prompt + reference analysis ≈ ~8KB.
- Total prompt ≈ ~60KB (~15K tokens) — well within any model's context.

If the total exceeds a configurable threshold (default 100K tokens), the script
truncates deep-dive logs first, then reduces the number of deep-dive runs.

**CLI interface:**

```
python3 scripts/analyze_workflow_logs.py \
  --data-dir /tmp/analysis-data \
  --output analysis/workflow-optimization-2026-04-09.md \
  --model "openai/gpt-5.3-codex" \
  --max-prompt-tokens 100000
```

---

### 4. Prompt: `prompts/mode-workflow-analysis.txt`

The LLM system prompt instructs the model to act as a GitHub Actions and LLM
pipeline optimization expert. Key sections:

```
You are a workflow optimization analyst for an AI-powered GitHub Actions
pipeline. You will receive structured metrics and log excerpts from recent
workflow runs across multiple repositories.

Your task is to produce an actionable optimization report covering three areas:

## 1. Speed
- Identify slowest phases and steps
- Find unnecessary sequential work that could be parallelized
- Spot redundant API calls, cache misses, or setup overhead
- Compare step durations across repos to find outliers

## 2. Cost (Token Usage)
- Identify phases with excessive token consumption
- Find patterns of wasted tokens (full file dumps, redundant searches,
  unnecessary Serena onboarding, repeated context)
- Suggest thinking_level downgrades where high reasoning is not needed
- Suggest model swaps for phases that don't need frontier models
- Estimate potential savings as percentage of current usage

## 3. Reliability
- Calculate failure rates per phase and per repo
- Identify common failure patterns (timeouts, stalls, autofix loops,
  rate limits, merge conflicts)
- Find stall-prone phases and suggest threshold adjustments
- Identify flaky steps vs. systemic issues

## Output Format

Use this structure for your report:

### Executive Summary
(3-5 bullet points with the highest-impact findings)

### Speed Optimizations
(Ranked by estimated time savings. Include specific workflow/step names.)

### Cost Optimizations
(Ranked by estimated token/dollar savings. Include current vs. suggested
thinking levels, model choices, and prompt changes.)

### Reliability Improvements
(Ranked by impact on failure rate. Include specific failure patterns and
suggested fixes.)

### Per-Repo Breakdown
(One subsection per repo with repo-specific observations.)

### Metrics Appendix
(Summary statistics tables for reference.)

## Rules
- Be specific: cite run IDs, step names, and durations.
- Be actionable: every finding must have a concrete recommendation.
- Estimate impact: use percentages or time savings where possible.
- Reference the codebase: suggest specific file/variable changes.
- Do NOT suggest adding external services or infrastructure.
- Do NOT suggest changes that would break backward compatibility.
```

---

### 5. Telegram Notification

After the report is committed and pushed, the analyze job sends a Telegram
message to `TG_ADMIN_CHAT_ID` with a link to the new report file on GitHub.

**Integration pattern:** Uses the existing `tg_helpers.sh` (already fetched by
the workflow). Since the analysis workflow has no associated issue, use the
simple `tg_send_msg` function (fire-and-forget, no issue-based tracking needed).

**Workflow step (end of analyze job):**

```yaml
- name: Notify via Telegram
  if: env.REPORT_FILE != ''
  env:
    TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}
    TG_ADMIN_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID }}
  run: |
    set -euo pipefail
    source scripts/tg_helpers.sh

    REPORT_NAME="$(basename "${REPORT_FILE}")"
    REPORT_URL="https://github.com/${{ github.repository }}/blob/${{ github.ref_name }}/analysis/${REPORT_NAME}"

    # Build a compact summary from the executive summary section
    EXEC_SUMMARY=""
    if [ -f "${REPORT_FILE}" ]; then
      EXEC_SUMMARY=$(sed -n '/^## Executive Summary/,/^## /{ /^## /d; p; }' \
        "${REPORT_FILE}" | head -10 | sed 's/^[[:space:]]*//')
    fi

    MSG="📊 Workflow Optimization Report
${REPORT_NAME}

${EXEC_SUMMARY}

📄 ${REPORT_URL}"

    tg_send_msg "${MSG}"
```

**Behavior when Telegram is not configured:**

`tg_send_msg` degrades gracefully — if `TG_BOT_SECRET` or `TG_ADMIN_CHAT_ID`
are unset, the function returns immediately without error. No workflow failure.

**Message content:**

- Report filename (includes date for quick identification)
- First ~10 lines of the Executive Summary section (key findings at a glance)
- Direct GitHub link to the full report

**Required secrets/variables (all existing, all optional):**

| Name | Type | Description |
|------|------|-------------|
| `TG_BOT_SECRET` | Secret | Telegram bot token |
| `TG_ADMIN_CHAT_ID` | Variable | Telegram chat ID for notifications |

---

### 6. Metrics Extraction — What We Parse From Logs

| Metric | Source | Extraction Method |
|--------|--------|-------------------|
| Run duration | Jobs API | `completed_at - started_at` on job |
| Step durations | Jobs API | `completed_at - started_at` per step |
| Run conclusion | Runs API | `conclusion` field (success/failure/cancelled) |
| Phase | Runs API | Map `workflow.name` → phase (see §2 above) |
| Token usage | Log text | Grep for `total_tokens`, `prompt_tokens`, `completion_tokens` patterns |
| Model used | Log text | Grep for `MODEL_EDITOR=`, `model =`, `"model":` in codex config steps |
| Thinking level | Log text | Grep for `MODEL_REASONING_EFFORT=`, `reasoning_effort` |
| Serena efficiency | Log text | Grep for `Serena efficiency` table (already emitted by `serena_efficiency_report.py`) |
| Autofix iterations | Jobs API | Count jobs/runs with `[ai-autofix]` in name or triggered by `workflow_dispatch` on same PR |
| Stall recoveries | Log text | Grep for `stall.*recover`, `STALL_DETECTED` |
| Failure category | Log text + conclusion | Classify: timeout, rate_limit, merge_conflict, codex_error, label_error, other |

**Note on token usage:** Current workflows don't consistently emit token counts
in a structured format. The collector will do best-effort extraction. A
follow-up improvement (out of scope for this plan) would be to add a standard
`echo "::notice::TOKENS_USED=${total}"` step to each workflow that emits token
usage in a parseable format. This is tracked as a future enhancement in the
report itself.

---

### 7. GitHub API Endpoints Used

| Endpoint | Purpose | Auth |
|----------|---------|------|
| `GET /repos/{owner}/{repo}/actions/runs` | List workflow runs with date filter | `GH_PAT` |
| `GET /repos/{owner}/{repo}/actions/runs/{id}/jobs` | Get step-level timing per run | `GH_PAT` |
| `GET /repos/{owner}/{repo}/actions/runs/{id}/logs` | Download full log zip (deep-dive only) | `GH_PAT` |

The `GH_PAT` already has `repo` scope on consumer repos (required for
`repository_dispatch` in the update workflow). The `actions` scope is implicitly
included with `repo` scope, so no PAT changes are needed.

---

### 8. Report Output Format

Example filename: `analysis/workflow-optimization-2026-04-09.md`

```markdown
# Workflow Optimization Report — 2026-04-09

**Analysis window:** 2026-04-02 → 2026-04-09 (7 days)
**Repos analyzed:** shubhodeep1/coding-workflows, consumer-repo-1, ...
**Total runs:** 142 (87 success, 12 failure, 43 cancelled)
**Model:** openai/gpt-5.3-codex via OpenRouter

---

## Executive Summary

- ...top findings...

## Speed Optimizations
...

## Cost Optimizations
...

## Reliability Improvements
...

## Per-Repo Breakdown
...

## Metrics Appendix

| Phase | Runs | Avg Duration | P95 Duration | Failure Rate | Avg Tokens |
|-------|------|-------------|-------------|-------------|-----------|
| clarify | 24 | 2m 15s | 4m 30s | 4% | 45,000 |
| plan | 18 | 6m 42s | 12m 10s | 11% | 107,000 |
| ... | | | | | |
```

---

## Implementation Sequence

1. **`prompts/mode-workflow-analysis.txt`** — Write the LLM prompt first; this
   defines what the analysis expects as input and drives the data model.
2. **`scripts/collect_workflow_logs.py`** — Log collection + metrics extraction.
   Can be tested standalone against the GitHub API.
3. **`scripts/analyze_workflow_logs.py`** — LLM analysis script. Can be tested
   with mock data before wiring into the workflow.
4. **`.github/workflows/workflow-log-analysis.yml`** — Workflow definition. Wire
   everything together.
5. **Test:** Run `workflow_dispatch` on `main` and verify end-to-end.
6. **README.md update:** Document the new workflow, its inputs, and where to
   find reports.

---

## Future Enhancements (Out of Scope)

These are not part of the initial implementation but are worth tracking:

1. **Structured token emission:** Add a standard `::notice::TOKENS_USED=N`
   output to each workflow phase so the collector can reliably extract token
   counts without log scraping.
2. **Trend analysis:** Once multiple reports exist in `analysis/`, a follow-up
   script could compare week-over-week metrics and flag regressions.
3. **Consumer repo wrapper:** A reusable workflow that consumer repos can call
   to analyze only their own runs (without needing access to other repos).
4. **Cost estimation:** Integrate OpenRouter pricing API to convert token counts
   into dollar estimates per phase.
5. **Scheduled runs:** Add an optional `schedule` trigger (e.g., weekly Monday)
   if on-demand proves insufficient.
6. **Configurable notification channel:** Support posting to a per-repo or
   per-run Telegram chat (e.g., via a `TG_ANALYSIS_CHAT_ID` override) in
   addition to the default admin chat.

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| GitHub API rate limiting during collection | Low | Collection fails | Batch requests, add retry with backoff, limit deep-dive count |
| Log zip download too large for runner disk | Low | OOM / disk full | Only download zips for top-N runs; truncate to last 200 lines |
| LLM context overflow | Medium | Truncated or refused prompt | Pre-compute prompt size; trim deep-dive logs dynamically |
| Token counts not parseable from logs | High | Missing cost data | Best-effort extraction; report "unknown" where unavailable; recommend structured emission (see Future §1) |
| Consumer repos not in `consumer_repos.json` | Medium | Incomplete analysis | Document that repos must be registered; `repos_override` input as escape hatch |
| Report quality varies with model | Low | Unhelpful suggestions | Use `high` reasoning effort; include reference analysis as few-shot example |
| Stale `GH_PAT` lacks scope on new consumer repo | Low | API 403 errors | Collector logs warnings per-repo and continues; report lists skipped repos |

---

## Variables & Secrets Required

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `GH_PAT` | Secret | Yes | — | GitHub PAT with `repo` scope (existing) |
| `OPENROUTER_API_KEY` | Secret | Yes | — | OpenRouter API key (existing) |
| `WORKFLOW_EDITOR_MODEL` | Variable | No | `openai/gpt-5.3-codex` | Model for LLM analysis (existing) |

No new secrets or variables needed.
