#!/usr/bin/env bash
# orchestrate_poll_process.sh — Process active orchestrator tracking issues.
# Extracted from orchestrate_poll.yml to stay within GitHub Actions
# expression length limits (21 000 chars max per run block).
#
# Required env vars (set by the workflow step):
#   RUNTIME_DIR, STATE_FILE, JUDGE_PROMPT_FILE, JUDGE_OUTPUT_FILE,
#   GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY,
#   MODEL_EDITOR, MODEL_REASONING_EFFORT_JUDGE,
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID, TOOL_CALL_BUDGET_JUDGE,
#   SERENA_VERSION, SERENA_LANGUAGES, SERENA_DISABLED, SERENA_IGNORED_DIRS,
#   CONTEXT7_DISABLED, GIT_MCP_DISABLED

set -euo pipefail

# ---------------------------------------------------------------
# Helper: Telegram (tracked via tg_helpers.sh)
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

# Process-lifetime cache of labels we've already verified exist on the
# repo.  `ensure_label_exists` is called from 10+ code paths with a
# small, repeating set of label names; each call used to hit
# /repos/{owner}/{repo}/labels/{name} even when the same label had
# already been confirmed earlier in the same cycle.  Labels are
# persistent on the repo side, so caching within a single orchestrator
# invocation is safe and collapses dozens of API calls into a handful.
declare -gA _ENSURED_LABELS_CACHE=()

# _gh_url constructs a full GitHub URL for the current repository.
_gh_url() {
  if [ -z "${GITHUB_REPOSITORY:-}" ]; then
    printf ''
    return
  fi
  printf '%s/%s/%s' "${GITHUB_SERVER_URL:-https://github.com}" "${GITHUB_REPOSITORY}" "$1"
}

# tg_notify wraps tg_send_tracked using the current TRACKING_NUM.
# TRACKING_NUM is set inside the main per-issue loop below.
# Automatically appends tracking issue link and Actions run link.
tg_notify() {
  local msg="$1"
  local level="${2:-CRITICAL}"
  local tracking_url run_url

  if [ -n "${TRACKING_NUM:-}" ] && [ "${TRACKING_NUM}" != "0" ]; then
    tracking_url="$(_gh_url "issues/${TRACKING_NUM}")"
    if [ -n "${tracking_url}" ]; then
      msg+=$'\n'"Tracking: ${tracking_url}"
    fi
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    run_url="$(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
    if [ -n "${run_url}" ]; then
      msg+=$'\n'"Run: ${run_url}"
    fi
  fi
  if [ -n "${TRACKING_NUM:-}" ]; then
    tg_send_tracked "${TRACKING_NUM}" "${msg}" "${level}"
  else
    # Fallback: untracked send (no issue context yet)
    tg_send_msg "${msg}" "${level}" >/dev/null
  fi
}

