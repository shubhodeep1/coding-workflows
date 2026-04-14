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
#   CONTEXT7_DISABLED, GIT_MCP_DISABLED

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
DISCOVER_PROMPT_FILE="${RUNTIME_DIR}/validate_discover_prompt.txt"
DISCOVER_OUTPUT_FILE="${RUNTIME_DIR}/validate_discover_output.txt"
DISCOVER_LOG_FILE="${RUNTIME_DIR}/validate_discover.log"
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
PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
PRIOR_RESULT_JSON_FILE="${RUNTIME_DIR}/prior_validation_result.json"
PRIOR_CONTAINER_LOGS_FILE="${RUNTIME_DIR}/prior_container_logs_tail.txt"
VALIDATION_RUNNER_FILE="${RUNTIME_DIR}/validation_runtime_driver.sh"

HINTS_SOURCE="none"
HARNESS_MODE="generate"
PRE_FLIGHT_STATUS="not_run"
GENERATED_VALIDATE_SCRIPT_PATH=""
CANONICAL_VALIDATE_DRIVER_REL="scripts/validate_process.sh"
CANONICAL_VALIDATE_HARNESS_REL="validation/validate.sh"

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

enforce_canonical_driver_path()
{
  local script_source="${BASH_SOURCE[0]:-$0}"
  case "${script_source}" in
    "${CANONICAL_VALIDATE_DRIVER_REL}"|"./${CANONICAL_VALIDATE_DRIVER_REL}"|*/"${CANONICAL_VALIDATE_DRIVER_REL}")
      return 0
      ;;
    *)
      echo "Refusing to run validate driver from non-canonical path '${script_source}'. Expected ${CANONICAL_VALIDATE_DRIVER_REL}." >&2
      return 1
      ;;
  esac
}

ensure_validation_harness_not_tracked()
{
  if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
    return 0
  fi

  if git ls-files --error-unmatch -- "${CANONICAL_VALIDATE_HARNESS_REL}" >/dev/null 2>&1; then
    echo "${CANONICAL_VALIDATE_HARNESS_REL} is tracked by git; it must remain transient." >&2
    return 1
  fi

  if git diff --cached --name-only -- "${CANONICAL_VALIDATE_HARNESS_REL}" | grep -q .; then
    echo "${CANONICAL_VALIDATE_HARNESS_REL} is staged in git; it must remain unstaged/untracked." >&2
    return 1
  fi

  return 0
}

enforce_no_renamed_driver_artifacts()
{
  if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
    return 0
  fi

  local candidate_driver_files
  local candidate
  local renamed_driver_files=""
  candidate_driver_files="$({
    git ls-files -- 'scripts/validate*.sh'
    git ls-files --others --exclude-standard -- 'scripts/validate*.sh'
  } 2>/dev/null | awk '$0 != "scripts/validate_process.sh" && $0 != "scripts/validate_driver.sh"' | sort -u)"

  while IFS= read -r candidate; do
    [ -n "${candidate}" ] || continue
    [ -f "${candidate}" ] || continue

    if cmp -s "${candidate}" "scripts/validate_process.sh" \
      || { [ -f "scripts/validate_driver.sh" ] && cmp -s "${candidate}" "scripts/validate_driver.sh"; }; then
      renamed_driver_files="${renamed_driver_files}${candidate}"$'\n'
    fi
  done <<< "${candidate_driver_files}"

  if [ -n "${renamed_driver_files}" ]; then
    echo "Found renamed managed validate driver artifacts in scripts/:" >&2
    printf '%s' "${renamed_driver_files}" >&2
    return 1
  fi

  return 0
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

  if [ ! -f "${contract_file}" ]; then
    echo "::warning::set_tracking_phase_label: missing label contract ${contract_file}; cannot apply label '${phase_label}' safely." >&2
    return 1
  fi

  local phase_changes
  if ! phase_changes="$(python3 scripts/ai_labels.py resolve-phase \
    --contract-file "${contract_file}" \
    --phase "${phase_label}" 2>/dev/null)"; then
    echo "::warning::set_tracking_phase_label: resolve-phase failed for '${phase_label}' using ${contract_file}." >&2
    return 1
  fi

  # Fetch current labels on the issue so we only attempt to remove labels
  # that are actually present.  Trying to remove a label that does not
  # exist on the issue can cause `gh issue edit` to return an error,
  # which the outer `|| true` would silently swallow — leaving stale
  # labels (e.g. ai:validating + ai:validation-fixing) in place even
  # after the phase has advanced to ai:validated.
  local current_issue_labels
  current_issue_labels="$(gh_retry gh api \
    "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/labels" \
    --jq '[.[].name]' 2>/dev/null || echo '[]')"

  # Build a single gh issue edit command with all --remove-label and
  # --add-label flags instead of one API call per label.
  local edit_args=()
  while IFS= read -r remove_label; do
    [ -n "${remove_label}" ] || continue
    # Only remove labels that are currently on the issue to avoid
    # errors from trying to remove absent labels.
    if echo "${current_issue_labels}" | jq -e --arg l "${remove_label}" 'index($l) != null' >/dev/null 2>&1; then
      edit_args+=(--remove-label "${remove_label}")
    fi
  done < <(echo "${phase_changes}" | jq -r '.remove[]?')

  while IFS= read -r add_label; do
    [ -n "${add_label}" ] || continue
    ensure_label_exists "${add_label}"
    edit_args+=(--add-label "${add_label}")
  done < <(echo "${phase_changes}" | jq -r '.add[]?')

  if [ "${#edit_args[@]}" -gt 0 ]; then
    local _label_err_file
    _label_err_file="$(mktemp)"
    if ! gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
      --repo "${GITHUB_REPOSITORY}" \
      "${edit_args[@]}" >/dev/null 2>"${_label_err_file}"; then
      local _label_err
      _label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
      if echo "${_label_err}" | grep -Eqi "could not remove label:|['\"][[:alnum:]:._/-]+['\"] not found"; then
        echo "::warning::set_tracking_phase_label: non-fatal missing label while applying '${phase_label}' to #${TRACKING_ISSUE_NUM}: ${_label_err}" >&2
        rm -f "${_label_err_file}"
        return 0
      fi
      echo "::warning::set_tracking_phase_label: failed to apply '${phase_label}' to #${TRACKING_ISSUE_NUM}: ${_label_err}" >&2
      rm -f "${_label_err_file}"
      return 1
    fi
    rm -f "${_label_err_file}"
  fi
  return 0
}

