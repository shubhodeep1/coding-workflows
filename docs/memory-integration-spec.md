# Spec: AI Memory Branch Integration

## Problem

The AI memory system is fully implemented — CLI (`scripts/ai_memory.py`), library (`scripts/ai_memory_lib.py`), JSON schemas, retrieval profiles, and branch-safe persistence logic all exist and work. However, **no workflow calls the memory CLI commands**. The `ai-memory` git branch has never been created, and no memory records are persisted. The entire memory subsystem is dormant.

## Goal

Wire the memory CLI into every workflow so that:

1. **Run events** are recorded at the start and end of every phase (clarify, plan, implement, review, orchestrate, validate).
2. **Candidate records** capture decisions, plans, implementation summaries, review findings, and validation results.
3. **Memory retrieval** feeds relevant prior context into LLM prompts for each phase.
4. **Processed-command idempotency** prevents duplicate plan/implement runs from race conditions.
5. **Task lineage** tracks the full issue-to-PR lifecycle (open → in_progress → merged/closed).
6. **Monthly compaction** (already partially wired) works correctly once data exists.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Retrieve injection | Between static and dynamic prompt sections | Preserves LLM prompt-prefix caching |
| `issue_pr_status.yml` | Add checkout + Python setup | Required for `finalize-task` — complete lineage tracking |
| `memory_helpers.sh` | Committed to repo at `scripts/` | Consistent with `ai_memory.py`, `tg_helpers.sh`; fetched by callers at runtime |
| Branch bootstrap | CLI auto-creates on first write | No extra workflow step needed |
| `orchestrate_poll_process.sh` | Memory calls in YAML wrapper only | Less invasive than modifying the 500+ line shell script |
| `review_autofix.yml` scope | Full integration (retrieve + candidates + run events) | Maximum value from review pattern capture |

## Fail Policy

From `ai-memory/README.md` — this is non-negotiable:

| Command | Fail Policy | Behavior on Error |
|---------|-------------|-------------------|
| `retrieve` | fail-open | Log warning, inject "status: unavailable" context, continue |
| `record-run-event` | fail-open | Log warning, continue workflow |
| `record-candidate` | fail-open | Log warning, continue workflow |
| `promote` | **fail-closed** | Workflow fails |
| `finalize-task` | **fail-closed** | Workflow fails |
| `processed-command-check` | fail-open | Default to "not processed", continue |
| `processed-command-claim` | **fail-closed** | Workflow fails (idempotency is safety-critical) |
| `processed-command-complete` | **fail-closed** | Workflow fails |

## Environment Variables

No new secrets required. Existing `GH_PAT` and `OPENROUTER_API_KEY` suffice. The memory CLI reads these env vars with defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_MEMORY_ENABLED` | `true` | Kill switch for all memory operations |
| `AI_MEMORY_BRANCH` | `ai-memory` | Git branch for memory storage |
| `AI_MEMORY_ROOT` | `ai-memory` | Directory root within the branch |
| `AI_MEMORY_PUSH_RETRIES` | `5` | Git push retry count with rebase |
| `AI_MEMORY_KEYWORD_MODEL` | `openai/gpt-5-mini` | Model for semantic keyword extraction |
| `AI_MEMORY_KEYWORD_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL for keyword model |
| `AI_MEMORY_TOKEN_BUDGET_<ROLE>` | per-profile | Per-role token budget override |

## CLI Reference

All subcommands share these optional flags:
- `--repo-root` (default: cwd)
- `--memory-branch` (default: env `AI_MEMORY_BRANCH` or `ai-memory`)
- `--memory-root` (default: env `AI_MEMORY_ROOT` or `ai-memory`)
- `--push-retries` (default: env `AI_MEMORY_PUSH_RETRIES` or `5`)
- `--enabled` (default: env `AI_MEMORY_ENABLED` or `true`)

### retrieve
```
python3 scripts/ai_memory.py retrieve \
  --role <clarify|planning|implementation|reviewer|autofix> \
  --issue-number <int> \
  --issue-title <str> \
  --issue-body-file <path> \
  --output-file <path>
```

### record-run-event
```
python3 scripts/ai_memory.py record-run-event \
  --run-id <str> --workflow <str> --event-type <str> \
  --status <ok|error|info> --message <str> --actor <str> \
  [--issue-number <int>] [--pr-number <int>] [--metadata-json <json>]
```

### record-candidate
```
python3 scripts/ai_memory.py record-candidate \
  --category <decisions|constraints|patterns|incidents|run_events|task_summaries> \
  --summary <str, max 500 chars> --details <str, max 12000 chars> \
  --workflow <str> --run-id <str> --actor <str> \
  [--confidence <float, 0-1, default 0.70>] \
  [--issue-number <int>] [--pr-number <int>] \
  [--source-refs <csv>] [--parent-ids <csv>]
```

### finalize-task
```
python3 scripts/ai_memory.py finalize-task \
  --issue-number <int> --issue-url <url> \
  --final-state <open|in_progress|merged|closed|cancelled> \
  --workflow <str> --run-id <str> --actor <str> \
  [--pr-number <int>] [--pr-url <url>]
```

### processed-command-check
```
python3 scripts/ai_memory.py processed-command-check \
  --issue-number <int> --comment-id <int> --command <str>
```
Output: JSON with `"exists": true|false`