# tg_notify_issue sends a Telegram alert for a standalone issue (no tracking issue).
tg_notify_issue() {
  local issue_num="$1"
  local msg="$2"
  local level="${3:-CRITICAL}"
  local issue_url run_url

  issue_url="$(_gh_url "issues/${issue_num}")"
  if [ -n "${issue_url}" ]; then
    msg+=$'\n'"Issue: ${issue_url}"
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    run_url="$(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
    if [ -n "${run_url}" ]; then
      msg+=$'\n'"Run: ${run_url}"
    fi
  fi

  tg_send_msg "${msg}" "${level}" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------
# Helper: GitHub API with retry
# ---------------------------------------------------------------
# gh_retry is provided by scripts/gh_helpers.sh (rate-limit-aware).
# Fallback definition in case gh_helpers.sh was not sourced.
if ! type gh_retry >/dev/null 2>&1; then
  gh_retry() { "$@"; }
fi

is_truthy() {
  local value
  value="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "${value}" in
    1|true|yes|on)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

assemble_judge_static_context() {
  local out_file="$1"
  local missing=""

  if [ ! -s codex_system_instructions.md ]; then
    missing="codex_system_instructions.md"
  fi
  if [ ! -s ai_pipeline.md ]; then
    missing="${missing}${missing:+, }ai_pipeline.md"
  fi
  if [ -n "${missing}" ]; then
    echo "::error::Required file(s) missing or empty: ${missing}" >&2
    return 1
  fi

  {
    echo "=== SYSTEM INSTRUCTIONS ==="
    cat codex_system_instructions.md
    echo
    echo "=== AI PIPELINE ==="
    cat ai_pipeline.md
    echo
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
  } > "${out_file}"
}

# ---------------------------------------------------------------
# Helper: Check whether all check-runs on a PR's head commit have
# completed.  Returns 0 when every check-run has status "completed"
# and an acceptable conclusion (success/neutral/skipped/cancelled),
# 1 otherwise (including API errors).  Callers should skip the merge
# when this returns non-zero so we never merge while checks (e.g.
# autofix) are still running.
# Usage:  _pr_checks_completed <PR_NUMBER> [<HEAD_SHA>]
#   HEAD_SHA is optional; when provided the extra PR fetch is skipped.
# ---------------------------------------------------------------
_pr_checks_completed()
{
	local pr_number="$1"
	local head_sha="${2:-}"
	if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
		local pr_json
		pr_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" || echo "")"
		head_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' 2>/dev/null | tail -n1)"
	fi
	if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
		echo "  [check-runs] Could not resolve head SHA for PR #${pr_number}. Skipping merge."
		return 1
	fi

	local check_runs_json
	check_runs_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/commits/${head_sha}/check-runs?per_page=100" || echo "")"

	local incomplete
	incomplete="$(printf '%s' "${check_runs_json}" | jq -r '
		if (type == "object" and (.check_runs | type == "array")) then
			[.check_runs[] | select(.status != "completed" or (.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled"))] | length
		else
			empty
		end
	' 2>/dev/null | tail -n1)"
	if ! [[ "${incomplete}" =~ ^[0-9]+$ ]]; then
		echo "  [check-runs] Could not query check-runs for PR #${pr_number} (SHA ${head_sha:0:7}). Skipping merge."
		return 1
	fi

	if [ "${incomplete}" -gt 0 ]; then
		echo "  [check-runs] PR #${pr_number} has ${incomplete} blocking check-run(s) (SHA ${head_sha:0:7}). Skipping merge."
		return 1
	fi

		echo "  [check-runs] All check-runs completed and acceptable for PR #${pr_number} (SHA ${head_sha:0:7}). Proceeding with merge."
	return 0
}

# ---------------------------------------------------------------
# Helper: Fetch PR JSON once and extract multiple fields.
#
# Reduces GitHub API calls by fetching the PR endpoint once instead
# of making separate calls for state, mergeable, merged, head SHA,
# head ref, and base ref.
#
# Usage:
#   local pr_json
#   pr_json="$(_fetch_pr_json "${PR_NUMBER}")"
#   PR_STATE="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
#   PR_MERGEABLE="$(_jq_field "${pr_json}" '.mergeable' 'true|false')"
# ---------------------------------------------------------------
_fetch_pr_json()
{
	local pr_number="$1"
	gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" || echo '{}'
}

# _jq_field — Extract a field from JSON, optionally validating against
# a grep pattern.  Returns empty string on mismatch or null.
_jq_field()
{
	local json="$1"
	local expr="$2"
	local pattern="${3:-}"
	local val
	val="$(echo "${json}" | jq -r "${expr} | if . == null then \"\" else tostring end" 2>/dev/null || echo "")"
	if [ -n "${pattern}" ]; then
		echo "${val}" | grep -xE "${pattern}" || echo ""
	else
		echo "${val}"
	fi
}

ENABLE_VALIDATION_RAW="${ENABLE_VALIDATION:-true}"
ENABLE_VALIDATION="false"
if is_truthy "${ENABLE_VALIDATION_RAW}"; then
  ENABLE_VALIDATION="true"
fi

MAX_VALIDATE_CYCLES="${MAX_VALIDATE_CYCLES:-3}"
if ! [[ "${MAX_VALIDATE_CYCLES}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATE_CYCLES}" -lt 1 ]; then
  echo "::warning::MAX_VALIDATE_CYCLES must be a positive integer; defaulting to 3"
  MAX_VALIDATE_CYCLES="3"
fi

# Stall detection settings
STALL_THRESHOLD_MINUTES="${STALL_THRESHOLD_MINUTES:-120}"
if ! [[ "${STALL_THRESHOLD_MINUTES}" =~ ^[0-9]+$ ]] || [ "${STALL_THRESHOLD_MINUTES}" -lt 1 ]; then
  echo "::warning::STALL_THRESHOLD_MINUTES must be a positive integer; defaulting to 120"
  STALL_THRESHOLD_MINUTES="120"
fi

# Per-phase stall thresholds (override the global fallback above).
# Each var maps to a pipeline phase label.  Unset vars use the built-in
# defaults in orchestrate_lib.py (60 min for lightweight phases, 120 min
# for heavy phases).
_validate_phase_threshold() {
  local var_name="$1"
  local val="${!var_name:-}"
  if [ -n "${val}" ]; then
    if ! [[ "${val}" =~ ^[0-9]+$ ]] || [ "${val}" -lt 1 ]; then
      echo "::warning::${var_name} must be a positive integer; ignoring invalid value '${val}'"
      eval "${var_name}="
    fi
  fi
}

STALL_THRESHOLD_NO_LABELS_MINUTES="${STALL_THRESHOLD_NO_LABELS_MINUTES:-}"
STALL_THRESHOLD_CLARIFICATION_MINUTES="${STALL_THRESHOLD_CLARIFICATION_MINUTES:-}"
STALL_THRESHOLD_PLANNING_MINUTES="${STALL_THRESHOLD_PLANNING_MINUTES:-}"
STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES="${STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES:-}"
STALL_THRESHOLD_IMPLEMENTING_MINUTES="${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}"
STALL_THRESHOLD_DONE_MINUTES="${STALL_THRESHOLD_DONE_MINUTES:-}"
STALL_THRESHOLD_READY_TO_MERGE_MINUTES="${STALL_THRESHOLD_READY_TO_MERGE_MINUTES:-}"

_validate_phase_threshold STALL_THRESHOLD_NO_LABELS_MINUTES
_validate_phase_threshold STALL_THRESHOLD_CLARIFICATION_MINUTES
_validate_phase_threshold STALL_THRESHOLD_PLANNING_MINUTES
_validate_phase_threshold STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES
_validate_phase_threshold STALL_THRESHOLD_IMPLEMENTING_MINUTES
_validate_phase_threshold STALL_THRESHOLD_DONE_MINUTES
_validate_phase_threshold STALL_THRESHOLD_READY_TO_MERGE_MINUTES

# Build the JSON dict for per-phase overrides (only include vars that are set).
_build_phase_thresholds_json() {
  local parts=()
  [ -n "${STALL_THRESHOLD_NO_LABELS_MINUTES:-}" ] && parts+=("\"no_labels\":${STALL_THRESHOLD_NO_LABELS_MINUTES}")
  [ -n "${STALL_THRESHOLD_CLARIFICATION_MINUTES:-}" ] && parts+=("\"ai:clarification\":${STALL_THRESHOLD_CLARIFICATION_MINUTES}")
  [ -n "${STALL_THRESHOLD_PLANNING_MINUTES:-}" ] && parts+=("\"ai:planning\":${STALL_THRESHOLD_PLANNING_MINUTES}")
  [ -n "${STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES:-}" ] && parts+=("\"ai:awaiting-approval\":${STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES}")
  [ -n "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ] && parts+=("\"ai:implementing\":${STALL_THRESHOLD_IMPLEMENTING_MINUTES}")
  [ -n "${STALL_THRESHOLD_DONE_MINUTES:-}" ] && parts+=("\"ai:done\":${STALL_THRESHOLD_DONE_MINUTES}")
  [ -n "${STALL_THRESHOLD_READY_TO_MERGE_MINUTES:-}" ] && parts+=("\"ai:ready-to-merge\":${STALL_THRESHOLD_READY_TO_MERGE_MINUTES}")

  if [ "${#parts[@]}" -eq 0 ]; then
    echo ""
    return
  fi

  local IFS=','
  echo "{${parts[*]}}"
}

PHASE_THRESHOLDS_JSON="$(_build_phase_thresholds_json)"

MAX_STALL_RECOVERIES_PER_ISSUE="${MAX_STALL_RECOVERIES_PER_ISSUE:-5}"
if ! [[ "${MAX_STALL_RECOVERIES_PER_ISSUE}" =~ ^[0-9]+$ ]] || [ "${MAX_STALL_RECOVERIES_PER_ISSUE}" -lt 1 ]; then
  echo "::warning::MAX_STALL_RECOVERIES_PER_ISSUE must be a positive integer; defaulting to 5"
  MAX_STALL_RECOVERIES_PER_ISSUE="5"
fi

# Stall judge trigger configuration
STALL_JUDGE_TRIGGER_COUNT="${STALL_JUDGE_TRIGGER_COUNT:-2}"
if ! [[ "${STALL_JUDGE_TRIGGER_COUNT}" =~ ^[0-9]+$ ]] || [ "${STALL_JUDGE_TRIGGER_COUNT}" -lt 1 ]; then
  echo "::warning::STALL_JUDGE_TRIGGER_COUNT must be a positive integer; defaulting to 2"
  STALL_JUDGE_TRIGGER_COUNT="2"
fi

ENABLE_STALL_JUDGE="${ENABLE_STALL_JUDGE:-true}"
if is_truthy "${ENABLE_STALL_JUDGE}"; then
  ENABLE_STALL_JUDGE="true"
else
  ENABLE_STALL_JUDGE="false"
fi

ENABLE_STANDALONE_STALL_RECOVERY="${ENABLE_STANDALONE_STALL_RECOVERY:-true}"
if is_truthy "${ENABLE_STANDALONE_STALL_RECOVERY}"; then
  ENABLE_STANDALONE_STALL_RECOVERY="true"
else
  ENABLE_STANDALONE_STALL_RECOVERY="false"
fi

ENABLE_CLOSE_MERGED_ISSUES="${ENABLE_CLOSE_MERGED_ISSUES:-true}"
if is_truthy "${ENABLE_CLOSE_MERGED_ISSUES}"; then
  ENABLE_CLOSE_MERGED_ISSUES="true"
else
  ENABLE_CLOSE_MERGED_ISSUES="false"
fi

MAX_RECOVERY_ATTEMPTS="${MAX_RECOVERY_ATTEMPTS:-3}"
if ! [[ "${MAX_RECOVERY_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_RECOVERY_ATTEMPTS}" -lt 1 ]; then
  echo "::warning::MAX_RECOVERY_ATTEMPTS must be a positive integer; defaulting to 3"
  MAX_RECOVERY_ATTEMPTS="3"
fi

MAX_VALIDATION_RECOVERY_ATTEMPTS="${MAX_VALIDATION_RECOVERY_ATTEMPTS:-2}"
if ! [[ "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" -lt 0 ]; then
  echo "::warning::MAX_VALIDATION_RECOVERY_ATTEMPTS must be a non-negative integer; defaulting to 2"
  MAX_VALIDATION_RECOVERY_ATTEMPTS="2"
fi

STALL_RECOVERY_SHOULD_INCREMENT="false"
STALL_RECOVERY_EFFECTIVE_ACTION=""
STALL_JUDGE_TARGET_PR=""
STALL_JUDGE_HEAD_REF=""

# ---------------------------------------------------------------
# Integration-branch self-healing knobs
# ---------------------------------------------------------------
# CONFLICT_DISPATCH_COOLDOWN_SECS throttles how often the poller may
# dispatch the review/autofix workflow against the integration branch's
# final PR. Without throttling, every poll tick would re-dispatch while
# the previous resolver is still running.
#
# INTEGRATION_CONFLICT_MAX_RETRIES is the circuit-breaker for the
# resolver dispatch loop. After this many consecutive unresolved
# conflict ticks, the orchestrator escalates to the judge instead of
# continuing to spam resolver dispatches.
CONFLICT_DISPATCH_COOLDOWN_SECS="${CONFLICT_DISPATCH_COOLDOWN_SECS:-900}"
if ! [[ "${CONFLICT_DISPATCH_COOLDOWN_SECS}" =~ ^[0-9]+$ ]] || [ "${CONFLICT_DISPATCH_COOLDOWN_SECS}" -lt 0 ]; then
  echo "::warning::CONFLICT_DISPATCH_COOLDOWN_SECS must be a non-negative integer; defaulting to 900"
  CONFLICT_DISPATCH_COOLDOWN_SECS="900"
fi

INTEGRATION_CONFLICT_MAX_RETRIES="${INTEGRATION_CONFLICT_MAX_RETRIES:-3}"
if ! [[ "${INTEGRATION_CONFLICT_MAX_RETRIES}" =~ ^[0-9]+$ ]] || [ "${INTEGRATION_CONFLICT_MAX_RETRIES}" -lt 1 ]; then
  echo "::warning::INTEGRATION_CONFLICT_MAX_RETRIES must be a positive integer; defaulting to 3"
  INTEGRATION_CONFLICT_MAX_RETRIES="3"
fi

post_tracking_comment() {
  local comment_body="$1"
  local payload_file
  local payload_err_file
  payload_file="$(mktemp "${TMPDIR:-/tmp}/comment_payload.XXXXXX")"
  payload_err_file="$(mktemp "${TMPDIR:-/tmp}/comment_payload_err.XXXXXX")"
  if ! jq -n --arg body "${comment_body}" '{body: $body}' > "${payload_file}" 2>"${payload_err_file}"; then
    echo "::warning::Failed to encode tracking comment JSON payload for issue #${TRACKING_NUM}: $(cat "${payload_err_file}" 2>/dev/null)" >&2
    rm -f "${payload_err_file}"
    rm -f "${payload_file}"
    return 0
  fi
  rm -f "${payload_err_file}"
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    --method POST \
    --input "${payload_file}" >/dev/null || true
  rm -f "${payload_file}"
}

post_state_comment() {
  local state_comment
  state_comment="<!-- ORCHESTRATOR_STATE_V1
$(cat "${STATE_FILE}")
ORCHESTRATOR_STATE_V1 -->"
  post_tracking_comment "${state_comment}"
}

extract_orchestrator_state_payload() {
  local comment_body="$1"
  printf '%s' "${comment_body}" | sed -n '/^<!-- ORCHESTRATOR_STATE_V1$/,/^ORCHESTRATOR_STATE_V1 -->$/p' | sed '1d;$d'
}

is_valid_orchestrator_state_json() {
  local state_json="$1"
  printf '%s' "${state_json}" | jq -e '
    type == "object" and
    (.schema_version == "orchestrate_state.v1") and
    (.status | type == "string") and
    (.waves | type == "array") and
    (.current_wave | type == "number") and
    (.total_waves | type == "number") and
    (.issue_number_map | type == "object") and
    (.pending_issue_defs | type == "object")
  ' >/dev/null 2>&1
}

extract_latest_valid_orchestrator_state() {
  local comments_json="$1"
  local candidate
  local candidate_body
  local candidate_state
  local candidate_id
  local latest_state_comment_id=""

  EXTRACTED_STATE_JSON=""
  EXTRACTED_STATE_FALLBACK_USED="false"
  EXTRACTED_STATE_COMMENT_COUNT=0

  while IFS= read -r candidate; do
    [ -n "${candidate}" ] || continue
    EXTRACTED_STATE_COMMENT_COUNT=$((EXTRACTED_STATE_COMMENT_COUNT + 1))
    candidate_id="$(printf '%s' "${candidate}" | jq -r '.id // empty' 2>/dev/null || echo "")"
    [ -n "${latest_state_comment_id}" ] || latest_state_comment_id="${candidate_id}"
    candidate_body="$(printf '%s' "${candidate}" | jq -r '.body // ""' 2>/dev/null || echo "")"
    candidate_state="$(extract_orchestrator_state_payload "${candidate_body}")"
    [ -n "${candidate_state}" ] || continue
    if is_valid_orchestrator_state_json "${candidate_state}"; then
      EXTRACTED_STATE_JSON="${candidate_state}"
      if [ -n "${latest_state_comment_id}" ] && [ "${candidate_id}" != "${latest_state_comment_id}" ]; then
        EXTRACTED_STATE_FALLBACK_USED="true"
      fi
      return 0
    fi
  done < <(printf '%s' "${comments_json}" | jq -c '[.[] | select((.body // "") | contains("ORCHESTRATOR_STATE_V1"))] | reverse | .[]?' 2>/dev/null || true)

  return 1
}

ensure_label_exists() {
  local label_name="$1"

  # Fast path: already verified (or created) earlier in this process.
  # Labels are persistent on the repo so a prior confirmation is still
  # valid for the lifetime of this orchestrator invocation.
  if [ -n "${_ENSURED_LABELS_CACHE[${label_name}]+set}" ]; then
    return 0
  fi

  local contract_file=".github/ai/label_contract.v1.json"
  local color="1d76db"
  local description="AI workflow label"
  local contract_color=""
  local contract_description=""

  if [ "${label_name}" = "ai:closed" ]; then
    color="6a737d"
    description="Linked PR closed without merge"
  elif [ "${label_name}" = "ai:ready-to-merge" ]; then
    color="0e8a16"
    description="PR review complete and ready to merge"
  fi

  if [ -f "${contract_file}" ]; then
    contract_color="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].color // empty' "${contract_file}" 2>/dev/null || echo "")"
    contract_description="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].description // empty' "${contract_file}" 2>/dev/null || echo "")"
    [ -n "${contract_color}" ] && color="${contract_color}"
    [ -n "${contract_description}" ] && description="${contract_description}"
  fi

  # Check if label already exists to avoid futile retries (gh label create
  # returns a non-zero exit code for existing labels, which is not transient).
  local encoded_name
  encoded_name="$(printf '%s' "${label_name}" | jq -sRr @uri)"
  if gh api "repos/${GITHUB_REPOSITORY}/labels/${encoded_name}" >/dev/null 2>&1; then
    _ENSURED_LABELS_CACHE[${label_name}]=1
    return 0
  fi

  if gh_retry gh label create "${label_name}" \
    --repo "${GITHUB_REPOSITORY}" \
    --color "${color}" \
    --description "${description}" >/dev/null 2>&1; then
    _ENSURED_LABELS_CACHE[${label_name}]=1
    return 0
  fi

  # Creation can fail if another concurrent actor created the label first.
  # Cache only when a follow-up read confirms the label now exists.
  if gh api "repos/${GITHUB_REPOSITORY}/labels/${encoded_name}" >/dev/null 2>&1; then
    _ENSURED_LABELS_CACHE[${label_name}]=1
  fi
}

set_tracking_phase_label() {
  local phase_label="$1"
  local contract_file=".github/ai/label_contract.v1.json"

  ensure_label_exists "${phase_label}"

  if [ ! -f "${contract_file}" ]; then
    echo "::warning::set_tracking_phase_label: missing label contract ${contract_file}; cannot apply '${phase_label}' safely." >&2
    return 1
  fi

  local phase_changes
  local _resolve_err_file
  _resolve_err_file="$(mktemp)"
  if ! phase_changes="$(python3 scripts/ai_labels.py resolve-phase --contract-file "${contract_file}" --phase "${phase_label}" 2>"${_resolve_err_file}")"; then
    local _resolve_err
    _resolve_err="$(tr '\n' ' ' < "${_resolve_err_file}" 2>/dev/null || true)"
    rm -f "${_resolve_err_file}"
    echo "::warning::set_tracking_phase_label: resolve-phase failed for '${phase_label}' using ${contract_file}: ${_resolve_err:-<no stderr captured>}" >&2
    return 1
  fi
  rm -f "${_resolve_err_file}"

  # Fetch current labels on the issue so we only attempt to remove labels
  # that are actually present.  Trying to remove a label that does not
  # exist on the issue can cause `gh issue edit` to return an error,
  # which the outer `|| true` would silently swallow — leaving stale
  # labels (e.g. ai:validating + ai:validation-fixing) in place even
  # after the phase has advanced to ai:validated.
  local current_issue_labels
  current_issue_labels="$(get_issue_labels_json "${TRACKING_NUM}")"

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
    if ! gh_retry gh issue edit "${TRACKING_NUM}" \
      --repo "${GITHUB_REPOSITORY}" \
      "${edit_args[@]}" >/dev/null 2>"${_label_err_file}"; then
      local _label_err
      _label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
      if echo "${_label_err}" | grep -Eqi "could not remove label:|['\"][[:alnum:]:._/-]+['\"] not found"; then
        echo "::warning::set_tracking_phase_label: non-fatal missing label while applying '${phase_label}' to #${TRACKING_NUM}: ${_label_err}" >&2
        rm -f "${_label_err_file}"
        return 0
      fi
      echo "::warning::set_tracking_phase_label: failed to apply '${phase_label}' to #${TRACKING_NUM}: ${_label_err}" >&2
      rm -f "${_label_err_file}"
      return 1
    fi
    rm -f "${_label_err_file}"
  fi
  return 0
}

get_issue_labels_json() {
  local issue_num="$1"
  gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/labels" --jq '[.[].name]' || echo '[]'
}

has_label() {
  local labels_json="$1"
  local label="$2"
  echo "${labels_json}" | jq -e --arg label "${label}" 'index($label) != null' >/dev/null 2>&1
}

validation_fix_issue_has_merged_pr_evidence() {
  local issue_num="$1"
  local timeline_json
  local pr_urls
  local pr_url
  local pr_json
  local lookup_failed="false"
  local github_api_base="${GITHUB_API_URL:-https://api.github.com}"
  local pr_api_prefix="${github_api_base}/repos/${GITHUB_REPOSITORY}/pulls/"

  if ! timeline_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" 2>/dev/null | jq -s 'add // []' 2>/dev/null)"; then
    return 2
  fi

  if ! echo "${timeline_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
    return 2
  fi

  pr_urls="$(echo "${timeline_json}" | jq -r '[.[] | select(.event == "cross-referenced" and (.source.issue.pull_request.url? | type == "string")) | .source.issue.pull_request.url] | unique | .[]?' 2>/dev/null || true)"
  if [ -z "${pr_urls}" ]; then
    return 1
  fi

  while IFS= read -r pr_url; do
    [ -n "${pr_url}" ] || continue
    if [[ "${pr_url}" != "${pr_api_prefix}"* ]]; then
      continue
    fi

    if ! pr_json="$(gh_retry gh api "${pr_url}" 2>/dev/null)"; then
      lookup_failed="true"
      continue
    fi

    if ! echo "${pr_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
      lookup_failed="true"
      continue
    fi

    if echo "${pr_json}" | jq -e '(.merged_at != null) or (.merged == true)' >/dev/null 2>&1; then
      return 0
    fi
  done <<< "${pr_urls}"

  if [ "${lookup_failed}" = "true" ]; then
    return 2
  fi

  return 1
}

backfill_validation_fix_issue_merged_label() {
  local issue_num="$1"
  local fix_labels
  local edit_args=()
  local _label_err_file

  ensure_label_exists "ai:merged"

  fix_labels="$(get_issue_labels_json "${issue_num}")"
  if has_label "${fix_labels}" "ai:merged"; then
    return 0
  fi

  edit_args+=(--add-label "ai:merged")
  if has_label "${fix_labels}" "ai:closed"; then
    edit_args+=(--remove-label "ai:closed")
  fi

  _label_err_file="$(mktemp)"
  if gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" "${edit_args[@]}" >/dev/null 2>"${_label_err_file}"; then
    rm -f "${_label_err_file}"
    return 0
  fi

  echo "::warning::Validation fix-up label backfill failed for #${issue_num}: $(cat "${_label_err_file}" 2>/dev/null)" >&2
  rm -f "${_label_err_file}"
  return 1
}

add_healing_note() {
  local note="$1"
  [ -n "${note}" ] || return 0
  HEALING_NOTES+=("${note}")
}

post_healing_summary_comment() {
  if [ "${#HEALING_NOTES[@]}" -eq 0 ]; then
    return 0
  fi
  local unique_notes
  unique_notes="$(printf '%s\n' "${HEALING_NOTES[@]}" | awk 'NF && !seen[$0]++')"
  [ -n "${unique_notes}" ] || return 0
  post_tracking_comment "## 🔧 Poller auto-healing updates

$(printf '%s\n' "${unique_notes}" | sed 's/^/- /')"
}

# close_merged_issues_sweep — scans all OPEN GitHub issues that carry the
# ai:merged label and closes any whose linked PR is verified merged via the
# GitHub REST API. Runs for both orchestrator-managed child issues and
# non-orchestrator-managed standalone issues. Tracking issues
# (label ai:orchestrator-tracking) are intentionally skipped — their
# close lifecycle is handled by the orchestrator completion path.
#
# Verification policy (Q2: A — strict): walks the issue timeline for
# cross-referenced PRs and only closes if at least one fetched PR reports
# merged == true (or merged_at != null). If no merged PR can be verified
# (e.g. stale label, missing PR link, transient API failure), the issue is
# left open and a Telegram alert is sent for operator investigation — this
# sweep does NOT attempt any other recovery in that case.
#
# Gated by ENABLE_CLOSE_MERGED_ISSUES (default true).
close_merged_issues_sweep() {
  if [ "${ENABLE_CLOSE_MERGED_ISSUES}" != "true" ]; then
    echo "Close merged issues sweep disabled by ENABLE_CLOSE_MERGED_ISSUES=${ENABLE_CLOSE_MERGED_ISSUES}."
    return 0
  fi

  echo ""
  echo "========================================"
  echo "Close merged issues sweep"
  echo "========================================"

  local issues_json
  issues_json="$(gh_retry gh issue list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --label "ai:merged" \
    --json number,labels \
    --limit 200 2>/dev/null || echo "[]")"

  local count
  count="$(echo "${issues_json}" | jq 'length' 2>/dev/null || echo "0")"
  echo "Found ${count} open issue(s) with ai:merged label."

  if [ "${count}" -eq 0 ]; then
    return 0
  fi

  local idx issue_num has_tracking_label timeline_json pr_urls pr_url pr_json merged_pr_num
  local github_api_base="${GITHUB_API_URL:-https://api.github.com}"
  local pr_api_prefix="${github_api_base}/repos/${GITHUB_REPOSITORY}/pulls/"
  local closed_count=0
  local skipped_count=0
  local alert_count=0

  for ((idx=0; idx<count; idx++)); do
    issue_num="$(echo "${issues_json}" | jq -r ".[${idx}].number" 2>/dev/null || echo "")"
    [ -n "${issue_num}" ] && [ "${issue_num}" != "null" ] || continue

    # Skip orchestrator tracking issues — handled by the project completion
    # close path (see set_tracking_phase_label "ai:merged" call sites).
    has_tracking_label="$(echo "${issues_json}" | jq -r --argjson i "${idx}" '[.[$i].labels[]?.name] | index("ai:orchestrator-tracking") // empty' 2>/dev/null || echo "")"
    if [ -n "${has_tracking_label}" ]; then
      echo "  Issue #${issue_num}: ai:orchestrator-tracking — skipping (handled by completion path)."
      skipped_count=$((skipped_count + 1))
      continue
    fi

    # Walk the issue timeline for cross-referenced PR URLs. Reuses the
    # same pattern as validation_fix_issue_has_merged_pr_evidence().
    if ! timeline_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" 2>/dev/null | jq -s 'add // []' 2>/dev/null)"; then
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} timeline_fetch_failed — skipping this cycle."
      skipped_count=$((skipped_count + 1))
      continue
    fi

    if ! echo "${timeline_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} timeline_not_array — skipping this cycle."
      skipped_count=$((skipped_count + 1))
      continue
    fi

    pr_urls="$(echo "${timeline_json}" | jq -r '[.[] | select(.event == "cross-referenced" and (.source.issue.pull_request.url? | type == "string")) | .source.issue.pull_request.url] | unique | .[]?' 2>/dev/null || true)"

    merged_pr_num=""
    if [ -n "${pr_urls}" ]; then
      while IFS= read -r pr_url; do
        [ -n "${pr_url}" ] || continue
        if [[ "${pr_url}" != "${pr_api_prefix}"* ]]; then
          continue
        fi
        if ! pr_json="$(gh_retry gh api "${pr_url}" 2>/dev/null)"; then
          continue
        fi
        if ! echo "${pr_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
          continue
        fi
        if echo "${pr_json}" | jq -e '(.merged_at != null) or (.merged == true)' >/dev/null 2>&1; then
          merged_pr_num="$(echo "${pr_json}" | jq -r '.number // empty' 2>/dev/null || echo "")"
          break
        fi
      done <<< "${pr_urls}"
    fi

    if [ -z "${merged_pr_num}" ]; then
      # Policy: do not close, send Telegram alert for investigation.
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} no_merged_pr_found — leaving open and alerting."
      tg_notify_issue "${issue_num}" "⚠️ Orchestrator poller: issue #${issue_num} carries the \`ai:merged\` label but no linked merged PR could be verified on its timeline. The label may be stale or the PR link may be missing. Not auto-closing — please investigate." "WARNING" || true
      alert_count=$((alert_count + 1))
      continue
    fi

    echo "  Issue #${issue_num}: verified merged PR #${merged_pr_num}. Closing."
    local _close_err_file
    _close_err_file="$(mktemp)"
    if gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: linked PR #${merged_pr_num} was merged. Auto-closed by orchestrator poller (close_merged_issues_sweep)." \
        >/dev/null 2>"${_close_err_file}"; then
      closed_count=$((closed_count + 1))
      echo "CLOSE_MERGED_SWEEP issue=${issue_num} pr=${merged_pr_num} status=closed"
    else
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} close_failed: $(cat "${_close_err_file}" 2>/dev/null)" >&2
    fi
    rm -f "${_close_err_file}"
  done

  echo "Close merged issues sweep complete. Closed=${closed_count} Skipped=${skipped_count} Alerts=${alert_count}."
}

reconcile_managed_issue_labels() {
  local issue_num="$1"
  local labels_json="$2"
  local issue_state="$3"
  local pr_state="$4"
  local pr_merged="$5"
  local contract_file=".github/ai/label_contract.v1.json"

  if [ ! -f "${contract_file}" ]; then
    echo "${labels_json}"
    return 0
  fi

  local labels_csv
  labels_csv="$(echo "${labels_json}" | jq -r 'join(",")' 2>/dev/null || echo "")"
  local repair_json
  repair_json="$(python3 scripts/ai_labels.py repair-labels --contract-file "${contract_file}" --issue-labels "${labels_csv}" 2>/dev/null || echo '{"add":[],"remove":[]}')"

  local plan_json
  plan_json="$(python3 - "${labels_json}" "${repair_json}" "${issue_state}" "${pr_merged}" <<'PY'
import json
import sys

current = set(json.loads(sys.argv[1]))
repair = json.loads(sys.argv[2])
issue_state = (sys.argv[3] or "").strip().lower()
pr_merged = (sys.argv[4] or "").strip().lower() == "true"

final = set(current)
for label in repair.get("remove", []):
    final.discard(label)
for label in repair.get("add", []):
    final.add(label)

if pr_merged:
    final.discard("ai:closed")
    final.add("ai:merged")
elif issue_state == "closed" and "ai:closed" not in final and "ai:merged" not in final:
    final.add("ai:closed")

add = sorted(final - current)
remove = sorted(current - final)
print(json.dumps({"add": add, "remove": remove, "final": sorted(final)}))
PY
)"

  local add_count remove_count
  add_count="$(echo "${plan_json}" | jq '(.add // []) | length' 2>/dev/null || echo "0")"
  remove_count="$(echo "${plan_json}" | jq '(.remove // []) | length' 2>/dev/null || echo "0")"
  if [ "${add_count}" -eq 0 ] && [ "${remove_count}" -eq 0 ]; then
    echo "${labels_json}"
    return 0
  fi

  local edit_args=()
  while IFS= read -r remove_label; do
    [ -n "${remove_label}" ] || continue
    edit_args+=(--remove-label "${remove_label}")
  done < <(echo "${plan_json}" | jq -r '.remove[]?')

  while IFS= read -r add_label; do
    [ -n "${add_label}" ] || continue
    ensure_label_exists "${add_label}"
    edit_args+=(--add-label "${add_label}")
  done < <(echo "${plan_json}" | jq -r '.add[]?')

  local updated_labels_json="${labels_json}"
  local _label_err_file
  _label_err_file="$(mktemp)"
  if gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" "${edit_args[@]}" >/dev/null 2>"${_label_err_file}"; then
    updated_labels_json="$(echo "${plan_json}" | jq -c '.final // []' 2>/dev/null || echo "${labels_json}")"
    echo "LABEL_REPAIR issue=${issue_num} issue_state=${issue_state} pr_state=${pr_state:-none} pr_merged=${pr_merged}" >&2
    echo "LABEL_REPAIR_DIFF issue=${issue_num} before=$(echo "${labels_json}" | jq -c .) after=$(echo "${updated_labels_json}" | jq -c .) add=$(echo "${plan_json}" | jq -c '.add // []') remove=$(echo "${plan_json}" | jq -c '.remove // []')" >&2
    local _added_labels _removed_labels
    _added_labels="$(echo "${plan_json}" | jq -r '(.add // []) | join(",")')"
    _removed_labels="$(echo "${plan_json}" | jq -r '(.remove // []) | join(",")')"
    [ -n "${_added_labels}" ] || _added_labels="none"
    [ -n "${_removed_labels}" ] || _removed_labels="none"
    add_healing_note "Issue #${issue_num}: labels repaired (+${_added_labels} -${_removed_labels})"
  else
    echo "::warning::LABEL_REPAIR issue=${issue_num} failed: $(cat "${_label_err_file}" 2>/dev/null)" >&2
  fi
  rm -f "${_label_err_file}"
  echo "${updated_labels_json}"
}

integration_branch_exists() {
  local branch_name="$1"
  [ -n "${branch_name}" ] || return 1
  local branch_ref
  local gh_error
  branch_ref="$(printf '%s' "${branch_name}" | jq -sRr @uri)"

  if gh_error="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${branch_ref}" 2>&1 >/dev/null)"; then
    return 0
  fi

  if printf '%s' "${gh_error}" | grep -Eqi '(^gh: Not Found|HTTP 404|404 Not Found|status code 404|\bnot found\b)'; then
    return 1
  fi

  echo "::warning::Unable to verify integration branch '${branch_name}' due to GitHub API error; assuming it still exists." >&2
  return 0
}

resolve_active_orchestrator_context_for_issue() {
  local issue_num="$1"
  local preferred_tracking_num="${2:-}"
  local ordered_tracking_nums=""
  local tracking_count
  local idx
  local tracking_num
  local tracking_comments
  local tracking_comments_pages_file
  local tracking_comments_merged
  local tracking_state_json

  RESOLVED_ORCHESTRATOR_OWNED="false"
  RESOLVED_TRACKING_ISSUE=""
  RESOLVED_INTEGRATION_BRANCH=""
  RESOLVED_INTEGRATION_BRANCH_EXISTS="false"

  if ! [[ "${issue_num}" =~ ^[0-9]+$ ]]; then
    return 0
  fi

  if ! [ -f "${RUNTIME_DIR}/tracking_issues.json" ]; then
    return 0
  fi

  if [[ "${preferred_tracking_num}" =~ ^[0-9]+$ ]]; then
    ordered_tracking_nums+="${preferred_tracking_num}"$'\n'
  fi

  tracking_count="$(jq 'length' "${RUNTIME_DIR}/tracking_issues.json" 2>/dev/null || echo "0")"
  for ((idx=0; idx<tracking_count; idx++)); do
    tracking_num="$(jq -r ".[$idx].number" "${RUNTIME_DIR}/tracking_issues.json" 2>/dev/null || echo "")"
    [ -n "${tracking_num}" ] || continue
    ordered_tracking_nums+="${tracking_num}"$'\n'
  done

  while IFS= read -r tracking_num; do
    [ -n "${tracking_num}" ] || continue
    if ! [[ "${tracking_num}" =~ ^[0-9]+$ ]]; then
      continue
    fi

    tracking_comments='[]'
    tracking_comments_pages_file="${RUNTIME_DIR}/tracking_issue_${tracking_num}_comments_pages.json"
    rm -f "${tracking_comments_pages_file}"
    if gh_retry_to_file "${tracking_comments_pages_file}" gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${tracking_num}/comments?per_page=100"; then
      if tracking_comments_merged="$(jq -s 'add // []' "${tracking_comments_pages_file}" 2>/dev/null)" && \
        printf '%s' "${tracking_comments_merged}" | jq -e 'type == "array"' >/dev/null 2>&1; then
        tracking_comments="${tracking_comments_merged}"
      fi
    fi
    rm -f "${tracking_comments_pages_file}"

    tracking_state_json=""
    if ! extract_latest_valid_orchestrator_state "${tracking_comments}"; then
      continue
    fi
    tracking_state_json="${EXTRACTED_STATE_JSON}"
    [ -n "${tracking_state_json}" ] || continue

    if ! printf '%s' "${tracking_state_json}" | jq -e --arg issue "${issue_num}" '
      ([
        (.issue_number_map // {} | to_entries[]?.value | tostring),
        (.waves[]?.issues[]?.github_issue // empty | tostring)
      ] | index($issue)) != null
    ' >/dev/null 2>&1; then
      continue
    fi

    RESOLVED_ORCHESTRATOR_OWNED="true"
    RESOLVED_TRACKING_ISSUE="${tracking_num}"
    RESOLVED_INTEGRATION_BRANCH="$(printf '%s' "${tracking_state_json}" | jq -r '.integration_branch // empty' 2>/dev/null || echo "")"

    if [ -n "${RESOLVED_INTEGRATION_BRANCH}" ] && integration_branch_exists "${RESOLVED_INTEGRATION_BRANCH}"; then
      RESOLVED_INTEGRATION_BRANCH_EXISTS="true"
    fi
    return 0
  done < <(printf '%s\n' "${ordered_tracking_nums}" | grep -E '^[0-9]+$' | awk '!seen[$0]++')

  return 0
}

mark_integration_branch_missing_failed() {
  local integration_branch="$1"
  local reason="Integration branch '${integration_branch}' is missing. It may have been deleted externally. Manual intervention required."
  jq --arg reason "${reason}" --arg branch "${integration_branch}" \
    '.status = "failed" |
     .final_merge_status = "failed" |
     .integration_branch = $branch |
     .final_merge_error = $reason' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  post_tracking_comment "## ❌ Integration branch missing\n\n${reason}"
  tg_notify "❌ Project #${TRACKING_NUM} failed: missing integration branch '${integration_branch}'."
  tg_cleanup_msgs "${TRACKING_NUM}"
}

sync_rebuild_runbook_url() {
  local default_branch="$1"
  local fallback_branch="${default_branch:-main}"
  local runbook_path="docs/orchestrator-integration-branch-rebuild-runbook.md"
  local url

  if gh_retry gh api "repos/${GITHUB_REPOSITORY}/contents/${runbook_path}?ref=${fallback_branch}" >/dev/null 2>&1; then
    url="$(_gh_url "blob/${fallback_branch}/${runbook_path}")"
    if [ -n "${url}" ]; then
      printf '%s' "${url}"
      return 0
    fi
  fi

  url="$(_gh_url "blob/${fallback_branch}/${runbook_path}")"
  if [ -n "${url}" ]; then
    printf '%s' "${url}"
    return 0
  fi

  printf '%s/%s/blob/%s/%s' "${GITHUB_SERVER_URL:-https://github.com}" "${GITHUB_REPOSITORY}" "${fallback_branch}" "${runbook_path}"
}

resolve_branch_analysis_ref() {
  local branch_name="$1"
  [ -n "${branch_name}" ] || return 1

  if git rev-parse --verify -q "refs/remotes/origin/${branch_name}" >/dev/null 2>&1; then
    printf 'refs/remotes/origin/%s' "${branch_name}"
    return 0
  fi

  if git rev-parse --verify -q "refs/heads/${branch_name}" >/dev/null 2>&1; then
    printf 'refs/heads/%s' "${branch_name}"
    return 0
  fi

  if git fetch --no-tags origin "refs/heads/${branch_name}:refs/remotes/origin/${branch_name}" >/dev/null 2>&1 \
    && git rev-parse --verify -q "refs/remotes/origin/${branch_name}" >/dev/null 2>&1; then
    printf 'refs/remotes/origin/%s' "${branch_name}"
    return 0
  fi

  return 1
}

merge_tree_conflict_paths_json() {
  local default_ref="$1"
  local integration_ref="$2"
  local merge_output=""
  local merge_rc=0

  if merge_output="$(git merge-tree --write-tree --name-only "${default_ref}" "${integration_ref}" 2>/dev/null)"; then
    merge_rc=0
  else
    merge_rc=$?
  fi

  if [ "${merge_rc}" -ne 0 ] && [ -z "${merge_output}" ]; then
    return 1
  fi

  printf '%s\n' "${merge_output}" \
    | sed '1{/^[0-9a-f]\{40,\}$/d};/^$/d' \
    | jq -Rsc 'split("\n") | map(select(length > 0)) | unique'
}

merge_tree_conflict_fingerprint() {
  local conflict_paths_json="$1"
  printf '%s' "${conflict_paths_json}" | sha256sum | awk '{print $1}'
}

format_conflict_paths_markdown() {
  local conflict_paths_json="$1"
  local max_paths="${2:-20}"
  local listed
  local overflow

  listed="$(echo "${conflict_paths_json}" | jq -r --argjson max_paths "${max_paths}" '.[0:$max_paths] | map("- `" + . + "`") | join("\n")')"
  overflow="$(echo "${conflict_paths_json}" | jq -r --argjson max_paths "${max_paths}" 'if length > $max_paths then (length - $max_paths) else 0 end')"

  if [ -z "${listed}" ] || [ "${listed}" = "null" ]; then
    listed='- Unable to extract conflict paths from git merge-tree output.'
  fi
  if [ "${overflow}" -gt 0 ]; then
    listed+=$'\n'"- ...and ${overflow} more"
  fi

  printf '%s' "${listed}"
}

SYNC_SUPERSEDED_BY_MAIN="false"
SYNC_SUPERSEDED_REASON=""
SYNC_SUPERSEDED_AFFECTED_PATHS_JSON='[]'
SYNC_SUPERSEDED_CONFLICT_PATHS_JSON='[]'

evaluate_sync_superseded_by_main() {
  local integration_branch="$1"
  local default_branch="$2"
  local issue_numbers
  local issue_num
  local timeline_prs
  local pr_num
  local pr_json
  local pr_state
  local pr_merged
  local pr_files_json
  local path
  local default_ref
  local integration_ref

  SYNC_SUPERSEDED_BY_MAIN="false"
  SYNC_SUPERSEDED_REASON=""
  SYNC_SUPERSEDED_AFFECTED_PATHS_JSON='[]'
  SYNC_SUPERSEDED_CONFLICT_PATHS_JSON='[]'

  issue_numbers="$(jq -r '[.waves[]?.issues[]? | .github_issue // empty | tostring] | unique[]' "${STATE_FILE}" 2>/dev/null || true)"
  if [ -z "${issue_numbers}" ]; then
    return 0
  fi

  local -a pr_numbers=()
  local -a affected_paths=()
  local -A pr_seen=()
  local -A path_seen=()

  while IFS= read -r issue_num; do
    [ -n "${issue_num}" ] || continue
    timeline_prs="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | unique | .[]' \
      2>/dev/null || true)"
    while IFS= read -r pr_num; do
      [[ "${pr_num}" =~ ^[0-9]+$ ]] || continue
      if [ -z "${pr_seen["${pr_num}"]+x}" ]; then
        pr_seen["${pr_num}"]=1
        pr_numbers+=("${pr_num}")
      fi
    done <<< "${timeline_prs}"
  done <<< "${issue_numbers}"

  if [ "${#pr_numbers[@]}" -eq 0 ]; then
    return 0
  fi

  for pr_num in "${pr_numbers[@]}"; do
	pr_json="$(_fetch_pr_json "${pr_num}")"
	pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
	pr_merged="$(_jq_field "${pr_json}" '.merged_at != null' 'true|false')"
	if [ -z "${pr_state}" ] || [ -z "${pr_merged}" ]; then
	  echo "  [superseded-check] Skipping PR #${pr_num}: unable to fetch state." >&2
	  return 0
	fi
	if [ "${pr_state}" = "open" ] && [ "${pr_merged}" != "true" ]; then
	  return 0
	fi

    if ! pr_files_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}/files?per_page=100" 2>/dev/null \
      | jq -sc '[.[]? | .[]? | .filename] | unique' 2>/dev/null)"; then
      echo "  [superseded-check] Skipping PR #${pr_num}: unable to fetch changed files." >&2
      continue
    fi

    while IFS= read -r path; do
      [ -n "${path}" ] || continue
      if [ -z "${path_seen["${path}"]+x}" ]; then
        path_seen["${path}"]=1
        affected_paths+=("${path}")
      fi
    done < <(echo "${pr_files_json}" | jq -r '.[]?')
  done

  if [ "${#affected_paths[@]}" -eq 0 ]; then
    return 0
  fi

  SYNC_SUPERSEDED_AFFECTED_PATHS_JSON="$(printf '%s\n' "${affected_paths[@]}" | jq -Rsc 'split("\n") | map(select(length > 0)) | unique')"

  if ! default_ref="$(resolve_branch_analysis_ref "${default_branch}")"; then
    return 0
  fi
  if ! integration_ref="$(resolve_branch_analysis_ref "${integration_branch}")"; then
    return 0
  fi

  SYNC_SUPERSEDED_CONFLICT_PATHS_JSON="$(merge_tree_conflict_paths_json "${default_ref}" "${integration_ref}" 2>/dev/null || echo '[]')"

  if git diff --quiet "${default_ref}..${integration_ref}" -- "${affected_paths[@]}" 2>/dev/null; then
    SYNC_SUPERSEDED_BY_MAIN="true"
    SYNC_SUPERSEDED_REASON="All tracked child PRs are terminal and integration changes for child PR paths are already represented on ${default_branch}."
  fi
}

# ---------------------------------------------------------------
# Self-healing helpers for integration-branch <-> default-branch drift
# ---------------------------------------------------------------
# These helpers implement the circuit-breaker flow for issue #832-style
# stalls, where a periodic `main -> orchestrator/project-<n>` merge
# silently fails with HTTP 409 every poll tick and nothing resolves it.
#
# Flow:
#   1. ensure_integration_conflict_state_fields: guarantees new state
#      fields exist (additive, idempotent) so jq arithmetic is safe.
#   2. sync_default_into_integration_branch tries the plain merges API.
#   3. On 409, it calls heal_integration_branch_conflict, which:
#        a. Ensures the final integration->default PR exists (creating
#           it on-demand via ensure_eager_final_pr).
#        b. Honours CONFLICT_DISPATCH_COOLDOWN_SECS throttling.
#        c. Dispatches the existing review/autofix workflow against the
#           final PR via _dispatch_review_for_conflicts.
#        d. Bumps integration_conflict_unresolved_ticks / dispatch_count.
#   4. When unresolved ticks reach INTEGRATION_CONFLICT_MAX_RETRIES the
#      flow escalates to invoke_judge_for_integration_conflict, which
#      runs codex exec with a prompt crafted for final-merge resolution.
#
# All helpers are idempotent; repeated invocation converges to a
# mergeable integration branch or a terminal 'failed' state.

ensure_integration_conflict_state_fields() {
  [ -f "${STATE_FILE}" ] || return 0
  jq '. + {
        integration_sync_status: (.integration_sync_status // "clean"),
        integration_sync_last_error: (.integration_sync_last_error // ""),
        integration_conflict_dispatch_count: (.integration_conflict_dispatch_count // 0),
        integration_conflict_dispatch_ts: (.integration_conflict_dispatch_ts // 0),
        integration_conflict_unresolved_ticks: (.integration_conflict_unresolved_ticks // 0)
      }' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
}

# Create (or discover) the integration->default PR eagerly so that the
# existing conflict-resolution pipeline has a concrete PR to target.
# Prints the PR number on stdout, empty string on failure.
# Also updates final_merge_pr in state when a new PR is created.
ensure_eager_final_pr() {
  local integration_branch="$1"
  local default_branch="$2"
  local project_title="$3"

  [ -n "${integration_branch}" ] || { echo ""; return 1; }

  local existing_pr
  existing_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [ -n "${existing_pr}" ] && [ "${existing_pr}" != "null" ]; then
    local pr_state
    pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${existing_pr}" --jq '.state' 2>/dev/null || echo "")"
    if [ "${pr_state}" = "open" ]; then
      echo "${existing_pr}"
      return 0
    fi
    # PR closed/merged — fall through to rediscover or recreate.
  fi

  local discovered
  discovered="$(gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --base "${default_branch}" \
    --head "${integration_branch}" \
    --json number \
    --jq '.[0].number // empty' 2>/dev/null || true)"

  if [ -z "${discovered}" ]; then
    local pr_url
    pr_url="$(gh pr create \
      --repo "${GITHUB_REPOSITORY}" \
      --base "${default_branch}" \
      --head "${integration_branch}" \
      --title "feat: ${project_title}" \
      --body "Squash merge of orchestrator project #${TRACKING_NUM}.\n\nThis PR is created eagerly by the self-healing pipeline so that \`main\` <-> \`${integration_branch}\` drift can be resolved continuously rather than only at finalize time.\n\nRefs #${TRACKING_NUM}" 2>/dev/null || true)"
    discovered="$(printf '%s\n' "${pr_url}" | grep -oE '/pull/[0-9]+' | tail -n1 | cut -d/ -f3 || true)"
  fi

  [[ "${discovered}" =~ ^[0-9]+$ ]] || discovered=""
  if [ -n "${discovered}" ]; then
    jq --argjson final_pr "${discovered}" '.final_merge_pr = $final_pr' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    echo "${discovered}"
    return 0
  fi

  echo ""
  return 1
}

# Build a prompt for the judge and run codex exec to resolve a
# final-merge conflict that has survived INTEGRATION_CONFLICT_MAX_RETRIES
# automated dispatches. Mirrors the codex setup used by the
# review-blocked judge block (~L3700-3720) but is PR-scoped rather
# than issue-scoped.
#
# Usage: invoke_judge_for_integration_conflict <final_pr> <integration_branch> <default_branch>
# Returns: 0 on successful invocation (not necessarily successful resolution),
#          1 on setup/dispatch failure.
invoke_judge_for_integration_conflict() {
  local final_pr="$1"
  local integration_branch="$2"
  local default_branch="$3"

  [ -n "${final_pr}" ] || return 1

  echo "  [integration-heal] Escalating to judge for final PR #${final_pr} (${integration_branch} -> ${default_branch})."

  # Ensure codex config exists — mirrors the review-blocked judge setup.
  mkdir -p ~/.codex
  local catalog_path="$(pwd)/scripts/codex_model_catalog.json"
  {
    echo 'web_search = "live"'
    echo 'model_provider = "openrouter"'
    echo "model = \"${MODEL_EDITOR:-openai/gpt-5.3-codex}\""
    echo "model_reasoning_effort = \"${MODEL_REASONING_EFFORT_JUDGE:-high}\""
    if [ -f "${catalog_path}" ]; then
      echo "model_catalog_json = \"${catalog_path}\""
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

  # Setup Serena (best-effort, same pattern as existing judge blocks).
  bash scripts/setup_serena.sh --mode editing --context codex || true

  local prompt_file
  local output_file
  prompt_file="$(mktemp "${TMPDIR:-/tmp}/integration_judge_prompt.XXXXXX")"
  output_file="$(mktemp "${TMPDIR:-/tmp}/integration_judge_output.XXXXXX")"

  local pr_diff
  local pr_files
  pr_diff="$(gh pr diff "${final_pr}" --repo "${GITHUB_REPOSITORY}" 2>/dev/null | head -c 120000 || true)"
  pr_files="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}/files" --jq '[.[] | {filename, status, additions, deletions}]' 2>/dev/null || echo "[]")"
  local retries
  retries="$(jq -r '.integration_conflict_dispatch_count // 0' "${STATE_FILE}")"

  {
    echo "You are the orchestrator final-merge judge. The automated resolver"
    echo "pipeline has attempted to sync \`${default_branch}\` into"
    echo "\`${integration_branch}\` ${retries} times without producing a"
    echo "mergeable state. Final PR #${final_pr} (${integration_branch} -> ${default_branch})"
    echo "is currently unmergeable."
    echo
    echo "Your task: fetch both branches, resolve the merge conflicts in a"
    echo "way that preserves the intent of every sub-issue already merged"
    echo "into ${integration_branch}, push the resolution to"
    echo "${integration_branch}, and then verify GitHub reports the final"
    echo "PR as mergeable=true. Do NOT merge the PR yourself — the poller"
    echo "will do that once mergeability is restored."
    echo
    echo "Context:"
    echo "- Tracking issue: #${TRACKING_NUM}"
    echo "- Final PR number: ${final_pr}"
    echo "- Integration branch: ${integration_branch}"
    echo "- Default branch: ${default_branch}"
    echo "- Automated resolver attempts so far: ${retries}"
    echo
    echo "Changed files in final PR (JSON):"
    printf '%s\n' "${pr_files}"
    echo
    echo "Truncated PR diff:"
    echo '```diff'
    printf '%s\n' "${pr_diff}"
    echo '```'
    echo
    echo "Rules:"
    echo "1. Preserve all intent from merged sub-issues."
    echo "2. Do not rewrite history of ${default_branch}."
    echo "3. Prefer merge commits over rebase for the integration branch."
    echo "4. If conflicts are semantic rather than textual, surface a"
    echo "   short diagnosis in the commit message."
  } > "${prompt_file}"

  if cat "${prompt_file}" | codex exec --model "${MODEL_EDITOR:-openai/gpt-5.3-codex}" --full-auto > "${output_file}" 2>> "${RUNTIME_DIR}/integration_judge.log"; then
    echo "  [integration-heal] Judge exec completed for PR #${final_pr}."
    rm -f "${prompt_file}" "${output_file}"
    return 0
  fi

  echo "::warning::Judge exec failed for integration conflict on PR #${final_pr}."
  rm -f "${prompt_file}" "${output_file}"
  return 1
}

# Drive one iteration of the self-healing loop for the integration
# branch. Must be called when we know a conflict exists (either from
# a 409 in sync_default_into_integration_branch or from a
# mergeable=false in finalize_integration_merge_if_needed).
#
# Returns 0 if healing progressed (dispatch queued, cooldown active,
# or judge invoked), 1 if the circuit breaker has tripped and the
# state was marked failed.
heal_integration_branch_conflict() {
  local integration_branch="$1"
  local default_branch="$2"
  local project_title="$3"
  local error_msg="${4:-merge conflict}"

  ensure_integration_conflict_state_fields

  local final_pr
  final_pr="$(ensure_eager_final_pr "${integration_branch}" "${default_branch}" "${project_title}" || true)"
  if [ -z "${final_pr}" ]; then
    echo "::warning::heal_integration_branch_conflict could not obtain a final PR for ${integration_branch}." >&2
    jq --arg err "${error_msg}" \
      '.integration_sync_status = "conflict" | .integration_sync_last_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    return 0
  fi

  local now_ts
  now_ts="$(date -u +%s)"
  local last_ts
  last_ts="$(jq -r '.integration_conflict_dispatch_ts // 0' "${STATE_FILE}")"
  local dispatch_count
  dispatch_count="$(jq -r '.integration_conflict_dispatch_count // 0' "${STATE_FILE}")"
  local unresolved_ticks
  unresolved_ticks="$(jq -r '.integration_conflict_unresolved_ticks // 0' "${STATE_FILE}")"

  jq --arg err "${error_msg}" \
    '.integration_sync_status = "conflict" |
     .integration_sync_last_error = $err' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

  # Cooldown gate: don't re-dispatch resolver too frequently.
  local elapsed=$((now_ts - last_ts))
  if [ "${last_ts}" -gt 0 ] && [ "${elapsed}" -lt "${CONFLICT_DISPATCH_COOLDOWN_SECS}" ]; then
    echo "  [integration-heal] Dispatch cooldown active (${elapsed}s < ${CONFLICT_DISPATCH_COOLDOWN_SECS}s); deferring resolver dispatch for PR #${final_pr}."
    return 0
  fi

  # Circuit breaker: after MAX retries, escalate to judge instead of
  # dispatching one more resolver run.
  if [ "${unresolved_ticks}" -ge "${INTEGRATION_CONFLICT_MAX_RETRIES}" ]; then
    if invoke_judge_for_integration_conflict "${final_pr}" "${integration_branch}" "${default_branch}"; then
      # Reset unresolved ticks so the resolver loop can resume after
      # the judge's push. Keep dispatch_count as audit trail.
      jq '.integration_sync_status = "healing" | .integration_conflict_unresolved_ticks = 0 | .integration_sync_last_error = ""' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_tracking_comment "## 🛠️ Integration judge invoked\n\nFinal PR #${final_pr} (\`${integration_branch}\` -> \`${default_branch}\`) did not become mergeable after ${INTEGRATION_CONFLICT_MAX_RETRIES} automated resolver attempts. The judge has been invoked with full PR context to resolve conflicts. The poller will retry merge on the next tick."
      return 0
    fi
    jq --arg err "judge escalation failed: ${error_msg}" \
      '.status = "failed" |
       .final_merge_status = "failed" |
       .integration_sync_status = "failed" |
       .integration_sync_last_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## ❌ Integration self-healing exhausted\n\nFinal PR #${final_pr} (\`${integration_branch}\` -> \`${default_branch}\`) could not be made mergeable after ${INTEGRATION_CONFLICT_MAX_RETRIES} automated attempts AND a judge escalation that itself failed. Manual intervention required."
    tg_notify "❌ Integration self-healing exhausted for #${TRACKING_NUM} (PR #${final_pr}). Manual intervention required."
    return 1
  fi

  # Dispatch the existing review/autofix workflow against the final PR.
  local dispatch_rc=0
  _dispatch_review_for_conflicts "${final_pr}" "${integration_branch}" || dispatch_rc=$?

  if [ "${dispatch_rc}" -eq 2 ]; then
    jq --argjson ts "${now_ts}" \
      '.integration_sync_status = "healing" |
       .integration_conflict_dispatch_ts = $ts' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    echo "  [integration-heal] Resolver already in flight for PR #${final_pr}; skipping dispatch this tick."
    return 0
  fi

  unresolved_ticks=$((unresolved_ticks + 1))
  jq --argjson ticks "${unresolved_ticks}" \
    '.integration_conflict_unresolved_ticks = $ticks' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

  case "${dispatch_rc}" in
    0)
      dispatch_count=$((dispatch_count + 1))
      jq --argjson count "${dispatch_count}" --argjson ts "${now_ts}" \
        '.integration_sync_status = "healing" |
         .integration_conflict_dispatch_count = $count |
         .integration_conflict_dispatch_ts = $ts' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      # Only post a user-facing comment on the FIRST dispatch of this
      # conflict episode to avoid the every-tick spam pattern seen on
      # #832. Subsequent dispatches log to the state comment instead.
      if [ "${unresolved_ticks}" -eq 1 ]; then
        post_tracking_comment "## 🔧 Integration self-healing started\n\nDetected a real merge conflict while syncing \`${default_branch}\` into \`${integration_branch}\`. Dispatched the review/autofix workflow against final PR #${final_pr} for automated resolution. Will retry up to ${INTEGRATION_CONFLICT_MAX_RETRIES} times before escalating to the judge."
      fi
      tg_notify "🔧 Integration conflict on #${TRACKING_NUM}: dispatched resolver for PR #${final_pr} (attempt ${dispatch_count}, unresolved_ticks=${unresolved_ticks})." "WARNING"
      ;;
    *)
      echo "::warning::[integration-heal] Could not dispatch review workflow for PR #${final_pr}."
      ;;
  esac

  return 0
}

# Called after a successful sync to clear conflict state. Idempotent.
# Usage: mark_integration_sync_clean <default_branch>
mark_integration_sync_clean() {
  local default_branch="${1:-main}"
  ensure_integration_conflict_state_fields
  local prev_status
  prev_status="$(jq -r '.integration_sync_status // "clean"' "${STATE_FILE}")"
  if [ "${prev_status}" != "clean" ]; then
    jq '.integration_sync_status = "clean" |
        .integration_sync_last_error = "" |
        .integration_conflict_unresolved_ticks = 0' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_tracking_comment "## ✅ Integration self-healing resolved\n\n\`${default_branch}\` now merges cleanly into the integration branch. Final merge will proceed on the next poll tick."
  fi
}

sync_default_into_integration_branch() {
  local integration_branch="$1"
  local default_branch="$2"
  local sync_status
  local prev_conflict_fingerprint
  local runbook_url
  local superseded_notified

  if [ -z "${integration_branch}" ]; then
    return 0
  fi

  sync_status="$(jq -r '.sync.status // "active"' "${STATE_FILE}")"
  prev_conflict_fingerprint="$(jq -r '.sync.last_conflict_fingerprint // ""' "${STATE_FILE}")"
  superseded_notified="$(jq -r '.sync.superseded_notified // false' "${STATE_FILE}")"

  if [ "${sync_status}" = "superseded-by-main" ]; then
    if [ "${superseded_notified}" != "true" ]; then
      runbook_url="$(sync_rebuild_runbook_url "${default_branch}")"
      jq --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '.sync = ((.sync // {}) + {
          "status": "superseded-by-main",
          "superseded_notified": true,
          "last_sync_outcome": "superseded-skip",
          "superseded_at": ((.sync.superseded_at // empty) // $now)
        })' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      post_tracking_comment "## ✅ Integration branch superseded by ${default_branch}\n\nThe integration branch \`${integration_branch}\` is marked as **\`superseded-by-main\` relative to \`${default_branch}\`**. Sync is intentionally skipped in future poll cycles to avoid repeated conflict churn.\n\nRunbook (if you need to rebuild the integration branch): [Rebuild integration branch](${runbook_url})"
    fi
    return 0
  fi

  if ! integration_branch_exists "${integration_branch}"; then
    mark_integration_branch_missing_failed "${integration_branch}"
    return 1
  fi

  evaluate_sync_superseded_by_main "${integration_branch}" "${default_branch}"
  if [ "${SYNC_SUPERSEDED_BY_MAIN}" = "true" ]; then
    runbook_url="$(sync_rebuild_runbook_url "${default_branch}")"
    jq --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg reason "${SYNC_SUPERSEDED_REASON}" \
      --argjson affected_paths "${SYNC_SUPERSEDED_AFFECTED_PATHS_JSON}" \
      --argjson conflict_paths "${SYNC_SUPERSEDED_CONFLICT_PATHS_JSON}" \
      '.sync = ((.sync // {}) + {
        "status": "superseded-by-main",
        "superseded_at": $now,
        "superseded_reason": $reason,
        "superseded_notified": true,
        "last_sync_outcome": "superseded-skip",
        "last_conflict_paths": $conflict_paths,
        "last_conflict_fingerprint": "",
        "affected_paths": $affected_paths
      })' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## ✅ Integration branch superseded by ${default_branch}\n\nSkipping sync of \`${default_branch}\` into \`${integration_branch}\` because all tracked child PRs are terminal and the branch is now treated as superseded by \`${default_branch}\`.\n\nReason: ${SYNC_SUPERSEDED_REASON}\n\nRunbook (if you need to rebuild the integration branch): [Rebuild integration branch](${runbook_url})"
    return 0
  fi

  ensure_integration_conflict_state_fields

  local merge_error
  if merge_error="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/merges" \
    -f base="${integration_branch}" \
    -f head="${default_branch}" \
    -f commit_message="chore: sync ${default_branch} into ${integration_branch}" 2>&1 >/dev/null)"; then
    if [ "${sync_status}" != "active" ] || [ -n "${prev_conflict_fingerprint}" ]; then
      jq '.sync = ((.sync // {}) + {
        "status": "active",
        "last_sync_outcome": "merged",
        "last_conflict_paths": [],
        "last_conflict_fingerprint": ""
      })' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
    fi
    mark_integration_sync_clean "${default_branch}"
    return 0
  fi

  if ! printf '%s' "${merge_error}" | grep -Eqi '(HTTP 409|status code 409|merge conflict|conflict)'; then
    echo "::warning::Unable to sync '${default_branch}' into '${integration_branch}' due to transient GitHub API error; will retry next poll." >&2
    jq --arg err "${merge_error}" '.integration_sync_last_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    return 0
  fi

  local default_ref=""
  local integration_ref=""
  local conflict_paths_json='[]'
  local conflict_fingerprint
  local conflict_paths_md

  if default_ref="$(resolve_branch_analysis_ref "${default_branch}")" \
    && integration_ref="$(resolve_branch_analysis_ref "${integration_branch}")"; then
    conflict_paths_json="$(merge_tree_conflict_paths_json "${default_ref}" "${integration_ref}" 2>/dev/null || echo '[]')"
  fi
  conflict_fingerprint="$(merge_tree_conflict_fingerprint "${conflict_paths_json}")"

  jq --arg fp "${conflict_fingerprint}" --argjson paths "${conflict_paths_json}" \
    '.sync = ((.sync // {}) + {
      "status": "conflict",
      "last_sync_outcome": "conflict",
      "last_conflict_paths": $paths,
      "last_conflict_fingerprint": $fp
    })' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

  if [ "${sync_status}" != "conflict" ] || [ "${prev_conflict_fingerprint}" != "${conflict_fingerprint}" ]; then
    post_state_comment
    runbook_url="$(sync_rebuild_runbook_url "${default_branch}")"
    conflict_paths_md="$(format_conflict_paths_markdown "${conflict_paths_json}")"
    post_tracking_comment "## ⚠️ Integration sync conflict\n\nUnable to sync \`${default_branch}\` into \`${integration_branch}\` due to merge conflicts. The project can continue, but final merge may require manual conflict resolution.\n\nConflicting paths:\n${conflict_paths_md}\n\nRunbook: [Rebuild integration branch](${runbook_url})"
    tg_notify "⚠️ Sync conflict for #${TRACKING_NUM}: could not merge '${default_branch}' into '${integration_branch}'."
  fi
  # Real conflict: trigger the self-healing loop. project_title is
  # read from state so finalize's signature-compatible call-sites can
  # keep invoking this function with just (branch, default).
  local project_title
  project_title="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"
  if ! heal_integration_branch_conflict "${integration_branch}" "${default_branch}" "${project_title}" "${merge_error}"; then
    return 1
  fi
  return 0
}

