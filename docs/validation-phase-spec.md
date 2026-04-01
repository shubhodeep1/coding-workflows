# Spec: Runtime Validation Phase (Post-Judge Dry Run)

## Problem Statement

The current pipeline ends at the judge phase, which performs **static code inspection only** — reading files, checking symbols, verifying CI status. It never actually **runs** the application. The `codex_system_instructions.md` (line 138) even says "Do NOT create test scripts unless asked."

This means a project can pass the judge while having runtime failures: missing dependencies at import time, startup crashes, broken HTTP routes returning 500, Telegram webhook handlers that crash on real payloads, database migrations that don't apply, misconfigured environment variables, etc.

## Goal

Add a new **validate** phase that sits between the judge's "complete" verdict and the tracking issue being closed. It builds, starts, and exercises the application in an ephemeral Docker environment on the GitHub runner, with AI-powered self-healing when tests fail. The entire process is unattended — no human intervention.

## New Pipeline

```
Before: ... → Judge (complete) → close tracking issue
After:  ... → Judge (complete) → Validate (runtime dry-run) → close tracking issue
```

The validate phase is **opt-in** via `ENABLE_VALIDATION=true` (default `false`). When disabled, the existing behavior (close immediately on judge complete) is preserved exactly.

## Architecture Overview

```
orchestrate_poll detects judge "complete" + ENABLE_VALIDATION=true
  → transitions state to "validating"
  → dispatches validate.yml via: gh workflow run ai-validate.yml -f tracking_issue=NUM
  → next poll cycles check for ai:validated or ai:validation-failed labels

validate.yml runs on ubuntu-latest:
  Phase 1: Codex (xhigh) reads full repo → generates validation/ harness
  Phase 2: Runner executes harness (docker compose on the runner)
  Phase 3: On failure → Codex diagnoses + fixes → re-run (up to N iterations)
  Phase 4: Pass → add ai:validated label. Exhausted → add ai:validation-failed label.
```

---

## Deliverable 1: Prompt files

### prompts/mode-validate-generate.txt

LLM prompt for the harness generation phase. Must follow the same conventions as existing prompts in `prompts/` (e.g., `mode-judge.txt`, `mode-orchestrate.txt`):
- Opens with "You are executing the VALIDATE-GENERATE phase..."
- References that static context is already inlined above
- Includes SERENA MCP EFFICIENCY block (mandatory, copy pattern from mode-judge.txt)
- No markdown fences in output — just write files to disk and explain

The prompt must instruct the LLM to:

1. **Analyze the repository** by reading the codebase to identify:
   - Language/framework (package.json, requirements.txt, go.mod, Cargo.toml, Dockerfile, etc.)
   - Application type: `http-server` | `telegram-bot` | `worker` | `cli` | `library` | `multi`
   - Entry point(s) and startup command(s)
   - Required external services (databases, caches, message queues)
   - Environment variables referenced in code (grep for `process.env`, `os.environ`, `Deno.env`, etc.)
   - Existing test infrastructure (npm test scripts, pytest, etc.)
   - Existing Dockerfile or docker-compose.yml (reuse when available)
   - Health check endpoints or readiness probes
   - Whether a `.ai/validate.yml` hints file exists (use its hints if present)

