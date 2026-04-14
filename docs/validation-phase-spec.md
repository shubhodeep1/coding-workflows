# Spec: Runtime Validation Phase

## Problem

The pipeline ends at the judge, which inspects code statically — reads files, checks symbols, verifies CI status. It never runs the application. `codex_system_instructions.md` line 138 says "Do NOT create test scripts unless asked." A project can pass the judge while having runtime failures that only manifest when the application actually starts.

## Goal

Add a `validate` phase between the judge's "complete" verdict and the tracking issue being closed. It builds, starts, and exercises the application in Docker on the GitHub runner, with **zero real credentials required**. On failure, it creates fix-up issues that go through the full AI pipeline (clarify → plan → implement → review → merge), then re-validates. The entire process is fully unattended.

## Scope — What We Test

Only things that can run without real external API credentials. All testing uses local Docker services and synthetic test data.

### Core tests (always generated)

1. **Build verification.** Dependency installation (`npm ci`, `pip install`, etc.), compilation (TypeScript, Go, Rust), type-checking, linting. Tests that the project produces a runnable artifact.

2. **Startup verification.** The process boots and reaches a ready state. For HTTP servers: binds to its port. For bots: process starts without crashing within 30 seconds. For workers: process starts and connects to its local queue/DB. The app must tolerate missing external API connections gracefully — the test verifies the process doesn't crash, not that external calls succeed.

3. **Local service integration.** Real Postgres, Redis, MongoDB, or RabbitMQ containers in Docker with test credentials. Real queries, real pub/sub, real migrations — not mocked. If the project has database migrations (Prisma, Knex, TypeORM, Alembic, Django, etc.), run them against a fresh database and verify the schema applies cleanly.

4. **Inbound handler testing.** POST realistically-shaped payloads to webhook endpoints, API routes, and message handlers. For Telegram bots: POST a well-formed Telegram Update JSON (with `/start` command) to the webhook endpoint. For HTTP APIs: POST valid request bodies to each POST/PUT/PATCH route. Verify the app responds with non-5xx status codes and valid response shapes.

5. **Route smoke testing.** Hit every HTTP endpoint the app defines. GET routes: expect 200 or valid redirect. POST/PUT/PATCH routes: send minimal valid payloads, expect non-5xx. Check content-type headers. If the app serves static assets, verify they load.

6. **Existing test suite execution.** Run whatever `npm test`, `pytest`, `go test`, `cargo test`, etc. the project already has. These run inside Docker with local services available. If no test suite exists, skip this — the other categories still cover the critical paths.

7. **Environment variable validation.** Grep the codebase for all referenced env vars (`process.env.X`, `os.environ["X"]`, `Deno.env.get("X")`, etc.). Verify every one either has a default value in code, is set in the Docker Compose config, or is explicitly optional. Flag any env var that would cause a crash if missing.

8. **Dependency auditing.** Verify all runtime imports resolve — no missing modules, no broken import paths, no packages in devDependencies that should be in dependencies. Test by importing/requiring the entry point in a clean container.

### Extended tests (generated when applicable)

9. **Database migration verification.** If the project uses a migration framework, apply all migrations to a fresh empty database, then verify the resulting schema matches expectations (tables exist, columns exist, indexes exist). Catches migration ordering bugs and schema conflicts.

10. **Graceful shutdown.** Send SIGTERM to the running process, verify it exits cleanly within 10 seconds without orphaned connections or error logs. Catches missing signal handlers that cause data loss or zombie processes in production.

11. **Error response format.** Send deliberately malformed requests (missing required fields, wrong types, invalid JSON). Verify the app returns structured error responses (JSON with error message), not raw stack traces or HTML error pages. Catches missing input validation and error handling middleware.

12. **Concurrent request handling.** Send 10 simultaneous requests to the same endpoint. Verify the app handles them all without crashing, deadlocking, or returning 5xx. Catches race conditions and connection pool exhaustion.

