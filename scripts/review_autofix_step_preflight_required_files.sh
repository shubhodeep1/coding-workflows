#!/usr/bin/env bash
# Body of the "Preflight: Verify required files before reviewer invocation" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

abs_path() {
  local p="$1"
  if [ -z "${p}" ]; then
    p="."
  fi
  if [[ "${p}" = /* ]]; then
    realpath -m "${p}"
  else
    realpath -m "$(pwd)/${p}"
  fi
}

declare -a missing=()
preflight_reviewer_model="$(printf '%s\n' "${REVIEWER_MODELS:-}" | tr ',' '\n' | sed -n '/[^[:space:]]/ { s/^[[:space:]]*//; s/[[:space:]]*$//; p; q; }')"
if [ -z "${preflight_reviewer_model}" ]; then
  preflight_reviewer_model="unknown"
fi
preflight_reviewer_model="$(printf '%s' "${preflight_reviewer_model}" | LC_ALL=C tr -c 'A-Za-z0-9_.:/+-' '_')"

emit_opencode_preflight_alert() {
  local alert_rc="$1"
  local failure_class="$2"
  local alert_payload
  if [ -f "${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh" ]; then
    # shellcheck source=/dev/null
    source "${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh" 2>/dev/null || true
  fi
  if type opencode_emit_failure_alert >/dev/null 2>&1; then
    opencode_emit_failure_alert review_autofix_preflight reviewer "${preflight_reviewer_model}" "${alert_rc}" "${failure_class}" || true
    return 0
  fi
  alert_payload="opencode_agent_failure phase=review_autofix_preflight role=reviewer model=${preflight_reviewer_model} rc=${alert_rc} failure_class=${failure_class}"
  if ! type tg_send_msg >/dev/null 2>&1 && [ -r "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh" ]; then
    # shellcheck source=/dev/null
    source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh" 2>/dev/null || true
  fi
  if type tg_send_msg >/dev/null 2>&1; then
    tg_send_msg "${alert_payload}" ERROR >/dev/null || true
  fi
  echo "${alert_payload}" >&2
}

check_required_dir() {
  local path="$1"
  local abs
  abs="$(abs_path "${path}")"
  if [ ! -d "${path}" ]; then
    echo "MISSING: ${abs}"
    missing+=("${abs}")
  fi
}

check_required_file() {
  local path="$1"
  local abs
  abs="$(abs_path "${path}")"
  if [ ! -f "${path}" ]; then
    echo "MISSING: ${abs}"
    missing+=("${abs}")
  fi
}

check_required_nonempty_file() {
  local path="$1"
  local abs
  abs="$(abs_path "${path}")"
  if [ ! -s "${path}" ]; then
    echo "MISSING_OR_EMPTY: ${abs}"
    missing+=("${abs} (missing or empty)")
  fi
}

check_soft_file() {
  local path="$1"
  local abs
  abs="$(abs_path "${path}")"
  if [ ! -f "${path}" ]; then
    echo "::warning::SOFT_MISSING: ${abs}"
  fi
}

check_required_dir "${RUNTIME_DIR}"
check_required_dir "${PREVIOUS_REVIEWS_DIR}"
check_required_dir "${RUNTIME_CONTEXT_DIR}"
check_required_dir "${SUPPORT_SCRIPTS_DIR}"
check_required_dir "${SUPPORT_PROMPTS_DIR}"

if [ ! -f "${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh" ]; then
  emit_opencode_preflight_alert 1 helpers_missing
fi
if [ ! -f "${SUPPORT_SCRIPTS_DIR}/write_opencode_config.sh" ]; then
  emit_opencode_preflight_alert 1 config_writer_missing
fi
for f in ${REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS}; do
  check_required_file "${SUPPORT_SCRIPTS_DIR}/${f}"
done
for f in ${REVIEW_PREFLIGHT_SOFT_SUPPORT_SCRIPTS}; do
  check_soft_file "${SUPPORT_SCRIPTS_DIR}/${f}"
done
check_required_nonempty_file "${SUPPORT_INSTRUCTIONS_FILE}"
check_required_file "./pre_assembled_static.txt"
check_required_file "${PR_DIFF_FILE}"
check_required_file "${ORIGINAL_PR_DIFF_FILE}"
check_required_file "${PR_CHANGED_FILES_FILE}"
check_required_file "${PR_ALL_COMMENTS_CONTEXT_FILE}"
check_required_file "${PR_CHECK_RUNS_CONTEXT_FILE}"
check_required_file "${SYMBOL_DIFF_SUMMARY_FILE}"
check_required_file "${MEMORY_CONTEXT_FILE}"
if ! opencode_bin="$(command -v opencode 2>/dev/null)"; then
  opencode_missing_message="opencode binary (not found in PATH)"
  echo "MISSING: ${opencode_missing_message}"
  missing+=("${opencode_missing_message}")
  emit_opencode_preflight_alert 127 binary_missing
else
  echo "Found opencode binary: ${opencode_bin}"
fi

check_soft_file "${SUPPORT_CODEX_INSTRUCTIONS_FILE}"
check_soft_file "${SUPPORT_AGENTS_FILE}"
check_soft_file "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"
check_soft_file "${SUPPORT_PROMPTS_DIR}/mode-judge-interim.txt"
check_soft_file "${SUPPORT_PROMPTS_DIR}/behavioural-smoke-synthesise.txt"
check_soft_file "${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt"
check_soft_file "${SUPPORT_PROMPTS_DIR}/_nag_reminders.txt"
case "$(printf '%s' "${REVIEW_REVIEWER_CHECKLIST_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) check_soft_file "${SUPPORT_PROMPTS_DIR}/review-reviewer-checklist.txt" ;;
esac
case "$(printf '%s' "${REVIEWER_FILTER_UNINTERESTING_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) check_soft_file "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh" ;;
esac
case "$(printf '%s' "${AGENTS_MD_MATERIALITY_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) check_soft_file "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh" ;;
esac
case "$(printf '%s' "${REVIEWER_CIRCUIT_BREAKER_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) check_soft_file "${SUPPORT_SCRIPTS_DIR}/reviewer_failback_chains.json" ;;
esac
check_soft_file "${SUPPORT_SCRIPTS_DIR}/codex_model_catalog.json"
check_soft_file "${SUPPORT_SCRIPTS_DIR}/install_semble.sh"
check_soft_file "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh"
check_soft_file "${LAST_RUN_DIFF_FILE}"
check_soft_file "${LAST_RUN_CHANGED_FILES_FILE}"
check_soft_file "${LAST_RUN_DIFF_STAT_FILE}"
check_soft_file "${LAST_COMMIT_STAT_FILE}"

if [ "${#missing[@]}" -gt 0 ]; then
  echo "Preflight check FAILED: ${#missing[@]} required file(s) missing:"
  printf '  %s\n' "${missing[@]}"
  exit 1
fi
echo "Preflight check PASSED: all required files present."

# Editor preflight: run the staged editor script's precondition
# guards now, so a deterministic editor failure (PR #4259: a
# `: "${VAR:?…}"` guard) fails here in seconds instead of after the
# reviewers. Skipped when the editor is not scheduled for this run,
# and fail-open for a staged copy that predates --preflight. On
# failure the run takes the ordinary failure path with
# EDITOR_PREFLIGHT_FAILED=true (failure_reason editor_preflight_failed).
# Kill switch: REVIEW_EDITOR_PREFLIGHT_ENABLED=false.
case "$(printf '%s' "${REVIEW_EDITOR_PREFLIGHT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off)
    echo "REVIEW_EDITOR_PREFLIGHT skip reason=disabled script_ref=${SCRIPT_REF:-unknown}"
    ;;
  *)
    if [ "${CLAUDE_BRANCH_REVIEW_MODE:-false}" = "true" ] || [ "${AUTOFIX_RESUME_TERMINAL:-false}" = "true" ]; then
      echo "REVIEW_EDITOR_PREFLIGHT skip reason=editor_not_scheduled script_ref=${SCRIPT_REF:-unknown}"
    elif ! grep -q '^# supports: --preflight' "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" 2>/dev/null; then
      echo "REVIEW_EDITOR_PREFLIGHT skip reason=unsupported script_ref=${SCRIPT_REF:-unknown}"
    else
      editor_preflight_rc=0
      bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" --preflight 2> >(tee -a "${RUNTIME_DIR}/editor_stage_stderr.txt" >&2) || editor_preflight_rc=$?
      if [ "${editor_preflight_rc}" -ne 0 ]; then
        echo "EDITOR_PREFLIGHT_FAILED=true" >> "$GITHUB_ENV"
        echo "::error::REVIEW_EDITOR_PREFLIGHT result=fail rc=${editor_preflight_rc} script_ref=${SCRIPT_REF:-unknown}"
        exit 1
      fi
    fi
    ;;
esac