2. **Generate files under `validation/` directory** at the repo root:

   **`validation/docker-compose.test.yml`** — ephemeral Docker Compose config that:
   - Builds the application from its Dockerfile (or generates a minimal Dockerfile if none exists)
   - Spins up required service dependencies (postgres, redis, etc.) with test credentials
   - Uses an isolated Docker network
   - Sets health checks on every service
   - All secrets/credentials are test values like `test-secret-XXXX`, `testpassword`, never real

   **`validation/validate.sh`** — POSIX-compliant master runner script that:
   - Runs pre-flight checks (docker available, required ports free)
   - Builds and starts services: `docker compose -f docker-compose.test.yml up -d --build`
   - Polls health checks with timeout (max 120 seconds)
   - Runs each test script in `validation/tests/`
   - Captures container logs to `validation/logs/`
   - Tears down: `docker compose -f docker-compose.test.yml down -v --remove-orphans`
   - Uses `trap` for cleanup on both success and failure
   - Outputs a **structured JSON result** to stdout as the last output:
     ```json
     {
       "result": "pass" | "fail",
       "phase": "build" | "startup" | "health" | "tests",
       "total_tests": <int>,
       "passed_tests": <int>,
       "failed_tests": <int>,
       "failures": [
         { "test": "<name>", "error": "<message>", "log_tail": "<last 30 lines>" }
       ],
       "duration_seconds": <int>
     }
     ```
   - Exits 0 on pass, 1 on fail

   **`validation/tests/*.sh`** — project-type-specific test scripts. Each script outputs TAP-like results: `ok N description` or `not ok N description`. Types:

   - **http-server**: `test_build.sh` (docker build succeeds), `test_startup.sh` (process starts, binds port), `test_health.sh` (health endpoint returns 200), `test_routes.sh` (smoke-test each defined route — GET for 200, POST/PUT for non-5xx), `test_env.sh` (required env vars are set), `test_dependencies.sh` (no missing modules at import time)
   - **telegram-bot**: `test_build.sh`, `test_startup.sh` (bot process starts without crashing), `test_mock_webhook.sh` (mock Telegram API server → bot registers via getMe → send test update → verify response), `test_env.sh`, `test_dependencies.sh`
   - **worker**: `test_build.sh`, `test_startup.sh` (worker connects to queue/DB), `test_processing.sh` (enqueue test payload, verify processing), `test_env.sh`, `test_dependencies.sh`
   - **cli**: `test_build.sh`, `test_help.sh` (--help exits 0), `test_basic.sh` (sample input → expected output), `test_dependencies.sh`
   - **library**: `test_build.sh`, `test_import.sh` (import without errors), `test_exports.sh` (public exports exist)

   **`validation/mocks/*.js` or `*.py`** (if needed) — lightweight single-file mock servers for external APIs (Telegram Bot API, payment gateways, etc.). Each mock returns structurally valid responses and logs requests to stdout. Referenced as services in docker-compose.test.yml.

3. **Rules the prompt must enforce**:
   - Use REAL dependency installs and builds — no mocking the build step
   - Total test execution budget: 10 minutes
   - If existing tests exist (npm test, pytest), include them but also add the runtime smoke tests
   - Do NOT modify application source code — only create files under `validation/`
   - Make validate.sh and all test scripts executable
   - Use `set -euo pipefail` in all shell scripts
   - Prefer `curl` for HTTP testing (available in all Docker images)

### prompts/mode-validate-fix.txt

LLM prompt for the self-healing phase. Conventions same as above.

The prompt must instruct the LLM to:
1. Read the structured failure JSON and container logs provided below the prompt
2. Diagnose the root cause
3. Fix it with minimal changes

Key rules to encode:
- **Prioritize application code fixes over test harness fixes.** If a test fails, the most likely cause is an app bug, not a bad test.
- **Only fix the harness if the test itself is provably wrong** (e.g., wrong port, wrong endpoint path).
- **Fix the root cause, not the symptom.** Don't catch errors — fix the underlying bug.
- **Minimize change scope.** No refactoring unrelated code.
- **Do NOT weaken tests** — no `|| true`, no `set +e`, no deleting failing tests.
- **After fixing, the same validate.sh must pass.**
- Output a summary: root cause, files modified, category (application-fix | harness-fix | config-fix | dependency-fix), confidence (high | medium | low).

---

## Deliverable 2: scripts/validate_process.sh

The main validation orchestration script, extracted from the workflow for maintainability (same pattern as `scripts/orchestrate_poll_process.sh` being extracted from `orchestrate_poll.yml`).

### Required environment variables (set by the workflow step)

```
RUNTIME_DIR, GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY,
MODEL_EDITOR, MODEL_REASONING_EFFORT,
TG_BOT_SECRET, TG_ADMIN_CHAT_ID,
TRACKING_ISSUE, MAX_VALIDATE_ITERATIONS, VALIDATION_TIMEOUT,
TOOL_CALL_BUDGET_VALIDATE,
SERENA_VERSION, SERENA_LANGUAGES, SERENA_DISABLED, SERENA_IGNORED_DIRS
```