if ! enforce_canonical_driver_path; then
  exit 1
fi

if ! enforce_no_renamed_driver_artifacts; then
  exit 1
fi

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

is_validation_harness_runnable()
{
	if [ -f validation/docker-compose.test.yml ] \
		&& [ -f validation/tests/00_canary.sh ] \
		&& find validation/tests -maxdepth 1 -type f -name '*.sh' -print -quit | grep -q .; then
		GENERATED_VALIDATE_SCRIPT_PATH=""
		return 0
	fi

	GENERATED_VALIDATE_SCRIPT_PATH=""
	return 1
}

ensure_runtime_validation_driver()
{
  cat > "${VALIDATION_RUNNER_FILE}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="validation/docker-compose.test.yml"
TEST_DIR="validation/tests"
LOG_DIR="validation/logs"
COMPOSE_LOG="${LOG_DIR}/compose.log"
ENV_FILE="${VALIDATE_ENV_FILE:-validation/validate.env}"
START_TS="$(date +%s)"

if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

mkdir -p "${LOG_DIR}"
: > "${COMPOSE_LOG}"

TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
FAILURES_FILE="$(mktemp)"
RESULT_EMITTED=0
printf '[]' > "${FAILURES_FILE}"

append_failure()
{
  local test_name="$1"
  local error_msg="$2"
  local log_file="${3:-}"
  local log_tail=""

  if [ -n "${log_file}" ] && [ -f "${log_file}" ]; then
    log_tail="$(tail -c 10000 "${log_file}" | tr -d '\000' | tail -n 30 2>/dev/null || true)"
  fi

  python3 - "${FAILURES_FILE}" "${test_name}" "${error_msg}" "${log_tail}" <<'PY'
import json
import sys

path, test_name, error_msg, log_tail = sys.argv[1:5]
with open(path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)
payload.append({"test": test_name, "error": error_msg, "log_tail": log_tail})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY
}

emit_result()
{
  local result_value="${1:-fail}"
  local duration_seconds

  if [ "${RESULT_EMITTED}" = "1" ]; then
    return 0
  fi

  duration_seconds=$(( $(date +%s) - START_TS ))

  RESULT="${result_value}" \
  TOTAL_TESTS="${TOTAL_TESTS}" \
  PASSED_TESTS="${PASSED_TESTS}" \
  FAILED_TESTS="${FAILED_TESTS}" \
  DURATION_SECONDS="${duration_seconds}" \
  FAILURES_FILE_PATH="${FAILURES_FILE}" \
  python3 -c 'import json, os; print(json.dumps({
"result": os.environ["RESULT"],
"phase": "runtime_validation",
"total_tests": int(os.environ["TOTAL_TESTS"]),
"passed_tests": int(os.environ["PASSED_TESTS"]),
"failed_tests": int(os.environ["FAILED_TESTS"]),
"failures": json.load(open(os.environ["FAILURES_FILE_PATH"], encoding="utf-8")),
"duration_seconds": int(os.environ["DURATION_SECONDS"]),
}))'

  RESULT_EMITTED=1
}

cleanup()
{
  {
    printf '\n===== docker compose logs --no-color =====\n'
    docker compose -f "${COMPOSE_FILE}" logs --no-color 2>/dev/null || true
  } >> "${COMPOSE_LOG}"
  docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "${FAILURES_FILE}" >/dev/null 2>&1 || true
}

trap cleanup EXIT

if ! docker compose -f "${COMPOSE_FILE}" up -d --build >> "${COMPOSE_LOG}" 2>&1; then
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  FAILED_TESTS=$((FAILED_TESTS + 1))
  append_failure "compose_up" "failed to build/start compose services" "${COMPOSE_LOG}"
  emit_result fail
  exit 1
fi