13. **Configuration validation.** Start the app with a deliberately invalid config (wrong database URL format, invalid port number). Verify it fails fast with a clear error message rather than hanging indefinitely or crashing with an obscure stack trace. Catches missing config validation.

14. **API schema compliance.** If the project has an OpenAPI/Swagger spec, validate that actual API responses match the documented schema. Catches drift between documentation and implementation.

15. **Log output verification.** Verify the app produces meaningful log output on startup and during request handling. Verify errors are logged, not swallowed silently. Catches misconfigured loggers and silent failure modes.

### What we explicitly do NOT test

- Outbound calls to external APIs (Telegram, Stripe, SendGrid, etc.) — no real credentials
- OAuth/SSO flows — require real app registration
- Visual appearance — no headless browser testing
- Performance/load — no baselines defined
- Security scanning — separate concern, not part of validation

---

## Architecture

### State machine

```
Judge says "complete" + ENABLE_VALIDATION=true
  │
  ▼
State: "validating"
  │ dispatch validate.yml via workflow_dispatch
  ▼
validate.yml runs:
  Phase 0: Discover `.ai/validate.yml` hints when missing (ephemeral runtime file)
  Phase 1: Codex generates/fixes validation harness (`generate` on cycle 1, `fix-harness` on cycle 2+)
  Phase 2: Harness pre-flight checks (compose config, shell syntax, build context/dockerfile paths)
  Phase 3: Runner builds app in Docker, runs tests
  Phase 4: Diagnose failures (unless pre-flight/canary shortcut already classified)
  │
  ├─ All tests pass
  │   → add ai:validated label
  │   → poller detects label → close tracking issue ✅
  │
  └─ Tests fail
      → Codex analyzes failures → outputs fix-up issues (same schema as judge)
      → validate_process.sh creates issues via GitHub API
      → add ai:validation-fixing label
      → State: "validation-fixing"
          │
          ▼
        Fix-up issues enter pipeline:
          clarify → plan → implement → review → merge
          │
          ▼
        Poller detects all fix-ups merged
          → re-dispatch validate.yml (validation_cycle += 1)
          → State: "validating"
          │
          ├─ Tests pass → ai:validated → close ✅
          └─ Tests fail again
              ├─ Cycles remaining → create more fix-ups → loop
              └─ MAX_VALIDATE_CYCLES exhausted
                  → ai:validation-failed → Telegram → manual review ❌

Manual reset (comment /revalidate on tracking issue):
  ai:validation-failed
    │ operator comments /revalidate
    ▼
  Reset counters (cycle=1, recovery=0)
    → ai:validating → dispatch validate.yml (fresh cycle 1)
```

### Self-healing via issue creation (not direct commits)

This is critical. When validation fails, Codex does NOT directly edit application code. Instead, it:

1. Reads the test failure output (structured JSON + logs)
2. Diagnoses the root cause
3. Outputs fix-up issues in JSON — same schema as the judge's `new_issues` (id, title, body, priority)
4. `validate_process.sh` creates those issues via `gh issue create` with orchestrator metadata
5. The issues go through the **full AI pipeline** — clarify, plan, implement, review, merge
6. After all fix-up issues merge, the poller re-dispatches validation

This is better than direct patching because:
- Fixes go through code review (review_autofix)
- Each fix is a proper PR with a reviewable diff
- The existing self-heal mechanisms (review autofix iterations) apply to the fix
- Failed fixes get caught by the review phase, not just by re-running the same tests
- The fix process itself is observable (issues, PRs, comments)

---

## Deliverables

### 1. prompts/mode-validate-generate.txt

Prompt for the harness generation phase. Conventions: same as `mode-judge.txt` and `mode-orchestrate.txt`.

Opens with "You are executing the VALIDATE-GENERATE phase..."

Instructs the LLM to:

1. **Analyze the repository** to identify: language/framework, application type (http-server | telegram-bot | worker | cli | library), entry points, startup commands, required local services, referenced env vars, existing test infrastructure, existing Dockerfile/docker-compose, health check endpoints, whether `.ai/validate.yml` hints exist.