### Script structure

Must include the standard helpers copied from `orchestrate_poll_process.sh`:
- `tg_notify()` — send Telegram notification (same pattern)
- `gh_retry()` — GitHub API with exponential backoff retry (same pattern)
- `post_comment()` — post comment to tracking issue
- `add_label()` — add label to tracking issue (call `python3 scripts/ai_labels.py ensure-labels` first)

### Phase 1: Generate harness

1. Setup Codex config at `~/.codex/config.toml` (same pattern as orchestrate_poll_process.sh lines 195-201)
2. Setup Serena: `bash scripts/setup_serena.sh --mode editing --context codex`
3. Assemble static context (same pattern as orchestrate_poll_process.sh lines 232-250): system instructions + ai_pipeline.md + agents.md + README.md
4. Get project spec from tracking issue body (via `gh api`), or from `${RUNTIME_DIR}/project_spec.txt`
5. Check for optional `.ai/validate.yml` hints file in the repo
6. Build the generate prompt: static context + `TOOL_CALL_BUDGET` + `mode-validate-generate.txt` + project spec + hints
7. Run Codex: `cat prompt | codex exec --model "${MODEL_EDITOR}" --full-auto > output 2> log`
   - Retry up to 2 times
   - Verify `validation/validate.sh` was actually created on disk
   - If generation fails after retries: post failure comment, notify via Telegram, exit 1
8. `chmod +x` all generated `.sh` files and mock server files

### Phase 2 + 3: Execute and self-heal loop

Loop from iteration 1 to `MAX_VALIDATE_ITERATIONS`:

1. Clean previous results
2. Run `validation/validate.sh` with `timeout ${VALIDATION_TIMEOUT}m`
   - Capture full output to `${VALIDATION_LOG}`
   - Handle timeout (exit code 124)
3. Extract structured JSON result from the log output
   - Use Python to find the last JSON object containing `"result"` key in the output (same brace-matching pattern as orchestrate_poll_process.sh lines 313-348)
   - If no JSON found, synthesize a failure result
4. Check result:
   - If `"pass"` → break loop, set FINAL_RESULT=pass
   - If `"fail"` and iterations remain → proceed to self-heal
   - If `"fail"` and no iterations remain → break loop, FINAL_RESULT=fail
5. Self-heal:
   - Collect failure details from the JSON result
   - Collect container logs from `validation/logs/`
   - Build fix prompt: static context + `mode-validate-fix.txt` + project spec + failure report + container logs + validation log tail
   - Run Codex: `cat prompt | codex exec --model "${MODEL_EDITOR}" --full-auto > output`
   - Check if files were modified (`git status --porcelain`)
   - If modified: `git add -A && git commit -m "validate: self-heal fix (iteration N)"`
   - Clean up docker state: `docker compose -f validation/docker-compose.test.yml down -v --remove-orphans`

### Phase 4: Report results

**On pass:**
- Add `ai:validated` label to tracking issue
- Post success comment with test count and iterations used
- Send Telegram notification
- Push self-heal commits if any (git push to current branch)

**On fail:**
- Add `ai:validation-failed` label to tracking issue
- Post detailed failure comment with: failed phase, failure list (first 5), last 50 lines of validation log in a `<details>` block
- Send Telegram notification
- Exit 1

---

## Deliverable 3: .github/workflows/validate.yml

Reusable `workflow_call` workflow. Follow the exact conventions of the existing workflows (see `orchestrate_poll.yml` and `implement.yml` for patterns).

### Inputs

```yaml
inputs:
  tracking_issue:
    description: Tracking issue number for status updates. 0 or empty for standalone.
    required: false
    type: string
    default: "0"
```

### Secrets

```yaml
secrets:
  GH_PAT:
    required: true
  OPENROUTER_API_KEY:
    required: true
  TG_BOT_SECRET:
    required: false
```

### Env

```yaml
env:
  MODEL_EDITOR: ${{ vars.WORKFLOW_VALIDATE_MODEL || vars.WORKFLOW_EDITOR_MODEL || 'openai/gpt-5.3-codex' }}
  MODEL_REASONING_EFFORT: ${{ vars.THINKING_LEVEL_VALIDATE || 'xhigh' }}
```