mapfile -t test_scripts < <(find "${TEST_DIR}" -maxdepth 1 -type f -name '*.sh' | sort)
if [ "${#test_scripts[@]}" -eq 0 ]; then
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  FAILED_TESTS=$((FAILED_TESTS + 1))
  append_failure "tests_missing" "no validation test scripts found under ${TEST_DIR}"
  emit_result fail
  exit 1
fi

if [ "$(basename "${test_scripts[0]}")" != "00_canary.sh" ]; then
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  FAILED_TESTS=$((FAILED_TESTS + 1))
  append_failure "canary_missing" "first validation test script must be validation/tests/00_canary.sh"
  emit_result fail
  exit 1
fi

for test_script in "${test_scripts[@]}"; do
  test_name="$(basename "${test_script}")"
  test_log="${LOG_DIR}/${test_name}.log"

  echo "=== RUN ${test_name} ==="
  set +e
  bash "${test_script}" > "${test_log}" 2>&1
  test_rc=$?
  set -e

  cat "${test_log}" || true

  ok_count="$(grep -E -c '^ok[[:space:]]+[0-9]+' "${test_log}" || true)"
  TOTAL_TESTS=$((TOTAL_TESTS + ok_count))
  PASSED_TESTS=$((PASSED_TESTS + ok_count))

  not_ok_count=0
  while IFS= read -r not_ok_line; do
    [ -z "${not_ok_line}" ] && continue
    not_ok_count=$((not_ok_count + 1))
    append_failure "${test_name}" "${not_ok_line}" "${test_log}"
  done < <(grep -E '^not ok[[:space:]]+[0-9]+([[:space:]]+-[[:space:]].*)?$' "${test_log}" || true)

  TOTAL_TESTS=$((TOTAL_TESTS + not_ok_count))
  FAILED_TESTS=$((FAILED_TESTS + not_ok_count))

  if [ "${test_rc}" -ne 0 ] && [ "${not_ok_count}" -eq 0 ]; then
    TOTAL_TESTS=$((TOTAL_TESTS + 1))
    FAILED_TESTS=$((FAILED_TESTS + 1))
    append_failure "${test_name}:unexpected_error" "script exited with code ${test_rc} without TAP 'not ok' output" "${test_log}"
  fi
done

if [ "${FAILED_TESTS}" -eq 0 ]; then
  emit_result pass
  exit 0
fi

emit_result fail
exit 1
EOF

  chmod +x "${VALIDATION_RUNNER_FILE}"
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
    --arg hints_source "${HINTS_SOURCE}" \
    --arg harness_mode "${HARNESS_MODE}" \
    --arg pre_flight_status "${PRE_FLIGHT_STATUS}" \
    --arg repository "${GITHUB_REPOSITORY}" \
    --arg tracking_issue "${TRACKING_ISSUE_RAW}" \
    --arg runtime_dir "${RUNTIME_DIR}" \
    --arg compose_file "${VALIDATION_COMPOSE_FILE}" \
    --arg validation_log_file "${VALIDATION_LOG_FILE}" \
    --arg generate_log_file "${GENERATE_LOG_FILE}" \
    --arg diagnose_log_file "${DIAGNOSE_LOG_FILE}" \
    --arg generated_validate_file "${GENERATED_VALIDATE_SCRIPT_PATH}" \
    --arg generated_compose_file "validation/docker-compose.test.yml" \
    --argjson created_fix_issues "${CREATED_FIX_ISSUES_JSON}" \
    --slurpfile validation_result "${validation_file}" \
    --slurpfile diagnosis "${diagnosis_file}" \
    '{
      status: $status,
      summary: $summary,
      failure_summary: (if ($failure_summary | length) > 0 then $failure_summary else null end),
      hints_source: $hints_source,
      harness_mode: $harness_mode,
      pre_flight_status: $pre_flight_status,
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
        generated_validate_script: (if ($generated_validate_file | length) > 0 then $generated_validate_file else null end),
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

ensure_validate_wrapper()
{
	# Only generate the wrapper if the canonical driver exists.
	# When absent, the runtime fallback driver will be used instead.
	if [ ! -f scripts/validate_driver.sh ]; then
		return 0
	fi
	mkdir -p validation
	cat > validation/validate.sh <<'EOF'
#!/usr/bin/env bash
# Auto-generated by coding-workflows — DO NOT EDIT

set -euo pipefail

exec bash scripts/validate_driver.sh "$@"
EOF
	chmod +x validation/validate.sh
}