2. **Generate files under `validation/`**:

   **`validation/docker-compose.test.yml`**:
   - Builds the app from its Dockerfile (or generates a minimal one if none exists)
   - Includes required service containers (postgres, redis, etc.) with test credentials
   - Isolated Docker network
   - Health checks on every service
   - All credentials are invented test values (`testpassword`, `test-db`, etc.) — never real
   - If the project already has a Dockerfile, reference it; if not, generate a minimal one

   **`validation/validate.sh`**:
   - Thin generated wrapper only (no embedded harness logic)
   - `#!/usr/bin/env bash` + `set -euo pipefail`
   - Delegates to `scripts/validate_driver.sh`:
     ```bash
     exec bash scripts/validate_driver.sh "$@"
     ```

   **`scripts/validate_driver.sh`** (canonical checked-in runtime harness):
   - Pre-flight checks (docker available, compose available, compose file exists, `docker compose config -q`)
   - Build and start: `docker compose -f validation/docker-compose.test.yml up -d --build`
   - Health-check polling with timeout/deadline
   - Execute sorted `validation/tests/*.sh` with canary-first gating
   - TAP-safe counting of `ok` / `not ok`
   - Capture deterministic logs under `validation/logs/` (`compose.log`)
   - Teardown via EXIT trap: `docker compose down -v --remove-orphans`
   - Robust `append_failure`, `emit_result`, `fail_fast`, and idempotent finalization
   - Output structured JSON result to stdout as last output with unchanged schema:
     ```json
     {
       "result": "pass" | "fail",
       "phase": "build" | "startup" | "health" | "tests" | "runtime_validation",
       "total_tests": <int>,
       "passed_tests": <int>,
       "failed_tests": <int>,
       "failures": [
         { "test": "<name>", "error": "<message>", "log_tail": "<last 30 lines>" }
       ],
       "duration_seconds": <int>
     }
     ```
   - Exit 0 on pass, 1 on fail
   - Loads optional `validation/validate.env` and applies conservative defaults for supported knobs (`APP_SERVICE`, `APP_URL`, `HEALTH_TIMEOUT`, `PHASE`, plus existing test credentials/paths)

   **`validation/tests/*.sh`**: Individual test scripts. Each outputs TAP-like lines (`ok N description` / `not ok N description`). Which scripts to generate depends on project type — the prompt must list all categories from the "Scope" section above and instruct the LLM to generate every test that applies.

   **`validation/mocks/*.js` or `*.py`** (only if needed): For inbound handler testing of bots — a tiny mock server that generates realistically-shaped webhook payloads (Telegram Update objects, etc.) and POSTs them to the app's webhook endpoint. These are NOT for mocking outbound calls — they are for simulating inbound traffic.

3. **Rules**:
   - Use real builds and real dependency installs — never mock the build step
   - All credentials are synthetic test values — never reference GitHub Secrets or real credentials
   - Total test budget: 10 minutes
   - If existing tests exist, include them as one of the test scripts, but always also generate the runtime smoke tests
   - Do NOT modify application source code — only create files under `validation/`
   - Make all `.sh` files executable
   - Prefer `curl` for HTTP testing
   - For startup testing: the app must tolerate missing external API connections. If the app crashes because it can't reach `api.telegram.org`, that IS a valid failure — the app should handle missing external connections gracefully (retry, degrade, etc.), not crash on startup

Additional requirements:
- First test must be a canary (`validation/tests/00_canary.sh`) that validates harness infra before app assertions.
- Coverage categories are conditional by applicability (not all mandatory); core categories always apply.

Include the SERENA MCP EFFICIENCY block (same as mode-judge.txt).

### 1b. prompts/mode-validate-discover.txt

Prompt for Phase 0 hint discovery.

Outputs only YAML for `.ai/validate.yml` schema-compatible hints. This output is runtime-only when repo hints are absent and is not committed to the repository.

### 1c. prompts/mode-validate-fix-harness.txt

Prompt for cycle 2+ targeted harness repair.