### Job: validate

- `runs-on: ubuntu-latest`
- `timeout-minutes: 120`
- Concurrency: `ai-validate-${{ github.repository }}-${{ inputs.tracking_issue || github.run_id }}`, `cancel-in-progress: false`

### Steps (follow exact patterns from orchestrate_poll.yml)

1. **Checkout** — `actions/checkout@v5`, `fetch-depth: 0`, token from GH_PAT
2. **Setup Node.js** — `actions/setup-node@v4`, node 22
3. **Setup Python** — `actions/setup-python@v5`, python 3.12
4. **Install Codex CLI and core tools** — same pattern as orchestrate_poll.yml (npm install codex@v0.114.0, check for jq/curl/gh, apt-get install if missing)
5. **Install uv for Serena** — `astral-sh/setup-uv@v7`
6. **Verify Docker is available** — `docker --version && docker compose version`
7. **Validate required env vars** — check OPENROUTER_API_KEY is set
8. **Create runtime workspace** — `/tmp/codex-validate-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`
9. **Fetch workflow support scripts** — fetch from `coding-workflows@stable` via `gh api`:
   - `scripts/setup_serena.sh`
   - `scripts/validate_process.sh`
   - `scripts/ai_labels.py`
   - `prompts/mode-validate-generate.txt`
   - `prompts/mode-validate-fix.txt`
   - `codex_system_instructions.md` (if not in consumer repo)
   - `ai_pipeline.md` (if not in consumer repo)
10. **Configure git identity** — same pattern as implement.yml (github-actions[bot])
11. **Run validation process** — `bash scripts/validate_process.sh` with env vars:
    - `GH_TOKEN`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`
    - `TRACKING_ISSUE` from input
    - `MAX_VALIDATE_ITERATIONS` from vars (default 3)
    - `VALIDATION_TIMEOUT` from vars (default 15)
    - `TOOL_CALL_BUDGET_VALIDATE` from vars (default 60)
    - `SERENA_VERSION`, `SERENA_LANGUAGES`, `SERENA_DISABLED`, `SERENA_IGNORED_DIRS`
12. **Push self-heal commits** — if success, check for unpushed commits, push to default branch. If direct push fails (branch protection), create a PR from a `validate/self-heal-*` branch.
13. **Upload validation artifacts** — `actions/upload-artifact@v4`: validation/logs/, runtime dir files, retention 14 days
14. **Clean up Docker resources** — `docker compose down -v`, `docker image prune -f` (in `if: always()`)
15. **Write run summary** — `$GITHUB_STEP_SUMMARY` table with tracking issue, model, reasoning, result, max iterations

---

## Deliverable 4: workflow-templates/ai-validate.yml

Consumer wrapper template. Follow exact pattern of existing templates in `workflow-templates/`.

```yaml
name: AI Validate
on:
  workflow_dispatch:
    inputs:
      tracking_issue:
        description: Tracking issue number for status updates
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

Also create `.github/workflows/internal-validate.yml` for this repo (same pattern as `internal-orchestrate.yml` but using `./.github/workflows/validate.yml`).

---

## Deliverable 5: Orchestrator integration (modify existing files)

### 5a: Modify scripts/orchestrate_poll_process.sh

**Change 1: Add "validating" state handler** — insert BEFORE the existing wave-completion check and judge invocation section (around the area where the script checks if all issues in the current wave have reached `ai:merged`). When `jq -r '.status'` on the state file returns `"validating"`:

- Check tracking issue labels via `gh api`
- If `ai:validated` present: set state to `"complete"`, post state comment, close tracking issue with comment "Project completed — judge approved and runtime validation passed.", notify via Telegram. Then `continue` (skip the rest of the loop for this tracking issue).
- If `ai:validation-failed` present: set state to `"validation-failed"`, post state comment, notify via Telegram. Then `continue`.
- Otherwise: log "Validation still running", `continue`.

**Change 2: Modify the `complete)` case** (currently at approximately line 387-404). Read `ENABLE_VALIDATION` env var (default `false`):