### processed-command-claim
```
python3 scripts/ai_memory.py processed-command-claim \
  --issue-number <int> --comment-id <int> --command <str> \
  --workflow <str> --actor <str> --run-id <str> \
  [--run-attempt <int>]
```
Output: JSON with `"claimed": true|false`

### processed-command-complete
```
python3 scripts/ai_memory.py processed-command-complete \
  --issue-number <int> --comment-id <int> --command <str> \
  --status <str> [--metadata-json <json>]
```

---

## Phase 0: Foundation — `scripts/memory_helpers.sh`

Create `scripts/memory_helpers.sh` with fail-open/fail-closed wrappers. This file is committed to the repo. Caller repos fetch it at runtime alongside `tg_helpers.sh` and other support scripts.

### Full file content

```bash
#!/usr/bin/env bash
# memory_helpers.sh — Shell wrappers for AI memory CLI with fail-open/closed semantics.
#
# Source this file after fetching support scripts:
#   source scripts/memory_helpers.sh
#
# Fail-open functions log a warning and return 0 on error.
# Fail-closed functions propagate the error (non-zero exit).

MEMORY_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_memory_enabled() {
	local enabled
	enabled="$(printf '%s' "${AI_MEMORY_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${enabled}" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

# --- Fail-open wrappers ---

memory_record_run_event() {
	if ! _memory_enabled; then return 0; fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-run-event "$@" 2>&1 || {
		echo "::warning::memory record-run-event failed (fail-open)"
		return 0
	}
}

memory_record_candidate() {
	if ! _memory_enabled; then return 0; fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-candidate "$@" 2>&1 || {
		echo "::warning::memory record-candidate failed (fail-open)"
		return 0
	}
}

memory_retrieve() {
	if ! _memory_enabled; then
		local outfile="${1:-}"
		if [ -n "${outfile}" ]; then
			printf 'AI MEMORY CONTEXT\nstatus: disabled\n' > "${outfile}"
		fi
		return 0
	fi
	# Usage: memory_retrieve <output-file> [extra-flags...]
	local outfile="$1"; shift
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" retrieve \
		--output-file "${outfile}" "$@" 2>&1 || {
		echo "::warning::memory retrieve failed (fail-open)"
		printf 'AI MEMORY CONTEXT\nstatus: unavailable\n' > "${outfile}"
		return 0
	}
}

memory_processed_command_check() {
	if ! _memory_enabled; then
		echo '{"exists": false}'
		return 0
	fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-check "$@" 2>&1 || {
		echo "::warning::memory processed-command-check failed (fail-open)"
		echo '{"exists": false}'
		return 0
	}
}

# --- Fail-closed wrappers ---

memory_finalize_task() {
	if ! _memory_enabled; then return 0; fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" finalize-task "$@"
}

memory_promote() {
	if ! _memory_enabled; then return 0; fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" promote "$@"
}

memory_processed_command_claim() {
	if ! _memory_enabled; then return 0; fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-claim "$@"
}

memory_processed_command_complete() {
	if ! _memory_enabled; then return 0; fi
	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-complete "$@"
}
```

### Fetching in workflows

Add `memory_helpers.sh` to each workflow's "Fetch workflow support scripts" step. The fetch block already downloads `tg_helpers.sh`, `codex_system_instructions.md`, etc. Add one more line:

```yaml
      gh api -H 'Accept: application/vnd.github.raw+json' \
        "repos/${wf_source}/contents/scripts/memory_helpers.sh?ref=${script_ref}" \
        > "scripts/memory_helpers.sh" 2>/dev/null || echo "::warning::Could not fetch memory_helpers.sh"
```

For `issue_pr_status.yml` (which has no existing fetch step), create a new step that fetches `ai_memory.py`, `ai_memory_lib.py`, and `memory_helpers.sh`.

---

## Shared Pattern: Memory Retrieval Injection

Memory context must be injected **between** the static prefix and the dynamic per-issue content in the prompt. This preserves LLM prompt-prefix caching (the static prefix is identical across runs).

### Current prompt structure (all core workflows)

```
cat ./pre_assembled_static.txt       ← STATIC (cacheable)
<dynamic issue/plan/implementation context>  ← DYNAMIC (per-run)
```

### New prompt structure

```
cat ./pre_assembled_static.txt       ← STATIC (cacheable, unchanged)
cat "${RUNTIME_DIR}/memory_context.txt"  ← MEMORY (per-issue, varies)
<dynamic issue/plan/implementation context>  ← DYNAMIC (per-run)
```

### Retrieve step template

Insert this step **after** "Pre-assemble static context" and **before** "Run Codex" in each workflow. The `Run Codex` step's prompt builder `cat` block must be updated to include the memory context file between static and dynamic sections.

```yaml
    - name: Retrieve memory context
      if: <skip-guard> != 'true'
      env:
        OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        jq -r '.body // ""' "${ISSUE_META_FILE}" > "${RUNTIME_DIR}/issue_body.txt"

        memory_retrieve "${RUNTIME_DIR}/memory_context.txt" \
          --role "<role>" \
          --issue-number "${ISSUE_NUMBER}" \
          --issue-title "${ISSUE_TITLE}" \
          --issue-body-file "${RUNTIME_DIR}/issue_body.txt"
```