finalize_integration_merge_if_needed() {
  local integration_branch="$1"
  local default_branch="$2"
  local project_title="$3"
  local final_pr

  if [ -z "${integration_branch}" ]; then
    return 0
  fi

  local final_merge_status
  final_merge_status="$(jq -r '.final_merge_status // "pending"' "${STATE_FILE}")"
  if [ "${final_merge_status}" = "merged" ] || [ "${final_merge_status}" = "superseded-by-main" ]; then
    return 0
  fi

  local sync_status
  sync_status="$(jq -r '.sync.status // "active"' "${STATE_FILE}")"
  if [ "${sync_status}" = "superseded-by-main" ]; then
    jq --arg reason "$(jq -r --arg default_branch "${default_branch}" '.sync.superseded_reason // ("Integration branch superseded by " + $default_branch + "; final merge intentionally skipped.")' "${STATE_FILE}")" \
      '.final_merge_status = "superseded-by-main" | .final_merge_error = $reason' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    return 0
  fi

  final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}")"
  if [ -n "${final_pr}" ] && [ "${final_pr}" != "null" ]; then
    local existing_pr_state
    local existing_pr_merged
    existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    if [ "${existing_pr_state}" = "closed" ] && [ "${existing_pr_merged}" = "true" ]; then
      jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      return 0
    fi
  fi

  if ! integration_branch_exists "${integration_branch}"; then
    mark_integration_branch_missing_failed "${integration_branch}"
    return 1
  fi

  if [ -z "${final_pr}" ] || [ "${final_pr}" = "null" ]; then
    final_pr="$(gh pr list \
      --repo "${GITHUB_REPOSITORY}" \
      --state open \
      --base "${default_branch}" \
      --head "${integration_branch}" \
      --json number \
      --jq '.[0].number // empty' 2>/dev/null || true)"
  fi

  if [ -z "${final_pr}" ]; then
    local final_pr_url
    final_pr_url="$(gh pr create \
      --repo "${GITHUB_REPOSITORY}" \
      --base "${default_branch}" \
      --head "${integration_branch}" \
      --title "feat: ${project_title}" \
      --body "Squash merge of orchestrator project #${TRACKING_NUM}.\n\nRefs #${TRACKING_NUM}" 2>/dev/null || true)"
    final_pr="$(printf '%s\n' "${final_pr_url}" | grep -oE '/pull/[0-9]+' | tail -n1 | cut -d/ -f3 || true)"
  fi

  if [ -z "${final_pr}" ]; then
    post_tracking_comment "## ⚠️ Final merge could not start\n\nUnable to create or locate the final integration PR from \`${integration_branch}\` to \`${default_branch}\`."
    return 1
  fi

  jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "pending"' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment

  local pr_state
  local pr_mergeable
  local pr_merged
  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"

  if [ "${pr_state}" = "closed" ] && [ "${pr_merged}" = "true" ]; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    return 0
  fi

  # Mergeability gate: if the final PR is not mergeable, hand off to
  # the self-healing flow and defer finalize to the next tick. This is
  # the primary fix for the #832-style stall: previously this code path
  # set final_merge_status=conflict and halted with no recovery.
  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" = "false" ]; then
    echo "  [final-merge] PR #${final_pr} is not mergeable; invoking self-healing flow."
    heal_integration_branch_conflict "${integration_branch}" "${default_branch}" "${project_title}" "final PR #${final_pr} mergeable=false" || true
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" != "true" ]; then
    echo "  [final-merge] PR #${final_pr} mergeability is '${pr_mergeable:-unknown}'. Will retry next poll."
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" = "true" ] && ! _pr_checks_completed "${final_pr}"; then
    echo "  [final-merge] Required checks not complete for PR #${final_pr}. Will retry next poll."
    return 1
  fi

  if gh pr merge "${final_pr}" --repo "${GITHUB_REPOSITORY}" --squash --delete-branch >/dev/null 2>&1; then
    jq --argjson final_pr "${final_pr}" \
      '.final_merge_pr = $final_pr |
       .final_merge_status = "merged" |
       .integration_sync_status = "clean" |
       .integration_sync_last_error = "" |
       .integration_conflict_unresolved_ticks = 0' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## ✅ Final merge complete\n\nIntegration branch \`${integration_branch}\` was squash-merged into \`${default_branch}\` via PR #${final_pr}."
    return 0
  fi

  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"

  if [ "${pr_state}" = "closed" ] && [ "${pr_merged}" = "true" ]; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    return 0
  fi

  # Post-merge-attempt conflict path: squash merge was rejected by
  # GitHub despite our pre-merge mergeability check (race with a push
  # to default). Hand off to the healing flow instead of halting.
  if [ "${pr_mergeable}" = "false" ]; then
    echo "  [final-merge] Post-attempt mergeability=false on PR #${final_pr}; invoking self-healing flow."
    heal_integration_branch_conflict "${integration_branch}" "${default_branch}" "${project_title}" "final PR #${final_pr} became unmergeable during merge" || true
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" != "true" ]; then
    echo "  [final-merge] PR #${final_pr} mergeability is '${pr_mergeable:-unknown}' after merge attempt. Will retry next poll."
    return 1
  fi

  post_tracking_comment "## ⚠️ Final merge blocked\n\nFinal PR #${final_pr} could not be merged automatically. Review branch protections/checks and merge manually if needed."
  return 1
}

dispatch_validation_workflow() {
  local validation_cycle="$1"
  local validation_ref="${2:-}"
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  echo "Dispatching ${wf_name} for tracking #${TRACKING_NUM} (cycle ${validation_cycle})"
  local run_args=("${wf_name}" "--repo" "${GITHUB_REPOSITORY}")
  if [ -n "${validation_ref}" ]; then
    run_args+=("--ref" "${validation_ref}")
  fi
  run_args+=("-f" "tracking_issue=${TRACKING_NUM}")

  local _dispatch_err
  _dispatch_err="$(mktemp)"
  if gh_retry gh workflow run "${run_args[@]}" >/dev/null 2>"${_dispatch_err}"; then
    rm -f "${_dispatch_err}"
    return 0
  fi
  local _primary_err
  _primary_err="$(cat "${_dispatch_err}" 2>/dev/null || true)"
  rm -f "${_dispatch_err}"

  # Fallback: try internal-validate.yml (coding-workflows repo convention)
  if [ "${wf_name}" != "internal-validate.yml" ]; then
    echo "Primary dispatch failed (${_primary_err:-unknown error}); trying internal-validate.yml fallback"
    run_args=("internal-validate.yml" "--repo" "${GITHUB_REPOSITORY}")
    if [ -n "${validation_ref}" ]; then
      run_args+=("--ref" "${validation_ref}")
    fi
    run_args+=("-f" "tracking_issue=${TRACKING_NUM}")
    local _fallback_err
    _fallback_err="$(mktemp)"
    if gh_retry gh workflow run "${run_args[@]}" >/dev/null 2>"${_fallback_err}"; then
      rm -f "${_fallback_err}"
      return 0
    fi
    local _fallback_err_msg
    _fallback_err_msg="$(cat "${_fallback_err}" 2>/dev/null || true)"
    rm -f "${_fallback_err}"
    echo "Fallback dispatch also failed (${_fallback_err_msg:-unknown error})"
    # Export combined error for caller to include in failure reason
    VALIDATION_DISPATCH_ERROR="primary (${wf_name}): ${_primary_err:-unknown}; fallback (internal-validate.yml): ${_fallback_err_msg:-unknown}"
  else
    echo "Dispatch of ${wf_name} failed (${_primary_err:-unknown error})"
    VALIDATION_DISPATCH_ERROR="${wf_name}: ${_primary_err:-unknown}"
  fi
  return 1
}

# Check whether the validation workflow (ai-validate.yml or internal-validate.yml)
# has any currently active (in_progress or queued) runs.  Used to avoid
# redispatching when a previous dispatch is still executing.
has_active_validation_run() {
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  local active_count

  active_count="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${wf_name}/runs?per_page=5" \
    --jq '[.workflow_runs[] | select(.status == "in_progress" or .status == "queued")] | length' 2>/dev/null || echo '0')"
  if [ "${active_count}" -gt 0 ]; then
    return 0
  fi

  # Fallback: check internal-validate.yml if primary name differs
  if [ "${wf_name}" != "internal-validate.yml" ]; then
    active_count="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/internal-validate.yml/runs?per_page=5" \
      --jq '[.workflow_runs[] | select(.status == "in_progress" or .status == "queued")] | length' 2>/dev/null || echo '0')"
    if [ "${active_count}" -gt 0 ]; then
      return 0
    fi
  fi

  return 1
}

# Return the conclusion of the most recent *completed* validation workflow run
# that was created on or after the last dispatch timestamp recorded in state.
# Used as a fallback when the ai:validated / ai:validation-failed label is
# missing despite the workflow having completed successfully.
get_last_validation_run_conclusion() {
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  local last_dispatch_ts
  last_dispatch_ts="$(jq -r '.validation_last_dispatch_ts // 0' "${STATE_FILE}")"

  local runs_json
  runs_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${wf_name}/runs?status=completed&per_page=5" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"

  # Fallback to internal-validate.yml if no completed runs found
  if [ "$(echo "${runs_json}" | jq 'length')" -eq 0 ] && [ "${wf_name}" != "internal-validate.yml" ]; then
    runs_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/internal-validate.yml/runs?status=completed&per_page=5" \
      --jq '.workflow_runs' 2>/dev/null || echo '[]')"
  fi

  # Select the most recent run created after our last dispatch timestamp
  local conclusion
  conclusion="$(echo "${runs_json}" | jq -r --argjson ts "${last_dispatch_ts}" '
    [.[] | select((.created_at | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) >= $ts)] |
    sort_by(.created_at) | last | .conclusion // ""
  ' 2>/dev/null || echo '')"

  echo "${conclusion}"
}

dispatch_validation_if_needed() {
  local validation_cycle="$1"
  local integration_branch
  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  local last_dispatch_cycle
  local last_dispatch_ts
  local now_epoch
  local stale_threshold_secs=3600  # 1 hour: if no label appears after dispatch, allow redispatch

  last_dispatch_cycle="$(jq -r '.validation_last_dispatch_cycle // 0' "${STATE_FILE}")"
  if [ "${last_dispatch_cycle}" = "${validation_cycle}" ]; then
    # Check for staleness: if dispatched but no label change for >1h, allow redispatch
    last_dispatch_ts="$(jq -r '.validation_last_dispatch_ts // 0' "${STATE_FILE}")"
    now_epoch="$(date +%s)"
    if [ "${last_dispatch_ts}" -gt 0 ] 2>/dev/null && [ $(( now_epoch - last_dispatch_ts )) -lt "${stale_threshold_secs}" ]; then
      echo "Validation workflow already dispatched for cycle ${validation_cycle} ($(( now_epoch - last_dispatch_ts ))s ago, threshold ${stale_threshold_secs}s)."
      return 0
    fi
    echo "Validation workflow for cycle ${validation_cycle} appears stale (dispatched >$(( stale_threshold_secs / 60 ))m ago with no label)."

    # Before redispatching, verify the workflow is not still running.
    # This prevents spurious redispatches when validation takes longer
    # than the stale threshold.
    if has_active_validation_run; then
      echo "Validation workflow still has active runs despite stale threshold. Skipping redispatch."
      return 0
    fi

    echo "No active validation runs found. Redispatching..."
  fi

  if dispatch_validation_workflow "${validation_cycle}" "${integration_branch}"; then
    jq --argjson cycle "${validation_cycle}" --argjson ts "$(date +%s)" \
      '.validation_last_dispatch_cycle = $cycle | .validation_last_dispatch_ts = $ts' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    if [ -n "${integration_branch}" ]; then
      post_tracking_comment "## 🧪 Runtime validation dispatched\n\n- Cycle: ${validation_cycle}\n- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`\n- Ref: \`${integration_branch}\`"
    else
      post_tracking_comment "## 🧪 Runtime validation dispatched\n\n- Cycle: ${validation_cycle}\n- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`"
    fi
    tg_notify "🧪 Validation dispatched for project #${TRACKING_NUM} (cycle ${validation_cycle})." "DEBUG"
    return 0
  fi

  return 1
}

mark_validation_failed() {
  local reason="$1"

  # Check validation recovery budget before going terminal
  local val_recovery_count
  val_recovery_count="$(jq -r '.validation_recovery_count // 0' "${STATE_FILE}")"
  if ! [[ "${val_recovery_count}" =~ ^[0-9]+$ ]]; then
    val_recovery_count="0"
  fi

  if [ "${val_recovery_count}" -lt "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" ]; then
    echo "Validation failed but recovery budget remains ($((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}). Transitioning back to judge."
    jq --arg reason "${reason}" --argjson count "$((val_recovery_count + 1))" \
      '.status = "in_progress" |
       .validation_recovery_count = $count |
       .validation_failure_reason = $reason |
       .validation_active_fix_issues = [] |
       .validation_cycle = 1 |
       .validation_last_dispatch_cycle = 0 |
       .validation_completed_cycle = null' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    set_tracking_phase_label "ai:validation-recovery"
    post_tracking_comment "## 🔄 Validation failed — recovery attempt $((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}\n\n${reason}\n\nTransitioning back to judge for re-evaluation."
    tg_notify "Validation recovery ($((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}) for #${TRACKING_NUM}: transitioning back to judge." "WARNING"
    return 0
  fi

  # Recovery budget exhausted — terminal failure
  jq --arg reason "${reason}" '.status = "failed" | .validation_failure_reason = $reason | .validation_active_fix_issues = []' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  set_tracking_phase_label "ai:validation-failed"
  post_tracking_comment "## ❌ Runtime validation failed\n\n${reason}\n\nValidation recovery exhausted (${val_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS}). Manual intervention required."
  tg_notify "Project #${TRACKING_NUM} validation failed after ${val_recovery_count} recovery attempt(s). Manual intervention required." "CRITICAL"
  tg_cleanup_msgs "${TRACKING_NUM}"
}

mark_validation_complete() {
  local validation_cycle="$1"
  local integration_branch
  local default_branch
  local project_title

  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  default_branch="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  project_title="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

  if ! finalize_integration_merge_if_needed "${integration_branch}" "${default_branch}" "${project_title}"; then
    return 0
  fi

  jq --argjson cycle "${validation_cycle}" '.status = "complete" | .validation_completed_cycle = $cycle' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  set_tracking_phase_label "ai:validated"
  post_tracking_comment "Project completed successfully after runtime validation passed (cycle ${validation_cycle}). Issue kept open for manual review."
  tg_cleanup_msgs "${TRACKING_NUM}"
  MSG="Project #${TRACKING_NUM} completed after validation pass (cycle ${validation_cycle})."
  MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
  fi
  tg_send_msg "${MSG}" "DEBUG" >/dev/null
}

extract_fix_issues_from_comment() {
  local comment_body="$1"
  # Normalise literal \n sequences (produced by post_tracking_comment) into
  # real newlines so the line-anchored sed below can match "- #<num>" items.
  echo "${comment_body}" | sed 's/\\n/\n/g' | sed -n 's/^- #\([0-9][0-9]*\).*$/\1/p' | awk '!seen[$0]++'
}

sync_validation_fix_issues_from_comments() {
  local comments_json="$1"
  local latest_fix_comment_json
  local fix_comment_id
  local fix_comment_body
  local last_fix_comment_id
  local new_fix_issues_json
  local new_fix_count

  latest_fix_comment_json="$(echo "${comments_json}" | jq -c '[.[] | select(.body | startswith("## 🧪 Runtime validation found fixable issues"))] | last // empty')"
  if [ -z "${latest_fix_comment_json}" ]; then
    return 0
  fi

  fix_comment_id="$(echo "${latest_fix_comment_json}" | jq -r '.id // 0')"
  if ! [[ "${fix_comment_id}" =~ ^[0-9]+$ ]]; then
    return 0
  fi

  last_fix_comment_id="$(jq -r '.validation_last_fix_comment_id // 0' "${STATE_FILE}")"
  if ! [[ "${last_fix_comment_id}" =~ ^[0-9]+$ ]]; then
    last_fix_comment_id="0"
  fi

  if [ "${fix_comment_id}" -le "${last_fix_comment_id}" ]; then
    return 0
  fi

  fix_comment_body="$(echo "${latest_fix_comment_json}" | jq -r '.body // ""')"
  new_fix_issues_json="$(extract_fix_issues_from_comment "${fix_comment_body}" | jq -R 'select(length > 0) | tonumber' | jq -s '.')"
  new_fix_count="$(echo "${new_fix_issues_json}" | jq 'length')"

  if [ "${new_fix_count}" -gt 0 ]; then
    jq --argjson comment_id "${fix_comment_id}" --argjson active_fix_issues "${new_fix_issues_json}" \
      '.status = "validation-fixing" |
       .validation_last_fix_comment_id = $comment_id |
       .validation_active_fix_issues = $active_fix_issues |
       .validation_seen_fix_issues = ((.validation_seen_fix_issues // []) + $active_fix_issues | unique)' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  else
    echo "::warning::Validation fix comment ${fix_comment_id} did not include extractable issue numbers; treating as validation failure."
    mark_validation_failed "Validation workflow produced a fixable-issues comment with no extractable issue numbers (comment ${fix_comment_id})."
    return 0
  fi

  set_tracking_phase_label "ai:validation-fixing"
  post_state_comment
}

# ---------------------------------------------------------------
# Stall recovery: workflow run status checks
# ---------------------------------------------------------------

# Build a set of issue numbers that have *genuinely active* (in_progress or
# queued) workflow runs — i.e., runs that started recently enough that they
# could still be making progress.
#
# Runs that have been in_progress for longer than STALL_THRESHOLD_MINUTES
# are treated as zombie/hung runs and excluded. This prevents a stuck
# Actions runner from blocking stall recovery indefinitely.
#
# Outputs a newline-separated list of issue numbers.
build_active_issue_set() {
  local now_epoch
  now_epoch="$(date +%s)"
  local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))

  # Fetch in_progress + queued runs (recent, max 50)
  local runs_json
  runs_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=in_progress&per_page=50" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"
  local queued_json
  queued_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=queued&per_page=50" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"

  # Merge both lists
  local all_runs
  all_runs="$(echo "${runs_json}" "${queued_json}" | jq -s 'add // []' 2>/dev/null || echo '[]')"

  # Filter out zombie runs: any run that has been active for longer than
  # the stall threshold is considered hung and should not block recovery.
  # Uses run_started_at (actual execution start) with created_at as fallback.
  local fresh_runs
  fresh_runs="$(echo "${all_runs}" | jq --argjson now "${now_epoch}" --argjson threshold "${stall_secs}" '
    [.[] |
     # Parse the start timestamp (ISO 8601 → epoch)
     (.run_started_at // .created_at // "1970-01-01T00:00:00Z") as $ts |
     ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) as $start_epoch |
     select(($now - $start_epoch) < $threshold)
    ]
  ' 2>/dev/null || echo '[]')"

  local fresh_count
  fresh_count="$(echo "${fresh_runs}" | jq 'length')"
  local total_count
  total_count="$(echo "${all_runs}" | jq 'length')"
  if [ "${total_count}" -gt "${fresh_count}" ]; then
    echo "  Active runs: ${total_count} total, ${fresh_count} fresh ($(( total_count - fresh_count )) zombie runs excluded as >$(( stall_secs / 60 ))m old)." >&2
  fi

  # Extract issue numbers from fresh runs via head_branch patterns.
  # Implement branches typically follow patterns like "ai/issue-42",
  # "ai/42-feature-name", or "ai-implement-42".
  local issue_nums
  issue_nums="$(echo "${fresh_runs}" | jq -r '
    [.[] |
     .head_branch // "" |
     select(test("(?:^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)[0-9]+(?:$|[^0-9])")) |
     capture("(?:^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)(?<num>[0-9]+)(?:$|[^0-9])") | .num
    ] | unique | .[]
  ' 2>/dev/null || true)"

  # Fallback extraction for AI-prefixed branches (single issue number only)
  local branch_nums
  branch_nums="$(echo "${fresh_runs}" | jq -r '
    [.[] | .head_branch // "" |
     select(test("(^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)[0-9]+(?:$|[^0-9])")) |
     capture("(^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)(?<num>[0-9]+)(?:$|[^0-9])") | .num
    ] | unique | .[]
  ' 2>/dev/null || true)"

  # Combine and deduplicate
  printf '%s\n%s\n' "${issue_nums}" "${branch_nums}" | sort -u | grep -E '^[0-9]+$' || true
}

# Check if a specific issue has an active (fresh) workflow run.
# Uses the pre-built active issue set (ACTIVE_WORKFLOW_ISSUES).
issue_has_active_workflow() {
  local issue_num="$1"
  echo "${ACTIVE_WORKFLOW_ISSUES}" | grep -qxF "${issue_num}"
}

# Cancel zombie workflow runs for a specific issue.
# A zombie is a run that has been in_progress for longer than the stall
# threshold. Cancelling prevents resource waste and avoids conflicts with
# the recovery action (e.g., two implement runs on the same branch).
cancel_zombie_runs_for_issue() {
  local issue_num="$1"
  local now_epoch
  now_epoch="$(date +%s)"
  local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))

  # Re-fetch in_progress runs and find zombies matching this issue
  local runs_json
  runs_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=in_progress&per_page=50" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"

  local zombie_run_ids
  zombie_run_ids="$(echo "${runs_json}" | jq -r --argjson now "${now_epoch}" --argjson threshold "${stall_secs}" --arg issue "${issue_num}" '
    [.[] |
     (.run_started_at // .created_at // "1970-01-01T00:00:00Z") as $ts |
     ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) as $start_epoch |
     select(($now - $start_epoch) >= $threshold) |
     select(.head_branch // "" | test("(^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)" + $issue + "(?:[^0-9]|$)")) |
     .id
    ] | .[]
  ' 2>/dev/null || true)"

  if [ -n "${zombie_run_ids}" ]; then
    for run_id in ${zombie_run_ids}; do
      echo "  Cancelling zombie workflow run ${run_id} for issue #${issue_num}..."
      gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/cancel" -X POST 2>/dev/null || true
    done
  fi
}

# Find and close the PR linked to an issue (if any).
close_linked_pr() {
  local issue_num="$1"
  local close_reason="${2:-Closed by orchestrator stall recovery.}"
  local pr_num
  pr_num="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" \
    --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
    || echo "")"
  if [[ "${pr_num}" =~ ^[0-9]+$ ]]; then
    local pr_state
    pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" --jq '.state' | grep -xE 'open|closed|merged' || echo "")"
    if [ "${pr_state}" = "open" ]; then
      echo "  Closing linked PR #${pr_num} for issue #${issue_num}..."
      gh_retry gh pr close "${pr_num}" --repo "${GITHUB_REPOSITORY}" \
        --comment "${close_reason}" 2>/dev/null || true
    fi
  fi
}

# ---------------------------------------------------------------
# Standalone stall recovery state helpers
# ---------------------------------------------------------------

STANDALONE_STATE_MARKER_OPEN="<!-- AI_STANDALONE_STALL_STATE_V1"
STANDALONE_STATE_MARKER_CLOSE="AI_STANDALONE_STALL_STATE_V1 -->"

# Pure parsing helpers: extract standalone state info from a pre-fetched
# comments JSON array (the array shape returned by GitHub's
# /issues/{n}/comments endpoint after paginate-flattening).  Using these
# lets callers that already hold the comments list avoid re-hitting the
# GitHub API just to re-parse the same data, which was previously a
# significant contributor to rate-limit pressure during standalone stall
# recovery sweeps.
_extract_standalone_state_comment_id_from_comments() {
  local comments_json="$1"
  printf '%s' "${comments_json:-[]}" \
    | jq -r --arg marker "${STANDALONE_STATE_MARKER_OPEN}" '(. // []) | [.[] | select((.body // "") | contains($marker))] | max_by(.created_at // "") | .id // ""' \
    2>/dev/null || true
}

_extract_standalone_state_json_from_comments() {
  local comments_json="$1"
  local state_raw
  state_raw="$(printf '%s' "${comments_json:-[]}" \
    | jq -r --arg marker "${STANDALONE_STATE_MARKER_OPEN}" '(. // []) | [.[] | select((.body // "") | contains($marker))] | max_by(.created_at // "") | .body // ""' \
    2>/dev/null || echo "")"

  if [ -z "${state_raw}" ] || [ "${state_raw}" = "null" ]; then
    echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0}'
    return
  fi

  local extracted
  extracted="$(printf '%s' "${state_raw}" | sed -n "/^${STANDALONE_STATE_MARKER_OPEN}$/,/^${STANDALONE_STATE_MARKER_CLOSE}$/p" | sed '1d;$d')"
  if [ -z "${extracted}" ]; then
    echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0}'
    return
  fi

  if ! echo "${extracted}" | jq -e . >/dev/null 2>&1; then
    echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0}'
    return
  fi

  echo "${extracted}" | jq -c '
    {
      schema_version: 1,
      last_seen_phase: (.last_seen_phase // ""),
      status_since_ts: ((.status_since_ts // 0) | tonumber),
      stall_recovery_count: ((.stall_recovery_count // 0) | tonumber),
      updated_ts: ((.updated_ts // 0) | tonumber)
    }
  ' 2>/dev/null || echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0}'
}

# Fetch the issue comments once and return the latest standalone state
# comment id.  Thin wrapper around the pure-parsing helper above; kept
# for callers that don't already have a comments list in hand.
get_standalone_state_comment_id() {
  local issue_num="$1"
  local comments_json
  if ! comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null)"; then
    comments_json='[]'
  fi
  _extract_standalone_state_comment_id_from_comments "${comments_json}"
}

read_standalone_state_json() {
  local issue_num="$1"
  local comments_json
  if ! comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null)"; then
    comments_json='[]'
  fi
  _extract_standalone_state_json_from_comments "${comments_json}"
}

write_standalone_state_json() {
  local issue_num="$1"
  local state_json="$2"
  local comment_body
  local comment_id

  comment_body="${STANDALONE_STATE_MARKER_OPEN}
${state_json}
${STANDALONE_STATE_MARKER_CLOSE}"

  # Optional 3rd argument: caller-supplied comment id.  Passing it (even
  # empty) skips the otherwise-automatic lookup, which saves a full
  # paginated /comments fetch whenever the caller already knows whether
  # a state comment exists (e.g. the standalone stall recovery loop,
  # which parses it out of its own cached comments_json).  Empty string
  # means "known-not-present, create a new comment".
  if [ "$#" -ge 3 ]; then
    comment_id="$3"
  else
    comment_id="$(get_standalone_state_comment_id "${issue_num}")"
  fi
  if [ -n "${comment_id}" ] && [ "${comment_id}" != "null" ]; then
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}" \
      -X PATCH -f body="${comment_body}" >/dev/null 2>&1 || true
  else
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
      -f body="${comment_body}" >/dev/null 2>&1 || true
  fi
}

recovery_action_for_phase() {
  local phase="$1"
  local recovery_count="$2"

  if [ "${recovery_count}" -ge "${MAX_STALL_RECOVERIES_PER_ISSUE}" ]; then
    echo "skip"
    return
  fi

  local action
  action="$(python3 - "$phase" "$recovery_count" <<'PY'
import sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import STALL_RECOVERY_ACTIONS

phase = sys.argv[1]
recovery_count = int(sys.argv[2])
actions = STALL_RECOVERY_ACTIONS.get(phase, ["retrigger_pipeline"])
idx = min(recovery_count, len(actions) - 1)
print(actions[idx])
PY
)"
  if [ -z "${action}" ]; then
    echo "retrigger_pipeline"
  else
    echo "${action}"
  fi
}

stall_recovery_action_is_terminal() {
  local action="$1"
  case "${action}" in
    close_and_reissue|skip|escalate_human)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

_robust_parse_json_file() {
  local file_path="$1"
  local parse_log="${RUNTIME_DIR:-/tmp}/stall_judge.log"
  python3 -c "
import json, re, sys

try:
    raw = open(sys.argv[1], 'r').read()
except FileNotFoundError:
    print(f'File not found: {sys.argv[1]}', file=sys.stderr)
    sys.exit(1)

try:
    data = json.loads(raw.strip())
    json.dump(data, sys.stdout)
    sys.exit(0)
except json.JSONDecodeError:
    pass

cleaned = re.sub(r'\`\`\`(?:json)?\s*', '', raw)
cleaned = re.sub(r'\`\`\`\s*$', '', cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for i, ch in enumerate(cleaned):
    if ch == '{':
        if brace_depth == 0:
            start = i
        brace_depth += 1
    elif ch == '}':
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            candidate = cleaned[start:i+1]
            try:
                data = json.loads(candidate)
                json.dump(data, sys.stdout)
                sys.exit(0)
            except json.JSONDecodeError:
                start = None

print('Could not parse JSON output', file=sys.stderr)
sys.exit(1)
" "${file_path}" 2>> "${parse_log}" || echo ""
}

execute_stall_recovery_action() {
  local issue_num="$1"
  local phase="$2"
  local action="$3"
  local recovery_count="$4"
  local local_id="$5"
  local stall_minutes="$6"

  STALL_RECOVERY_SHOULD_INCREMENT="false"
  STALL_RECOVERY_EFFECTIVE_ACTION="${action}"

  case "${action}" in
    retrigger_pipeline)
      echo "  Re-triggering pipeline for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/reclarify

_Orchestrator stall recovery: this issue never entered the AI pipeline.
Re-triggering the clarification phase. If the issue description is
sufficient, proceed directly to planning and implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: re-triggered pipeline for issue #${issue_num} (stuck ${stall_minutes}m with no labels, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    auto_respond_clarify)
      echo "  Auto-responding to clarification for issue #${issue_num}..."
      local recommended_answers
      local answer_body
      recommended_answers="$(extract_recommended_answers "${issue_num}")"
      if [ -n "${recommended_answers}" ]; then
        answer_body="/answer [auto-answered-by-orchestrator]

_Orchestrator stall recovery: this issue has been in clarification for
too long. Auto-selecting recommended answers and proceeding with
planning and implementation._

${recommended_answers}"
      else
        answer_body="/answer [auto-answered-by-orchestrator]

_Orchestrator stall recovery: this issue has been in clarification for
too long. No recommended answers could be extracted — the issue
description is deemed sufficient. Proceed with planning and
implementation._"
      fi
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="${answer_body}" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: auto-responded to clarification on issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    retrigger_plan)
      echo "  Re-triggering plan for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/answer

_Orchestrator stall recovery: planning phase stalled. Re-triggering
plan generation._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: re-triggered planning for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    auto_approve)
      local noop_cnt
      noop_cnt="$(get_impl_noop_count "${local_id}")"
      if [ "${noop_cnt}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
        echo "  [stall-recovery] Issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt}/${MAX_IMPL_NOOP_REISSUES}) — closing to let judge verify."
        gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:awaiting-approval' --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          -c "Closing: implementation produced no changes ${noop_cnt} time(s). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
        tg_notify "Stall recovery: issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt}). Closed — judge will verify."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
        STALL_RECOVERY_EFFECTIVE_ACTION="close_and_reissue"
        return 0
      fi
      echo "  Auto-approving plan for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: auto-approving plan. This is an
orchestrator-managed issue that does not require human approval._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: auto-approved plan for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    retrigger_implement)
      local noop_cnt_impl
      noop_cnt_impl="$(get_impl_noop_count "${local_id}")"
      if [ "${noop_cnt_impl}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
        echo "  [stall-recovery] Issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt_impl}/${MAX_IMPL_NOOP_REISSUES}) — closing to let judge verify."
        gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:implementing' --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          -c "Closing: implementation produced no changes ${noop_cnt_impl} time(s). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
        tg_notify "Stall recovery: issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt_impl}). Closed — judge will verify."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
        STALL_RECOVERY_EFFECTIVE_ACTION="close_and_reissue"
        return 0
      fi
      echo "  Re-triggering implementation for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: implementation phase appears stalled.
Re-triggering implementation. If a previous attempt crashed or timed
out, start fresh from the approved plan._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: re-triggered implementation for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    retrigger_review)
      echo "  Re-triggering review for issue #${issue_num}..."
      local pr_num
      pr_num="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" \
        --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
        || echo "")"
      if [[ "${pr_num}" =~ ^[0-9]+$ ]]; then
        local head_ref
        head_ref="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" --jq '.head.ref' || echo "")"
        if [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ]; then
          if git fetch origin "${head_ref}:refs/remotes/origin/${head_ref}" 2>/dev/null && \
             git checkout "origin/${head_ref}" 2>/dev/null; then
            git config user.name "codex-bot"
            git config user.email "codex@users.noreply.github.com"
            git commit --allow-empty -m "[orchestrator] stall recovery: re-trigger review for issue #${issue_num}" 2>/dev/null || true
            if git push origin "HEAD:${head_ref}" 2>/dev/null; then
              tg_notify "Stall recovery: re-triggered review for PR #${pr_num} (issue #${issue_num}, stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"PR: $(_gh_url "pull/${pr_num}")"$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
            fi
            git checkout --detach HEAD 2>/dev/null || true
          fi
        fi
      else
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
          -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: issue is marked done but no PR was found.
Re-triggering implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
        tg_notify "Stall recovery: re-triggered implement for issue #${issue_num} (ai:done but no PR, stuck ${stall_minutes}m)."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      fi
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    attempt_merge)
      echo "  Issue #${issue_num} stuck at ready-to-merge. Main merge loop will retry."
      tg_notify "Stall recovery: issue #${issue_num} stuck at ready-to-merge for ${stall_minutes}m (attempt $((recovery_count + 1))). Merge loop will retry."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    close_and_reissue)
      echo "  Closing and re-issuing stalled issue #${issue_num}..."
      close_linked_pr "${issue_num}" \
        "Closed by orchestrator stall recovery — issue #${issue_num} was stuck in '${phase}' for ${stall_minutes}m. A replacement issue will be created."

      local orig_title orig_body
      orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title // ""' || echo "")"
      orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"

      ensure_label_exists "ai:closed"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --remove-label 'ai:done' --remove-label 'ai:implementing' \
        --remove-label 'ai:planning' --remove-label 'ai:clarification' \
        --remove-label 'ai:awaiting-approval' \
        --add-label 'ai:closed' 2>/dev/null || true
      gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: orchestrator stall recovery. Issue was stuck in '${phase}' for ${stall_minutes} minutes after $((recovery_count + 1)) recovery attempt(s). Re-issuing with additional guidance." 2>/dev/null || true

      local new_body
      new_body="$(cat <<REISSUE_EOF
${orig_body}

---

**⚠️ Re-issued from #${issue_num}** — the previous issue stalled in the \`${phase}\` phase for ${stall_minutes} minutes despite $((recovery_count + 1)) recovery attempt(s).