Scope is strictly `validation/` artifacts and prior failure context; it must patch failing assertions/config minimally instead of regenerating the whole harness. In canonical driver mode, keep `validation/validate.sh` as a wrapper and leave `scripts/validate_driver.sh` unchanged during fix-harness runs.

### 2. prompts/mode-validate-diagnose.txt

Prompt for the failure diagnosis phase. This is different from the old "validate-fix" prompt because it does NOT instruct the LLM to edit code. Instead, it outputs fix-up issues.

Instructs the LLM to:

1. Read the structured failure JSON, container logs, and validation log
2. Diagnose the root cause of each failure
3. Output a JSON object with fix-up issues — same schema as the judge's output:

```json
{
  "status": "needs_fixes" | "harness_error" | "infeasible",
  "diagnosis": "<overall diagnosis of what went wrong>",
  "fix_issues": [
    {
      "id": "<local-id>",
      "title": "<issue title>",
      "body": "<full issue body with enough context for the AI pipeline>",
      "priority": <1-10>
    }
  ],
  "harness_fixes": "<if status is harness_error: description of what's wrong with the test harness itself>"
}
```

- `needs_fixes`: application has bugs that need fixing. `fix_issues` contains the issues to create.
- `harness_error`: the test harness itself is wrong (e.g., testing the wrong port, wrong endpoint path, expecting the wrong response shape). The harness should be regenerated, not the app fixed. `harness_fixes` explains what's wrong.
- `infeasible`: the test requires something that can't be done without real credentials (e.g., the app has no graceful degradation for missing external APIs and we can't fix that in a single issue). Escalate to human.

Rules:
- Each fix-up issue body must be **self-contained** — enough context for the AI pipeline to clarify, plan, and implement without knowing about the validation phase. Include: what file(s) need to change, what the current behavior is, what the expected behavior is, and how to verify the fix.
- Focus on root causes. If 5 tests fail because of one missing error handler, create one issue for the error handler, not 5 issues.
- Prioritize: startup crashes (priority 1) > route errors (priority 3) > missing validation (priority 5) > graceful shutdown (priority 7)

Include the SERENA MCP EFFICIENCY block.

### 3. scripts/validate_process.sh

Main orchestration script, extracted from the workflow (same pattern as `orchestrate_poll_process.sh`).

**Required env vars:**

```
RUNTIME_DIR, GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY,
MODEL_EDITOR, MODEL_REASONING_EFFORT,
TG_BOT_SECRET, TG_ADMIN_CHAT_ID,
TRACKING_ISSUE, VALIDATION_TIMEOUT, TOOL_CALL_BUDGET_VALIDATE,
SERENA_VERSION, SERENA_LANGUAGES, SERENA_DISABLED, SERENA_IGNORED_DIRS
```

**Helpers** — copy from `orchestrate_poll_process.sh`:
- `tg_notify()` — Telegram notification
- `gh_retry()` — GitHub API with exponential backoff
- `post_comment()` — post comment to tracking issue
- `add_label()` — add label (call `python3 scripts/ai_labels.py ensure-labels` first)

**Phase 0: Discover hints**

1. If `.ai/validate.yml` exists, copy/use it directly.
2. Else run Codex with `mode-validate-discover.txt` and store discovered YAML in runtime workspace only.
3. If discovery fails, continue with fallback "no hints" behavior.

**Phase 1: Generate or fix harness**

1. Setup Codex config (`~/.codex/config.toml`) — same pattern as `orchestrate_poll_process.sh` lines 195-201
2. Setup Serena: `bash scripts/setup_serena.sh --mode editing --context codex`
3. Assemble static context — same pattern as `orchestrate_poll_process.sh` lines 232-250
4. Get project spec from tracking issue body via `gh api`, or `${RUNTIME_DIR}/project_spec.txt`
5. Determine harness mode by cycle:
   - Cycle 1 (or missing owned harness): full generate (clean `validation/` then `mode-validate-generate.txt`)
   - Cycle 2+: if an owned harness already exists in the workspace (for example, restored from artifacts), preserve `validation/`, clean only `validation/logs/`, and use `mode-validate-fix-harness.txt`; otherwise fall back to full generate mode