Then update the "Run Codex" step's prompt block. Currently:

```bash
{
  cat ./pre_assembled_static.txt
  echo
  # ... dynamic content ...
} > "${CODEX_PROMPT_FILE}"
```

Change to:

```bash
{
  cat ./pre_assembled_static.txt
  echo
  # Memory context (injected between static prefix and dynamic content)
  if [ -s "${RUNTIME_DIR}/memory_context.txt" ]; then
    echo "=== AI MEMORY CONTEXT ==="
    cat "${RUNTIME_DIR}/memory_context.txt"
    echo
  fi
  # ... dynamic content (unchanged) ...
} > "${CODEX_PROMPT_FILE}"
```

### Role mapping

| Workflow | Retrieve Role |
|----------|--------------|
| `clarify.yml` | `clarify` |
| `plan.yml` | `planning` |
| `implement.yml` | `implementation` |
| `review_autofix.yml` | `reviewer` |
| `orchestrate.yml` | `planning` |
| `orchestrate_clarify_respond.yml` | `clarify` |

### PR-scoped retrieval (review_autofix.yml)

For `review_autofix.yml`, use `--pr-number` instead of `--issue-number`:

```yaml
    - name: Retrieve memory context
      env:
        OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_retrieve "${RUNTIME_DIR}/memory_context.txt" \
          --role "reviewer" \
          --pr-number "${PR_NUMBER}"
```

---

## Shared Pattern: Processed-Command Idempotency

The `plan.yml` and `implement.yml` workflows are triggered by comment commands (`/answer` and `/approved`). Race conditions can cause duplicate runs. The processed-command system prevents this.

### Pattern

```
1. CHECK  — Has this comment+command been processed before?
2. CLAIM  — Atomically claim it (only one runner wins)
3. <do work>
4. COMPLETE — Mark as done with final status
```

### plan.yml — `/answer` idempotency

Insert **after** "Skip stale /answer comments" step and **before** "Cleanup clarify-phase Telegram messages":

```yaml
    - name: Check and claim /answer command
      if: env.SKIP_PLAN != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        TRIGGER_COMMENT_ID: ${{ github.event.comment.id }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        CHECK_RESULT="$(memory_processed_command_check \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${TRIGGER_COMMENT_ID}" \
          --command "answer")"

        if echo "${CHECK_RESULT}" | jq -e '.exists == true' >/dev/null 2>&1; then
          echo "::notice::/answer comment ${TRIGGER_COMMENT_ID} already processed; skipping."
          echo "SKIP_PLAN=true" >> "$GITHUB_ENV"
          exit 0
        fi

        CLAIM_RESULT="$(memory_processed_command_claim \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${TRIGGER_COMMENT_ID}" \
          --command "answer" \
          --workflow "plan" \
          --actor "${{ github.actor }}" \
          --run-id "${{ github.run_id }}" \
          --run-attempt "${{ github.run_attempt }}")"

        if echo "${CLAIM_RESULT}" | jq -e '.claimed == false' >/dev/null 2>&1; then
          echo "::notice::/answer comment ${TRIGGER_COMMENT_ID} claimed by another run; skipping."
          echo "SKIP_PLAN=true" >> "$GITHUB_ENV"
        fi
```

Insert processed-command-complete **after** "Post implementation plan" and "Post clarification questions":

```yaml
    - name: Complete /answer command (plan posted)
      if: env.SKIP_PLAN != 'true' && steps.parse_plan.outputs.needs_clarification != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        TRIGGER_COMMENT_ID: ${{ github.event.comment.id }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_processed_command_complete \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${TRIGGER_COMMENT_ID}" \
          --command "answer" \
          --status "completed"

    - name: Complete /answer command (needs clarification)
      if: env.SKIP_PLAN != 'true' && steps.parse_plan.outputs.needs_clarification == 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        TRIGGER_COMMENT_ID: ${{ github.event.comment.id }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_processed_command_complete \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${TRIGGER_COMMENT_ID}" \
          --command "answer" \
          --status "needs_clarification"
```

### implement.yml — `/approved` idempotency

Insert **after** "Validate approval phase label" and **before** "Cleanup plan-phase Telegram messages":

```yaml
    - name: Check and claim /approved command
      if: env.SKIP_IMPLEMENT != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        CHECK_RESULT="$(memory_processed_command_check \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${APPROVAL_COMMENT_ID}" \
          --command "approved")"

        if echo "${CHECK_RESULT}" | jq -e '.exists == true' >/dev/null 2>&1; then
          echo "::notice::/approved comment ${APPROVAL_COMMENT_ID} already processed; skipping."
          echo "SKIP_IMPLEMENT=true" >> "$GITHUB_ENV"
          exit 0
        fi

        CLAIM_RESULT="$(memory_processed_command_claim \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${APPROVAL_COMMENT_ID}" \
          --command "approved" \
          --workflow "implement" \
          --actor "${{ github.actor }}" \
          --run-id "${{ github.run_id }}" \
          --run-attempt "${{ github.run_attempt }}")"

        if echo "${CLAIM_RESULT}" | jq -e '.claimed == false' >/dev/null 2>&1; then
          echo "::notice::/approved comment ${APPROVAL_COMMENT_ID} claimed by another run; skipping."
          echo "SKIP_IMPLEMENT=true" >> "$GITHUB_ENV"
        fi
```