**Guidance for AI agents:**
- This issue has been re-created by the orchestrator stall recovery system.
- Previous attempt stalled at phase: \`${phase}\`
- Proceed through the full pipeline (clarify → plan → implement → review).
- If the task encounters the same blocker, explain the specific failure in a comment.
REISSUE_EOF
)"

      local new_url new_url_clean new_num
      ensure_label_exists "ai:clarification"
      new_url="$(gh_retry gh issue create --repo "${GITHUB_REPOSITORY}" \
        --title "${orig_title}" \
        --body "${new_body}" \
        --label "ai:clarification" 2>/dev/null || echo "")"
      if [ -n "${new_url}" ]; then
        new_url_clean="$(printf '%s\n' "${new_url}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
        new_num="$(basename "${new_url_clean%%[?#]*}")"
        if [[ "${new_num}" =~ ^[0-9]+$ ]] && [ -n "${local_id}" ] && [ "${local_id}" != "null" ]; then
          jq --arg lid "${local_id}" --argjson new_num "${new_num}" --argjson wave_idx "${WAVE_IDX}" \
            '.issue_number_map[$lid] = $new_num |
             (.waves[$wave_idx].issues[] | select(.id == $lid)) |=
               (.github_issue = $new_num | .status = "pending" | .last_seen_phase = "" | .status_since_ts = (now | floor) | .stall_recovery_count = 0)' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        fi
        tg_notify "Stall recovery: closed stalled issue #${issue_num} and re-issued as #${new_num} (phase: ${phase}, stuck ${stall_minutes}m)."$'\n'"Old issue: $(_gh_url "issues/${issue_num}")"$'\n'"New issue: $(_gh_url "issues/${new_num}")" "WARNING"
      else
        echo "::warning::Could not create replacement issue for stalled #${issue_num}."
      fi
      ;;

    skip)
      echo "  Skipping issue #${issue_num} after ${recovery_count} recovery attempts."
      close_linked_pr "${issue_num}" \
        "Closed by orchestrator: stall recovery exhausted (${recovery_count} attempts). The judge will evaluate this gap."
      ensure_label_exists "ai:closed"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --add-label 'ai:closed' 2>/dev/null || true
      gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: orchestrator stall recovery exhausted (${recovery_count} attempts over $((stall_minutes)) minutes in '${phase}' phase). The judge will evaluate this gap at wave completion." 2>/dev/null || true
      post_tracking_comment "## ⏭️ Issue #${issue_num} skipped (stall recovery exhausted)

- **Phase:** \`${phase}\`
- **Stalled for:** ${stall_minutes} minutes
- **Recovery attempts:** ${recovery_count}
- **Local ID:** \`${local_id}\`

The judge will evaluate this gap when the wave completes and decide whether to reissue, accept, or adjust the project."
      tg_notify "Issue #${issue_num} skipped after ${recovery_count} stall recovery attempts (${stall_minutes}m in '${phase}'). Judge will handle at wave completion."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    escalate_human)
      ensure_label_exists "ai:needs-human"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:needs-human" >/dev/null 2>&1 || true
      tg_notify "Stall judge escalated issue #${issue_num} for human intervention (phase ${phase}, stuck ${stall_minutes}m)."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "CRITICAL"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    resolve_merge_conflict)
      local target_pr="${STALL_JUDGE_TARGET_PR:-}"
      local head_ref="${STALL_JUDGE_HEAD_REF:-}"
      if ! [[ "${target_pr}" =~ ^[0-9]+$ ]] || [ -z "${head_ref}" ]; then
        echo "::warning::resolve_merge_conflict requires target_pr and head_ref; skipping dispatch"
        return 1
      fi
      local pr_json
      local head_sha
      local dispatch_rc=0
      pr_json="$(_fetch_pr_json "${target_pr}")"
      head_sha="$(_jq_field "${pr_json}" '.head.sha')"
      if [ -n "${head_sha}" ]; then
        gh api "repos/${GITHUB_REPOSITORY}/pulls/${target_pr}/update-branch" \
          -X PUT -f expected_head_sha="${head_sha}" >/dev/null 2>&1 || true
      fi
      _dispatch_review_for_conflicts "${target_pr}" "${head_ref}" || dispatch_rc=$?
      if [ "${dispatch_rc}" -eq 1 ]; then
        return 1
      fi
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    *)
      echo "::warning::Unknown stall recovery action: ${action} for issue #${issue_num}"
      return 1
      ;;
  esac

  return 0
}

invoke_stall_judge() {
  local issue_num="$1"
  local phase="$2"
  local recovery_count="$3"
  local stall_minutes="$4"
  local local_id="${5:-}"

  local fallback_action
  fallback_action="$(recovery_action_for_phase "${phase}" "${recovery_count}")"

  local comments_issue_num="${issue_num}"
  if [ -n "${local_id}" ] && [[ "${TRACKING_NUM:-}" =~ ^[0-9]+$ ]]; then
    comments_issue_num="${TRACKING_NUM}"
  fi

  local issue_comments_json
  issue_comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${comments_issue_num}/comments?per_page=100" | jq -s 'add // []' 2>/dev/null || echo '[]')"
  local recent_comments
  recent_comments="$(printf '%s' "${issue_comments_json}" | jq -c '[.[] | {author: (.user.login // ""), created_at: (.created_at // ""), body: (.body // "")}] | (if length > 8 then .[-8:] else . end)' 2>/dev/null || echo '[]')"

  local linked_pr
  linked_pr="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' || echo "")"

  local linked_pr_json='{}'
  local target_pr=""
  local pr_state=""
  local pr_mergeable=""
  local head_ref=""
  local base_ref=""
  local head_sha=""

  if [[ "${linked_pr}" =~ ^[0-9]+$ ]]; then
    target_pr="${linked_pr}"
    linked_pr_json="$(_fetch_pr_json "${linked_pr}")"
    pr_state="$(_jq_field "${linked_pr_json}" '.state' 'open|closed|merged')"
    pr_mergeable="$(_jq_field "${linked_pr_json}" '.mergeable' 'true|false')"
    head_ref="$(_jq_field "${linked_pr_json}" '.head.ref')"
    base_ref="$(_jq_field "${linked_pr_json}" '.base.ref')"
    head_sha="$(_jq_field "${linked_pr_json}" '.head.sha')"
  fi

  local workflows_json
  local workflow_outcomes
  workflows_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs?per_page=50" 2>/dev/null || echo '{"workflow_runs":[]}')"
  workflow_outcomes="$(printf '%s' "${workflows_json}" | jq -c --arg head_ref "${head_ref}" --arg head_sha "${head_sha}" '
    [.workflow_runs[]?
      | select((.name // "") == "AI Review"
               or (.name // "") == "Internal Review"
               or (.name // "") == "Review Autofix"
               or (.path // "" | endswith("ai-review.yml"))
               or (.path // "" | endswith("internal-review.yml"))
               or (.path // "" | endswith("review_autofix.yml")))
      | select(($head_ref != "" and (.head_branch // "") == $head_ref) or ($head_sha != "" and (.head_sha // "") == $head_sha))
      | {id: .id, workflow: (.name // ""), conclusion: (.conclusion // ""), status: (.status // ""), head_branch: (.head_branch // ""), created_at: (.created_at // "")}
    ]
    | sort_by(.created_at)
    | reverse
    | .[:3]
  ' 2>/dev/null || echo '[]')"

  local prior_actions='[]'
  if [ -n "${local_id}" ] && [ "${local_id}" != "null" ]; then
    prior_actions="$(jq -c --arg lid "${local_id}" --argjson wi "${WAVE_IDX}" '
      (.waves[$wi].issues[] | select(.id == $lid)) as $issue
      | [
          {key:"stall_recovery_count", value: ($issue.stall_recovery_count // 0)},
          {key:"last_seen_phase", value: ($issue.last_seen_phase // "")},
          {key:"status", value: ($issue.status // "")}
        ]
    ' "${STATE_FILE}" 2>/dev/null || echo '[]')"
  fi

  local diagnostics
  diagnostics="$(jq -cn \
    --arg issue_number "${issue_num}" \
    --arg local_id "${local_id}" \
    --arg phase "${phase}" \
    --argjson stall_minutes "${stall_minutes}" \
    --argjson recovery_count "${recovery_count}" \
    --argjson recent_comments "${recent_comments}" \
    --arg target_pr "${target_pr}" \
    --arg pr_state "${pr_state}" \
    --arg pr_mergeable "${pr_mergeable}" \
    --arg head_ref "${head_ref}" \
    --arg base_ref "${base_ref}" \
    --argjson workflow_outcomes "${workflow_outcomes}" \
    --argjson current_wave "${CURRENT_WAVE:-1}" \
    --argjson prior_actions "${prior_actions}" \
    '{
      issue_number: ($issue_number | tonumber),
      local_id: (if $local_id == "" then null else $local_id end),
      phase: $phase,
      stall_minutes: $stall_minutes,
      recovery_count: $recovery_count,
      recent_tracking_comments: $recent_comments,
      linked_pr: {
        number: (if $target_pr == "" then null else ($target_pr | tonumber) end),
        state: (if $pr_state == "" then null else $pr_state end),
        mergeable: (if $pr_mergeable == "" then null else ($pr_mergeable == "true") end),
        head_ref: (if $head_ref == "" then null else $head_ref end),
        base_ref: (if $base_ref == "" then null else $base_ref end)
      },
      recent_review_workflow_outcomes: $workflow_outcomes,
      current_wave: $current_wave,
      prior_recovery_actions: $prior_actions
    }')"

  local stall_judge_prompt_file="${RUNTIME_DIR}/stall_judge_prompt_${issue_num}.txt"
  local stall_judge_output_file="${RUNTIME_DIR}/stall_judge_output_${issue_num}.txt"
  local static_file="${RUNTIME_DIR}/judge_static.txt"

  if [ ! -s "${static_file}" ]; then
    if ! assemble_judge_static_context "${static_file}"; then
      echo "WARNING: failed to assemble stall judge static context; continuing with fallback-safe execution" >&2
      : > "${static_file}"
    fi
  fi

  {
    cat "${static_file}"
    echo
    echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
    echo
    echo "=== STALL JUDGE TASK ==="
    echo
    bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt
    echo
    echo "=== STALL DIAGNOSTICS JSON ==="
    echo
    echo "${diagnostics}"
  } > "${stall_judge_prompt_file}"

  mkdir -p ~/.codex
  CATALOG_PATH="$(pwd)/scripts/codex_model_catalog.json"
  {
    echo 'web_search = "live"'
    echo 'model_provider = "openrouter"'
    echo "model = \"${MODEL_EDITOR}\""
    echo "model_reasoning_effort = \"${MODEL_REASONING_EFFORT_JUDGE}\""
    if [ -f "${CATALOG_PATH}" ]; then
      echo "model_catalog_json = \"${CATALOG_PATH}\""
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

  local judge_success="false"
  local attempt
  for attempt in 1 2; do
    if [ -n "${MOCK_STALL_JUDGE_JSON:-}" ]; then
      printf '%s\n' "${MOCK_STALL_JUDGE_JSON}" > "${stall_judge_output_file}"
    else
      codex exec --model "${MODEL_EDITOR}" --full-auto < "${stall_judge_prompt_file}" > "${stall_judge_output_file}" 2>> "${RUNTIME_DIR}/stall_judge.log" || true
    fi
    if grep -q '[^[:space:]]' "${stall_judge_output_file}"; then
      judge_success="true"
      break
    fi
    sleep $(( 8 * attempt ))
  done

  if [ "${judge_success}" != "true" ]; then
    echo "::warning::Stall judge failed for issue #${issue_num}; falling back to ${fallback_action}."
    execute_stall_recovery_action "${issue_num}" "${phase}" "${fallback_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
    return $?
  fi

  local judge_json
  judge_json="$(_robust_parse_json_file "${stall_judge_output_file}")"
  if [ -z "${judge_json}" ]; then
    echo "::warning::Could not parse stall judge output for #${issue_num}; falling back to ${fallback_action}."
    execute_stall_recovery_action "${issue_num}" "${phase}" "${fallback_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
    return $?
  fi

  local judge_action
  local judge_justification
  judge_action="$(echo "${judge_json}" | jq -r '.action // ""')"
  judge_justification="$(echo "${judge_json}" | jq -r '.justification // "no justification provided"')"
  STALL_JUDGE_TARGET_PR="$(echo "${judge_json}" | jq -r '.target_pr // empty')"
  if [ -z "${STALL_JUDGE_TARGET_PR}" ] && [[ "${target_pr}" =~ ^[0-9]+$ ]]; then
    STALL_JUDGE_TARGET_PR="${target_pr}"
  fi
  STALL_JUDGE_HEAD_REF="$(echo "${judge_json}" | jq -r '.head_ref // empty')"
  if [ -z "${STALL_JUDGE_HEAD_REF}" ] && [ -n "${head_ref}" ]; then
    STALL_JUDGE_HEAD_REF="${head_ref}"
  fi

  local judge_comment
  judge_comment="## 🧑‍⚖️ Stall Judge — Issue #${issue_num} attempt $((recovery_count + 1))

**Decision:** ${judge_action}
**Justification:** ${judge_justification}

**Diagnostics snapshot:**

\`\`\`json
${diagnostics}
\`\`\`
"
  if [ -n "${local_id}" ] && [[ "${TRACKING_NUM:-}" =~ ^[0-9]+$ ]]; then
    post_tracking_comment "${judge_comment}"
  else
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="${judge_comment}" >/dev/null 2>&1 || true
  fi
  tg_notify "Stall judge evaluated issue #${issue_num}: ${judge_action}. ${judge_justification}"$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"

  case "${judge_action}" in
    retrigger_pipeline|auto_respond_clarify|retrigger_plan|auto_approve|retrigger_implement|retrigger_review|attempt_merge|close_and_reissue|escalate_human)
      execute_stall_recovery_action "${issue_num}" "${phase}" "${judge_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
      return $?
      ;;
    resolve_merge_conflict)
      if ! [[ "${STALL_JUDGE_TARGET_PR}" =~ ^[0-9]+$ ]]; then
        echo "::warning::Stall judge returned resolve_merge_conflict without target_pr; falling back to ${fallback_action}."
        execute_stall_recovery_action "${issue_num}" "${phase}" "${fallback_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
        return $?
      fi
      if [ -z "${STALL_JUDGE_HEAD_REF}" ] || [ "${STALL_JUDGE_HEAD_REF}" = "null" ]; then
        echo "::warning::Stall judge returned resolve_merge_conflict without head_ref; falling back to ${fallback_action}."
        execute_stall_recovery_action "${issue_num}" "${phase}" "${fallback_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
        return $?
      fi
      if ! execute_stall_recovery_action "${issue_num}" "${phase}" "resolve_merge_conflict" "${recovery_count}" "${local_id}" "${stall_minutes}"; then
        echo "::warning::resolve_merge_conflict dispatch failed for issue #${issue_num}; falling back to ${fallback_action}."
        execute_stall_recovery_action "${issue_num}" "${phase}" "${fallback_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
        return $?
      fi
      return 0
      ;;
    *)
      echo "::warning::Unknown stall judge action '${judge_action}' for issue #${issue_num}; falling back to ${fallback_action}."
      execute_stall_recovery_action "${issue_num}" "${phase}" "${fallback_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
      return $?
      ;;
  esac
}

# Run both standalone-stall marker searches in a single GraphQL
# request via aliases, replacing two paginated REST /search/issues
# round-trips with one.  Returns a JSON object of the form
#   {"state": [{number:N},...], "clarify": [{number:N},...]}
# where each list normally comes from the single GraphQL request. If
# either query reports pagination (`hasNextPage`), we fall back to the
# original paginated REST search for complete coverage. On GraphQL
# failure, returns empty lists so the caller degrades gracefully.
_fetch_standalone_marker_issues_graphql() {
  local q_state="repo:${GITHUB_REPOSITORY} is:issue is:open \"AI_STANDALONE_STALL_STATE_V1\" in:comments"
  local q_clarify="repo:${GITHUB_REPOSITORY} is:issue is:open \"ai:clarification-questions\" in:comments"
  # shellcheck disable=SC2016
  local query='query($q_state: String!, $q_clarify: String!) {
  state: search(query: $q_state, type: ISSUE, first: 100) {
    pageInfo { hasNextPage }
    nodes { ... on Issue { number } }
  }
  clarify: search(query: $q_clarify, type: ISSUE, first: 100) {
    pageInfo { hasNextPage }
    nodes { ... on Issue { number } }
  }
}'
  local resp
  if ! resp="$(gh_retry gh api graphql \
      -F q_state="${q_state}" \
      -F q_clarify="${q_clarify}" \
      -f query="${query}" 2>/dev/null)"; then
    echo '{"state":[],"clarify":[]}'
    return
  fi

  local state_has_next
  local clarify_has_next
  state_has_next="$(printf '%s' "${resp}" | jq -r '.data.state.pageInfo.hasNextPage // false' 2>/dev/null || echo "false")"
  clarify_has_next="$(printf '%s' "${resp}" | jq -r '.data.clarify.pageInfo.hasNextPage // false' 2>/dev/null || echo "false")"
  if [ "${state_has_next}" = "true" ] || [ "${clarify_has_next}" = "true" ]; then
    local marker_state
    local marker_clarify
    marker_state="$(gh_retry gh api --paginate "search/issues" -f per_page=100 -f q="${q_state}" 2>/dev/null | jq -s '[.[].items[]? | {number}] | unique_by(.number)' 2>/dev/null || echo '[]')"
    marker_clarify="$(gh_retry gh api --paginate "search/issues" -f per_page=100 -f q="${q_clarify}" 2>/dev/null | jq -s '[.[].items[]? | {number}] | unique_by(.number)' 2>/dev/null || echo '[]')"
    jq -cn --argjson state "${marker_state}" --argjson clarify "${marker_clarify}" '{state:$state,clarify:$clarify}'
    return
  fi

  echo "${resp}" | jq -c '{
      state: [((.data.state.nodes // [])[] | select(. != null) | {number})],
      clarify: [((.data.clarify.nodes // [])[] | select(. != null) | {number})]
    }' 2>/dev/null || echo '{"state":[],"clarify":[]}'
}

# Batch-fetch labels and recent comments for a list of candidate issue
# numbers via a single GraphQL query per batch (aliased issue selectors).
# Replaces the per-candidate REST /labels and /comments round-trips
# with ceil(N / batch_size) GraphQL calls.  Returns a JSON object keyed
# by stringified issue number:
#   { "123": {"labels": ["ai:clarification"],
#             "comments": [{"id":N,"body":"...","created_at":"..."},...]},
#     ... }
# Comment shape mirrors the REST response (.id / .body / .created_at)
# so existing parsers (e.g. _extract_standalone_state_comment_id_from_comments)
# keep working unchanged.
#
# Trade-off: we fetch `comments(last: 100)` which covers only the 100
# newest comments rather than the full pagination walk the REST path
# did.  In practice the standalone-state marker comment is written
# every poll cycle (so it's always in the recent window), and the
# ai:clarification-questions marker is added near the top of the
# clarification phase before high comment counts accrue.  If an issue
# drifts past 100 comments *and* has an ancient marker, the
# label-based detection path still catches it.
_fetch_candidate_issue_details_graphql() {
  local numbers_json="$1"
  local count
  count="$(printf '%s' "${numbers_json}" | jq 'length' 2>/dev/null || echo 0)"
  if [ -z "${count}" ] || [ "${count}" -eq 0 ]; then
    echo '{}'
    return
  fi

  local owner="${GITHUB_REPOSITORY%%/*}"
  local name="${GITHUB_REPOSITORY##*/}"
  local batch_size=25
  local merged='{}'
  local start=0
  local end
  local i
  local n
  local query
  local fragment
  local batch_resp
  local batch_transformed

  while [ "${start}" -lt "${count}" ]; do
    end=$(( start + batch_size ))
    [ "${end}" -gt "${count}" ] && end="${count}"

    fragment=""
    for ((i=start; i<end; i++)); do
      n="$(printf '%s' "${numbers_json}" | jq -r ".[${i}]")"
      [[ "${n}" =~ ^[0-9]+$ ]] || continue
      fragment+=$'\n'"        i${i}: issue(number: ${n}) {
          number
          labels(first: 50) { nodes { name } }
          comments(last: 100) { nodes { databaseId body createdAt } }
        }"
    done

    if [ -z "${fragment}" ]; then
      start="${end}"
      continue
    fi

    query="query {
  repository(owner: \"${owner}\", name: \"${name}\") {${fragment}
  }
}"

    if ! batch_resp="$(gh_retry gh api graphql -f query="${query}" 2>/dev/null)"; then
      # Leave this batch's issues out of the cache; the loop-level
      # fallbacks (empty labels/comments → issue skipped naturally)
      # keep the cycle moving without crashing.
      start="${end}"
      continue
    fi

    batch_transformed="$(printf '%s' "${batch_resp}" | jq -c '
      (.data.repository // {}) | to_entries | map(
        select(.value != null and (.value.number? != null)) | {
          key: (.value.number | tostring),
          value: {
            labels: [(.value.labels.nodes // [])[]?.name],
            comments: [(.value.comments.nodes // [])[]? | {
              id: .databaseId,
              body: .body,
              created_at: .createdAt
            }]
          }
        }
      ) | from_entries
    ' 2>/dev/null || echo '{}')"

    merged="$(jq -s '.[0] * .[1]' <(printf '%s\n' "${merged}") <(printf '%s\n' "${batch_transformed}") 2>/dev/null || echo "${merged}")"

    start="${end}"
  done

  echo "${merged}"
}

run_standalone_stall_recovery() {
  if [ "${ENABLE_STANDALONE_STALL_RECOVERY}" != "true" ]; then
    echo "Standalone stall recovery disabled by ENABLE_STANDALONE_STALL_RECOVERY=${ENABLE_STANDALONE_STALL_RECOVERY}."
    return
  fi

  echo ""
  echo "========================================"
  echo "Standalone issue stall recovery"
  echo "========================================"

  local orchestrator_managed_set=""
  local t_count
  local t_idx
  local t_num
  local t_comments
  local t_state_json
  local managed_nums

  if [ -f "${RUNTIME_DIR}/tracking_issues.json" ]; then
    t_count="$(jq 'length' "${RUNTIME_DIR}/tracking_issues.json" 2>/dev/null || echo "0")"
    for ((t_idx=0; t_idx<t_count; t_idx++)); do
      t_num="$(jq -r ".[${t_idx}].number" "${RUNTIME_DIR}/tracking_issues.json" 2>/dev/null || echo "")"
      [ -n "${t_num}" ] || continue
      orchestrator_managed_set="${orchestrator_managed_set}"$'\n'"${t_num}"
      if ! t_comments="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${t_num}/comments?per_page=100" | jq -s 'add // []' 2>/dev/null)"; then
        t_comments='[]'
      fi
      t_state_json=""
      if extract_latest_valid_orchestrator_state "${t_comments}"; then
        t_state_json="${EXTRACTED_STATE_JSON}"
      fi
      managed_nums="$(printf '%s' "${t_state_json}" | jq -r '.waves[]?.issues[]?.github_issue // empty' 2>/dev/null || true)"
      if [ -n "${managed_nums}" ]; then
        orchestrator_managed_set="${orchestrator_managed_set}"$'\n'"${managed_nums}"
      fi
    done
  fi
  orchestrator_managed_set="$(printf '%s\n' "${orchestrator_managed_set}" | grep -E '^[0-9]+$' | sort -u || true)"

  local pipeline_labels='["ai:clarification","ai:planning","ai:awaiting-approval","ai:implementing","ai:done","ai:ready-to-merge"]'
  local labeled_issues='[]'
  local lbl
  for lbl in ai:clarification ai:planning ai:awaiting-approval ai:implementing ai:done ai:ready-to-merge; do
    local by_label
    by_label="$(gh_retry gh issue list --repo "${GITHUB_REPOSITORY}" --state open --label "${lbl}" --json number --limit 1000 2>/dev/null || echo '[]')"
    labeled_issues="$(jq -s 'add | unique_by(.number)' <(printf '%s\n' "${labeled_issues}") <(printf '%s\n' "${by_label}"))"
  done

  local marker_issues
  local marker_state
  local marker_clarify
  local _markers_resp
  _markers_resp="$(_fetch_standalone_marker_issues_graphql)"
  marker_state="$(printf '%s' "${_markers_resp}" | jq -c '.state // []' 2>/dev/null || echo '[]')"
  marker_clarify="$(printf '%s' "${_markers_resp}" | jq -c '.clarify // []' 2>/dev/null || echo '[]')"
  marker_issues="$(jq -s 'add | unique_by(.number)' <(printf '%s\n' "${marker_state}") <(printf '%s\n' "${marker_clarify}"))"

  local candidates
  candidates="$(jq -s 'add | unique_by(.number)' <(printf '%s\n' "${labeled_issues}") <(printf '%s\n' "${marker_issues}"))"

  ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"

  local _candidate_details_json
  _candidate_details_json="$(_fetch_candidate_issue_details_graphql "$(printf '%s' "${candidates}" | jq -c '[.[].number]')")"

  local c_count
  c_count="$(echo "${candidates}" | jq 'length')"
  local c_idx
  local issue_num
  local labels_json
  local has_pipeline_label
  local comments_json
  local has_marker
  local phase
  local state_json
  local state_comment_id
  local updated_state
  local status_since
  local recovery_count
  local threshold_minutes
  local elapsed_minutes
  local action
  local took_action

  for ((c_idx=0; c_idx<c_count; c_idx++)); do
    issue_num="$(echo "${candidates}" | jq -r ".[${c_idx}].number")"
    [ -n "${issue_num}" ] || continue

    if [ -n "${orchestrator_managed_set}" ] && echo "${orchestrator_managed_set}" | grep -qxF "${issue_num}"; then
      continue
    fi

    if printf '%s' "${_candidate_details_json}" | jq -e --arg n "${issue_num}" 'has($n)' >/dev/null 2>&1; then
      labels_json="$(printf '%s' "${_candidate_details_json}" | jq -c --arg n "${issue_num}" '.[$n].labels // []')"
      comments_json="$(printf '%s' "${_candidate_details_json}" | jq -c --arg n "${issue_num}" '.[$n].comments // []')"
    else
      labels_json="$(get_issue_labels_json "${issue_num}")"
      comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null || echo '[]')"
    fi
    has_pipeline_label="$(echo "${labels_json}" | jq -r --argjson wanted "${pipeline_labels}" '[.[] | select($wanted | index(.))] | length')"
    has_marker="$(echo "${comments_json}" | jq -r '[.[] | select((.body // "") | test("<!-- AI_STANDALONE_STALL_STATE_V1|<!-- ai:clarification-questions -->"))] | length')"

    if [ "${has_pipeline_label}" -eq 0 ] && [ "${has_marker}" -eq 0 ]; then
      continue
    fi

    phase="$(python3 - "$labels_json" <<'PY'
import json, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import determine_phase
labels = json.loads(sys.argv[1])
print(determine_phase(labels))
PY
)"

    if [ "${phase}" = "ai:review-blocked" ] || [ "${phase}" = "ai:implementation-failed" ] || [ "${phase}" = "ai:validating" ] || [ "${phase}" = "ai:validation-fixing" ] || [ "${phase}" = "ai:merged" ] || [ "${phase}" = "ai:closed" ] || [ "${phase}" = "ai:validated" ] || [ "${phase}" = "ai:validation-failed" ]; then
      continue
    fi

    state_comment_id="$(_extract_standalone_state_comment_id_from_comments "${comments_json}")"
    state_json="$(_extract_standalone_state_json_from_comments "${comments_json}")"

    updated_state="$(python3 - "$state_json" "$phase" <<'PY'
import json, sys, time
state = json.loads(sys.argv[1])
phase = sys.argv[2]
now = int(time.time())
last = state.get("last_seen_phase", "")
if phase != last:
    state["last_seen_phase"] = phase
    state["status_since_ts"] = now
    if last:
        state["stall_recovery_count"] = 0
    state["updated_ts"] = now
elif not state.get("status_since_ts"):
    state["status_since_ts"] = now
    state["updated_ts"] = now
state["schema_version"] = 1
print(json.dumps(state, separators=(",", ":")))
PY
)"

    status_since="$(echo "${updated_state}" | jq -r '.status_since_ts // 0')"
    recovery_count="$(echo "${updated_state}" | jq -r '.stall_recovery_count // 0')"
    threshold_minutes="$(python3 - "$phase" "$STALL_THRESHOLD_MINUTES" "$PHASE_THRESHOLDS_JSON" <<'PY'
import json, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import DEFAULT_PHASE_STALL_THRESHOLDS
phase = sys.argv[1]
fallback = int(sys.argv[2])
overrides_raw = sys.argv[3]
thresholds = dict(DEFAULT_PHASE_STALL_THRESHOLDS)
if overrides_raw:
    try:
        thresholds.update({k:int(v) for k,v in json.loads(overrides_raw).items()})
    except Exception:
        pass
print(thresholds.get(phase, fallback))
PY
)"

    elapsed_minutes="$(( ( $(date +%s) - status_since ) / 60 ))"
    if [ "${status_since}" -le 0 ] || [ "${elapsed_minutes}" -lt "${threshold_minutes}" ]; then
      if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
        write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
      fi
      continue
    fi

    if [ "${recovery_count}" -ge "${MAX_STALL_RECOVERIES_PER_ISSUE}" ]; then
      action="skip"
    elif [ "${ENABLE_STALL_JUDGE}" = "true" ] && [ "${recovery_count}" -ge "${STALL_JUDGE_TRIGGER_COUNT}" ]; then
      action="run_stall_judge"
    else
      action="$(recovery_action_for_phase "${phase}" "${recovery_count}")"
    fi
    echo "  [standalone-stall] Issue #${issue_num} stuck in '${phase}' for ${elapsed_minutes}m (attempt $((recovery_count + 1))). Action: ${action}"

    if issue_has_active_workflow "${issue_num}"; then
      echo "  [standalone-stall] Issue #${issue_num} has a recent active workflow run — skipping recovery."
      if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
        write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
      fi
      continue
    fi

    if [ "${action}" != "skip" ] && [ "${action}" != "attempt_merge" ] && [ "${action}" != "escalate_human" ]; then
      cancel_zombie_runs_for_issue "${issue_num}"
    fi

    took_action="false"
    STALL_RECOVERY_SHOULD_INCREMENT="false"
    STALL_RECOVERY_EFFECTIVE_ACTION="${action}"

    case "${action}" in
      run_stall_judge)
        if invoke_stall_judge "${issue_num}" "${phase}" "${recovery_count}" "${elapsed_minutes}" ""; then
          took_action="true"
          action="${STALL_RECOVERY_EFFECTIVE_ACTION:-run_stall_judge}"
        fi
        ;;
      retrigger_pipeline)
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/reclarify

_Standalone stall recovery: this issue did not enter the AI pipeline.
Re-triggering clarification._
STALL_EOF
)" >/dev/null 2>&1 || true
        tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered pipeline (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      auto_respond_clarify)
        local rec_answers
        local answer_body
        rec_answers="$(extract_recommended_answers "${issue_num}")"
        if [ -n "${rec_answers}" ]; then
          answer_body="/answer [auto-answered-by-poller]

_Standalone stall recovery: clarification stalled. Auto-selecting recommended answers._

${rec_answers}"
        else
          answer_body="/answer [auto-answered-by-poller]

_Standalone stall recovery: clarification stalled. Proceeding with available context._"
        fi
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="${answer_body}" >/dev/null 2>&1 || true
        tg_notify_issue "${issue_num}" "Standalone stall recovery: auto-responded to clarification (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      retrigger_plan)
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/answer

_Standalone stall recovery: planning stalled. Re-triggering plan generation._
STALL_EOF
)" >/dev/null 2>&1 || true
        tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered planning (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      auto_approve)
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/approved

_Standalone stall recovery: plan approval stalled. Auto-approving to proceed._
STALL_EOF
)" >/dev/null 2>&1 || true
        tg_notify_issue "${issue_num}" "Standalone stall recovery: auto-approved plan (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      retrigger_implement)
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/approved

_Standalone stall recovery: implementation stalled. Re-triggering implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
        tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered implementation (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      retrigger_review)
        local pr_num
        local pr_lookup_ok="false"
        local head_ref
        local timeline_json
        local pr_json
        timeline_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" 2>/dev/null || echo "")"
        pr_num="$(printf '%s' "${timeline_json}" | jq -r '
          if (type == "array") then
            [.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last // empty
          else
            empty
          end
        ' 2>/dev/null | tail -n1)"
        if printf '%s' "${timeline_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
          pr_lookup_ok="true"
        fi
        if [ "${pr_lookup_ok}" = "true" ] && [[ "${pr_num}" =~ ^[0-9]+$ ]]; then
          pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" 2>/dev/null || echo "")"
          head_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.ref?) then .head.ref else empty end' 2>/dev/null | tail -n1)"
          if [ -n "${head_ref}" ] && git fetch origin "${head_ref}:refs/remotes/origin/${head_ref}" 2>/dev/null && git checkout "origin/${head_ref}" 2>/dev/null; then
            git config user.name "codex-bot"
            git config user.email "codex@users.noreply.github.com"
            git commit --allow-empty -m "[standalone] stall recovery: re-trigger review for issue #${issue_num}" 2>/dev/null || true
            git push origin "HEAD:${head_ref}" 2>/dev/null || true
            git checkout --detach HEAD 2>/dev/null || true
          fi
        elif [ "${pr_lookup_ok}" = "true" ]; then
          gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/approved

_Standalone stall recovery: issue marked done but no linked PR found. Re-triggering implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered review flow (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      attempt_merge)
        local merge_pr
        local merge_timeline_json
        merge_timeline_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" 2>/dev/null || echo "")"
        merge_pr="$(printf '%s' "${merge_timeline_json}" | jq -r '
          if (type == "array") then
            [.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last // empty
          else
            empty
          end
        ' 2>/dev/null | tail -n1)"
        if [[ "${merge_pr}" =~ ^[0-9]+$ ]]; then
          local merge_pr_json
          local merge_state
          local merge_mergeable
          merge_pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${merge_pr}" 2>/dev/null || echo "")"
          merge_state="$(printf '%s' "${merge_pr_json}" | jq -r 'if (type == "object" and .state?) then .state else empty end' 2>/dev/null | tail -n1)"
          merge_mergeable="$(printf '%s' "${merge_pr_json}" | jq -r 'if (type == "object" and (.mergeable == true or .mergeable == false)) then .mergeable else empty end' 2>/dev/null | tail -n1)"
          if [ "${merge_state}" = "open" ] && [ "${merge_mergeable}" = "true" ] && _pr_checks_completed "${merge_pr}"; then
            gh pr merge "${merge_pr}" --repo "${GITHUB_REPOSITORY}" --squash --auto >/dev/null 2>&1 \
              || gh pr merge "${merge_pr}" --repo "${GITHUB_REPOSITORY}" --squash >/dev/null 2>&1 \
              || true
          fi
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: attempted merge retry for ready-to-merge issue (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      close_and_reissue)
        local orig_title
        local orig_body
        local new_body
        local new_url
        local new_url_clean
        local bt='`'
        local new_num
        orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title' || echo "")"
        orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"

        new_body="$(cat <<REISSUE_EOF
${orig_body}

---

**⚠️ Re-issued from #${issue_num}** — previous issue stalled in ${bt}${phase}${bt} for ${elapsed_minutes} minutes despite $((recovery_count + 1)) recovery attempt(s).

**Guidance for AI agents:**
- This issue was re-created by standalone stall recovery.
- Previous attempt stalled at phase: ${bt}${phase}${bt}.
- Proceed through clarify → plan → implement → review.
REISSUE_EOF
)"
        ensure_label_exists "ai:clarification"
        new_url="$(gh_retry gh issue create --repo "${GITHUB_REPOSITORY}" --title "${orig_title}" --body "${new_body}" --label "ai:clarification" 2>/dev/null || echo "")"
        new_url_clean="$(printf '%s\n' "${new_url}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
        new_num="$(basename "${new_url_clean%%[?#]*}")"
        if [[ "${new_num}" =~ ^[0-9]+$ ]]; then
          close_linked_pr "${issue_num}" "Closed by standalone stall recovery — issue #${issue_num} was stuck in '${phase}' for ${elapsed_minutes}m."
          ensure_label_exists "ai:closed"
          gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:done' --remove-label 'ai:implementing' --remove-label 'ai:planning' --remove-label 'ai:clarification' --remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
            --add-label 'ai:closed' 2>/dev/null || true
          gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" -c "Closing: standalone stall recovery. Issue was stuck in '${phase}' for ${elapsed_minutes} minutes after $((recovery_count + 1)) recovery attempt(s)." 2>/dev/null || true
          local new_state
          new_state="$(python3 - "$updated_state" <<'PY'
import json, sys, time
state = json.loads(sys.argv[1])
now = int(time.time())
state["last_seen_phase"] = ""
state["status_since_ts"] = now
state["stall_recovery_count"] = 0
state["updated_ts"] = now
print(json.dumps(state, separators=(",", ":")))
PY
)"
          write_standalone_state_json "${new_num}" "${new_state}" ""
          tg_notify_issue "${issue_num}" "Standalone stall recovery: closed and re-issued as #${new_num} (phase: ${phase}, stuck ${elapsed_minutes}m)." "WARNING"
        else
          echo "::warning::Standalone close_and_reissue failed to create replacement issue for #${issue_num}."
          tg_notify_issue "${issue_num}" "Standalone stall recovery: attempted close-and-reissue but could not create replacement issue." "ERROR"
          write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
        fi
        took_action="true"
        ;;
      skip)
        close_linked_pr "${issue_num}" "Closed by standalone stall recovery: max recovery attempts exhausted (${recovery_count})."
        ensure_label_exists "ai:closed"
        gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" -c "Closing: standalone stall recovery exhausted (${recovery_count} attempts over ${elapsed_minutes} minutes in '${phase}' phase)." 2>/dev/null || true
        tg_notify_issue "${issue_num}" "Standalone stall recovery exhausted for issue #${issue_num} (${recovery_count} attempts). Issue closed with ai:closed." "CRITICAL"
        STALL_RECOVERY_SHOULD_INCREMENT="false"
        took_action="true"
        ;;
      *)
        echo "::warning::Unknown standalone stall action ${action} for issue #${issue_num}"
        ;;
    esac

    if [ "${took_action}" = "true" ] && [ "${action}" != "close_and_reissue" ]; then
      updated_state="$(python3 - "$updated_state" "$STALL_RECOVERY_SHOULD_INCREMENT" <<'PY'
import json, sys, time
state = json.loads(sys.argv[1])
should_increment = sys.argv[2].lower() == "true"
now = int(time.time())
if should_increment:
    state["stall_recovery_count"] = int(state.get("stall_recovery_count", 0)) + 1
state["status_since_ts"] = now
state["updated_ts"] = now
print(json.dumps(state, separators=(",", ":")))
PY
)"
      write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
    fi
  done
}

# ---------------------------------------------------------------
# Implementation no-op tracking helpers
# ---------------------------------------------------------------