6. Build prompt: static context + TOOL_CALL_BUDGET + selected prompt + project spec + hints + prior failure context
7. Run Codex: `cat prompt | codex exec --model "${MODEL_EDITOR}" --full-auto > output 2> log`
   - Retry up to 2 attempts
   - Verify `validation/validate.sh` exists as a delegating wrapper and `scripts/validate_driver.sh` is present
   - On failure: post comment, Telegram, exit 1
8. `chmod +x` all `.sh` files in `validation/`

**Phase 2: Pre-flight checks**

1. `docker compose -f validation/docker-compose.test.yml config --quiet`
2. `bash -n` for every `validation/**/*.sh`
3. Verify each compose `build.context` + `dockerfile` resolves to existing paths
4. On failure: terminal `harness_error` for the run (`ai:validation-failed`), skip execution and diagnosis

**Phase 3: Execute validation**

1. Run `validation/validate.sh` (wrapper -> `scripts/validate_driver.sh`) with `timeout ${VALIDATION_TIMEOUT}m`
2. Capture output to `${VALIDATION_LOG}`
3. Extract structured JSON result from output — use the brace-matching Python pattern from `orchestrate_poll_process.sh` lines 313-348 to find last JSON with `"result"` key
4. If timeout (exit 124): synthesize failure JSON with `"phase": "timeout"`

**Phase 4: Handle result**

**If result is "pass":**
- Add `ai:validated` label
- Post success comment with test count and duration
- Telegram notification
- Exit 0

**If result is "fail":**
- Canary shortcut: if canary-only failure indicates infra/harness cause, classify directly as `harness_error`.
- If canary indicates app startup/crash behavior, continue to diagnosis (`needs_fixes` remains possible).
- Build diagnosis prompt: static context + mode-validate-diagnose.txt + project spec + failure JSON + container logs (tail) + validation log (tail 200 lines)
- Run Codex to diagnose
- Parse the diagnosis JSON (same brace-matching pattern)
- Handle by status:
  - `needs_fixes`: Create fix-up issues via `gh issue create` with orchestrator metadata (same pattern as `orchestrate_poll_process.sh` lines 480-501, but with `Type: validation-fix-up (cycle N)`)
  - `harness_error`: Log warning, post comment explaining the harness was wrong, add `ai:validation-failed` label, Telegram. This run is terminal; poller-level next steps decide revalidation.
  - `infeasible`: Post comment explaining why, add `ai:validation-failed` label, Telegram.
- For `needs_fixes`: add `ai:validation-fixing` label, post comment listing fix-up issues created, Telegram notification
- Exit 0 (the poller manages the re-validation cycle, not this script)

### 4. .github/workflows/validate.yml

Reusable `workflow_call` workflow. Follow conventions of `orchestrate_poll.yml` and `implement.yml`.

**Inputs:**
```yaml
inputs:
  tracking_issue:
    description: Tracking issue number. 0 or empty for standalone.
    required: false
    type: string
    default: "0"
```

**Secrets:** GH_PAT (required), OPENROUTER_API_KEY (required), TG_BOT_SECRET (optional)

**Env:**
```yaml
env:
  MODEL_EDITOR: ${{ vars.WORKFLOW_VALIDATE_MODEL || vars.WORKFLOW_EDITOR_MODEL || 'openai/gpt-5.3-codex' }}
  MODEL_REASONING_EFFORT: ${{ vars.THINKING_LEVEL_VALIDATE || 'xhigh' }}
```

**Job:** validate
- `runs-on: ubuntu-latest`
- `timeout-minutes: 60`
- Concurrency: `ai-validate-${{ github.repository }}-${{ inputs.tracking_issue || github.run_id }}`, cancel-in-progress false