- If `true`:
  - Set state to `"validating"`, increment judge_cycle
  - Post state comment
  - Post comment: "## 🧪 Runtime Validation Dispatched\n\nJudge approved all waves. Now running runtime validation..."
  - Dispatch: `gh workflow run ai-validate.yml --repo "${GITHUB_REPOSITORY}" -f tracking_issue="${TRACKING_NUM}"`
  - If dispatch fails (workflow not found): fall back to original behavior — set state to `"complete"`, close tracking issue, notify
  - Notify via Telegram
- If `false`:
  - Preserve the existing behavior exactly (set state to complete, close issue, notify)

### 5b: Modify .github/workflows/orchestrate_poll.yml

Add to the "Process each tracking issue" step's `env:` block:

```yaml
ENABLE_VALIDATION: ${{ vars.ENABLE_VALIDATION || 'false' }}
MAX_VALIDATE_ITERATIONS: ${{ vars.MAX_VALIDATE_ITERATIONS || '3' }}
```

### 5c: Modify .github/ai/label_contract.v1.json

Add three new labels under `"labels"`:

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
  "description": "Runtime validation failed after self-heal attempts"
}
```

Add `"ai:validating"`, `"ai:validated"`, `"ai:validation-failed"` to the `"issue_phase"` → `"members"` array (after `"ai:closed"`).

---

## Deliverable 6: Documentation

### 6a: Update README.md

Add a new section **"Runtime Validation"** after the "Project Orchestrator" section. Cover:
- What it does (1 paragraph)
- Architecture diagram (ASCII)
- Setup steps (copy wrapper, set ENABLE_VALIDATION=true, optional hints file)
- New variables table
- New labels table
- What gets tested per project type (table: http-server, telegram-bot, worker, cli, library)
- Updated pipeline diagram showing the full flow

### 6b: Add examples/ai-validate-hints.yml

An annotated example `.ai/validate.yml` hints file that consumer repos can optionally place at `.ai/validate.yml` to guide the harness generator. Include:
- `type` (http-server | telegram-bot | worker | cli | library)
- `entry` (entry point path)
- `port` (listen port)
- `health_check` (health endpoint)
- `services` (list of docker images: postgres:16, redis:7, etc.)
- `env_overrides` (map of env var → test value)
- `custom_tests` (list of shell commands to run as additional tests)
- `bot_commands` (for telegram-bot: list of commands to test)
- `worker_verify` (for worker: queue name, test payload, success check SQL)
- `timeout` (minutes)

All fields optional. Annotate with comments explaining each field.

---

## Constraints

- **No breaking changes.** When `ENABLE_VALIDATION=false` (the default), the entire system must behave identically to today.
- **Follow existing conventions exactly.** Match the code style, error handling, retry patterns, Telegram notification format, label management, git identity setup, Codex invocation pattern, and YAML structure of the existing workflows.
- **Shell scripts use `set -euo pipefail`.**
- **Codex CLI version is pinned to `v0.114.0`** (match existing workflows).
- **Python scripts use `PYTHONDONTWRITEBYTECODE=1`.**
- **The validate workflow must work on `ubuntu-latest` runners** which have Docker and Docker Compose pre-installed.
- **All secrets/credentials used in the validation harness must be fake test values.** Never reference real secrets.
- **The validation/ directory is ephemeral** — generated fresh each run, never committed to the consumer repo (it's gitignored or exists only during the workflow run).

## Variables Summary

| Variable | Default | Description |
|---|---|---|
| `ENABLE_VALIDATION` | `false` | Master switch — opt-in per consumer repo |
| `MAX_VALIDATE_ITERATIONS` | `3` | Self-heal attempts before escalating |
| `THINKING_LEVEL_VALIDATE` | `xhigh` | Reasoning effort for harness gen + diagnosis |
| `TOOL_CALL_BUDGET_VALIDATE` | `60` | Tool call budget |
| `VALIDATION_TIMEOUT` | `15` | Minutes per validation attempt |
| `WORKFLOW_VALIDATE_MODEL` | falls back to `WORKFLOW_EDITOR_MODEL` | Model override for validation |