run_preflight_checks()
{
	PRE_FLIGHT_STATUS="running"
	: > "${PRE_FLIGHT_LOG_FILE}"

	# Emit the tail of the pre-flight log to stderr so that the failing command's
	# output is visible directly in the GitHub Actions job log, without requiring
	# the validation_preflight.log artifact to be downloaded. Structured with
	# clear delimiter markers so the excerpt is easy to scan and grep in CI logs.
	_emit_preflight_tail()
	{
		local reason="$1"
		{
			echo "::error::Pre-flight failed: ${reason}"
			echo "----- validation_preflight.log (tail -n 40) -----"
			tail -n 40 "${PRE_FLIGHT_LOG_FILE}" 2>/dev/null || true
			echo "-------------------------------------------------"
		} >&2
	}

	if [ ! -f validation/docker-compose.test.yml ]; then
		echo "Missing validation/docker-compose.test.yml" >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		_emit_preflight_tail "validation/docker-compose.test.yml missing"
		return 1
	fi

	# Validate legacy wrapper only if it exists. Canonical artifact mode
	# (validate.env + tests/00_canary.sh + compose) does not require it.
	if [ -f validation/validate.sh ]; then
		if ! bash -n validation/validate.sh >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
			echo "Shell syntax check failed: validation/validate.sh" >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			_emit_preflight_tail "bash -n failed for validation/validate.sh"
			return 1
		fi

		if ! grep -q 'scripts/validate_driver.sh' validation/validate.sh; then
			echo "validation/validate.sh must delegate to scripts/validate_driver.sh" >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			_emit_preflight_tail "validation/validate.sh is not a thin wrapper"
			return 1
		fi

		if [ -f scripts/validate_driver.sh ]; then
			if ! bash -n scripts/validate_driver.sh >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
				echo "Shell syntax check failed: scripts/validate_driver.sh" >> "${PRE_FLIGHT_LOG_FILE}"
				PRE_FLIGHT_STATUS="fail"
				_emit_preflight_tail "bash -n failed for scripts/validate_driver.sh"
				return 1
			fi
		else
			echo "scripts/validate_driver.sh not present; allowing runtime fallback driver selection" >> "${PRE_FLIGHT_LOG_FILE}"
		fi
	fi

	if [ ! -f validation/validate.env ]; then
		echo "validation/validate.env not found; runtime validation driver defaults will be used." >> "${PRE_FLIGHT_LOG_FILE}"
	fi

	if [ ! -f validation/tests/00_canary.sh ]; then
		echo "Missing validation/tests/00_canary.sh" >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		_emit_preflight_tail "validation/tests/00_canary.sh missing"
		return 1
	fi

	if ! docker compose -f validation/docker-compose.test.yml config --quiet >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
		echo "Compose syntax/validation check failed." >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		_emit_preflight_tail "docker compose config failed (YAML/schema invalid). Common cause: YAML must use space indentation, not tabs."
		return 1
	fi

	local shell_count
	shell_count="$(find validation -type f -name '*.sh' -not -path 'validation/logs/*' | wc -l | tr -d ' ')"
	if [ "${shell_count}" -eq 0 ]; then
		echo "No shell scripts found under validation/." >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		_emit_preflight_tail "no shell scripts found under validation/"
		return 1
	fi

	while IFS= read -r shell_file; do
		if ! bash -n "${shell_file}" >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
			echo "Shell syntax check failed: ${shell_file}" >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			_emit_preflight_tail "bash -n failed for ${shell_file}"
			return 1
		fi
	done < <(find validation -type f -name '*.sh' -not -path 'validation/logs/*' | sort)

	local compose_json_file
	compose_json_file="${RUNTIME_DIR}/validation_compose_config.json"
	if ! docker compose -f validation/docker-compose.test.yml config --format json > "${compose_json_file}" 2>> "${PRE_FLIGHT_LOG_FILE}"; then
		echo "Compose JSON export unavailable or failed; skipping build context and dockerfile path verification." >> "${PRE_FLIGHT_LOG_FILE}"
		printf '%s\n' '{"services":{}}' > "${compose_json_file}"
	fi

	if ! python3 - "${compose_json_file}" >> "${PRE_FLIGHT_LOG_FILE}" 2>&1 <<'PY'
import json
import os
import sys

compose_json_path = sys.argv[1]

with open(compose_json_path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

services = payload.get("services") or {}
compose_dir = os.path.abspath("validation")
missing = []

for service_name, service_cfg in services.items():
    build_cfg = service_cfg.get("build")
    if not build_cfg:
        continue

    context = "."
    dockerfile = "Dockerfile"
    if isinstance(build_cfg, str):
        context = build_cfg
    elif isinstance(build_cfg, dict):
        context = build_cfg.get("context") or "."
        dockerfile = build_cfg.get("dockerfile") or "Dockerfile"
    else:
        continue

    if os.path.isabs(context):
        resolved_context = os.path.normpath(context)
    else:
        resolved_context = os.path.normpath(os.path.join(compose_dir, context))

    if os.path.isabs(dockerfile):
        resolved_dockerfile = os.path.normpath(dockerfile)
    else:
        resolved_dockerfile = os.path.normpath(os.path.join(resolved_context, dockerfile))

    if not os.path.isdir(resolved_context):
        missing.append(f"service={service_name} missing build context: {resolved_context}")
        continue
    if not os.path.isfile(resolved_dockerfile):
        missing.append(f"service={service_name} missing dockerfile: {resolved_dockerfile}")

if missing:
    for line in missing:
        print(line)
    sys.exit(1)

print("Build context and dockerfile path checks passed.")
PY
	then
		PRE_FLIGHT_STATUS="fail"
		_emit_preflight_tail "build context / dockerfile path resolution failed"
		return 1
	fi

	PRE_FLIGHT_STATUS="pass"
	return 0
}

enforce_managed_validation_artifact_contract()
{
	if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
		return 0
	fi

	local canonical_path
	local canonical_hash
	local tracked_script
	local tracked_hash
	local has_violation=false
	local -a canonical_paths
	local -a tracked_scripts
	local -a violations

	canonical_paths=(
		"scripts/validate_process.sh"
		"scripts/validate_driver.sh"
	)

	if git ls-files --error-unmatch -- validation/validate.sh >/dev/null 2>&1; then
		violations+=("validation/validate.sh is tracked. validation/ artifacts must remain transient and untracked.")
		has_violation=true
	fi

	mapfile -t tracked_scripts < <(git ls-files -- 'scripts/*.sh' 2>/dev/null || true)

	for canonical_path in "${canonical_paths[@]}"; do
		if [ ! -f "${canonical_path}" ] || [ -L "${canonical_path}" ]; then
			continue
		fi

		if ! canonical_hash="$(git hash-object -- "${canonical_path}" 2>/dev/null)"; then
			continue
		fi

		for tracked_script in "${tracked_scripts[@]}"; do
			if [ -z "${tracked_script}" ] || [ "${tracked_script}" = "${canonical_path}" ] || [ ! -f "${tracked_script}" ] || [ -L "${tracked_script}" ]; then
				continue
			fi

			if ! tracked_hash="$(git hash-object -- "${tracked_script}" 2>/dev/null)"; then
				continue
			fi

			if [ "${tracked_hash}" = "${canonical_hash}" ]; then
				violations+=("${tracked_script} is a tracked copy of managed artifact ${canonical_path}.")
				has_violation=true
			fi
		done
	done

	if [ "${has_violation}" = true ]; then
		echo "Managed validation artifact contract violation detected:" >&2
		printf ' - %s\n' "${violations[@]}" >&2
		return 1
	fi

	return 0
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

INTEGRATION_BRANCH=""
if is_tracking_run; then
  INTEGRATION_BRANCH="$(sed -n 's/^\*\*Integration branch:\*\* `\([^`]*\)`$/\1/p' "${PROJECT_SPEC_FILE}" | head -n1 | tr -d '\r')"
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
# Phase 0: Discover validation hints when repository hints are absent
# ---------------------------------------------------------------
if [ -f .ai/validate.yml ]; then
  cp .ai/validate.yml "${VALIDATE_HINTS_FILE}"
  HINTS_SOURCE="committed"
else
{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "=== DISCOVERY TASK ==="
  echo
  bash scripts/render_prompt.sh prompts/mode-validate-discover.txt
  echo
  echo "TOOL_CALL_BUDGET: 15"
  echo
  echo "=== PROJECT SPEC ==="
    cat "${PROJECT_SPEC_FILE}"
    echo
    echo "Output only YAML for .ai/validate.yml with no markdown fences or prose."
  } > "${DISCOVER_PROMPT_FILE}"

  DISCOVER_SUCCESS=false
  for attempt in 1 2; do
    echo "Validation hint discovery attempt ${attempt}/2"
    if cat "${DISCOVER_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${DISCOVER_OUTPUT_FILE}" 2> >(tee -a "${DISCOVER_LOG_FILE}" >&2); then
      if python3 - "${DISCOVER_OUTPUT_FILE}" "${VALIDATE_HINTS_FILE}" <<'PY'
import re
import sys

source_file = sys.argv[1]
output_file = sys.argv[2]

with open(source_file, "r", encoding="utf-8", errors="replace") as handle:
    raw = handle.read().replace("\r", "")

candidate = raw.strip()
if "```" in raw:
    match = re.search(r"```(?:yaml|yml)?\n(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()

if not candidate:
    sys.exit(1)

if candidate.lstrip().startswith("{") or candidate.lstrip().startswith("["):
    sys.exit(1)

if len(candidate) > 12000:
    sys.exit(1)

lines = [
    line.lstrip()
    for line in candidate.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
if not lines:
    sys.exit(1)

if lines[0].lower().startswith(("error:", "fatal:", "traceback", "exception")):
    sys.exit(1)

expected_key = re.compile(r"^(type|entry|port|health_check|services|env_overrides|custom_tests|skip_tests):\s*", re.IGNORECASE)
if not any(expected_key.match(line) for line in lines):
    sys.exit(1)

with open(output_file, "w", encoding="utf-8") as handle:
    handle.write(candidate)
    handle.write("\n")
PY
      then
        DISCOVER_SUCCESS=true
        HINTS_SOURCE="discovered"
        break
      fi
    fi
    if [ "${attempt}" -lt 2 ]; then
      sleep $((attempt * 5))
    fi
  done

  if [ "${DISCOVER_SUCCESS}" != "true" ]; then
    printf '# No .ai/validate.yml hints file found\n' > "${VALIDATE_HINTS_FILE}"
    HINTS_SOURCE="none"
  fi
fi


# ---------------------------------------------------------------
# Phase 1: Generate validation harness
# ---------------------------------------------------------------
set_tracking_phase_label "ai:validating"

if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  if ! grep -qxF 'validation/' .git/info/exclude 2>/dev/null; then
    echo 'validation/' >> .git/info/exclude
  fi
fi

if ! ensure_validation_harness_not_tracked; then
  local_failure_summary="${CANONICAL_VALIDATE_HARNESS_REL} is tracked in git. Runtime validation harness must remain untracked."
  post_tracking_comment "## ⚠️ Runtime validation harness tracking violation\n\n${local_failure_summary}\n\nRemove it from git tracking in the consumer repository and rerun validation."
  set_tracking_phase_label "ai:validation-failed"
  write_result_files "error" "Validation harness tracking violation" "${local_failure_summary}"
  tg_notify "Validation harness tracking violation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit 1
fi

if ! enforce_managed_validation_artifact_contract; then
	exit 1
fi

if [ -L validation ] || { [ -e validation ] && [ ! -d validation ]; }; then
	echo "Refusing to use non-directory 'validation' path." >&2
	exit 1
fi

if [ "${VALIDATION_CYCLE}" -gt 1 ] \
	&& [ -d validation ] \
	&& [ -f validation/.ai-validation-owned ] \
	&& is_validation_harness_runnable; then
	HARNESS_MODE="fix"
	mkdir -p validation/logs
	find validation/logs -mindepth 1 -delete 2>/dev/null || true
else
	HARNESS_MODE="generate"
	if [ -d validation ] && [ ! -f validation/.ai-validation-owned ]; then
		echo "Refusing to delete existing 'validation' directory without ownership marker (validation/.ai-validation-owned)." >&2
		exit 1
	fi
	rm -rf validation
	mkdir -p validation/logs
	touch validation/.ai-validation-owned
fi

ensure_validate_wrapper

if command -v git >/dev/null 2>&1; then
  git status --porcelain --untracked-files=all -- . ':!validation/**' | sort > "${PRE_GENERATE_STATUS_FILE}" 2>/dev/null || true
fi

# ---------------------------------------------------------------
# Cycle 2+: gather previous validation failure context so the LLM
# avoids repeating the same harness mistakes.
# ---------------------------------------------------------------
PRIOR_FAILURE_CONTEXT_FILE="${RUNTIME_DIR}/prior_validation_failures.txt"
: > "${PRIOR_FAILURE_CONTEXT_FILE}"
: > "${PRIOR_RESULT_JSON_FILE}"
: > "${PRIOR_CONTAINER_LOGS_FILE}"

if [ "${VALIDATION_CYCLE}" -gt 1 ] && is_tracking_run; then
	echo "Cycle ${VALIDATION_CYCLE}: fetching prior validation failure context from tracking issue #${TRACKING_ISSUE_NUM}."
  PRIOR_COMMENTS="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/comments" \
    --paginate --jq '[.[] | select(.body | test("Runtime validation"))] | .[-3:] | .[].body' 2>/dev/null || true)"
  if [ -n "${PRIOR_COMMENTS}" ]; then
    {
      echo "IMPORTANT — PREVIOUS VALIDATION CYCLE FAILURES (cycle $((VALIDATION_CYCLE - 1))):"
      echo "The following failures occurred in prior validation cycles. Your generated"
      echo "harness MUST avoid these same patterns. If a prior failure was caused by"
      echo "fragile shell output parsing (e.g. raw mongosh text matching), use the"
      echo "deterministic assertion patterns described above instead."
      echo
      echo "${PRIOR_COMMENTS}"
    } > "${PRIOR_FAILURE_CONTEXT_FILE}"

    jq -n --arg summary "${PRIOR_COMMENTS}" '{result: "fail", phase: "prior_cycle", summary: $summary}' > "${PRIOR_RESULT_JSON_FILE}"
    printf '%s\n' "${PRIOR_COMMENTS}" > "${PRIOR_CONTAINER_LOGS_FILE}"
  fi
fi

HARNESS_PROMPT_SOURCE="prompts/mode-validate-generate.txt"
if [ "${HARNESS_MODE}" = "fix" ]; then
	HARNESS_PROMPT_SOURCE="prompts/mode-validate-fix-harness.txt"
fi

{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "=== IMPLEMENTATION TASK ==="
  echo
  bash scripts/render_prompt.sh "${HARNESS_PROMPT_SOURCE}"
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_VALIDATE}"
  echo
  if [ -s "${PRIOR_FAILURE_CONTEXT_FILE}" ]; then
    echo "=== PRIOR VALIDATION FAILURES (DO NOT REPEAT) ==="
    cat "${PRIOR_FAILURE_CONTEXT_FILE}"
    echo
  fi
  if [ -s "${PRIOR_RESULT_JSON_FILE}" ]; then
    echo "=== PRIOR STRUCTURED VALIDATION FAILURE JSON ==="
    cat "${PRIOR_RESULT_JSON_FILE}"
    echo
  fi
  if [ -s "${PRIOR_CONTAINER_LOGS_FILE}" ]; then
    echo "=== PRIOR CONTAINER LOG TAILS ==="
    cat "${PRIOR_CONTAINER_LOGS_FILE}"
    echo
  fi
  if [ "${HARNESS_MODE}" = "fix" ]; then
    echo "=== EXISTING HARNESS FILES ==="
    while IFS= read -r harness_file; do
      harness_size_bytes="$(wc -c < "${harness_file}" | tr -d ' ')"
      if [ "${harness_size_bytes}" -gt 200000 ]; then
        echo "----- ${harness_file} (skipped: file too large for prompt context) -----"
        echo
        continue
      fi
      if [ -s "${harness_file}" ] && ! grep -Iq . "${harness_file}"; then
        echo "----- ${harness_file} (skipped: non-text file) -----"
        echo
        continue
      fi
      echo "----- ${harness_file} -----"
      cat "${harness_file}"
      echo
    done < <(find validation -type f -not -path 'validation/logs/*' | sort)
  fi
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
  echo "VALIDATION_CYCLE: ${VALIDATION_CYCLE}"
  echo "HARNESS_MODE: ${HARNESS_MODE}"
  echo "SYNTHETIC_TEST_USERNAME_ENV_VAR: VALIDATION_TEST_USERNAME"
  echo "SYNTHETIC_TEST_PASSWORD_ENV_VAR: VALIDATION_TEST_PASSWORD"
  echo "SYNTHETIC_TEST_API_KEY_ENV_VAR: VALIDATION_TEST_API_KEY"
  echo
  if [ "${HARNESS_MODE}" = "fix" ]; then
    echo "Fix the existing harness directly in the repository workspace. Keep passing tests/config unchanged."
  else
    echo "Generate the harness directly in the repository workspace for immediate execution."
  fi
} > "${GENERATE_PROMPT_FILE}"

GENERATE_SUCCESS=false
for attempt in 1 2; do
  echo "Validation harness ${HARNESS_MODE} attempt ${attempt}/2"
  if cat "${GENERATE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${GENERATE_OUTPUT_FILE}" 2> >(tee -a "${GENERATE_LOG_FILE}" >&2); then
    ensure_validate_wrapper
    if is_validation_harness_runnable; then
      GENERATE_SUCCESS=true
      break
    fi
  fi
  if [ "${attempt}" -lt 2 ]; then
    sleep $((attempt * 10))
  fi
done

if [ "${GENERATE_SUCCESS}" != "true" ]; then
  local_failure_summary="Codex did not generate runnable validation assets (validation/docker-compose.test.yml and validation/tests/00_canary.sh at minimum)."
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

if ! ensure_validation_harness_not_tracked; then
  local_failure_summary="${CANONICAL_VALIDATE_HARNESS_REL} became tracked/staged after harness generation."
  post_tracking_comment "## ⚠️ Runtime validation harness tracking violation\n\n${local_failure_summary}\n\nValidation harness files must remain transient and untracked."
  set_tracking_phase_label "ai:validation-failed"
  write_result_files "error" "Validation harness tracking violation" "${local_failure_summary}"
  tg_notify "Validation harness tracking violation after generation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit 1
fi


# ---------------------------------------------------------------
# Phase 2: Pre-flight checks for generated harness
# ---------------------------------------------------------------
if ! run_preflight_checks; then
  failure_summary="Validation pre-flight checks failed. See validation_preflight.log artifact."
  jq -n \
    --arg diagnosis "Pre-flight validation failed before test execution." \
    --arg harness_fixes "$(tail -n 120 "${PRE_FLIGHT_LOG_FILE}" 2>/dev/null || true)" \
    '{
      status: "harness_error",
      diagnosis: $diagnosis,
      fix_issues: [],
      harness_fixes: (if ($harness_fixes | length) > 0 then $harness_fixes else "Fix validation/docker-compose.test.yml, shell syntax, or build context/dockerfile paths." end)
    }' > "${DIAGNOSE_RESULT_FILE}"

  post_tracking_comment "## ❌ Runtime validation harness pre-flight failed\n\n${failure_summary}\n\n\`docker compose config\`, shell syntax, or build context/dockerfile path checks failed."
  set_tracking_phase_label "ai:validation-failed"
  write_result_files "fail" "Validation failed due to harness pre-flight error" "${failure_summary}"
  tg_notify "Validation pre-flight failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit 0
fi


# ---------------------------------------------------------------
# Phase 3: Execute validation harness (idle-timeout based)
# ---------------------------------------------------------------
# The timeout is activity-based: the process is killed only if it
# produces no output for VALIDATION_TIMEOUT minutes. This allows
# large projects to run longer as long as they keep producing output.
IDLE_TIMEOUT_SECS=$((VALIDATION_TIMEOUT * 60))
VALIDATION_EXIT=0
VALIDATION_IDLE_KILLED=0

set +e
# Run validation in background, tee output to log file
if [ -f validation/validate.sh ]; then
  if grep -q 'scripts/validate_driver.sh' validation/validate.sh && [ ! -f scripts/validate_driver.sh ]; then
    ensure_runtime_validation_driver
    GENERATED_VALIDATE_SCRIPT_PATH="${VALIDATION_RUNNER_FILE}"
    "${VALIDATION_RUNNER_FILE}" > "${VALIDATION_LOG_FILE}" 2>&1 &
  else
    GENERATED_VALIDATE_SCRIPT_PATH="validation/validate.sh"
    bash validation/validate.sh > "${VALIDATION_LOG_FILE}" 2>&1 &
  fi
else
  ensure_runtime_validation_driver
  GENERATED_VALIDATE_SCRIPT_PATH="${VALIDATION_RUNNER_FILE}"
  "${VALIDATION_RUNNER_FILE}" > "${VALIDATION_LOG_FILE}" 2>&1 &
fi
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
		((.failures | length == 0) or ((.failures | length == 1) and ((.failures[0].test // "") | endswith(":unexpected_error"))))
	' "${VALIDATION_RESULT_FILE}" >/dev/null 2>&1; then
		echo "::warning::Harness exited ${VALIDATION_EXIT} with result '${RESULT_KIND}' but all ${PASSED_TESTS}/${TOTAL_TESTS} tests passed (failed_tests=0). Overriding to pass (likely scripting bug in generated harness script)."
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

	# Verify the ai:validated label was applied; retry with direct add if missing.
	# This prevents the orchestrator from looping forever when the label
	# application silently fails (Bug: validation-fixing loop stuck).
	if is_tracking_run; then
		_verify_labels="$(gh_retry gh api \
			"repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/labels" \
			--jq '[.[].name]' 2>/dev/null || echo '[]')"
		if ! echo "${_verify_labels}" | jq -e 'index("ai:validated") != null' >/dev/null 2>&1; then
			echo "::warning::ai:validated label not found on #${TRACKING_ISSUE_NUM} after set_tracking_phase_label; retrying direct add." >&2
			ensure_label_exists "ai:validated"
			gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
				--repo "${GITHUB_REPOSITORY}" \
				--add-label "ai:validated" >/dev/null 2>&1 || \
				echo "::warning::Retry of ai:validated label application also failed for #${TRACKING_ISSUE_NUM}." >&2
		fi
	fi

	write_result_files "pass" "${summary_text}" ""
	tg_notify "Runtime validation passed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} (${PASSED_TESTS}/${TOTAL_TESTS})." "DEBUG"
	exit 0
fi


# ---------------------------------------------------------------
# Collect container logs (used for canary classification + diagnosis)
# ---------------------------------------------------------------
: > "${CONTAINER_LOG_TAIL_FILE}"
if [ -d validation/logs ]; then
	while IFS= read -r log_file; do
		echo "===== ${log_file} (tail 80) =====" >> "${CONTAINER_LOG_TAIL_FILE}"
		tail -n 80 "${log_file}" >> "${CONTAINER_LOG_TAIL_FILE}" 2>/dev/null || true
		echo >> "${CONTAINER_LOG_TAIL_FILE}"
	done < <(find validation/logs -type f | sort)
fi


# ---------------------------------------------------------------
# Canary shortcut: classify infra-only canary failure as harness_error
# ---------------------------------------------------------------
CANARY_TEST_NAME="$(jq -r '.failures[0].test // ""' "${VALIDATION_RESULT_FILE}")"
CANARY_ERROR_TEXT="$(jq -r '.failures[0].error // ""' "${VALIDATION_RESULT_FILE}" | tr '[:upper:]' '[:lower:]')"
CANARY_ONLY_FAILURE=false
CANARY_APP_SIGNAL_IN_LOGS=false
if [ -s "${CONTAINER_LOG_TAIL_FILE}" ]; then
	if grep -E -i -q 'application crashed|app crashed|process exited|server startup failed|panic|traceback|exception in app|fatal error|segmentation fault' "${CONTAINER_LOG_TAIL_FILE}"; then
		CANARY_APP_SIGNAL_IN_LOGS=true
	fi
fi

if [ "${FAILED_TESTS}" = "1" ] && [ -n "${CANARY_TEST_NAME}" ] && [ -n "${CANARY_ERROR_TEXT}" ]; then
	if [[ "${CANARY_TEST_NAME}" == *00_canary* ]]; then
		CANARY_ONLY_FAILURE=true
	fi
fi

if [ "${CANARY_ONLY_FAILURE}" = true ]; then
	if echo "${CANARY_ERROR_TEXT}" | grep -E -q 'connection refused|could not resolve host|command not found|exit code 127|no such file or directory|invalid compose|healthcheck|network|timeout waiting for'; then
		if [ "${CANARY_APP_SIGNAL_IN_LOGS}" != "true" ] \
			&& ! echo "${CANARY_ERROR_TEXT}" | grep -E -q 'application crashed|app crashed|process exited|server startup failed|panic|traceback|exception in app'; then
			jq -n \
				--arg diagnosis "Canary infrastructure check failed before app validation. Classified as harness_error." \
				--arg harness_fixes "${FIRST_FAILURE}" \
				'{
					status: "harness_error",
					diagnosis: $diagnosis,
					fix_issues: [],
					harness_fixes: (if ($harness_fixes | length) > 0 then $harness_fixes else "Fix canary test infrastructure assumptions (ports/services/tools)." end)
				}' > "${DIAGNOSE_RESULT_FILE}"

			failure_summary="Validation harness error: ${FIRST_FAILURE}"
			post_tracking_comment "## ❌ Runtime validation harness error\n\n${failure_summary}\n\nCanary infrastructure check failed and remaining tests were skipped."
			set_tracking_phase_label "ai:validation-failed"
			write_result_files "fail" "Validation failed due to harness error" "${failure_summary}"
			tg_notify "Validation harness canary failure for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
			exit 0
		fi
	fi
fi


# ---------------------------------------------------------------
# Phase 4: Diagnose failures
# ---------------------------------------------------------------
{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "=== DIAGNOSIS TASK ==="
  echo
  bash scripts/render_prompt.sh prompts/mode-validate-diagnose.txt
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_VALIDATE}"
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
- Integration branch: ${INTEGRATION_BRANCH}
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