**Steps** (match patterns from orchestrate_poll.yml):
1. Checkout (v5, fetch-depth 0, GH_PAT token)
2. Setup Node.js 22
3. Setup Python 3.12
4. Install Codex CLI v0.114.0 + core tools (jq, curl, gh) — same install block as orchestrate_poll.yml
5. Install uv for Serena
6. Verify Docker available (`docker --version && docker compose version`)
7. Validate required env vars
8. Create runtime workspace (`/tmp/codex-validate-...`)
9. Fetch support scripts from coding-workflows@stable: `setup_serena.sh`, `validate_process.sh`, `validate_driver.sh`, `ai_labels.py`, `mode-validate-generate.txt`, `mode-validate-diagnose.txt`, `codex_system_instructions.md`, `ai_pipeline.md`
10. Configure git identity (github-actions[bot])
11. Run `bash scripts/validate_process.sh` with env vars: GH_TOKEN, OPENROUTER_API_KEY, TG_BOT_SECRET, TG_ADMIN_CHAT_ID, TRACKING_ISSUE, VALIDATION_TIMEOUT (default 15), TOOL_CALL_BUDGET_VALIDATE (default 60), SERENA vars
12. Upload artifacts (validation/logs/, runtime dir files) — `actions/upload-artifact@v4`, retention 14 days, `if: always()`
13. Clean up Docker (`docker compose down -v`, `docker image prune -f`) — `if: always()`
14. Write run summary to `$GITHUB_STEP_SUMMARY`

### 5. workflow-templates/ai-validate.yml

Consumer wrapper:
```yaml
name: AI Validate
on:
  workflow_dispatch:
    inputs:
      tracking_issue:
        description: Tracking issue number
        required: false
        type: string
        default: "0"
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  validate:
    uses: shubhodeep1/coding-workflows/.github/workflows/validate.yml@stable
    with:
      tracking_issue: ${{ inputs.tracking_issue || '0' }}
    secrets: inherit
```

Also create `.github/workflows/internal-validate.yml` — same pattern as `internal-orchestrate.yml` but referencing `./.github/workflows/validate.yml`.

### 6. Modify scripts/orchestrate_poll_process.sh

**Change 1: Add state handlers for "validating" and "validation-fixing".**

Insert after the existing status check at line 84 (`if [ "${PROJECT_STATUS}" = "complete" ] || [ "${PROJECT_STATUS}" = "failed" ]`) — add these two states to the early-exit/special-handling section, BEFORE the wave-completion check.

**"validating" state handler:**
- Check tracking issue labels via `gh api`
- If `ai:validated`: set state to `"complete"`, post state comment, close tracking issue with "Project completed — judge approved and runtime validation passed.", Telegram. `continue`.
- If `ai:validation-failed`: set state to `"validation-failed"`, post state comment, Telegram. `continue`.
- Otherwise: log "Validation running", `continue`.

**"validation-fixing" state handler:**
- Get the list of fix-up issue numbers from state (stored when validate_process.sh created them)
- Check labels on each fix-up issue
- If all fix-up issues have `ai:merged`: 
  - Read `validation_cycle` from state. If `>= MAX_VALIDATE_CYCLES` (default 3): set state to `"validation-failed"`, post comment, Telegram, `continue`.
  - Otherwise: increment `validation_cycle`, set state to `"validating"`, re-dispatch validate.yml (`gh workflow run ai-validate.yml -f tracking_issue=NUM`), Telegram ("Re-running validation after fix-ups merged"), `continue`.
- If any fix-up issue has `ai:closed` (closed without merge): set state to `"validation-failed"`, Telegram ("Fix-up issue closed without merge"), `continue`.
- Otherwise: log "Fix-ups still in progress", `continue`.

Also add `"validation-failed"` to the early-exit check at line 84.

**Change 2: Modify the `complete)` case** (approximately line 387-404).

Read `ENABLE_VALIDATION` env var (default `true`):
- If `true`:
  - Set state to `"validating"`, set `validation_cycle` to 1, increment judge_cycle
  - Post state comment
  - Post comment: "## 🧪 Runtime Validation Dispatched\n\nJudge approved all waves. Running runtime validation (build → start → test) before marking complete."
  - Dispatch: `gh workflow run ai-validate.yml --repo "${GITHUB_REPOSITORY}" -f tracking_issue="${TRACKING_NUM}"`
  - If dispatch fails: fall back to original behavior (close immediately), Telegram with note that validation workflow is not configured
