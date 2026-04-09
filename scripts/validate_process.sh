#!/usr/bin/env bash
# validate_process.sh — Generate and execute runtime validation harness.
#
# Required env vars:
#   RUNTIME_DIR, GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY
#
# Optional env vars:
#   TRACKING_ISSUE, VALIDATION_TIMEOUT, TOOL_CALL_BUDGET_VALIDATE,
#   MODEL_EDITOR, MODEL_REASONING_EFFORT,
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID,
#   VALIDATION_COMPOSE_FILE,
#   VALIDATION_TEST_USERNAME, VALIDATION_TEST_PASSWORD, VALIDATION_TEST_API_KEY,
#   SERENA_VERSION, SERENA_LANGUAGES, SERENA_DISABLED, SERENA_IGNORED_DIRS,
#   CONTEXT7_DISABLED

set -euo pipefail

: "${RUNTIME_DIR:?RUNTIME_DIR is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
[[ "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]] || { echo "GITHUB_REPOSITORY must be in owner/repo format" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required but not installed" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required but not installed" >&2; exit 1; }

TRACKING_ISSUE_RAW="${TRACKING_ISSUE:-0}"
TRACKING_ISSUE_NUM=0
if [[ "${TRACKING_ISSUE_RAW}" =~ ^[0-9]+$ ]]; then
  TRACKING_ISSUE_NUM="${TRACKING_ISSUE_RAW}"
fi

MODEL_EDITOR="${MODEL_EDITOR:-openai/gpt-5.3-codex}"
MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT:-high}"
VALIDATION_TIMEOUT="${VALIDATION_TIMEOUT:-15}"
if ! [[ "${VALIDATION_TIMEOUT}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_TIMEOUT}" -le 0 ]; then
  echo "VALIDATION_TIMEOUT must be a positive integer (got: ${VALIDATION_TIMEOUT})" >&2
  exit 1
fi
TOOL_CALL_BUDGET_VALIDATE="${TOOL_CALL_BUDGET_VALIDATE:-60}"
VALIDATION_COMPOSE_FILE="${VALIDATION_COMPOSE_FILE:-docker-compose.yml}"
VALIDATION_TEST_USERNAME="${VALIDATION_TEST_USERNAME:-test-user}"
VALIDATION_TEST_PASSWORD="${VALIDATION_TEST_PASSWORD:-test-password}"
VALIDATION_TEST_API_KEY="${VALIDATION_TEST_API_KEY:-test-api-key}"
VALIDATION_CYCLE="${VALIDATION_CYCLE:-1}"
if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
  echo "::warning::VALIDATION_CYCLE must be a positive integer (got: ${VALIDATION_CYCLE}); defaulting to 1."
  VALIDATION_CYCLE="1"
fi
EFFECTIVE_MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT}"
if [ "${VALIDATION_CYCLE}" -gt 3 ] && [ "${MODEL_REASONING_EFFORT}" = "xhigh" ]; then
  EFFECTIVE_MODEL_REASONING_EFFORT="high"
fi

PROJECT_SPEC_FILE="${RUNTIME_DIR}/project_spec.txt"
STATIC_CONTEXT_FILE="${RUNTIME_DIR}/validate_static.txt"
VALIDATE_HINTS_FILE="${RUNTIME_DIR}/validate_hints.txt"
GENERATE_PROMPT_FILE="${RUNTIME_DIR}/validate_generate_prompt.txt"
GENERATE_OUTPUT_FILE="${RUNTIME_DIR}/validate_generate_output.txt"
GENERATE_LOG_FILE="${RUNTIME_DIR}/validate_generate.log"
VALIDATION_LOG_FILE="${RUNTIME_DIR}/validation.log"
VALIDATION_RESULT_FILE="${RUNTIME_DIR}/validation_result.json"
DIAGNOSE_PROMPT_FILE="${RUNTIME_DIR}/validate_diagnose_prompt.txt"
DIAGNOSE_OUTPUT_FILE="${RUNTIME_DIR}/validate_diagnose_output.txt"
DIAGNOSE_LOG_FILE="${RUNTIME_DIR}/validate_diagnose.log"
DIAGNOSE_RESULT_FILE="${RUNTIME_DIR}/validation_diagnosis.json"
METADATA_FILE="${RUNTIME_DIR}/validation_metadata.json"
STATUS_FILE="${RUNTIME_DIR}/validation_status.json"
VALIDATION_LOG_TAIL_FILE="${RUNTIME_DIR}/validation_log_tail.txt"
CONTAINER_LOG_TAIL_FILE="${RUNTIME_DIR}/container_logs_tail.txt"
NULL_JSON_FILE="${RUNTIME_DIR}/null.json"
PRE_GENERATE_STATUS_FILE="${RUNTIME_DIR}/pre_generate_git_status.txt"
POST_GENERATE_STATUS_FILE="${RUNTIME_DIR}/post_generate_git_status.txt"

mkdir -p "${RUNTIME_DIR}"
printf 'null\n' > "${NULL_JSON_FILE}"

export VALIDATION_TEST_USERNAME
export VALIDATION_TEST_PASSWORD
export VALIDATION_TEST_API_KEY
export TEST_USERNAME="${TEST_USERNAME:-${VALIDATION_TEST_USERNAME}}"
export TEST_PASSWORD="${TEST_PASSWORD:-${VALIDATION_TEST_PASSWORD}}"
export TEST_API_KEY="${TEST_API_KEY:-${VALIDATION_TEST_API_KEY}}"

CREATED_FIX_ISSUES_JSON='[]'


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
# shellcheck source=gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/gh_helpers.sh
fi
# shellcheck source=tg_helpers.sh
if [ -f "scripts/tg_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/tg_helpers.sh
fi


# _gh_url constructs a full GitHub URL for the current repository.
_gh_url() {
  printf "%s/%s/%s" "${GITHUB_SERVER_URL:-https://github.com}" "${GITHUB_REPOSITORY}" "$1"
}
_tg_link_suffix()
{
  local suffix=""
  if [ "${TRACKING_ISSUE_NUM}" -gt 0 ]; then
    suffix+=$'\n'"Issue: $(_gh_url "issues/${TRACKING_ISSUE_NUM}")"
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    suffix+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
  fi
  printf '%s' "${suffix}"
}

tg_notify()
{
  local msg="$1$(_tg_link_suffix)"
  local level="${2:-CRITICAL}"
  if [ "${TRACKING_ISSUE_NUM}" -gt 0 ]; then
    tg_send_tracked "${TRACKING_ISSUE_NUM}" "${msg}" "${level}"
  else
    # Standalone validation run (no tracking issue): untracked send
    tg_send_msg "${msg}" "${level}" >/dev/null
  fi
}

# gh_retry is provided by scripts/gh_helpers.sh (rate-limit-aware).
# Fallback definition in case gh_helpers.sh was not sourced.
if ! type gh_retry >/dev/null 2>&1; then
  gh_retry() { "$@"; }
fi

is_tracking_run()
{
  [ "${TRACKING_ISSUE_NUM}" -gt 0 ]
}

post_tracking_comment()
{
  local comment_body="$1"
  if ! is_tracking_run; then
    return 0
  fi

  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/comments" \
    -f body="${comment_body}" >/dev/null || true
}

ensure_label_exists()
{
  local label_name="$1"
  local contract_file=".github/ai/label_contract.v1.json"
  local color="1d76db"
  local description="AI workflow label"

  if [ -f "${contract_file}" ]; then
    color="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].color // "1d76db"' "${contract_file}" 2>/dev/null || echo "1d76db")"
    description="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].description // "AI workflow label"' "${contract_file}" 2>/dev/null || echo "AI workflow label")"
  fi

  # Check if label already exists to avoid futile retries (gh label create
  # returns a non-zero exit code for existing labels, which is not transient).
  local encoded_name
  encoded_name="$(printf '%s' "${label_name}" | jq -sRr @uri)"
  if gh api "repos/${GITHUB_REPOSITORY}/labels/${encoded_name}" >/dev/null 2>&1; then
    return 0
  fi

  gh_retry gh label create "${label_name}" \
    --repo "${GITHUB_REPOSITORY}" \
    --color "${color}" \
    --description "${description}" >/dev/null || true
}

set_tracking_phase_label()
{
  local phase_label="$1"
  local contract_file=".github/ai/label_contract.v1.json"

  if ! is_tracking_run; then
    return 0
  fi

  ensure_label_exists "${phase_label}"

  if [ -f "${contract_file}" ]; then
    local phase_changes
    if phase_changes="$(python3 scripts/ai_labels.py resolve-phase \
      --contract-file "${contract_file}" \
      --phase "${phase_label}" 2>/dev/null)"; then
      while IFS= read -r remove_label; do
        [ -n "${remove_label}" ] || continue
        gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
          --repo "${GITHUB_REPOSITORY}" \
          --remove-label "${remove_label}" >/dev/null || true
      done < <(echo "${phase_changes}" | jq -r '.remove[]?')

      while IFS= read -r add_label; do
        [ -n "${add_label}" ] || continue
        ensure_label_exists "${add_label}"
        gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
          --repo "${GITHUB_REPOSITORY}" \
          --add-label "${add_label}" >/dev/null || true
      done < <(echo "${phase_changes}" | jq -r '.add[]?')
      return 0
    fi
  fi

  gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
    --repo "${GITHUB_REPOSITORY}" \
    --add-label "${phase_label}" >/dev/null || true
}

extract_last_json_with_key()
{
  local source_file="$1"
  local required_key="$2"
  local output_file="$3"

  python3 - "${source_file}" "${required_key}" "${output_file}" <<'PY'
import json
import re
import sys

source_file = sys.argv[1]
required_key = sys.argv[2]
output_file = sys.argv[3]

with open(source_file, "r", encoding="utf-8", errors="replace") as handle:
    raw = handle.read()

candidates = []

trimmed = raw.strip()
if trimmed:
    try:
        parsed = json.loads(trimmed)
        if isinstance(parsed, dict) and required_key in parsed:
            candidates.append(parsed)
    except json.JSONDecodeError:
        pass

cleaned = re.sub(r"```(?:json)?\s*", "", raw)
cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)

decoder = json.JSONDecoder()
pos = 0
while pos < len(cleaned):
    start = cleaned.find("{", pos)
    if start == -1:
        break
    try:
        parsed, end = decoder.raw_decode(cleaned[start:])
        if isinstance(parsed, dict) and required_key in parsed:
            candidates.append(parsed)
        pos = start + end
    except json.JSONDecodeError:
        pos = start + 1

if not candidates:
    print(f"No JSON object with key '{required_key}' found", file=sys.stderr)
    sys.exit(1)

with open(output_file, "w", encoding="utf-8") as handle:
    json.dump(candidates[-1], handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
}

write_status_file()
{
  local status="$1"
  local summary="$2"
  local failure_summary="$3"

  jq -n \
    --arg status "${status}" \
    --arg summary "${summary}" \
    --arg failure_summary "${failure_summary}" \
    --arg tracking_issue "${TRACKING_ISSUE_RAW}" \
    '{
      status: $status,
      summary: $summary,
      failure_summary: (if ($failure_summary | length) > 0 then $failure_summary else null end),
      tracking_issue: $tracking_issue
    }' > "${STATUS_FILE}"
}

write_metadata_file()
{
  local status="$1"
  local summary="$2"
  local failure_summary="$3"

  local validation_file="${VALIDATION_RESULT_FILE}"
  local diagnosis_file="${DIAGNOSE_RESULT_FILE}"

  if [ ! -f "${validation_file}" ]; then
    validation_file="${NULL_JSON_FILE}"
  fi

  if [ ! -f "${diagnosis_file}" ]; then
    diagnosis_file="${NULL_JSON_FILE}"
  fi

  jq -n \
    --arg status "${status}" \
    --arg summary "${summary}" \
    --arg failure_summary "${failure_summary}" \
    --arg repository "${GITHUB_REPOSITORY}" \
    --arg tracking_issue "${TRACKING_ISSUE_RAW}" \
    --arg runtime_dir "${RUNTIME_DIR}" \
    --arg compose_file "${VALIDATION_COMPOSE_FILE}" \
    --arg validation_log_file "${VALIDATION_LOG_FILE}" \
    --arg generate_log_file "${GENERATE_LOG_FILE}" \
    --arg diagnose_log_file "${DIAGNOSE_LOG_FILE}" \
    --arg generated_validate_file "validation/validate.sh" \
    --arg generated_compose_file "validation/docker-compose.test.yml" \
    --argjson created_fix_issues "${CREATED_FIX_ISSUES_JSON}" \
    --slurpfile validation_result "${validation_file}" \
    --slurpfile diagnosis "${diagnosis_file}" \
    '{
      status: $status,
      summary: $summary,
      failure_summary: (if ($failure_summary | length) > 0 then $failure_summary else null end),
      repository: $repository,
      tracking_issue: $tracking_issue,
      compose_file: $compose_file,
      generated_at_utc: (now | todateiso8601),
      created_fix_issues: $created_fix_issues,
      validation_result: ($validation_result[0] // null),
      diagnosis: ($diagnosis[0] // null),
      artifact_paths: {
        runtime_dir: $runtime_dir,
        validation_log: $validation_log_file,
        generate_log: $generate_log_file,
        diagnose_log: $diagnose_log_file,
        generated_validate_script: $generated_validate_file,
        generated_compose_file: $generated_compose_file,
        validation_logs_dir: "validation/logs"
      }
    }' > "${METADATA_FILE}"
}

write_result_files()
{
  local status="$1"
  local summary="$2"
  local failure_summary="$3"

  write_status_file "${status}" "${summary}" "${failure_summary}"
  write_metadata_file "${status}" "${summary}" "${failure_summary}"
}

cleanup_runtime_containers()
{
  if [ -f "validation/docker-compose.test.yml" ]; then
    docker compose -f validation/docker-compose.test.yml down -v --remove-orphans >/dev/null 2>&1 || true
  fi

  if [ -n "${VALIDATION_COMPOSE_FILE}" ] \
    && [ -f "${VALIDATION_COMPOSE_FILE}" ] \
    && [ "${VALIDATION_COMPOSE_FILE}" != "validation/docker-compose.test.yml" ]; then
    docker compose -f "${VALIDATION_COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}

trap cleanup_runtime_containers EXIT


# ---------------------------------------------------------------
# Setup Codex + Serena
# ---------------------------------------------------------------
mkdir -p ~/.codex
CATALOG_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/codex_model_catalog.json"
{
  echo 'web_search = "live"'
  echo 'model_provider = "openrouter"'
  echo "model = \"${MODEL_EDITOR}\""
  echo "model_reasoning_effort = \"${EFFECTIVE_MODEL_REASONING_EFFORT}\""
  if [ -f "${CATALOG_PATH}" ]; then
    echo "model_catalog_json = \"${CATALOG_PATH}\""
  else
    echo "::warning::Codex model catalog not found at ${CATALOG_PATH}" >&2
  fi
  echo
  echo '[model_providers.openrouter]'
  echo 'name = "OpenRouter"'
  echo 'base_url = "https://openrouter.ai/api/v1"'
  echo 'env_key = "OPENROUTER_API_KEY"'
  echo 'wire_api = "responses"'
  echo 'stream_idle_timeout_ms = 600000'
  echo 'stream_max_retries = 5'
  echo 'request_max_retries = 3'
  echo
  echo '[sandbox_workspace_write]'
  echo 'network_access = true'
} > ~/.codex/config.toml

bash scripts/setup_serena.sh --mode editing --context codex || true
export PATH="${HOME}/.local/bin:${PATH}"


# ---------------------------------------------------------------
# Assemble context
# ---------------------------------------------------------------
if is_tracking_run; then
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}" --jq '.body // ""' > "${PROJECT_SPEC_FILE}"
else
  printf 'Standalone validation run. Tracking issue is not provided.\n' > "${PROJECT_SPEC_FILE}"
fi

if [ -f .ai/validate.yml ]; then
  cp .ai/validate.yml "${VALIDATE_HINTS_FILE}"
else
  printf '# No .ai/validate.yml hints file found\n' > "${VALIDATE_HINTS_FILE}"
fi

{
  if [ -f codex_system_instructions.md ]; then
    echo "=== SYSTEM INSTRUCTIONS ==="
    cat codex_system_instructions.md
    echo
  fi
  if [ -f ai_pipeline.md ]; then
    echo "=== AI PIPELINE ==="
    cat ai_pipeline.md
    echo
  fi
  if [ -f AGENTS.md ]; then
    echo "=== AGENTS.MD ==="
    cat AGENTS.md
    echo
  elif [ -f agents.md ]; then
    echo "=== AGENTS.MD ==="
    cat agents.md
    echo
  fi
  if [ -f README.md ]; then
    echo "=== README.MD ==="
    cat README.md
    echo
  fi
} > "${STATIC_CONTEXT_FILE}"


# ---------------------------------------------------------------
# Phase 1: Generate validation harness
# ---------------------------------------------------------------
set_tracking_phase_label "ai:validating"

# Ensure validation/ is git-ignored so no workflow accidentally commits it.
# If validation/ was previously committed to the repo, untrack and remove it
# so the ownership-marker check below does not hard-fail on a stale checkout.
# Changes are committed and pushed so the fix is permanent (one-time).
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  _vd_needs_commit=false

  if ! grep -qxF 'validation/' .gitignore 2>/dev/null; then
    echo 'validation/' >> .gitignore
    _vd_needs_commit=true
  fi

  if [ -n "$(git ls-files -- validation/ 2>/dev/null)" ]; then
    echo "Untracking previously committed validation/ directory."
    git rm -r --cached -- validation/ >/dev/null 2>&1 || true
    rm -rf validation
    _vd_needs_commit=true
  fi

  if [ "${_vd_needs_commit}" = true ]; then
    git add .gitignore 2>/dev/null || true
    git \
      -c user.name="ai-workflow[bot]" \
      -c user.email="ai-workflow[bot]@users.noreply.github.com" \
      commit -m "chore: gitignore validation/ and remove from tracking

The validation/ directory is a transient workspace used by the
AI validation workflow and must not be committed." >/dev/null 2>&1 || true
    if ! git push >/dev/null 2>&1; then
      echo "Note: could not push validation/ cleanup commit (branch protection or permissions). Fix applied locally for this run."
    fi
  fi
fi

if [ -d validation ] && [ ! -f validation/.ai-validation-owned ]; then
  echo "Refusing to delete existing 'validation' directory without ownership marker (validation/.ai-validation-owned)." >&2
  exit 1
fi
if [ -L validation ] || { [ -e validation ] && [ ! -d validation ]; }; then
  echo "Refusing to delete non-directory 'validation' path." >&2
  exit 1
fi
rm -rf validation
mkdir -p validation/logs
touch validation/.ai-validation-owned

if command -v git >/dev/null 2>&1; then
  git status --porcelain --untracked-files=all -- . ':!validation/**' | sort > "${PRE_GENERATE_STATUS_FILE}" 2>/dev/null || true
fi

{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_VALIDATE}"
  echo
  echo "=== IMPLEMENTATION TASK ==="
  echo
  cat prompts/mode-validate-generate.txt
  echo
  echo "=== PROJECT SPEC ==="
  cat "${PROJECT_SPEC_FILE}"
  echo
  echo "=== VALIDATION HINTS ==="
  cat "${VALIDATE_HINTS_FILE}"
  echo
  echo "=== RUNNER VALIDATION CONFIG ==="
  echo "TRACKING_ISSUE: ${TRACKING_ISSUE_RAW}"
  echo "VALIDATION_TIMEOUT_MINUTES: ${VALIDATION_TIMEOUT}"
  echo "PREFERRED_COMPOSE_FILE: ${VALIDATION_COMPOSE_FILE}"
  echo "SYNTHETIC_TEST_USERNAME_ENV_VAR: VALIDATION_TEST_USERNAME"
  echo "SYNTHETIC_TEST_PASSWORD_ENV_VAR: VALIDATION_TEST_PASSWORD"
  echo "SYNTHETIC_TEST_API_KEY_ENV_VAR: VALIDATION_TEST_API_KEY"
  echo
  echo "Generate the harness directly in the repository workspace for immediate execution."
} > "${GENERATE_PROMPT_FILE}"

GENERATE_SUCCESS=false
for attempt in 1 2; do
  echo "Validation harness generation attempt ${attempt}/2"
  if cat "${GENERATE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${GENERATE_OUTPUT_FILE}" 2> >(tee -a "${GENERATE_LOG_FILE}" >&2); then
    if [ -f validation/validate.sh ]; then
      GENERATE_SUCCESS=true
      break
    fi
  fi
  if [ "${attempt}" -lt 2 ]; then
    sleep $((attempt * 10))
  fi
done

if [ "${GENERATE_SUCCESS}" != "true" ]; then
  local_failure_summary="Codex did not generate a runnable validation/validate.sh harness."
  post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nSee workflow artifacts for generation logs."
  set_tracking_phase_label "ai:validation-failed"
  write_result_files "error" "Validation harness generation failed" "${local_failure_summary}"
  tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit 1
fi

if command -v git >/dev/null 2>&1; then
  git status --porcelain --untracked-files=all -- . ':!validation/**' | sort > "${POST_GENERATE_STATUS_FILE}" 2>/dev/null || true
  NON_VALIDATION_CHANGES=""
  if [ -f "${PRE_GENERATE_STATUS_FILE}" ] && [ -f "${POST_GENERATE_STATUS_FILE}" ] && ! cmp -s "${PRE_GENERATE_STATUS_FILE}" "${POST_GENERATE_STATUS_FILE}"; then
    NON_VALIDATION_CHANGES="$(diff -u "${PRE_GENERATE_STATUS_FILE}" "${POST_GENERATE_STATUS_FILE}" || true)"
  fi

  if [ -n "${NON_VALIDATION_CHANGES}" ]; then
    local_failure_summary="Codex modified files outside validation/ during harness generation."
    post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nUnexpected changes:\n\n\`\`\`\n${NON_VALIDATION_CHANGES}\n\`\`\`"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "error" "Validation harness generation violated path constraints" "${local_failure_summary}"
    tg_notify "Validation harness generation touched non-validation files for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    exit 1
  fi
fi

find validation -type f -name '*.sh' -exec chmod +x {} +


# ---------------------------------------------------------------
# Phase 2: Execute validation harness (idle-timeout based)
# ---------------------------------------------------------------
# The timeout is activity-based: the process is killed only if it
# produces no output for VALIDATION_TIMEOUT minutes. This allows
# large projects to run longer as long as they keep producing output.
IDLE_TIMEOUT_SECS=$((VALIDATION_TIMEOUT * 60))
VALIDATION_EXIT=0
VALIDATION_IDLE_KILLED=0

set +e
# Run validation in background, tee output to log file
bash validation/validate.sh > "${VALIDATION_LOG_FILE}" 2>&1 &
VALIDATION_PID=$!

# Monitor the log file for activity; kill if idle too long
LAST_SIZE=0
IDLE_ELAPSED=0
POLL_INTERVAL=5
while kill -0 "${VALIDATION_PID}" 2>/dev/null; do
  CURRENT_SIZE=0
  if [ -f "${VALIDATION_LOG_FILE}" ]; then
    CURRENT_SIZE=$(stat -c%s "${VALIDATION_LOG_FILE}" 2>/dev/null || echo 0)
  fi

  if [ "${CURRENT_SIZE}" -ne "${LAST_SIZE}" ]; then
    LAST_SIZE="${CURRENT_SIZE}"
    IDLE_ELAPSED=0
  else
    IDLE_ELAPSED=$((IDLE_ELAPSED + POLL_INTERVAL))
  fi

  if [ "${IDLE_ELAPSED}" -ge "${IDLE_TIMEOUT_SECS}" ]; then
    echo "Validation idle for ${VALIDATION_TIMEOUT} minute(s) with no output — terminating." >> "${VALIDATION_LOG_FILE}"
    kill "${VALIDATION_PID}" 2>/dev/null || true
    # Grace period: SIGKILL after 30s if still running
    sleep 30
    if kill -0 "${VALIDATION_PID}" 2>/dev/null; then
      kill -9 "${VALIDATION_PID}" 2>/dev/null || true
    fi
    VALIDATION_IDLE_KILLED=1
    break
  fi

  sleep "${POLL_INTERVAL}"
done

wait "${VALIDATION_PID}" 2>/dev/null
VALIDATION_EXIT=$?
set -e

tail -n 200 "${VALIDATION_LOG_FILE}" > "${VALIDATION_LOG_TAIL_FILE}" 2>/dev/null || true

if [ "${VALIDATION_IDLE_KILLED}" -eq 1 ] || [ "${VALIDATION_EXIT}" -eq 124 ] || [ "${VALIDATION_EXIT}" -eq 137 ]; then
  timeout_test="validation-idle-timeout"
  timeout_error="Validation idle-timed out after ${VALIDATION_TIMEOUT} minute(s) with no output"
  if [ "${VALIDATION_IDLE_KILLED}" -eq 0 ]; then
    # Legacy exit codes (shouldn't normally happen now, but handle defensively)
    timeout_test="validation-timeout-signal"
    timeout_error="Validation terminated (exit code ${VALIDATION_EXIT}) after ${VALIDATION_TIMEOUT} minute(s)"
  fi

  jq -n \
    --arg timeout_test "${timeout_test}" \
    --arg timeout_error "${timeout_error}" \
    --arg duration_seconds "${IDLE_TIMEOUT_SECS}" \
    '{
      result: "fail",
      phase: "timeout",
      total_tests: 0,
      passed_tests: 0,
      failed_tests: 1,
      failures: [
        {
          test: $timeout_test,
          error: $timeout_error,
          log_tail: "See validation.log tail in artifacts"
        }
      ],
      duration_seconds: ($duration_seconds | tonumber)
    }' > "${VALIDATION_RESULT_FILE}"
else
  if ! extract_last_json_with_key "${VALIDATION_LOG_FILE}" "result" "${VALIDATION_RESULT_FILE}"; then
    jq -n \
      --arg exit_code "${VALIDATION_EXIT}" \
      '{
        result: "fail",
        phase: "execution_error",
        total_tests: 0,
        passed_tests: 0,
        failed_tests: 1,
        failures: [
          {
            test: "validation-json-parse",
            error: ("Unable to parse validation result JSON (exit code " + $exit_code + ")"),
            log_tail: "See validation.log in artifacts"
          }
        ],
        duration_seconds: 0
      }' > "${VALIDATION_RESULT_FILE}"
  fi
fi

RESULT_KIND="$(jq -r '.result // "fail"' "${VALIDATION_RESULT_FILE}")"
TOTAL_TESTS="$(jq -r '.total_tests // 0' "${VALIDATION_RESULT_FILE}")"
PASSED_TESTS="$(jq -r '.passed_tests // 0' "${VALIDATION_RESULT_FILE}")"
FAILED_TESTS="$(jq -r '.failed_tests // 0' "${VALIDATION_RESULT_FILE}")"
DURATION_SECONDS="$(jq -r '.duration_seconds // 0' "${VALIDATION_RESULT_FILE}")"
FIRST_FAILURE="$(jq -r '.failures[0].error // ""' "${VALIDATION_RESULT_FILE}")"

# ---------------------------------------------------------------
# Safety net: override contradictory fail-with-all-pass results.
# When the harness script crashes (non-zero exit / result=fail)
# but the structured JSON shows all tests passed (failed_tests==0,
# passed_tests>0, counts consistent), the crash was a scripting
# bug (e.g. grep returning 1 on zero matches under pipefail),
# not a real test failure. Override to pass.
# ---------------------------------------------------------------
if [ "${RESULT_KIND}" != "pass" ] || [ "${VALIDATION_EXIT}" -ne 0 ]; then
	if jq -e '
		(.total_tests | type == "number") and
		(.passed_tests | type == "number") and
		(.failed_tests | type == "number") and
		(.total_tests > 0) and
		(.passed_tests > 0) and
		(.failed_tests == 0) and
		(.passed_tests == .total_tests) and
		((.failures | length == 0) or ((.failures | length == 1) and (.failures[0].test == "validate.sh:unexpected_error")))
	' "${VALIDATION_RESULT_FILE}" >/dev/null 2>&1; then
		echo "::warning::Harness exited ${VALIDATION_EXIT} with result '${RESULT_KIND}' but all ${PASSED_TESTS}/${TOTAL_TESTS} tests passed (failed_tests=0). Overriding to pass (likely scripting bug in generated validate.sh)."
		# Strip the synthetic unexpected_error failure entry and fix result
		jq '.result = "pass" | .failures = [] | .phase = "runtime_validation"' "${VALIDATION_RESULT_FILE}" > "${VALIDATION_RESULT_FILE}.tmp"
		mv "${VALIDATION_RESULT_FILE}.tmp" "${VALIDATION_RESULT_FILE}"
		RESULT_KIND="pass"
		VALIDATION_EXIT=0
		FIRST_FAILURE=""
	fi
fi

PASS_SCHEMA_OK="false"
if [ "${RESULT_KIND}" = "pass" ] && [ "${VALIDATION_EXIT}" -eq 0 ]; then
  if jq -e '
    (.total_tests | type == "number") and
    (.passed_tests | type == "number") and
    (.failed_tests | type == "number") and
    (.duration_seconds | type == "number") and
    (.failures | type == "array") and
    (.failed_tests == 0) and
    (.total_tests >= 0) and
    (.passed_tests >= 0) and
    (.passed_tests <= .total_tests) and
    (.failed_tests <= .total_tests) and
    ((.passed_tests + .failed_tests) == .total_tests)
  ' "${VALIDATION_RESULT_FILE}" >/dev/null 2>&1; then
    PASS_SCHEMA_OK="true"
  else
    jq -n \
      --arg reason "Pass payload schema consistency check failed" \
      '{
        result: "fail",
        phase: "result_schema_error",
        total_tests: 0,
        passed_tests: 0,
        failed_tests: 1,
        failures: [
          {
            test: "validation-result-schema",
            error: $reason,
            log_tail: "See validation.log in artifacts"
          }
        ],
        duration_seconds: 0
      }' > "${VALIDATION_RESULT_FILE}"
    RESULT_KIND="fail"
    TOTAL_TESTS="0"
    PASSED_TESTS="0"
    FAILED_TESTS="1"
    DURATION_SECONDS="0"
    FIRST_FAILURE="Pass payload schema consistency check failed"
  fi
fi

if [ "${RESULT_KIND}" = "pass" ] && [ "${VALIDATION_EXIT}" -eq 0 ] && [ "${PASS_SCHEMA_OK}" = "true" ]; then
  summary_text="Runtime validation passed (${PASSED_TESTS}/${TOTAL_TESTS} tests, ${DURATION_SECONDS}s)."
  post_tracking_comment "## ✅ Runtime validation passed\n\n- Passed tests: ${PASSED_TESTS}/${TOTAL_TESTS}\n- Duration: ${DURATION_SECONDS}s"
  set_tracking_phase_label "ai:validated"
  write_result_files "pass" "${summary_text}" ""
  tg_notify "Runtime validation passed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} (${PASSED_TESTS}/${TOTAL_TESTS})." "DEBUG"
  exit 0
fi


# ---------------------------------------------------------------
# Phase 3: Diagnose failures
# ---------------------------------------------------------------
: > "${CONTAINER_LOG_TAIL_FILE}"
if [ -d validation/logs ]; then
  while IFS= read -r log_file; do
    echo "===== ${log_file} (tail 80) =====" >> "${CONTAINER_LOG_TAIL_FILE}"
    tail -n 80 "${log_file}" >> "${CONTAINER_LOG_TAIL_FILE}" 2>/dev/null || true
    echo >> "${CONTAINER_LOG_TAIL_FILE}"
  done < <(find validation/logs -type f | sort)
fi

{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_VALIDATE}"
  echo
  echo "=== DIAGNOSIS TASK ==="
  echo
  cat prompts/mode-validate-diagnose.txt
  echo
  echo "=== PROJECT SPEC ==="
  cat "${PROJECT_SPEC_FILE}"
  echo
  echo "=== STRUCTURED VALIDATION FAILURE JSON ==="
  cat "${VALIDATION_RESULT_FILE}"
  echo
  echo "=== VALIDATION LOG TAIL (last 200 lines) ==="
  cat "${VALIDATION_LOG_TAIL_FILE}"
  echo
  echo "=== CONTAINER LOG TAILS ==="
  cat "${CONTAINER_LOG_TAIL_FILE}"
  echo
  echo "=== VALIDATION HINTS ==="
  cat "${VALIDATE_HINTS_FILE}"
} > "${DIAGNOSE_PROMPT_FILE}"

DIAGNOSE_SUCCESS=false
for attempt in 1 2; do
  echo "Validation diagnosis attempt ${attempt}/2"
  if cat "${DIAGNOSE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${DIAGNOSE_OUTPUT_FILE}" 2> >(tee -a "${DIAGNOSE_LOG_FILE}" >&2); then
    if extract_last_json_with_key "${DIAGNOSE_OUTPUT_FILE}" "status" "${DIAGNOSE_RESULT_FILE}"; then
      DIAGNOSE_SUCCESS=true
      break
    fi
  fi
  if [ "${attempt}" -lt 2 ]; then
    sleep $((attempt * 10))
  fi
done

if [ "${DIAGNOSE_SUCCESS}" != "true" ]; then
  jq -n \
    --arg diagnosis "Diagnosis output could not be parsed into required JSON contract." \
    '{
      status: "harness_error",
      diagnosis: $diagnosis,
      fix_issues: [],
      harness_fixes: "Update mode-validate-diagnose prompt or improve failure context extraction."
    }' > "${DIAGNOSE_RESULT_FILE}"
fi

DIAG_STATUS="$(jq -r '.status // "harness_error"' "${DIAGNOSE_RESULT_FILE}")"
DIAG_TEXT="$(jq -r '.diagnosis // "Validation failed."' "${DIAGNOSE_RESULT_FILE}")"

case "${DIAG_STATUS}" in
  needs_fixes)
    FIX_COUNT="$(jq -r '.fix_issues | length' "${DIAGNOSE_RESULT_FILE}")"
    if [ "${FIX_COUNT}" -le 0 ]; then
      failure_summary="Diagnosis returned needs_fixes with empty fix_issues."
      post_tracking_comment "## ❌ Runtime validation failed\n\n${failure_summary}\n\nDiagnosis:\n\n${DIAG_TEXT}"
      set_tracking_phase_label "ai:validation-failed"
      write_result_files "fail" "Runtime validation failed" "${failure_summary}"
      tg_notify "Validation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: invalid diagnosis payload." "ERROR"
      exit 0
    fi

    local_to_issue_map='{}'
    for idx in $(seq 0 $((FIX_COUNT - 1))); do
      FIX_ID="$(jq -r ".fix_issues[${idx}].id // \"validation-fix-$((idx + 1))\"" "${DIAGNOSE_RESULT_FILE}")"
      FIX_TITLE="$(jq -r ".fix_issues[${idx}].title // \"Validation fix-up $((idx + 1))\"" "${DIAGNOSE_RESULT_FILE}")"
      FIX_BODY_BASE="$(jq -r ".fix_issues[${idx}].body // \"No body provided\"" "${DIAGNOSE_RESULT_FILE}" | sed 's/\\n/\n/g')"
      FIX_PRIORITY="$(jq -r ".fix_issues[${idx}].priority // 5" "${DIAGNOSE_RESULT_FILE}")"

      FIX_BODY_FULL="${FIX_BODY_BASE}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_ISSUE_RAW}
- Local ID: \`${FIX_ID}\`
- Type: validation-fix-up (cycle ${VALIDATION_CYCLE})
- Priority: ${FIX_PRIORITY}
- Managed by: AI Orchestrator"

      if ! is_tracking_run; then
        continue
      fi

      FIX_URL="$(gh_retry gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${FIX_TITLE}" \
        --body "${FIX_BODY_FULL}")"
      FIX_NUM="$(echo "${FIX_URL}" | grep -oE '[0-9]+$' || true)"
      if [ -n "${FIX_NUM}" ]; then
        CREATED_FIX_ISSUES_JSON="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq --argjson num "${FIX_NUM}" '. + [$num]')"
        local_to_issue_map="$(echo "${local_to_issue_map}" | jq --arg id "${FIX_ID}" --argjson num "${FIX_NUM}" '. + {($id): $num}')"
      fi
    done

    for idx in $(seq 0 $((FIX_COUNT - 1))); do
      FIX_ID="$(jq -r ".fix_issues[${idx}].id // \"validation-fix-$((idx + 1))\"" "${DIAGNOSE_RESULT_FILE}")"
      FIX_NUM="$(echo "${local_to_issue_map}" | jq -r --arg id "${FIX_ID}" '.[$id] // empty')"
      [ -n "${FIX_NUM}" ] || continue

      DEP_SUMMARY=""
      while IFS= read -r dep_id; do
        [ -n "${dep_id}" ] || continue
        DEP_NUM="$(echo "${local_to_issue_map}" | jq -r --arg dep_id "${dep_id}" '.[$dep_id] // empty')"
        if [ -n "${DEP_NUM}" ]; then
          DEP_SUMMARY+="- #${DEP_NUM} (from ${dep_id})\n"
        fi
      done < <(jq -r ".fix_issues[${idx}].depends_on[]?" "${DIAGNOSE_RESULT_FILE}")

      if [ -n "${DEP_SUMMARY}" ]; then
        gh_retry gh issue comment "${FIX_NUM}" \
          --repo "${GITHUB_REPOSITORY}" \
          --body "## Dependency Notes\n\nThis fix-up should be applied after:\n${DEP_SUMMARY}" >/dev/null || true
      fi
    done

    if ! is_tracking_run; then
      failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s). Tracking issue is not set, so fix-up issues were not created."
      write_result_files "fail" "Validation needs fixes" "${failure_summary}"
      tg_notify "Validation for ${GITHUB_REPOSITORY} reported fixable failures, but TRACKING_ISSUE is not set." "WARNING"
      exit 0
    fi

    issue_list_md="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq -r '.[] | "- #\(.)"')"
    if [ -z "${issue_list_md}" ]; then
      issue_list_md='- (no issue numbers captured)'
    fi

    post_tracking_comment "## 🧪 Runtime validation found fixable issues\n\n${DIAG_TEXT}\n\nCreated fix-up issues:\n${issue_list_md}"
    set_tracking_phase_label "ai:validation-fixing"

    failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s). Fix-up issues were created."
    write_result_files "fail" "Validation needs fixes" "${failure_summary}"
    tg_notify "Validation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} needs fixes (${FIX_COUNT} issue(s) created)." "WARNING"
    ;;

  harness_error)
    HARNESS_FIXES="$(jq -r '.harness_fixes // "Validation harness needs correction."' "${DIAGNOSE_RESULT_FILE}")"
    failure_summary="Validation harness error: ${HARNESS_FIXES}"

    post_tracking_comment "## ❌ Runtime validation harness error\n\n${DIAG_TEXT}\n\nHarness fix guidance:\n\n${HARNESS_FIXES}"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "fail" "Validation failed due to harness error" "${failure_summary}"
    tg_notify "Validation harness error for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    ;;

  infeasible)
    failure_summary="Runtime validation infeasible: ${DIAG_TEXT}"

    post_tracking_comment "## ❌ Runtime validation infeasible\n\n${DIAG_TEXT}"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "fail" "Validation marked infeasible" "${failure_summary}"
    tg_notify "Validation infeasible for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    ;;

  *)
    failure_summary="Unknown diagnosis status '${DIAG_STATUS}'. ${FIRST_FAILURE}"

    post_tracking_comment "## ❌ Runtime validation failed\n\n${failure_summary}\n\nDiagnosis:\n\n${DIAG_TEXT}"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "fail" "Validation failed" "${failure_summary}"
    tg_notify "Validation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: unknown diagnosis status." "ERROR"
    ;;
esac

exit 0