# Read the impl_noop_count for a local_id from the state file.
get_impl_noop_count() {
  local lid="$1"
  STATE_FILE="${STATE_FILE}" IMPL_NOOP_LID="${lid}" python3 -c "
import json, os, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import get_impl_noop_count

with open(os.environ['STATE_FILE']) as f:
    state = json.load(f)

value = get_impl_noop_count(state, os.environ['IMPL_NOOP_LID'])
try:
    print(int(value))
except (TypeError, ValueError):
    print(0)
" 2>/dev/null || echo "0"
}

# Increment the impl_noop_count for a local_id in the state file.
bump_impl_noop_count() {
  local lid="$1"
  STATE_FILE="${STATE_FILE}" IMPL_NOOP_LID="${lid}" python3 -c "
import json, os, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import increment_impl_noop_count

with open(os.environ['STATE_FILE']) as f:
    state = json.load(f)

increment_impl_noop_count(state, os.environ['IMPL_NOOP_LID'])

with open(os.environ['STATE_FILE'], 'w') as f:
    json.dump(state, f, indent=2)
" || true
}

MAX_IMPL_NOOP_REISSUES="${MAX_IMPL_NOOP_REISSUES:-2}"
if ! [[ "${MAX_IMPL_NOOP_REISSUES}" =~ ^[1-9][0-9]*$ ]]; then
  MAX_IMPL_NOOP_REISSUES=2
fi

# ---------------------------------------------------------------
# Helper: extract (RECOMMENDED) answers from clarification comments
# ---------------------------------------------------------------

# Fetches the latest clarification-questions comment for an issue and
# parses (RECOMMENDED) choices to build a "Q1: A\nQ2: B\n..." answer
# string.  Falls back to an empty string if no questions are found.
extract_recommended_answers() {
  local issue_num="$1"

  # Fetch recent comments (50 is the same limit used by clarify.yml)
  local comments_json
  comments_json="$(gh_retry gh api \
    "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=50" \
    2>/dev/null || echo "[]")"

  # Find the latest clarification comment (HTML marker or legacy prefix).
  # Search from newest to oldest (direction=desc).
  local clarify_body
  clarify_body="$(printf '%s' "${comments_json}" | jq -r '
    [ .[]
      | select(
          (.user.login // "" | test("\\[bot\\]$")) and
          ((.body // "") | test("<!-- ai:clarification-questions -->|^Clarification required"))
        )
    ]
    | if length > 0 then .[0].body // "" else "" end
  ')"

  if [ -z "${clarify_body}" ]; then
    echo ""
    return
  fi

  # Parse Q-blocks and pick the (RECOMMENDED) letter(s) for each.
  # When multiple options are recommended, combine with "+" (e.g. "A+C").
  # Expected format per question:
  #   **Q1: <question>**
  #   Choices:
  #   - **A** — <desc> (RECOMMENDED)
  #   - **B** — <desc>
  printf '%s' "${clarify_body}" | perl -ne '
    BEGIN { @order = (); %rec = (); $qid = undef; }
    if (/^\s*\*?\*?Q(\d+)/i) {
      # New question block — flush previous if it had recommendations
      if (defined $qid && exists $rec{$qid}) {
        push @order, $qid unless grep { $_ eq $qid } @order;
      }
      $qid = $1;
    }
    if (defined $qid && /^\s*-\s*\*\*([A-Z])\*\*\s*.*\(RECOMMENDED\)/i) {
      push @{$rec{$qid}}, $1;
    }
    END {
      # Flush the last question
      if (defined $qid && exists $rec{$qid}) {
        push @order, $qid unless grep { $_ eq $qid } @order;
      }
      for my $q (@order) {
        print "Q${q}: " . join("+", @{$rec{$q}}) . "\n";
      }
    }
  '
}

# ---------------------------------------------------------------
# Stall recovery: phase-specific healing actions
# ---------------------------------------------------------------

recover_stalled_issue() {
  local issue_num="$1"
  local phase="$2"
  local action="$3"
  local recovery_count="$4"
  local local_id="$5"
  local stall_minutes="$6"

  STALL_RECOVERY_SHOULD_INCREMENT="false"
  STALL_RECOVERY_EFFECTIVE_ACTION="${action}"

  echo "  [stall-recovery] Issue #${issue_num} stuck in '${phase}' for ${stall_minutes}m (attempt $((recovery_count + 1))). Action: ${action}"

  # ---- Guard: skip recovery for terminal reconciled state ----
  local _reconciled_status
  _reconciled_status="$(jq -r --arg lid "${local_id}" --argjson wi "${WAVE_IDX}" '.waves[$wi].issues[] | select(.id == $lid) | .status // empty' "${STATE_FILE}" 2>/dev/null | head -n1)"
  # Keep this terminal list aligned with scripts/orchestrate_lib.py:TERMINAL_WAVE_STATUSES.
  case "${_reconciled_status}" in
    merged|closed|skipped|not_created)
      echo "STALL_SKIP issue=${issue_num} reason=terminal_state status=${_reconciled_status} phase=${phase} action=${action}"
      add_healing_note "Issue #${issue_num}: skipped stall recovery (terminal state ${_reconciled_status})"
      return 1
      ;;
  esac

  # ---- Guard: skip recovery if the issue is already closed on GitHub ----
  # Defence-in-depth for the current poll cycle: even if the state file
  # hasn't been updated yet, don't post recovery comments on closed issues
  # (e.g. issues whose PRs were already merged).
  local _gh_issue_state
  _gh_issue_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.state' || echo "")"
  if [ "${_gh_issue_state}" = "closed" ]; then
    local _closed_labels
    _closed_labels="$(get_issue_labels_json "${issue_num}")"
    if ! has_label "${_closed_labels}" "ai:merged" && ! has_label "${_closed_labels}" "ai:closed"; then
      ensure_label_exists "ai:closed"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:closed" >/dev/null 2>&1 || true
      _closed_labels="$(echo "${_closed_labels}" | jq -c '. + ["ai:closed"] | unique' 2>/dev/null || echo "${_closed_labels}")"
      add_healing_note "Issue #${issue_num}: closed issue healed with ai:closed label"
      STALL_HEALING_CHANGED=true
    fi
    if [ -n "${local_id}" ] && [ "${local_id}" != "null" ]; then
      jq --arg lid "${local_id}" --argjson wi "${WAVE_IDX}" \
        '(.waves[$wi].issues[] | select(.id == $lid)).status = "closed"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      add_healing_note "Issue #${issue_num}: state reconciled to closed"
      STALL_HEALING_CHANGED=true
    fi
    echo "STALL_SKIP issue=${issue_num} reason=issue_closed_healed phase=${phase} action=${action}"
    return 1  # Signal: no action taken
  fi

  # ---- Guard: skip recovery if a *fresh* workflow is actively processing this issue ----
  if issue_has_active_workflow "${issue_num}"; then
    echo "STALL_SKIP issue=${issue_num} reason=active_workflow phase=${phase} action=${action}"
    return 1  # Signal: no action taken (caller should not increment counter)
  fi

  # Cancel any zombie runs for this issue before retrying.
  if [ "${action}" != "skip" ] && [ "${action}" != "attempt_merge" ] && [ "${action}" != "escalate_human" ]; then
    cancel_zombie_runs_for_issue "${issue_num}"
  fi

  if [ "${action}" = "run_stall_judge" ]; then
    invoke_stall_judge "${issue_num}" "${phase}" "${recovery_count}" "${stall_minutes}" "${local_id}"
    return $?
  fi

  execute_stall_recovery_action "${issue_num}" "${phase}" "${action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
  return $?
}

# ---------------------------------------------------------------
# Helper: Check if an active autofix run already exists for a PR branch
# ---------------------------------------------------------------
# Cycle-local dispatch tracker — prevents the same PR from being
# dispatched for conflict resolution more than once within a single
# orchestrate_poll execution.  File-based so it works across piped
# subshells (the while-read loops run in subshells where shell
# variable changes don't propagate back).
_CONFLICT_DISPATCH_TRACKER="${TMPDIR:-/tmp}/.conflict_dispatch_$$"
: > "${_CONFLICT_DISPATCH_TRACKER}"

# Queries the GitHub Actions API for in_progress or queued runs of
# review/autofix workflows on the given branch.  A new dispatch would
# cancel the existing run (cancel-in-progress concurrency) and trigger
# a spurious "cancelled/timed out" Telegram alert.
#
# Usage: _has_active_autofix_run <pr_number> <head_ref>
# Returns 0 if an active run exists (skip dispatch), 1 otherwise.
_has_active_autofix_run()
{
	local pr_number="$1"
	local head_ref="$2"
	local log_prefix="[conflict-dispatch] PR #${pr_number}"

	for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
		local active
		active="$(gh run list --repo "${GITHUB_REPOSITORY}" \
			--workflow "${wf_candidate}" \
			--branch "${head_ref}" \
			--limit 5 \
			--json status \
			--jq '[.[] | select(.status == "in_progress" or .status == "queued")] | length' \
			2>/dev/null || echo "0")"
		if [ "${active}" -gt 0 ]; then
			echo "  ${log_prefix} Active autofix run found (workflow=${wf_candidate}, count=${active}). Skipping dispatch."
			return 0
		fi
	done

	return 1
}

# Helper: Dispatch review workflow for merge conflict resolution
# ---------------------------------------------------------------
# Instead of resolving conflicts locally with Codex, dispatch the
# review_autofix workflow via workflow_dispatch.  The review workflow
# has its own Codex-based conflict resolver that runs on a dedicated
# runner with a clean checkout — more reliable than the shared
# orchestrator environment.
#
# workflow_dispatch resolves from the target ref (the PR branch),
# which always exists, bypassing the unbuildable merge-ref problem
# that affects pull_request synchronize events.
#
# Usage: _dispatch_review_for_conflicts <pr_number> <head_ref>
# Returns: 0 = dispatched, 1 = dispatch failed, 2 = skipped (active run exists).
_dispatch_review_for_conflicts()
{
	local pr_number="$1"
	local head_ref="$2"
	local log_prefix="[conflict-dispatch] PR #${pr_number}"

	# Guard 1: skip if already dispatched in this poll cycle.
	# Multiple code paths (ready-to-merge loop, in-progress loop,
	# standalone sweep) can encounter the same conflicted PR within a
	# single script execution.  The GitHub API has a visibility delay
	# for newly-dispatched runs, so _has_active_autofix_run may miss
	# them.  The cycle-local tracker catches this immediately.
	if grep -qx "${pr_number}" "${_CONFLICT_DISPATCH_TRACKER}" 2>/dev/null; then
		echo "  ${log_prefix} Already dispatched in this poll cycle. Skipping."
		return 2
	fi

	# Guard 2: skip dispatch if an autofix run is already active for this PR.
	# A new dispatch would cancel the running job (cancel-in-progress
	# concurrency) and fire a spurious "cancelled/timed out" alert.
	if _has_active_autofix_run "${pr_number}" "${head_ref}"; then
		return 2
	fi

	echo "  ${log_prefix} Dispatching review workflow for conflict resolution..."

	for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
		if gh workflow run "${wf_candidate}" \
			--repo "${GITHUB_REPOSITORY}" \
			--ref "${head_ref}" \
			-f pr_number="${pr_number}" 2>/dev/null; then
			echo "  ${log_prefix} Dispatched ${wf_candidate} on ${head_ref}."
			# Record in cycle-local tracker to prevent duplicate dispatches
			echo "${pr_number}" >> "${_CONFLICT_DISPATCH_TRACKER}"
			return 0
		fi
	done

	echo "::warning::${log_prefix} Could not dispatch review workflow via workflow_dispatch."
	return 1
}

# ---------------------------------------------------------------
# Normalize truthy env vars (case-insensitive 1/true/yes/on)
# ---------------------------------------------------------------
_is_truthy() {
  case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}
if _is_truthy "${ENABLE_VALIDATION:-true}"; then
  ENABLE_VALIDATION="true"
else
  ENABLE_VALIDATION="false"
fi

# Sanitize MAX_VALIDATE_CYCLES
if ! [[ "${MAX_VALIDATE_CYCLES:-3}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATE_CYCLES:-3}" -lt 1 ]; then
  MAX_VALIDATE_CYCLES="3"
fi

# ---------------------------------------------------------------
# Process each tracking issue
# ---------------------------------------------------------------
TRACKING_ISSUES="$(cat "${RUNTIME_DIR}/tracking_issues.json")"
COUNT="$(echo "${TRACKING_ISSUES}" | jq 'length')"

for ((tidx=0; tidx<COUNT; tidx++)); do
  TRACKING_NUM="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].number")"
  TRACKING_TITLE="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].title")"
  HEALING_NOTES=()
  echo "========================================"
  echo "Processing tracking issue #${TRACKING_NUM}: ${TRACKING_TITLE}"
  echo "========================================"

  # ---------------------------------------------------------------
  # Extract state from the tracking issue's comments
  # ---------------------------------------------------------------
  # Capture paginated comments to a temp file first, then validate the JSON
  # before combining pages.  Piping gh api --paginate directly to jq can
  # produce "Unfinished string at EOF" errors when the response is truncated
  # due to network interruptions.
  _comments_raw="$(mktemp "${TMPDIR:-/tmp}/comments_raw.XXXXXX")"
  COMMENTS='[]'
  if gh_retry_to_file "${_comments_raw}" gh api --paginate \
    "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments?per_page=100"; then
    _comments_merged="$(mktemp "${TMPDIR:-/tmp}/comments_merged.XXXXXX")"
    if jq -s 'add // []' "${_comments_raw}" > "${_comments_merged}" 2>/dev/null \
      && jq -e 'type == "array"' "${_comments_merged}" >/dev/null 2>&1; then
      COMMENTS="$(cat "${_comments_merged}")"
    else
      echo "::warning::Comments JSON for issue #${TRACKING_NUM} failed validation; proceeding with empty list" >&2
      echo "::group::Raw comments response (first 50 lines)" >&2
      head -50 "${_comments_raw}" >&2
      echo "::endgroup::" >&2
    fi
    rm -f "${_comments_merged}"
  fi
  rm -f "${_comments_raw}"

  STATE_JSON=""
  STATE_COMMENT_COUNT=0
  STATE_FALLBACK_USED="false"
  if extract_latest_valid_orchestrator_state "${COMMENTS}"; then
    STATE_JSON="${EXTRACTED_STATE_JSON}"
    STATE_COMMENT_COUNT="${EXTRACTED_STATE_COMMENT_COUNT}"
    STATE_FALLBACK_USED="${EXTRACTED_STATE_FALLBACK_USED}"
  else
    STATE_COMMENT_COUNT="${EXTRACTED_STATE_COMMENT_COUNT:-0}"
  fi

  if [ "${STATE_FALLBACK_USED}" = "true" ] && [ -n "${STATE_JSON}" ]; then
    printf '%s\n' "${STATE_JSON}" > "${STATE_FILE}"
    post_state_comment
    echo "::warning::Detected malformed latest ORCHESTRATOR_STATE_V1 for issue #${TRACKING_NUM}; restored from older valid state and posted healed canonical state."
  fi

  if [ -z "${STATE_JSON}" ] || [ "${STATE_JSON}" = "null" ]; then
    if [ "${STATE_COMMENT_COUNT}" -gt 0 ]; then
      echo "::warning::No valid ORCHESTRATOR_STATE_V1 comment found for tracking issue #${TRACKING_NUM}. Attempting state reconstruction..."
    else
      echo "::warning::No state found for tracking issue #${TRACKING_NUM}. Attempting state reconstruction..."
    fi

    # ---------------------------------------------------------------
    # State reconstruction: the orchestrate.yml workflow created the
    # tracking issue and child issues but failed before posting the
    # initial state comment.  Recover by parsing the tracking body
    # and searching for child issues that reference this tracker.
    # ---------------------------------------------------------------
    TRACKING_BODY="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" --jq '.body' || echo "")"
    if [ -z "${TRACKING_BODY}" ]; then
      echo "::warning::Could not fetch body for tracking issue #${TRACKING_NUM}, skipping."
      continue
    fi

    # Search for child issues whose body contains the tracking reference
    CHILD_ISSUES="$(gh api "search/issues" \
      -f q="repo:${GITHUB_REPOSITORY} \"Tracking issue: #${TRACKING_NUM}\" in:body" \
      --jq '.items // []' 2>/dev/null || echo '[]')"

    # Build issue_number_map from child issue bodies: extract Local ID metadata
    ISSUE_MAP_JSON="$(echo "${CHILD_ISSUES}" | jq '
      reduce .[] as $issue ({};
        ($issue.body | capture("Local ID: `(?<id>[^`]+)`") // null) as $cap |
        if $cap != null then
          . + {($cap.id): $issue.number}
        else . end
      )
    ' 2>/dev/null || echo '{}')"

    echo "  Discovered issue map: ${ISSUE_MAP_JSON}"

    # Write tracking body to temp file for the Python helper
    REBUILD_BODY_FILE="${RUNTIME_DIR}/rebuild_body_${TRACKING_NUM}.txt"
    printf '%s\n' "${TRACKING_BODY}" > "${REBUILD_BODY_FILE}"

    if python3 scripts/orchestrate_lib.py rebuild-state \
      --body-file "${REBUILD_BODY_FILE}" \
      --issue-map-json "${ISSUE_MAP_JSON}" \
      --tracking-issue "${TRACKING_NUM}" > "${STATE_FILE}" 2>/dev/null; then

      if [ -s "${STATE_FILE}" ] && jq -e '.schema_version' "${STATE_FILE}" >/dev/null 2>&1; then
        STATE_JSON="$(cat "${STATE_FILE}")"
        # Post the reconstructed state so future poll cycles find it
        post_state_comment
        echo "  State reconstructed and posted for tracking issue #${TRACKING_NUM}."
        tg_notify "Auto-recovery: rebuilt missing orchestrator state for tracking issue #${TRACKING_NUM}." "DEBUG"
      else
        echo "::warning::State reconstruction produced invalid output for #${TRACKING_NUM}, skipping."
        continue
      fi
    else
      echo "::warning::State reconstruction failed for tracking issue #${TRACKING_NUM}, skipping."
      continue
    fi
  fi

  if ! is_valid_orchestrator_state_json "${STATE_JSON}"; then
    echo "::warning::STATE_JSON for issue #${TRACKING_NUM} is not a valid orchestrator state object; skipping"
    continue
  fi

  echo "${STATE_JSON}" > "${STATE_FILE}"
  PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"

  DEFAULT_BRANCH_TRACKING="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  INTEGRATION_BRANCH_TRACKING="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  if [ -n "${INTEGRATION_BRANCH_TRACKING}" ] \
    && [ "${PROJECT_STATUS}" != "complete" ] \
    && [ "${PROJECT_STATUS}" != "failed" ] \
    && [ "${PROJECT_STATUS}" != "merge_conflict" ] \
    && [ "${PROJECT_STATUS}" != "validation-failed" ]; then
    if ! sync_default_into_integration_branch "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}"; then
      continue
    fi
    PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
    if [ "${PROJECT_STATUS}" = "failed" ]; then
      continue
    fi
  fi

  TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"

  if [ "${PROJECT_STATUS}" = "merge_conflict" ]; then
    FINAL_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
    FINAL_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
    FINAL_PROJECT_TITLE="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

    if ! finalize_integration_merge_if_needed "${FINAL_INTEGRATION_BRANCH}" "${FINAL_DEFAULT_BRANCH}" "${FINAL_PROJECT_TITLE}"; then
      continue
    fi

    if has_label "${TRACKING_LABELS}" "ai:validated"; then
      VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
      mark_validation_complete "${VALIDATION_CYCLE}"
      continue
    fi

    echo "Project complete!"
    jq '.status = "complete" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    set_tracking_phase_label "ai:merged"
    post_tracking_comment "Project completed successfully. Issue kept open for manual review."
    tg_cleanup_msgs "${TRACKING_NUM}"
    MSG="✅ Project #${TRACKING_NUM} completed successfully."
    MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
    if [ -n "${GITHUB_RUN_ID:-}" ]; then
      MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
    fi
    tg_send_msg "${MSG}" >/dev/null
    continue
  fi

  if [ "${PROJECT_STATUS}" = "validating" ] || [ "${PROJECT_STATUS}" = "validation-fixing" ]; then
    sync_validation_fix_issues_from_comments "${COMMENTS}"
    PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
    TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"
  fi

  if [ "${PROJECT_STATUS}" = "validating" ] || [ "${PROJECT_STATUS}" = "validation-fixing" ]; then
    VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
    if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
      VALIDATION_CYCLE="1"
    fi

    if has_label "${TRACKING_LABELS}" "ai:validation-failed"; then
      # Extract the detailed failure diagnosis from the most recent validation
      # comment posted by validate_process.sh (matches headings like
      # "Runtime validation failed", "Runtime validation harness error",
      # "Runtime validation infeasible", or "Runtime validation found fixable issues").
      VALIDATION_FAIL_BODY="$(echo "${COMMENTS}" | jq -r '
        [.[] | select(.body | test("## [❌🧪⚠️]+ Runtime validation"))] | last | .body // ""
      ')"
      if [ -n "${VALIDATION_FAIL_BODY}" ] && [ "${VALIDATION_FAIL_BODY}" != "" ]; then
        mark_validation_failed "${VALIDATION_FAIL_BODY}"
      else
        mark_validation_failed "Validation workflow reported failure (label ai:validation-failed)."
      fi
      continue
    fi

    if has_label "${TRACKING_LABELS}" "ai:validated"; then
      mark_validation_complete "${VALIDATION_CYCLE}"
      continue
    fi

    # Fallback: if the last validation workflow run completed successfully
    # and no ai:validation-failed label exists, treat as validated.
    # This handles the case where validate_process.sh completed but the
    # ai:validated label was lost or never persisted (silent gh API failure).
    if [ "${PROJECT_STATUS}" = "validating" ]; then
      LAST_VAL_CONCLUSION="$(get_last_validation_run_conclusion)"
      if [ "${LAST_VAL_CONCLUSION}" = "success" ]; then
        echo "Fallback: last validation run concluded success without ai:validated label. Applying label and marking complete."
        set_tracking_phase_label "ai:validated"
        post_tracking_comment "## ℹ️ Validation completion detected via workflow run fallback\n\nThe \`ai:validated\` label was missing but the last validation workflow run concluded successfully. Applying label and completing."
        mark_validation_complete "${VALIDATION_CYCLE}"
        continue
      fi
    fi

    if [ "${PROJECT_STATUS}" = "validating" ]; then
      if has_label "${TRACKING_LABELS}" "ai:validation-fixing"; then
        jq '.status = "validation-fixing"' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment
        continue
      fi

      if ! dispatch_validation_if_needed "${VALIDATION_CYCLE}"; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write. Error: ${VALIDATION_DISPATCH_ERROR:-unknown}"
      fi
      continue
    fi

    ACTIVE_FIX_ISSUES_JSON="$(jq -c '.validation_active_fix_issues // []' "${STATE_FILE}")"
    ACTIVE_FIX_COUNT="$(echo "${ACTIVE_FIX_ISSUES_JSON}" | jq 'length')"

    if [ "${ACTIVE_FIX_COUNT}" -le 0 ]; then
      echo "Validation is in fixing state but no active fix issues are tracked yet."
      continue
    fi

    FIX_ANY_CLOSED="false"
    FIX_ALL_MERGED="true"
    CLOSED_FIX_NUMS=""

    while IFS= read -r fix_num; do
      [ -n "${fix_num}" ] || continue
      FIX_LABELS="$(get_issue_labels_json "${fix_num}")"
      FIX_IS_MERGED="false"

      if has_label "${FIX_LABELS}" "ai:merged"; then
        FIX_IS_MERGED="true"
      else
        if validation_fix_issue_has_merged_pr_evidence "${fix_num}"; then
          echo "Validation fix-up issue #${fix_num}: merged PR detected; backfilling ai:merged."
          if backfill_validation_fix_issue_merged_label "${fix_num}"; then
            echo "Validation fix-up issue #${fix_num}: ai:merged label backfilled."
          else
            echo "::warning::Validation fix-up issue #${fix_num}: merged PR detected but ai:merged backfill failed." >&2
          fi
          FIX_IS_MERGED="true"
        else
          EVIDENCE_STATUS="$?"
          if [ "${EVIDENCE_STATUS}" -eq 1 ]; then
            echo "Validation fix-up issue #${fix_num}: no merged PR evidence detected."
          else
            echo "::warning::Validation fix-up issue #${fix_num}: merged PR lookup failed; leaving labels unchanged this cycle." >&2
          fi
        fi
      fi

      if has_label "${FIX_LABELS}" "ai:closed" && [ "${FIX_IS_MERGED}" != "true" ]; then
        FIX_ANY_CLOSED="true"
        CLOSED_FIX_NUMS="${CLOSED_FIX_NUMS} #${fix_num}"
      fi

      if [ "${FIX_IS_MERGED}" != "true" ]; then
        FIX_ALL_MERGED="false"
      fi
    done < <(echo "${ACTIVE_FIX_ISSUES_JSON}" | jq -r '.[]')

    if [ "${FIX_ANY_CLOSED}" = "true" ]; then
      mark_validation_failed "Validation fix-up issue(s) closed without merge:${CLOSED_FIX_NUMS}"
      continue
    fi

    if [ "${FIX_ALL_MERGED}" != "true" ]; then
      echo "Validation fix-up issues are still in progress."
      continue
    fi

    NEXT_VALIDATION_CYCLE=$(( VALIDATION_CYCLE + 1 ))
    if [ "${NEXT_VALIDATION_CYCLE}" -gt "${MAX_VALIDATE_CYCLES}" ]; then
      mark_validation_failed "Validation exceeded MAX_VALIDATE_CYCLES=${MAX_VALIDATE_CYCLES} without passing."
      continue
    fi

    tg_notify "Attempting to re-dispatch validation for project #${TRACKING_NUM} (cycle ${NEXT_VALIDATION_CYCLE}) after fix-up issues merged." "DEBUG"

    jq --argjson cycle "${NEXT_VALIDATION_CYCLE}" \
      '.status = "validating" |
       .validation_cycle = $cycle |
       .validation_active_fix_issues = []' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    set_tracking_phase_label "ai:validating"

    if ! dispatch_validation_if_needed "${NEXT_VALIDATION_CYCLE}"; then
      mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${NEXT_VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write. Error: ${VALIDATION_DISPATCH_ERROR:-unknown}"
    fi
    continue
  fi

  # ---------------------------------------------------------------
  # /revalidate — manual reset from validation-failed
  # ---------------------------------------------------------------
  # When a project is in terminal validation-failed state (status="failed"
  # with ai:validation-failed label), a /revalidate comment posted AFTER
  # the latest state comment resets counters and re-dispatches validation.
  if [ "${PROJECT_STATUS}" = "failed" ] && has_label "${TRACKING_LABELS}" "ai:validation-failed"; then
    REVALIDATE_REQUESTED="$(echo "${COMMENTS}" | jq -r '
      (to_entries | map(select(.value.body | contains("ORCHESTRATOR_STATE_V1"))) | last | .key // -1) as $last_state_idx |
      [to_entries[] | select(.key > $last_state_idx and (.value.body | test("^\\s*/revalidate(\\s|$)"; "m")))] | length > 0
    ')"

    if [ "${REVALIDATE_REQUESTED}" = "true" ]; then
      echo "  /revalidate requested for project #${TRACKING_NUM}. Resetting validation state."
      jq \
        '.status = "validating" |
         .validation_cycle = 1 |
         .validation_recovery_count = 0 |
         .validation_active_fix_issues = [] |
         .validation_last_dispatch_cycle = 0 |
         .validation_completed_cycle = null |
         del(.validation_failure_reason)' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      gh_retry gh issue edit "${TRACKING_NUM}" \
        --repo "${GITHUB_REPOSITORY}" \
        --remove-label "ai:validation-failed" >/dev/null || true
      set_tracking_phase_label "ai:validating"
      post_tracking_comment "## 🔁 Validation reset via /revalidate\n\nAll validation counters cleared. Re-dispatching validation (cycle 1)."
      tg_notify "/revalidate: project #${TRACKING_NUM} reset from validation-failed. Dispatching validation cycle 1." "DEBUG"
      if ! dispatch_validation_if_needed 1; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} after /revalidate reset. Error: ${VALIDATION_DISPATCH_ERROR:-unknown}"
      fi
      continue
    fi
  fi

  # ---------------------------------------------------------------
  # /judge_resume — manual reset from judge/recovery failure
  # ---------------------------------------------------------------
  # When a project is in terminal failed state (status="failed") but
  # NOT from validation (no ai:validation-failed label), a /judge_resume
  # comment posted AFTER the latest state comment resets judge stall
  # cycles and recovery counters, allowing the poller to resume.
  if [ "${PROJECT_STATUS}" = "failed" ] && ! has_label "${TRACKING_LABELS}" "ai:validation-failed"; then
    JUDGE_RESUME_REQUESTED="$(echo "${COMMENTS}" | jq -r '
      (to_entries | map(select(.value.body | contains("ORCHESTRATOR_STATE_V1"))) | last | .key // -1) as $last_state_idx |
      [to_entries[] | select(.key > $last_state_idx and (.value.body | test("^\\s*/judge_resume(\\s|$)"; "m")))] | length > 0
    ')"

    if [ "${JUDGE_RESUME_REQUESTED}" = "true" ]; then
      echo "  /judge_resume requested for project #${TRACKING_NUM}. Resetting judge and recovery counters."
      PREV_JUDGE_STALL="$(jq -r '.judge_stall_cycles // .judge_cycle' "${STATE_FILE}")"
      PREV_RECOVERY="$(jq -r '.recovery_count // 0' "${STATE_FILE}")"
      jq \
        '.status = "in_progress" |
         .judge_stall_cycles = 0 |
         .recovery_count = 0' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      post_tracking_comment "## ▶️ Project resumed via /judge_resume\n\nJudge stall cycles reset: ${PREV_JUDGE_STALL} → 0\nRecovery count reset: ${PREV_RECOVERY} → 0\nStatus: failed → in_progress\n\nThe poller will resume processing on the next cycle."
      tg_notify "/judge_resume: project #${TRACKING_NUM} resumed from failed state. Judge stall cycles ${PREV_JUDGE_STALL}→0, recovery ${PREV_RECOVERY}→0." "WARNING"
      # Fall through to normal processing below instead of continuing
      PROJECT_STATUS="in_progress"
    fi
  fi

  if [ "${PROJECT_STATUS}" = "complete" ] || [ "${PROJECT_STATUS}" = "failed" ] || [ "${PROJECT_STATUS}" = "validation-failed" ]; then
    echo "Project already ${PROJECT_STATUS}, skipping."
    continue
  fi

  CURRENT_WAVE="$(jq -r '.current_wave' "${STATE_FILE}")"
  TOTAL_WAVES="$(jq -r '.total_waves' "${STATE_FILE}")"
  JUDGE_CYCLE="$(jq -r '.judge_cycle' "${STATE_FILE}")"
  JUDGE_STALL_CYCLES="$(jq -r '.judge_stall_cycles // .judge_cycle' "${STATE_FILE}")"
  # Backward compat: read recovery_count (new) or migrate from recovery_attempted (old)
  RECOVERY_COUNT="$(jq -r '.recovery_count // (if .recovery_attempted == true then 1 else 0 end)' "${STATE_FILE}")"

  echo "Current wave: ${CURRENT_WAVE}/${TOTAL_WAVES}, Judge cycle: ${JUDGE_CYCLE} (stall: ${JUDGE_STALL_CYCLES}), Recovery count: ${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}"

  # ---------------------------------------------------------------
  # Backward scan: check prior waves for non-terminal issues
  # ---------------------------------------------------------------
  # Safety net: if a fix-up issue was added to a prior wave (or a
  # status update was missed), detect it here and update state /
  # attempt auto-merge so the issue doesn't stay orphaned forever.
  WAVE_IDX=$(( CURRENT_WAVE - 1 ))

  PRIOR_WAVE_REMEDIATED="false"
  if [ "${WAVE_IDX}" -gt 0 ]; then
    for prior_idx in $(seq 0 $(( WAVE_IDX - 1 ))); do
      PRIOR_NON_TERMINAL="$(jq -r --argjson wi "${prior_idx}" \
        '.waves[$wi].issues[] | select(.status != "merged" and .status != "closed" and .status != "skipped") | .github_issue' \
        "${STATE_FILE}" 2>/dev/null || echo "")"
      for pw_inum in ${PRIOR_NON_TERMINAL}; do
        [ -n "${pw_inum}" ] && [ "${pw_inum}" != "null" ] || continue
        echo "  [backward-scan] Prior wave $((prior_idx + 1)) issue #${pw_inum} is non-terminal. Checking labels..."
        PW_LABELS="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels" --jq '[.[].name]' || echo '[]')"
        [ -n "${PW_LABELS}" ] || PW_LABELS='[]'
        PW_LOCAL_ID="$(jq -r --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
          '.waves[$wi].issues[] | select((.github_issue | tostring) == $inum) | .id' "${STATE_FILE}" | head -n 1)"

        if echo "${PW_LABELS}" | jq -e 'index("ai:merged")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is now ai:merged. Updating state."
          jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
            '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "merged"' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          PRIOR_WAVE_REMEDIATED="true"
        elif echo "${PW_LABELS}" | jq -e 'index("ai:closed")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is now ai:closed. Updating state."
          jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
            '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "closed"' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          PRIOR_WAVE_REMEDIATED="true"
        elif echo "${PW_LABELS}" | jq -e 'index("ai:ready-to-merge")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is ai:ready-to-merge. Attempting auto-merge..."
          PW_PR="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/timeline" \
            --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
            || echo "")"
          if [[ "${PW_PR}" =~ ^[0-9]+$ ]]; then
            _pw_pr_json="$(_fetch_pr_json "${PW_PR}")"
            PW_PR_STATE="$(_jq_field "${_pw_pr_json}" '.state' 'open|closed|merged')"
            PW_PR_MERGEABLE="$(_jq_field "${_pw_pr_json}" '.mergeable' 'true|false')"
            _pw_head_sha="$(_jq_field "${_pw_pr_json}" '.head.sha')"
			if [ "${PW_PR_STATE}" = "open" ] && [ "${PW_PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${PW_PR}" "${_pw_head_sha}"; then
			  gh pr merge "${PW_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null \
			    || gh pr merge "${PW_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null || true
            elif [ "${PW_PR_STATE}" = "open" ] && [ "${PW_PR_MERGEABLE}" = "false" ]; then
              gh api "repos/${GITHUB_REPOSITORY}/pulls/${PW_PR}/update-branch" \
                -X PUT -f expected_head_sha="${_pw_head_sha}" \
                2>/dev/null || true
            fi
          fi
        else
          echo "  [backward-scan] #${pw_inum} (${PW_LOCAL_ID}) in prior wave $((prior_idx + 1)) still non-terminal. Will be caught by next poll cycle."
        fi
      done
    done
    if [ "${PRIOR_WAVE_REMEDIATED}" = "true" ]; then
      post_state_comment
    fi
  fi

  # ---------------------------------------------------------------
  # Collect label states for all child issues in the current wave
  # ---------------------------------------------------------------
  ISSUE_NUMS="$(jq -r ".waves[${WAVE_IDX}].issues[].github_issue" "${STATE_FILE}" 2>/dev/null || echo "")"

  # ---------------------------------------------------------------
  # Deferred issue creation for current wave: create GitHub issues
  # for any entries with github_issue == null that have pending
  # definitions.  This recovers from orchestrate.yml failing after
  # creating the tracking issue but before creating child issues.
  # ---------------------------------------------------------------
  UNCREATED_IDS="$(jq -r ".waves[${WAVE_IDX}].issues[] | select(.github_issue == null) | .id" "${STATE_FILE}" 2>/dev/null || true)"
  if [ -n "${UNCREATED_IDS}" ]; then
    echo "Detected uncreated issues in wave ${CURRENT_WAVE}. Creating GitHub issues..."
    DEFERRED_STATE_CHANGED=false
    DEFERRED_CREATED_NUMS=""
    while IFS= read -r local_id; do
      [ -n "${local_id}" ] || continue

      # Check if already in issue_number_map (created in a prior cycle but state not synced)
      EXISTING_NUM="$(jq -r ".issue_number_map[\"${local_id}\"] // empty" "${STATE_FILE}")"
      if [ -n "${EXISTING_NUM}" ]; then
        echo "  ${local_id}: already mapped to #${EXISTING_NUM}, updating wave entry."
        jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${EXISTING_NUM} | .status = \"pending\")" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        ISSUE_NUMS="${ISSUE_NUMS} ${EXISTING_NUM}"
        DEFERRED_CREATED_NUMS="${DEFERRED_CREATED_NUMS} ${EXISTING_NUM}"
        DEFERRED_STATE_CHANGED=true
        continue
      fi

      # Get issue definition from pending_issue_defs
      ISSUE_DEF="$(jq -c ".pending_issue_defs[\"${local_id}\"] // empty" "${STATE_FILE}")"
      if [ -z "${ISSUE_DEF}" ]; then
        echo "::warning::No pending definition for ${local_id} in wave ${CURRENT_WAVE}, skipping."
        continue
      fi

      DEF_TITLE="$(echo "${ISSUE_DEF}" | jq -r '.title')"
      DEF_BODY="$(echo "${ISSUE_DEF}" | jq -r '.body' | sed 's/\\n/\n/g')"
      DEF_PRIORITY="$(echo "${ISSUE_DEF}" | jq -r '.priority')"

      FULL_BODY="${DEF_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${local_id}\`
- Priority: ${DEF_PRIORITY}
- Managed by: AI Orchestrator"

      ensure_label_exists "ai:clarification"
      NEW_URL="$(gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${DEF_TITLE}" \
        --body "${FULL_BODY}" \
        --label "ai:clarification" 2>/dev/null || echo "")"

      if [ -z "${NEW_URL}" ]; then
        echo "::warning::Failed to create issue for ${local_id}; will retry next poll cycle."
        continue
      fi

      NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
      NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
      if ! [[ "${NEW_NUM}" =~ ^[0-9]+$ ]]; then
        echo "::warning::Could not parse issue number for ${local_id}; will retry next poll cycle."
        continue
      fi

      echo "  Created #${NEW_NUM}: ${DEF_TITLE} (${local_id})"
      ISSUE_NUMS="${ISSUE_NUMS} ${NEW_NUM}"
      DEFERRED_CREATED_NUMS="${DEFERRED_CREATED_NUMS} ${NEW_NUM}"

      # Update state: record the new issue number and remove from pending
      jq ".issue_number_map[\"${local_id}\"] = ${NEW_NUM} | del(.pending_issue_defs[\"${local_id}\"])" \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

      # Update the wave entry with the github issue number
      jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\" | .last_seen_phase = \"\" | .status_since_ts = $(date +%s) | .stall_recovery_count = 0)" \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

      DEFERRED_STATE_CHANGED=true
    done <<< "${UNCREATED_IDS}"

    if [ "${DEFERRED_STATE_CHANGED}" = "true" ]; then
      post_state_comment
      post_tracking_comment "## 🔧 Deferred Issue Creation (Wave ${CURRENT_WAVE})

Issues in this wave had not been created yet (likely from an interrupted initial setup). Created them now:

$(for inum in ${DEFERRED_CREATED_NUMS}; do
  [ -n "${inum}" ] && echo "- #${inum}"
done)

These issues will enter the AI pipeline (clarify → plan → implement → review)."
      tg_notify "Deferred issue creation for wave ${CURRENT_WAVE} of project #${TRACKING_NUM}. Created missing GitHub issues." "WARNING"
    fi
  fi

  # ---------------------------------------------------------------
  # Orphan sweep: find review-blocked issues that belong to this
  # project but are not tracked in the current wave.  The poller
  # only scans issues listed in the state file's current wave —
  # orphans would get stuck with ai:review-blocked forever without
  # this sweep re-injecting them.
  # ---------------------------------------------------------------
  ORPHAN_RB_JSON="[]"
  if ! ORPHAN_RB_JSON="$(gh_retry gh issue list --repo "${GITHUB_REPOSITORY}" \
    --label "ai:review-blocked" --state open \
    --json number,body --limit 50)"; then
    echo "::warning::Failed to list ai:review-blocked issues during orphan sweep; skipping orphan injection for this pass."
    ORPHAN_RB_JSON="[]"
  fi

  if [ -n "${ORPHAN_RB_JSON}" ] && [ "${ORPHAN_RB_JSON}" != "[]" ]; then
    ORPHAN_COUNT="$(printf '%s' "${ORPHAN_RB_JSON}" | jq -r 'if type=="array" then length else 0 end' 2>/dev/null || echo 0)"
    if ! [[ "${ORPHAN_COUNT}" =~ ^[0-9]+$ ]]; then
      echo "::warning::Orphan sweep received invalid issue array length '${ORPHAN_COUNT}'; skipping orphan injection for this pass." >&2
      ORPHAN_COUNT=0
    fi

    if [ "${ORPHAN_COUNT}" -gt 0 ]; then
      for oidx in $(seq 0 $(( ORPHAN_COUNT - 1 ))); do
        orphan_num="$(echo "${ORPHAN_RB_JSON}" | jq -r ".[${oidx}].number")"
        orphan_body="$(echo "${ORPHAN_RB_JSON}" | jq -r ".[${oidx}].body")"

        case "${orphan_num}" in
          ''|null|*[!0-9]*)
            echo "  [orphan-sweep] Skipping orphan entry at index ${oidx}: invalid issue number '${orphan_num}'." >&2
            continue
            ;;
        esac

        # Skip if already tracked in the current wave
        already_tracked="false"
        for inum in ${ISSUE_NUMS}; do
          if [ "${inum}" = "${orphan_num}" ]; then
            already_tracked="true"
            break
          fi
        done
        [ "${already_tracked}" = "false" ] || continue

        # Skip if not part of this project (body must reference our tracking issue)
        if ! printf '%s\n' "${orphan_body}" | grep -qE "^- Tracking issue: #${TRACKING_NUM}[[:space:]]*$"; then
          continue
        fi

        # Skip if not orchestrator-managed
        if ! printf '%s' "${orphan_body}" | grep -qF "Managed by: AI Orchestrator"; then
          continue
        fi

        echo "  [orphan-sweep] Injecting orphan review-blocked issue #${orphan_num} into wave ${CURRENT_WAVE}."

        # Inject into the state file's current wave
        if jq "(.waves[${WAVE_IDX}].issues) += [{\"id\": \"orphan-rb-${orphan_num}\", \"github_issue\": ${orphan_num}, \"status\": \"in_progress\"}]" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
          ISSUE_NUMS="${ISSUE_NUMS} ${orphan_num}"
        else
          echo "::warning::Orphan sweep failed to update state file for issue #${orphan_num}; skipping orphan injection for this pass." >&2
          rm -f "${STATE_FILE}.tmp"
        fi
      done
    fi
  fi

  if [ -z "${ISSUE_NUMS}" ]; then
    echo "::warning::No issues in wave ${CURRENT_WAVE}, skipping."
    continue
  fi

  # Batch-fetch labels for all wave issues in a single GraphQL query
  # instead of N individual REST calls (one per issue).
  _repo_owner="${GITHUB_REPOSITORY%%/*}"
  _repo_name="${GITHUB_REPOSITORY##*/}"
  _gql_fields=""
  _gql_issue_nums=()
  for inum in ${ISSUE_NUMS}; do
    [ -n "${inum}" ] && [ "${inum}" != "null" ] || continue
    _gql_fields+=" i${inum}: issue(number: ${inum}) { labels(first: 50) { nodes { name } } }"
    _gql_issue_nums+=("${inum}")
  done

  if [ "${#_gql_issue_nums[@]}" -gt 0 ]; then
    _gql_query="query { repository(owner: \"${_repo_owner}\", name: \"${_repo_name}\") {${_gql_fields} } }"
    _labels_result="$(gh api graphql -f query="${_gql_query}" 2>/dev/null || echo '{}')"
    LABELS_JSON="$(echo "${_labels_result}" | python3 -c "