- If `false`:
  - Existing behavior exactly — set state to complete, close tracking issue, Telegram

### 7. Modify .github/workflows/orchestrate_poll.yml

Add to "Process each tracking issue" step's `env:` block:
```yaml
ENABLE_VALIDATION: ${{ vars.ENABLE_VALIDATION || 'true' }}
MAX_VALIDATE_CYCLES: ${{ vars.MAX_VALIDATE_CYCLES || '3' }}
```

### 8. Modify .github/ai/label_contract.v1.json

Add labels:
```json
"ai:validating": {
  "color": "1d76db",
  "description": "Runtime validation in progress (post-judge)"
},
"ai:validated": {
  "color": "0e8a16",
  "description": "Runtime validation passed — ready for release"
},
"ai:validation-failed": {
  "color": "e11d48",
  "description": "Runtime validation failed — manual review needed"
},
"ai:validation-fixing": {
  "color": "d93f0b",
  "description": "Validation fix-up issues in pipeline"
}
```

Add all four to the `"issue_phase"` → `"members"` array after `"ai:closed"`.

### 9. Update README.md

Add a "Runtime Validation" section after "Project Orchestrator". Cover:
- What it does (1 paragraph)
- Architecture diagram (the state machine from this spec)
- What it tests (the full scope list)
- What it doesn't test and why
- Setup steps (copy wrapper, set ENABLE_VALIDATION=true, optional hints file)
- New variables table
- New labels table
- Updated full pipeline diagram

### 10. Add examples/ai-validate-hints.yml

Annotated example of the optional `.ai/validate.yml` hints file for consumer repos. Fields (all optional):
- `type`: http-server | telegram-bot | worker | cli | library
- `entry`: entry point path
- `port`: listen port
- `health_check`: health endpoint path
- `services`: list of docker images (postgres:16, redis:7, etc.)
- `env_overrides`: map of env var → test value
- `custom_tests`: list of shell commands to run as additional tests
- `bot_commands`: for telegram-bot, list of commands to test via webhook
- `worker_verify`: for worker, queue name + test payload + success check
- `timeout`: minutes
- `skip_tests`: list of test categories to skip

---

## Constraints

- **No breaking changes.** `ENABLE_VALIDATION` defaults to `true`. When disabled, behavior is identical to today.
- **No real credentials.** Everything runs with synthetic test values. The validation harness never reads GitHub Secrets. Consumer repos do NOT need to add any new secrets.
- **Follow existing conventions exactly.** Code style, error handling, retry patterns, Telegram notification format, label management, git identity setup, Codex invocation (`codex exec --model X --full-auto`), YAML structure, orchestrator metadata format on issues.
- **Codex CLI pinned to v0.114.0.**
- **Shell scripts use `set -euo pipefail`.**
- **Python uses `PYTHONDONTWRITEBYTECODE=1`.**
- **Runs on `ubuntu-latest`** which has Docker and Docker Compose pre-installed.
- **`validation/` directory is ephemeral** — generated fresh each validate run, never committed to consumer repos.

## Variables

| Variable | Default | Description |
|---|---|---|
| `ENABLE_VALIDATION` | `true` | Master switch — opt-in per consumer repo |
| `MAX_VALIDATE_CYCLES` | `3` | Full validate → fix → re-validate cycles before escalating |
| `THINKING_LEVEL_VALIDATE` | `xhigh` | Reasoning effort for harness generation + diagnosis |
| `TOOL_CALL_BUDGET_VALIDATE` | `60` | Tool call budget |
| `VALIDATION_TIMEOUT` | `60` | Minutes for the Docker test run |
| `WORKFLOW_VALIDATE_MODEL` | falls back to `WORKFLOW_EDITOR_MODEL` | Model override |