Insert processed-command-complete **after** "Mark issue done after implementation PR":

```yaml
    - name: Complete /approved command
      if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'true' && steps.create_pr.outputs.pr_url != ''
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_processed_command_complete \
          --issue-number "${ISSUE_NUMBER}" \
          --comment-id "${APPROVAL_COMMENT_ID}" \
          --command "approved" \
          --status "completed" \
          --metadata-json "{\"pr_url\": \"${{ steps.create_pr.outputs.pr_url }}\"}"
```

---

## Phase 1A: `clarify.yml`

### Step 1 — Record run start

Insert **after** "Set clarification phase label" (~line 152), **before** "Build context file" (~line 169):

```yaml
    - name: Record clarification run start
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "clarify" \
          --event-type "phase_started" \
          --status "ok" \
          --message "Clarification phase started for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 2 — Retrieve memory context

Insert **after** "Pre-assemble static context" (~line 274), **before** "Run Codex" (~line 281). See [Shared Pattern: Memory Retrieval Injection](#shared-pattern-memory-retrieval-injection) for the step template. Use `--role clarify`.

Then update the "Run Codex" step's prompt builder to inject memory context between static and dynamic sections (see shared pattern).

### Step 3 — Record candidate

Insert **after** "Post clarification questions" (~line 547):

```yaml
    - name: Record clarification candidate (needs clarification)
      if: steps.parse_codex.outputs.needs_clarification == 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        DETAILS="$(cat "${QUESTIONS_FILE}" | head -c 12000)"

        memory_record_candidate \
          --category "decisions" \
          --summary "Issue #${ISSUE_NUMBER} needs clarification" \
          --details "${DETAILS}" \
          --confidence "0.90" \
          --workflow "clarify" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

Insert **after** "Post clear-to-proceed comment" (~line 566):

```yaml
    - name: Record clarification candidate (clear)
      if: steps.parse_codex.outputs.needs_clarification != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_candidate \
          --category "decisions" \
          --summary "Issue #${ISSUE_NUMBER} is clear — no clarification needed" \
          --details "The issue provided sufficient context for planning. Auto-posted /answer." \
          --confidence "0.85" \
          --workflow "clarify" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 4 — Record run end

Insert **before** "Comment on issue failure" (~line 605) (success path):

```yaml
    - name: Record clarification run completed
      if: success()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        OUTCOME="clear"
        if [ "${{ steps.parse_codex.outputs.needs_clarification }}" = "true" ]; then
          OUTCOME="needs_clarification"
        fi

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "clarify" \
          --event-type "phase_completed" \
          --status "ok" \
          --message "Clarification completed: ${OUTCOME}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}" \
          --metadata-json "{\"outcome\": \"${OUTCOME}\"}"
```

In the failure handler section (alongside existing "Comment on issue failure"):

```yaml
    - name: Record clarification run failed
      if: failure() || cancelled()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "clarify" \
          --event-type "phase_failed" \
          --status "error" \
          --message "Clarification failed or cancelled for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

---

## Phase 1B: `plan.yml`

### Step 1 — Processed command check+claim