import json, sys
raw = json.load(sys.stdin)
nums = $(printf '%s\n' "${_gql_issue_nums[@]}" | jq -R 'tonumber' | jq -s '.')
data = raw.get('data')
if not isinstance(data, dict):
    print('{}')
    sys.exit(0)
repo = data.get('repository')
if not isinstance(repo, dict):
    print('{}')
    sys.exit(0)
result = {}
for n in nums:
    key = 'i' + str(n)
    issue_data = repo.get(key)
    if not isinstance(issue_data, dict):
        print('{}')
        sys.exit(0)
    labels_data = issue_data.get('labels')
    if not isinstance(labels_data, dict):
        print('{}')
        sys.exit(0)
    nodes = labels_data.get('nodes')
    if not isinstance(nodes, list):
        print('{}')
        sys.exit(0)
    names = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get('name'), str):
            print('{}')
            sys.exit(0)
        names.append(node['name'])
    result[str(n)] = names
json.dump(result, sys.stdout)
" 2>/dev/null || echo '{}')"
  else
    LABELS_JSON="{}"
  fi

  # Fallback: if GraphQL failed, fall back to per-issue REST
  if [ "${LABELS_JSON}" = "{}" ] && [ "${#_gql_issue_nums[@]}" -gt 0 ]; then
    echo "  [labels] GraphQL batch failed, falling back to per-issue REST"
    LABELS_JSON="{"
    first=true
    for inum in "${_gql_issue_nums[@]}"; do
      LABELS="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels" --jq '[.[].name]' || echo '[]')"
      if [ "${first}" = true ]; then
        first=false
      else
        LABELS_JSON+=","
      fi
      LABELS_JSON+="\"${inum}\":${LABELS}"
    done
    LABELS_JSON+="}"
  fi

  # ---------------------------------------------------------------
  # Reconcile managed issue labels + truth signals before status/stall checks
  # ---------------------------------------------------------------
  ISSUE_STATES_JSON='{}'
  PR_STATES_JSON='{}'
  RECONCILE_LABELS_CHANGED=false
  for inum in ${ISSUE_NUMS}; do
    if [ -z "${inum}" ] || [ "${inum}" = "null" ]; then
      continue
    fi

    ISSUE_STATE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}" --jq '.state' | grep -xE 'open|closed' || echo "open")"
    ISSUE_STATES_JSON="$(echo "${ISSUE_STATES_JSON}" | jq -c --arg key "${inum}" --arg state "${ISSUE_STATE}" '. + {($key): $state}')"

    LINKED_PR_NUM="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last // empty' || echo "")"
    PR_STATE="unknown"
    PR_MERGED="false"
    if [[ "${LINKED_PR_NUM}" =~ ^[0-9]+$ ]]; then
      _linked_pr_json="$(_fetch_pr_json "${LINKED_PR_NUM}")"
      PR_STATE="$(_jq_field "${_linked_pr_json}" '.state' 'open|closed|merged')"
      PR_MERGED="$(_jq_field "${_linked_pr_json}" '.merged_at != null' 'true|false')"
      [ -n "${PR_MERGED}" ] || PR_MERGED="false"
    fi
    PR_STATES_JSON="$(echo "${PR_STATES_JSON}" | jq -c --arg key "${inum}" --arg state "${PR_STATE}" --arg merged "${PR_MERGED}" '. + {($key): {state: $state, merged: ($merged == "true")}}')"

    BEFORE_LABELS="$(echo "${LABELS_JSON}" | jq -c --arg key "${inum}" '.[$key] // []')"
    AFTER_LABELS="$(reconcile_managed_issue_labels "${inum}" "${BEFORE_LABELS}" "${ISSUE_STATE}" "${PR_STATE}" "${PR_MERGED}")"
    if [ "${BEFORE_LABELS}" != "${AFTER_LABELS}" ]; then
      RECONCILE_LABELS_CHANGED=true
      LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${inum}" --argjson labels "${AFTER_LABELS}" '. + {($key): $labels}')"
    fi
  done

  # ---------------------------------------------------------------
  # Update issue phase timestamps for stall tracking
  # ---------------------------------------------------------------
  STATE_HASH_BEFORE="$(sha256sum "${STATE_FILE}" 2>/dev/null | awk '{print $1}' || true)"
  python3 scripts/orchestrate_lib.py update-timestamps \
    --state-file "${STATE_FILE}" \
    --labels-json "${LABELS_JSON}" || true
  STATE_HASH_AFTER="$(sha256sum "${STATE_FILE}" 2>/dev/null | awk '{print $1}' || true)"
  TIMESTAMP_STATE_CHANGED="false"
  if [ "${STATE_HASH_BEFORE}" != "${STATE_HASH_AFTER}" ]; then
    TIMESTAMP_STATE_CHANGED="true"
  fi

  # ---------------------------------------------------------------
  # Check wave status
  # ---------------------------------------------------------------
  WAVE_STATUS="$(python3 scripts/orchestrate_lib.py check-wave-status \
    --state-file "${STATE_FILE}" \
    --labels-json "${LABELS_JSON}" \
    --issue-states-json "${ISSUE_STATES_JSON}" \
    --pr-states-json "${PR_STATES_JSON}")"

  echo "Wave status: ${WAVE_STATUS}"
  WAVE_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.wave_complete')"
  ANY_FAILED="$(echo "${WAVE_STATUS}" | jq -r '.any_failed')"
  PROJECT_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.project_complete')"

  # Persist reconciled status decisions every cycle (not only narrow branches).
  RECONCILE_STATE_CHANGED=false
  while IFS= read -r _ws_entry; do
    _ws_id="$(echo "${_ws_entry}" | jq -r '.id')"
    _ws_gh="$(echo "${_ws_entry}" | jq -r '.github_issue')"
    _ws_status="$(echo "${_ws_entry}" | jq -r '.status')"
    _ws_source="$(echo "${_ws_entry}" | jq -r '.decision_source // "unknown"')"
    [ -n "${_ws_id}" ] && [ "${_ws_id}" != "null" ] || continue
    _old_status="$(jq -r --arg lid "${_ws_id}" --argjson wi "${WAVE_IDX}" '.waves[$wi].issues[] | select(.id == $lid) | .status // ""' "${STATE_FILE}" | head -n1)"
    echo "STATE_RECONCILE issue=${_ws_gh} id=${_ws_id} old=${_old_status:-none} new=${_ws_status} source=${_ws_source}"
    if [ "${_old_status}" != "${_ws_status}" ]; then
      jq --arg lid "${_ws_id}" --arg st "${_ws_status}" --argjson wi "${WAVE_IDX}" \
        '(.waves[$wi].issues[] | select(.id == $lid)).status = $st' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      RECONCILE_STATE_CHANGED=true
      add_healing_note "Issue #${_ws_gh}: state ${_old_status:-pending} -> ${_ws_status} (${_ws_source})"
    fi
  done < <(echo "${WAVE_STATUS}" | jq -c '.issues[]')

  # ---------------------------------------------------------------
  # Auto-merge: merge PRs that are ready-to-merge
  # ---------------------------------------------------------------
  echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "ready-to-merge") | .github_issue' | while read -r rtm_issue; do
    [[ "${rtm_issue}" =~ ^[0-9]+$ ]] || continue
    echo "  Issue #${rtm_issue} is ready-to-merge, finding linked PR..."
    RTM_PR="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rtm_issue}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
      || echo "")"
    if [[ "${RTM_PR}" =~ ^[0-9]+$ ]]; then
      _rtm_pr_json="$(_fetch_pr_json "${RTM_PR}")"
      PR_STATE="$(_jq_field "${_rtm_pr_json}" '.state' 'open|closed|merged')"
      PR_MERGEABLE="$(_jq_field "${_rtm_pr_json}" '.mergeable' 'true|false')"
      _rtm_head_sha="$(_jq_field "${_rtm_pr_json}" '.head.sha')"
      if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
		if _pr_checks_completed "${RTM_PR}" "${_rtm_head_sha}"; then
		  echo "  Merging PR #${RTM_PR} (squash)..."
		  if gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
		    echo "  PR #${RTM_PR} merge initiated."
		  elif gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
		    echo "  PR #${RTM_PR} merged directly."
		  else
		    echo "::warning::Could not merge PR #${RTM_PR} for issue #${rtm_issue}. May need manual merge or branch protection prevents it."
		  fi
		fi
      elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
        echo "  PR #${RTM_PR} is not mergeable (mergeable=${PR_MERGEABLE}). Attempting branch update..."
        if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}/update-branch" \
          -X PUT -f expected_head_sha="${_rtm_head_sha}" \
          2>/dev/null; then
          echo "  PR #${RTM_PR} branch updated via API. The synchronize event will re-trigger review (including conflict resolution)."
        else
          echo "  API branch update failed for PR #${RTM_PR}. Dispatching review workflow for conflict resolution..."

          RTM_HEAD_REF="$(_jq_field "${_rtm_pr_json}" '.head.ref')"
          if [ -n "${RTM_HEAD_REF}" ] && [ "${RTM_HEAD_REF}" != "null" ]; then
            _dispatch_rc=0
            _dispatch_review_for_conflicts "${RTM_PR}" "${RTM_HEAD_REF}" || _dispatch_rc=$?
            if [ "${_dispatch_rc}" -eq 0 ]; then
              tg_notify "PR #${RTM_PR} (issue #${rtm_issue}) has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${RTM_PR}")"$'\n'"Issue: $(_gh_url "issues/${rtm_issue}")" "WARNING"
            elif [ "${_dispatch_rc}" -eq 2 ]; then
              echo "  PR #${RTM_PR}: autofix already in progress, skipping dispatch."
            else
              tg_notify "PR #${RTM_PR} (issue #${rtm_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${RTM_PR}")"$'\n'"Issue: $(_gh_url "issues/${rtm_issue}")" "WARNING"
            fi
          else
            echo "::warning::Could not determine head ref for PR #${RTM_PR}."
          fi
        fi
      else
        echo "  PR #${RTM_PR} state=${PR_STATE} mergeable=${PR_MERGEABLE}, skipping."
      fi
    else
      echo "  No linked PR found for issue #${rtm_issue}."
    fi
  done

  # ---------------------------------------------------------------
  # Auto-resolve merge conflicts on in-progress and done PRs
  # ---------------------------------------------------------------
  # When the base branch advances (e.g. another PR merges), existing
  # PRs may develop merge conflicts.  The review workflow already has
  # Codex-based conflict resolution, but it only runs on PR events
  # (opened/synchronize/reopened).  No event fires when the *base*
  # branch moves, so the review workflow is never re-triggered.
  #
  # This block detects issues whose linked PRs have become unmergeable
  # across two wave statuses:
  #   - in_progress: phases ai:implementing/validating/etc. where the
  #     PR is actively moving through the pipeline.
  #   - done: the ai:done phase where review has passed and the PR is
  #     waiting for promotion to ai:ready-to-merge.  Conflicts here
  #     would otherwise be silently picked up only by the stall
  #     detector's retrigger_review action (empty-commit nudge), which
  #     does not actually resolve conflicts.
  # First tries the GitHub API update-branch endpoint (handles clean
  # merges).  If that fails (real conflicts), dispatches the review
  # workflow via workflow_dispatch so it can resolve conflicts on a
  # dedicated runner with a clean environment.
  # ---------------------------------------------------------------
  echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "in_progress" or .status == "done") | .github_issue' | while read -r ip_issue; do
    [[ "${ip_issue}" =~ ^[0-9]+$ ]] || continue
    IP_PR="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${ip_issue}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
      || echo "")"
    if ! [[ "${IP_PR}" =~ ^[0-9]+$ ]]; then
      continue
    fi
    _ip_pr_json="$(_fetch_pr_json "${IP_PR}")"
    IP_PR_STATE="$(_jq_field "${_ip_pr_json}" '.state' 'open|closed|merged')"
    IP_MERGEABLE="$(_jq_field "${_ip_pr_json}" '.mergeable' 'true|false')"
    if [ "${IP_PR_STATE}" != "open" ] || [ "${IP_MERGEABLE}" != "false" ]; then
      continue
    fi
    echo "  Issue #${ip_issue} has PR #${IP_PR} with merge conflicts. Running Codex conflict resolution..."

    _ip_head_sha="$(_jq_field "${_ip_pr_json}" '.head.sha')"

    # Try the GitHub API update-branch first (creates a merge commit
    # if the merge is clean; fails when there are real conflicts).
    if gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}/update-branch" \
      -X PUT -f expected_head_sha="${_ip_head_sha}" \
      2>/dev/null; then
      echo "  PR #${IP_PR} branch updated via API. Synchronize event will re-trigger review."
      tg_notify "PR #${IP_PR} (issue #${ip_issue}) had merge conflicts. Branch updated via API to re-trigger review."$'\n'"PR: $(_gh_url "pull/${IP_PR}")"$'\n'"Issue: $(_gh_url "issues/${ip_issue}")" "WARNING"
      continue
    fi

    # API update failed — real conflicts exist.  Dispatch review workflow.
    IP_HEAD_REF="$(_jq_field "${_ip_pr_json}" '.head.ref')"
    if [ -n "${IP_HEAD_REF}" ] && [ "${IP_HEAD_REF}" != "null" ]; then
      _dispatch_rc=0
      _dispatch_review_for_conflicts "${IP_PR}" "${IP_HEAD_REF}" || _dispatch_rc=$?
      if [ "${_dispatch_rc}" -eq 0 ]; then
        tg_notify "PR #${IP_PR} (issue #${ip_issue}) has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${IP_PR}")"$'\n'"Issue: $(_gh_url "issues/${ip_issue}")" "WARNING"
      elif [ "${_dispatch_rc}" -eq 2 ]; then
        echo "  PR #${IP_PR}: autofix already in progress, skipping dispatch."
      else
        tg_notify "PR #${IP_PR} (issue #${ip_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${IP_PR}")"$'\n'"Issue: $(_gh_url "issues/${ip_issue}")" "WARNING"
      fi
    else
      echo "::warning::Could not determine head ref for PR #${IP_PR}."
    fi
  done

  # ---------------------------------------------------------------
  # Phase-agnostic orchestrator-PR sweep (Q6-B self-healing)
  # ---------------------------------------------------------------
  # The loop above only touches PRs whose tracking-state status is
  # "in_progress" and whose mergeable=false. A PR can still drift
  # behind `main` (mergeable_state="behind") without being marked
  # dirty, and it can belong to an issue that isn't currently
  # flagged in-progress (e.g. sitting at ai:review, ai:validating,
  # ai:implementation-failed). Without this sweep, such PRs rot
  # until they're finally labeled ready-to-merge, at which point the
  # update-branch call can fail because they've diverged too far.
  #
  # Strategy: enumerate all open PRs whose head branch matches the
  # orchestrator naming pattern `ai/issue-*`, and for any that are
  # `behind` but not currently `dirty` (conflicts are handled above),
  # call the GitHub update-branch endpoint to fast-forward them.
  # Real conflicts still fall through to the in-progress loop on the
  # next tick via the mergeable=false path.
  #
  # This pass is bounded per poll tick via --limit 100 and does not
  # dispatch review workflows; it only attempts update-branch.
  echo "  [feature-sweep] Scanning open ai/issue-* PRs for base-branch drift..."
  _FEATURE_SWEEP_JSON="$(gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --json number,headRefName,baseRefName,mergeable,mergeStateStatus \
    --limit 100 2>/dev/null || echo "[]")"
  if [ -n "${_FEATURE_SWEEP_JSON}" ] && [ "${_FEATURE_SWEEP_JSON}" != "[]" ]; then
    echo "${_FEATURE_SWEEP_JSON}" | jq -c '.[]' | while read -r _fs_pr; do
      _fs_num="$(echo "${_fs_pr}" | jq -r '.number // empty')"
      _fs_head="$(echo "${_fs_pr}" | jq -r '.headRefName // empty')"
      _fs_state="$(echo "${_fs_pr}" | jq -r '.mergeStateStatus // empty' | tr '[:upper:]' '[:lower:]')"
      _fs_mergeable="$(echo "${_fs_pr}" | jq -r '.mergeable // empty' | tr '[:upper:]' '[:lower:]')"
      [[ "${_fs_num}" =~ ^[0-9]+$ ]] || continue
      case "${_fs_head}" in
        ai/issue-*) ;;
        *) continue ;;
      esac
      # Skip dirty PRs — those go through the proper conflict loop
      # above so the resolver workflow can be dispatched.
      if [ "${_fs_state}" = "dirty" ] || [ "${_fs_mergeable}" = "false" ]; then
        continue
      fi
      # Only act on PRs that are actually behind base.
      if [ "${_fs_state}" != "behind" ]; then
        continue
      fi
      _fs_head_sha="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${_fs_num}" --jq '.head.sha' 2>/dev/null || echo "")"
      if [ -z "${_fs_head_sha}" ]; then
        echo "  [feature-sweep] PR #${_fs_num}: cannot resolve head sha; skipping."
        continue
      fi
      echo "  [feature-sweep] PR #${_fs_num} (${_fs_head}) is behind base; calling update-branch..."
      if gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${_fs_num}/update-branch" \
        -X PUT -f expected_head_sha="${_fs_head_sha}" >/dev/null 2>&1; then
        echo "  [feature-sweep] PR #${_fs_num} fast-forwarded."
      else
        echo "  [feature-sweep] PR #${_fs_num}: update-branch failed (likely real conflict). The in-progress conflict loop will handle it on the next tick."
      fi
    done
  fi

  # ---------------------------------------------------------------
  # Handle review-blocked issues: invoke judge to decide
  # ---------------------------------------------------------------
  ANY_REVIEW_BLOCKED="$(echo "${WAVE_STATUS}" | jq -r '.any_review_blocked')"
  if [ "${ANY_REVIEW_BLOCKED}" = "true" ]; then
    echo "Detected review-blocked issues in wave ${CURRENT_WAVE}. Invoking judge to unblock..."

    # Ensure codex config exists for the judge
    mkdir -p ~/.codex
    CATALOG_PATH="$(pwd)/scripts/codex_model_catalog.json"
    {
      echo 'web_search = "live"'
      echo 'model_provider = "openrouter"'
      echo "model = \"${MODEL_EDITOR}\""
      echo "model_reasoning_effort = \"${MODEL_REASONING_EFFORT_JUDGE}\""
      if [ -f "${CATALOG_PATH}" ]; then
        echo "model_catalog_json = \"${CATALOG_PATH}\""
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

    # Setup Serena for judge
    bash scripts/setup_serena.sh --mode editing --context codex || true

    MAX_REVIEW_BLOCKED_RETRIES="${MAX_REVIEW_BLOCKED_RETRIES:-2}"
    REVIEW_BLOCKED_STATE_CHANGED=false

    while read -r rb_issue; do
      [[ "${rb_issue}" =~ ^[0-9]+$ ]] || continue
      echo "  Processing review-blocked issue #${rb_issue}..."

      # Track retries per issue
      RETRY_COUNT="$(jq -r ".review_blocked_retries[\"${rb_issue}\"] // 0" "${STATE_FILE}")"
      echo "  Retry count for #${rb_issue}: ${RETRY_COUNT}/${MAX_REVIEW_BLOCKED_RETRIES}"

      # Find linked PR
      RB_PR="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rb_issue}/timeline" \
        --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
        || echo "")"
      if ! [[ "${RB_PR}" =~ ^[0-9]+$ ]]; then
        echo "  No linked PR found for issue #${rb_issue}, skipping."
        continue
      fi
      echo "  Linked PR: #${RB_PR}"

      # Guard: check PR state before invoking the judge
      # Fetch PR JSON once — reused for state/merged checks and PR_META below.
      _rb_pr_json="$(_fetch_pr_json "${RB_PR}")"
      RB_PR_STATE="$(_jq_field "${_rb_pr_json}" '.state' 'open|closed|merged')"
      RB_PR_MERGED="$(_jq_field "${_rb_pr_json}" '.merged_at != null' 'true|false')"
      [ -n "${RB_PR_MERGED}" ] || RB_PR_MERGED="false"
      if [ "${RB_PR_STATE}" != "open" ] && [ "${RB_PR_MERGED}" != "true" ]; then
        # PR is closed (not merged) — skip entirely
        echo "  PR #${RB_PR} is closed (not merged). Cleaning up labels and skipping."
        ensure_label_exists "ai:closed"
        gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:review-blocked' --remove-label 'ai:in-progress' \
          --add-label 'ai:closed' 2>/dev/null || true
        REVIEW_BLOCKED_STATE_CHANGED=true
        continue
      elif [ "${RB_PR_MERGED}" = "true" ]; then
        # PR was merged — judge should still run; fixes will go into a follow-up PR
        echo "  PR #${RB_PR} is already merged. Judge fixes will target a follow-up PR against ${DEFAULT_BRANCH:-main}."
      fi

      # Collect full PR context for the judge
      PR_DIFF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" \
        -H 'Accept: application/vnd.github.diff' 2>/dev/null || echo "(diff unavailable)")"
      PR_COMMENTS="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${RB_PR}/comments" \
        | jq -s 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}]' 2>/dev/null || echo "[]")"
      PR_REVIEW_COMMENTS="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/comments" \
        | jq -s 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}]' 2>/dev/null || echo "[]")"
      PR_META="$(echo "${_rb_pr_json}" | jq '{title: .title, body: .body, head_ref: .head.ref, base_ref: .base.ref, head_sha: .head.sha}' 2>/dev/null || echo "{}")"
      ISSUE_BODY="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rb_issue}" --jq '.body' || echo "")"

      # Determine if this is a final decision (retries exhausted) or a fix attempt
      IS_FINAL="false"
      if [ "${RETRY_COUNT}" -ge "${MAX_REVIEW_BLOCKED_RETRIES}" ]; then
        IS_FINAL="true"
        echo "  Retries exhausted — judge will make final decision (merge or close+reissue)."
      fi

      # Build the judge prompt for review-blocked evaluation
      RB_JUDGE_PROMPT_FILE="${RUNTIME_DIR}/rb_judge_prompt_${rb_issue}.txt"
      RB_JUDGE_OUTPUT_FILE="${RUNTIME_DIR}/rb_judge_output_${rb_issue}.txt"

      if [ ! -s "${RUNTIME_DIR}/judge_static.txt" ]; then
        assemble_judge_static_context "${RUNTIME_DIR}/judge_static.txt"
      fi

      {
        cat "${RUNTIME_DIR}/judge_static.txt"
        echo
        echo "=== REVIEW-BLOCKED JUDGE TASK ==="
        echo
        bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt
        echo
        echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
        echo
        echo "=== ISSUE #${rb_issue} (original requirement) ==="
        echo
        echo "${ISSUE_BODY}"
        echo
        echo "=== PR #${RB_PR} METADATA ==="
        echo
        echo "${PR_META}" | jq '.'
        echo
        echo "=== PR #${RB_PR} DIFF ==="
        echo
        head -1000 <<< "${PR_DIFF}"
        echo
        echo "=== PR #${RB_PR} COMMENTS (editor summaries, reviewer findings) ==="
        echo
        echo "${PR_COMMENTS}" | jq '.'
        echo
        echo "=== PR #${RB_PR} INLINE REVIEW COMMENTS ==="
        echo
        echo "${PR_REVIEW_COMMENTS}" | jq '.'
        echo
        echo "=== ORCHESTRATOR CONTEXT ==="
        echo "Review-blocked retry: $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}"
        echo "Retries exhausted: ${IS_FINAL}"
        if [ "${IS_FINAL}" = "true" ]; then
          echo
          echo "IMPORTANT: This is the FINAL attempt. You MUST choose either 'merge' or"
          echo "'close_and_reissue'. The 'fix' option is NOT available because previous"
          echo "fix attempts did not resolve the issues. Pick the action that best serves"
          echo "the project: merge if the PR is good enough, or close and reissue if the"
          echo "approach is fundamentally wrong."
        fi
      } > "${RB_JUDGE_PROMPT_FILE}"

      # Run the judge
      RB_JUDGE_SUCCESS=false
      for attempt in 1 2; do
        echo "  Review-blocked judge attempt ${attempt}/2..."
        cat "${RB_JUDGE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${RB_JUDGE_OUTPUT_FILE}" 2>/dev/null || true
        if grep -q '[^[:space:]]' "${RB_JUDGE_OUTPUT_FILE}"; then
          RB_JUDGE_SUCCESS=true
          break
        fi
        if [ "${attempt}" -lt 2 ]; then
          sleep 10
        fi
      done

      if [ "${RB_JUDGE_SUCCESS}" != "true" ]; then
        echo "::warning::Review-blocked judge failed for issue #${rb_issue}"
        tg_notify "Review-blocked judge failed for issue #${rb_issue} (PR #${RB_PR}). Will retry next poll cycle."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
        continue
      fi

      # Parse judge output
      RB_JUDGE_JSON="$(python3 -c "
import json, re, sys

raw = open('${RB_JUDGE_OUTPUT_FILE}', 'r').read()

try:
    data = json.loads(raw.strip())
    json.dump(data, sys.stdout)
    sys.exit(0)
except json.JSONDecodeError:
    pass

cleaned = re.sub(r'\`\`\`(?:json)?\s*', '', raw)
cleaned = re.sub(r'\`\`\`\s*$', '', cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for i, ch in enumerate(cleaned):
    if ch == '{':
        if brace_depth == 0:
            start = i
        brace_depth += 1
    elif ch == '}':
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            candidate = cleaned[start:i+1]
            try:
                data = json.loads(candidate)
                json.dump(data, sys.stdout)
                sys.exit(0)
            except json.JSONDecodeError:
                start = None

print('Could not parse review-blocked judge JSON', file=sys.stderr)
sys.exit(1)
" 2>/dev/null || echo "")"

      if [ -z "${RB_JUDGE_JSON}" ]; then
        echo "::warning::Could not parse review-blocked judge output for #${rb_issue}"
        continue
      fi

      RB_ACTION="$(echo "${RB_JUDGE_JSON}" | jq -r '.action')"
      RB_JUSTIFICATION="$(echo "${RB_JUDGE_JSON}" | jq -r '.justification // "no justification"')"
      RB_FIX_DESC="$(echo "${RB_JUDGE_JSON}" | jq -r '.fix_description // ""')"
      RB_REMAINING="$(echo "${RB_JUDGE_JSON}" | jq -r '.remaining_issues_summary // ""')"

      echo "  Judge decision for #${rb_issue}: ${RB_ACTION}"
      echo "  Justification: ${RB_JUSTIFICATION}"

      # Post judge assessment to PR
      RB_COMMENT="## Orchestrator Review-Blocked Judge — Issue #${rb_issue}

**Decision:** ${RB_ACTION}
**Retry:** $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}
**Justification:** ${RB_JUSTIFICATION}