Insert **after** "Skip stale /answer comments" (~line 229), **before** "Cleanup clarify-phase Telegram messages" (~line 258). See [Shared Pattern: Processed-Command Idempotency](#planyml--answer-idempotency) for the full YAML.

### Step 2 — Record run start

Insert **after** "Validate planning phase label" (~line 176), **before** "Skip when issue already has a PR" (~line 204):

```yaml
    - name: Record planning run start
      if: env.SKIP_PLAN != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "plan" \
          --event-type "phase_started" \
          --status "ok" \
          --message "Planning phase started for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 3 — Retrieve memory context

Insert **after** "Pre-assemble static context" (~line 386), **before** "Run Codex planning" (~line 420). Use `--role planning`. See shared retrieve pattern.

Update the "Run Codex planning" prompt builder to inject memory context between static and dynamic sections.

### Step 4 — Record candidate (plan posted)

Insert **after** "Post implementation plan" (~line 677), **before** "Auto-approve clear plan" (~line 711):

```yaml
    - name: Record plan candidate
      if: env.SKIP_PLAN != 'true' && steps.parse_plan.outputs.needs_clarification != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        PLAN_DETAILS="$(cat "${CODEX_OUTPUT_FILE}" | head -c 12000)"

        memory_record_candidate \
          --category "decisions" \
          --summary "Implementation plan for issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}" \
          --details "${PLAN_DETAILS}" \
          --confidence "0.80" \
          --workflow "plan" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 5 — Record candidate (re-clarification)

Insert **after** "Post clarification questions" (~line 634):

```yaml
    - name: Record plan re-clarification candidate
      if: env.SKIP_PLAN != 'true' && steps.parse_plan.outputs.needs_clarification == 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        DETAILS="$(cat "${CODEX_OUTPUT_FILE}" | head -c 12000)"

        memory_record_candidate \
          --category "decisions" \
          --summary "Plan needs re-clarification for issue #${ISSUE_NUMBER}" \
          --details "${DETAILS}" \
          --confidence "0.75" \
          --workflow "plan" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 6 — Processed command complete

Insert **after** "Auto-approve clear plan" (~line 711) and after "Telegram clarification notification" (~line 659). See shared idempotency pattern for full YAML.

### Step 7 — Record run end

Same pattern as clarify.yml — success and failure handlers:

```yaml
    - name: Record planning run completed
      if: env.SKIP_PLAN != 'true' && success()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        OUTCOME="plan_posted"
        if [ "${{ steps.parse_plan.outputs.needs_clarification }}" = "true" ]; then
          OUTCOME="needs_clarification"
        fi

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "plan" \
          --event-type "phase_completed" \
          --status "ok" \
          --message "Planning completed: ${OUTCOME}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}" \
          --metadata-json "{\"outcome\": \"${OUTCOME}\"}"

    - name: Record planning run failed
      if: failure() || cancelled()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "plan" \
          --event-type "phase_failed" \
          --status "error" \
          --message "Planning failed or cancelled for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

---

## Phase 1C: `implement.yml`

### Step 1 — Processed command check+claim

Insert **after** "Validate approval phase label" (~line 273), **before** "Cleanup plan-phase Telegram messages" (~line 288). Uses `APPROVAL_COMMENT_ID` (already set as env var at line 46). See [Shared Pattern: Processed-Command Idempotency](#implementyml--approved-idempotency) for the full YAML.

### Step 2 — Record run start

Insert **after** the processed-command claim step:

```yaml
    - name: Record implementation run start
      if: env.SKIP_IMPLEMENT != 'true'
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "implement" \
          --event-type "phase_started" \
          --status "ok" \
          --message "Implementation phase started for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 3 — Retrieve memory context

Insert **after** "Pre-assemble static context" (~line 401), **before** "Run Codex implementation" (~line 439). Use `--role implementation`. See shared retrieve pattern.

Update the "Run Codex implementation" prompt builder to inject memory context between static and dynamic sections.

### Step 4 — Record candidate (implementation completed)

Insert **after** "Mark issue done after implementation PR" (~line 915):

```yaml
    - name: Record implementation candidate
      if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'true' && steps.create_pr.outputs.pr_url != ''
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        # Summarize what was implemented
        CHANGED_FILES="$(git diff --name-only HEAD~1 2>/dev/null | head -20 || echo "unknown")"
        DETAILS="Implementation completed for issue #${ISSUE_NUMBER}.
        PR: ${{ steps.create_pr.outputs.pr_url }}
        Branch: ${{ steps.create_pr.outputs.branch_name }}
        Files changed:
        ${CHANGED_FILES}"

        memory_record_candidate \
          --category "task_summaries" \
          --summary "Implemented issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}" \
          --details "${DETAILS}" \
          --confidence "0.85" \
          --workflow "implement" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}" \
          --pr-number "${{ steps.create_pr.outputs.pr_number }}" \
          --source-refs "${{ steps.create_pr.outputs.pr_url }}"
```

### Step 5 — Processed command complete

Insert **after** the record candidate step. See shared idempotency pattern for full YAML.

### Step 6 — Finalize task lineage

Insert **after** processed-command-complete. Uses `in_progress` because the PR is created but not yet merged — `issue_pr_status.yml` handles the final `merged`/`closed` transition.

```yaml
    - name: Finalize task lineage (PR created)
      if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'true' && steps.create_pr.outputs.pr_url != ''
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_finalize_task \
          --issue-number "${ISSUE_NUMBER}" \
          --issue-url "${ISSUE_URL}" \
          --final-state "in_progress" \
          --workflow "implement" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --pr-number "${{ steps.create_pr.outputs.pr_number }}" \
          --pr-url "${{ steps.create_pr.outputs.pr_url }}"
```

### Step 7 — Record run end

Same pattern as clarify/plan — success and failure handlers. Include PR URL in success metadata:

```yaml
    - name: Record implementation run completed
      if: env.SKIP_IMPLEMENT != 'true' && success()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        PR_URL="${{ steps.create_pr.outputs.pr_url }}"
        DID_COMMIT="${{ steps.commit_changes.outputs.did_commit }}"

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "implement" \
          --event-type "phase_completed" \
          --status "ok" \
          --message "Implementation completed for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}" \
          --metadata-json "{\"pr_url\": \"${PR_URL}\", \"did_commit\": \"${DID_COMMIT}\"}"

    - name: Record implementation run failed
      if: failure() || cancelled()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "implement" \
          --event-type "phase_failed" \
          --status "error" \
          --message "Implementation failed or cancelled for issue #${ISSUE_NUMBER}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

---

## Phase 2A: `issue_pr_status.yml`

This workflow currently has **no checkout, no Python setup, and no script fetching**. New steps must be added to support `finalize-task`.

### New steps to add before existing "Update linked issue labels" step

```yaml
    steps:
      - name: Checkout repository
        uses: actions/checkout@v5
        with:
          fetch-depth: 0
          token: ${{ secrets.GH_PAT }}

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Fetch memory scripts
        env:
          GH_TOKEN: ${{ secrets.GH_PAT }}
        run: |
          set -euo pipefail
          wf_source="shubhodeep1/coding-workflows"
          if [ "${{ github.repository }}" = "${wf_source}" ]; then
            script_ref="${{ github.sha }}"
          else
            script_ref="stable"
          fi
          mkdir -p scripts
          for script in ai_memory.py ai_memory_lib.py memory_helpers.sh; do
            gh api -H 'Accept: application/vnd.github.raw+json' \
              "repos/${wf_source}/contents/scripts/${script}?ref=${script_ref}" \
              > "scripts/${script}" 2>/dev/null || echo "::warning::Could not fetch ${script}"
          done
```

### Finalize task step

Insert **after** the existing "Update linked issue labels" step (~line 68) and **before** "Send PR merged Telegram alert" (~line 75). The `finalize-task` call goes inside the existing issue-number loop logic:

```yaml
      - name: Finalize task lineage on PR close
        env:
          GH_TOKEN: ${{ secrets.GH_PAT }}
          PYTHONDONTWRITEBYTECODE: "1"
          PR_NUMBER: ${{ github.event.pull_request.number }}
          PR_URL: ${{ github.event.pull_request.html_url }}
          PR_MERGED: ${{ github.event.pull_request.merged }}
        run: |
          set -euo pipefail
          source scripts/memory_helpers.sh

          if [ "${PR_MERGED}" = "true" ]; then
            FINAL_STATE="merged"
          else
            FINAL_STATE="closed"
          fi

          if [ -z "${LINKED_ISSUE_NUMBERS:-}" ]; then
            echo "No linked issues; skipping finalize-task."
            exit 0
          fi

          while IFS= read -r issue_number; do
            [ -n "${issue_number}" ] || continue
            ISSUE_URL="https://github.com/${{ github.repository }}/issues/${issue_number}"
            echo "Finalizing task lineage for issue #${issue_number} (${FINAL_STATE})"

            memory_finalize_task \
              --issue-number "${issue_number}" \
              --issue-url "${ISSUE_URL}" \
              --final-state "${FINAL_STATE}" \
              --workflow "issue_pr_status" \
              --run-id "${{ github.run_id }}" \
              --actor "${{ github.actor }}" \
              --pr-number "${PR_NUMBER}" \
              --pr-url "${PR_URL}" || {
              echo "::warning::finalize-task failed for issue #${issue_number}"
            }
          done <<< "${LINKED_ISSUE_NUMBERS}"
```

**Note:** `LINKED_ISSUE_NUMBERS` is exported to `$GITHUB_ENV` by the existing "Update linked issue labels" step (line 71-73), so it is available in subsequent steps.

---

## Phase 2B: `review_autofix.yml`

### Step 1 — Retrieve memory context

Insert **after** "Pre-assemble static context" (~line 227), **before** "Collect PR metadata" (~line 261). Use `--role reviewer --pr-number`. See [PR-scoped retrieval](#pr-scoped-retrieval-review_autofixyml) for the step template.

Update the reviewer prompt builder to inject memory context between static and dynamic sections.

### Step 2 — Record candidate (review findings)

Insert **after** "Filter reviewer issues by consensus" (~line 1394):

```yaml
    - name: Record review findings candidate
      if: success()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
        PR_NUMBER: ${{ github.event.pull_request.number }}
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        # Collect consensus review findings
        FINDINGS_FILE="${RUNTIME_DIR}/consensus_issues.txt"
        if [ ! -s "${FINDINGS_FILE:-}" ]; then
          echo "No consensus findings to record."
          exit 0
        fi

        DETAILS="$(cat "${FINDINGS_FILE}" | head -c 12000)"
        ISSUE_COUNT="$(wc -l < "${FINDINGS_FILE}" 2>/dev/null || echo 0)"

        memory_record_candidate \
          --category "patterns" \
          --summary "Review findings for PR #${PR_NUMBER}: ${ISSUE_COUNT} consensus issues" \
          --details "${DETAILS}" \
          --confidence "0.80" \
          --workflow "review_autofix" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --pr-number "${PR_NUMBER}"
```

### Step 3 — Record run events

Insert run start **after** "Initialize runtime workspace" (~line 145):

```yaml
    - name: Record review run start
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
        PR_NUMBER: ${{ github.event.pull_request.number }}
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "review_autofix" \
          --event-type "phase_started" \
          --status "ok" \
          --message "Review started for PR #${PR_NUMBER}" \
          --actor "${{ github.actor }}" \
          --pr-number "${PR_NUMBER}"
```

Insert run end (success + failure) near the end of the job, before any cleanup steps:

```yaml
    - name: Record review run completed
      if: success()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
        PR_NUMBER: ${{ github.event.pull_request.number }}
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "review_autofix" \
          --event-type "phase_completed" \
          --status "ok" \
          --message "Review completed for PR #${PR_NUMBER}" \
          --actor "${{ github.actor }}" \
          --pr-number "${PR_NUMBER}"

    - name: Record review run failed
      if: failure() || cancelled()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
        PR_NUMBER: ${{ github.event.pull_request.number }}
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "review_autofix" \
          --event-type "phase_failed" \
          --status "error" \
          --message "Review failed or cancelled for PR #${PR_NUMBER}" \
          --actor "${{ github.actor }}" \
          --pr-number "${PR_NUMBER}"
```

---

## Phase 3A: `orchestrate.yml`

### Step 1 — Record run start

Insert **after** "Validate required environment variables" (~line 95), **before** "Create runtime workspace" (~line 106):

```yaml
    - name: Record orchestration run start
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "orchestrate" \
          --event-type "orchestration_started" \
          --status "ok" \
          --message "Orchestration started" \
          --actor "${{ github.actor }}"
```

### Step 2 — Retrieve memory context

Insert **after** "Pre-assemble static context" (~line 219), **before** "Build Codex prompt" (~line 246). Use `--role planning`. No `--issue-number` since the tracking issue hasn't been created yet. Pass `--issue-title` from the project description input if available.

### Step 3 — Record candidate (decomposition)

Insert **after** "Build and post initial state" (~line 482), **before** "Dispatch Wave 1" (~line 528):

```yaml
    - name: Record decomposition candidate
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
        TRACKING_ISSUE_NUMBER: ${{ steps.tracking_issue.outputs.tracking_issue_number }}
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        ISSUE_COUNT="$(jq '.issues | length' "${DECOMPOSITION_FILE}" 2>/dev/null || echo 0)"
        PROJECT_TITLE="$(jq -r '.project_title' "${DECOMPOSITION_FILE}" 2>/dev/null || echo "unknown")"
        DETAILS="$(jq -c '{project_title: .project_title, issues: [.issues[] | {id, title, priority}], dependency_edges}' "${DECOMPOSITION_FILE}" | head -c 12000)"

        memory_record_candidate \
          --category "decisions" \
          --summary "Decomposed project '${PROJECT_TITLE}' into ${ISSUE_COUNT} issues (tracking #${TRACKING_ISSUE_NUMBER})" \
          --details "${DETAILS}" \
          --confidence "0.80" \
          --workflow "orchestrate" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${TRACKING_ISSUE_NUMBER}"
```

### Step 4 — Record run end

Insert in the "Write run summary" step (~line 562) or immediately after it:

```yaml
    - name: Record orchestration run end
      if: always()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
        TRACKING_ISSUE_NUMBER: ${{ steps.tracking_issue.outputs.tracking_issue_number }}
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        if [ "${{ job.status }}" = "success" ]; then
          EVENT_TYPE="orchestration_completed"
          STATUS="ok"
          MSG="Orchestration completed. Tracking: #${TRACKING_ISSUE_NUMBER:-unknown}"
        else
          EVENT_TYPE="orchestration_failed"
          STATUS="error"
          MSG="Orchestration failed (${{ job.status }})"
        fi

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "orchestrate" \
          --event-type "${EVENT_TYPE}" \
          --status "${STATUS}" \
          --message "${MSG}" \
          --actor "${{ github.actor }}" \
          --issue-number "${TRACKING_ISSUE_NUMBER:-0}"
```

---

## Phase 3B: `orchestrate_poll.yml`

Memory calls go in the YAML wrapper only (not inside `orchestrate_poll_process.sh`).

### Step 1 — Record run start

Insert **before** "Process each tracking issue" (~line 186):

```yaml
    - name: Record poll run start
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "orchestrate_poll" \
          --event-type "poll_started" \
          --status "ok" \
          --message "Orchestrator poll cycle started" \
          --actor "${{ github.actor }}"
```

### Step 2 — Record run end

Insert **after** "Write run summary" (~line 206):

```yaml
    - name: Record poll run end
      if: always()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        if [ "${{ job.status }}" = "success" ]; then
          STATUS="ok"; MSG="Poll cycle completed"
        else
          STATUS="error"; MSG="Poll cycle failed (${{ job.status }})"
        fi

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "orchestrate_poll" \
          --event-type "poll_completed" \
          --status "${STATUS}" \
          --message "${MSG}" \
          --actor "${{ github.actor }}"
```

---

## Phase 3C: `orchestrate_clarify_respond.yml`

### Step 1 — Retrieve memory context

Insert **before** "Build prompt and run Codex" (~line 212). Use `--role clarify --issue-number "${ISSUE_NUMBER}"`. See shared retrieve pattern.

Update the Codex prompt builder to inject memory context.

### Step 2 — Record candidate (auto-answer)

Insert **after** "Parse and post answer" (~line 258):

```yaml
    - name: Record auto-answer candidate
      if: success()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        ANSWER_TEXT="$(cat "${RUNTIME_DIR}/answer_output.txt" 2>/dev/null | head -c 12000 || echo "Auto-answer posted")"

        memory_record_candidate \
          --category "decisions" \
          --summary "Auto-answered clarification for issue #${ISSUE_NUMBER}" \
          --details "${ANSWER_TEXT}" \
          --confidence "0.75" \
          --workflow "orchestrate_clarify_respond" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}" \
          --issue-number "${ISSUE_NUMBER}"
```

### Step 3 — Record run events

Same start/end pattern. Insert run start after "Check orchestrator metadata" (~line 52), run end before failure handler (~line 318).

---

## Phase 4: `validate.yml`

### Step 1 — Record candidate (validation result)

Insert **after** "Collect validation status" (~line 171):

```yaml
    - name: Record validation candidate
      if: always()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        STATUS_FILE="${RUNTIME_DIR}/validation_status.json"
        if [ ! -f "${STATUS_FILE}" ]; then
          echo "No validation status file; skipping candidate."
          exit 0
        fi

        VAL_STATUS="$(jq -r '.status // "unknown"' "${STATUS_FILE}")"
        VAL_SUMMARY="$(jq -r '.summary // "No summary"' "${STATUS_FILE}" | head -c 12000)"

        memory_record_candidate \
          --category "patterns" \
          --summary "Validation ${VAL_STATUS} for tracking issue ${{ inputs.tracking_issue }}" \
          --details "${VAL_SUMMARY}" \
          --confidence "0.85" \
          --workflow "validate" \
          --run-id "${{ github.run_id }}" \
          --actor "${{ github.actor }}"
```

### Step 2 — Record run events

Insert run start **after** "Setup shared runtime" (~line 68), run end **after** "Write run summary" (~line 230):

```yaml
    - name: Record validation run start
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "validate" \
          --event-type "validation_started" \
          --status "ok" \
          --message "Validation started for tracking issue ${{ inputs.tracking_issue }}" \
          --actor "${{ github.actor }}"

    - name: Record validation run end
      if: always()
      env:
        GH_TOKEN: ${{ secrets.GH_PAT }}
        PYTHONDONTWRITEBYTECODE: "1"
      run: |
        set -euo pipefail
        source scripts/memory_helpers.sh

        if [ "${{ job.status }}" = "success" ]; then
          STATUS="ok"; MSG="Validation completed"
        else
          STATUS="error"; MSG="Validation failed (${{ job.status }})"
        fi

        memory_record_run_event \
          --run-id "${{ github.run_id }}" \
          --workflow "validate" \
          --event-type "validation_completed" \
          --status "${STATUS}" \
          --message "${MSG}" \
          --actor "${{ github.actor }}"
```

---

## Implementation Order

Phases must be implemented in order. Each phase depends on the previous.

| Phase | Files Modified | New Steps | Est. Lines | Depends On |
|-------|---------------|-----------|------------|------------|
| **P0** | `scripts/memory_helpers.sh` (new), fetch blocks in all workflows | 1 new file + fetch line per workflow | ~80 | Nothing |
| **P1A** | `.github/workflows/clarify.yml` | 5 steps + prompt edit | ~80 | P0 |
| **P1B** | `.github/workflows/plan.yml` | 8 steps + prompt edit | ~120 | P0 |
| **P1C** | `.github/workflows/implement.yml` | 8 steps + prompt edit | ~130 | P0 |
| **P2A** | `.github/workflows/issue_pr_status.yml` | 4 steps (checkout + python + fetch + finalize) | ~70 | P0 |
| **P2B** | `.github/workflows/review_autofix.yml` | 4 steps + prompt edit | ~80 | P0 |
| **P3A** | `.github/workflows/orchestrate.yml` | 4 steps + prompt edit | ~80 | P0 |
| **P3B** | `.github/workflows/orchestrate_poll.yml` | 2 steps | ~30 | P0 |
| **P3C** | `.github/workflows/orchestrate_clarify_respond.yml` | 3 steps + prompt edit | ~50 | P0 |
| **P4** | `.github/workflows/validate.yml` | 3 steps | ~50 | P0 |
| **Total** | **11 files** | **~42 steps** | **~770 lines** | |

## Files Changed Summary

| File | Action | Description |
|------|--------|-------------|
| `scripts/memory_helpers.sh` | **CREATE** | Fail-open/closed shell wrappers for memory CLI |
| `.github/workflows/clarify.yml` | EDIT | Add retrieve, run events, candidate recording, fetch |
| `.github/workflows/plan.yml` | EDIT | Add retrieve, run events, candidates, idempotency, fetch |
| `.github/workflows/implement.yml` | EDIT | Add retrieve, run events, candidates, idempotency, lineage, fetch |
| `.github/workflows/issue_pr_status.yml` | EDIT | Add checkout, Python, script fetch, finalize-task |
| `.github/workflows/review_autofix.yml` | EDIT | Add retrieve, run events, candidate recording, fetch |
| `.github/workflows/orchestrate.yml` | EDIT | Add retrieve, run events, candidate recording, fetch |
| `.github/workflows/orchestrate_poll.yml` | EDIT | Add run events, fetch |
| `.github/workflows/orchestrate_clarify_respond.yml` | EDIT | Add retrieve, run events, candidate recording, fetch |
| `.github/workflows/validate.yml` | EDIT | Add run events, candidate recording, fetch |

## Post-Implementation: README.md Updates

After all phases are implemented, update `README.md`:

1. Add `AI_MEMORY_ENABLED` to the Variables table (default: `true`, used by: all workflows).
2. Add a "Memory System" section under Overview explaining that workflows persist decisions, plans, and implementation summaries to a dedicated `ai-memory` branch.
3. Note that `OPENROUTER_API_KEY` is also used for memory keyword extraction (already listed but description should be updated).

## Testing Strategy

1. **Smoke test:** Trigger a clarify workflow on a test issue. Verify the `ai-memory` branch is created with a run ledger entry.
2. **Idempotency test:** Post `/answer` twice rapidly on the same issue. Verify only one plan run proceeds.
3. **Lineage test:** Complete a full issue lifecycle (clarify → plan → implement → merge). Verify `task_lineage.v1.json` shows all state transitions.
4. **Kill switch test:** Set `AI_MEMORY_ENABLED=false` as a repo variable. Verify all workflows run normally with no memory operations.
5. **Fail-open test:** Temporarily break `ai_memory.py` (e.g., syntax error). Verify clarify/plan/implement workflows still complete (memory steps warn but don't fail the job).