**Remaining issues:** ${RB_REMAINING}"

      gh api "repos/${GITHUB_REPOSITORY}/issues/${RB_PR}/comments" \
        -f body="${RB_COMMENT}" >/dev/null 2>&1 || true

      case "${RB_ACTION}" in
        merge)
          echo "  Judge says merge PR #${RB_PR} as-is."
          # Remove review-blocked, set ready-to-merge
          ensure_label_exists "ai:ready-to-merge"
          gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true

          # Attempt squash merge (with branch update if needed)
          # Re-fetch PR data (state may have changed since the judge ran)
          RB_MERGED="false"
          _rb_merge_json="$(_fetch_pr_json "${RB_PR}")"
          PR_STATE="$(_jq_field "${_rb_merge_json}" '.state' 'open|closed|merged')"
          PR_MERGEABLE="$(_jq_field "${_rb_merge_json}" '.mergeable' 'true|false')"
          _rb_merge_sha="$(_jq_field "${_rb_merge_json}" '.head.sha')"
		  if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}" "${_rb_merge_sha}"; then
		    if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
		      echo "  PR #${RB_PR} merge initiated (auto)."
		      RB_MERGED="true"
		    elif gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
		      echo "  PR #${RB_PR} merged directly."
		      RB_MERGED="true"
		    else
		      echo "::warning::Could not merge PR #${RB_PR}."
		    fi
          elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
            echo "  PR #${RB_PR} is not mergeable. Attempting branch update..."
            if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
              -X PUT -f expected_head_sha="${_rb_merge_sha}" \
              2>/dev/null; then
              echo "  PR #${RB_PR} branch updated. Synchronize event will re-trigger review + conflict resolution."
            else
              echo "  API branch update failed for review-blocked PR #${RB_PR}. Dispatching review workflow for conflict resolution..."
              RB_HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
              if [ -n "${RB_HEAD_REF}" ] && [ "${RB_HEAD_REF}" != "null" ]; then
                _dispatch_rc=0
                _dispatch_review_for_conflicts "${RB_PR}" "${RB_HEAD_REF}" || _dispatch_rc=$?
                if [ "${_dispatch_rc}" -eq 0 ]; then
                  tg_notify "Review-blocked PR #${RB_PR} (issue #${rb_issue}) has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                elif [ "${_dispatch_rc}" -eq 2 ]; then
                  echo "  PR #${RB_PR}: autofix already in progress, skipping dispatch."
                else
                  tg_notify "Review-blocked PR #${RB_PR} (issue #${rb_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                fi
              else
                echo "::warning::Could not determine head ref for review-blocked PR #${RB_PR}."
              fi
            fi
          else
            echo "  PR #${RB_PR} state=${PR_STATE} mergeable=${PR_MERGEABLE}, cannot merge yet."
          fi

          REVIEW_BLOCKED_STATE_CHANGED=true
          if [ "${RB_MERGED}" = "true" ]; then
            tg_notify "Orchestrator judge merged review-blocked PR #${RB_PR} (issue #${rb_issue}): ${RB_JUSTIFICATION}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
          fi
          ;;

        fix)
          if [ "${IS_FINAL}" = "true" ]; then
            echo "  Judge returned 'fix' but retries exhausted — treating as merge."
            ensure_label_exists "ai:ready-to-merge"
            gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
              --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
            RB_FORCE_MERGED="false"
            _rb_fm_json="$(_fetch_pr_json "${RB_PR}")"
            PR_STATE="$(_jq_field "${_rb_fm_json}" '.state' 'open|closed|merged')"
            PR_MERGEABLE="$(_jq_field "${_rb_fm_json}" '.mergeable' 'true|false')"
            _rb_fm_sha="$(_jq_field "${_rb_fm_json}" '.head.sha')"
				if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}" "${_rb_fm_sha}"; then
				  if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto \
				    || gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
				    RB_FORCE_MERGED="true"
				  else
				    echo "::warning::Could not merge PR #${RB_PR} in force-merge path."
				  fi
            elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
              echo "  PR #${RB_PR} is not mergeable (force-merge path). Attempting branch update..."
              if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
                -X PUT -f expected_head_sha="${_rb_fm_sha}" \
                2>/dev/null; then
                echo "  PR #${RB_PR} branch updated. Will retry force-merge on next poll cycle."
              else
                echo "  API branch update failed for force-merge PR #${RB_PR}. Dispatching review workflow for conflict resolution..."
                RB_HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
                if [ -n "${RB_HEAD_REF}" ] && [ "${RB_HEAD_REF}" != "null" ]; then
                  _dispatch_rc=0
                  _dispatch_review_for_conflicts "${RB_PR}" "${RB_HEAD_REF}" || _dispatch_rc=$?
                  if [ "${_dispatch_rc}" -eq 0 ]; then
                    echo "  PR #${RB_PR} review workflow dispatched. Will retry force-merge on next poll cycle."
                  elif [ "${_dispatch_rc}" -eq 2 ]; then
                    echo "  PR #${RB_PR}: autofix already in progress, skipping dispatch. Will retry force-merge on next poll cycle."
                  else
                    tg_notify "Force-merge PR #${RB_PR} (issue #${rb_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                  fi
                else
                  echo "::warning::Could not determine head ref for force-merge PR #${RB_PR}."
                fi
              fi
            fi
            REVIEW_BLOCKED_STATE_CHANGED=true
            if [ "${RB_FORCE_MERGED}" = "true" ]; then
              tg_notify "Orchestrator force-merged review-blocked PR #${RB_PR} (retries exhausted, issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
            fi
          else
            echo "  Judge is applying fixes to PR #${RB_PR}..."
            # Re-check PR state before expensive fix+push (race condition safety net)
            _rb_recheck_json="$(_fetch_pr_json "${RB_PR}")"
            RB_PR_STATE_NOW="$(_jq_field "${_rb_recheck_json}" '.state' 'open|closed|merged')"
            RB_PR_MERGED_NOW="$(_jq_field "${_rb_recheck_json}" '.merged_at != null' 'true|false')"
            [ -n "${RB_PR_MERGED_NOW}" ] || RB_PR_MERGED_NOW="false"
            if [ "${RB_PR_STATE_NOW}" != "open" ] && [ "${RB_PR_MERGED_NOW}" != "true" ]; then
              # PR was closed without merge — skip
              echo "::warning::PR #${RB_PR} is closed (not merged). Skipping fix application."
              gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                --remove-label 'ai:review-blocked' 2>/dev/null || true
              REVIEW_BLOCKED_STATE_CHANGED=true
            else
            # Determine target: push to PR branch (open) or create follow-up PR (merged)
            RB_TARGET_MERGED="false"
            if [ "${RB_PR_MERGED}" = "true" ] || [ "${RB_PR_MERGED_NOW}" = "true" ]; then
              RB_TARGET_MERGED="true"
            fi

            HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
            BASE_REF="$(echo "${PR_META}" | jq -r '.base_ref')"
            : "${BASE_REF:=${DEFAULT_BRANCH:-main}}"

            ORCH_FOLLOWUP_OWNED="false"
            ORCH_FOLLOWUP_TRACKING_NUM=""
            ORCH_FOLLOWUP_INTEGRATION_BRANCH=""
            ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS="false"
            FOLLOWUP_PR_BLOCKED="false"

            if [ "${RB_TARGET_MERGED}" = "true" ]; then
              resolve_active_orchestrator_context_for_issue "${rb_issue}" "${TRACKING_NUM:-}"
              ORCH_FOLLOWUP_OWNED="${RESOLVED_ORCHESTRATOR_OWNED}"
              ORCH_FOLLOWUP_TRACKING_NUM="${RESOLVED_TRACKING_ISSUE}"
              ORCH_FOLLOWUP_INTEGRATION_BRANCH="${RESOLVED_INTEGRATION_BRANCH}"
              ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS="${RESOLVED_INTEGRATION_BRANCH_EXISTS}"

              if [ "${ORCH_FOLLOWUP_OWNED}" = "true" ]; then
                if [ "${ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS}" = "true" ] && [ -n "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                  BASE_REF="${ORCH_FOLLOWUP_INTEGRATION_BRANCH}"
                  echo "  Follow-up PR for issue #${rb_issue} is orchestrator-managed (tracking #${ORCH_FOLLOWUP_TRACKING_NUM}). Retargeting base to ${BASE_REF}."
                else
                  FOLLOWUP_PR_BLOCKED="true"
                  FOLLOWUP_BLOCK_REASON="Issue #${rb_issue} is orchestrator-managed (tracking #${ORCH_FOLLOWUP_TRACKING_NUM}), but integration branch '${ORCH_FOLLOWUP_INTEGRATION_BRANCH:-<missing>}' is unavailable. Aborting follow-up PR creation to avoid targeting ${DEFAULT_BRANCH:-main}."
                  echo "::warning::${FOLLOWUP_BLOCK_REASON}"
                  ORIGINAL_TRACKING_NUM="${TRACKING_NUM:-}"
                  if [ -n "${ORCH_FOLLOWUP_TRACKING_NUM:-}" ]; then
                    TRACKING_NUM="${ORCH_FOLLOWUP_TRACKING_NUM}"
                  fi
                  post_tracking_comment "## ⚠️ Follow-up PR blocked\n\n${FOLLOWUP_BLOCK_REASON}"
                  tg_notify "${FOLLOWUP_BLOCK_REASON}" "WARNING"
                  TRACKING_NUM="${ORIGINAL_TRACKING_NUM}"
                fi
              fi
            fi

            if [ "${RB_TARGET_MERGED}" = "true" ] && [ "${FOLLOWUP_PR_BLOCKED}" != "true" ]; then
              # PR already merged — work on a follow-up branch from the base branch
              FOLLOWUP_BRANCH="fix/${rb_issue}-followup-$(date +%s)"
              echo "  PR already merged. Creating follow-up branch ${FOLLOWUP_BRANCH} from ${BASE_REF}."
              git fetch --no-tags origin "refs/heads/${BASE_REF}:refs/remotes/origin/${BASE_REF}" 2>/dev/null || true
              git checkout -B "${FOLLOWUP_BRANCH}" "refs/remotes/origin/${BASE_REF}" 2>/dev/null || true
            elif [ "${RB_TARGET_MERGED}" = "true" ]; then
              echo "::warning::Skipping follow-up branch creation for issue #${rb_issue}; follow-up PR creation is blocked."
            elif [ -n "${HEAD_REF}" ] && [ "${HEAD_REF}" != "null" ]; then
              # PR is open — push to existing PR branch
              git fetch --no-tags origin "refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}" 2>/dev/null || true
              git checkout -B "${HEAD_REF}" "refs/remotes/origin/${HEAD_REF}" 2>/dev/null || true
            else
              echo "::warning::Cannot determine PR head branch for #${RB_PR}."
              git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
              REVIEW_BLOCKED_STATE_CHANGED=true
              # Skip to retry counter via nested-fi + fi
            fi

            if { [ "${RB_TARGET_MERGED}" = "true" ] && [ "${FOLLOWUP_PR_BLOCKED}" != "true" ]; } || { [ "${RB_TARGET_MERGED}" != "true" ] && [ -n "${HEAD_REF}" ] && [ "${HEAD_REF}" != "null" ]; }; then
              # Re-run the judge in editing mode on the target branch
              RB_FIX_PROMPT_FILE="${RUNTIME_DIR}/rb_fix_prompt_${rb_issue}.txt"
              RB_FIX_OUTPUT_FILE="${RUNTIME_DIR}/rb_fix_output_${rb_issue}.txt"
              {
                cat "${RB_JUDGE_PROMPT_FILE}"
                echo
                echo "=== APPLY FIXES NOW ==="
                if [ "${RB_TARGET_MERGED}" = "true" ]; then
                  echo "You are on a follow-up branch based on ${BASE_REF}."
                  echo "The original PR #${RB_PR} was already merged."
                  echo "Apply only the fixes identified during review — do not re-apply the original PR's changes."
                else
                  echo "You are now on the PR branch (${HEAD_REF})."
                fi
                echo "Apply the fixes you identified directly to the repository files."
                echo "Focus only on the issues that blocked the review."
                echo "Do not create new files unless absolutely required."
                echo "After applying fixes, output the same JSON with action='fix' and"
                echo "fix_description describing what you changed."
              } > "${RB_FIX_PROMPT_FILE}"

              if cat "${RB_FIX_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${RB_FIX_OUTPUT_FILE}" 2>/dev/null; then
                echo "  Fix codex completed."
              else
                echo "::warning::Fix codex failed for PR #${RB_PR}."
              fi

              # Remove workflow-generated/fetched artifacts so they are never
              # committed to caller repos.
              #
              # Gate on the git origin URL rather than ${GITHUB_REPOSITORY}:
              # the env var is user-controllable and any test harness that
              # sets e.g. GITHUB_REPOSITORY=owner/repo while running this
              # poller as a subprocess from the real coding-workflows
              # checkout would trip this block and rm the tracked source
              # files under that checkout (see PRs #917/#931 for the
              # incident). The remote URL reflects the actual checkout on
              # disk, not a user-overridable env var. Unknown/empty URL is
              # fail-closed: skip cleanup (strictly safer — at worst a
              # commit carries a few extra untracked fetched files that
              # downstream path excludes already block from staging).
              _orig_origin_url="$(git config --get remote.origin.url 2>/dev/null || true)"
              case "${_orig_origin_url}" in
                ""|*/coding-workflows|*/coding-workflows.git|*/coding-workflows/|*/coding-workflows.git/)
                  : # self-repo or unknown — keep files; consumer-repo-only cleanup
                  ;;
                *)
                  rm -f ./pre_assembled_static.txt
                  rm -f codex_system_instructions.md ai_pipeline.md unattended_llm_system_instructions.md agents.md
                  rm -f scripts/setup_serena.sh scripts/git_ref_health_check.sh scripts/serena_efficiency_report.py \
                    scripts/generate_symbol_diff_summary.py scripts/label_helpers.sh scripts/tg_helpers.sh \
                    scripts/codex_model_catalog.json
                  rm -rf .serena
                  rm -f .github/ai/orchestrate_schema.v1.json
                  ;;
              esac
              unset _orig_origin_url

              # Check if there are changes to commit
              if [ -n "$(git status --porcelain)" ]; then
                git config user.name "codex-bot"
                git config user.email "codex@users.noreply.github.com"
                if [ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ]; then
                  git add -u -- ':!node_modules' ':!.serena' ':!.github/prompts' ':!.github/scripts'
                  git ls-files --others --exclude-standard -z -- ':!node_modules' ':!.serena' ':!.github/prompts' ':!.github/scripts' | xargs -0 -r git add --
                else
                  git add -u -- ':!node_modules' ':!scripts' ':!prompts' ':!.github/ai' ':!.serena' ':!.github/prompts' ':!.github/scripts'
                  git ls-files --others --exclude-standard -z -- ':!node_modules' ':!.serena' ':!scripts' ':!prompts' ':!.github/ai' ':!.github/prompts' ':!.github/scripts' | xargs -0 -r git add --
                fi
                echo "Staged files before commit:"
                git diff --cached --name-only | sed 's/^/ - /' || true
                if git diff --cached --name-only | grep -E '^\.github/(prompts|scripts)/'; then
                  echo "Error: .github/prompts or .github/scripts is staged"
                  exit 1
                fi
                git commit -m "[orchestrator-fix] address review-blocked issues for #${rb_issue}

Orchestrator judge applied fixes to unblock the review pipeline.
Retry $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}.

${RB_FIX_DESC}" || true

                git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}"

                if [ "${RB_TARGET_MERGED}" = "true" ]; then
                  # Push follow-up branch and create a new PR
                  if git push origin "HEAD:${FOLLOWUP_BRANCH}" 2>/dev/null; then
                    echo "  Pushed follow-up branch ${FOLLOWUP_BRANCH}."

                    if [ "${ORCH_FOLLOWUP_OWNED}" = "true" ] && [ "${ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS}" = "true" ] && [ -n "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                      if [ "${BASE_REF}" != "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                        echo "::warning::Detected follow-up PR base '${BASE_REF}' for orchestrator-owned issue #${rb_issue}; retargeting to '${ORCH_FOLLOWUP_INTEGRATION_BRANCH}'."
                        BASE_REF="${ORCH_FOLLOWUP_INTEGRATION_BRANCH}"
                      fi
                    fi

                    if [ "${ORCH_FOLLOWUP_OWNED}" = "true" ] && [ "${BASE_REF}" = "${DEFAULT_BRANCH:-main}" ]; then
                      FOLLOWUP_GUARD_REASON="Issue #${rb_issue} is orchestrator-managed (tracking #${ORCH_FOLLOWUP_TRACKING_NUM}); refusing to create follow-up PR against '${BASE_REF}'. Required base is '${ORCH_FOLLOWUP_INTEGRATION_BRANCH:-<missing>}'."
                      echo "::warning::${FOLLOWUP_GUARD_REASON}"
                      ORIGINAL_TRACKING_NUM="${TRACKING_NUM:-}"
                      if [ -n "${ORCH_FOLLOWUP_TRACKING_NUM:-}" ]; then
                        TRACKING_NUM="${ORCH_FOLLOWUP_TRACKING_NUM}"
                      fi
                      post_tracking_comment "## ⚠️ Follow-up PR blocked\n\n${FOLLOWUP_GUARD_REASON}"
                      tg_notify "${FOLLOWUP_GUARD_REASON}" "WARNING"
                      TRACKING_NUM="${ORIGINAL_TRACKING_NUM}"
                      FOLLOWUP_PR_URL=""
                    else
                      FOLLOWUP_PR_URL="$(gh pr create \
                      --repo "${GITHUB_REPOSITORY}" \
                      --base "${BASE_REF}" \
                      --head "${FOLLOWUP_BRANCH}" \
                      --title "[orchestrator-fix] follow-up fixes for #${rb_issue}" \
                      --body "Follow-up fixes for issues identified during review of PR #${RB_PR} (already merged).

Closes #${rb_issue}

**Original issue:** #${rb_issue}
**Original PR:** #${RB_PR}

${RB_FIX_DESC}

---
*Created automatically by the orchestrator judge.*" 2>/dev/null || echo "")"
                    fi
                    if [ -n "${FOLLOWUP_PR_URL}" ]; then
                      echo "  Created follow-up PR: ${FOLLOWUP_PR_URL}"
                      gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                        --remove-label 'ai:review-blocked' 2>/dev/null || true
                      tg_notify "Orchestrator judge created follow-up PR for merged PR #${RB_PR} (issue #${rb_issue}): ${FOLLOWUP_PR_URL}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                    else
                      echo "::warning::Failed to create follow-up PR for merged PR #${RB_PR}."
                    fi
                  else
                    echo "::warning::Failed to push follow-up branch ${FOLLOWUP_BRANCH}."
                  fi
                else
                  # Push to existing open PR branch
                  if git push origin "HEAD:${HEAD_REF}" 2>/dev/null; then
                    echo "  Pushed [orchestrator-fix] commit to ${HEAD_REF}."
                    # Remove review-blocked label — the push triggers synchronize
                    # which re-runs review_autofix with a reset autofix counter.
                    gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                      --remove-label 'ai:review-blocked' 2>/dev/null || true
                    tg_notify "Orchestrator judge pushed fix for review-blocked PR #${RB_PR} (issue #${rb_issue}, retry $((RETRY_COUNT + 1))/${MAX_REVIEW_BLOCKED_RETRIES})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                  else
                    echo "::warning::Failed to push orchestrator fix for PR #${RB_PR}."
                  fi
                fi
              else
                echo "  Judge produced no file changes."
                if [ "${RB_TARGET_MERGED}" = "true" ]; then
                  echo "  No follow-up needed — merged PR has no outstanding fixes."
                  gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' 2>/dev/null || true
                  tg_notify "Orchestrator judge found no fixes needed for merged PR #${RB_PR} (issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                else
                  echo "  Treating as merge decision."
                  ensure_label_exists "ai:ready-to-merge"
                  gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
                  _rb_nofix_json="$(_fetch_pr_json "${RB_PR}")"
                  PR_STATE="$(_jq_field "${_rb_nofix_json}" '.state' 'open|closed|merged')"
                  PR_MERGEABLE="$(_jq_field "${_rb_nofix_json}" '.mergeable' 'true|false')"
                  _rb_nofix_sha="$(_jq_field "${_rb_nofix_json}" '.head.sha')"
                  if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}" "${_rb_nofix_sha}"; then
                    if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto \
                      || gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
                      tg_notify "Orchestrator judge merged PR #${RB_PR} (no fix changes needed, issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                    else
                      echo "::warning::Could not merge PR #${RB_PR} in no-fix merge path."
                    fi
                  elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
                    echo "  PR #${RB_PR} is not mergeable in no-fix merge path. Skipping merge notification."
                  fi
                fi
              fi

              # Switch back to default branch for remaining processing
              git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
            fi

            # Increment retry counter
            jq ".review_blocked_retries[\"${rb_issue}\"] = $((RETRY_COUNT + 1))" \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
            REVIEW_BLOCKED_STATE_CHANGED=true
          fi
          fi
          ;;

        close_and_reissue)
          echo "  Judge says close PR #${RB_PR} and reissue."
          # Close the PR
          gh pr close "${RB_PR}" --repo "${GITHUB_REPOSITORY}" \
            --comment "Closed by orchestrator judge — the approach needs rework. A new issue will be created with refined guidance." \
            2>/dev/null || true

          # Label issue as closed
          ensure_label_exists "ai:closed"
          gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:review-blocked' --remove-label 'ai:done' \
            --add-label 'ai:closed' 2>/dev/null || true

          # Create replacement issue
          NEW_ISSUE_TITLE="$(echo "${RB_JUDGE_JSON}" | jq -r '.new_issue.title // empty')"
          NEW_ISSUE_BODY="$(echo "${RB_JUDGE_JSON}" | jq -r '.new_issue.body // empty' | sed 's/\\n/\n/g')"
          if [ -n "${NEW_ISSUE_TITLE}" ] && [ -n "${NEW_ISSUE_BODY}" ]; then
            RB_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
            FULL_NEW_BODY="${NEW_ISSUE_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: ${RB_INTEGRATION_BRANCH}
- Replaces: #${rb_issue} (PR #${RB_PR} closed — approach rework)
- Type: review-blocked-reissue
- Managed by: AI Orchestrator"

            ensure_label_exists "ai:clarification"
            NEW_URL="$(gh issue create \
              --repo "${GITHUB_REPOSITORY}" \
              --title "${NEW_ISSUE_TITLE}" \
              --body "${FULL_NEW_BODY}" \
              --label "ai:clarification")"
            NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
            NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
            echo "  Created replacement issue #${NEW_NUM}: ${NEW_ISSUE_TITLE}"

            # Get local_id for the blocked issue and remap it
            LOCAL_ID="$(echo "${WAVE_STATUS}" | jq -r ".issues[] | select(.github_issue == \"${rb_issue}\") | .id")"
            if [[ "${NEW_NUM}" =~ ^[0-9]+$ ]] && [ -n "${LOCAL_ID}" ] && [ "${LOCAL_ID}" != "null" ]; then
              jq ".issue_number_map[\"${LOCAL_ID}\"] = ${NEW_NUM}" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
              # Update the wave entry
              jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${LOCAL_ID}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\")" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
            fi

            tg_notify "Orchestrator closed PR #${RB_PR} and reissued as #${NEW_NUM} (issue #${rb_issue}): ${RB_JUSTIFICATION}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"New issue: $(_gh_url "issues/${NEW_NUM}")"$'\n'"Old issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
          else
            echo "::warning::Judge chose close_and_reissue but provided no new issue details."
            tg_notify "Orchestrator closed PR #${RB_PR} (issue #${rb_issue}) but could not create replacement issue."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
          fi

          REVIEW_BLOCKED_STATE_CHANGED=true
          ;;

        *)
          echo "::warning::Unknown review-blocked judge action: ${RB_ACTION}"
          ;;
      esac
    done < <(echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "review-blocked") | .github_issue')

    # Persist updated state if any review-blocked issues were handled
    if [ "${REVIEW_BLOCKED_STATE_CHANGED}" = "true" ]; then
      post_state_comment

      # Re-check wave status after handling review-blocked issues
      # (some may have been merged or reissued)
      echo "Re-checking wave status after review-blocked handling..."
      LABELS_JSON="{"
      first=true
      for inum in ${ISSUE_NUMS}; do
        if [ -z "${inum}" ] || [ "${inum}" = "null" ]; then
          continue
        fi
        # Re-read labels since we may have changed them
        LABELS="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels" --jq '[.[].name]' || echo '[]')"
        if [ "${first}" = true ]; then
          first=false
        else
          LABELS_JSON+=","
        fi
        LABELS_JSON+="\"${inum}\":${LABELS}"
      done
      LABELS_JSON+="}"

      # Also check any reissued issue numbers
      REISSUED_NUMS="$(jq -r '.waves['"${WAVE_IDX}"'].issues[].github_issue' "${STATE_FILE}" 2>/dev/null | sort -u)"
      for rnum in ${REISSUED_NUMS}; do
        if [ -z "${rnum}" ] || [ "${rnum}" = "null" ]; then continue; fi
        if echo "${LABELS_JSON}" | jq -e --arg key "${rnum}" 'has($key)' >/dev/null 2>&1; then
          continue
        fi
        LABELS="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rnum}/labels" --jq '[.[].name]' || echo '[]')"
        [ -z "${LABELS}" ] && LABELS='[]'
        LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${rnum}" --argjson labels "${LABELS}" '. + {($key): $labels}')"
      done

      WAVE_STATUS="$(python3 scripts/orchestrate_lib.py check-wave-status \
        --state-file "${STATE_FILE}" \
        --labels-json "${LABELS_JSON}")"
      WAVE_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.wave_complete')"
      ANY_FAILED="$(echo "${WAVE_STATUS}" | jq -r '.any_failed')"
      echo "Updated wave status after review-blocked handling: complete=${WAVE_COMPLETE}, failed=${ANY_FAILED}"
    fi
  fi

  # ---------------------------------------------------------------
  # Handle implementation-failed issues: close and re-issue
  # ---------------------------------------------------------------
  IMPL_FAILED_STATE_CHANGED=false
  while read -r if_issue; do
    [[ "${if_issue}" =~ ^[0-9]+$ ]] || continue

    # Look up local_id for this issue so we can track no-op count
    IF_LOCAL_ID="$(jq -r --arg if_issue "${if_issue}" --argjson wave_idx "${WAVE_IDX}" \
      '.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue) | .id' \
      "${STATE_FILE}" | head -n 1)"

    # ---- No-op loop guard: if this task has already been re-issued
    # MAX_IMPL_NOOP_REISSUES times without producing changes, the code
    # likely already exists on main.  Close the issue and let the
    # wave-completion judge verify instead of looping forever.
    NOOP_COUNT="$(get_impl_noop_count "${IF_LOCAL_ID}")"
    # The current failure is itself a no-op, so the observed count
    # includes this cycle even though we haven't bumped yet.
    OBSERVED_NOOP_COUNT=$((NOOP_COUNT + 1))
    # Cap semantics: MAX_IMPL_NOOP_REISSUES controls how many re-issues
    # are allowed after prior no-op failures.
    if [ "${NOOP_COUNT}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
      echo "  Issue #${if_issue} (${IF_LOCAL_ID}) hit implementation no-op cap (${OBSERVED_NOOP_COUNT}/${MAX_IMPL_NOOP_REISSUES}). Closing as likely already resolved — judge will verify."
      bump_impl_noop_count "${IF_LOCAL_ID}"
      gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
        --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
      gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: implementation produced no changes ${OBSERVED_NOOP_COUNT} time(s). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
      tg_notify "Issue #${if_issue} (${IF_LOCAL_ID}) hit impl no-op cap (${OBSERVED_NOOP_COUNT}). Closed as likely already resolved — judge will verify."$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "WARNING"
      IMPL_FAILED_STATE_CHANGED=true
      continue
    fi

    echo "  Issue #${if_issue} has implementation-failed (no-op ${OBSERVED_NOOP_COUNT}/${MAX_IMPL_NOOP_REISSUES}). Closing and re-issuing..."

    # Increment no-op counter before re-issuing
    bump_impl_noop_count "${IF_LOCAL_ID}"

    # Read the original issue to preserve its content
    IF_TITLE="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.title' || echo "")"
    IF_BODY="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.body' || echo "")"

    # Close the failed issue
    ensure_label_exists "ai:closed"
    gh_retry gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
      --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
    gh_retry gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
      -c "Closing: implementation produced no changes. Re-issuing with additional guidance." 2>/dev/null || true

    # Create replacement issue with extra guidance
    NEW_BODY="$(cat <<REISSUE_EOF
${IF_BODY}

---

**⚠️ Re-issued from #${if_issue}** — the previous implementation attempt produced no repository changes.

**Guidance for the implementation model:**
- You MUST create or modify files as described in the plan. Do NOT just describe changes.
- If the plan requires creating files under \`.github/workflows/\`, the consumer repo must have \`ALLOW_WORKFLOW_EDITS=true\` set as a repository variable.
- Verify your changes exist on disk before finishing (e.g. \`ls -la\` the target path).
- If the task genuinely requires no code changes, explain why in a comment instead of silently producing no output.
REISSUE_EOF
)"

    ensure_label_exists "ai:clarification"
    NEW_ISSUE_URL="$(gh issue create --repo "${GITHUB_REPOSITORY}" \
      --title "${IF_TITLE}" \
      --body "${NEW_BODY}" \
      --label "ai:clarification" 2>/dev/null || echo "")"
    if [ -n "${NEW_ISSUE_URL}" ]; then
      NEW_ISSUE_URL_CLEAN="$(printf '%s\n' "${NEW_ISSUE_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
      NEW_ISSUE_NUM="$(basename "${NEW_ISSUE_URL_CLEAN%%[?#]*}")"
      echo "  Created replacement issue #${NEW_ISSUE_NUM} for failed #${if_issue}."

      # Update state file: replace the old issue number with the new one
      # (impl_noop_count is preserved on the issue entry since we only
      # change github_issue, not the issue object itself)
      if [[ "${NEW_ISSUE_NUM}" =~ ^[0-9]+$ ]]; then
        jq --arg if_issue "${if_issue}" --arg new_issue_num "${NEW_ISSUE_NUM}" --arg local_id "${IF_LOCAL_ID}" --argjson wave_idx "${WAVE_IDX}" '(.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue)).github_issue = $new_issue_num | if ($local_id != "" and $local_id != "null") then .issue_number_map[$local_id] = $new_issue_num else . end' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi

      tg_notify "Re-issued implementation-failed issue #${if_issue} as #${NEW_ISSUE_NUM}: ${IF_TITLE}"$'\n'"Old issue: $(_gh_url "issues/${if_issue}")"$'\n'"New issue: $(_gh_url "issues/${NEW_ISSUE_NUM}")" "WARNING"
      IMPL_FAILED_STATE_CHANGED=true
    else
      echo "::warning::Could not create replacement issue for #${if_issue}."
    fi
  done < <(echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "implementation-failed") | .github_issue')

if [ "${IMPL_FAILED_STATE_CHANGED}" = "true" ]; then
  post_state_comment

  # Add labels for any replacement issues created in this cycle.
  REISSUED_NUMS="$(jq -r '.waves['"${WAVE_IDX}"'].issues[].github_issue' "${STATE_FILE}" 2>/dev/null | sort -u)"
  for rnum in ${REISSUED_NUMS}; do
    if [ -z "${rnum}" ] || [ "${rnum}" = "null" ]; then continue; fi
    if echo "${LABELS_JSON}" | jq -e --arg key "${rnum}" 'has($key)' >/dev/null 2>&1; then continue; fi
    LABELS="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rnum}/labels" --jq '[.[].name]' || echo '[]')"
    [ -z "${LABELS}" ] && LABELS='[]'
    LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${rnum}" --argjson labels "${LABELS}" '. + {($key): $labels}')"
  done
fi

  INVOKE_JUDGE_FOR_STUCK=false

  if [ "${WAVE_COMPLETE}" != "true" ]; then
    echo "Wave ${CURRENT_WAVE} not yet complete."

    # Update individual issue statuses in state
    echo "${WAVE_STATUS}" | jq -r '.issues[] | "\(.id) \(.status)"' | while read -r local_id status; do
      echo "  ${local_id}: ${status}"
    done

    # ---------------------------------------------------------------
    # Stuck-wave detection: if some issues have github_issue == null
    # and there are no pending_issue_defs for them, the deferred
    # creation mechanism cannot create them.  If every *created*
    # issue is already terminal (merged/closed), the wave will never
    # complete on its own — invoke the judge to resolve it.
    # ---------------------------------------------------------------
    ANY_NOT_CREATED="$(echo "${WAVE_STATUS}" | jq -r '.any_not_created')"
    if [ "${ANY_NOT_CREATED}" = "true" ]; then
      _stuck_uncreated=0
      while IFS= read -r _unc_id; do
        [ -n "${_unc_id}" ] || continue
        _unc_def="$(jq -r ".pending_issue_defs[\"${_unc_id}\"] // empty" "${STATE_FILE}")"
        if [ -z "${_unc_def}" ]; then
          _stuck_uncreated=$((_stuck_uncreated + 1))
        fi
      done < <(jq -r ".waves[${WAVE_IDX}].issues[] | select(.github_issue == null) | .id" "${STATE_FILE}" 2>/dev/null)

      if [ "${_stuck_uncreated}" -gt 0 ]; then
        # Check whether every created (non-null) issue is in a terminal state.
        NON_TERMINAL_CREATED="$(echo "${WAVE_STATUS}" | jq \
          '[.issues[] | select(.github_issue != null and .status != "merged" and .status != "closed")] | length')"
        if [ "${NON_TERMINAL_CREATED}" -eq 0 ]; then
          echo "Wave ${CURRENT_WAVE} is stuck: ${_stuck_uncreated} issue(s) have no GitHub issue and no pending definition. All created issues are terminal."
          echo "Invoking judge to decide next action for stuck wave..."
          tg_notify "Wave ${CURRENT_WAVE} of project #${TRACKING_NUM} is stuck: ${_stuck_uncreated} issue(s) have no GitHub issue and no pending definition. Invoking judge." "WARNING"
          INVOKE_JUDGE_FOR_STUCK=true
        fi
      fi
    fi

    if [ "${INVOKE_JUDGE_FOR_STUCK}" != "true" ]; then
    # ---------------------------------------------------------------
    # Stall detection and self-healing
    # ---------------------------------------------------------------
    _stall_check_args=(
      --state-file "${STATE_FILE}"
      --labels-json "${LABELS_JSON}"
      --threshold-minutes "${STALL_THRESHOLD_MINUTES}"
      --max-recoveries "${MAX_STALL_RECOVERIES_PER_ISSUE}"
      --stall-judge-trigger-count "${STALL_JUDGE_TRIGGER_COUNT}"
      --enable-stall-judge "${ENABLE_STALL_JUDGE}"
    )
    if [ -n "${PHASE_THRESHOLDS_JSON:-}" ]; then
      _stall_check_args+=(--phase-thresholds-json "${PHASE_THRESHOLDS_JSON}")
    fi

    STALLS_JSON="$(python3 scripts/orchestrate_lib.py check-stalls \
      "${_stall_check_args[@]}" 2>/dev/null || echo '{"ok":false,"stalls":[],"count":0}')"

    STALL_COUNT="$(echo "${STALLS_JSON}" | jq -r '.count')"

    STALL_STATE_CHANGED=false
    STALL_HEALING_CHANGED=false

    if [ "${STALL_COUNT}" -gt 0 ]; then
      echo "Detected ${STALL_COUNT} stalled issue(s). Checking for active workflow runs..."

      # Build the set of issues with active workflows (one API call batch,
      # reused across all stalled issues to avoid per-issue API calls).
      ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"
      if [ -n "${ACTIVE_WORKFLOW_ISSUES}" ]; then
        echo "Issues with active workflow runs: $(echo "${ACTIVE_WORKFLOW_ISSUES}" | tr '\n' ' ')"
      fi

      while IFS= read -r stall_entry; do
        [ -n "${stall_entry}" ] || continue
        STALL_ISSUE="$(echo "${stall_entry}" | jq -r '.github_issue')"
        STALL_PHASE="$(echo "${stall_entry}" | jq -r '.phase')"
        STALL_ACTION="$(echo "${stall_entry}" | jq -r '.recovery_action')"
        STALL_LOCAL_ID="$(echo "${stall_entry}" | jq -r '.id')"
        STALL_RECOVERY_COUNT="$(echo "${stall_entry}" | jq -r '.stall_recovery_count')"
        STALL_DURATION="$(echo "${stall_entry}" | jq -r '.stall_duration_minutes')"
        STALL_RECOVERY_SHOULD_INCREMENT="false"
        if recover_stalled_issue \
          "${STALL_ISSUE}" "${STALL_PHASE}" "${STALL_ACTION}" \
          "${STALL_RECOVERY_COUNT}" "${STALL_LOCAL_ID}" "${STALL_DURATION}"; then

          STALL_STATE_CHANGED=true

          if [ "${STALL_RECOVERY_SHOULD_INCREMENT}" = "true" ] && [ -n "${STALL_LOCAL_ID}" ] && [ "${STALL_LOCAL_ID}" != "null" ]; then
            python3 -c "
import json, time, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import increment_stall_recovery

with open('${STATE_FILE}') as f:
    state = json.load(f)

increment_stall_recovery(state, '${STALL_LOCAL_ID}')

with open('${STATE_FILE}', 'w') as f:
    json.dump(state, f, indent=2)
" || true
          fi
        else
          echo "  [stall-recovery] No action taken for #${STALL_ISSUE} (active workflow or guard)."
        fi
      done < <(echo "${STALLS_JSON}" | jq -c '.stalls[]')

    else
      if [ -n "${PHASE_THRESHOLDS_JSON:-}" ]; then
        echo "No stalled issues detected (fallback: ${STALL_THRESHOLD_MINUTES}m, per-phase overrides active)."
      else
        echo "No stalled issues detected (threshold: ${STALL_THRESHOLD_MINUTES}m)."
      fi
    fi

    if [ "${STALL_STATE_CHANGED}" = "true" ] || [ "${STALL_HEALING_CHANGED}" = "true" ] || [ "${TIMESTAMP_STATE_CHANGED}" = "true" ] || [ "${RECONCILE_STATE_CHANGED}" = "true" ] || [ "${RECONCILE_LABELS_CHANGED}" = "true" ]; then
      post_state_comment
    fi
    if [ "${RECONCILE_LABELS_CHANGED}" = "true" ] || [ "${RECONCILE_STATE_CHANGED}" = "true" ] || [ "${STALL_HEALING_CHANGED}" = "true" ]; then
      post_healing_summary_comment
    fi

    if [ "${INVOKE_JUDGE_FOR_STUCK}" != "true" ]; then
      continue
    fi
    fi  # end: INVOKE_JUDGE_FOR_STUCK != true
  fi

  if [ "${INVOKE_JUDGE_FOR_STUCK}" = "true" ]; then
    echo "Wave ${CURRENT_WAVE} stuck — invoking judge to define missing issues or decide next action..."
  else
    echo "Wave ${CURRENT_WAVE} complete!"
  fi

  # ---------------------------------------------------------------
  # Guard: cap judge stall cycles to prevent infinite loops.
  # Only non-advancing actions (recovery, fix-ups, stalls) count
  # against this budget. Clean wave advances are free.
  # ---------------------------------------------------------------
  MAX_JUDGE="${MAX_JUDGE_CYCLES:-25}"
  if ! [[ "${MAX_JUDGE}" =~ ^[0-9]+$ ]] || [ "${MAX_JUDGE}" -lt 1 ]; then
    MAX_JUDGE="25"
  fi
  # Dynamic floor: ensure budget is at least total_waves * 2 so that
  # a project can never be starved by its own size.
  DYNAMIC_FLOOR=$(( TOTAL_WAVES * 2 ))
  if [ "${MAX_JUDGE}" -lt "${DYNAMIC_FLOOR}" ]; then
    echo "Raising MAX_JUDGE_CYCLES from ${MAX_JUDGE} to dynamic floor ${DYNAMIC_FLOOR} (total_waves=${TOTAL_WAVES} × 2)."
    MAX_JUDGE="${DYNAMIC_FLOOR}"
  fi
  if [ "$((JUDGE_STALL_CYCLES + 1))" -gt "${MAX_JUDGE}" ]; then
    echo "::error::Judge stall cycle limit reached ($((JUDGE_STALL_CYCLES + 1)) > ${MAX_JUDGE}). Marking project as failed."
    jq '.status = "failed"' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -f body="## Project Failed — Judge stall cycle limit exceeded

Judge has used ${JUDGE_STALL_CYCLES} stall cycle(s) (recovery/fix-ups) out of ${MAX_JUDGE} allowed (total judge evaluations: ${JUDGE_CYCLE}).
Clean wave advances do not count against this limit.
Manual intervention required." >/dev/null
    tg_notify "Project #${TRACKING_NUM} FAILED: judge stall cycle limit (${JUDGE_STALL_CYCLES}/${MAX_JUDGE}) exceeded." "CRITICAL"
    tg_cleanup_msgs "${TRACKING_NUM}"
    continue
  fi

  # ---------------------------------------------------------------
  # Run judge (full repo checkout + Codex call)
  # ---------------------------------------------------------------
  echo "Running judge evaluation (cycle $((JUDGE_CYCLE + 1)), stall: ${JUDGE_STALL_CYCLES}, budget: ${MAX_JUDGE})..."

  # Setup Codex config for judge
  mkdir -p ~/.codex
  JUDGE_INVOCATION_CYCLE=$((JUDGE_CYCLE + 1))
  EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE="${MODEL_REASONING_EFFORT_JUDGE}"
  if [ "${JUDGE_INVOCATION_CYCLE}" -gt 3 ] && [ "${MODEL_REASONING_EFFORT_JUDGE}" = "xhigh" ]; then
    EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE="high"
  fi
  echo "Judge reasoning effort for cycle ${JUDGE_INVOCATION_CYCLE}: ${EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE}"
  CATALOG_PATH="$(pwd)/scripts/codex_model_catalog.json"
  {
    echo 'web_search = "live"'
    echo 'model_provider = "openrouter"'
    echo "model = \"${MODEL_EDITOR}\""
    echo "model_reasoning_effort = \"${EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE}\""
    if [ -f "${CATALOG_PATH}" ]; then
      echo "model_catalog_json = \"${CATALOG_PATH}\""
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

  # Setup Serena for judge
  bash scripts/setup_serena.sh --mode planning --context codex || true

  # Collect merged PR diffs for context
  PR_SUMMARIES=""
  ISSUE_MAP="$(jq -r '.issue_number_map // {}' "${STATE_FILE}")"
  for inum in ${ISSUE_NUMS}; do
    PR_NUM="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
      || echo "")"
    if [[ "${PR_NUM}" =~ ^[0-9]+$ ]]; then
      PR_DIFF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUM}" \
        -H 'Accept: application/vnd.github.diff' 2>/dev/null | head -500 || echo "(diff unavailable)")"
      PR_SUMMARIES+="
--- PR #${PR_NUM} (Issue #${inum}) ---
${PR_DIFF}

"
    fi
  done

  # Fetch CI status on default branch
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  CI_STATUS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/commits/${DEFAULT_BRANCH}/check-runs" \
    --jq '[.check_runs[] | {name: .name, conclusion: .conclusion}]' || echo "[]")"

  # Get original project description from tracking issue body
  PROJECT_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" --jq '.body' || echo "")"

  # Build one stable static prefix per run for provider-side prompt caching.
  assemble_judge_static_context "${RUNTIME_DIR}/judge_static.txt"

  # Build judge prompt
  {
    cat "${RUNTIME_DIR}/judge_static.txt"
    echo
    echo "=== JUDGE TASK ==="
    echo
    bash scripts/render_prompt.sh prompts/mode-judge.txt
    echo
    echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
    echo
    echo "=== PROJECT SPEC ==="
    echo
    echo "${PROJECT_BODY}"
    echo
    echo "=== WAVE ${CURRENT_WAVE} COMPLETION STATUS ==="
    echo
    echo "${WAVE_STATUS}" | jq '.'
    echo
    echo "=== MERGED PR DIFFS (truncated) ==="
    echo
    echo "${PR_SUMMARIES}"
    echo
    echo "=== CI STATUS ON ${DEFAULT_BRANCH} ==="
    echo
    echo "${CI_STATUS}" | jq '.'
    echo
    echo "=== ORCHESTRATOR STATE ==="
    echo
    echo "Judge cycle: $((JUDGE_CYCLE + 1))"
    echo "Current wave just completed: ${CURRENT_WAVE} of ${TOTAL_WAVES}"
    echo "Project complete (all waves dispatched and merged): ${PROJECT_COMPLETE}"
    echo "Recovery count: ${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}"
    PENDING_DEFS_COUNT="$(jq '.pending_issue_defs | length' "${STATE_FILE}")"
    echo "Pending issue definitions (not yet created): ${PENDING_DEFS_COUNT}"
    if [ "${PENDING_DEFS_COUNT}" -gt 0 ]; then
      echo
      echo "=== REMAINING WAVES (issues not yet created) ==="
      jq -r '.pending_issue_defs | to_entries[] | "- \(.key): \(.value.title)"' "${STATE_FILE}"
    fi
    if [ "${ANY_FAILED}" = "true" ]; then
      echo "WARNING: Some issues in this wave were closed without merge."
    fi
    if [ "${INVOKE_JUDGE_FOR_STUCK}" = "true" ]; then
      echo
      echo "WARNING: This judge invocation was triggered because the wave is STUCK."
      echo "Some issues in this wave have github_issue == null and no pending_issue_defs"
      echo "to create them from. Deferred issue creation could not create these issues."
      echo "All other (created) issues in the wave are in terminal states."
      echo "You must decide what to do: define the missing issues, skip them, or take"
      echo "another corrective action. The wave CANNOT complete on its own."
    fi
    echo
    echo "IMPORTANT: If current wave < total waves, the project is NOT complete."
    echo "Return in_progress to advance to the next wave."
  } > "${JUDGE_PROMPT_FILE}"

  # Run judge via Codex
  JUDGE_SUCCESS=false
  max_attempts=2
  for attempt in $(seq 1 "${max_attempts}"); do
    echo "Judge attempt ${attempt}/${max_attempts}..."
    # The pipeline may return 141 (SIGPIPE) when the prompt is larger
    # than the OS pipe buffer and codex closes stdin before cat finishes.
    # This is harmless — check the output file regardless of exit code.
    cat "${JUDGE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${JUDGE_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/judge_log.txt" >&2) || true
    if grep -q '[^[:space:]]' "${JUDGE_OUTPUT_FILE}"; then
      JUDGE_SUCCESS=true
      break
    fi
    if [ "${attempt}" -lt "${max_attempts}" ]; then
      sleep $(( 10 * attempt + RANDOM % 10 ))
    fi
  done

  if [ "${JUDGE_SUCCESS}" != "true" ]; then
    echo "::error::Judge failed for tracking issue #${TRACKING_NUM}"
    tg_notify "Orchestrator Judge failed for #${TRACKING_NUM}. Manual review needed." "CRITICAL"
    continue
  fi

  # ---------------------------------------------------------------
  # Parse judge output
  # ---------------------------------------------------------------
  JUDGE_JSON="$(python3 -c "
import json, re, sys

raw = open('${JUDGE_OUTPUT_FILE}', 'r').read()

try:
    data = json.loads(raw.strip())
    json.dump(data, sys.stdout)
    sys.exit(0)
except json.JSONDecodeError:
    pass

cleaned = re.sub(r'\`\`\`(?:json)?\s*', '', raw)
cleaned = re.sub(r'\`\`\`\s*$', '', cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for i, ch in enumerate(cleaned):
    if ch == '{':
        if brace_depth == 0:
            start = i
        brace_depth += 1
    elif ch == '}':
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            candidate = cleaned[start:i+1]
            try:
                data = json.loads(candidate)
                json.dump(data, sys.stdout)
                sys.exit(0)
            except json.JSONDecodeError:
                start = None

print('Could not parse judge JSON', file=sys.stderr)
sys.exit(1)
" 2>/dev/null || echo "")"

  if [ -z "${JUDGE_JSON}" ]; then
    echo "::error::Could not parse judge output for #${TRACKING_NUM}"
    tg_notify "Orchestrator Judge output unparseable for #${TRACKING_NUM}. Manual review needed." "CRITICAL"
    continue
  fi

  JUDGE_STATUS="$(echo "${JUDGE_JSON}" | jq -r '.status')"
  JUDGE_JUSTIFICATION="$(echo "${JUDGE_JSON}" | jq -r '.justification // "no justification provided"')"
  JUDGE_ASSESSMENT="$(echo "${JUDGE_JSON}" | jq -r '.assessment // ""')"
  NEW_ISSUES_COUNT="$(echo "${JUDGE_JSON}" | jq '.new_issues | length')"
  REVERT_COUNT="$(echo "${JUDGE_JSON}" | jq '.issues_to_revert | length')"

  echo "Judge verdict: ${JUDGE_STATUS}"
  echo "Justification: ${JUDGE_JUSTIFICATION}"
  echo "New issues: ${NEW_ISSUES_COUNT}, Reverts: ${REVERT_COUNT}"

  # Notify operator after every judge evaluation
  tg_notify "Judge evaluated #${TRACKING_NUM} (cycle $((JUDGE_CYCLE + 1))): ${JUDGE_STATUS}. ${JUDGE_JUSTIFICATION}" "DEBUG"

  # Post judge assessment to tracking issue
  JUDGE_COMMENT="## Judge Evaluation — Cycle $((JUDGE_CYCLE + 1)) (stall budget: ${JUDGE_STALL_CYCLES}/${MAX_JUDGE})

**Status:** ${JUDGE_STATUS}
**Justification:** ${JUDGE_JUSTIFICATION}

${JUDGE_ASSESSMENT}

New fix-up issues: ${NEW_ISSUES_COUNT}
PRs to revert: ${REVERT_COUNT}"

  gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    -f body="${JUDGE_COMMENT}" >/dev/null

  # ---------------------------------------------------------------
  # Hard guard: judge cannot declare "complete" while waves remain
  # ---------------------------------------------------------------
  if [ "${JUDGE_STATUS}" = "complete" ] && [ "${PROJECT_COMPLETE}" != "true" ]; then
    echo "::warning::Judge returned 'complete' but project_complete=${PROJECT_COMPLETE} (wave ${CURRENT_WAVE}/${TOTAL_WAVES}). Overriding to 'in_progress'."
    JUDGE_STATUS="in_progress"
    gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -f body="⚠️ Judge verdict overridden: \`complete\` → \`in_progress\` because wave ${CURRENT_WAVE}/${TOTAL_WAVES} is not the final wave. Advancing to next wave." >/dev/null
  fi

  # ---------------------------------------------------------------
  # Handle judge verdict
  # ---------------------------------------------------------------
  case "${JUDGE_STATUS}" in
    complete)
      if [ "${ENABLE_VALIDATION}" != "true" ]; then
        FINAL_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
        FINAL_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
        FINAL_PROJECT_TITLE="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

        if ! finalize_integration_merge_if_needed "${FINAL_INTEGRATION_BRANCH}" "${FINAL_DEFAULT_BRANCH}" "${FINAL_PROJECT_TITLE}"; then
          continue
        fi

        echo "Project complete!"
        jq '.status = "complete" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment

        set_tracking_phase_label "ai:merged"
        post_tracking_comment "Project completed successfully after $((JUDGE_CYCLE + 1)) judge cycle(s) (${JUDGE_STALL_CYCLES} stall). Issue kept open for manual review."

        tg_cleanup_msgs "${TRACKING_NUM}"
        MSG="Project #${TRACKING_NUM} completed! All waves merged and judge approved."
        MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
        if [ -n "${GITHUB_RUN_ID:-}" ]; then
          MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
        fi
        tg_send_msg "${MSG}" "DEBUG" >/dev/null
        continue
      fi

      VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
      if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
        VALIDATION_CYCLE="1"
      fi

      if [ "${VALIDATION_CYCLE}" -gt "${MAX_VALIDATE_CYCLES}" ]; then
        mark_validation_failed "Validation cycle ${VALIDATION_CYCLE} exceeds MAX_VALIDATE_CYCLES=${MAX_VALIDATE_CYCLES}."
        continue
      fi

      post_tracking_comment "## ✅ Judge declared project complete — cycle $((JUDGE_CYCLE + 1))\n\n**Reason:** ${JUDGE_JUSTIFICATION}\n\nAll waves have merged and the judge is satisfied. Transitioning to runtime validation (cycle ${VALIDATION_CYCLE}) to confirm correctness before closing."

      jq --argjson cycle "${VALIDATION_CYCLE}" \
        '.status = "validating" |
         .judge_cycle += 1 |
         .validation_cycle = $cycle |
         .validation_active_fix_issues = [] |
         .validation_seen_fix_issues = (.validation_seen_fix_issues // []) |
         .validation_last_fix_comment_id = (.validation_last_fix_comment_id // 0) |
         .validation_last_dispatch_cycle = 0 |
         .validation_failure_reason = null |
         .validation_completed_cycle = null' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      set_tracking_phase_label "ai:validating"

      if ! dispatch_validation_if_needed "${VALIDATION_CYCLE}"; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write. Error: ${VALIDATION_DISPATCH_ERROR:-unknown}"
      fi
      ;;

    failed)
      echo "Judge declared failure."

      # ---------------------------------------------------------------
      # Auto-recovery: configurable attempts (replaces single-shot boolean)
      # ---------------------------------------------------------------
      if [ "${RECOVERY_COUNT}" -ge "${MAX_RECOVERY_ATTEMPTS}" ]; then
        echo "Recovery attempts exhausted (${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}). Stopping."
        jq '.status = "failed" | .judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        post_state_comment

        gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
          -f body="## Project Failed

Recovery was attempted ${RECOVERY_COUNT} time(s) (max ${MAX_RECOVERY_ATTEMPTS}) but the judge still reports failure. Manual intervention required.

**Assessment:** ${JUDGE_ASSESSMENT}" >/dev/null

        tg_notify "Project #${TRACKING_NUM} FAILED after ${RECOVERY_COUNT} recovery attempt(s). Manual intervention needed." "CRITICAL"
        tg_cleanup_msgs "${TRACKING_NUM}"
        continue
      fi

      echo "Attempting auto-recovery ($((RECOVERY_COUNT + 1))/${MAX_RECOVERY_ATTEMPTS})..."

      # Revert problematic PRs if judge requested
      if [ "${REVERT_COUNT}" -gt 0 ]; then
        echo "Reverting ${REVERT_COUNT} PR(s)..."
        echo "${JUDGE_JSON}" | jq -r '.issues_to_revert[]' | while read -r revert_issue; do
          # Find PR linked to this issue
          PR_TO_REVERT="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${revert_issue}/timeline" \
            --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
            || echo "")"
          if [[ "${PR_TO_REVERT}" =~ ^[0-9]+$ ]]; then
            echo "  Reverting PR #${PR_TO_REVERT} (issue #${revert_issue})..."
            # Create revert PR via gh
            gh api "repos/${GITHUB_REPOSITORY}/pulls" \
              -f title="Revert PR #${PR_TO_REVERT} (orchestrator auto-recovery)" \
              -f head="revert-${PR_TO_REVERT}-$(date +%s)" \
              -f base="${DEFAULT_BRANCH}" \
              -f body="Automated revert of PR #${PR_TO_REVERT} by orchestrator judge.

**Reason:** ${JUDGE_JUSTIFICATION}" >/dev/null 2>&1 || {
              # If API-based revert fails, create a revert via git
              echo "  API revert failed; creating revert commit..."
              MERGE_SHA="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${PR_TO_REVERT}" --jq '.merge_commit_sha' || echo "")"
              if [ -n "${MERGE_SHA}" ] && [ "${MERGE_SHA}" != "null" ]; then
                REVERT_BRANCH="revert-${PR_TO_REVERT}-$(date +%s)"
                git checkout -b "${REVERT_BRANCH}" "${DEFAULT_BRANCH}"
                if git revert --no-edit "${MERGE_SHA}"; then
                  git push -u origin "${REVERT_BRANCH}"
                  gh pr create \
                    --repo "${GITHUB_REPOSITORY}" \
                    --title "Revert PR #${PR_TO_REVERT} (orchestrator auto-recovery)" \
                    --body "Automated revert of PR #${PR_TO_REVERT} by orchestrator judge.

**Reason:** ${JUDGE_JUSTIFICATION}" \
                    --base "${DEFAULT_BRANCH}" \
                    --head "${REVERT_BRANCH}"
                else
                  echo "::warning::Git revert of ${MERGE_SHA} failed (conflicts). Manual revert needed."
                fi
                git checkout "${DEFAULT_BRANCH}" 2>/dev/null || true
              fi
            }
          fi
        done
      fi

      # Create fix-up issues from judge
      if [ "${NEW_ISSUES_COUNT}" -gt 0 ]; then
        echo "Creating ${NEW_ISSUES_COUNT} fix-up issue(s)..."
        echo "${JUDGE_JSON}" | jq -c '.new_issues[]' | while read -r fix_issue; do
          FIX_TITLE="$(echo "${fix_issue}" | jq -r '.title')"
          FIX_BODY="$(echo "${fix_issue}" | jq -r '.body' | sed 's/\\n/\n/g')"
          FIX_ID="$(echo "${fix_issue}" | jq -r '.id')"

          # --- Dedup guard: skip if this local ID already has a GitHub issue ---
          if [ -n "${FIX_ID}" ] && [ "${FIX_ID}" != "null" ]; then
            EXISTING_NUM="$(jq -r --arg fix_id "${FIX_ID}" '.issue_number_map[$fix_id] // empty' "${STATE_FILE}")"
            if [ -n "${EXISTING_NUM}" ]; then
              EXISTING_LABELS="$(get_issue_labels_json "${EXISTING_NUM}")"
              if ! has_label "${EXISTING_LABELS}" "ai:merged" && ! has_label "${EXISTING_LABELS}" "ai:closed"; then
                jq --arg fix_id "${FIX_ID}" --argjson existing_num "${EXISTING_NUM}" --argjson wave_idx "${WAVE_IDX}" \
                  '.issue_number_map[$fix_id] = $existing_num | .waves[$wave_idx].issues |= map(select(.id != $fix_id)) | .waves[$wave_idx].issues += [{"id": $fix_id, "github_issue": $existing_num, "status": "pending"}]' \
                  "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
                echo "  ${FIX_ID}: already exists as #${EXISTING_NUM} and is still open, skipping duplicate fix-up."
                continue
              fi
              echo "  ${FIX_ID}: prior issue #${EXISTING_NUM} is already merged/closed, allowing new fix-up."
            fi
          fi

          FULL_FIX_BODY="${FIX_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${FIX_ID}\`
- Type: judge-fix-up (cycle $((JUDGE_CYCLE + 1)))
- Managed by: AI Orchestrator"

          ensure_label_exists "ai:clarification"
          FIX_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${FIX_TITLE}" \
            --body "${FULL_FIX_BODY}" \
            --label "ai:clarification")"
          echo "  Created fix-up: ${FIX_URL}"

          # Record in state so subsequent cycles/iterations won't recreate,
          # and add to the current wave so the poller tracks merge progress.
          FIX_URL_CLEAN="$(printf '%s\n' "${FIX_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          FIX_NEW_NUM="$(basename "${FIX_URL_CLEAN%%[?#]*}")"
          if [[ "${FIX_NEW_NUM}" =~ ^[0-9]+$ ]] && [ -n "${FIX_ID}" ] && [ "${FIX_ID}" != "null" ]; then
            jq --arg fix_id "${FIX_ID}" --argjson fix_new_num "${FIX_NEW_NUM}" --argjson wave_idx "${WAVE_IDX}" \
              '.issue_number_map[$fix_id] = $fix_new_num | .waves[$wave_idx].issues |= map(select(.id != $fix_id)) | .waves[$wave_idx].issues += [{"id": $fix_id, "github_issue": $fix_new_num, "status": "pending"}]' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          fi
        done
      fi

      # Update state — increment recovery_count (replaces old recovery_attempted boolean)
      jq '.judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1) | .recovery_count = ((.recovery_count // (if .recovery_attempted == true then 1 else 0 end)) + 1) | .status = "in_progress"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

      post_state_comment

      tg_notify "Orchestrator auto-recovery ($((RECOVERY_COUNT + 1))/${MAX_RECOVERY_ATTEMPTS}) started for #${TRACKING_NUM}: ${NEW_ISSUES_COUNT} fix-up issues, ${REVERT_COUNT} reverts." "WARNING"
      ;;

    in_progress)
      echo "Project in progress."

      # Create new issues if judge found gaps
      WAVE_ISSUE_COUNT_BEFORE="$(jq --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | length' "${STATE_FILE}")"
      WAVE_ISSUE_TRACKING_BEFORE="$(jq -c --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | map({id, github_issue, status}) | sort_by(.id)' "${STATE_FILE}")"
      if [ "${NEW_ISSUES_COUNT}" -gt 0 ]; then
        echo "Creating ${NEW_ISSUES_COUNT} new issue(s) from judge..."
        echo "${JUDGE_JSON}" | jq -c '.new_issues[]' | while read -r new_issue; do
          NEW_TITLE="$(echo "${new_issue}" | jq -r '.title')"
          NEW_BODY="$(echo "${new_issue}" | jq -r '.body' | sed 's/\\n/\n/g')"
          NEW_ID="$(echo "${new_issue}" | jq -r '.id')"

          # --- Dedup guard: skip if this local ID already has a GitHub issue ---
          if [ -n "${NEW_ID}" ] && [ "${NEW_ID}" != "null" ]; then
            EXISTING_NUM="$(jq -r --arg new_id "${NEW_ID}" '.issue_number_map[$new_id] // empty' "${STATE_FILE}")"
            if [ -n "${EXISTING_NUM}" ]; then
              EXISTING_LABELS="$(get_issue_labels_json "${EXISTING_NUM}")"
              if ! has_label "${EXISTING_LABELS}" "ai:merged" && ! has_label "${EXISTING_LABELS}" "ai:closed"; then
                jq --arg new_id "${NEW_ID}" --argjson existing_num "${EXISTING_NUM}" --argjson wave_idx "${WAVE_IDX}" \
                  '.issue_number_map[$new_id] = $existing_num | .waves[$wave_idx].issues |= map(select(.id != $new_id)) | .waves[$wave_idx].issues += [{"id": $new_id, "github_issue": $existing_num, "status": "pending"}]' \
                  "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
                echo "  ${NEW_ID}: already exists as #${EXISTING_NUM} and is still open, skipping duplicate addition."
                continue
              fi
              echo "  ${NEW_ID}: prior issue #${EXISTING_NUM} is already merged/closed, allowing new addition."
            fi
          fi

          FULL_NEW_BODY="${NEW_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${NEW_ID}\`
- Type: judge-addition (cycle $((JUDGE_CYCLE + 1)))
- Managed by: AI Orchestrator"

          ensure_label_exists "ai:clarification"
          NEW_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${NEW_TITLE}" \
            --body "${FULL_NEW_BODY}" \
            --label "ai:clarification")"
          echo "  Created: ${NEW_URL}"

          # Record in state so subsequent cycles/iterations won't recreate,
          # and add to the current wave so the poller tracks merge progress.
          NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          ADD_NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
          if [[ "${ADD_NEW_NUM}" =~ ^[0-9]+$ ]] && [ -n "${NEW_ID}" ] && [ "${NEW_ID}" != "null" ]; then
            jq --arg new_id "${NEW_ID}" --argjson add_new_num "${ADD_NEW_NUM}" --argjson wave_idx "${WAVE_IDX}" \
              '.issue_number_map[$new_id] = $add_new_num | .waves[$wave_idx].issues |= map(select(.id != $new_id)) | .waves[$wave_idx].issues += [{"id": $new_id, "github_issue": $add_new_num, "status": "pending"}]' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          fi
        done
      fi

      # ---------------------------------------------------------------
      # Guard: do not advance if fix-up/new issues were added to the
      # current wave during this judge cycle. Those issues are still
      # pending and the poller only monitors current_wave, so advancing
      # would orphan them.
      # ---------------------------------------------------------------
      WAVE_ISSUE_COUNT_AFTER="$(jq --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | length' "${STATE_FILE}")"
      WAVE_ISSUE_TRACKING_AFTER="$(jq -c --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | map({id, github_issue, status}) | sort_by(.id)' "${STATE_FILE}")"
      if [ "${WAVE_ISSUE_COUNT_AFTER}" -gt "${WAVE_ISSUE_COUNT_BEFORE}" ] || [ "${WAVE_ISSUE_TRACKING_AFTER}" != "${WAVE_ISSUE_TRACKING_BEFORE}" ]; then
        if [ "${WAVE_ISSUE_COUNT_AFTER}" -gt "${WAVE_ISSUE_COUNT_BEFORE}" ]; then
          ADDED_COUNT=$(( WAVE_ISSUE_COUNT_AFTER - WAVE_ISSUE_COUNT_BEFORE ))
          echo "Current wave gained ${ADDED_COUNT} new issue(s) from judge. Staying on wave ${CURRENT_WAVE} until they complete."
        else
          echo "Current wave issue tracking changed from judge output. Staying on wave ${CURRENT_WAVE} until updated issues complete."
        fi
        jq '.judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment
        # Skip wave advancement — next poll cycle will re-check this wave
      else

      # Advance to next wave
      NEXT_WAVE=$(( CURRENT_WAVE + 1 ))
      if [ "${NEXT_WAVE}" -le "${TOTAL_WAVES}" ]; then
        echo "Advancing to wave ${NEXT_WAVE}..."
        NEXT_WAVE_IDX=$(( NEXT_WAVE - 1 ))

        # -----------------------------------------------------------
        # Deferred issue creation: create issues for this wave now.
        # This triggers clarify.yml via the issues.opened event.
        # -----------------------------------------------------------
        CREATED_NUMS=""
        NEXT_WAVE_ISSUE_IDS="$(jq -r ".waves[${NEXT_WAVE_IDX}].issues[].id" "${STATE_FILE}")"
        for local_id in ${NEXT_WAVE_ISSUE_IDS}; do
          # Check if already created (has a github_issue number)
          EXISTING_NUM="$(jq -r ".issue_number_map[\"${local_id}\"] // empty" "${STATE_FILE}")"
          if [ -n "${EXISTING_NUM}" ]; then
            echo "  ${local_id}: already exists as #${EXISTING_NUM}"
            CREATED_NUMS="${CREATED_NUMS} ${EXISTING_NUM}"
            continue
          fi

          # Get issue definition from pending_issue_defs in state
          ISSUE_DEF="$(jq -c ".pending_issue_defs[\"${local_id}\"] // empty" "${STATE_FILE}")"
          if [ -z "${ISSUE_DEF}" ]; then
            echo "::warning::No pending definition for ${local_id}, skipping."
            continue
          fi

          DEF_TITLE="$(echo "${ISSUE_DEF}" | jq -r '.title')"
          DEF_BODY="$(echo "${ISSUE_DEF}" | jq -r '.body' | sed 's/\\n/\n/g')"
          DEF_PRIORITY="$(echo "${ISSUE_DEF}" | jq -r '.priority')"

          FULL_BODY="${DEF_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${local_id}\`
- Priority: ${DEF_PRIORITY}
- Managed by: AI Orchestrator"

          ensure_label_exists "ai:clarification"
          NEW_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${DEF_TITLE}" \
            --body "${FULL_BODY}" \
            --label "ai:clarification")"

          NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
          if ! [[ "${NEW_NUM}" =~ ^[0-9]+$ ]]; then
            echo "::warning::Could not parse numeric issue number for pending issue ${local_id}; skipping state update."
            continue
          fi
          echo "  Created #${NEW_NUM}: ${DEF_TITLE} (${local_id})"
          CREATED_NUMS="${CREATED_NUMS} ${NEW_NUM}"

          # Update state: record the new issue number and remove from pending
          jq ".issue_number_map[\"${local_id}\"] = ${NEW_NUM} | del(.pending_issue_defs[\"${local_id}\"])" \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

          # Update the wave entry with the github issue number
          jq "(.waves[${NEXT_WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\")" \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        done

        # Clean wave advance: does not consume stall budget.
        jq ".current_wave = ${NEXT_WAVE} | .judge_cycle += 1" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        WAVE_COMMENT="## Wave ${NEXT_WAVE} Dispatched

Dependencies from Wave ${CURRENT_WAVE} are met. Created and dispatched:

$(for inum in ${CREATED_NUMS}; do echo "- #${inum}"; done)

These issues will enter the AI pipeline (clarify → plan → implement → review)."

        gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
          -f body="${WAVE_COMMENT}" >/dev/null
      else
        # All waves dispatched but judge says in_progress with new issues
        jq '.judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi

      # Post updated state
      post_state_comment

      fi  # end: PENDING_IN_WAVE guard
      ;;

    *)
      echo "::warning::Unknown judge status: ${JUDGE_STATUS}"
      ;;
  esac
done

run_standalone_stall_recovery

close_merged_issues_sweep

# ---------------------------------------------------------------
# Standalone PR conflict sweep
# ---------------------------------------------------------------
# The tracking-issue loop above only handles PRs linked to
# orchestrator-managed issues. Standalone PRs can also develop merge
# conflicts when the base branch advances. The review workflow already
# has conflict resolution logic, but it only runs on PR synchronize
# events — no event fires when the *base* branch moves forward.
#
# This section scans all eligible open PRs and, for any PR in
# mergeable_state=dirty, first attempts update-branch and then falls
# back to workflow_dispatch conflict handling.
# ---------------------------------------------------------------
echo ""
echo "========================================"
echo "Standalone PR conflict sweep"
echo "========================================"

# Collect open PR candidates with their refs.
# gh pr list does not expose mergeable, so we fetch the full list and
# then query each candidate via the REST API.
STANDALONE_PRS="$(gh pr list \
	--repo "${GITHUB_REPOSITORY}" \
	--state open \
	--json number,headRefName,baseRefName \
	--limit 100 2>/dev/null || echo "[]")"

STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
echo "Found ${STANDALONE_COUNT} open PR(s) to scan."

CONFLICT_SWEEP_FIXED=0
DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"

for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
	S_PR="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].number")"
	S_HEAD="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].headRefName")"
	S_BASE="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].baseRefName")"
	if [ -z "${S_PR}" ] || [ "${S_PR}" = "null" ]; then
		continue
	fi

	if [ -z "${S_HEAD}" ] || [ "${S_HEAD}" = "null" ]; then
		echo "::warning::Standalone PR #${S_PR} has no head ref. Skipping conflict sweep candidate."
		continue
	fi

	if [[ "${S_BASE}" == orchestrator/project-* ]]; then
		continue
	fi

	# Check mergeable state via REST API (dirty == merge conflicts)
	S_PR_JSON="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}" 2>/dev/null || echo '{}')"
	S_STATE="$(echo "${S_PR_JSON}" | jq -r '.state // ""')"
	if [ "${S_STATE}" != "open" ]; then
		continue
	fi

	S_HEAD_REF="$(echo "${S_PR_JSON}" | jq -r '.head.ref // ""')"
	if [ -z "${S_HEAD_REF}" ] || [ "${S_HEAD_REF}" = "null" ]; then
		echo "::warning::Standalone PR #${S_PR} has unavailable head ref from API. Skipping conflict dispatch path."
		continue
	fi

	S_MERGEABLE_STATE="$(echo "${S_PR_JSON}" | jq -r '.mergeable_state // ""')"
	if [ -z "${S_MERGEABLE_STATE}" ] || [ "${S_MERGEABLE_STATE}" = "unknown" ]; then
		continue
	fi

	if [ "${S_MERGEABLE_STATE}" != "dirty" ]; then
		continue
	fi

	echo "  PR #${S_PR} (${S_HEAD_REF}) is in conflicted mergeable state. Attempting conflict recovery..."

	# Stage 1: Try the GitHub API update-branch endpoint (clean merge)
	S_HEAD_SHA="$(echo "${S_PR_JSON}" | jq -r '.head.sha // ""')"
	if [ -n "${S_HEAD_SHA}" ] && gh api "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}/update-branch" \
		-X PUT -f expected_head_sha="${S_HEAD_SHA}" 2>/dev/null; then
		echo "  PR #${S_PR} branch updated via API. Synchronize event will re-trigger review."
		CONFLICT_SWEEP_FIXED=$(( CONFLICT_SWEEP_FIXED + 1 ))
		continue
	fi

	# Stage 2: Dispatch review workflow for conflict resolution.
	_dispatch_rc=0
	_dispatch_review_for_conflicts "${S_PR}" "${S_HEAD_REF}" || _dispatch_rc=$?
	if [ "${_dispatch_rc}" -eq 0 ]; then
		CONFLICT_SWEEP_FIXED=$(( CONFLICT_SWEEP_FIXED + 1 ))
		tg_send_msg "Standalone PR #${S_PR} has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${S_PR}")" "WARNING" >/dev/null 2>&1 || true
	elif [ "${_dispatch_rc}" -eq 2 ]; then
		echo "  PR #${S_PR}: autofix already in progress, skipping dispatch."
	else
		tg_send_msg "Standalone PR #${S_PR} has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${S_PR}")" "WARNING" >/dev/null 2>&1 || true
	fi
done

echo "Standalone conflict sweep complete. Fixed: ${CONFLICT_SWEEP_FIXED}."
