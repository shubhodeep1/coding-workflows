#!/usr/bin/env bash
# orchestrate_poll_process.sh — Process active orchestrator tracking issues.
# Extracted from orchestrate_poll.yml to stay within GitHub Actions
# expression length limits (21 000 chars max per run block).
#
# Required env vars (set by the workflow step):
#   RUNTIME_DIR, STATE_FILE, JUDGE_PROMPT_FILE, JUDGE_OUTPUT_FILE,
#   GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY,
#   MODEL_EDITOR, MODEL_REASONING_EFFORT_JUDGE,
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID, TOOL_CALL_BUDGET_JUDGE

set -euo pipefail

# ---------------------------------------------------------------
# Helper: Telegram (tracked via tg_helpers.sh)
# ---------------------------------------------------------------
# shellcheck source=gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/gh_helpers.sh
fi
if ! type emit_event >/dev/null 2>&1; then
  emit_event() { return 0; }
fi
# shellcheck source=tg_helpers.sh
if [ -f "scripts/tg_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/tg_helpers.sh
fi
# shellcheck source=memory_helpers.sh
if [ -f "scripts/memory_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/memory_helpers.sh
fi
if [ -f "scripts/transcript_archive.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/transcript_archive.sh 2>/dev/null || true
fi
if ! type archive_transcript >/dev/null 2>&1; then
  archive_transcript() { return 0; }
fi
if [ -f "scripts/nag_reminder.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/nag_reminder.sh 2>/dev/null || true
fi
# shellcheck source=pr_checks_lib.sh
# Shared PR check-runs merge gate (_pr_checks_completed /
# _pr_required_check_names_for_base). Single source of truth shared with
# scripts/review_rb_judge.sh so the orchestrator's merge gates and the
# review-blocked judge's gate can never drift. Resolve relative to this
# script's own directory first (robust when invoked or sourced from any
# CWD — e.g. the test harness sources this script by absolute path), then
# fall back to the CWD-relative staged path. A missing lib leaves
# _pr_checks_completed undefined, so every gate call fails closed (no
# merge) — the safe direction.
_OPP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "scripts")"
if [ -f "${_OPP_LIB_DIR}/pr_checks_lib.sh" ]; then
  # shellcheck disable=SC1091
  source "${_OPP_LIB_DIR}/pr_checks_lib.sh"
elif [ -f "scripts/pr_checks_lib.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/pr_checks_lib.sh
fi
unset _OPP_LIB_DIR
# shellcheck source=scripts/semble_helpers.sh
SEMBLE_HELPERS_AVAILABLE="false"
JUDGE_SEMBLE_MAX_CHUNKS="4"
JUDGE_SEMBLE_QUERY_MAX_BYTES="12000"
JUDGE_SEMBLE_CONTEXT_MAX_BYTES="12000"
if [ -f "scripts/semble_helpers.sh" ]; then
  # shellcheck disable=SC1091
  if source scripts/semble_helpers.sh; then
    if type semble_query_block >/dev/null 2>&1; then
      SEMBLE_HELPERS_AVAILABLE="true"
    else
      echo "::warning::scripts/semble_helpers.sh did not provide semble_query_block; continuing without Semble judge context." >&2
    fi
  else
    echo "::warning::Failed to source scripts/semble_helpers.sh; continuing without Semble judge context." >&2
  fi
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
if ! command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
  sanitize_codex_prompt_file() { :; }
fi
if ! command -v nag_reminder_enabled >/dev/null 2>&1; then
  nag_reminder_enabled() { return 1; }
fi
if ! command -v nag_silent_round_threshold >/dev/null 2>&1; then
  nag_silent_round_threshold() { printf '3\n'; }
fi
if ! command -v maybe_inject_nag >/dev/null 2>&1; then
  maybe_inject_nag() { return 0; }
fi

worktree_registry_enabled() {
	_is_truthy "${ORCH_WORKTREE_REGISTRY_ENABLED:-false}"
}

worktree_registry_register() {
	local name="${1:-}"
	local path="${2:-}"
	local branch="${3:-}"
	local task_id="${4:-}"
	local owner_phase="${5:-orchestrate-poll}"

	worktree_registry_enabled || return 0
	[ -n "${name}" ] || return 0
	[ -f "scripts/worktree_registry.sh" ] || return 0
	bash scripts/worktree_registry.sh register "${name}" "${path}" "${branch}" "${task_id}" "${owner_phase}" || true
}

worktree_registry_deregister() {
	local name="${1:-}"

	worktree_registry_enabled || return 0
	[ -n "${name}" ] || return 0
	[ -f "scripts/worktree_registry.sh" ] || return 0
	bash scripts/worktree_registry.sh deregister "${name}" || true
}

extract_judge_json_with_status() {
  local output_file="$1"
  local parsed_json=""

  [ -s "${output_file}" ] || return 0
  parsed_json="$(PYTHONDONTWRITEBYTECODE=1 python3 - "${output_file}" <<'PY' 2>/dev/null || true
import json
import re
import sys
from pathlib import Path


raw = Path(sys.argv[1]).read_text(encoding="utf-8")


def emit_if_valid(candidate: str) -> bool:
    if not candidate:
        return False
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return False
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, str) or not status:
        return False
    json.dump(data, sys.stdout)
    return True


if emit_if_valid(raw.strip()):
    raise SystemExit(0)

cleaned = re.sub(r"```(?:json)?\s*", "", raw)
cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for idx, ch in enumerate(cleaned):
    if ch == "{":
        if brace_depth == 0:
            start = idx
        brace_depth += 1
    elif ch == "}":
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            if emit_if_valid(cleaned[start:idx + 1]):
                raise SystemExit(0)
            start = None

raise SystemExit(1)
PY
 )"
  printf '%s' "${parsed_json}"
}

append_judge_semble_query_text() {
  local label="$1"
  local text="${2:-}"
  local max_bytes="${3:-2048}"
  local truncated_text=""

  [ -n "${text}" ] || return 0

  truncated_text="${text:0:${max_bytes}}"

  printf '%s\n' "${label}"
  printf '%s' "${truncated_text}"
  printf '\n'
}

render_judge_semble_prefetch_from_query_file() {
  local query_file="$1"
  local header_label="${2:-Judge Context}"
  local max_chunks="${3:-${JUDGE_SEMBLE_MAX_CHUNKS}}"
  local query_text=""
  local prefetch_text=""

  if [ "${SEMBLE_HELPERS_AVAILABLE}" != "true" ] \
    || [ "${SEMBLE_AVAILABLE:-false}" != "true" ] \
    || [ "${SEMBLE_INDEX_AVAILABLE:-false}" != "true" ] \
    || [ ! -s "${query_file}" ]; then
    return 0
  fi

  query_text="$(cat "${query_file}" 2>/dev/null || true)"
  query_text="${query_text:0:${JUDGE_SEMBLE_QUERY_MAX_BYTES}}"
  [ -n "${query_text}" ] || return 0

  prefetch_text="$(semble_query_block "${query_text}" "${max_chunks}" "${header_label}" || true)"
  [ -n "${prefetch_text}" ] || return 0

  printf '%s\n' "${prefetch_text:0:${JUDGE_SEMBLE_CONTEXT_MAX_BYTES}}"
}

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

emit_judge_lessons_learned_records() {
  local source_name="${1:-}"
  local issue_number="${2:-}"
  local pr_number="${3:-}"
  local judge_json="${4:-}"
  local telemetry_json=""

  if ! is_truthy "${AI_MEMORY_ENABLED:-true}" || ! is_truthy "${LESSONS_LEARNED_ENABLED:-true}"; then
    return 0
  fi
  [ -n "${judge_json}" ] || return 0
  if ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi

  telemetry_json="$(printf '%s\n' "${judge_json}" | {
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="${PWD}/scripts${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "${PWD}" "${source_name}" "${issue_number}" "${pr_number}" <<'PY'
import json
import os
import sys
from pathlib import Path

from ai_memory_lib import persist_memory_operation, record_lessons_learned, resolve_memory_root_dir


def safe_int(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


repo_root = Path(sys.argv[1]).resolve()
source_name = str(sys.argv[2] or "judge").strip() or "judge"
issue_number = safe_int(sys.argv[3])
pr_number = safe_int(sys.argv[4])
memory_branch = str(os.environ.get("AI_MEMORY_BRANCH", "ai-memory") or "ai-memory").strip() or "ai-memory"
memory_root_relative = str(os.environ.get("AI_MEMORY_ROOT", "ai-memory") or "ai-memory").strip() or "ai-memory"
push_retries = safe_int(os.environ.get("AI_MEMORY_PUSH_RETRIES")) or 16

payload = json.loads(sys.stdin.read())
lessons_raw = payload.get("lessons_learned") if isinstance(payload, dict) else None
if lessons_raw is None:
    lessons = []
elif not isinstance(lessons_raw, list):
    raise ValueError("lessons_learned must be an array when present")
else:
    lessons = lessons_raw

telemetry = {
    "op": "write_lessons_learned",
    "ok": True,
    "phase": "judge",
    "source": source_name,
    "issue_number": issue_number,
    "pr_number": pr_number,
    "count": 0,
    "did_push": False,
}

if lessons:
    def operation(clone_dir: Path) -> dict[str, object]:
        memory_root = resolve_memory_root_dir(clone_dir, memory_root_relative)
        records = record_lessons_learned(
            memory_root,
            issue_number=issue_number,
            pr_number=pr_number,
            phase="judge",
            lessons=lessons,
        )
        return {"records": records}

    result = persist_memory_operation(
        repo_root,
        memory_branch=memory_branch,
        memory_root_relative=memory_root_relative,
        push_retries=push_retries,
        commit_message=f"ai-memory: record lessons learned [judge {source_name}]",
        operation=operation,
    )
    records = (result.get("operation_result") or {}).get("records") or []
    telemetry["count"] = len(records)
    telemetry["did_push"] = bool(result.get("did_push", False))

print(json.dumps(telemetry, ensure_ascii=True, sort_keys=True))
PY
  } 2>&1)" || {
    echo "::warning::${source_name} lessons-learned write failed; continuing fail-open" >&2
    printf 'AI_MEMORY_TELEMETRY: {"count":0,"fail_open":true,"ok":false,"op":"write_lessons_learned","phase":"judge","source":"%s"}\n' "${source_name}" >&2
    return 0
  }

  [ -n "${telemetry_json}" ] && printf 'AI_MEMORY_TELEMETRY: %s\n' "${telemetry_json}" >&2
}

_state_snapshot_json_object_or_empty() {
	local payload="${1:-}"

	if [ -n "${payload}" ] && printf '%s' "${payload}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		printf '%s' "${payload}" | jq -c . 2>/dev/null || printf '{}'
		return 0
	fi

	printf '{}'
}

write_state_snapshot_actions_runs_export() {
	local empty_blob='{"workflow_runs":[]}'
	local actions_runs_blob="${_ACTIONS_RUNS_BLOB_CACHE:-${empty_blob}}"

	[ -n "${RUNTIME_DIR:-}" ] || return 0
	local out_file="${RUNTIME_DIR}/state_snapshot_actions_runs.json"
	if ! printf '%s' "${actions_runs_blob}" | jq -e 'type == "object" and (.workflow_runs? | type == "array")' >/dev/null 2>&1; then
		actions_runs_blob="${empty_blob}"
	fi

	mkdir -p "${RUNTIME_DIR}" 2>/dev/null || return 0
	printf '%s\n' "${actions_runs_blob}" > "${out_file}" 2>/dev/null || true
}

write_state_snapshot_tracker_export() {
	local tracking_num="${1:-}"
	local tracking_title="${2:-}"
	local trackers_dir out_file
	local state_json wave_status_json labels_json issue_states_json pr_states_json candidate_details_json
	local runtime_blocker_enabled="false"
	local ledger_substates_enabled="false"

	[ -n "${RUNTIME_DIR:-}" ] || return 0
	[[ "${tracking_num}" =~ ^[0-9]+$ ]] || return 0

	trackers_dir="${RUNTIME_DIR}/state_snapshot_trackers"
	out_file="${trackers_dir}/tracking_${tracking_num}.json"
	state_json="$(_state_snapshot_json_object_or_empty "$(cat "${STATE_FILE}" 2>/dev/null || printf '{}')")"
	wave_status_json="$(_state_snapshot_json_object_or_empty "${WAVE_STATUS:-}")"
	labels_json="$(_state_snapshot_json_object_or_empty "${LABELS_JSON:-}")"
	issue_states_json="$(_state_snapshot_json_object_or_empty "${ISSUE_STATES_JSON:-}")"
	pr_states_json="$(_state_snapshot_json_object_or_empty "${PR_STATES_JSON:-}")"
	candidate_details_json="$(_state_snapshot_json_object_or_empty "${_current_wave_details_json:-}")"

	if [ "${RUNTIME_BLOCKER_CHECK_ENABLED:-false}" = "true" ]; then
		runtime_blocker_enabled="true"
	fi
	if is_truthy "${LEDGER_SUBSTATES_ENABLED:-true}"; then
		ledger_substates_enabled="true"
	fi

	mkdir -p "${trackers_dir}" 2>/dev/null || return 0
	jq -cn \
		--argjson number "${tracking_num}" \
		--arg title "${tracking_title}" \
		--argjson state "${state_json}" \
		--argjson wave_status "${wave_status_json}" \
		--argjson labels_json "${labels_json}" \
		--argjson issue_states_json "${issue_states_json}" \
		--argjson pr_states_json "${pr_states_json}" \
		--argjson candidate_details_json "${candidate_details_json}" \
		--argjson runtime_blocker_check_enabled "${runtime_blocker_enabled}" \
		--argjson ledger_substates_enabled "${ledger_substates_enabled}" \
		'{
			number: $number,
			title: $title,
			state: $state,
			wave_status: $wave_status,
			labels_json: $labels_json,
			issue_states_json: $issue_states_json,
			pr_states_json: $pr_states_json,
			candidate_details_json: $candidate_details_json,
			runtime_blocker_check_enabled: $runtime_blocker_check_enabled,
			ledger_substates_enabled: $ledger_substates_enabled
		}' > "${out_file}" 2>/dev/null || true
}

assemble_judge_static_context() {
  local out_file="$1"
  local missing=""

  if [ ! -s unattended_system_instructions.md ]; then
    missing="unattended_system_instructions.md"
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
    cat unattended_system_instructions.md
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
    if [ -f probably_unnecessary_but_read_if_stuck.md ]; then
      echo "=== OVERFLOW REFERENCE ==="
      echo "If you cannot make progress without operator-runbook details (env var reference, autofix retrigger/dedup internals, orchestrator integration-sync auto-heal, validation self-healing, workflow log analysis pipeline, semantic cache scope, wrapper pin policy), read ./probably_unnecessary_but_read_if_stuck.md from the working tree before bailing."
      echo
    fi
  } > "${out_file}"
}

# ---------------------------------------------------------------
# _pr_checks_completed / _pr_required_check_names_for_base now live in
# scripts/pr_checks_lib.sh (sourced near the top of this file) so the
# orchestrator's merge gates and scripts/review_rb_judge.sh's
# review-blocked judge gate share one definition and can never drift.
# The required-checks resolution (branch protection ∪
# ORCH_FINAL_MERGE_REQUIRED_CHECKS ∪ built-in default) and the "*"=legacy /
# ""=allow-all sentinels are documented there.
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Layer 2: time-bounded ineligibility alerting.
#
# finalize_integration_merge_if_needed sets FINAL_MERGE_BUDGET_ELIGIBLE=0
# for "transient" merge deferrals — required checks not complete yet,
# mergeability still computing, mergeable=false (conflict-resolver
# handling), etc. Those deferrals never increment final_merge_attempt_count,
# so MAX_FINAL_MERGE_ATTEMPTS=3 will never escalate them to ai:blocked.
# Without an alert path the orchestrator can sit in that deferral loop
# indefinitely while a single third-party advisory check (e.g. a Copilot
# review failure) keeps the merge gate closed — the silent-loop failure
# mode described in shubhodeep1/coding-workflows#2955.
#
# These helpers attach a per-SHA clock to the deferral so an alert fires
# exactly once after ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS hours. The
# clock resets when:
#   - The final PR's head SHA changes (autofix push) — fresh autofix
#     attempt deserves a fresh window.
#   - A finalize attempt succeeds (the merge lands).
#   - The state-file ineligibility keys are cleared by an operator.
#
# Auto-escalation to ai:blocked is INTENTIONALLY not implemented — per
# operator decision, this layer is alert-only. The CRITICAL Telegram
# alert plus tracking comment surfaces the stall; a human or a future
# Fix 6 (`ai:force-merge` handler) bypass is the resolution.
# ---------------------------------------------------------------

# Helper: fire (idempotently per SHA) a CRITICAL alert when the
# budget-ineligible deferral path has persisted for at least
# ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS hours on the same final PR
# head SHA. Called from mark_validation_complete after the
# budget-ineligible early-return is detected. State-file writes are
# fail-open: jq/mv errors are swallowed so a transient FS issue doesn't
# block the orchestrator loop.
_check_final_merge_ineligibility_alert()
{
	local alert_hours="${ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS:-0}"
	if [ "${alert_hours}" = "0" ]; then
		return 0
	fi

	local final_pr
	final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
	if [ -z "${final_pr}" ] || [ "${final_pr}" = "null" ]; then
		return 0
	fi

	local pr_json current_sha _final_merge_ineligibility_base_ref
	pr_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" 2>/dev/null || echo "")"
	current_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' 2>/dev/null | tail -n1)"
	# Reuse the same PR payload inside _summarize_final_merge_blockers so the
	# alert path does not burn a second PR-metadata round-trip for .base.ref.
	_final_merge_ineligibility_base_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .base.ref?) then .base.ref else empty end' 2>/dev/null | tail -n1)"
	if [ -z "${current_sha}" ] || [ "${current_sha}" = "null" ]; then
		return 0
	fi

	local now_utc
	now_utc="$(date -u +%s)"

	local prev_sha prev_first_blocked_at prev_alert_sent_for_sha
	prev_sha="$(jq -r '.final_merge_ineligible_blocked_at_sha // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
	prev_first_blocked_at="$(jq -r '.final_merge_ineligible_first_blocked_at_utc // 0' "${STATE_FILE}" 2>/dev/null || echo "0")"
	prev_alert_sent_for_sha="$(jq -r '.final_merge_ineligible_alert_sent_for_sha // ""' "${STATE_FILE}" 2>/dev/null || echo "")"

	# Defensive: if jq returned non-numeric for the timestamp (state file
	# corruption / unexpected legacy value), treat as "clock just started".
	if ! [[ "${prev_first_blocked_at}" =~ ^[0-9]+$ ]]; then
		prev_first_blocked_at="0"
	fi

	if [ "${prev_sha}" != "${current_sha}" ]; then
		# New SHA (or first observation) — start the clock fresh, no alert.
		jq --arg sha "${current_sha}" --argjson now "${now_utc}" \
			'.final_merge_ineligible_blocked_at_sha = $sha
			 | .final_merge_ineligible_first_blocked_at_utc = $now
			 | .final_merge_ineligible_alert_sent_for_sha = ""' \
			"${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}" || true
		return 0
	fi

	local elapsed_secs threshold_secs
	elapsed_secs=$(( now_utc - prev_first_blocked_at ))
	if [ "${elapsed_secs}" -lt 0 ]; then
		elapsed_secs=0
	fi
	threshold_secs=$(( alert_hours * 3600 ))

	if [ "${elapsed_secs}" -lt "${threshold_secs}" ]; then
		return 0
	fi

	if [ "${prev_alert_sent_for_sha}" = "${current_sha}" ]; then
		# Already alerted for this SHA — once per SHA contract.
		return 0
	fi

	local elapsed_hours
	elapsed_hours=$(( elapsed_secs / 3600 ))

	local blocker_summary pr_url
	blocker_summary="$(_summarize_final_merge_blockers "${final_pr}" "${current_sha}")"
	pr_url="$(_gh_url "pull/${final_pr}")"

	local msg
	msg="⛔ Final integration merge stuck for ${elapsed_hours}h+ (project #${TRACKING_NUM})"
	msg+=$'\n'"PR: ${pr_url}"
	msg+=$'\n'"Head SHA: ${current_sha:0:7}"
	if [ -n "${blocker_summary}" ]; then
		msg+=$'\n'"Blockers: ${blocker_summary}"
	fi
	msg+=$'\n'"Resolution: re-run or dismiss the blocking check-run(s), merge manually, or set ORCH_FINAL_MERGE_REQUIRED_CHECKS to omit advisory checks."

	tg_notify "${msg}" "CRITICAL"

	local comment_body
	comment_body="## ⏰ Final merge blocked for ${elapsed_hours}h+"$'\n\n'
	comment_body+="The integration squash PR #${final_pr} (head \`${current_sha:0:7}\`) has been in the orchestrator's budget-ineligible deferral path for at least ${elapsed_hours}h. Each poll cycle logs \`[final-merge] budget-ineligible deferral/failure\` and the project never advances to \`status=complete\`."$'\n\n'
	if [ -n "${blocker_summary}" ]; then
		comment_body+="**Blocking check-runs:** ${blocker_summary}"$'\n\n'
	else
		comment_body+="**Blocking check-runs:** unknown — see PR check-runs."$'\n\n'
	fi
	comment_body+="**Resolution options:**"$'\n'
	comment_body+="- Re-run, dismiss, or fix the blocking check-run(s) so they return \`success\`/\`neutral\`/\`skipped\`/\`cancelled\`."$'\n'
	comment_body+="- Merge PR #${final_pr} manually via the GitHub UI (works when the base branch isn't protected)."$'\n'
	comment_body+="- Override the gate per-repo via \`ORCH_FINAL_MERGE_REQUIRED_CHECKS\` to exclude advisory checks."$'\n\n'
	comment_body+="This alert fires once per head SHA. A fresh push to PR #${final_pr} resets the clock and requires another ${alert_hours}h before re-alerting."$'\n\n'
	comment_body+="*(Threshold configurable via \`ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS\`, currently ${alert_hours}h; set to 0 to disable.)*"
	post_tracking_comment "${comment_body}" || true

	jq --arg sha "${current_sha}" \
		'.final_merge_ineligible_alert_sent_for_sha = $sha' \
		"${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}" || true

	echo "  [final-merge] FINAL_MERGE_INELIGIBILITY_ALERT_SENT pr=${final_pr} sha=${current_sha:0:7} elapsed=${elapsed_hours}h threshold=${alert_hours}h"
}

# Helper: best-effort short summary of which check-runs would block the
# final-merge gate on this SHA. Mirrors Layer 1's required-set filter so
# advisory failures (for example Copilot) are not mislabeled as blockers
# in the Layer 2 alert body. Returns the empty string on API failure (the
# alert still fires; the body just omits the blocker line).
_summarize_final_merge_blockers()
{
	local final_pr="$1"
	local head_sha="$2"
	local pr_json
	local base_ref="${_final_merge_ineligibility_base_ref:-}"
	local required_names_csv

	if [ -z "${base_ref}" ] && [[ "${final_pr}" =~ ^[0-9]+$ ]]; then
		pr_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" 2>/dev/null || echo "")"
		base_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .base.ref?) then .base.ref else empty end' 2>/dev/null | tail -n1)"
	fi
	required_names_csv="$(_pr_required_check_names_for_base "${base_ref}")"

	local check_runs_json
	check_runs_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${GITHUB_REPOSITORY}/commits/${head_sha}/check-runs?per_page=100" 2>/dev/null || echo "")"
	if [ -z "${check_runs_json}" ]; then
		echo ""
		return 0
	fi
	if [ "${required_names_csv}" = "" ]; then
		echo ""
		return 0
	fi

	if [ "${required_names_csv}" = "*" ]; then
		printf '%s' "${check_runs_json}" | jq -r '
			(
				if (type == "array") then
					[.[]? | (.check_runs // [])[]]
				elif (type == "object" and (.check_runs | type == "array")) then
					.check_runs
				else
					[]
				end
			)
			| map(select(.status != "completed" or (.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled")))
			| map("\(.name) (\(.conclusion // .status))")
			| unique
			| join(", ")
		' 2>/dev/null | tail -n1
		return 0
	fi

	printf '%s' "${check_runs_json}" | jq -r --arg names "${required_names_csv}" '
		($names | split(",") | map(gsub("^\\s+|\\s+$"; ""))) as $required |
		(
			if (type == "array") then
				[.[]? | (.check_runs // [])[]]
			elif (type == "object" and (.check_runs | type == "array")) then
				.check_runs
			else
				[]
			end
		)
		| map(select(
			(.status != "completed")
			or (
				(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled")
				and (.name as $n | $required | index($n))
			)
		))
		| map("\(.name) (\(.conclusion // .status))")
		| unique
		| join(", ")
	' 2>/dev/null | tail -n1
}

# Helper: clear the Layer 2 ineligibility tracking state. Called from the
# merge-success path in finalize_integration_merge_if_needed so a future
# stall on the same project starts a fresh clock.
_clear_final_merge_ineligibility_state()
{
	jq '.final_merge_ineligible_blocked_at_sha = ""
	    | .final_merge_ineligible_first_blocked_at_utc = 0
	    | .final_merge_ineligible_alert_sent_for_sha = ""' \
		"${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}" || true
}

# ---------------------------------------------------------------
# Helper: Pre-merge sibling-conflict probe.
#
# Before squash-merging a sub-PR into the integration branch, check
# whether merging it would textually conflict with any OTHER open
# sub-PR targeting the same integration branch.  This catches the
# common "both siblings edit README.md / orchestrate_poll_process.sh
# in the same wave" collision early, before the merge goes through
# and forces the loser into a conflict-resolution autofix loop.
#
# Implementation notes:
# - Zero GH API calls per probe: we fetch the open-PR list ONCE per
#   poll cycle (into _MERGE_PROBE_CACHE_JSON) and memoize. All cross-
#   checks use local `git merge-tree --write-tree --name-only` which
#   operates entirely on the fetched refs.
# - `git merge-tree --write-tree` (git ≥ 2.38) returns 0 on clean
#   merge and 1 when textual conflicts occur. With `--name-only` it
#   prints the written tree SHA followed by conflicting paths to
#   stdout; we defensively handle outputs that omit the SHA line.
#   We rely on both signals.
# - Siblings are fetched in a single batched `git fetch` per probe
#   cycle so the network cost is bounded by wave size.
# - MAX_MERGE_DEFERRALS caps how many cycles a single PR may be
#   deferred before the poller escalates via Telegram for human
#   review; the defer counter is stored in state on the wave entry
#   as `merge_deferral_count` (additive, backward compatible).
# ---------------------------------------------------------------
MAX_MERGE_DEFERRALS="${MAX_MERGE_DEFERRALS:-5}"
if ! [[ "${MAX_MERGE_DEFERRALS}" =~ ^[0-9]+$ ]] || [ "${MAX_MERGE_DEFERRALS}" -lt 1 ]; then
	echo "::warning::MAX_MERGE_DEFERRALS='${MAX_MERGE_DEFERRALS}' invalid; falling back to 5." >&2
	MAX_MERGE_DEFERRALS=5
fi

# Cache: JSON array of open PRs per integration branch, keyed by branch name.
# Populated lazily on first probe in each poll cycle.
declare -gA _MERGE_PROBE_CACHE_JSON=()
declare -gA _MERGE_PROBE_CACHE_FETCHED=()

# _merge_probe_refresh — populate the sibling-PR cache for a given
# integration branch. Performs exactly one `gh pr list` API call per
# integration branch per cycle and a single batched `git fetch` for
# all sibling head refs.
#
# Usage: _merge_probe_refresh <integration_branch>
_merge_probe_refresh()
{
	local integration_branch="$1"
	[ -n "${integration_branch}" ] || return 0

	if [ -n "${_MERGE_PROBE_CACHE_FETCHED[${integration_branch}]:-}" ]; then
		return 0
	fi

	local prs_json
	prs_json="$(gh_retry gh pr list \
		--repo "${GITHUB_REPOSITORY}" \
		--state open \
		--base "${integration_branch}" \
		--json number,headRefName,headRefOid \
		--limit 100 2>/dev/null || echo '[]')"
	if ! printf '%s' "${prs_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
		prs_json='[]'
	fi
	_MERGE_PROBE_CACHE_JSON["${integration_branch}"]="${prs_json}"

	# Batched fetch of all sibling head refs + the integration branch.
	# Ignore failures — a failed fetch means the probe will be best-effort.
	local refs=()
	while IFS= read -r ref; do
		[ -n "${ref}" ] && refs+=("${ref}")
	done < <(printf '%s' "${prs_json}" | jq -r '.[].headRefName // empty')
	refs+=("${integration_branch}")
	if [ "${#refs[@]}" -gt 0 ]; then
		git fetch --no-tags --quiet origin "${refs[@]}" 2>/dev/null || true
	fi

	_MERGE_PROBE_CACHE_FETCHED["${integration_branch}"]="1"
}

# probe_sibling_merge_conflicts — returns 0 when the candidate PR can
# be merged without conflicts against every other open sibling PR
# targeting the same integration branch, 1 when a conflict is
# detected. On conflict, prints the conflicting sibling PR number and
# the colliding paths to stdout for caller logging AND records a
# telemetry event on the ai-memory branch so the next orchestrator
# run's planner guard can promote these paths to "learned hot"
# without any human updating .github/ai/hot_files.json.
#
# Usage: probe_sibling_merge_conflicts <candidate_pr> <candidate_head_ref> <integration_branch> [<project_id>]
probe_sibling_merge_conflicts()
{
	local candidate_pr="$1"
	local candidate_head_ref="$2"
	local integration_branch="$3"
	local project_id="${4:-${TRACKING_NUM:-}}"

	if [ -z "${candidate_pr}" ] || [ -z "${candidate_head_ref}" ] || [ -z "${integration_branch}" ]; then
		# Missing inputs => probe cannot run; fall through to existing merge.
		return 0
	fi
	if ! command -v git >/dev/null 2>&1; then
		return 0
	fi
	# Availability check: run the modern merge-tree mode directly instead
	# of grepping help text, which exits non-zero under `set -o pipefail`
	# on many git builds when invoked without refs.
	if ! git merge-tree --write-tree --name-only --no-messages HEAD HEAD >/dev/null 2>&1; then
		echo "  [merge-probe] git merge-tree --write-tree unavailable; skipping probe for PR #${candidate_pr}."
		return 0
	fi

	_merge_probe_refresh "${integration_branch}"
	local prs_json="${_MERGE_PROBE_CACHE_JSON[${integration_branch}]:-[]}"

	local candidate_ref="refs/remotes/origin/${candidate_head_ref}"
	if ! git rev-parse --verify --quiet "${candidate_ref}" >/dev/null 2>&1; then
		# Candidate ref not present locally — try a one-off fetch.
		git fetch --no-tags --quiet origin "${candidate_head_ref}" 2>/dev/null || true
		if ! git rev-parse --verify --quiet "${candidate_ref}" >/dev/null 2>&1; then
			echo "  [merge-probe] Cannot locate candidate ref ${candidate_ref}; skipping probe."
			return 0
		fi
	fi

	local any_conflict=0
	local sibling_entries
	sibling_entries="$(printf '%s' "${prs_json}" | jq -c '.[]')"
	while IFS= read -r entry; do
		[ -n "${entry}" ] || continue
		local sib_num sib_ref
		sib_num="$(printf '%s' "${entry}" | jq -r '.number // empty')"
		sib_ref="$(printf '%s' "${entry}" | jq -r '.headRefName // empty')"
		[ -n "${sib_num}" ] || continue
		[ -n "${sib_ref}" ] || continue
		# Skip self
		if [ "${sib_num}" = "${candidate_pr}" ]; then
			continue
		fi
		local sib_local_ref="refs/remotes/origin/${sib_ref}"
		if ! git rev-parse --verify --quiet "${sib_local_ref}" >/dev/null 2>&1; then
			# The cache fetch didn't get this one; try once more, then skip.
			git fetch --no-tags --quiet origin "${sib_ref}" 2>/dev/null || true
			if ! git rev-parse --verify --quiet "${sib_local_ref}" >/dev/null 2>&1; then
				continue
			fi
		fi
		local conflicts_out
		if conflicts_out="$(git merge-tree --write-tree --name-only --no-messages "${candidate_ref}" "${sib_local_ref}" 2>/dev/null)"; then
			# Exit 0 => clean merge
			continue
		fi
		# Non-zero => conflict. Most git versions print the written tree
		# SHA on the first line; some may emit only paths. Strip the first
		# line only when it looks like an object ID.
		local conflict_paths
		conflict_paths="$(printf '%s\n' "${conflicts_out}" | sed '/^$/d')"
		if printf '%s\n' "${conflict_paths}" | head -n1 | grep -Eq '^[[:xdigit:]]{40}([[:xdigit:]]{24})?$'; then
			conflict_paths="$(printf '%s\n' "${conflict_paths}" | awk 'NR==1{next} {print}')"
		fi
		if [ -z "${conflict_paths}" ]; then
			# Older git: conflict information may appear on the first line
			# already. Treat the whole output as conflict context.
			conflict_paths="${conflicts_out}"
		fi
		echo "  [merge-probe] PR #${candidate_pr} conflicts with sibling PR #${sib_num} on paths:"
		printf '      %s\n' "${conflict_paths}" | sed 's/^      $//' | sed '/^$/d'
		any_conflict=1

		# Append a telemetry event to the ai-memory branch so the NEXT
		# orchestrator run's planner can auto-promote these paths to
		# the effective hot-file set. Fail-open: best-effort only,
		# never blocks the defer path below.
		local -a _tel_paths=()
		while IFS= read -r _tp; do
			[ -n "${_tp}" ] || continue
			_tel_paths+=("${_tp}")
		done <<< "${conflict_paths}"
		if [ "${#_tel_paths[@]}" -gt 0 ]; then
			_record_merge_conflict_telemetry "${project_id}" "${candidate_pr}" "${sib_num}" "${_tel_paths[@]}" || true
		fi
		# Keep scanning so the log captures all colliding siblings for
		# this cycle; early-return is fine if we only care about the
		# boolean.
	done <<< "${sibling_entries}"

	if [ "${any_conflict}" -eq 1 ]; then
		return 1
	fi
	return 0
}

# _record_merge_conflict_telemetry — append a conflict-detection
# event to the append-only JSONL at
# ``<AI_MEMORY_ROOT>/orchestrator/merge_conflicts.jsonl`` on the
# ``ai-memory`` branch. Used by the orchestrate.yml planner step on
# the NEXT run to automatically learn which files are hot across
# projects, without any human updating .github/ai/hot_files.json.
#
# Contract: fail-open (telemetry is best-effort; never blocks the
# merge flow). Zero GitHub API calls — uses git protocol via a
# throwaway worktree on the ai-memory branch. Concurrency-safe via
# fetch+retry; if two poller runs push simultaneously, the loser
# retries up to 3 times before giving up.
#
# Usage:
#   _record_merge_conflict_telemetry <project_id> <pr_a> <pr_b> <path> [<path>...]
_record_merge_conflict_telemetry()
{
	local project="${1:-}"
	local pr_a="${2:-}"
	local pr_b="${3:-}"
	shift 3 || true
	local -a paths=("$@")
	[ "${#paths[@]}" -gt 0 ] || return 0

	# Respect the existing memory kill switch.
	if ! type _memory_enabled >/dev/null 2>&1; then
		return 0
	fi
	_memory_enabled || return 0

	# Ensure branch exists (idempotent; no-op when already present).
	if type memory_ensure_branch >/dev/null 2>&1; then
		memory_ensure_branch >/dev/null 2>&1 || return 0
	fi

	local branch="${AI_MEMORY_BRANCH:-ai-memory}"
	local mem_root="${AI_MEMORY_ROOT:-ai-memory}"
	local runtime_dir="${RUNTIME_DIR:-/tmp}"
	local wt="${runtime_dir}/ai-memory-wt-$$-${RANDOM:-0}"

	local ts paths_json record_json
	ts="$(date -u +%s)"
	paths_json="$(printf '%s\n' "${paths[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')"
	record_json="$(jq -cn \
		--argjson ts "${ts}" \
		--arg project "${project}" \
		--arg pr_a "${pr_a}" \
		--arg pr_b "${pr_b}" \
		--argjson paths "${paths_json}" \
		'{ts: $ts, project: $project, pr_a: $pr_a, pr_b: $pr_b, paths: $paths}' 2>/dev/null || echo "")"
	[ -n "${record_json}" ] || return 0

	local attempt
	for attempt in 1 2 3; do
		rm -rf "${wt}" 2>/dev/null || true
		if ! git fetch --quiet origin "${branch}:refs/remotes/origin/${branch}" 2>/dev/null; then
			# Branch may not exist yet despite memory_ensure_branch; fall
			# through and try to create the worktree from an orphan.
			:
		fi
		if git rev-parse --verify --quiet "refs/remotes/origin/${branch}" >/dev/null 2>&1; then
			if ! git worktree add --quiet -B "${branch}" "${wt}" "refs/remotes/origin/${branch}" 2>/dev/null; then
				sleep 1
				continue
			fi
			worktree_registry_register "$(basename -- "${wt}")" "${wt}" "${branch}" "project-${project}" "orchestrate-poll"
		else
			# Orphan init: produce a fresh branch locally.
			if ! git worktree add --quiet --detach "${wt}" "$(git rev-parse HEAD 2>/dev/null || echo "")" 2>/dev/null; then
				return 0
			fi
			worktree_registry_register "$(basename -- "${wt}")" "${wt}" "${branch}" "project-${project}" "orchestrate-poll"
			( cd "${wt}" && git checkout --quiet --orphan "${branch}" && git rm -rf --quiet . 2>/dev/null || true ) || true
		fi
		mkdir -p "${wt}/${mem_root}/orchestrator"
		printf '%s\n' "${record_json}" >> "${wt}/${mem_root}/orchestrator/merge_conflicts.jsonl"
		local push_rc=0
		(
			cd "${wt}"
			git config user.name "codex-bot"
			git config user.email "codex@users.noreply.github.com"
			git add "${mem_root}/orchestrator/merge_conflicts.jsonl" || exit 1
			if git diff --cached --quiet; then
				exit 0
			fi
			git commit --quiet -m "orchestrator: merge-conflict telemetry (${project}/${pr_a}↔${pr_b})" 2>/dev/null || exit 1
			git push --quiet origin "${branch}:${branch}" 2>/dev/null || exit 2
		) || push_rc=$?
		worktree_registry_deregister "$(basename -- "${wt}")"
		git worktree remove --force "${wt}" 2>/dev/null || true
		if [ "${push_rc}" -eq 0 ]; then
			echo "  [merge-probe] telemetry append: recorded conflict for ${project} PR #${pr_a}↔#${pr_b} (${#paths[@]} path(s)) to ${branch}/${mem_root}/orchestrator/merge_conflicts.jsonl"
			return 0
		fi
		if [ "${push_rc}" -eq 2 ] && [ "${attempt}" -lt 3 ]; then
			# Push race; sleep briefly and retry with fresh fetch.
			sleep 1
			continue
		fi
		return 0
	done
	return 0
}

# _bump_merge_deferral_count — increment the per-wave-issue
# merge-deferral counter in state and return the new count.
#
# Usage: _bump_merge_deferral_count <wave_idx> <github_issue>
_bump_merge_deferral_count()
{
	local wave_idx="$1"
	local gh_issue="$2"
	[[ "${wave_idx}" =~ ^[0-9]+$ ]] || return 0
	[[ "${gh_issue}" =~ ^[0-9]+$ ]] || return 0
	[ -f "${STATE_FILE}" ] || return 0
	local tmp
	tmp="${STATE_FILE}.merge_defer.tmp"
	if jq --argjson wi "${wave_idx}" --argjson gi "${gh_issue}" '
		(.waves[$wi].issues[] | select(.github_issue == $gi)) |= (
			.merge_deferral_count = ((.merge_deferral_count // 0) + 1)
		)
	' "${STATE_FILE}" > "${tmp}" 2>/dev/null; then
		mv "${tmp}" "${STATE_FILE}"
	else
		rm -f "${tmp}" 2>/dev/null || true
	fi
	jq --argjson wi "${wave_idx}" --argjson gi "${gh_issue}" -r \
		'.waves[$wi].issues[] | select(.github_issue == $gi) | .merge_deferral_count // 0' \
		"${STATE_FILE}" 2>/dev/null | head -n1
}

# _sync_integration_and_rebase_subissue — pre-merge alignment helper.
#
# Before squash-merging a ready-to-merge sub-issue PR into an
# orchestrator/project-* integration branch, opportunistically:
#   (1) merge the default branch into the integration branch when
#       it can be done without textual conflicts, so the integration
#       branch is kept current with main between sub-issue merges
#       (this prevents the integration->main drift that produces the
#       large fingerprint-regression set the integration-sync
#       resolver fails to converge on); and
#   (2) rebase the sub-issue head onto the (now possibly-updated)
#       integration tip so the squash-merge captures only the
#       sub-issue's own intent, never main's content as a side
#       effect.  This keeps fingerprint capture (which uses
#       GitHub's three-dot PR diff at capture_intent_fingerprints_
#       for_merged_subissue) accurate.
#
# Both steps are best-effort and fail open: when step (1) would
# require textual conflict resolution it is skipped (review_autofix
# integration-sync still handles that on its own trigger), and when
# step (2) hits conflicts the merge is deferred via the caller's
# existing _bump_merge_deferral_count flow.  Uses git only — zero
# GH API calls.
#
# Returns:
#   0 — already aligned (no push happened, or alignment was a no-op);
#       caller should proceed with the squash-merge this tick.
#   1 — sub-issue rebase produced conflicts; caller should defer the
#       merge AND bump _bump_merge_deferral_count (counts toward the
#       MAX_MERGE_DEFERRALS budget).
#   2 — alignment force-pushed a new SHA on the integration branch
#       and/or the sub-issue head; caller should defer the merge for
#       one tick so the new SHA's CI can re-run before squash-merge
#       fires.  Branch protection's required-checks gate is
#       server-side and lags the force-push by seconds, and the
#       non-`--auto` fallback merge path can otherwise race ahead of
#       the new SHA's checks.  Defer on rc=2 does NOT consume the
#       _bump_merge_deferral_count budget — this is expected one-tick
#       latency, not a failure.
#
# Usage: _sync_integration_and_rebase_subissue <pr_num> <head_ref> <integration_branch>
_sync_integration_and_rebase_subissue()
{
	local pr_num="$1"
	local head_ref="$2"
	local integration_branch="$3"
	local _did_push=0

	if [ -z "${pr_num}" ] || [ -z "${head_ref}" ] || [ -z "${integration_branch}" ]; then
		return 0
	fi
	if ! command -v git >/dev/null 2>&1; then
		return 0
	fi

	local default_branch="${DEFAULT_BRANCH:-main}"
	if [ "${integration_branch}" = "${default_branch}" ]; then
		return 0
	fi

	git fetch --no-tags --quiet origin "${default_branch}" "${integration_branch}" "${head_ref}" 2>/dev/null || return 0

	local main_ref="refs/remotes/origin/${default_branch}"
	local int_ref="refs/remotes/origin/${integration_branch}"
	local head_full="refs/remotes/origin/${head_ref}"
	local r
	for r in "${main_ref}" "${int_ref}" "${head_full}"; do
		git rev-parse --verify --quiet "${r}" >/dev/null 2>&1 || return 0
	done

	local main_sha int_sha head_sha
	main_sha="$(git rev-parse "${main_ref}")"
	int_sha="$(git rev-parse "${int_ref}")"
	head_sha="$(git rev-parse "${head_full}")"

	# Step 1: opportunistic main -> integration sync.
	if ! git merge-base --is-ancestor "${main_sha}" "${int_sha}" 2>/dev/null; then
		local _ws=""
		_ws="$(mktemp -d -t premerge-int-sync.XXXXXX 2>/dev/null)" || _ws=""
		if [ -n "${_ws}" ] && git worktree add --quiet --detach "${_ws}" "${int_sha}" 2>/dev/null; then
			worktree_registry_register "$(basename -- "${_ws}")" "${_ws}" "${int_sha}" "pr-${pr_num}" "orchestrate-poll"
			if (cd "${_ws}" && \
				git -c user.email="orchestrator@coding-workflows" \
				    -c user.name="orchestrator" \
				    merge --no-edit --no-ff "${main_sha}" >/dev/null 2>&1); then
				local merged_sha
				merged_sha="$(git -C "${_ws}" rev-parse HEAD)"
				if git push --force-with-lease="${integration_branch}:${int_sha}" \
				    origin "${merged_sha}:refs/heads/${integration_branch}" >/dev/null 2>&1; then
					echo "  [premerge-rebase] ${integration_branch}: merged ${default_branch}@${main_sha:0:7} (pre-merge alignment)."
					int_sha="${merged_sha}"
					_did_push=1
				else
					echo "  [premerge-rebase] ${integration_branch}: opportunistic ${default_branch}-merge succeeded but force-push failed; skipping pre-merge alignment for PR #${pr_num}."
				fi
			else
				(cd "${_ws}" && git merge --abort >/dev/null 2>&1) || true
				echo "  [premerge-rebase] ${integration_branch}: ${default_branch} cannot merge cleanly (textual conflicts); skipping step 1 for PR #${pr_num}, integration-sync resolver will handle."
			fi
			worktree_registry_deregister "$(basename -- "${_ws}")"
			git worktree remove --force "${_ws}" >/dev/null 2>&1 || true
		elif [ -n "${_ws}" ]; then
			rm -rf "${_ws}" 2>/dev/null || true
		fi
	fi

	# Step 2: rebase sub-issue head onto integration tip.
	if git merge-base --is-ancestor "${int_sha}" "${head_sha}" 2>/dev/null; then
		if [ "${_did_push}" -eq 1 ]; then
			return 2
		fi
		return 0
	fi

	local _wh="" _rc=0 _tmp_branch="ai-premerge-rebase-${pr_num}-$$"
	_wh="$(mktemp -d -t premerge-rebase.XXXXXX 2>/dev/null)" || return 0
	# `git rebase` requires being on a branch — `git worktree add --detach`
	# leaves HEAD detached and would fail with "fatal: You are not
	# currently on a branch", which the soft-failure path would
	# misclassify as a rebase conflict and trigger an unnecessary
	# defer.  Use `-B` to create/reset a per-pid temp branch.
	if ! git worktree add --quiet -B "${_tmp_branch}" "${_wh}" "${head_sha}" 2>/dev/null; then
		rm -rf "${_wh}" 2>/dev/null || true
		git branch -D "${_tmp_branch}" >/dev/null 2>&1 || true
		return 0
	fi
	worktree_registry_register "$(basename -- "${_wh}")" "${_wh}" "${_tmp_branch}" "pr-${pr_num}" "orchestrate-poll"

	if (cd "${_wh}" && \
		git -c user.email="orchestrator@coding-workflows" \
		    -c user.name="orchestrator" \
		    rebase "${int_sha}" >/dev/null 2>&1); then
		local rebased_sha
		rebased_sha="$(git -C "${_wh}" rev-parse HEAD)"
		if git push --force-with-lease="${head_ref}:${head_sha}" \
		    origin "${rebased_sha}:refs/heads/${head_ref}" >/dev/null 2>&1; then
			echo "  [premerge-rebase] PR #${pr_num}: rebased ${head_ref} onto ${integration_branch}@${int_sha:0:7}."
			_did_push=1
		else
			echo "::warning::[premerge-rebase] PR #${pr_num}: rebase succeeded but force-push failed; proceeding without alignment."
		fi
	else
		(cd "${_wh}" && git rebase --abort >/dev/null 2>&1) || true
		echo "  [premerge-rebase] PR #${pr_num}: rebase of ${head_ref} onto ${integration_branch} hit conflicts; deferring merge."
		_rc=1
	fi

	worktree_registry_deregister "$(basename -- "${_wh}")"
	git worktree remove --force "${_wh}" >/dev/null 2>&1 || true
	git branch -D "${_tmp_branch}" >/dev/null 2>&1 || true
	if [ "${_rc}" -eq 0 ] && [ "${_did_push}" -eq 1 ]; then
		return 2
	fi
	return "${_rc}"
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

if is_truthy "${ALLOW_WORKFLOW_EDITS:-true}"; then
  ALLOW_WORKFLOW_EDITS="true"
else
  ALLOW_WORKFLOW_EDITS="false"
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

# Review-run freshness window for the in-flight / zombie guards.
#
# A review_autofix run legitimately stays in_progress far longer than the
# generic STALL_THRESHOLD_MINUTES: the codex-agent job in review_autofix.yml
# has timeout-minutes:240, and the editor's own budget
# (review_apply_fixes.sh JOB_TIMEOUT_SECS) is 180 min while its watchdog keeps
# extending idle windows whenever the model is still holding an API socket.
# A review running between STALL_THRESHOLD_MINUTES and that ceiling is still
# making progress, but the freshness gates in build_active_issue_set, the
# retrigger_review inline guard, and _direct_inflight_review_run_on_branch
# previously used STALL_THRESHOLD_MINUTES (120) as the cutoff — so they
# misclassified such a run as a hung zombie, dropped it from the active set,
# and let stall recovery push a destructive empty commit that trips the live
# run's AUTOFIX_PRE_EDITOR_STALE_BASE -> soft_exit and discards the whole
# review pass (the PR #3082 / issue #3081 "stuck 169m, attempt 2" loop).
#
# Default 250 ≈ the 240-min codex-agent job hard timeout plus a small buffer,
# so the window expires right around when GitHub force-terminates a genuinely
# hung job (after which the run is no longer in_progress and naturally stops
# blocking recovery).  Floored at STALL_THRESHOLD_MINUTES so it can never be
# *shorter* than the generic window and reintroduce the bug.
REVIEW_RUN_MAX_RUNTIME_MINUTES="${REVIEW_RUN_MAX_RUNTIME_MINUTES:-250}"
if ! [[ "${REVIEW_RUN_MAX_RUNTIME_MINUTES}" =~ ^[0-9]+$ ]] || [ "${REVIEW_RUN_MAX_RUNTIME_MINUTES}" -lt 1 ]; then
  echo "::warning::REVIEW_RUN_MAX_RUNTIME_MINUTES must be a positive integer; defaulting to 250"
  REVIEW_RUN_MAX_RUNTIME_MINUTES="250"
fi
if [ "${REVIEW_RUN_MAX_RUNTIME_MINUTES}" -lt "${STALL_THRESHOLD_MINUTES}" ]; then
  echo "::warning::REVIEW_RUN_MAX_RUNTIME_MINUTES (${REVIEW_RUN_MAX_RUNTIME_MINUTES}) is below STALL_THRESHOLD_MINUTES (${STALL_THRESHOLD_MINUTES}); raising it to the stall threshold so review runs are never treated as zombies sooner than other runs."
  REVIEW_RUN_MAX_RUNTIME_MINUTES="${STALL_THRESHOLD_MINUTES}"
fi

ACTIONS_RUNS_CACHE_TTL_SECONDS="${ACTIONS_RUNS_CACHE_TTL_SECONDS:-60}"
if ! [[ "${ACTIONS_RUNS_CACHE_TTL_SECONDS}" =~ ^[0-9]+$ ]] || [ "${ACTIONS_RUNS_CACHE_TTL_SECONDS}" -lt 1 ]; then
  echo "::warning::ACTIONS_RUNS_CACHE_TTL_SECONDS must be a positive integer; defaulting to 60"
  ACTIONS_RUNS_CACHE_TTL_SECONDS="60"
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
STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES="${STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES:-}"

_validate_phase_threshold STALL_THRESHOLD_NO_LABELS_MINUTES
_validate_phase_threshold STALL_THRESHOLD_CLARIFICATION_MINUTES
_validate_phase_threshold STALL_THRESHOLD_PLANNING_MINUTES
_validate_phase_threshold STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES
_validate_phase_threshold STALL_THRESHOLD_IMPLEMENTING_MINUTES
_validate_phase_threshold STALL_THRESHOLD_DONE_MINUTES
_validate_phase_threshold STALL_THRESHOLD_READY_TO_MERGE_MINUTES
_validate_phase_threshold STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES

# S2 event-idle kill mode makes the implement-phase run-age threshold an outer
# safety net (90 minutes by default) only when kill mode is actually active.
# Observe-only mode preserves the legacy global window unless an explicit
# implement override was provided.
normalize_stall_guard_thresholds() {
  CODEX_STALL_GUARD_ENABLED="${CODEX_STALL_GUARD_ENABLED:-false}"
  if is_truthy "${CODEX_STALL_GUARD_ENABLED}"; then
    CODEX_STALL_GUARD_ENABLED="true"
  else
    CODEX_STALL_GUARD_ENABLED="false"
  fi

  if [ -z "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ] && [ "${CODEX_STALL_GUARD_ENABLED}" = "true" ]; then
    STALL_THRESHOLD_IMPLEMENTING_MINUTES="90"
  fi
}

normalize_stall_guard_thresholds

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
  [ -n "${STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES:-}" ] && parts+=("\"ai:review-blocked\":${STALL_THRESHOLD_REVIEW_BLOCKED_MINUTES}")

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

# Phase-specific stall recovery cap for ai:done.
#
# Each ai:done "recovery" dispatches a fresh review-autofix run that itself
# takes ≥10 min, so the global 5× cap can hard-close a PR after ~25 minutes
# of cron ticks even when the autofix loop is making real progress.  The
# legitimate stall reason for ai:done is "PR not merging" — escalate via
# the ladder (retrigger_review → escalate_human) rather than terminating
# with skip.  Set MAX_STALL_RECOVERIES_DONE to a small value (e.g. 2) to
# reach escalate_human quickly, or leave at the high default to effectively
# avoid skip for typical runs (the cap is still finite — skip will fire
# after MAX_STALL_RECOVERIES_DONE attempts unless the value is raised).
MAX_STALL_RECOVERIES_DONE="${MAX_STALL_RECOVERIES_DONE:-99}"
if ! [[ "${MAX_STALL_RECOVERIES_DONE}" =~ ^[0-9]+$ ]] || [ "${MAX_STALL_RECOVERIES_DONE}" -lt 1 ]; then
  echo "::warning::MAX_STALL_RECOVERIES_DONE must be a positive integer; defaulting to 99 (effectively avoids skip for typical ai:done runs)"
  MAX_STALL_RECOVERIES_DONE="99"
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

ENABLE_STALL_HUMAN_TERMINALIZATION="${ENABLE_STALL_HUMAN_TERMINALIZATION:-false}"
if is_truthy "${ENABLE_STALL_HUMAN_TERMINALIZATION}"; then
  ENABLE_STALL_HUMAN_TERMINALIZATION="true"
else
  ENABLE_STALL_HUMAN_TERMINALIZATION="false"
fi

ENABLE_CLEAN_WAVE_JUDGE_SKIP="${ENABLE_CLEAN_WAVE_JUDGE_SKIP:-true}"
if is_truthy "${ENABLE_CLEAN_WAVE_JUDGE_SKIP}"; then
  ENABLE_CLEAN_WAVE_JUDGE_SKIP="true"
else
  ENABLE_CLEAN_WAVE_JUDGE_SKIP="false"
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

# ENABLE_STALL_MERGED_PR_GUARD — when true, early-phase stall recovery
# actions (retrigger_pipeline / auto_respond_clarify / retrigger_plan /
# auto_approve / retrigger_implement) double-check the issue's linked
# pull request state before firing a command.  If the most recent
# linked PR is MERGED, the action is skipped and the issue is tagged
# ai:merged so close_merged_issues_sweep can close it on the next
# cycle.  Introduced to stop /reclarify (and friends) from being
# posted on issues whose work is already merged but whose phase label
# got stripped (or whose "Closes #N" autolink never fired).  The
# linked-PR state is prefetched in a single batched GraphQL call per
# stall path, so the guard adds 0 additional per-issue REST calls on
# successful prefetch, but may fall back to per-issue REST (timeline
# + PR payload) on cache/prefetch miss.
# Default true; set to false to disable the guard entirely.
ENABLE_STALL_MERGED_PR_GUARD="${ENABLE_STALL_MERGED_PR_GUARD:-true}"
if is_truthy "${ENABLE_STALL_MERGED_PR_GUARD}"; then
  ENABLE_STALL_MERGED_PR_GUARD="true"
else
  ENABLE_STALL_MERGED_PR_GUARD="false"
fi

MAX_RECOVERY_ATTEMPTS="${MAX_RECOVERY_ATTEMPTS:-3}"
if ! [[ "${MAX_RECOVERY_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_RECOVERY_ATTEMPTS}" -lt 1 ]; then
  echo "::warning::MAX_RECOVERY_ATTEMPTS must be a positive integer; defaulting to 3"
  MAX_RECOVERY_ATTEMPTS="3"
fi

JUDGE_REPEAT_FINGERPRINT_MAX="${JUDGE_REPEAT_FINGERPRINT_MAX:-2}"
if ! [[ "${JUDGE_REPEAT_FINGERPRINT_MAX}" =~ ^[0-9]+$ ]] || [ "${JUDGE_REPEAT_FINGERPRINT_MAX}" -lt 1 ]; then
  echo "::warning::JUDGE_REPEAT_FINGERPRINT_MAX must be a positive integer; defaulting to 2"
  JUDGE_REPEAT_FINGERPRINT_MAX="2"
fi

MAX_VALIDATION_RECOVERY_ATTEMPTS="${MAX_VALIDATION_RECOVERY_ATTEMPTS:-2}"
if ! [[ "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" -lt 0 ]; then
  echo "::warning::MAX_VALIDATION_RECOVERY_ATTEMPTS must be a non-negative integer; defaulting to 2"
  MAX_VALIDATION_RECOVERY_ATTEMPTS="2"
fi

# Bounded retry budget for the post-validation final integration→default
# squash merge inside mark_validation_complete. Each poll tick that runs
# mark_validation_complete increments .final_merge_attempt_count when
# finalize_integration_merge_if_needed returns a budget-eligible failure;
# transient/budget-ineligible deferrals do not consume retry budget. On
# success the counter is reset. After the budget is exhausted the project is
# escalated to ai:blocked instead of being silently advanced to status=complete.
MAX_FINAL_MERGE_ATTEMPTS="${MAX_FINAL_MERGE_ATTEMPTS:-3}"
if ! [[ "${MAX_FINAL_MERGE_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_FINAL_MERGE_ATTEMPTS}" -lt 1 ]; then
  echo "::warning::MAX_FINAL_MERGE_ATTEMPTS must be a positive integer; defaulting to 3"
  MAX_FINAL_MERGE_ATTEMPTS="3"
fi


# ORCH_FINAL_MERGE_REQUIRED_CHECKS controls which check-runs the
# orchestrator's _pr_checks_completed gate treats as blocking when deciding
# whether to attempt the final integration→default squash merge inside
# finalize_integration_merge_if_needed.
#
# Resolution order inside _pr_checks_completed:
#   1. If branch protection on the PR's base ref exposes a non-empty
#      required_status_checks.contexts list, that list IS the gate;
#      advisory third-party checks (e.g. Copilot) not in that list are
#      ignored even when failing.
#   2. Otherwise the comma-separated names in this env var form the gate.
#   3. Otherwise the built-in default below — the five checks the
#      orchestrator already produces on every integration PR — is used.
#
# Sentinels:
#   "*"  — legacy fail-closed mode (ANY non-acceptable check-run blocks).
#   ""   — allow-all (no check-run blocks; relies entirely on GitHub
#           branch protection enforcement at merge time).
#
# Defaults preserve current behaviour on protected branches (the
# branch-protection contexts win) and add a sensible allowlist for the
# bitsafe-style "main is unprotected, third-party advisory check
# failure stalls the orchestrator" case that motivated this knob.
ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT="CI,Integration PR readiness check,Lint plan-archival completeness,Lint PR body for auto-close keywords against orchestrator-tracking issues,review / gate"
ORCH_FINAL_MERGE_REQUIRED_CHECKS="${ORCH_FINAL_MERGE_REQUIRED_CHECKS-${ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT}}"

# ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS bounds how long the orchestrator
# may sit in the budget-ineligible deferral path of
# finalize_integration_merge_if_needed (FINAL_MERGE_BUDGET_ELIGIBLE=0) on
# the same final PR head SHA before firing a CRITICAL Telegram alert plus
# tracking-issue comment. The alert fires exactly once per SHA; a fresh
# autofix push to the integration PR resets the clock.
#
# The default 6h matches the staleness threshold used elsewhere for
# integration-branch alerts. Set to 0 to disable the alert path entirely.
ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS="${ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS:-6}"
if ! [[ "${ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS}" =~ ^[0-9]+$ ]]; then
  echo "::warning::ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS must be a non-negative integer; defaulting to 6"
  ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS="6"
fi

# A value of 0 disables the stale-integration alert path entirely (parity
# with ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS=0); 0 is the workflow default
# (see .github/workflows/orchestrate_poll.yml). Any non-numeric value is
# rejected and falls back to the default 6.
ORCH_INTEGRATION_STALE_ALERT_HOURS="${ORCH_INTEGRATION_STALE_ALERT_HOURS:-6}"
if ! [[ "${ORCH_INTEGRATION_STALE_ALERT_HOURS}" =~ ^[0-9]+$ ]]; then
  echo "::warning::ORCH_INTEGRATION_STALE_ALERT_HOURS must be a non-negative integer; defaulting to 6"
  ORCH_INTEGRATION_STALE_ALERT_HOURS="6"
fi

ORCH_INTEGRATION_STALE_REALERT_HOURS="${ORCH_INTEGRATION_STALE_REALERT_HOURS:-12}"
if ! [[ "${ORCH_INTEGRATION_STALE_REALERT_HOURS}" =~ ^[0-9]+$ ]] || [ "${ORCH_INTEGRATION_STALE_REALERT_HOURS}" -lt 1 ]; then
  echo "::warning::ORCH_INTEGRATION_STALE_REALERT_HOURS must be a positive integer; defaulting to 12"
  ORCH_INTEGRATION_STALE_REALERT_HOURS="12"
fi

ORCH_INTEGRATION_MAX_AHEAD_COMMITS="${ORCH_INTEGRATION_MAX_AHEAD_COMMITS:-10}"
if ! [[ "${ORCH_INTEGRATION_MAX_AHEAD_COMMITS}" =~ ^[0-9]+$ ]] || [ "${ORCH_INTEGRATION_MAX_AHEAD_COMMITS}" -lt 1 ]; then
  echo "::warning::ORCH_INTEGRATION_MAX_AHEAD_COMMITS must be a positive integer; defaulting to 10"
  ORCH_INTEGRATION_MAX_AHEAD_COMMITS="10"
fi

# Headroom added on top of a project's planned sub-issue count when deriving
# the size-aware backpressure floor (see _integration_backpressure_effective_threshold).
# It absorbs the non-sub-issue commits a healthy project still accrues on its
# integration branch before the integration->default PR drains — main->integration
# syncs and a handful of judge-added fix-up issues. Non-negative integer.
ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN="${ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN:-5}"
if ! [[ "${ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN}" =~ ^[0-9]+$ ]]; then
  echo "::warning::ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN must be a non-negative integer; defaulting to 5"
  ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN="5"
fi

# Per-batch ceiling for "validation-fixing" poll cycles.  The fix-up loop
# iterates whatever issue numbers a validation workflow posted in its latest
# "Runtime validation found fixable issues" comment and waits for all of them
# to reach ai:merged (or a merged-PR-evidence backfill).  If any of those
# issues stalls open without progress, the loop previously had no self-imposed
# ceiling and relied entirely on the global stall-recovery path.  This knob
# bounds how many poll cycles a single fix batch can spend in progress before
# the poller declares the batch stalled and routes the project through
# mark_validation_failed (which still honours MAX_VALIDATION_RECOVERY_ATTEMPTS
# for the judge re-evaluation budget).
MAX_VALIDATION_FIX_BATCH_CYCLES="${MAX_VALIDATION_FIX_BATCH_CYCLES:-30}"
if ! [[ "${MAX_VALIDATION_FIX_BATCH_CYCLES}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATION_FIX_BATCH_CYCLES}" -lt 1 ]; then
  echo "::warning::MAX_VALIDATION_FIX_BATCH_CYCLES must be a positive integer; defaulting to 30"
  MAX_VALIDATION_FIX_BATCH_CYCLES="30"
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

# INTEGRATION_SYNC_CONFLICT_MAX_RETRIES is a tighter circuit-breaker
# applied ONLY to integration-branch sync conflicts (head ref matches
# orchestrator/project-*). The first-line conflict resolver
# (prompts/conflict-resolver.txt) has no built-in awareness of merged
# sub-issue intent — when it cannot reconcile two refactors of the same
# hunk, the safest default is to escalate to the integration judge
# quickly rather than burn three resolver dispatches that may "succeed"
# textually while silently dropping a merged sub-issue's intent.
#
# Default 1: one resolver shot, then the judge (which gets a much
# richer prompt with full PR context and the sub-issue intent rules).
# Set to a higher value to give the resolver more attempts before
# escalation. Set to 0 to skip the first-line resolver entirely and
# escalate to the judge immediately on the first conflict tick.
#
# This setting only takes effect when the integration branch's head
# ref matches orchestrator/project-*; non-integration conflict
# dispatch sites continue to honour INTEGRATION_CONFLICT_MAX_RETRIES.
INTEGRATION_SYNC_CONFLICT_MAX_RETRIES="${INTEGRATION_SYNC_CONFLICT_MAX_RETRIES:-1}"
if ! [[ "${INTEGRATION_SYNC_CONFLICT_MAX_RETRIES}" =~ ^[0-9]+$ ]]; then
  echo "::warning::INTEGRATION_SYNC_CONFLICT_MAX_RETRIES must be a non-negative integer; defaulting to 1"
  INTEGRATION_SYNC_CONFLICT_MAX_RETRIES="1"
fi

# INTEGRATION_CONFLICT_LIFETIME_MAX — global lifetime cap on the total
# number of resolver+judge dispatches per integration branch before the
# orchestrator gives up and flips status to "failed".  Unlike the
# per-burst INTEGRATION_*_CONFLICT_MAX_RETRIES counters (which reset to
# 0 after each judge escalation), this counter is additive across all
# dispatch episodes for the lifetime of the tracking-issue state and
# is only zeroed when the tracking-issue state itself is rebuilt.
# Prevents the multi-hour alternating resolver-judge loop observed on
# orchestrator/project-1479 (PR #1533) where every judge invocation
# reset unresolved_ticks but the merge stayed dirty as main moved.
INTEGRATION_CONFLICT_LIFETIME_MAX="${INTEGRATION_CONFLICT_LIFETIME_MAX:-10}"
if ! [[ "${INTEGRATION_CONFLICT_LIFETIME_MAX}" =~ ^[0-9]+$ ]] || [ "${INTEGRATION_CONFLICT_LIFETIME_MAX}" -lt 1 ]; then
  echo "::warning::INTEGRATION_CONFLICT_LIFETIME_MAX must be a positive integer; defaulting to 10"
  INTEGRATION_CONFLICT_LIFETIME_MAX="10"
fi

BRANCH_REBUILD_ENABLED="${BRANCH_REBUILD_ENABLED:-false}"
if is_truthy "${BRANCH_REBUILD_ENABLED}"; then
  BRANCH_REBUILD_ENABLED="true"
else
  BRANCH_REBUILD_ENABLED="false"
fi

BRANCH_REBUILD_THRESHOLD_HOURS="${BRANCH_REBUILD_THRESHOLD_HOURS:-24}"
if ! [[ "${BRANCH_REBUILD_THRESHOLD_HOURS}" =~ ^[0-9]+$ ]] || [ "${BRANCH_REBUILD_THRESHOLD_HOURS}" -lt 1 ]; then
  echo "::warning::BRANCH_REBUILD_THRESHOLD_HOURS must be a positive integer; defaulting to 24"
  BRANCH_REBUILD_THRESHOLD_HOURS="24"
fi

BRANCH_REBUILD_COOLDOWN_HOURS="${BRANCH_REBUILD_COOLDOWN_HOURS:-48}"
if ! [[ "${BRANCH_REBUILD_COOLDOWN_HOURS}" =~ ^[0-9]+$ ]] || [ "${BRANCH_REBUILD_COOLDOWN_HOURS}" -lt 1 ]; then
  echo "::warning::BRANCH_REBUILD_COOLDOWN_HOURS must be a positive integer; defaulting to 48"
  BRANCH_REBUILD_COOLDOWN_HOURS="48"
fi

# MAX_BUDGET_NEUTRAL_OVERRIDES caps how many times the retrigger_review
# stall recovery action may be rerouted to resolve_merge_conflict for a
# given PR head_sha without consuming a stall-recovery attempt. The
# rerouting itself is correct (an empty-commit push cannot resolve a
# dirty merge), but with no cap the loop runs unboundedly while main
# keeps moving. After this many overrides for the same head_sha, the
# orchestrator stops giving the conflict resolver a free pass and
# starts consuming budget so the stall ladder eventually reaches its
# terminal step.
MAX_BUDGET_NEUTRAL_OVERRIDES="${MAX_BUDGET_NEUTRAL_OVERRIDES:-2}"
if ! [[ "${MAX_BUDGET_NEUTRAL_OVERRIDES}" =~ ^[0-9]+$ ]]; then
  echo "::warning::MAX_BUDGET_NEUTRAL_OVERRIDES must be a non-negative integer; defaulting to 2"
  MAX_BUDGET_NEUTRAL_OVERRIDES="2"
fi

# MAX_JUDGE_REPLAY caps how many consecutive cached judge decisions the
# orchestrator will replay before forcing escalate_human. The judge
# decision cache (see invoke_stall_judge) keyed on
# sha256({issue_num, head_sha, phase, last_conclusion,
# recent_comments_hash}) skips the LLM call when the same inputs
# reproduce — but if the cached decision is wrong (the judge picked
# resolve_merge_conflict on a hot-file collision the resolver can't
# fix), replaying it forever wastes budget. After this many replays,
# the judge cache is bypassed and the issue is escalated.
MAX_JUDGE_REPLAY="${MAX_JUDGE_REPLAY:-2}"
if ! [[ "${MAX_JUDGE_REPLAY}" =~ ^[0-9]+$ ]]; then
  echo "::warning::MAX_JUDGE_REPLAY must be a non-negative integer; defaulting to 2"
  MAX_JUDGE_REPLAY="2"
fi

post_tracking_comment() {
  local comment_body="$1"
  local payload_file
  local payload_err_file
  local body_bytes
  body_bytes="$(printf '%s' "${comment_body}" | wc -c | tr -d '[:space:]')"
  if ! [[ "${body_bytes}" =~ ^[0-9]+$ ]]; then
    echo "::warning::Failed to capture numeric body size for tracking issue #${TRACKING_NUM}; skipping post." >&2
    return 0
  fi
  if [ "${body_bytes}" -gt 65536 ]; then
    echo "::warning::Tracking comment body too large for issue #${TRACKING_NUM} (${body_bytes} bytes > 65536 GitHub limit); skipping post." >&2
    return 0
  fi
  payload_file="$(mktemp "${TMPDIR:-/tmp}/comment_payload.XXXXXX")"
  payload_err_file="$(mktemp "${TMPDIR:-/tmp}/comment_payload_err.XXXXXX")"
  # Pipe the body through jq's stdin (-Rs reads it as a single raw
  # string) rather than passing it via `--arg body "${comment_body}"`.
  # Large `<!-- ORCHESTRATOR_STATE_V1 ... -->` snapshots for big
  # projects can exceed 100 KB and trip Linux's per-argv MAX_ARG_STRLEN
  # cap (128 KB on x86_64), failing with "Argument list too long".
  # When that happened silently, the orchestrator state never persisted
  # post-validation, leaving the project pinned at the last successfully
  # posted state and re-dispatching validation every poll cycle (loop
  # observed on tracking issue #2263). printf is a bash builtin so the
  # variable expansion stays in-process and avoids the same cap.
  if ! printf '%s' "${comment_body}" | jq -Rs '{body: .}' > "${payload_file}" 2>"${payload_err_file}"; then
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

post_issue_comment_json() {
	local issue_num="$1"
	local comment_body="$2"
	local payload_file
	local payload_err_file
	local response_file
	local body_bytes
	local comment_id=""
	local comment_url=""

	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 1
	body_bytes="$(printf '%s' "${comment_body}" | wc -c | tr -d '[:space:]')"
	if ! [[ "${body_bytes}" =~ ^[0-9]+$ ]]; then
		echo "::warning::Failed to capture numeric body size for #${issue_num}; skipping post." >&2
		return 1
	fi
	if [ "${body_bytes}" -gt 65536 ]; then
		echo "::warning::Issue comment body too large for #${issue_num} (${body_bytes} bytes > 65536 GitHub limit); skipping post." >&2
		return 1
	fi

	payload_file="$(mktemp "${TMPDIR:-/tmp}/issue_comment_payload.XXXXXX")"
	payload_err_file="$(mktemp "${TMPDIR:-/tmp}/issue_comment_payload_err.XXXXXX")"
	if ! printf '%s' "${comment_body}" | jq -Rs '{body: .}' > "${payload_file}" 2>"${payload_err_file}"; then
		echo "::warning::Failed to encode issue comment JSON payload for #${issue_num}: $(cat "${payload_err_file}" 2>/dev/null)" >&2
		rm -f "${payload_err_file}" "${payload_file}"
		return 1
	fi
	rm -f "${payload_err_file}"

	response_file="$(mktemp "${TMPDIR:-/tmp}/issue_comment_response.XXXXXX")"
	if ! gh_retry_to_file "${response_file}" gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
		--method POST \
		--input "${payload_file}"; then
		echo "::warning::Failed to post issue comment for #${issue_num}; will retry on a later cycle." >&2
		head -c 4096 "${response_file}" >&2 || true
		echo >&2
		rm -f "${payload_file}" "${response_file}"
		return 1
	fi
	rm -f "${payload_file}"

	comment_id="$(jq -r '.id // empty' "${response_file}" 2>/dev/null || echo "")"
	if [[ "${comment_id}" =~ ^[0-9]+$ ]]; then
		comment_url="$(_gh_url "issues/${issue_num}#issuecomment-${comment_id}")"
	fi
	jq -n \
		--argjson id "${comment_id:-0}" \
		--arg url "${comment_url}" '
			{
				id: (if $id == 0 then null else $id end),
				html_url: (if $url == "" then null else $url end)
			}
		'
	rm -f "${response_file}"
}

post_state_comment() {
  # Persist orchestrator state as a V2 chunked-comment chain so a snapshot
  # bigger than GitHub's 65,536-byte comment-body cap still lands.  The
  # legacy V1 single-comment writer silently no-op'd on oversize bodies:
  # GitHub's POST /comments returns HTTP 422 for body > 65,536 bytes,
  # gh_retry burns its retries on the 422, and post_tracking_comment's
  # `gh_retry … || true` swallows the final failure.  The persisted
  # state then stays pinned at the last successful (smaller) snapshot,
  # so every poll re-reads the stale state and re-runs wave-advance /
  # deferred-issue-creation — observed as six duplicate issues for
  # tracking #2373's `semble-judge-prefetch`.
  #
  # Reader (extract_latest_valid_orchestrator_state) tries V2 first and
  # falls back to V1 so existing tracking issues with V1 state comments
  # keep working until the next write supersedes them.
  #
  # Defence-in-depth: the helper script is staged into scripts/ by the
  # workflow's "Stage workflow support files" step alongside this script,
  # but if the staging list omits it we surface a hard error here rather
  # than silently swallowing pack failures and falling through to stale
  # V1 state on the next poll cycle.
  if [ ! -f "scripts/orchestrate_state_v2.py" ]; then
    echo "::error::scripts/orchestrate_state_v2.py is missing from the staged scripts tree; V2 state persistence cannot run for issue #${TRACKING_NUM}. Update the workflow's 'Stage workflow support files' loop to include this helper." >&2
    return 1
  fi
  local pack_dir manifest_json total raw_bytes chunk_files chunk_file idx chunk_count
  pack_dir="$(mktemp -d "${TMPDIR:-/tmp}/orchstate_v2_pack.XXXXXX")"
  if ! manifest_json="$(python3 scripts/orchestrate_state_v2.py pack \
      --state-file "${STATE_FILE}" \
      --out-dir "${pack_dir}" 2>&1)"; then
    echo "::error::orchestrate_state_v2 pack failed for issue #${TRACKING_NUM}: ${manifest_json}" >&2
    rm -rf "${pack_dir}"
    return 1
  fi
  total="$(printf '%s' "${manifest_json}" | jq -r '.total // 0' 2>/dev/null || echo 0)"
  raw_bytes="$(printf '%s' "${manifest_json}" | jq -r '.raw_bytes // 0' 2>/dev/null || echo 0)"
  if ! [[ "${total}" =~ ^[0-9]+$ ]] || [ "${total}" -lt 1 ]; then
    echo "::error::orchestrate_state_v2 pack returned no chunks for issue #${TRACKING_NUM}: ${manifest_json}" >&2
    rm -rf "${pack_dir}"
    return 1
  fi
  # Validate the manifest's files array before posting anything.  A
  # mismatched count would otherwise emit a torn V2 chain and then fall
  # back to stale state on the next poll.
  chunk_count="$(printf '%s' "${manifest_json}" | jq -r '.files | if type == "array" then length else -1 end' 2>/dev/null || echo -1)"
  if ! [[ "${chunk_count}" =~ ^-?[0-9]+$ ]] || [ "${chunk_count}" -ne "${total}" ]; then
    echo "::error::orchestrate_state_v2 pack returned ${chunk_count} chunk file(s) but declared total=${total} for issue #${TRACKING_NUM} (raw_bytes=${raw_bytes}); skipping torn V2 state write." >&2
    rm -rf "${pack_dir}"
    return 1
  fi
  chunk_files="$(printf '%s' "${manifest_json}" | jq -r '.files[]' 2>/dev/null || echo "")"
  idx=0
  while IFS= read -r chunk_file; do
    [ -n "${chunk_file}" ] || continue
    idx=$((idx + 1))
    if ! _post_state_comment_v2_chunk "${chunk_file}"; then
      echo "::error::Failed to post V2 state chunk ${idx}/${total} for issue #${TRACKING_NUM} (raw_bytes=${raw_bytes}); chain incomplete, reader will fall back to last persisted state." >&2
      rm -rf "${pack_dir}"
      return 1
    fi
  done <<< "${chunk_files}"
  if [ "${idx}" -ne "${total}" ]; then
    echo "::error::orchestrate_state_v2 pack manifest mismatch for issue #${TRACKING_NUM}: posted ${idx} chunk(s) but manifest declared ${total} (raw_bytes=${raw_bytes}); treating V2 write as failed to avoid persisting a torn chain." >&2
    rm -rf "${pack_dir}"
    return 1
  fi
  rm -rf "${pack_dir}"
  _mirror_task_state_files_from_state
  return 0
}

_mirror_task_state_files_from_state() {
	local task_state_files_enabled
	task_state_files_enabled="${ORCH_TASK_FILES_ENABLED:-false}"
	if [ "${task_state_files_enabled,,}" != "true" ]; then
		return 0
	fi

	local script_dir task_state_helper
	script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "scripts")"
	task_state_helper="${script_dir}/task_state.py"
	if [ ! -f "${task_state_helper}" ]; then
		echo "TASK_STATE_WRITE_FAIL helper_missing task_state_py_not_staged" >&2
		return 0
	fi

	if [ -z "${STATE_FILE:-}" ] || [ ! -s "${STATE_FILE}" ]; then
		echo "TASK_STATE_WRITE_FAIL state_file state_file_missing_or_empty" >&2
		return 0
	fi

	if ! python3 "${task_state_helper}" mirror-state --state-file "${STATE_FILE}"; then
		echo "TASK_STATE_WRITE_FAIL state_file mirror_state_command_failed" >&2
	fi
	return 0
}

# Posts a single V2 state-chunk comment to the tracking issue.  Unlike
# post_tracking_comment(), this returns non-zero on hard failure so the
# caller can detect torn-write conditions instead of silently producing
# a partial chain.  Each chunk is sized by orchestrate_state_v2.py to fit
# under GitHub's 65,536-byte comment-body cap, so the post_tracking_comment
# size guard does not apply here.
_post_state_comment_v2_chunk() {
  local chunk_file="$1"
  local payload_file diag_file gh_rc
  payload_file="$(mktemp "${TMPDIR:-/tmp}/orchstate_v2_chunk_payload.XXXXXX")"
  if ! jq -Rs '{body: .}' < "${chunk_file}" > "${payload_file}" 2>/dev/null; then
    rm -f "${payload_file}"
    echo "::error::_post_state_comment_v2_chunk: jq failed to wrap chunk payload (chunk=${chunk_file})" >&2
    return 1
  fi
  # Capture stdout+stderr from gh api so failed posts surface HTTP status,
  # response body, validation errors, and rate-limit details to operators
  # instead of collapsing into a generic "::error::Failed to post V2 state
  # chunk" with no diagnostic context.  The success path still drops
  # stdout (GitHub returns the comment JSON, which is large and not
  # informative for the caller).
  diag_file="$(mktemp "${TMPDIR:-/tmp}/orchstate_v2_chunk_diag.XXXXXX")"
  set +e
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    --method POST \
    --input "${payload_file}" >"${diag_file}" 2>&1
  gh_rc=$?
  set -e
  rm -f "${payload_file}"
  if [ "${gh_rc}" -ne 0 ]; then
    {
      echo "::error::_post_state_comment_v2_chunk: gh api POST /comments failed (rc=${gh_rc}) for issue #${TRACKING_NUM:-?} chunk ${chunk_file##*/}"
      echo "----- gh api stdout/stderr (truncated to 4 KiB) -----"
      head -c 4096 "${diag_file}" 2>/dev/null || true
      echo
      echo "----- end gh api diagnostics -----"
    } >&2
    rm -f "${diag_file}"
    return 1
  fi
  rm -f "${diag_file}"
  return 0
}

persist_completion_status_comment_state() {
  local comment_id="$1"
  local body_hash="$2"
  local state_tmp old_hash old_comment_id

  if [ -z "${STATE_FILE:-}" ] || [ ! -f "${STATE_FILE}" ]; then
    echo "::warning::[completion-status] cannot persist comment metadata for issue #${TRACKING_NUM:-?}: STATE_FILE is missing." >&2
    return 1
  fi
  if ! [[ "${comment_id}" =~ ^[0-9]+$ ]]; then
    echo "::warning::[completion-status] cannot persist comment metadata for issue #${TRACKING_NUM:-?}: invalid comment id '${comment_id:-<empty>}'" >&2
    return 1
  fi
  if ! [[ "${body_hash}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "::warning::[completion-status] cannot persist comment metadata for issue #${TRACKING_NUM:-?}: invalid body hash." >&2
    return 1
  fi

  old_hash="$(jq -r '.completion_status_comment_body_hash // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  [[ "${old_hash}" =~ ^[0-9a-f]{64}$ ]] || old_hash=""
  old_comment_id="$(jq -r '.completion_status_comment_id // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  [[ "${old_comment_id}" =~ ^[0-9]+$ ]] || old_comment_id=""

  if [ "${old_hash}" = "${body_hash}" ] && [ "${old_comment_id}" = "${comment_id}" ]; then
    return 0
  fi

  state_tmp="${STATE_FILE}.tmp"
  if jq --arg hash "${body_hash}" --argjson comment_id "${comment_id}" '
      .completion_status_comment_body_hash = $hash
      | .completion_status_comment_id = $comment_id' \
      "${STATE_FILE}" > "${state_tmp}" && mv "${state_tmp}" "${STATE_FILE}"; then
    COMPLETION_STATUS_STATE_CHANGED="true"
    return 0
  fi

  rm -f "${state_tmp}" || true
  echo "::warning::[completion-status] failed to persist comment metadata for issue #${TRACKING_NUM:-?}; cross-cycle idempotency may retry next tick." >&2
  return 1
}

# recover_completion_status_comment_id_from_live_comments — rare-path repair
# for GitHub comment updates that succeeded but returned a malformed body
# without a numeric `.id`. The cycle-local COMMENTS cache predates a POST,
# so only this recovery path is allowed to re-list comments.
recover_completion_status_comment_id_from_live_comments() {
  local full_body="$1"
  local marker="$2"
  local comments_json recovered_id comments_raw

  comments_raw="$(mktemp "${TMPDIR:-/tmp}/completion_status_comments.XXXXXX")" || return 1
  if ! gh_retry_to_file "${comments_raw}" gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments?per_page=100"; then
    rm -f "${comments_raw}"
    return 1
  fi
  if ! comments_json="$(jq -s 'add // []' "${comments_raw}" 2>/dev/null)"; then
    rm -f "${comments_raw}"
    return 1
  fi
  rm -f "${comments_raw}"

  recovered_id="$(printf '%s' "${comments_json}" | jq -r --arg body "${full_body}" '
    [.[] | select((.body // "") == $body)]
    | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)])
    | .id // empty
  ' 2>/dev/null || echo "")"
  if ! [[ "${recovered_id}" =~ ^[0-9]+$ ]]; then
    recovered_id="$(printf '%s' "${comments_json}" | jq -r --arg marker "${marker}" '
      [.[] | select((.body // "") | contains($marker))]
      | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)])
      | .id // empty
    ' 2>/dev/null || echo "")"
  fi

  [[ "${recovered_id}" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "${recovered_id}"
}

# update_completion_status_comment — maintain a single pinned "what is
# blocking completion" comment on the tracking issue, edit-in-place.
#
# The V2 state chain written by post_state_comment is the canonical
# machine-readable state record. This comment is a separate, human-
# facing summary keyed to a unique marker so it can be edited in place
# every cycle as wave PRs land and the project converges on completion
# — without producing a fresh comment per poll tick.
#
# Marker: <!-- orchestrator:completion-status -->
# Status tag (grep-friendly second-line marker):
#   <!-- status:in-progress|waiting|ready|validated|failed -->
#
# Args:
#   $1 = status token (in-progress | waiting | ready | validated | failed)
#   $2 = rendered markdown body (the marker and status-tag lines are
#        prepended by the helper)
#
# Idempotency: hashes the rendered body and skips the API call when the
# comment already matches. Successful writes persist the body hash +
# comment ID into STATE_FILE so the edit-in-place fallback survives the
# next cron invocation even though ${TMPDIR:-/tmp} does not.
#
# API hygiene (§15): when COMMENTS is set (paginated comments fetched
# earlier in the same cycle), the existing-comment lookup is satisfied
# from that cache. Only the malformed-response recovery path re-lists
# comments, and only when the POST/PATCH response omitted a numeric id.
update_completion_status_comment() {
  local status="$1"
  local body_markdown="$2"
  local marker="<!-- orchestrator:completion-status -->"
  local full_body body_hash existing_id existing_body response_file
  local response_id state_hash state_comment_id existing_hash comments_fetch_ok

  if [ -z "${TRACKING_NUM:-}" ] || [ "${TRACKING_NUM}" = "0" ]; then
    return 0
  fi

  case "${status}" in
    in-progress|waiting|ready|validated|failed) ;;
    *)
      echo "::warning::[completion-status] invalid status token '${status}' for issue #${TRACKING_NUM:-?}; skipping comment update." >&2
      return 1
      ;;
  esac

  full_body="${marker}"$'\n'"<!-- status:${status} -->"$'\n'"${body_markdown}"
  body_hash="$(printf '%s' "${full_body}" | sha256sum | awk '{print $1}')"
  state_hash=""
  state_comment_id=""
  comments_fetch_ok="${COMMENTS_FETCH_OK:-false}"

  if [ -f "${STATE_FILE:-/dev/null}" ]; then
    state_hash="$(jq -r '.completion_status_comment_body_hash // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
    state_comment_id="$(jq -r '.completion_status_comment_id // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
    [[ "${state_hash}" =~ ^[0-9a-f]{64}$ ]] || state_hash=""
    [[ "${state_comment_id}" =~ ^[0-9]+$ ]] || state_comment_id=""
  fi

  existing_id=""
  existing_body=""
  if [ "${comments_fetch_ok}" = "true" ] && [ -n "${COMMENTS:-}" ] && [ "${COMMENTS}" != "[]" ]; then
    existing_id="$(printf '%s' "${COMMENTS}" \
      | jq -r --arg marker "${marker}" '
          [.[] | select((.body // "") | contains($marker))]
          | first | .id // empty' 2>/dev/null || echo "")"
    existing_body="$(printf '%s' "${COMMENTS}" \
      | jq -r --arg marker "${marker}" '
          [.[] | select((.body // "") | contains($marker))]
          | first | .body // empty' 2>/dev/null || echo "")"
    if [ -n "${existing_body}" ]; then
      existing_hash="$(printf '%s' "${existing_body}" | sha256sum | awk '{print $1}')"
      if [ "${existing_hash}" = "${body_hash}" ]; then
        if ! [[ "${existing_id}" =~ ^[0-9]+$ ]] && [[ "${state_comment_id}" =~ ^[0-9]+$ ]]; then
          existing_id="${state_comment_id}"
        fi
        persist_completion_status_comment_state "${existing_id}" "${body_hash}"
        return $?
      fi
    fi
  elif [ "${comments_fetch_ok}" != "true" ] && [ -n "${state_hash}" ] && [ "${state_hash}" = "${body_hash}" ]; then
    return 0
  fi

  if [ -z "${existing_id}" ] && [ "${comments_fetch_ok}" != "true" ] && [[ "${state_comment_id}" =~ ^[0-9]+$ ]]; then
    existing_id="${state_comment_id}"
  fi

  response_file="$(mktemp "${TMPDIR:-/tmp}/completion_status_response.XXXXXX")"

  if [ -n "${existing_id}" ] && [[ "${existing_id}" =~ ^[0-9]+$ ]]; then
    if ! gh_retry_to_file "${response_file}" gh api "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_id}" \
      -X PATCH -f body="${full_body}"; then
      echo "::warning::[completion-status] failed to PATCH comment ${existing_id} for issue #${TRACKING_NUM:-?}; will retry on a later cycle." >&2
      head -c 4096 "${response_file}" >&2 || true
      echo >&2
      rm -f "${response_file}"
      return 1
    fi
  else
    if ! gh_retry_to_file "${response_file}" gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -X POST -f body="${full_body}"; then
      echo "::warning::[completion-status] failed to POST comment for issue #${TRACKING_NUM:-?}; will retry on a later cycle." >&2
      head -c 4096 "${response_file}" >&2 || true
      echo >&2
      rm -f "${response_file}"
      return 1
    fi
  fi

  response_id="$(jq -r '.id // empty' "${response_file}" 2>/dev/null || echo "")"
  rm -f "${response_file}"
  if ! [[ "${response_id}" =~ ^[0-9]+$ ]]; then
    if [ -n "${existing_id}" ] && [[ "${existing_id}" =~ ^[0-9]+$ ]]; then
      response_id="${existing_id}"
    elif response_id="$(recover_completion_status_comment_id_from_live_comments "${full_body}" "${marker}" 2>/dev/null || echo "")" \
      && [[ "${response_id}" =~ ^[0-9]+$ ]]; then
      :
    else
      echo "::warning::[completion-status] GitHub comment update succeeded but no numeric comment id was returned for issue #${TRACKING_NUM:-?}; will retry metadata persistence on a later cycle." >&2
      return 1
    fi
  fi
  if [ "${COMMENTS_FETCH_OK:-false}" = "true" ] && [ -n "${COMMENTS:-}" ]; then
    COMMENTS="$(printf '%s' "${COMMENTS}" | jq -c --arg marker "${marker}" --arg body "${full_body}" --argjson id "${response_id}" '
      (if type == "array" then . else [] end)
      | map(select(((.body // "") | contains($marker)) | not))
      | . + [{id: $id, body: $body}]
    ' 2>/dev/null || printf '%s' "${COMMENTS}")"
  fi
  persist_completion_status_comment_state "${response_id}" "${body_hash}"
}

# completion_status_comment_failed_state_observation — return-code contract:
#   0 => live comments show the completion-status marker with status=failed
#   1 => live comments were fetched and the marker is missing or not failed
#   2 => live comments are unavailable this cycle (fail open)
completion_status_comment_failed_state_observation() {
  local marker="<!-- orchestrator:completion-status -->"

  if [ "${COMMENTS_FETCH_OK:-false}" != "true" ] || [ -z "${COMMENTS:-}" ] || [ "${COMMENTS}" = "[]" ]; then
    return 2
  fi

  if printf '%s' "${COMMENTS}" | jq -e --arg marker "${marker}" '
    any(.[]; ((.body // "") | contains($marker)) and ((.body // "") | contains("<!-- status:failed -->")))
  ' >/dev/null 2>&1; then
    return 0
  fi

  return 1
}

set_failed_completion_status_comment() {
  local detail="$1"

  COMPLETION_STATUS_STATE_CHANGED="false"
  update_completion_status_comment "failed" \
    "## Completion status"$'\n\n'"**State:** \`failed\`"$'\n\n'"${detail}" \
    || true
  if [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
    post_state_comment || true
  fi
}

refresh_validation_dispatch_wave_gate() {
  local wave_issue_nums_json candidate_details_json labels_json issue_states_json pr_states_json
  local integration_branch default_branch ahead_by wave_status

  wave_issue_nums_json="$(jq -c '[.waves[((.current_wave // 1) - 1)].issues[]?.github_issue | select(. != null) | tonumber?]' "${STATE_FILE}" 2>/dev/null || echo '[]')"
  if ! printf '%s' "${wave_issue_nums_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
    wave_issue_nums_json='[]'
  fi

  if [ "${wave_issue_nums_json}" = '[]' ]; then
    candidate_details_json='{}'
  else
    candidate_details_json="$(_fetch_candidate_issue_details_graphql "${wave_issue_nums_json}")"
  fi
  if ! printf '%s' "${candidate_details_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    candidate_details_json='{}'
  fi

  labels_json="$(printf '%s' "${candidate_details_json}" | jq -c 'with_entries(.value = (.value.labels // []))' 2>/dev/null || echo '{}')"
  issue_states_json="$(printf '%s' "${candidate_details_json}" | jq -c 'with_entries(.value = (((.value.state // "OPEN") | ascii_downcase) | if . == "closed" then "closed" else "open" end))' 2>/dev/null || echo '{}')"
  pr_states_json="$(printf '%s' "${candidate_details_json}" | jq -c '
    with_entries(.value = (
      if (.value.linked_pr // null) == null then {state: "unknown", merged: false}
      else {
        state: (
          if (.value.linked_pr.merged // false) == true then "closed"
          else ((.value.linked_pr.state // "") | ascii_downcase | if . == "open" or . == "closed" then . else "unknown" end)
          end
        ),
        merged: ((.value.linked_pr.merged // false) == true)
      }
      end
    ))
  ' 2>/dev/null || echo '{}')"

  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  [ "${integration_branch}" = "null" ] && integration_branch=""
  if [ -n "${integration_branch}" ]; then
    default_branch="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "")"
    if [ -z "${default_branch}" ]; then
      ahead_by=""
    elif ahead_by="$(_integration_branch_ahead_of_default "${integration_branch}" "${default_branch}")"; then
      :
    else
      ahead_by=""
    fi
  else
    ahead_by="0"
  fi

  wave_status="$(python3 scripts/orchestrate_lib.py check-wave-status \
    --state-file "${STATE_FILE}" \
    --labels-json "${labels_json}" \
    --issue-states-json "${issue_states_json}" \
    --pr-states-json "${pr_states_json}" \
    --integration-ahead-by "${ahead_by}" 2>/dev/null || echo '')"
  [ -n "${wave_status}" ] || return 1

  WAVE_COMPLETE="$(echo "${wave_status}" | jq -r '.wave_complete // false' 2>/dev/null || echo false)"
  ANY_FAILED="$(echo "${wave_status}" | jq -r '.any_failed // false' 2>/dev/null || echo false)"
  VALIDATION_DISPATCH_SAFE_DESPITE_FAILURES="$(echo "${wave_status}" | jq -r '.validation_dispatch_safe_despite_failures // false' 2>/dev/null || echo false)"
  PROJECT_COMPLETE="$(echo "${wave_status}" | jq -r '.project_complete // false' 2>/dev/null || echo false)"
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

  # Try the V2 chunked-chain reader first.  If a complete V2 chain is
  # present (newest write wins), use it; otherwise fall through to the
  # legacy V1 single-comment scan.  This keeps existing tracking issues
  # whose state was last persisted as V1 readable until a V2 write
  # supersedes them.  See scripts/orchestrate_state_v2.py for framing.
  local _v2_comments_file _v2_payload_file _v2_rc
  _v2_comments_file="$(mktemp "${TMPDIR:-/tmp}/orch_state_v2_comments.XXXXXX")"
  _v2_payload_file="$(mktemp "${TMPDIR:-/tmp}/orch_state_v2_payload.XXXXXX")"
  printf '%s' "${comments_json}" > "${_v2_comments_file}"
  python3 scripts/orchestrate_state_v2.py extract \
    --comments-json "${_v2_comments_file}" > "${_v2_payload_file}" 2>/dev/null
  _v2_rc=$?
  if [ "${_v2_rc}" = "0" ] && [ -s "${_v2_payload_file}" ]; then
    # Independent size guard: the extractor enforces its own per-chunk
    # cap, but the stitched payload itself has no upper bound inside
    # orchestrate_state_v2.py.  Cap at 8 MiB here so a corrupted manifest
    # (e.g. forged `total=N` with N near MAX_CHUNKS_PER_MANIFEST) cannot
    # poison the poller by feeding it an arbitrarily large blob even
    # after JSON-shape validation.  A healthy state snapshot is ~10s of
    # KiB; 8 MiB is two orders of magnitude above the worst real case
    # while still bounding the failure mode.
    local _v2_payload_bytes
    _v2_payload_bytes="$(wc -c < "${_v2_payload_file}" 2>/dev/null | tr -d ' \n' || echo "")"
    # Fail closed: if wc -c fails or returns non-numeric output the size
    # cap below cannot be evaluated, so reject the payload rather than
    # bypassing the cap (a hostile/corrupted blob is the very thing we
    # are trying to keep out of the poller's state).
    if ! [[ "${_v2_payload_bytes}" =~ ^[0-9]+$ ]]; then
      echo "::warning::V2 state extract payload byte count unavailable (wc -c output: ${_v2_payload_bytes:-<empty>}); rejecting and falling back to V1 for issue #${TRACKING_NUM:-?}." >&2
    elif [ "${_v2_payload_bytes}" -gt $((8 * 1024 * 1024)) ]; then
      echo "::warning::V2 state extract payload is ${_v2_payload_bytes} bytes (>8 MiB cap); rejecting and falling back to V1 for issue #${TRACKING_NUM:-?}." >&2
    else
      candidate_state="$(cat "${_v2_payload_file}")"
      if is_valid_orchestrator_state_json "${candidate_state}"; then
        EXTRACTED_STATE_JSON="${candidate_state}"
        rm -f "${_v2_comments_file}" "${_v2_payload_file}"
        return 0
      fi
    fi
  fi
  rm -f "${_v2_comments_file}" "${_v2_payload_file}"

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

  local _label_err_file
  _label_err_file="$(mktemp 2>/dev/null || echo '/dev/null')"

  if gh_retry gh label create "${label_name}" \
    --repo "${GITHUB_REPOSITORY}" \
    --color "${color}" \
    --description "${description}" >/dev/null 2>"${_label_err_file}"; then
    [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"
    _ENSURED_LABELS_CACHE[${label_name}]=1
    return 0
  fi

  local _label_err=""
  _label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
  [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"

  if printf '%s' "${_label_err}" | grep -Eiq 'already[ _-]*exists|already_exists'; then
    echo "::debug::ensure_label_exists: label already exists, skipping '${label_name}'." >&2
    _ENSURED_LABELS_CACHE[${label_name}]=1
    return 0
  fi

  echo "::warning::ensure_label_exists: failed to create label '${label_name}' in repo '${GITHUB_REPOSITORY}': ${_label_err}" >&2
  return 0
}

set_issue_phase_label() {
  local issue_num="$1"
  local phase_label="$2"
  local contract_file=".github/ai/label_contract.v1.json"

  ensure_label_exists "${phase_label}"

  if [ ! -f "${contract_file}" ]; then
    echo "::warning::set_issue_phase_label: missing label contract ${contract_file}; cannot apply '${phase_label}' safely on #${issue_num}." >&2
    return 1
  fi

  local phase_changes
  local _resolve_err_file
  _resolve_err_file="$(mktemp)"
  if ! phase_changes="$(python3 scripts/ai_labels.py resolve-phase --contract-file "${contract_file}" --phase "${phase_label}" 2>"${_resolve_err_file}")"; then
    local _resolve_err
    _resolve_err="$(tr '\n' ' ' < "${_resolve_err_file}" 2>/dev/null || true)"
    rm -f "${_resolve_err_file}"
    echo "::warning::set_issue_phase_label: resolve-phase failed for '${phase_label}' using ${contract_file} on #${issue_num}: ${_resolve_err:-<no stderr captured>}" >&2
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
  current_issue_labels="$(get_issue_labels_json "${issue_num}")"

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
    if ! gh_retry gh issue edit "${issue_num}" \
      --repo "${GITHUB_REPOSITORY}" \
      "${edit_args[@]}" >/dev/null 2>"${_label_err_file}"; then
      local _label_err
      _label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
      if echo "${_label_err}" | grep -Eqi "could not remove label:|['\"][[:alnum:]:._/-]+['\"] not found"; then
        echo "::warning::set_issue_phase_label: non-fatal missing label while applying '${phase_label}' to #${issue_num}: ${_label_err}" >&2
        rm -f "${_label_err_file}"
        return 0
      fi
      echo "::warning::set_issue_phase_label: failed to apply '${phase_label}' to #${issue_num}: ${_label_err}" >&2
      rm -f "${_label_err_file}"
      return 1
    fi
    rm -f "${_label_err_file}"
  fi
  return 0
}

set_tracking_phase_label() {
  local phase_label="$1"
  set_issue_phase_label "${TRACKING_NUM}" "${phase_label}"
}

get_issue_labels_json() {
  local issue_num="$1"
  gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/labels" --jq '[.[].name]' || echo '[]'
}

write_label_names_file_from_json() {
	local labels_json="${1:-[]}"
	local out_file="$2"
	if ! printf '%s' "${labels_json}" | jq -r '.[]?' > "${out_file}" 2>/dev/null; then
		: > "${out_file}"
	fi
}

reconcile_tracking_issue_body_from_state() {
	local final_pr="${1:-}"
	local integration_branch="${2:-}"
	local current_hash=""
	local last_refresh_hash=""
	local desired_hash=""
	local desired_body_file=""
	local render_err_file=""
	local edit_err_file=""
	local template_body_file=""
	local issue_json_file=""
	local body_changed="false"
	local pr_json=""
	local pr_state=""
	local pr_head_sha=""
	local pr_head_ref=""
	local pr_labels_json='[]'
	local pr_labels_file=""
	local tracking_labels_file=""
	local render_err=""
	local edit_err=""

	current_hash="$(jq -r '.tracking_body_sync_hash // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
	if [ -z "${current_hash}" ] && jq -e '(.project_body_snapshot // "") != ""' "${STATE_FILE}" >/dev/null 2>&1; then
		current_hash="$(python3 - "${STATE_FILE}" <<'PY'
import hashlib
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
	state = json.load(fh)
body = state.get("project_body_snapshot", "") or ""
print(hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "")
PY
)"
	fi
	last_refresh_hash="$(jq -r '
		if has("tracking_body_last_readiness_refresh_hash") then
			(.tracking_body_last_readiness_refresh_hash // "")
		else
			""
		end
	' "${STATE_FILE}" 2>/dev/null || echo "")"

	desired_body_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_render.XXXXXX")" || return 1
	render_err_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_render_err.XXXXXX")" || {
		rm -f "${desired_body_file}"
		return 1
	}

	if jq -e '(.project_body_snapshot // "") != ""' "${STATE_FILE}" >/dev/null 2>&1; then
		if ! python3 scripts/orchestrate_lib.py render-tracking-body \
			--state-file "${STATE_FILE}" > "${desired_body_file}" 2>"${render_err_file}"; then
			render_err="$(tr '\n' ' ' < "${render_err_file}" 2>/dev/null | head -c 512 || true)"
			echo "::warning::[tracking-body-sync] failed to render tracking body for issue #${TRACKING_NUM}: ${render_err:-unknown error}" >&2
			rm -f "${desired_body_file}" "${render_err_file}"
			return 1
		fi
	else
		issue_json_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_issue.XXXXXX")" || {
			rm -f "${desired_body_file}" "${render_err_file}"
			return 1
		}
		template_body_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_template.XXXXXX")" || {
			rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}"
			return 1
		}
		if ! gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" > "${issue_json_file}" 2>"${render_err_file}"; then
			render_err="$(tr '\n' ' ' < "${render_err_file}" 2>/dev/null | head -c 512 || true)"
			echo "::warning::[tracking-body-sync] failed to fetch live template body for issue #${TRACKING_NUM}: ${render_err:-unknown error}" >&2
			rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}" "${template_body_file}"
			return 1
		fi
		if ! python3 - "${issue_json_file}" > "${template_body_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
	issue = json.load(fh)
sys.stdout.write(issue.get("body", "") or "")
PY
		then
			echo "::warning::[tracking-body-sync] failed to decode live template body for issue #${TRACKING_NUM}." >&2
			rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}" "${template_body_file}"
			return 1
		fi
		if ! grep -Eq '^[[:space:]]*-[[:space:]]*\[[ xX]\][[:space:]]+\*\*[^*]+' "${template_body_file}"; then
			rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}" "${template_body_file}"
			return 0
		fi
		if [ -z "${current_hash}" ]; then
			current_hash="$(sha256sum "${template_body_file}" 2>/dev/null | awk '{print $1}' || true)"
		fi
		if ! python3 scripts/orchestrate_lib.py render-tracking-body \
			--state-file "${STATE_FILE}" \
			--template-body-file "${template_body_file}" > "${desired_body_file}" 2>"${render_err_file}"; then
			render_err="$(tr '\n' ' ' < "${render_err_file}" 2>/dev/null | head -c 512 || true)"
			echo "::warning::[tracking-body-sync] failed to render tracking body from live template for issue #${TRACKING_NUM}: ${render_err:-unknown error}" >&2
			rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}" "${template_body_file}"
			return 1
		fi
	fi

	desired_hash="$(sha256sum "${desired_body_file}" 2>/dev/null | awk '{print $1}' || true)"
	if [ -z "${desired_hash}" ]; then
		echo "::warning::[tracking-body-sync] failed to hash rendered tracking body for issue #${TRACKING_NUM}." >&2
		rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}" "${template_body_file}"
		return 1
	fi

	if [ "${desired_hash}" != "${current_hash}" ]; then
		edit_err_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_edit_err.XXXXXX")" || edit_err_file="/dev/null"
		if gh_retry gh issue edit "${TRACKING_NUM}" \
			--repo "${GITHUB_REPOSITORY}" \
			--body-file "${desired_body_file}" >/dev/null 2>"${edit_err_file}"; then
			if jq --arg hash "${desired_hash}" '
				.tracking_body_sync_hash = $hash
				| .tracking_body_last_readiness_refresh_hash = (.tracking_body_last_readiness_refresh_hash // "")
			' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
				TRACKING_BODY_SYNC_STATE_CHANGED="true"
			else
				rm -f "${STATE_FILE}.tmp" 2>/dev/null || true
				echo "::warning::[tracking-body-sync] body edit for issue #${TRACKING_NUM} succeeded but the new body hash could not be persisted; a later cycle may retry the edit." >&2
			fi
			body_changed="true"
			echo "TRACKING_BODY_SYNC issue=${TRACKING_NUM} hash=${desired_hash}" >&2
		else
			edit_err="$(tr '\n' ' ' < "${edit_err_file}" 2>/dev/null | head -c 512 || true)"
			echo "::warning::[tracking-body-sync] failed to update tracking issue #${TRACKING_NUM}: ${edit_err:-unknown error}" >&2
		fi
		[ "${edit_err_file}" = "/dev/null" ] || rm -f "${edit_err_file}"
	fi

	# Refresh readiness only when the live issue body is known to match the
	# rendered desired body (just edited successfully, or already synced).
	if [ "${body_changed}" = "true" ] || { [ "${desired_hash}" = "${current_hash}" ] && [ "${desired_hash}" != "${last_refresh_hash}" ]; }; then
		if [[ "${final_pr}" =~ ^[0-9]+$ ]]; then
			pr_json="$(_fetch_pr_json "${final_pr}")"
			pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
			pr_head_sha="$(_jq_field "${pr_json}" '.head.sha')"
			pr_head_ref="$(_jq_field "${pr_json}" '.head.ref')"
			[ -n "${pr_head_ref}" ] || pr_head_ref="${integration_branch}"
			if [ "${pr_state}" = "open" ] && [ -n "${pr_head_sha}" ] && [ -n "${pr_head_ref}" ]; then
				tracking_labels_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_labels.XXXXXX")" || tracking_labels_file=""
				pr_labels_file="$(mktemp "${TMPDIR:-/tmp}/tracking_body_pr_labels.XXXXXX")" || pr_labels_file=""
				[ -n "${tracking_labels_file}" ] || tracking_labels_file="/dev/null"
				[ -n "${pr_labels_file}" ] || pr_labels_file="/dev/null"
				write_label_names_file_from_json "${TRACKING_LABELS:-[]}" "${tracking_labels_file}"
				pr_labels_json="$(get_issue_labels_json "${final_pr}")"
				write_label_names_file_from_json "${pr_labels_json}" "${pr_labels_file}"
				if python3 scripts/check_integration_pr_readiness.py \
					--repo "${GITHUB_REPOSITORY}" \
					--head-ref "${pr_head_ref}" \
					--head-sha "${pr_head_sha}" \
					--pr-labels-file "${pr_labels_file}" \
					--tracking-body-file "${desired_body_file}" \
					--tracking-labels-file "${tracking_labels_file}"; then
					if jq --arg hash "${desired_hash}" '
						.tracking_body_sync_hash = $hash
						| .tracking_body_last_readiness_refresh_hash = $hash
					' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
						TRACKING_BODY_SYNC_STATE_CHANGED="true"
					else
						rm -f "${STATE_FILE}.tmp" 2>/dev/null || true
						echo "::warning::[tracking-body-sync] refreshed readiness for PR #${final_pr} but could not persist the refresh hash; a later cycle may refresh again." >&2
					fi
					echo "TRACKING_BODY_READINESS_REFRESH issue=${TRACKING_NUM} pr=${final_pr} sha=${pr_head_sha}" >&2
				else
					echo "::warning::[tracking-body-sync] failed to refresh orchestrator/integration-pr-not-ready for PR #${final_pr}; will retry on a later cycle if needed." >&2
				fi
				[ "${tracking_labels_file}" = "/dev/null" ] || rm -f "${tracking_labels_file}"
				[ "${pr_labels_file}" = "/dev/null" ] || rm -f "${pr_labels_file}"
			fi
		fi
	fi

	rm -f "${desired_body_file}" "${render_err_file}" "${issue_json_file}" "${template_body_file}"
	return 0
}

# _fetch_issue_labels_batch_graphql — Batch-fetch issue labels for a list
# of issue numbers using GraphQL aliases.
#
# Input: JSON array of issue numbers, e.g. "[123, 456]"
# Output: JSON object keyed by stringified issue number:
#   {"123":["ai:done",...], "456":[...]}.
#
# Fail-open contract:
# - Any failed batch is skipped (its issues are omitted from the cache).
# - Callers must treat missing keys as cache misses and fall back to the
#   legacy per-issue REST labels lookup for those specific issues.
_fetch_issue_labels_batch_graphql() {
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
      n="$(printf '%s' "${numbers_json}" | jq -r ".[$i]")"
      [[ "${n}" =~ ^[0-9]+$ ]] || continue
      fragment+=$'\n'"        i${i}: issue(number: ${n}) {
          number
          labels(first: 50) { nodes { name } }
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
      start="${end}"
      continue
    fi

    batch_transformed="$(printf '%s' "${batch_resp}" | jq -c '
      (.data.repository // {}) | to_entries | map(
        select(.value != null and (.value.number? != null)) | {
          key: (.value.number | tostring),
          value: [(.value.labels.nodes // [])[]?.name]
        }
      ) | from_entries
    ' 2>/dev/null || echo '{}')"

    merged="$(jq -s '.[0] * .[1]' <(printf '%s\n' "${merged}") <(printf '%s\n' "${batch_transformed}") 2>/dev/null || echo "${merged}")"

    start="${end}"
  done

  echo "${merged}"
}

# get_issue_state_labels_json — fetch {state, state_reason, labels} in a single
# API call.  Used by the validation fix-up loop to consolidate what used to be
# two separate round-trips (labels + state) and to make the loop state-aware
# (an issue closed without the ai:closed label was previously invisible to the
# closure detector).  On lookup failure, emits a conservative open/empty
# fallback so callers keep making progress rather than mis-classifying a
# transient API failure as a closed-without-merge event.
get_issue_state_labels_json() {
  local issue_num="$1"
  gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" \
    --jq '{state: (.state // "open"), state_reason: (.state_reason // ""), labels: [(.labels // [])[] | .name]}' \
    || echo '{"state":"open","state_reason":"","labels":[]}'
}

_issue_timeline_with_cross_refs_json() {
  local issue_num="$1"
  local owner="${GITHUB_REPOSITORY%%/*}"
  local repo="${GITHUB_REPOSITORY##*/}"

  if type gh_issue_timeline_with_cross_refs >/dev/null 2>&1; then
    gh_issue_timeline_with_cross_refs "${owner}" "${repo}" "${issue_num}"
    return $?
  fi

  if type _gh_issue_timeline_with_cross_refs_rest >/dev/null 2>&1; then
    _gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_num}"
    return $?
  fi

  gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" 2>/dev/null | jq -s 'add // []' 2>/dev/null
}

_issue_cross_ref_pr_numbers_unique() {
  local issue_num="$1"
  local timeline_json

  if ! timeline_json="$(_issue_timeline_with_cross_refs_json "${issue_num}")"; then
    return 1
  fi

  printf '%s' "${timeline_json}" | jq -r '
    if (type == "array") then
      [.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | unique | .[]
    else
      empty
    end
  ' 2>/dev/null
}

_issue_cross_ref_pr_number_last() {
  local issue_num="$1"
  local timeline_json

  if ! timeline_json="$(_issue_timeline_with_cross_refs_json "${issue_num}")"; then
    return 1
  fi

  printf '%s' "${timeline_json}" | jq -r '
    if (type == "array") then
      [.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last // empty
    else
      empty
    end
  ' 2>/dev/null | tail -n1
}

# _linked_prs_by_branch_name — Return open PR numbers whose head branch
# matches the orchestrator's conventional `ai/issue-<n>` naming.  Used by
# `close_linked_pr` as a fallback when the issue timeline cross-reference
# event was never recorded (observed in prod for issue #2552 / PR #2568,
# where the timeline API did not expose the reference even though the PR
# body said "Closes #<n>").
# Output: newline-separated PR numbers, empty if none.  Fail-open.
_linked_prs_by_branch_name()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" \
		--head "ai/issue-${issue_num}" --state open \
		--json number --jq '.[].number' 2>/dev/null || true
}

# _linked_prs_by_body_reference — Return open PR numbers whose body
# contains a GitHub closing keyword ("Closes #N" / "Fixes #N" /
# "Resolves #N", case-insensitive) targeting this issue.  Used as a second fallback
# for `close_linked_pr` because relying solely on the timeline
# cross-reference event is brittle (edits, Actions-bot-authored PRs, and
# certain merge-queue interactions have all been observed to suppress the
# event).  Narrows candidates via GitHub search then applies a
# word-boundary regex so e.g. "#25528" does not match for issue #2552.
# Output: newline-separated PR numbers, empty if none.  Fail-open.
_linked_prs_by_body_reference()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	local candidates_json
	candidates_json="$(gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" --state open \
		--search "#${issue_num} in:body" \
		--json number,body --limit 100 2>/dev/null || echo "[]")"
	printf '%s' "${candidates_json}" | jq -r --arg n "${issue_num}" '
		(. // [])
		| .[]
		| select((.body // "") | test("(?i)(close[sd]?|fix(es|ed)?|resolve[sd]?):?[[:space:]]+#" + $n + "\\b"))
		| .number
	' 2>/dev/null || true
}

# _find_all_linked_prs — Union of the three lookup strategies above, used
# by `close_linked_pr` to enumerate every PR that should be considered
# for close-on-stall.  Dedupes across strategies so a PR surfaced by both
# the timeline and the branch-name lookup is only emitted once.
# API calls issued per invocation (upper bound):
#   - 1 timeline fetch via _issue_cross_ref_pr_numbers_unique
#   - 1 `gh pr list --head`
#   - 1 `gh pr list --search` (narrowed; not a full-repo scan)
# Fail-open: each strategy swallows its own errors; missing data never
# raises an exception to the caller.
# Output: newline-separated, sorted-unique PR numbers.
_find_all_linked_prs()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	{
		_issue_cross_ref_pr_numbers_unique "${issue_num}" 2>/dev/null || true
		_linked_prs_by_branch_name "${issue_num}" 2>/dev/null || true
		_linked_prs_by_body_reference "${issue_num}" 2>/dev/null || true
	} | grep -E '^[0-9]+$' | sort -u
}

# _resolve_linked_pr_fresh_by_branch — Deterministic fallback for a linked
# PR's head-commit timestamp when the issue→PR cross-reference timeline (the
# single source that feeds BOTH stall-freshness guards: the detect_stalls
# ai:done clock re-anchor and _check_fresh_push_guard) comes back empty.
#
# The cross-reference event is brittle — edits, Actions-bot-authored PRs, and
# certain merge-queue interactions have all been observed to suppress it (see
# _linked_prs_by_branch_name / issue #2552, PR #2568).  When it is missing the
# linked-PR entry carries no headPushedAt and BOTH guards fail open in
# lock-step, producing a false-positive ai:done stall recovery even though the
# PR was just pushed.  This helper re-resolves the PR by the orchestrator's
# conventional `ai/issue-<n>` head branch (the same deterministic lookup
# close_linked_pr uses via _linked_prs_by_branch_name) and reads the head
# commit's date from the SAME `gh pr list` response, so a suppressed
# cross-reference can no longer blind the freshness signal.
#
# §15 audit: the cross-ref caches (STALL_MANAGED_LINKED_PR_CACHE,
# _current_wave_details_json) are exactly what is empty in this failure mode,
# so they cannot supply the data.  _linked_prs_by_branch_name returns only PR
# numbers and would need a second call for the commit date; folding
# number+commits into one `gh pr list --head` request is the minimal call
# (1 REST request) and reuses the existing branch-lookup pattern.  Callers
# gate this to stalled PR-bearing-phase issues whose primary headPushedAt is
# already missing, so the steady-state (cross-ref present) path adds 0 calls.
#
# Output: compact JSON `{"number":N,"headPushedAt":"<ISO8601>"}` for the
# freshest open `ai/issue-<n>` PR, or empty string when none resolves.
# headPushedAt is the head commit's committedDate — GitHub exposes the true
# pushedDate only over GraphQL, and committedDate is the same field the
# cross-ref fetchers coalesce to (`pushedDate // committedDate`); for autofix
# commits (created and pushed together) the two are equal.
# Fail-open: any error yields empty output and the caller proceeds unchanged.
_resolve_linked_pr_fresh_by_branch()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	local prs_json
	prs_json="$(gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" \
		--head "ai/issue-${issue_num}" --state open \
		--json number,commits 2>/dev/null || echo '[]')"
	printf '%s' "${prs_json}" | jq -c '
		(. // [])
		| map(
			select(.number? != null)
			| {
				number: .number,
				headPushedAt: (
					((.commits // []) | map(.committedDate) | map(select(. != null)) | sort | last) // null
				)
			}
		)
		| map(select(.headPushedAt != null))
		| sort_by(.headPushedAt, .number)
		| last // empty
	' 2>/dev/null || true
}

# _timeline_jq — Paginated timeline query with jq filter.
# Fetches ALL pages of the timeline API (via _issue_timeline_with_cross_refs_json,
# which is GraphQL-first with REST fallback) and applies a jq filter.
# Usage: _timeline_jq <issue_number> '<jq_filter>'
# Input:  issue_number (integer), jq_filter (string — applied to the merged array)
# Output: jq-filtered result on stdout; empty string on failure
# API calls: 1 GraphQL (fail-open to 1+ paginated REST) via the existing
#            _issue_timeline_with_cross_refs_json helper.
# Fail-open: returns empty on timeline fetch failure or jq error.
_timeline_jq()
{
	local issue_num="$1"
	local jq_filter="$2"
	local timeline_json

	if ! timeline_json="$(_issue_timeline_with_cross_refs_json "${issue_num}")"; then
		echo ""
		return 1
	fi

	printf '%s' "${timeline_json}" | jq -r "${jq_filter}" 2>/dev/null
}

# _subissue_closing_pr_number — Resolve the PR that actually implemented
# and merged a sub-issue, for orchestrator intent-fingerprint capture.
#
# Intent-fingerprint capture (capture_intent_fingerprints_for_merged_subissue)
# must read the diff of the PR that *implemented* the sub-issue — not an
# unrelated PR that merely carries a `Refs #N` cross-reference in its
# body.  GitHub records a `cross-referenced` timeline event for both
# kinds, so the earlier selection (`... | .source.issue.number | last`
# — the most-recent cross-reference) latched onto whichever PR
# referenced the issue last.  When that was a `Refs #N` infrastructure
# PR, capture fingerprinted THAT PR's diff lines as the sub-issue's
# must_contain patterns and the wave-dispatch fingerprint gate then
# wedged because those lines were never merged onto the integration
# branch.
#
# Selection (strongest signal first; every step fails open to empty):
#   1. The most-recently-merged PR on the orchestrator's conventional
#      `ai/issue-<n>` head branch — the deterministic implementation-PR
#      naming.  A `Refs #N` PR is never on that branch.
#   2. Otherwise, the newest merged cross-referenced PR whose body
#      carries a GitHub closing keyword (close/fix/resolve and
#      inflections) targeting THIS issue, in either the `#N` form or the
#      `.../issues/N` URL form emitted by the implement workflow.
#      GraphQL `willCloseTarget` is deliberately NOT used: sub-issue
#      implementation PRs target the integration branch, not the default
#      branch, so GitHub never sets willCloseTarget on them.
#
# Output: a single PR number on stdout, or empty when no implementing
#         PR can be identified — the caller then skips capture, which is
#         correct for a sub-issue that was never actually implemented
#         (e.g. one falsely marked merged by a `Refs #N` cross-reference).
# API calls: 1 `gh pr list --head` (tier 1); on a tier-1 miss, 1 timeline
#         fetch (via _issue_cross_ref_pr_numbers_unique) plus up to one
#         `gh api pulls/<n>` per cross-referenced PR.  Capture is
#         idempotent and runs at most once per sub-issue, so this is not
#         a hot path.
# Fail-open: any lookup error yields empty; capture is then skipped.
_subissue_closing_pr_number()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0

	# Tier 1 — the orchestrator's conventional implementation branch.
	local branch_pr=""
	branch_pr="$(gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" \
		--head "ai/issue-${issue_num}" --state merged \
		--json number,mergedAt \
		--jq 'map(select((.mergedAt // "") != "")) | sort_by(.mergedAt) | .[-1].number // empty' 2>/dev/null || true)"
	if [[ "${branch_pr}" =~ ^[0-9]+$ ]]; then
		printf '%s\n' "${branch_pr}"
		return 0
	fi

	# Tier 2 — closing-keyword body match across cross-referenced PRs,
	# newest first.  Only a merged PR can have contributed its diff to
	# the integration branch, so an open `Refs #N` PR is filtered out.
	local xref_prs=""
	xref_prs="$(_issue_cross_ref_pr_numbers_unique "${issue_num}" 2>/dev/null || true)"
	[ -n "${xref_prs}" ] || return 0

	local pr pr_json pr_body
	while IFS= read -r pr; do
		[[ "${pr}" =~ ^[0-9]+$ ]] || continue
		pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr}" 2>/dev/null)" || return 0
		[ -n "${pr_json}" ] || return 0
		printf '%s' "${pr_json}" | jq -e '(.merged_at // null) != null' >/dev/null 2>&1 || continue
		pr_body="$(printf '%s' "${pr_json}" | jq -r '.body // ""' 2>/dev/null || echo "")"
		if printf '%s' "${pr_body}" | grep -qiE \
			"(^|[^[:alnum:]_/-])(close[sd]?|fix(es|ed)?|resolve[sd]?):?[[:space:]]+(#${issue_num}|[^[:space:]]*/issues/${issue_num})([^[:alnum:]_/-]|$)"; then
			printf '%s\n' "${pr}"
			return 0
		fi
	done <<< "$(printf '%s\n' "${xref_prs}" | sort -rn -u)"

	return 0
}

# _purge_stale_fingerprint_entries_on_integration_branch — Self-heal
# stale `merged_issue_fingerprints` entries the wave-dispatch gate
# cannot reasonably satisfy.  Capture is idempotent (the early-return
# at the top of `capture_intent_fingerprints_for_merged_subissue`),
# so a single bad capture writes an entry that no later poll tick
# overwrites — the gate then hard-fails forever on the stale state.
#
# Two stale shapes are observable from local git plumbing alone (zero
# GitHub API calls):
#
#   1. The recorded PR has no commit referencing `(#<pr>)` on the
#      integration branch at all.  The captured diff cannot be on the
#      branch.  Original (pre-PR-#2907) symptom: capture latched onto
#      an open `Refs #N` cross-reference that never merged anywhere.
#
#   2. The recorded PR DOES have a merge commit on the integration
#      branch (subject ending `(#<pr>)`), but the entry's
#      `captured_at` predates that commit's committer date.  Capture
#      ran against an open-PR snapshot whose content was iterated
#      before the squash-merge landed (observed on project #2867 /
#      issue #2872: fingerprints captured 2026-05-22T09:04:12Z,
#      PR #2894's merge committed 2026-05-22T10:59:08Z with the
#      REST-fallback half rewritten in between, so the captured
#      patterns no longer reflect what is actually on the branch).
#
# Healthy entries — capture ran AFTER the merge via the orchestrator's
# normal merge-detection flow — have `captured_at` > merge committer
# date and are kept untouched, so a genuine post-merge resolver
# regression still hard-fails the gate as designed.
#
# Inputs:  <state_file_path> <integration_branch_git_ref>
# Output:  one `<issue_num>\t<pr_num>\t<reason>` line per purged entry
#          on stdout.
# API calls: zero (uses local `git log`, `jq`, and `date -u -d` only).
# Side effect: mutates the state file in place via one `jq | mv`
#              pass after stale keys are collected.
# Fail-open per entry: any git/jq/date error keeps the entry untouched.
_purge_stale_fingerprint_entries_on_integration_branch()
{
	local state_file="$1"
	local gate_ref="$2"
	[ -f "${state_file}" ] || return 0
	[ -n "${gate_ref}" ] || return 0

	local issue pr captured_at sfx log_out _sha _ct _subj merge_unix captured_unix any_ref reason fingerprint_rows purge_keys purge_rows
	if ! fingerprint_rows="$(jq -r '(.merged_issue_fingerprints // {}) | to_entries | .[] | "\(.key)\t\(.value.pr // "")\t\(.value.captured_at // "")"' "${state_file}" 2>/dev/null)"; then
		echo "Fingerprint-state self-heal: could not parse '${state_file}'; keeping existing fingerprint state." >&2
		return 0
	fi
	purge_keys=""
	purge_rows=""
	while IFS=$'\t' read -r issue pr captured_at; do
		[ -n "${issue}" ] || continue
		[[ "${pr}" =~ ^[0-9]+$ ]] || continue
		sfx="(#${pr})"
		if ! log_out="$(git log --reverse --format='%H%x09%ct%x09%s' --grep="${sfx}" "${gate_ref}" 2>/dev/null)"; then
			continue  # git plumbing failure — fail-safe, keep entry
		fi
		any_ref=0
		merge_unix=""
		while IFS=$'\t' read -r _sha _ct _subj; do
			[ -n "${_sha}" ] || continue
			any_ref=1
			# Under `git log --reverse`, the first subject-ending
			# match is the oldest and therefore the squash-merge.
			if [ -z "${merge_unix}" ] && [ "${_subj: -${#sfx}}" = "${sfx}" ]; then
				merge_unix="${_ct}"
			fi
		done <<< "${log_out}"
		reason=""
		if [ "${any_ref}" -eq 0 ]; then
			reason="pr_not_referenced_on_integration_branch"
		elif [ -n "${merge_unix}" ] && [ -n "${captured_at}" ]; then
			captured_unix="$(date -u -d "${captured_at}" +%s 2>/dev/null || true)"
			if [[ "${captured_unix}" =~ ^[0-9]+$ ]]; then
				if [[ "${merge_unix}" =~ ^[0-9]+$ ]] && [ "${merge_unix}" -gt "${captured_unix}" ]; then
					reason="captured_before_pr_merged_into_integration_branch"
				fi
			else
				echo "Fingerprint-state self-heal: issue #${issue} kept because captured_at '${captured_at}' is not parseable." >&2
			fi
		fi
		if [ -n "${reason}" ]; then
			purge_keys="${purge_keys}${issue}"$'\n'
			purge_rows="${purge_rows}${issue}"$'\t'"${pr}"$'\t'"${reason}"$'\n'
		fi
	done <<< "${fingerprint_rows}"

	[ -n "${purge_keys}" ] || return 0
	if jq --arg purge_keys "${purge_keys}" '
		($purge_keys | split("\n") | map(select(length > 0))) as $drop
		| reduce $drop[] as $k (. ; del(.merged_issue_fingerprints[$k]))
	' "${state_file}" > "${state_file}.tmp" \
		&& mv "${state_file}.tmp" "${state_file}"; then
		printf '%s' "${purge_rows}"
	else
		rm -f "${state_file}.tmp" 2>/dev/null || true
	fi

	return 0
}

has_label() {
  local labels_json="$1"
  local label="$2"
  echo "${labels_json}" | jq -e --arg label "${label}" 'index($label) != null' >/dev/null 2>&1
}

validation_fix_issue_has_merged_pr_evidence() {
  local issue_num="$1"
  local timeline_json

  if ! timeline_json="$(_issue_timeline_with_cross_refs_json "${issue_num}")"; then
    return 2
  fi

  if ! printf '%s' "${timeline_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
    return 2
  fi

  if printf '%s' "${timeline_json}" | jq -e '
    [.[]
      | select(.event == "cross-referenced")
      | select((.source.issue.pull_request.url? | type == "string") and ((.source.issue.merged // false) == true))
    ]
    | length > 0
  ' >/dev/null 2>&1; then
    return 0
  fi

  if printf '%s' "${timeline_json}" | jq -e '
    [.[]
      | select(.event == "cross-referenced")
      | select((.source.issue.lookup_failed // false) == true)
    ]
    | length > 0
  ' >/dev/null 2>&1; then
    return 2
  fi

  return 1
}

backfill_validation_fix_issue_merged_label() {
  local issue_num="$1"
  # Optional second arg: cached labels JSON (e.g. '["ai:closed",...]')
  # already fetched by the caller.  When provided, skips the per-call
  # `get_issue_labels_json` round-trip.  When empty/omitted the helper
  # falls back to fetching itself, preserving the original contract.
  local cached_labels="${2:-}"
  local contract_file=".github/ai/label_contract.v1.json"
  local fix_labels
  local phase_changes
  local edit_args=()
  local remove_label
  local _label_err_file

  ensure_label_exists "ai:merged"

  if [ -n "${cached_labels}" ]; then
    fix_labels="${cached_labels}"
  else
    fix_labels="$(get_issue_labels_json "${issue_num}")"
  fi
  if has_label "${fix_labels}" "ai:merged"; then
    return 0
  fi

  if [ -f "${contract_file}" ] && set_issue_phase_label "${issue_num}" "ai:merged"; then
    return 0
  fi

  if [ -f "${contract_file}" ]; then
    echo "::warning::set_issue_phase_label failed for #${issue_num}; falling back to manual ai:merged label edit." >&2
  fi

  edit_args+=(--add-label "ai:merged")
  if [ -f "${contract_file}" ]; then
    phase_changes="$(python3 scripts/ai_labels.py resolve-phase --contract-file "${contract_file}" --phase "ai:merged" 2>/dev/null || jq -c --arg phase "ai:merged" '[((.phase_groups // [])[]? | select(type == "object") | .members as $members | select(($members | type) == "array" and ($members | index($phase) != null)) | $members[]? | select(type == "string" and . != $phase))] | unique | {remove: .}' "${contract_file}" 2>/dev/null || echo '{"remove":["ai:closed"]}')"
    while IFS= read -r remove_label; do
      [ -n "${remove_label}" ] || continue
      if has_label "${fix_labels}" "${remove_label}"; then
        edit_args+=(--remove-label "${remove_label}")
      fi
    done < <(echo "${phase_changes}" | jq -r '.remove[]?' 2>/dev/null || true)
  elif has_label "${fix_labels}" "ai:closed"; then
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

# close_merged_issues_sweep — scans all OPEN GitHub issues that carry either
# the ai:merged label OR the ai:ready-to-merge label, and closes any whose
# linked PR is verified merged via the timeline cross-reference helper
# (GraphQL-first, fail-open to REST). Runs for both orchestrator-managed
# child issues and non-orchestrator-managed standalone issues. Tracking
# issues (label ai:orchestrator-tracking) are intentionally skipped — their
# close lifecycle is handled by the orchestrator completion path.
#
# Two label-origin classes (renames are breaking per CLAUDE.md §6 — public
# log prefixes embed the origin):
#   - merged_label: issue carries ai:merged. Existing strict policy applies:
#       no merged PR found in timeline -> leave open + Telegram WARNING
#       (the label is a strong signal something is wrong if we cannot
#       verify it, e.g. stale label, missing PR link, transient API
#       failure).
#   - ready_label: issue carries ai:ready-to-merge but NOT ai:merged. This
#       is the defensive backstop branch (added 2026-04-27) for the case
#       where reconcile_managed_issue_labels never promoted ai:ready-to-merge
#       -> ai:merged because the wave moved past the issue and the
#       backward-scan only observed labels without reconciling against PR
#       state. Policy: when a merged PR IS verified on timeline, backfill
#       ai:merged before closing so any concurrent reader (wave-status
#       resolver, validation fix-up loop) sees consistent state. When no
#       merged PR is found, this is the normal pending state -> exit
#       silently with NO Telegram alert (the label is not a contract that
#       a merged PR exists yet, only that auto-merge is requested).
#
# Verification policy (strict for both classes): walks timeline
# cross-references and only closes if at least one linked PR is verified
# merged (merged == true or merged_at != null).
#
# Gated by ENABLE_CLOSE_MERGED_ISSUES (default true).
#
# API hygiene (CLAUDE.md §15): up to 2 `gh issue list` calls per sweep
# (one per label class) regardless of N issues. Per-issue cost is the
# existing single _issue_timeline_with_cross_refs_json call (GraphQL-first
# with fail-open REST fallback) plus, in the ready_label-origin merged-PR
# case, one `gh issue edit` to backfill ai:merged before close.
close_merged_issues_sweep() {
  if [ "${ENABLE_CLOSE_MERGED_ISSUES}" != "true" ]; then
    echo "Close merged issues sweep disabled by ENABLE_CLOSE_MERGED_ISSUES=${ENABLE_CLOSE_MERGED_ISSUES}."
    return 0
  fi

  echo ""
  echo "========================================"
  echo "Close merged issues sweep"
  echo "========================================"

  local merged_json ready_json
  merged_json="$(gh_retry gh issue list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --label "ai:merged" \
    --json number,labels \
    --limit 200 2>/dev/null || echo "[]")"
  ready_json="$(gh_retry gh issue list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --label "ai:ready-to-merge" \
    --json number,labels \
    --limit 200 2>/dev/null || echo "[]")"

  # Build a single deduplicated list of {number, labels, origin} entries.
  # When an issue carries BOTH labels (legitimate transition state), prefer
  # the merged_label origin so the strict alerting policy applies.
  local issues_json
  issues_json="$(jq -c -n \
    --argjson merged "${merged_json:-[]}" \
    --argjson ready "${ready_json:-[]}" '
      def normalize($origin):
        map(
          select(type == "object" and (.number | type == "number"))
          | {number: .number, labels: (.labels // []), origin: $origin}
        );
      ($merged | normalize("merged_label")) as $m
      | ($ready | normalize("ready_label")) as $r
      | ($m + ($r | map(select(.number as $n | ($m | map(.number) | index($n)) == null))))
    ' 2>/dev/null || echo "[]")"

  local count
  count="$(echo "${issues_json}" | jq 'length' 2>/dev/null || echo "0")"
  local merged_count ready_count
  merged_count="$(echo "${merged_json}" | jq 'length' 2>/dev/null || echo "0")"
  ready_count="$(echo "${ready_json}" | jq 'length' 2>/dev/null || echo "0")"
  echo "Found ${merged_count} open issue(s) with ai:merged and ${ready_count} with ai:ready-to-merge (deduped: ${count})."

  if [ "${count}" -eq 0 ]; then
    return 0
  fi

  local idx issue_num origin has_tracking_label timeline_json merged_pr_num
  local closed_count=0
  local skipped_count=0
  local alert_count=0

  for ((idx=0; idx<count; idx++)); do
    issue_num="$(echo "${issues_json}" | jq -r ".[${idx}].number" 2>/dev/null || echo "")"
    [ -n "${issue_num}" ] && [ "${issue_num}" != "null" ] || continue
    origin="$(echo "${issues_json}" | jq -r ".[${idx}].origin" 2>/dev/null || echo "merged_label")"

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
    if ! timeline_json="$(_issue_timeline_with_cross_refs_json "${issue_num}")"; then
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} origin=${origin} timeline_fetch_failed — skipping this cycle."
      skipped_count=$((skipped_count + 1))
      continue
    fi

    if ! echo "${timeline_json}" | jq -e 'type == "array"' >/dev/null 2>&1; then
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} origin=${origin} timeline_not_array — skipping this cycle."
      skipped_count=$((skipped_count + 1))
      continue
    fi

    merged_pr_num="$(printf '%s' "${timeline_json}" | jq -r '
      [.[]
        | select(.event == "cross-referenced")
        | select((.source.issue.pull_request.url? | type == "string") and ((.source.issue.merged // false) == true))
        | .source.issue.number
      ]
      | first // empty
    ' 2>/dev/null || echo "")"

    if [ -z "${merged_pr_num}" ]; then
      if [ "${origin}" = "ready_label" ]; then
        # Normal pending state for ai:ready-to-merge. No alert — the
        # label is not a contract that a merged PR exists yet.
        echo "CLOSE_MERGED_SWEEP issue=${issue_num} origin=${origin} no_merged_pr_found — pending, leaving open."
        skipped_count=$((skipped_count + 1))
        continue
      fi
      # ai:merged origin: stale-label path retained — alert and skip.
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} origin=${origin} no_merged_pr_found — leaving open and alerting."
      tg_notify_issue "${issue_num}" "⚠️ Orchestrator poller: issue #${issue_num} carries the \`ai:merged\` label but no linked merged PR could be verified on its timeline. The label may be stale or the PR link may be missing. Not auto-closing — please investigate." "WARNING" || true
      alert_count=$((alert_count + 1))
      continue
    fi

    # Backfill ai:merged for ready_label-origin issues so concurrent
    # readers (wave-status resolver, validation fix-up loop, lineage
    # finalizer) see the same terminal label the merged_label-origin
    # branch has always produced. Idempotent: gh issue edit --add-label
    # is a no-op if the label is already present (race with the main
    # poller's reconcile_managed_issue_labels).
    if [ "${origin}" = "ready_label" ]; then
      ensure_label_exists "ai:merged" >/dev/null 2>&1 || true
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --add-label "ai:merged" --remove-label "ai:ready-to-merge" >/dev/null 2>&1 \
        || echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} origin=${origin} backfill_ai_merged_failed — proceeding to close anyway."
    fi

    echo "  Issue #${issue_num}: verified merged PR #${merged_pr_num} (origin=${origin}). Closing."
    local _close_err_file
    _close_err_file="$(mktemp)"
    if gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: linked PR #${merged_pr_num} was merged. Auto-closed by orchestrator poller (close_merged_issues_sweep)." \
        >/dev/null 2>"${_close_err_file}"; then
      closed_count=$((closed_count + 1))
      echo "CLOSE_MERGED_SWEEP issue=${issue_num} pr=${merged_pr_num} origin=${origin} status=closed"
    else
      echo "::warning::CLOSE_MERGED_SWEEP issue=${issue_num} origin=${origin} close_failed: $(cat "${_close_err_file}" 2>/dev/null)" >&2
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
  plan_json="$(python3 - "${labels_json}" "${repair_json}" "${issue_state}" "${pr_merged}" "${contract_file}" <<'PY'
import json
import sys

current = set(json.loads(sys.argv[1]))
repair = json.loads(sys.argv[2])
issue_state = (sys.argv[3] or "").strip().lower()
pr_merged = (sys.argv[4] or "").strip().lower() == "true"
contract_file = sys.argv[5] if len(sys.argv) > 5 else ""

final = set(current)
for label in repair.get("remove", []):
    final.discard(label)
for label in repair.get("add", []):
    final.add(label)

forced = ""
if pr_merged:
    final.discard("ai:closed")
    final.add("ai:merged")
    forced = "ai:merged"
elif issue_state == "closed" and "ai:closed" not in final and "ai:merged" not in final:
    final.add("ai:closed")
    forced = "ai:closed"

# Phase-group repair above ran on the original `current` set, so if
# `current` had only one phase-group member the repair was a no-op —
# then we force ai:merged/ai:closed on top, producing a dual-phase
# state (e.g. {ai:review-blocked, ai:merged}) that the wave-status
# resolver treats as terminal via ai:merged priority and never
# re-processes, stranding the non-terminal label forever. Re-apply
# phase exclusivity so only the forced terminal member survives.
if forced and contract_file:
    try:
        with open(contract_file, "r") as fh:
            contract = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"::warning::reconcile_managed_issue_labels: failed to load contract file {contract_file}: {exc}", file=sys.stderr)
        contract = None
    if isinstance(contract, dict):
        for group in contract.get("phase_groups", []) or []:
            if not isinstance(group, dict):
                continue
            raw_members = group.get("members")
            if not isinstance(raw_members, list):
                continue
            members = [str(item) for item in raw_members]
            if forced in members:
                for label in members:
                    if label != forced:
                        final.discard(label)

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
    emit_event "LABEL_REPAIR" \
      "issue=${issue_num}" \
      "issue_state=${issue_state}" \
      "pr_state=${pr_state:-none}" \
      "pr_merged=${pr_merged}"
    echo "LABEL_REPAIR_DIFF issue=${issue_num} before=$(echo "${labels_json}" | jq -c .) after=$(echo "${updated_labels_json}" | jq -c .) add=$(echo "${plan_json}" | jq -c '.add // []') remove=$(echo "${plan_json}" | jq -c '.remove // []')" >&2
    emit_event "LABEL_REPAIR_DIFF" \
      "issue=${issue_num}" \
      "before=$(echo "${labels_json}" | jq -c .)" \
      "after=$(echo "${updated_labels_json}" | jq -c .)" \
      "add=$(echo "${plan_json}" | jq -c '.add // []')" \
      "remove=$(echo "${plan_json}" | jq -c '.remove // []')"
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
  branch_ref="$(printf '%s' "${branch_name}" | jq -sRr '@uri')"

  if gh_error="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${branch_ref}" 2>&1 >/dev/null)"; then
    return 0
  fi

  if printf '%s' "${gh_error}" | grep -Eqi '(^gh: Not Found|HTTP 404|404 Not Found|status code 404|\bnot found\b)'; then
    return 1
  fi

  echo "::warning::Unable to verify integration branch '${branch_name}' due to GitHub API error; assuming it still exists." >&2
  return 0
}

_branch_head_sha() {
  local branch_name="$1"
  [ -n "${branch_name}" ] || return 1

  local branch_ref
  local branch_sha
  branch_ref="$(printf '%s' "${branch_name}" | jq -sRr '@uri')"
  branch_sha="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/git/ref/heads/${branch_ref}" --jq '.object.sha // ""' 2>/dev/null || echo "")"
  [[ "${branch_sha}" =~ ^[0-9A-Fa-f]{7,64}$ ]] || return 1
  printf '%s' "${branch_sha}"
  return 0
}

# Returns the integration branch's "ahead_by" count vs the default branch via
# GitHub's compare API. Stdout: integer count (0 = default branch contains the
# integration tip). Exit 0 on success, 1 on API or parse error.
#
# Callers should fail closed on exit 1: treat an unknown ahead_by as "default
# does NOT contain integration tip" and refuse to advance any state machine
# whose contract requires the integration tip to have landed on default
# (project_complete, mark_validation_complete, finalize_integration_merge_if_needed).
# Mirrors the fail-closed posture of the e2e-smoke-test label guard at
# review_autofix.yml:4948-4951. See shubhodeep1/binance-blessings#135 for the
# regression that motivated this helper.
#
# Special cases that return ahead_by=0 (success, no drift) without calling the
# compare API:
#  * integration_branch empty or equal to default_branch
#  * integration_branch no longer exists on the remote (e.g. the legitimate
#    finalize path's `gh pr merge --squash --delete-branch` already ran). A
#    deleted branch cannot be ahead of default, and callers MUST be able to
#    finalize cleanly in that state — the steady-state post-merge condition.
#
# API hygiene (§15): callers in tight inner loops should cache the result for
# the cycle rather than re-invoking per iteration.
_integration_branch_ahead_of_default() {
  local integration_branch="$1"
  local default_branch="${2:-main}"
  if [ -z "${integration_branch}" ] || [ -z "${default_branch}" ]; then
    echo "0"
    return 0
  fi
  if [ "${integration_branch}" = "${default_branch}" ]; then
    echo "0"
    return 0
  fi
  # If the integration branch is gone (deleted by `gh pr merge --delete-branch`
  # during the legitimate finalize path), there's no drift to detect. Treat as
  # fully contained so the caller's pinned "merged" state remains valid.
  # integration_branch_exists fails-open on API error (assumes the branch is
  # still there), so a flaky API does NOT mask drift here — it falls through
  # to the compare call below, which can then fail closed as usual.
  if ! integration_branch_exists "${integration_branch}"; then
    echo "0"
    return 0
  fi
  local integration_ref default_ref
  integration_ref="$(printf '%s' "${integration_branch}" | jq -sRr '@uri')"
  default_ref="$(printf '%s' "${default_branch}" | jq -sRr '@uri')"
  local ahead_by
  if ! ahead_by="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/compare/${default_ref}...${integration_ref}" --jq '.ahead_by')"; then
    return 1
  fi
  if ! [[ "${ahead_by}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  echo "${ahead_by}"
  return 0
}

compute_cycle_integration_ahead_by() {
	CWS_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
	[ "${CWS_INTEGRATION_BRANCH}" = "null" ] && CWS_INTEGRATION_BRANCH=""
	CWS_DEFAULT_BRANCH=""
	if [ -n "${CWS_INTEGRATION_BRANCH}" ]; then
		CWS_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "")"
		if [ -z "${CWS_DEFAULT_BRANCH}" ]; then
			CWS_AHEAD_BY=""
			echo "::warning::  [check-wave-status] Could not resolve default branch via GitHub API; passing empty ahead_by so check-wave-status fails closed and keeps project_complete=false."
		elif CWS_AHEAD_BY="$(_integration_branch_ahead_of_default "${CWS_INTEGRATION_BRANCH}" "${CWS_DEFAULT_BRANCH}")"; then
			:
		else
			CWS_AHEAD_BY=""
			echo "::warning::  [check-wave-status] Compare API failed for ${CWS_DEFAULT_BRANCH}...${CWS_INTEGRATION_BRANCH}; failing closed (project_complete forced to false this tick)."
		fi
	else
		# No integration branch (default-branch-only flow): ahead_by is trivially
		# 0 since there is no integration→default merge gate to honour.
		CWS_AHEAD_BY="0"
	fi
}

# Per-project effective backpressure threshold.
#
# ORCH_INTEGRATION_MAX_AHEAD_COMMITS is a floor, not the whole story. A
# project's own planned sub-issues squash-merge into the integration branch
# one commit at a time, driving ahead_by up toward (and past) that floor
# *before* the integration->default PR can drain it — that PR is gated on
# project completion (the readiness gate only promotes the eager draft once
# the tracking issue reaches validated/ready-to-merge). With a flat floor
# below the project's commit count this self-deadlocks: backpressure pauses
# the very sub-issue merges needed to reach completion, completion is what
# lets the integration PR drain, and draining is what clears backpressure.
#
# Raise the effective threshold to (planned issue count + margin) so a
# project's own merges never trip backpressure, while genuinely anomalous
# over-drift (e.g. a runaway fix-up loop far beyond the planned count) still
# trips it. Fails open to the configured floor when the planned count is
# unavailable (missing/unreadable STATE_FILE, malformed JSON, non-numeric
# planned-count fields). Cached per tracking-issue loop: STATE_FILE is
# rewritten once at the top of the loop and the planned issue count is stable
# for the rest of the cycle. Callers that want the cache benefit must invoke
# the helper directly with an output-variable name; command substitution would
# run it in a subshell and lose the cached value.
_integration_backpressure_effective_threshold() {
	local outvar="${1:-}"
	if [ -n "${_INTEGRATION_BACKPRESSURE_EFFECTIVE_THRESHOLD_CACHE+set}" ]; then
		if [ -n "${outvar}" ]; then
			printf -v "${outvar}" '%s' "${_INTEGRATION_BACKPRESSURE_EFFECTIVE_THRESHOLD_CACHE}"
		else
			printf '%s' "${_INTEGRATION_BACKPRESSURE_EFFECTIVE_THRESHOLD_CACHE}"
		fi
		return 0
	fi

	local base="${ORCH_INTEGRATION_MAX_AHEAD_COMMITS}"
	local resolved_threshold="${base}"
	local planned=""
	if [ -n "${STATE_FILE:-}" ] && [ -f "${STATE_FILE}" ]; then
		planned="$(jq -r '((.total_issues // ([.waves[]?.issues? | length] | add)) // 0) | tostring' "${STATE_FILE}" 2>/dev/null || echo "")"
	fi
	if [[ "${planned}" =~ ^[0-9]+$ ]]; then
		local floor=$(( planned + ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN ))
		if [ "${floor}" -gt "${base}" ]; then
			resolved_threshold="${floor}"
		fi
	fi
	_INTEGRATION_BACKPRESSURE_EFFECTIVE_THRESHOLD_CACHE="${resolved_threshold}"
	if [ -n "${outvar}" ]; then
		printf -v "${outvar}" '%s' "${resolved_threshold}"
	else
		printf '%s' "${resolved_threshold}"
	fi
}

integration_backpressure_active_for_ahead_by() {
	local ahead_by="$1"
	local threshold
	_integration_backpressure_effective_threshold threshold
	[[ "${ahead_by}" =~ ^[0-9]+$ ]] && [ "${ahead_by}" -ge "${threshold}" ]
}

refresh_integration_backpressure_gate_after_merge() {
	local prior_block="${INTEGRATION_BACKPRESSURE_BLOCK_MERGES:-false}"

	compute_cycle_integration_ahead_by
	if ! [[ "${CWS_AHEAD_BY}" =~ ^[0-9]+$ ]]; then
		INTEGRATION_BACKPRESSURE_BLOCK_MERGES="${prior_block}"
		return 1
	fi

	INTEGRATION_BACKPRESSURE_BLOCK_MERGES="false"
	if integration_backpressure_active_for_ahead_by "${CWS_AHEAD_BY}"; then
		INTEGRATION_BACKPRESSURE_BLOCK_MERGES="true"
	fi
	return 0
}

latest_force_merge_label_event_json() {
	local events_json
	local latest_event

	if [ -n "${FORCE_MERGE_LABEL_EVENT_JSON_CACHE+set}" ]; then
		[ -n "${FORCE_MERGE_LABEL_EVENT_JSON_CACHE}" ] || return 1
		printf '%s' "${FORCE_MERGE_LABEL_EVENT_JSON_CACHE}"
		return 0
	fi

	# Existing cycle-local state caches cover tracking comments, labels, and PR
	# payloads, but none retain who applied the latest ai:force-merge label.
	# A single bounded GET /issues/{n}/events?per_page=100 is the smallest safe
	# fallback for actor attribution when the bypass is first applied for a SHA.
	if ! events_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/events?per_page=100" 2>/dev/null)"; then
		FORCE_MERGE_LABEL_EVENT_JSON_CACHE=""
		return 1
	fi

	latest_event="$(printf '%s' "${events_json}" | jq -c '
		[
			.[]
			| select((.event // "") == "labeled" and (.label.name // "") == "ai:force-merge")
		]
		| sort_by((.created_at // ""), (.id // 0))
		| last // empty
	' 2>/dev/null || echo "")"
	if [ -z "${latest_event}" ] || [ "${latest_event}" = "null" ]; then
		FORCE_MERGE_LABEL_EVENT_JSON_CACHE=""
		return 1
	fi

	FORCE_MERGE_LABEL_EVENT_JSON_CACHE="${latest_event}"
	printf '%s' "${latest_event}"
	return 0
}

reconcile_integration_backpressure_label() {
	local integration_branch="$1"
	local default_branch="$2"
	local ahead_by="$3"
	local final_pr="$4"
	local label_name="ai:integration-backpressure"
	local label_present="false"
	local effective_threshold=""

	[ -n "${integration_branch}" ] || return 1
	if has_label "${TRACKING_LABELS:-[]}" "${label_name}"; then
		label_present="true"
	fi

	if integration_backpressure_active_for_ahead_by "${ahead_by}"; then
		_integration_backpressure_effective_threshold effective_threshold
		if [ "${label_present}" != "true" ]; then
			ensure_label_exists "${label_name}" >/dev/null 2>&1 || true
			if gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" \
				--add-label "${label_name}" >/dev/null 2>&1; then
				TRACKING_LABELS="$(printf '%s' "${TRACKING_LABELS:-[]}" | jq -c --arg label "${label_name}" '(. + [$label]) | unique' 2>/dev/null || echo '["ai:integration-backpressure"]')"
				echo "BACKPRESSURE_TRIGGERED tracking_issue=${TRACKING_NUM} integration_branch=${integration_branch} default_branch=${default_branch:-unknown} ahead_by=${ahead_by} threshold=${ORCH_INTEGRATION_MAX_AHEAD_COMMITS} effective_threshold=${effective_threshold} final_pr=${final_pr:-0}"
			else
				echo "::warning::[backpressure] failed to add ${label_name} to tracking issue #${TRACKING_NUM}; merge gate remains active this cycle." >&2
			fi
		fi
		return 0
	fi

	if ! [[ "${ahead_by}" =~ ^[0-9]+$ ]]; then
		return 1
	fi

	if [ "${label_present}" = "true" ]; then
		_integration_backpressure_effective_threshold effective_threshold
		if gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" \
			--remove-label "${label_name}" >/dev/null 2>&1; then
			TRACKING_LABELS="$(printf '%s' "${TRACKING_LABELS:-[]}" | jq -c --arg label "${label_name}" 'map(select(. != $label))' 2>/dev/null || echo '[]')"
			echo "BACKPRESSURE_CLEARED tracking_issue=${TRACKING_NUM} integration_branch=${integration_branch} default_branch=${default_branch:-unknown} ahead_by=${ahead_by} threshold=${ORCH_INTEGRATION_MAX_AHEAD_COMMITS} effective_threshold=${effective_threshold} final_pr=${final_pr:-0}"
		else
			echo "::warning::[backpressure] failed to remove ${label_name} from tracking issue #${TRACKING_NUM}; will retry after the next numeric compare result." >&2
		fi
	fi

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
  local _tracking_labels
  local reason
  local tg_reason

  if [ -n "${integration_branch}" ]; then
    reason="Integration branch '${integration_branch}' is missing. It may have been deleted externally. Manual intervention required."
    tg_reason="missing integration branch '${integration_branch}'"
    jq --arg reason "${reason}" --arg branch "${integration_branch}" \
      '.status = "failed" |
       .final_merge_status = "failed" |
       .integration_branch = $branch |
       .final_merge_error = $reason' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  else
    reason="Integration branch is not set in state. Final merge cannot proceed until this is repaired. Manual intervention required."
    tg_reason="integration branch is not set in state"
    jq --arg reason "${reason}" \
      '.status = "failed" |
       .final_merge_status = "failed" |
       .final_merge_error = $reason' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  fi

  post_state_comment || true
  post_tracking_comment "## ❌ Integration branch missing

${reason}"
  _tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"
  handle_comprehensive_release_callback_if_needed "failed" "${_tracking_labels}" "${COMMENTS:-[]}"
  set_failed_completion_status_comment \
    "${reason} See the \"❌ Integration branch missing\" comment for the diagnostic detail."
  tg_cleanup_msgs "${TRACKING_NUM}"
  tg_notify "❌ Project #${TRACKING_NUM} failed: ${tg_reason}."
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

prepare_tracking_judge_checkout() {
  local integration_branch="$1"
  local default_branch="$2"
  local target_branch="${default_branch:-main}"
  local target_ref=""

  JUDGE_EXECUTION_SOURCE="default_branch"
  JUDGE_EXECUTION_REF=""
  JUDGE_CONTEXT_SENTINEL_PRESENT="false"
  JUDGE_CONTEXT_SENTINEL_VALUE=""

  if [ -n "${integration_branch}" ]; then
    JUDGE_EXECUTION_SOURCE="integration_branch"
    target_branch="${integration_branch}"
    if ! integration_branch_exists "${integration_branch}"; then
      mark_integration_branch_missing_failed "${integration_branch}"
      return 1
    fi
  fi

  if target_ref="$(resolve_branch_analysis_ref "${target_branch}")"; then
    if ! git checkout -q "${target_branch}" >/dev/null 2>&1 && ! git checkout -q "${target_ref}" >/dev/null 2>&1; then
      # Workflow support files (scripts/, prompts/, .github/ai/) installed
      # from coding-workflows are untracked and can block checkout when the
      # target branch has overlapping paths.  Back them up, force-checkout,
      # then restore so render_prompt.sh et al. remain available.
      local _wf_backup
      _wf_backup="$(mktemp -d)"
      for _d in scripts prompts; do
        [ -d "${_d}" ] && cp -a "${_d}" "${_wf_backup}/${_d}" 2>/dev/null || true
      done
      [ -d ".github/ai" ] && { mkdir -p "${_wf_backup}/github_ai"; cp -a ".github/ai/." "${_wf_backup}/github_ai/" 2>/dev/null || true; }

      if git checkout -f -q "${target_ref}" >/dev/null 2>&1; then
        # Force checkout succeeded — restore workflow support files
        for _d in scripts prompts; do
          [ -d "${_wf_backup}/${_d}" ] && { mkdir -p "${_d}"; cp -a "${_wf_backup}/${_d}/." "${_d}/" 2>/dev/null || true; }
        done
        [ -d "${_wf_backup}/github_ai" ] && { mkdir -p ".github/ai"; cp -a "${_wf_backup}/github_ai/." ".github/ai/" 2>/dev/null || true; }
        rm -rf "${_wf_backup}"
      else
        rm -rf "${_wf_backup}"
        if [ -n "${integration_branch}" ]; then
          echo "::error::Integration branch '${integration_branch}' exists but could not be checked out for judge context (resolved ref '${target_ref}')." >&2
        else
          echo "::error::Default branch '${target_branch}' could not be checked out for judge context (resolved ref '${target_ref}')." >&2
        fi
        return 1
      fi
    fi
  else
    if [ -n "${integration_branch}" ]; then
      echo "::error::Integration branch '${integration_branch}' exists but its analysis ref could not be resolved for judge context." >&2
    else
      echo "::error::Default branch '${target_branch}' could not be resolved for judge context." >&2
    fi
    return 1
  fi

  JUDGE_EXECUTION_REF="$(git symbolic-ref --short HEAD 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
  if [ -f .orchestrator_judge_context_sentinel.txt ]; then
    JUDGE_CONTEXT_SENTINEL_PRESENT="true"
    JUDGE_CONTEXT_SENTINEL_VALUE="$(head -c 200 .orchestrator_judge_context_sentinel.txt 2>/dev/null | head -n 1 | tr -d '\r')"
  fi

  echo "  Judge execution context for tracking #${TRACKING_NUM}: source=${JUDGE_EXECUTION_SOURCE} ref=${JUDGE_EXECUTION_REF} sentinel_present=${JUDGE_CONTEXT_SENTINEL_PRESENT}"
  if [ "${JUDGE_CONTEXT_SENTINEL_PRESENT}" = "true" ] && [ -n "${JUDGE_CONTEXT_SENTINEL_VALUE}" ]; then
    echo "  Judge context sentinel for tracking #${TRACKING_NUM}: ${JUDGE_CONTEXT_SENTINEL_VALUE}"
  fi
  return 0
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
    | sed '1{/^[[:xdigit:]]\{40,\}$/d};/^$/d' \
    | jq -Rsc 'split("\n") | map(select(length > 0)) | unique'
}

merge_tree_conflict_fingerprint() {
  local conflict_paths_json="$1"
  local default_ref="${2:-}"
  local integration_ref="${3:-}"
  if [ "${conflict_paths_json}" = '[]' ] && [ -n "${default_ref}" ] && [ -n "${integration_ref}" ]; then
    printf '%s|%s|%s' "${conflict_paths_json}" "$(git rev-parse --verify "${default_ref}" 2>/dev/null || echo '')" "$(git rev-parse --verify "${integration_ref}" 2>/dev/null || echo '')" \
      | sha256sum | awk '{print $1}'
    return 0
  fi
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
  SYNC_SUPERSEDED_CONFIDENT="true"

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
    if ! timeline_prs="$(_issue_cross_ref_pr_numbers_unique "${issue_num}" 2>/dev/null)"; then
      SYNC_SUPERSEDED_CONFIDENT="false"
      continue
    fi
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
	  SYNC_SUPERSEDED_CONFIDENT="false"
	  echo "  [superseded-check] Skipping PR #${pr_num}: unable to fetch state." >&2
	  continue
	fi
	if [ "${pr_state}" = "open" ] && [ "${pr_merged}" != "true" ]; then
	  SYNC_SUPERSEDED_CONFIDENT="true"
	  return 0
	fi

    if ! pr_files_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}/files?per_page=100" 2>/dev/null \
      | jq -sc '[.[]? | .[]? | .filename] | unique' 2>/dev/null)"; then
      SYNC_SUPERSEDED_CONFIDENT="false"
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
    SYNC_SUPERSEDED_CONFIDENT="false"
    return 0
  fi
  if ! integration_ref="$(resolve_branch_analysis_ref "${integration_branch}")"; then
    SYNC_SUPERSEDED_CONFIDENT="false"
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
        integration_conflict_unresolved_ticks: (.integration_conflict_unresolved_ticks // 0),
        integration_conflict_total_dispatches: (.integration_conflict_total_dispatches // 0),
        merged_issue_fingerprints: (.merged_issue_fingerprints // {}),
        final_merge_attempt_count: (.final_merge_attempt_count // 0),
        last_main_squash_at_utc: (.last_main_squash_at_utc // null),
        integration_stale_last_alerted_at_utc: (.integration_stale_last_alerted_at_utc // null),
        judge_last_fingerprint: (.judge_last_fingerprint // ""),
        judge_fingerprint_repeat_count: (.judge_fingerprint_repeat_count // 0)
      }' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
}

extract_autofix_resolver_retry_state_from_pr_body() {
  # Read the PR body before invoking `python3 - <<'PY'`: the heredoc
  # consumes stdin for the script itself, so piping directly into
  # python would otherwise drop the body and make the extractor fail
  # closed on every call.
  local retry_state_body=""
  retry_state_body="$(cat)"

  RETRY_STATE_BODY="${retry_state_body}" python3 - <<'PY'
from __future__ import annotations

import json
import os
import re
import sys

body = os.environ.get("RETRY_STATE_BODY", "").replace("\r\n", "\n").replace("\r", "\n")
pattern = re.compile(r"<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1\n(.*?)\n-->", re.S)
matches = pattern.findall(body)
for raw in reversed(matches):
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    continue
  if isinstance(parsed, dict):
    sys.stdout.write(json.dumps(parsed, sort_keys=True, ensure_ascii=True))
    raise SystemExit(0)
raise SystemExit(0)
PY
}

normalize_judge_justification_for_fingerprint() {
  local raw_text="${1-}"
  # Pass input via env var, not stdin: the GHA Ubuntu 24.04 runner's
  # `bash -e {0}` shell closes the heredoc-bound FD 3 before exec'ing
  # python3, so the previous `python3 /dev/fd/3 3<<'PY'` form failed
  # with "can't open file '/dev/fd/3': [Errno 2]" and turned every
  # poller invocation that touched judge fingerprints into a non-zero
  # exit. Reading the text from RAW_TEXT and the script from stdin
  # (`python3 -`) sidesteps the FD-3 dance entirely.
  RAW_TEXT="${raw_text}" python3 - <<'PY'
import os
import re

text = os.environ.get("RAW_TEXT", "")
if not text:
    print("")
    raise SystemExit(0)

text = text.replace("\r\n", "\n").replace("\r", "\n")
text = re.sub(r'\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b', ' ', text)
text = re.sub(r'\b\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}\b', ' ', text)
text = re.sub(r'\b(?:judge\s+)?cycle\s*[#:=-]?\s*\d+(?:\s*/\s*\d+)?\b', ' ', text, flags=re.IGNORECASE)
text = re.sub(
    r'((?:\./|/)?(?:[^/\s:]+/)*[^/\s:]+)(?::\d+(?:-\d+)?)(?::\d+(?:-\d+)?)?',
    lambda match: match.group(1),
    text,
)
text = re.sub(r'\s+', ' ', text).strip()
print(text)
PY
}

judge_justification_fingerprint() {
  local normalized_text="${1-}"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "${normalized_text}" | sha256sum | awk '{print $1}'
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "${normalized_text}" | shasum -a 256 | awk '{print $1}'
    return 0
  fi
  printf '%s' "${normalized_text}" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

# Capture merged-sub-issue intent fingerprints for a sub-issue whose
# linked PR has just landed on the integration branch. The resulting
# regex allowlist/denylist is used by the integration-sync conflict
# resolver (review_autofix.yml) to verify that the resolver's commit
# preserves the sub-issue's intent — must_contain patterns are derived
# from lines the sub-issue NET-ADDED, must_not_contain from lines it
# NET-REMOVED. Any stripped-line that appears on both sides of the
# unified diff (e.g. a bare call wrapped in a new if/else fallback)
# is load-bearing on the post-state and is excluded from BOTH sets;
# the verifier also defensively skips self-contradictory pairs still
# present in legacy state entries.
#
# In addition to the regex-based pair, every file the merged sub-PR
# REMOVED outright (PR diff lists ``+++ /dev/null`` against the path)
# is recorded under ``must_not_exist`` so a downstream back-merge
# silently reintroducing the deleted file is rejected by
# scripts/verify_integration_fingerprints.py. The path-existence
# contract is enforced PATH-AGNOSTICALLY — the resolver-safe
# ALLOWED_PREFIXES allowlist below applies only to text-regex capture
# (where it filters binary-prone / generated / out-of-scope paths),
# never to ``must_not_exist`` (where the contract is a cheap binary
# check and the deletion intent is unambiguous regardless of where in
# the consumer repo's tree the file lives).
#
# Storage: top-level state field `merged_issue_fingerprints`, an
# object keyed by the sub-issue's GitHub issue number (string). Each
# entry is:
#   {
#     "issue":            <int>,           # github issue number
#     "pr":               <int>,           # github PR number
#     "captured_at":      <iso8601>,
#     "must_contain":     [ {file, regex}, ... ],
#     "must_not_contain": [ {file, regex}, ... ],
#     "must_not_exist":   [ {file},         ... ]
#   }
#
# Deliberately fail-open: any error in capture (no PR found, network
# failure, malformed diff) logs a warning and leaves state unchanged.
# Going-forward only (Q4:A): existing already-merged sub-issues on
# in-flight integration branches are NOT backfilled.
#
# Caps:
#   - Up to FINGERPRINT_PER_FILE_CAP patterns per file per direction.
#   - Files outside the resolver-safe allowlist
#     (.github/, scripts/, prompts/, ai-memory/, tests/,
#     workflow-templates/, docs/, db/contracts/, and root
#     {agents,README,CLAUDE}.md) are skipped FOR TEXT-REGEX CAPTURE
#     ONLY (binary-prone, generated, or out-of-scope for resolver
#     edits). ``must_not_exist`` capture has NO allowlist filter.
#   - Patterns shorter than FINGERPRINT_MIN_PATTERN_CHARS (after trim)
#     are skipped — too generic to fingerprint reliably.
#   - Patterns containing only whitespace, only braces/brackets, or
#     bash conflict-marker characters are skipped.
#
# Usage: capture_intent_fingerprints_for_merged_subissue <issue_num> <pr_num>
FINGERPRINT_PER_FILE_CAP="${FINGERPRINT_PER_FILE_CAP:-12}"
FINGERPRINT_MIN_PATTERN_CHARS="${FINGERPRINT_MIN_PATTERN_CHARS:-12}"
capture_intent_fingerprints_for_merged_subissue() {
  local issue_num="$1"
  local pr_num="$2"
  [ -f "${STATE_FILE}" ] || return 0
  [[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
  [[ "${pr_num}" =~ ^[0-9]+$ ]] || return 0
  ensure_integration_conflict_state_fields

  # Skip if state already has fingerprints for this issue (idempotent).
  local existing
  existing="$(jq -r --arg k "${issue_num}" '.merged_issue_fingerprints[$k] // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [ -n "${existing}" ]; then
    return 0
  fi

  local diff_file
  diff_file="$(mktemp "${TMPDIR:-/tmp}/intent_fp_diff.XXXXXX")"
  if ! gh api -H 'Accept: application/vnd.github.diff' \
    "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" > "${diff_file}" 2>/dev/null; then
    echo "::warning::[intent-fp] Failed to fetch diff for PR #${pr_num} (issue #${issue_num}); fingerprint capture skipped."
    rm -f "${diff_file}"
    return 0
  fi
  if [ ! -s "${diff_file}" ]; then
    rm -f "${diff_file}"
    return 0
  fi

  # Resolve a fresh integration-branch commit for the post-merge presence
  # filter inside the python heredoc. Fingerprint capture runs after the
  # orchestrator detects the sub-issue PR has merged onto the integration
  # branch, so a successful fetch's FETCH_HEAD already reflects the
  # post-merge state. Fail-open: if the branch name is invalid, origin is
  # unavailable, the timeout wrapper is missing, or the fetch fails, the
  # heredoc skips the post-merge filter rather than reading a potentially
  # stale local ref (the existing
  # net-change / substring-overlap filters still apply, and the verifier-
  # side partial-removal defense catches the remaining false positives).
  local integration_branch_for_capture integration_ref_for_capture="" integration_fetch_timeout_secs="${GIT_COMMAND_TIMEOUT_SECS:-30}"
  case "${integration_fetch_timeout_secs}" in
    ''|*[!0-9]*) integration_fetch_timeout_secs=30 ;;
  esac
  if [ "${integration_fetch_timeout_secs}" -le 0 ]; then
    integration_fetch_timeout_secs=30
  fi
  integration_branch_for_capture="$(jq -r '.integration_branch // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [ -n "${integration_branch_for_capture}" ] && [ "${integration_branch_for_capture}" != "null" ]; then
    if ! command -v timeout >/dev/null 2>&1; then
      echo "::notice::capture_intent_fingerprints_for_merged_subissue: skipping post-merge presence filter for '${integration_branch_for_capture}' because 'timeout' is not available on this runner." >&2
    elif git check-ref-format "refs/heads/${integration_branch_for_capture}" >/dev/null 2>&1 \
      && git remote get-url origin >/dev/null 2>&1 \
      && env GIT_TERMINAL_PROMPT=0 timeout "${integration_fetch_timeout_secs}s" \
        git fetch --no-tags --quiet origin "${integration_branch_for_capture}" >/dev/null 2>&1; then
      integration_ref_for_capture="$(git rev-parse --verify FETCH_HEAD 2>/dev/null || echo "")"
    else
      echo "::notice::capture_intent_fingerprints_for_merged_subissue: skipping post-merge presence filter for '${integration_branch_for_capture}' because integration ref refresh failed or timed out after ${integration_fetch_timeout_secs}s." >&2
    fi
  fi

  local fp_json
  fp_json="$(FINGERPRINT_PER_FILE_CAP="${FINGERPRINT_PER_FILE_CAP}" \
    FINGERPRINT_MIN_PATTERN_CHARS="${FINGERPRINT_MIN_PATTERN_CHARS}" \
    GIT_COMMAND_TIMEOUT_SECS="${integration_fetch_timeout_secs}" \
    FINGERPRINT_POST_MERGE_REF="${integration_ref_for_capture}" \
    python3 - "${diff_file}" <<'PY' 2>/dev/null || true
import json, os, re, subprocess, sys
from collections import Counter

cap = int(os.environ.get("FINGERPRINT_PER_FILE_CAP", "12"))
minlen = int(os.environ.get("FINGERPRINT_MIN_PATTERN_CHARS", "12"))
git_timeout_secs = int(os.environ.get("GIT_COMMAND_TIMEOUT_SECS", "30"))

ALLOWED_PREFIXES = (
    ".github/", "scripts/", "prompts/", "ai-memory/",
    "tests/", "workflow-templates/", "docs/", "db/contracts/",
    "agents.md", "README.md", "CLAUDE.md",
)
SKIP_RE = re.compile(r"^[\s{}\[\]()<>=+\-*]*$")

def acceptable(path: str) -> bool:
    return any(path.startswith(p) or path == p.rstrip("/") for p in ALLOWED_PREFIXES)

def cleanup(line: str) -> str:
    return line.rstrip("\n").rstrip("\r")

def is_useful(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < minlen:
        return False
    if SKIP_RE.match(stripped):
        return False
    # Skip lines that look like raw conflict markers (paranoia).
    if stripped.startswith(("<<<<<<<", "=======", ">>>>>>>")):
        return False
    return True

per_file_added: dict[str, list[str]] = {}
per_file_removed: dict[str, list[str]] = {}
# Path-agnostic list of files the PR deleted outright (``+++ /dev/null``
# paired with ``--- a/<path>``). NO ALLOWED_PREFIXES filter — see the
# function docstring for the rationale.
removed_paths: list[str] = []

current = None
# Most recent ``--- a/<path>`` line seen while scanning a diff header.
# Captured separately so that, on encountering ``+++ /dev/null``, we
# know which path the PR deleted (the unified-diff format pairs the
# two lines but processes ``---`` before ``+++``).
prev_minus_path: str | None = None
diff_path = sys.argv[1]
try:
    with open(diff_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = cleanup(raw)
            if line.startswith("diff --git "):
                current = None
                prev_minus_path = None
                continue
            if line.startswith("--- a/"):
                prev_minus_path = line[6:].strip()
                continue
            if line.startswith("--- /dev/null"):
                # File didn't exist before this PR (pure addition);
                # no deletion to record on the ``+++`` line that
                # follows.
                prev_minus_path = None
                continue
            if line.startswith("+++ b/"):
                current = line[6:].strip()
                prev_minus_path = None
                continue
            if line.startswith("+++ /dev/null"):
                # File was deleted by this PR.  Record the path
                # captured from the preceding ``--- a/<path>`` line
                # under removed_paths without applying the
                # text-regex ALLOWED_PREFIXES filter — the deletion
                # intent is a path-level binary fact, enforceable
                # regardless of where in the consumer repo's tree
                # the file lives.
                if prev_minus_path:
                    removed_paths.append(prev_minus_path)
                current = None
                prev_minus_path = None
                continue
            if current is None or current == "/dev/null":
                continue
            if not acceptable(current):
                continue
            if line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                if is_useful(content):
                    per_file_added.setdefault(current, []).append(content)
            elif line.startswith("-") and not line.startswith("---"):
                content = line[1:]
                if is_useful(content):
                    per_file_removed.setdefault(current, []).append(content)
except Exception:
    print("{}")
    sys.exit(0)

def to_patterns(by_file: dict[str, list[str]]) -> list[dict]:
    out: list[dict] = []
    for path, lines in by_file.items():
        seen = set()
        kept: list[str] = []
        for raw in lines:
            stripped = raw.strip()
            if stripped in seen:
                continue
            seen.add(stripped)
            kept.append(stripped)
            if len(kept) >= cap:
                break
        for stripped in kept:
            out.append({"file": path, "regex": re.escape(stripped)})
    return out

def subtract_shared(lines: list[str], shared_counts: Counter[str]) -> list[str]:
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if shared_counts.get(stripped, 0) > 0:
            shared_counts[stripped] -= 1
            continue
        out.append(raw)
    return out

# Net-change filter: drop any stripped line that a PR both removed and
# re-added (same stripped content appearing on both sides of the unified
# diff). This happens naturally when a PR wraps a bare call in an
# if/else fallback, moves code inside a conditional, or reformats a
# block — the line remains in the post-state, so capturing it as
# must_not_contain would false-trigger on every downstream commit that
# preserves the PR's intent, and simultaneously capturing it as
# must_contain creates a self-contradictory contract. Subtract the
# shared occurrence count from BOTH sets so only true net additions /
# true net removals survive as fingerprints.
for path in set(per_file_added) | set(per_file_removed):
    added_counts = Counter(l.strip() for l in per_file_added.get(path, []))
    removed_counts = Counter(l.strip() for l in per_file_removed.get(path, []))
    shared_counts = added_counts & removed_counts
    if not shared_counts:
        continue
    if path in per_file_added:
        per_file_added[path] = subtract_shared(per_file_added[path], shared_counts.copy())
        if not per_file_added[path]:
            del per_file_added[path]
    if path in per_file_removed:
        per_file_removed[path] = subtract_shared(per_file_removed[path], shared_counts.copy())
        if not per_file_removed[path]:
            del per_file_removed[path]

# Substring-overlap filter: drop any removed-line whose stripped text is
# a literal substring of any added-line stripped text on the same file.
# Capture below wraps each kept line with re.escape(...), so substring
# containment in the captured text is equivalent to substring
# containment under re.search at verify time. When the added line
# supersedes the removed line by extending it (e.g. a sub-issue
# appended " When X is enabled, accepted ..." to "...cohort-mix
# rollouts."), keeping the shorter removed text as a must_not_contain
# produces a structurally unsatisfiable pair: any tree that satisfies
# the longer must_contain also matches the shorter must_not_contain,
# and the resolver burns its 3-attempt retry budget then times out at
# the step wall-clock cap on a hunk it cannot make pass. Drop the
# must_not_contain side; the must_contain side already enforces the
# stronger intent. Companion verifier-side dedup at
# scripts/verify_integration_fingerprints.py covers state files
# captured before this filter landed.
for path, added_lines in list(per_file_added.items()):
    if path not in per_file_removed:
        continue
    added_stripped = {l.strip() for l in added_lines if l.strip()}
    if not added_stripped:
        continue
    new_removed: list[str] = []
    for raw in per_file_removed[path]:
        stripped = raw.strip()
        if stripped and any(stripped != a and stripped in a for a in added_stripped):
            continue
        new_removed.append(raw)
    if new_removed:
        per_file_removed[path] = new_removed
    else:
        del per_file_removed[path]

# De-duplicate removed_paths preserving first-seen order. A
# unified diff lists at most one ``+++ /dev/null`` per file, but
# the same path can legitimately appear across multiple diff
# blocks in pathological histories (e.g. rename detection
# disabled); preserve order so the captured fingerprint is stable
# across re-runs and downstream operator inspection.
seen_removed_paths: set[str] = set()
removed_paths_unique: list[str] = []
for p in removed_paths:
    if not isinstance(p, str) or not p:
        continue
    if p in seen_removed_paths:
        continue
    seen_removed_paths.add(p)
    removed_paths_unique.append(p)

# Post-merge presence filter: drop any removed line whose stripped
# text still appears in the post-merge file content. This catches
# the multi-occurrence partial-removal case: a PR that removes line
# X from one position while leaving X at other positions in the same
# file. The unified-diff parser only sees the per-position removal,
# so X ends up in must_not_contain even though it survives in the
# post-merge tree. Without this filter the verifier later re-detects
# X (because re.search hits an unchanged occurrence) and reports a
# fake regression. The post-merge ref is the integration branch tip
# at capture time — capture runs after the orchestrator observes the
# sub-issue PR as merged, so the integration branch already reflects
# the merge. Fail-open: any git failure (no ref, file absent, decode
# error) leaves the candidate line in place; the verifier-side
# partial-removal defense in scripts/verify_integration_fingerprints.py
# provides the second line of defense for those leftover false
# positives.
post_merge_ref = (os.environ.get("FINGERPRINT_POST_MERGE_REF") or "").strip()
if post_merge_ref and per_file_removed:
    for path in list(per_file_removed.keys()):
        try:
            git_result = subprocess.run(
                ["git", "show", f"{post_merge_ref}:{path}"],
                capture_output=True,
                check=False,
                timeout=git_timeout_secs,
            )
        except Exception:
            continue
        if git_result.returncode != 0:
            continue
        try:
            post_merge_content = git_result.stdout.decode("utf-8", errors="replace")
        except Exception:
            continue
        kept_lines: list[str] = []
        for raw in per_file_removed[path]:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                if re.search(re.escape(stripped), post_merge_content):
                    continue
            except re.error:
                pass
            kept_lines.append(raw)
        if kept_lines:
            per_file_removed[path] = kept_lines
        else:
            del per_file_removed[path]

result = {
    "must_contain": to_patterns(per_file_added),
    "must_not_contain": to_patterns(per_file_removed),
    "must_not_exist": [{"file": p} for p in removed_paths_unique],
}
print(json.dumps(result))
PY
  )"
  rm -f "${diff_file}"

  if [ -z "${fp_json}" ]; then
    return 0
  fi
  if ! printf '%s' "${fp_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    return 0
  fi

  local mc_count nmc_count mne_count
  mc_count="$(printf '%s' "${fp_json}" | jq -r '.must_contain | length' 2>/dev/null || echo 0)"
  nmc_count="$(printf '%s' "${fp_json}" | jq -r '.must_not_contain | length' 2>/dev/null || echo 0)"
  mne_count="$(printf '%s' "${fp_json}" | jq -r '(.must_not_exist // []) | length' 2>/dev/null || echo 0)"
  if [ "${mc_count}" -eq 0 ] && [ "${nmc_count}" -eq 0 ] && [ "${mne_count}" -eq 0 ]; then
    return 0
  fi

  local now_iso
  now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  jq --arg k "${issue_num}" \
     --argjson issue "${issue_num}" \
     --argjson pr "${pr_num}" \
     --arg ts "${now_iso}" \
     --argjson fp "${fp_json}" \
     '.merged_issue_fingerprints[$k] = {
        issue: $issue,
        pr: $pr,
        captured_at: $ts,
        must_contain: $fp.must_contain,
        must_not_contain: $fp.must_not_contain,
        must_not_exist: ($fp.must_not_exist // [])
      }' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  echo "  [intent-fp] Captured fingerprints for issue #${issue_num} (PR #${pr_num}): must_contain=${mc_count} must_not_contain=${nmc_count} must_not_exist=${mne_count}"
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
  discovered="$(gh_retry gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --base "${default_branch}" \
    --head "${integration_branch}" \
    --json number \
    --jq '.[0].number // empty' 2>/dev/null || true)"

  if [ -z "${discovered}" ]; then
    local pr_url
    pr_url="$(gh_retry gh pr create \
      --repo "${GITHUB_REPOSITORY}" \
      --draft \
      --base "${default_branch}" \
      --head "${integration_branch}" \
      --title "feat: ${project_title}" \
      --body "Squash merge of orchestrator project #${TRACKING_NUM}.

This PR is created eagerly by the self-healing pipeline so that \`main\` <-> \`${integration_branch}\` drift can be resolved continuously rather than only at finalize time.

Refs #${TRACKING_NUM}" 2>/dev/null || true)"
    discovered="$(printf '%s\n' "${pr_url}" | grep -oE '/pull/[0-9]+' | tail -n1 | cut -d/ -f3 || true)"
    if [[ "${discovered}" =~ ^[0-9]+$ ]]; then
      echo "EAGER_DRAFT_PR_CREATED pr=${discovered} integration_branch=${integration_branch} tracking_issue=${TRACKING_NUM}" >&2
    else
      # A concurrent poll tick may have created the PR after our initial
      # list probe but before gh pr create returned. Re-list once so the
      # caller can still reuse that PR instead of failing this cycle.
      discovered="$(gh_retry gh pr list \
        --repo "${GITHUB_REPOSITORY}" \
        --state open \
        --base "${default_branch}" \
        --head "${integration_branch}" \
        --json number \
        --jq '.[0].number // empty' 2>/dev/null || true)"
    fi
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

build_eager_pr_validation_status_block() {
  local next_action_override="${1-}"
  local tracking_labels_json validation_cycle last_dispatch_cycle
  local last_run_info last_outcome run_id run_attempt run_url run_timestamp last_run_display next_action

  tracking_labels_json="${TRACKING_LABELS:-[]}"
  validation_cycle="$(jq -r '.validation_cycle // 1' "${STATE_FILE}" 2>/dev/null || echo 1)"
  last_dispatch_cycle="$(jq -r '.validation_last_dispatch_cycle // 0' "${STATE_FILE}" 2>/dev/null || echo 0)"
  last_run_info='{"run_id":"","run_attempt":0,"conclusion":"","raw_status":"","run_url":"","run_timestamp":""}'

  if [ "${ENABLE_VALIDATION}" = "true" ] \
    && { [ "${PROJECT_STATUS:-}" = "validating" ] \
      || [ "${PROJECT_STATUS:-}" = "validation-fixing" ] \
      || has_label "${tracking_labels_json}" "ai:validated" \
      || has_label "${tracking_labels_json}" "ai:validation-failed" \
      || has_label "${tracking_labels_json}" "ai:validate-failed" \
      || has_label "${tracking_labels_json}" "ai:harness-broken" \
      || [ "${last_dispatch_cycle}" != "0" ]; }; then
    last_run_info="$(get_last_validation_run_info)"
  fi

  last_outcome="$(printf '%s' "${last_run_info}" | jq -r '.raw_status // empty' 2>/dev/null || echo '')"
  if [ -z "${last_outcome}" ]; then
    last_outcome="$(printf '%s' "${last_run_info}" | jq -r '.conclusion // empty' 2>/dev/null || echo '')"
  fi
  run_id="$(printf '%s' "${last_run_info}" | jq -r '.run_id // ""' 2>/dev/null || echo '')"
  run_attempt="$(printf '%s' "${last_run_info}" | jq -r '.run_attempt // 0' 2>/dev/null || echo 0)"
  run_url="$(printf '%s' "${last_run_info}" | jq -r '.run_url // ""' 2>/dev/null || echo '')"
  run_timestamp="$(printf '%s' "${last_run_info}" | jq -r '.run_timestamp // ""' 2>/dev/null || echo '')"

  if [ -z "${last_outcome}" ]; then
    if has_label "${tracking_labels_json}" "ai:harness-broken"; then
      last_outcome="harness_error"
    elif has_label "${tracking_labels_json}" "ai:validation-failed" || has_label "${tracking_labels_json}" "ai:validate-failed"; then
      last_outcome="failed"
    elif has_label "${tracking_labels_json}" "ai:validated"; then
      last_outcome="passed"
    elif [ "${ENABLE_VALIDATION}" != "true" ]; then
      last_outcome="validation-disabled"
    else
      last_outcome="pending"
    fi
  fi

  last_run_display="not available"
  if [ -n "${run_url}" ] && [ -n "${run_id}" ] && [ "${run_id}" != "0" ]; then
    if [ "${run_attempt}" != "0" ]; then
      last_run_display="${run_url} (run ${run_id}, attempt ${run_attempt})"
    else
      last_run_display="${run_url} (run ${run_id})"
    fi
  elif [ -n "${run_url}" ]; then
    last_run_display="${run_url}"
  elif [ -n "${run_id}" ] && [ "${run_id}" != "0" ]; then
    if [ "${run_attempt}" != "0" ]; then
      last_run_display="run ${run_id}, attempt ${run_attempt}"
    else
      last_run_display="run ${run_id}"
    fi
  fi

  if [ -n "${next_action_override}" ]; then
    next_action="${next_action_override}"
  elif has_label "${tracking_labels_json}" "ai:harness-broken"; then
    next_action="Harness broken — see #${TRACKING_NUM}."
  elif has_label "${tracking_labels_json}" "ai:validation-failed" || has_label "${tracking_labels_json}" "ai:validate-failed"; then
    next_action="Validation failed — see #${TRACKING_NUM}."
  elif has_label "${tracking_labels_json}" "ai:validated"; then
    if has_label "${tracking_labels_json}" "ai:ready-to-merge"; then
      next_action="Validation passing — final merge may proceed."
    else
      next_action="Validation passing — awaiting \`ai:ready-to-merge\`."
    fi
  elif [ "${ENABLE_VALIDATION}" != "true" ]; then
    next_action="Validation disabled — final merge will proceed when the project is complete."
  elif [ "${PROJECT_STATUS:-}" = "validation-fixing" ]; then
    next_action="Validation fixing in progress — awaiting merged fix-up issues."
  else
    next_action="Awaiting validation."
  fi

  cat <<EOF
<!-- VALIDATION_STATUS_V1 -->
## Validation status

- Tracking issue: #${TRACKING_NUM}
- Validation cycle: ${validation_cycle}
- Last outcome: \`${last_outcome}\`
- Last run: ${last_run_display}
- Timestamp (UTC): ${run_timestamp:-not available}
- Next action: ${next_action}
<!-- /VALIDATION_STATUS_V1 -->
EOF
}

update_eager_pr_validation_status_section() {
  local pr_number="$1"
  local next_action_override="${2-}"
  local pr_json pr_state pr_body validation_block body_payload_file updated_body response_file

  [[ "${pr_number}" =~ ^[0-9]+$ ]] || return 1

  pr_json="$(_fetch_pr_json "${pr_number}")"
  pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
  if [ "${pr_state}" != "open" ]; then
    return 0
  fi

  pr_body="$(printf '%s' "${pr_json}" | jq -r '.body // ""' 2>/dev/null || echo '')"
  validation_block="$(build_eager_pr_validation_status_block "${next_action_override}")"
  updated_body="$(printf '%s' "${pr_body}" | VALIDATION_STATUS_BLOCK="${validation_block}" python3 -c '
from __future__ import annotations

import os
import re
import sys

body = sys.stdin.read().replace("\r\n", "\n").replace("\r", "\n")
block = os.environ.get("VALIDATION_STATUS_BLOCK", "").replace("\r\n", "\n").replace("\r", "\n").strip()
pattern = re.compile(r"(?ms)^<!-- VALIDATION_STATUS_V1 -->\n.*?\n<!-- /VALIDATION_STATUS_V1 -->\n?", re.M)
stripped = pattern.sub("", body).strip()
if stripped:
    print(f"{stripped}\n\n{block}")
else:
    print(block)
')"

  if [ "${updated_body}" = "${pr_body}" ]; then
    return 0
  fi

  body_payload_file="$(mktemp "${TMPDIR:-/tmp}/final_pr_body.XXXXXX")"
  response_file="$(mktemp "${TMPDIR:-/tmp}/final_pr_body_response.XXXXXX")"
  jq -n --arg body "${updated_body}" '{body: $body}' > "${body_payload_file}"
  if ! gh_retry_to_file "${response_file}" gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" \
    -X PATCH --input "${body_payload_file}"; then
    echo "::warning::[validation-status] failed to update PR #${pr_number} body; will retry on a later cycle." >&2
    head -c 4096 "${response_file}" >&2 || true
    echo >&2
    rm -f "${body_payload_file}" "${response_file}"
    return 1
  fi
  rm -f "${body_payload_file}" "${response_file}"
  return 0
}

project_is_validation_origin_terminal_failure() {
	local project_status="${1:-${PROJECT_STATUS:-}}"
	local tracking_labels="${2:-${TRACKING_LABELS:-[]}}"

	if [ "${project_status}" = "validation-failed" ]; then
		return 0
	fi
	if [ "${project_status}" != "failed" ]; then
		return 1
	fi
	has_label "${tracking_labels}" "ai:validation-failed" \
		|| has_label "${tracking_labels}" "ai:validate-failed" \
		|| has_label "${tracking_labels}" "ai:harness-broken"
}

maybe_apply_force_merge_bypass() {
	local final_pr="$1"
	local integration_branch="$2"
	local ahead_by="$3"
	local pr_json
	local pr_state
	local pr_draft
	local integration_sha=""
	local validation_cycle="0"
	local force_merge_message=""
	local last_bypassed_sha=""
	local existing_tracking_comment_json=""
	local existing_tracking_comment_id=""
	local existing_tracking_comment_url=""
	local existing_failure_tracking_comment_json=""
	local now_utc=""
	local force_merge_event_json=""
	local requested_actor=""
	local requested_at=""
	local requested_actor_display=""
	local requested_reason=""
	local validation_context=""
	local draft_action_text=""
	local force_merge_retry_message=""
	local failure_tracking_comment_body=""
	local tracking_comment_body=""
	local tracking_comment_json=""
	local tracking_comment_id=""
	local existing_tracking_comment_id_json='null'
	local tracking_comment_id_json='null'
	local tracking_comment_url=""
	local pr_comment_body=""
	local pr_comment_json=""
	local pr_comment_id=""
	local pr_comment_id_json='null'
	local pr_comment_url=""
	local memory_entry_file=""
	local memory_result=""

	[[ "${final_pr}" =~ ^[0-9]+$ ]] || return 1
	[ -n "${integration_branch}" ] || return 1
	has_label "${TRACKING_LABELS:-[]}" "ai:force-merge" || return 1
	[[ "${ahead_by}" =~ ^[0-9]+$ ]] || return 1
	[ "${ahead_by}" -gt 0 ] || return 1
	case "${PROJECT_STATUS:-}" in
		complete)
			return 1
			;;
		failed|validation-failed)
			project_is_validation_origin_terminal_failure "${PROJECT_STATUS:-}" "${TRACKING_LABELS:-[]}" || return 1
			;;
	esac

	pr_json="$(_fetch_pr_json "${final_pr}")"
	pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
	[ "${pr_state}" = "open" ] || return 1
	pr_draft="$(_jq_field "${pr_json}" '.draft' 'true|false')"
	[ -n "${pr_draft}" ] || pr_draft="false"

	integration_sha="$(_branch_head_sha "${integration_branch}" || echo "")"
	[[ "${integration_sha}" =~ ^[0-9A-Fa-f]{7,64}$ ]] || return 1
	integration_sha="$(printf '%s' "${integration_sha}" | tr '[:upper:]' '[:lower:]')"
	validation_cycle="$(jq -r '.validation_cycle // 0' "${STATE_FILE}" 2>/dev/null || echo 0)"
	force_merge_message="Operator bypass active — \`ai:force-merge\` advanced this integration PR before validation completed for integration SHA \`${integration_sha}\`."

	last_bypassed_sha="$(jq -r '.force_merge_last_bypassed_integration_sha // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
	if [ "${last_bypassed_sha}" = "${integration_sha}" ]; then
		update_eager_pr_validation_status_section "${final_pr}" "${force_merge_message}" || true
		return 0
	fi

	existing_tracking_comment_json="$(printf '%s' "${COMMENTS:-[]}" | jq -c --arg sha "${integration_sha}" '
		[
			.[]
			| select((.body // "") | contains("<!-- force-merge-bypass:" + $sha + " -->"))
		]
		| sort_by((.created_at // ""), (.id // 0))
		| last // empty
	' 2>/dev/null || echo "")"
	if [ -n "${existing_tracking_comment_json}" ] && [ "${existing_tracking_comment_json}" != "null" ]; then
		existing_tracking_comment_id="$(printf '%s' "${existing_tracking_comment_json}" | jq -r '.id // ""' 2>/dev/null || echo "")"
		existing_tracking_comment_url="$(printf '%s' "${existing_tracking_comment_json}" | jq -r '.html_url // ""' 2>/dev/null || echo "")"
		if [[ "${existing_tracking_comment_id}" =~ ^[0-9]+$ ]]; then
			existing_tracking_comment_id_json="${existing_tracking_comment_id}"
		fi
		if [ -z "${existing_tracking_comment_url}" ] && [[ "${existing_tracking_comment_id}" =~ ^[0-9]+$ ]]; then
			existing_tracking_comment_url="$(_gh_url "issues/${TRACKING_NUM}#issuecomment-${existing_tracking_comment_id}")"
		fi
		update_eager_pr_validation_status_section "${final_pr}" "${force_merge_message}" || true
		now_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
		jq \
			--arg sha "${integration_sha}" \
			--arg now_utc "${now_utc}" \
			--argjson tracking_comment_id "${existing_tracking_comment_id_json}" \
			--arg tracking_comment_url "${existing_tracking_comment_url}" '
			.force_merge_last_bypassed_integration_sha = $sha
			| .force_merge_last_bypassed_at_utc = $now_utc
			| .force_merge_last_bypass_tracking_comment_id = $tracking_comment_id
			| .force_merge_last_bypass_tracking_comment_url = (if $tracking_comment_url == "" then null else $tracking_comment_url end)
		' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
		post_state_comment || true
		return 0
	fi

	now_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	if force_merge_event_json="$(latest_force_merge_label_event_json 2>/dev/null || true)" \
		&& [ -n "${force_merge_event_json}" ]; then
		requested_actor="$(printf '%s' "${force_merge_event_json}" | jq -r '.actor.login // ""' 2>/dev/null || echo "")"
		requested_at="$(printf '%s' "${force_merge_event_json}" | jq -r '.created_at // ""' 2>/dev/null || echo "")"
	fi
	[ -n "${requested_actor}" ] || requested_actor="unknown"
	[ -n "${requested_at}" ] || requested_at="${now_utc}"
	if [ "${requested_actor}" = "unknown" ]; then
		requested_actor_display="unknown (latest \`ai:force-merge\` label event unavailable)"
		requested_reason="Tracking issue carries \`ai:force-merge\`, but the latest label event could not be attributed from current cycle data."
	else
		requested_actor_display="@${requested_actor}"
		requested_reason="Tracking issue labeled \`ai:force-merge\` by ${requested_actor_display} at ${requested_at}."
	fi
	validation_context="status=${PROJECT_STATUS:-unknown}; validation_cycle=${validation_cycle}; ahead_by=${ahead_by}; final_pr=${final_pr}; integration_branch=${integration_branch}"
	force_merge_retry_message="Operator requested \`ai:force-merge\` for integration SHA \`${integration_sha}\`, but promoting draft integration PR #${final_pr} failed this cycle. The poller will retry on the next run."

	if [ "${pr_draft}" = "true" ]; then
		if ! gh_retry gh pr ready "${final_pr}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
			update_eager_pr_validation_status_section "${final_pr}" "${force_merge_retry_message}" || true
			existing_failure_tracking_comment_json="$(printf '%s' "${COMMENTS:-[]}" | jq -c --arg sha "${integration_sha}" '
				[
					.[]
					| select((.body // "") | contains("<!-- force-merge-bypass-failed:" + $sha + " -->"))
				]
				| sort_by((.created_at // ""), (.id // 0))
				| last // empty
			' 2>/dev/null || echo "")"
			if [ -z "${existing_failure_tracking_comment_json}" ] || [ "${existing_failure_tracking_comment_json}" = "null" ]; then
				failure_tracking_comment_body="$(cat <<EOF
## ⚠️ Operator bypass requested but not yet applied: ai:force-merge

The poller could not promote draft integration PR #${final_pr} for this integration SHA on this cycle, so the bypass is not yet active.

- Integration branch: \`${integration_branch}\`
- Integration SHA: \`${integration_sha}\`
- Ahead of default: ${ahead_by} commit(s)
- Requested by: ${requested_actor_display}
- Request observed at (UTC): ${requested_at}
- Promotion attempt failed at (UTC): ${now_utc}
- Validation context: \`${validation_context}\`

The poller will retry the promotion on a later cycle.

<!-- force-merge-bypass-failed:${integration_sha} -->
EOF
)"
				post_issue_comment_json "${TRACKING_NUM}" "${failure_tracking_comment_body}" >/dev/null || true
			fi
			echo "::warning::[force-merge] Unable to promote draft integration PR #${final_pr}; will retry next poll." >&2
			return 1
		fi
		draft_action_text="promoted integration PR #${final_pr} from draft to ready"
	else
		draft_action_text="recorded the operator bypass for already-ready integration PR #${final_pr}"
	fi
	update_eager_pr_validation_status_section "${final_pr}" "${force_merge_message}" || true

	tracking_comment_body="$(cat <<EOF
## ⚠️ Operator bypass applied: ai:force-merge

The orchestrator ${draft_action_text} before validation completed for this integration SHA.

- Integration branch: \`${integration_branch}\`
- Integration SHA: \`${integration_sha}\`
- Ahead of default: ${ahead_by} commit(s)
- Requested by: ${requested_actor_display}
- Request observed at (UTC): ${requested_at}
- Bypass applied at (UTC): ${now_utc}
- Validation context: \`${validation_context}\`

This bypass is recorded once per integration SHA and keeps the existing validation audit trail intact.

<!-- force-merge-bypass:${integration_sha} -->
EOF
)"
	if ! tracking_comment_json="$(post_issue_comment_json "${TRACKING_NUM}" "${tracking_comment_body}")"; then
		return 1
	fi
	tracking_comment_id="$(printf '%s' "${tracking_comment_json}" | jq -r '.id // ""' 2>/dev/null || echo "")"
	tracking_comment_url="$(printf '%s' "${tracking_comment_json}" | jq -r '.html_url // ""' 2>/dev/null || echo "")"
	if [[ "${tracking_comment_id}" =~ ^[0-9]+$ ]]; then
		tracking_comment_id_json="${tracking_comment_id}"
	fi

	pr_comment_body="$(cat <<EOF
## ⚠️ Operator bypass recorded

Tracking issue #${TRACKING_NUM} carries \`ai:force-merge\`, so this PR is allowed to proceed before validation completed for integration SHA \`${integration_sha}\`.

- Requested by: ${requested_actor_display}
- Tracking issue audit: ${tracking_comment_url:-$(_gh_url "issues/${TRACKING_NUM}")}
- Validation context: \`${validation_context}\`

<!-- force-merge-bypass:${integration_sha} -->
EOF
)"
	if pr_comment_json="$(post_issue_comment_json "${final_pr}" "${pr_comment_body}")"; then
		pr_comment_id="$(printf '%s' "${pr_comment_json}" | jq -r '.id // ""' 2>/dev/null || echo "")"
		pr_comment_url="$(printf '%s' "${pr_comment_json}" | jq -r '.html_url // ""' 2>/dev/null || echo "")"
		if [[ "${pr_comment_id}" =~ ^[0-9]+$ ]]; then
			pr_comment_id_json="${pr_comment_id}"
		fi
	else
		echo "::warning::[force-merge] Failed to post PR audit comment for #${final_pr}; tracking issue audit is still authoritative." >&2
	fi

	memory_entry_file="$(mktemp "${TMPDIR:-/tmp}/operator_bypass_audit.XXXXXX")"
	jq -n \
		--arg actor "${requested_actor}" \
		--arg timestamp_utc "${now_utc}" \
		--arg bypass_kind "force-merge" \
		--arg reason "${requested_reason}" \
		--arg validation_context "${validation_context}" \
		--argjson source_comment_id "${tracking_comment_id_json}" \
		--arg source_comment_url "${tracking_comment_url}" '
			{
				actor: $actor,
				timestamp_utc: $timestamp_utc,
				bypass_kind: $bypass_kind,
				reason: (if $reason == "" then null else $reason end),
				validation_context: (if $validation_context == "" then null else $validation_context end),
				source_comment_id: $source_comment_id,
				source_comment_url: (if $source_comment_url == "" then null else $source_comment_url end)
			}
		' > "${memory_entry_file}"
	memory_result="$(memory_operator_bypass_audit_append \
		--repo-root . \
		--memory-branch "${AI_MEMORY_BRANCH:-ai-memory}" \
		--memory-root "${AI_MEMORY_ROOT:-ai-memory}" \
		--repo "${GITHUB_REPOSITORY}" \
		--tracking-issue "${TRACKING_NUM}" \
		--integration-sha "${integration_sha}" \
		--entry-file "${memory_entry_file}" 2>/dev/null || echo '{"ok": true, "enabled": true, "stored": false, "audit": null}')"
	rm -f "${memory_entry_file}"
	if [ "$(printf '%s' "${memory_result}" | jq -r '.stored // false' 2>/dev/null || echo false)" != "true" ]; then
		echo "::warning::[force-merge] operator bypass audit append did not confirm storage for integration SHA ${integration_sha}; tracking issue audit comment remains the canonical trail." >&2
	fi

	jq \
		--arg sha "${integration_sha}" \
		--arg now_utc "${now_utc}" \
		--arg actor "${requested_actor}" \
		--argjson tracking_comment_id "${tracking_comment_id_json}" \
		--arg tracking_comment_url "${tracking_comment_url}" \
		--argjson pr_comment_id "${pr_comment_id_json}" \
		--arg pr_comment_url "${pr_comment_url}" '
		.force_merge_last_bypassed_integration_sha = $sha
		| .force_merge_last_bypassed_at_utc = $now_utc
		| .force_merge_last_bypass_actor = $actor
		| .force_merge_last_bypass_tracking_comment_id = $tracking_comment_id
		| .force_merge_last_bypass_tracking_comment_url = (if $tracking_comment_url == "" then null else $tracking_comment_url end)
		| .force_merge_last_bypass_pr_comment_id = $pr_comment_id
		| .force_merge_last_bypass_pr_comment_url = (if $pr_comment_url == "" then null else $pr_comment_url end)
	' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
	post_state_comment || true
	echo "FORCE_MERGE_BYPASS tracking_issue=${TRACKING_NUM} pr=${final_pr} integration_branch=${integration_branch} integration_sha=${integration_sha} actor=${requested_actor} ahead_by=${ahead_by}"
	return 0
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
  # Centralised in scripts/write_codex_config.sh — see that script's
  # header for the apply_patch / trust / elevation rationale.
  bash scripts/write_codex_config.sh \
    --model "${MODEL_EDITOR:-openai/gpt-5.6-sol}" \
    --reasoning "${MODEL_REASONING_EFFORT_JUDGE:-xhigh}"

  local prompt_file
  local output_file
  local judge_static_file
  prompt_file="$(mktemp "${TMPDIR:-/tmp}/integration_judge_prompt.XXXXXX")"
  output_file="$(mktemp "${TMPDIR:-/tmp}/integration_judge_output.XXXXXX")"
  judge_static_file="$(mktemp "${TMPDIR:-/tmp}/integration_judge_static.XXXXXX")"

  if ! assemble_judge_static_context "${judge_static_file}"; then
    rm -f "${prompt_file}" "${output_file}" "${judge_static_file}"
    return 1
  fi

  local pr_diff
  local pr_files
  # Fetch into a temp file before truncating: piping gh pr diff directly
  # into `head -c` causes SIGPIPE on gh pr diff once head has read enough
  # bytes, which gh_retry then treats as a transient failure and retries
  # with exponential backoff. Capture first, truncate second.
  local _pr_diff_tmp
  _pr_diff_tmp="$(mktemp)"
  if gh_retry_to_file "${_pr_diff_tmp}" gh pr diff "${final_pr}" --repo "${GITHUB_REPOSITORY}"; then
    pr_diff="$(head -c 120000 "${_pr_diff_tmp}" 2>/dev/null || true)"
  else
    echo "::warning::Failed to fetch PR #${final_pr} diff for integration-conflict judge; continuing with empty pr_diff." >&2
    pr_diff=""
  fi
  rm -f "${_pr_diff_tmp}"
  pr_files="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}/files" --jq '[.[] | {filename, status, additions, deletions}]' || echo "[]")"
  [ -n "${pr_files}" ] || pr_files='[]'
  local retries
  retries="$(jq -r '.integration_conflict_dispatch_count // 0' "${STATE_FILE}")"

  # Pull the structured per-sub-issue intent fingerprints from state so
  # the judge gets the same hard verification contract the resolver
  # already operates against (must_contain / must_not_contain regex
  # patterns per file per merged sub-issue).  Without this the judge
  # had to re-derive intent from the truncated PR diff alone, which is
  # the gap that let main-side reverts ship past the judge in PR #1533
  # 2026-04-25.  Compact JSON so the prompt stays well under the
  # 120KB budget alongside the truncated diff.
  local intent_fingerprints
  intent_fingerprints="$(jq -c '.merged_issue_fingerprints // {}' "${STATE_FILE}" 2>/dev/null || echo "{}")"
  [ -n "${intent_fingerprints}" ] || intent_fingerprints='{}'
  local judge_semble_query_file
  local judge_semble_prefetch=""
  judge_semble_query_file="$(mktemp "${TMPDIR:-/tmp}/integration_judge_semble_query.XXXXXX")"
  {
    printf '%s\n' 'Integration conflict judge context.'
    append_judge_semble_query_text "Tracking + branch summary:" "tracking issue #${TRACKING_NUM}; final PR #${final_pr}; integration branch ${integration_branch}; default branch ${default_branch}; retries ${retries}" 800
    append_judge_semble_query_text "Changed files JSON:" "${pr_files}" 2500
    append_judge_semble_query_text "PR diff excerpt:" "${pr_diff}" 5000
    append_judge_semble_query_text "Intent fingerprints JSON:" "${intent_fingerprints}" 3500
  } > "${judge_semble_query_file}"
  judge_semble_prefetch="$(render_judge_semble_prefetch_from_query_file "${judge_semble_query_file}" "Integration Conflict Judge Context")"

  {
    cat "${judge_static_file}"
    echo
    echo "=== INTEGRATION CONFLICT JUDGE TASK ==="
    echo
    if [ -n "${judge_semble_prefetch}" ]; then
      printf '%s\n' "${judge_semble_prefetch}"
      echo
    fi
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
    echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
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
    echo "Merged sub-issue intent fingerprints (verification contract):"
    echo "Each entry is keyed by GitHub issue number; \`must_contain\`"
    echo "patterns are regexes that MUST match in the post-resolve tree,"
    echo "\`must_not_contain\` patterns are regexes that MUST NOT match."
    echo "After you push, \`scripts/verify_integration_fingerprints.py\`"
    echo "is run against this exact JSON — every violation is a hard"
    echo "rejection that returns the project to this judge cycle.  Use"
    echo "this as the authoritative spec when reconciling conflicts;"
    echo "the truncated PR diff above is context, the fingerprints are"
    echo "the test."
    echo '```json'
    printf '%s\n' "${intent_fingerprints}"
    echo '```'
    echo
    echo "Rules:"
    echo "1. Preserve all intent from merged sub-issues — every"
    echo "   \`must_contain\` regex must still match the post-resolve"
    echo "   working tree, every \`must_not_contain\` regex must not."
    echo "2. Do not rewrite history of ${default_branch}."
    echo "3. Prefer merge commits over rebase for the integration branch."
    echo "4. When a hunk has both ${default_branch} content and merged"
    echo "   sub-issue content, synthesize rather than pick a side —"
    echo "   wholesale reverts to \`${default_branch}\`'s version are"
    echo "   the dominant failure mode the fingerprint contract is"
    echo "   designed to catch."
    echo "5. If conflicts are semantic rather than textual, surface a"
    echo "   short diagnosis in the commit message."
  } > "${prompt_file}"

  sanitize_codex_prompt_file "${prompt_file}"
  if cat "${prompt_file}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR:-openai/gpt-5.6-sol}" --sandbox danger-full-access > "${output_file}" 2>> "${RUNTIME_DIR}/integration_judge.log"; then
    echo "  [integration-heal] Judge exec completed for PR #${final_pr}."
    rm -f "${prompt_file}" "${output_file}" "${judge_static_file}" "${judge_semble_query_file}"
    return 0
  fi

  echo "::warning::Judge exec failed for integration conflict on PR #${final_pr}."
  rm -f "${prompt_file}" "${output_file}" "${judge_static_file}" "${judge_semble_query_file}"
  return 1
}

# _refresh_integration_resolver_tooling — copy the resolver toolchain
# (scripts + prompts the merge-conflict resolver consumes) from
# default_branch onto the integration branch as an [ai-maint] commit
# when their content hashes differ.
#
# Why: review_autofix.yml dispatches with --ref <integration_branch>,
# so the workflow YAML AND its support scripts come from the
# integration branch's tip — the same tip that, by definition, has not
# yet absorbed default_branch.  When a fix lands on default_branch
# that the resolver itself depends on (e.g. PR #1581 added the
# fingerprint-violation expansion to scripts/review_conflict_prepare.sh
# so auto-merge regressions outside the unmerged set become editable),
# the integration branch can deadlock: the only path to pick up the
# fix is `main -> orchestrator/project-N` sync, but that sync needs
# the fix to succeed.  This helper breaks the deadlock by force-
# refreshing only the resolver's own toolchain — the smallest set of
# files that, if stale, would silently sabotage the next dispatch.
#
# Scope: orchestrator/project-* branches only; no-op otherwise.  The
# refreshed file set is intentionally narrow (no .github/workflows/*,
# no app code) so the maintenance commit cannot drag unrelated
# default_branch changes onto the integration branch and create a
# new wave of conflicts.
#
# Side effects: pushes a commit on success.  The push fires
# pull_request.synchronize on the integration PR which itself
# triggers review_autofix.yml; the explicit dispatch in
# heal_integration_branch_conflict still fires immediately after, and
# the cancel-in-progress concurrency on review_autofix.yml resolves
# the race.  Idempotent: when nothing has drifted (no file hashes
# differ) the function is a no-op, no commit is created, and no push
# is attempted.
#
# API hygiene (per CLAUDE.md §15): zero gh API calls.  Uses local git
# (fetch + worktree + push) only.  All git failures fail-open with a
# ::warning:: so the heal flow proceeds with whatever tooling the
# integration branch already has.
#
# Usage: _refresh_integration_resolver_tooling <integration_branch> <default_branch>
# Returns: always 0 (fail-open).
_refresh_integration_resolver_tooling() {
  local integration_branch="$1"
  local default_branch="$2"

  case "${integration_branch}" in
    orchestrator/project-*) ;;
    *) return 0 ;;
  esac
  [ -n "${default_branch}" ] || return 0

  local log_prefix="[resolver-tooling-refresh] ${integration_branch}"

  # Files kept in sync with default_branch.  Limited to the
  # resolver-side of conflict resolution: the prepare/resolve scripts,
  # the post-resolver fingerprint verifier, the touched-subset guard,
  # and the prompt templates the resolver renders.  Adding entries
  # here is a contract change — see probably_unnecessary_but_read_if_stuck.md §18.
  local refresh_files=(
    "scripts/review_conflict_prepare.sh"
    "scripts/review_conflict_resolve.sh"
    "scripts/verify_integration_fingerprints.py"
    "scripts/check_resolver_diff.sh"
    "scripts/targeted_file_context.py"
    "prompts/conflict-resolver.txt"
    "prompts/integration-sync-conflict-resolver.txt"
    "prompts/integration-sync-conflict-resolver-retry-prelude.txt"
    "prompts/integration-sync-conflict-resolver-retry-timeout-prelude.txt"
  )

  local runtime_dir="${RUNTIME_DIR:-/tmp}"
  local wt="${runtime_dir}/resolver-tooling-refresh-wt-$$-${RANDOM:-0}"

  local attempt
  for attempt in 1 2 3; do
    rm -rf "${wt}" 2>/dev/null || true

    if ! git fetch --quiet --no-tags --prune origin \
        "+refs/heads/${default_branch}:refs/remotes/origin/${default_branch}" \
        "+refs/heads/${integration_branch}:refs/remotes/origin/${integration_branch}" \
        2>/dev/null; then
      echo "::warning::${log_prefix} git fetch failed (attempt ${attempt}/3); proceeding to dispatch with current tooling."
      [ "${attempt}" -lt 3 ] && sleep 1 && continue
      return 0
    fi

	if ! git worktree add --quiet --detach "${wt}" \
	     "refs/remotes/origin/${integration_branch}" 2>/dev/null; then
	  echo "::warning::${log_prefix} git worktree add failed (attempt ${attempt}/3); proceeding to dispatch with current tooling."
	  [ "${attempt}" -lt 3 ] && sleep 1 && continue
	  return 0
	fi
	worktree_registry_register "$(basename -- "${wt}")" "${wt}" "refs/remotes/origin/${integration_branch}" "tracking-${TRACKING_NUM:-0}" "orchestrate-poll"

    local subshell_rc=0
    (
      cd "${wt}" || exit 9
      git config user.name "codex-bot"
      git config user.email "codex@users.noreply.github.com"

      # Resolve the merge-base ONCE before the per-file loop so we can
      # compare each file's integration-branch hash against the hash it
      # had when the two branches last shared history.  When int_hash
      # differs from the merge-base hash, the integration branch has
      # its own committed changes to the file since the branches
      # diverged (e.g. via a merged sub-issue's PR like #2738 landing
      # Phase 1A baseline/delta verifier changes).  Force-refreshing
      # from default_branch in that case silently reverts those
      # merged sub-issue changes, which the post-resolve merged
      # sub-issue fingerprint verifier
      # (scripts/verify_integration_fingerprints.py) then flags as a
      # contract violation, blocking wave dispatch on the project
      # tracking issue (see issue #2734 for the original incident).
      # Fail-open: if the merge-base cannot be resolved, fall through
      # to the legacy hash-only comparison rather than aborting the
      # deadlock-breaking refresh entirely.
      local merge_base=""
      merge_base="$(git merge-base \
        "refs/remotes/origin/${integration_branch}" \
        "refs/remotes/origin/${default_branch}" 2>/dev/null || echo "")"

      if [ -z "${merge_base}" ]; then
        echo "::warning::${log_prefix} merge-base unresolved for ${integration_branch} vs ${default_branch}; falling back to legacy hash-only comparison." >&2
      fi

      local refreshed_count=0
      local refreshed_list=""
      local drifted_count=0
      local skipped_count=0
      # Subset of refreshed_count: files that were staged via the 3-way
      # merge fallback rather than the deadlock-breaker checkout. Tracked
      # separately so the post-refresh summary + commit-message body can
      # name them — operators reviewing the refresh commit should see at
      # a glance which files came from a clean overwrite vs a merge.
      local merged_3way_count=0
      local f main_hash int_hash base_hash
      for f in "${refresh_files[@]}"; do
        # Skip if file does not exist on default_branch — never delete
        # an integration-branch file just because main lacks it.
        if ! git cat-file -e "refs/remotes/origin/${default_branch}:${f}" 2>/dev/null; then
          continue
        fi
        # Keep --verify/--quiet on these tree-path lookups: without
        # them, a missing path writes the unresolved REV:PATH token to
        # stdout, which makes absent files look like real hashes.
        main_hash="$(git rev-parse --verify --quiet "refs/remotes/origin/${default_branch}:${f}" 2>/dev/null || echo "")"
        int_hash="$(git rev-parse --verify --quiet "HEAD:${f}" 2>/dev/null || echo "")"
        [ -n "${main_hash}" ] || continue
        if [ "${main_hash}" = "${int_hash}" ]; then
          continue
        fi
        drifted_count=$((drifted_count + 1))
        # Refuse to clobber a file the integration branch has its own
        # committed changes to since the merge-base.  The deadlock-
        # breaker is for files the integration branch has NOT touched
        # but main has fixed — overwriting a file the integration
        # branch HAS touched would silently revert merged sub-issue
        # PR intent and trip the fingerprint verifier (issue #2734).
        # `[ -z "${base_hash}" ]` covers the "file did not exist at
        # merge-base, integration added it" case the same way — never
        # clobber an added file.
        #
        # P5 from docs/postmortems/2026-05-18-project-2734-stall.md:
        # When BOTH main and integration have changed since the
        # merge-base, try `git merge-file` (3-way merge) before
        # skipping. This is the layered defense-in-depth that PR
        # #2760's divergence guard left as future work: when both
        # branches edited the same allowlisted toolchain file in
        # non-overlapping ways, the merge cleanly combines them, the
        # toolchain ships its update to integration without losing the
        # sub-issue PR intent, AND the deadlock-breaker stays alive.
        # Only when the 3-way merge produces conflicts do we fall back
        # to the conservative skip (let the normal main->integration
        # sync handle the conflict resolution under operator review).
        if [ -n "${merge_base}" ]; then
          base_hash="$(git rev-parse --verify --quiet "${merge_base}:${f}" 2>/dev/null || echo "")"
          if [ "${int_hash}" != "${base_hash}" ]; then
            # Sub-case (a): main is unchanged from the merge-base
            # (only integration moved). Nothing to refresh from main.
            # base_hash empty means "file added on integration" — still
            # sub-case (a) because main has nothing to contribute.
            if [ -z "${base_hash}" ] || [ "${main_hash}" = "${base_hash}" ]; then
              skipped_count=$((skipped_count + 1))
              local _int_short="${int_hash:0:8}"
              local _base_short="${base_hash:0:8}"
              [ -n "${_base_short}" ] || _base_short="none"
              [ -n "${_int_short}" ] || _int_short="none"
              echo "  ${log_prefix} skip ${f} — integration branch has committed changes since merge-base (int=${_int_short}, base=${_base_short}); main version must arrive via normal sync, not refresh."
              continue
            fi
            # Sub-case (b): BOTH main and integration changed since
            # the merge-base. Try a 3-way merge; on conflict, fall
            # back to skip.
            local merge_tmpdir
            merge_tmpdir="$(mktemp -d 2>/dev/null || echo "")"
            if [ -z "${merge_tmpdir}" ] || [ ! -d "${merge_tmpdir}" ]; then
              echo "::warning::${log_prefix} could not create tmpdir for 3-way merge of ${f}; skipping." >&2
              skipped_count=$((skipped_count + 1))
              continue
            fi
            if ! git cat-file -p "${base_hash}" > "${merge_tmpdir}/base" 2>/dev/null \
              || ! git cat-file -p "${int_hash}" > "${merge_tmpdir}/int" 2>/dev/null \
              || ! git cat-file -p "${main_hash}" > "${merge_tmpdir}/main" 2>/dev/null; then
              echo "::warning::${log_prefix} could not materialize one or more 3-way merge inputs for ${f}; skipping." >&2
              skipped_count=$((skipped_count + 1))
              rm -rf "${merge_tmpdir}" 2>/dev/null || true
              continue
            fi
            if git merge-file --quiet -L integration -L merge-base -L main \
                "${merge_tmpdir}/int" "${merge_tmpdir}/base" "${merge_tmpdir}/main" 2>/dev/null; then
              # Clean merge (exit 0): integration + main edits combined
              # without conflict. Stage the merged content as the new
              # integration-branch version.
              if cp "${merge_tmpdir}/int" "${f}" 2>/dev/null; then
                if git add -- "${f}" 2>/dev/null; then
                  refreshed_count=$((refreshed_count + 1))
                  refreshed_list+="${f} "
                  merged_3way_count=$((merged_3way_count + 1))
                  local _m_int_short="${int_hash:0:8}"
                  local _m_main_short="${main_hash:0:8}"
                  echo "  ${log_prefix} 3-way merged ${f} — combined integration (${_m_int_short}) and main (${_m_main_short}) edits since merge-base."
                else
                  git checkout -- "${f}" 2>/dev/null || true
                  echo "::warning::${log_prefix} git add failed after 3-way merge of ${f}; reverted worktree copy and excluded it from the refresh commit." >&2
                fi
              else
                git checkout -- "${f}" 2>/dev/null || true
                echo "::warning::${log_prefix} could not copy 3-way merge result for ${f}; reverted worktree copy and excluded it from the refresh commit." >&2
              fi
            else
              # Non-zero exit: conflicts (1+) or merge-file error
              # (e.g. binary file at 255). Fall back to skip — the
              # normal main->integration sync will surface the
              # conflict under operator review.
              skipped_count=$((skipped_count + 1))
              local _c_int_short="${int_hash:0:8}"
              local _c_base_short="${base_hash:0:8}"
              echo "  ${log_prefix} skip ${f} — 3-way merge produced conflicts (int=${_c_int_short}, base=${_c_base_short}); main version must arrive via normal sync."
            fi
            rm -rf "${merge_tmpdir}" 2>/dev/null || true
            continue
          fi
        fi
        if git checkout "refs/remotes/origin/${default_branch}" -- "${f}" 2>/dev/null; then
          # Only count the file as refreshed if `git add` succeeds, so
          # the commit message and refreshed_count never claim a file
          # was updated when its staging actually failed (e.g. a path
          # permission issue or .gitignore conflict).
          if git add -- "${f}" 2>/dev/null; then
            refreshed_count=$((refreshed_count + 1))
            refreshed_list+="${f} "
          else
            echo "::warning::${log_prefix} git add failed for ${f}; excluding from refresh commit." >&2
          fi
        fi
      done

      if [ "${refreshed_count}" -eq 0 ]; then
        if [ "${skipped_count}" -gt 0 ] && [ "${skipped_count}" -eq "${drifted_count}" ]; then
          echo "  ${log_prefix} detected resolver-toolchain drift in ${drifted_count} file(s), but skipped refresh because integration-branch changes must land via normal sync."
        elif [ "${drifted_count}" -gt 0 ]; then
          echo "  ${log_prefix} detected resolver-toolchain drift in ${drifted_count} file(s), but nothing was refreshed."
        else
          echo "  ${log_prefix} no resolver-toolchain drift; nothing to refresh."
        fi
        exit 0
      fi

      if [ "${merged_3way_count}" -gt 0 ]; then
        # Surface the 3-way merges in the per-tick log so they're easy to
        # find when an operator audits the refresh commit. The commit-
        # message body further down also records the count.
        echo "  ${log_prefix} ${merged_3way_count} file(s) refreshed via 3-way merge (combined integration + main edits)."
      fi

      if git diff --cached --quiet; then
        # Edge case: checkout reported success but git sees no staged
        # change (e.g. mode-only update on a filesystem without exec
        # bit).  Skip rather than create an empty commit.
        echo "  ${log_prefix} ${refreshed_count} file(s) marked but no staged diff; skipping commit."
        exit 0
      fi

      local body=""
      body+="Files refreshed (${refreshed_count}):"$'\n'
      local _f
      for _f in ${refreshed_list}; do
        body+=" - ${_f}"$'\n'
      done
      if [ "${merged_3way_count}" -gt 0 ]; then
        body+=$'\n'
        body+="${merged_3way_count} file(s) refreshed via 3-way merge (combined integration"$'\n'
        body+="and ${default_branch} edits since merge-base). The remaining $((refreshed_count - merged_3way_count))"$'\n'
        body+="file(s) were refreshed by direct checkout of ${default_branch}'s version"$'\n'
        body+="because the integration branch had not modified them since the"$'\n'
        body+="merge-base."$'\n'
      fi
      body+=$'\n'
      body+="Brings the integration branch's resolver toolchain up to date with"$'\n'
      body+="${default_branch} so any bug fixes shipped there take effect on the next"$'\n'
      body+="review_autofix dispatch instead of being blocked behind the deadlock"$'\n'
      body+="where the integration branch's stale resolver scripts cannot resolve"$'\n'
      body+="the conflicts that block ${default_branch} from being merged in."$'\n'
      body+=$'\n'
      body+="Triggered by _refresh_integration_resolver_tooling in"$'\n'
      body+="scripts/orchestrate_poll_process.sh."
      if ! git commit --quiet \
            -m "[ai-maint] refresh resolver tooling from ${default_branch}" \
            -m "${body}" 2>/dev/null; then
        exit 1
      fi

      if ! git push --quiet origin "HEAD:refs/heads/${integration_branch}" 2>/dev/null; then
        exit 2
      fi
      echo "  ${log_prefix} pushed [ai-maint] refresh: ${refreshed_count} file(s) updated from ${default_branch}."
      exit 0
	# Keep cleanup reachable under the script's global `set -e`.
	# This subshell returns intentional rc values used by the retry logic.
	) || subshell_rc=$?
	worktree_registry_deregister "$(basename -- "${wt}")"
	git worktree remove --force "${wt}" 2>/dev/null || rm -rf "${wt}" 2>/dev/null || true

    case "${subshell_rc}" in
      0)
        return 0
        ;;
      2)
        # Push race (non-fast-forward).  Refetch and retry; matches
        # the pattern in _record_merge_conflict_telemetry.
        if [ "${attempt}" -lt 3 ]; then
          sleep 1
          continue
        fi
        echo "::warning::${log_prefix} push race exhausted; proceeding to dispatch with current tip."
        return 0
        ;;
      *)
        echo "::warning::${log_prefix} refresh subshell exit ${subshell_rc}; proceeding to dispatch with current tooling."
        return 0
        ;;
    esac
  done

  return 0
}

# Drive one iteration of the self-healing loop for the integration
# branch. Must be called when we know a conflict exists (either from
# a 409 in sync_default_into_integration_branch or from a
# mergeable=false in finalize_integration_merge_if_needed).
#
# Returns 0 if healing progressed (dispatch queued, cooldown active,
# or judge invoked), 1 if the circuit breaker has tripped and the
# state was marked failed.
# _list_integration_conflict_files — enumerate the filenames that would
# conflict if <integration_branch> were merged into <default_branch>.
# Uses ``git merge-tree --write-tree --name-only`` for a stateless
# three-way merge probe (same technique as probe_sibling_merge_conflicts
# at line 304+).  Echoes one file path per line on stdout; returns 0
# when conflicts were detected, 1 otherwise (including unavailable git
# version, missing refs, or a clean merge).  Used by
# heal_integration_branch_conflict (Q2c) to short-circuit the first-line
# resolver on hot-file collisions.
_list_integration_conflict_files() {
  local integration_branch="$1"
  local default_branch="$2"

  [ -n "${integration_branch}" ] && [ -n "${default_branch}" ] || return 1
  command -v git >/dev/null 2>&1 || return 1
  if ! git merge-tree --write-tree --name-only --no-messages HEAD HEAD >/dev/null 2>&1; then
    return 1
  fi

  local ib_ref="refs/remotes/origin/${integration_branch}"
  local db_ref="refs/remotes/origin/${default_branch}"
  git fetch --no-tags --quiet origin \
    "+refs/heads/${integration_branch}:refs/remotes/origin/${integration_branch}" \
    "+refs/heads/${default_branch}:refs/remotes/origin/${default_branch}" 2>/dev/null || true
  git rev-parse --verify --quiet "${ib_ref}" >/dev/null 2>&1 || return 1
  git rev-parse --verify --quiet "${db_ref}" >/dev/null 2>&1 || return 1

  local out
  if out="$(git merge-tree --write-tree --name-only --no-messages "${db_ref}" "${ib_ref}" 2>/dev/null)"; then
    # Exit 0 from merge-tree means the merge is clean.
    return 1
  fi
  # On conflict, modern git prints the written tree SHA on the first
  # line followed by conflict paths. Strip the SHA line when present.
  printf '%s\n' "${out}" | sed '/^$/d' | awk 'NR==1 && /^[[:xdigit:]]{40}([[:xdigit:]]{24})?$/ {next} {print}'
  return 0
}

_iso8601_to_epoch() {
  local ts="$1"
  [ -n "${ts}" ] || return 1
  local epoch
  epoch="$(jq -nr --arg ts "${ts}" 'try ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch empty' 2>/dev/null || echo "")"
  [[ "${epoch}" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "${epoch}"
}

_branch_rebuild_audit_get() {
  local integration_branch="$1"
  if ! type _memory_enabled >/dev/null 2>&1 || ! _memory_enabled || [ ! -f "scripts/ai_memory.py" ]; then
    return 1
  fi

  local audit_json
  audit_json="$(python3 scripts/ai_memory.py branch-rebuild-audit get \
    --repo "${GITHUB_REPOSITORY}" \
    --tracking-issue "${TRACKING_NUM}" \
    --integration-branch "${integration_branch}" 2>/dev/null || echo "")"
  if [ -z "${audit_json}" ] || ! printf '%s' "${audit_json}" | jq -e '.ok == true and .enabled == true and .hit != null and ((.warning? // "") == "")' >/dev/null 2>&1; then
    return 1
  fi
  printf '%s' "${audit_json}"
}

_branch_rebuild_audit_put() {
  local integration_branch="$1"
  local audit_json="$2"
  if ! type _memory_enabled >/dev/null 2>&1 || ! _memory_enabled || [ ! -f "scripts/ai_memory.py" ]; then
    return 1
  fi

  local audit_file=""
  local result_json=""
  audit_file="$(mktemp "${TMPDIR:-/tmp}/branch-rebuild-audit.XXXXXX" 2>/dev/null || true)"
  [ -n "${audit_file}" ] || return 1
  printf '%s\n' "${audit_json}" > "${audit_file}"
  result_json="$(python3 scripts/ai_memory.py branch-rebuild-audit put \
    --repo "${GITHUB_REPOSITORY}" \
    --tracking-issue "${TRACKING_NUM}" \
    --integration-branch "${integration_branch}" \
    --audit-file "${audit_file}" 2>/dev/null || echo "")"
  rm -f "${audit_file}"
  printf '%s' "${result_json}" | jq -e '.ok == true and .enabled == true and .stored == true' >/dev/null 2>&1
}

validation_history_current_integration_sha() {
	local integration_branch=""
	integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
	[ "${integration_branch}" = "null" ] && integration_branch=""
	[ -n "${integration_branch}" ] || return 1
	_branch_head_sha "${integration_branch}" 2>/dev/null
}

validation_history_run_info_json() {
	local run_info="${LAST_VAL_RUN_INFO:-}"
	if [ -z "${run_info}" ] || ! printf '%s' "${run_info}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		run_info="$(get_last_validation_run_info 2>/dev/null || echo '{}')"
	fi
	if ! printf '%s' "${run_info}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		run_info='{}'
	fi
	printf '%s' "${run_info}"
}

append_validation_history_for_current_sha() {
	local outcome="$1"
	local raw_status="$2"
	local raw_conclusion="$3"
	local cycle="$4"
	local context="$5"
	local source="$6"
	local integration_sha=""
	local run_info='{}'
	local run_id=""
	local run_attempt=""
	local run_url=""
	local run_timestamp=""
	local resolved_conclusion="${raw_conclusion}"
	local recorded_at=""
	local run_id_json='null'
	local run_attempt_json='null'
	local cycle_json='null'
	local entry_file=""
	local append_json=""

	VALIDATION_HISTORY_LAST_APPEND_STORED=""
	VALIDATION_HISTORY_LAST_APPEND_SHA=""
	VALIDATION_HISTORY_LAST_APPEND_WARNING=""

	integration_sha="$(validation_history_current_integration_sha 2>/dev/null || true)"
	if [ -z "${integration_sha}" ]; then
		return 0
	fi
	VALIDATION_HISTORY_LAST_APPEND_SHA="$(printf '%s' "${integration_sha}" | tr '[:upper:]' '[:lower:]')"

	run_info="$(validation_history_run_info_json)"
	run_id="$(printf '%s' "${run_info}" | jq -r '.run_id // ""' 2>/dev/null || echo '')"
	run_attempt="$(printf '%s' "${run_info}" | jq -r '.run_attempt // ""' 2>/dev/null || echo '')"
	run_url="$(printf '%s' "${run_info}" | jq -r '.run_url // ""' 2>/dev/null || echo '')"
	run_timestamp="$(printf '%s' "${run_info}" | jq -r '.run_timestamp // ""' 2>/dev/null || echo '')"
	if [ -z "${resolved_conclusion}" ]; then
		resolved_conclusion="$(printf '%s' "${run_info}" | jq -r '.conclusion // ""' 2>/dev/null || echo '')"
	fi
	recorded_at="${run_timestamp}"
	if [ -z "${recorded_at}" ] || [ "${recorded_at}" = "null" ]; then
		recorded_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	fi
	if [[ "${run_id}" =~ ^[0-9]+$ ]] && [ "${run_id}" -ge 1 ]; then
		run_id_json="${run_id}"
	fi
	if [[ "${run_attempt}" =~ ^[0-9]+$ ]] && [ "${run_attempt}" -ge 1 ]; then
		run_attempt_json="${run_attempt}"
	fi
	if [[ "${cycle}" =~ ^[0-9]+$ ]] && [ "${cycle}" -ge 1 ]; then
		cycle_json="${cycle}"
	fi

	entry_file="$(mktemp "${TMPDIR:-/tmp}/validation_history_entry.XXXXXX")"
	jq -n \
		--arg outcome "${outcome}" \
		--arg raw_status "${raw_status}" \
		--arg raw_conclusion "${resolved_conclusion}" \
		--arg run_url "${run_url}" \
		--arg recorded_at "${recorded_at}" \
		--arg context "${context}" \
		--arg source "${source}" \
		--argjson run_id "${run_id_json}" \
		--argjson run_attempt "${run_attempt_json}" \
		--argjson cycle "${cycle_json}" '
			{
				outcome: $outcome,
				raw_status: (if $raw_status == "" then null else $raw_status end),
				raw_conclusion: (if $raw_conclusion == "" then null else $raw_conclusion end),
				run_id: $run_id,
				run_attempt: $run_attempt,
				run_url: (if $run_url == "" then null else $run_url end),
				recorded_at: $recorded_at,
				cycle: $cycle,
				context: (if $context == "" then null else $context end),
				source: (if $source == "" then null else $source end)
			}
		' > "${entry_file}"
	append_json="$(memory_validation_history_append \
		--repo-root . \
		--memory-branch "${AI_MEMORY_BRANCH:-ai-memory}" \
		--memory-root "${AI_MEMORY_ROOT:-ai-memory}" \
		--repo "${GITHUB_REPOSITORY}" \
		--integration-sha "${integration_sha}" \
		--entry-file "${entry_file}" 2>/dev/null || echo '')"
	rm -f "${entry_file}"
	if [ -z "${append_json}" ] || ! printf '%s' "${append_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		VALIDATION_HISTORY_LAST_APPEND_STORED="false"
		VALIDATION_HISTORY_LAST_APPEND_WARNING="invalid_json"
		return 0
	fi
	if [ "$(printf '%s' "${append_json}" | jq -r '.stored // false' 2>/dev/null || echo false)" = "true" ]; then
		VALIDATION_HISTORY_LAST_APPEND_STORED="true"
		return 0
	fi
	VALIDATION_HISTORY_LAST_APPEND_STORED="false"
	VALIDATION_HISTORY_LAST_APPEND_WARNING="$(printf '%s' "${append_json}" | jq -r '.warning // ""' 2>/dev/null || echo '')"
	return 0
}

validation_history_gate_next_action() {
	local reason="$1"
	local integration_sha="$2"
	case "${reason}" in
		missing_pass)
			printf 'Validation label present, but no passing validation-history entry exists yet for integration SHA `%s`.' "${integration_sha:-unknown}"
			;;
		later_non_harness_failure)
			printf 'Validation label present, but a later non-harness validation failure is recorded for integration SHA `%s`; rerun validation before promoting.' "${integration_sha:-unknown}"
			;;
		*)
			printf 'Validation history is blocking eager draft promotion for integration SHA `%s`.' "${integration_sha:-unknown}"
			;;
	esac
}

validation_history_gate_decision_for_current_sha() {
	local integration_sha=""
	local history_json=""
	local warning_code=""

	integration_sha="$(validation_history_current_integration_sha 2>/dev/null || true)"
	if [ -z "${integration_sha}" ]; then
		jq -cn '{available: false, allow: true, reason: "integration_sha_unavailable", integration_sha: null}'
		return 0
	fi
	integration_sha="$(printf '%s' "${integration_sha}" | tr '[:upper:]' '[:lower:]')"

	if [ "${VALIDATION_HISTORY_LAST_APPEND_STORED:-}" = "false" ] && [ "${VALIDATION_HISTORY_LAST_APPEND_SHA:-}" = "${integration_sha}" ]; then
		jq -cn --arg integration_sha "${integration_sha}" --arg warning "${VALIDATION_HISTORY_LAST_APPEND_WARNING:-}" '
			{available: false, allow: true, reason: "history_write_failed_current_tick", integration_sha: $integration_sha, warning: (if $warning == "" then null else $warning end)}
		'
		return 0
	fi

	history_json="$(memory_validation_history_get \
		--repo-root . \
		--memory-branch "${AI_MEMORY_BRANCH:-ai-memory}" \
		--memory-root "${AI_MEMORY_ROOT:-ai-memory}" \
		--repo "${GITHUB_REPOSITORY}" \
		--integration-sha "${integration_sha}" 2>/dev/null || echo '')"
	if [ -z "${history_json}" ] || ! printf '%s' "${history_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		jq -cn --arg integration_sha "${integration_sha}" '{available: false, allow: true, reason: "history_read_invalid", integration_sha: $integration_sha}'
		return 0
	fi

	warning_code="$(printf '%s' "${history_json}" | jq -r '.warning_code // ""' 2>/dev/null || echo '')"
	if [ "$(printf '%s' "${history_json}" | jq -r '.enabled // false' 2>/dev/null || echo false)" != "true" ] || [ -n "${warning_code}" ]; then
		printf '%s' "${history_json}" | jq -c --arg integration_sha "${integration_sha}" --arg reason "${warning_code:-history_unavailable}" '
			{
				available: false,
				allow: true,
				reason: $reason,
				integration_sha: $integration_sha,
				warning: (.warning // null)
			}
		'
		return 0
	fi

	printf '%s' "${history_json}" | jq -c --arg integration_sha "${integration_sha}" '
		def raw_status_text:
			(.raw_status | if type == "string" then ascii_downcase else "" end);
		def ordering_key:
			[(.recorded_at // ""), .__idx];
		def is_pass:
			((.outcome // "") | ascii_downcase) as $outcome
			| ($outcome == "passed" or $outcome == "pass" or $outcome == "success");
		def is_fail:
			((.outcome // "") | ascii_downcase) as $outcome
			| ((.raw_conclusion // "") | ascii_downcase) as $conclusion
			| ($outcome == "failed" or $outcome == "fail" or $outcome == "error" or $outcome == "errored" or $conclusion == "failure");
		(.validation_history.entries // []) as $entries
		| ($entries | to_entries | map(.value + {__idx: .key})) as $indexed
		| ($indexed | map(select(is_pass and (raw_status_text != "harness_error")))) as $passes
		| if ($passes | length) == 0 then
			{
				available: true,
				allow: false,
				reason: "missing_pass",
				integration_sha: $integration_sha
			}
		else
			($passes | max_by(ordering_key)) as $latest_pass
			| ($indexed
				| map(select(
					is_fail
					and (raw_status_text != "harness_error")
					and (ordering_key > [($latest_pass.recorded_at // ""), $latest_pass.__idx])
				))) as $later_failures
			| if ($later_failures | length) > 0 then
				($later_failures | max_by(ordering_key)) as $latest_failure
				| {
					available: true,
					allow: false,
					reason: "later_non_harness_failure",
					integration_sha: $integration_sha,
					latest_pass_recorded_at: ($latest_pass.recorded_at // null),
					latest_failure_recorded_at: ($latest_failure.recorded_at // null)
				}
			else
				{
					available: true,
					allow: true,
					reason: "pass_record_present",
					integration_sha: $integration_sha,
					latest_pass_recorded_at: ($latest_pass.recorded_at // null)
				}
			end
		end
	'
}

mark_integration_branch_squash_fresh() {
	local now_epoch="${1:-$(date +%s)}"
	if ! [[ "${now_epoch}" =~ ^[0-9]+$ ]]; then
		now_epoch="$(date +%s)"
	fi
	jq --argjson now_epoch "${now_epoch}" '
		.last_main_squash_at_utc = $now_epoch
		| .integration_stale_last_alerted_at_utc = null
	' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
}

check_integration_branch_staleness() {
	local integration_branch="$1"
	local default_branch="$2"
	local ahead_by="$3"
	local now_epoch="$(date +%s)"
	local last_main_squash_at_utc=""
	local integration_stale_last_alerted_at_utc=""

	# ORCH_INTEGRATION_STALE_ALERT_HOURS=0 disables this alert entirely
	# (parity with ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS=0). Return before
	# touching state so the disabled path is a true no-op.
	if [ "${ORCH_INTEGRATION_STALE_ALERT_HOURS}" -eq 0 ]; then
		return 0
	fi

	local stale_threshold_secs=$(( ORCH_INTEGRATION_STALE_ALERT_HOURS * 3600 ))
	local stale_realert_secs=$(( ORCH_INTEGRATION_STALE_REALERT_HOURS * 3600 ))
	local stale_age_secs=0

	[ -f "${STATE_FILE}" ] || return 0
	[ -n "${integration_branch}" ] || return 0

	if ! [[ "${ahead_by}" =~ ^[0-9]+$ ]]; then
		return 0
	fi

	if [ "${ahead_by}" -eq 0 ]; then
		mark_integration_branch_squash_fresh "${now_epoch}"
		return 0
	fi

	last_main_squash_at_utc="$(jq -r '.last_main_squash_at_utc // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
	if ! [[ "${last_main_squash_at_utc}" =~ ^[0-9]+$ ]]; then
		mark_integration_branch_squash_fresh "${now_epoch}"
		return 0
	fi
	integration_stale_last_alerted_at_utc="$(jq -r '.integration_stale_last_alerted_at_utc // empty' "${STATE_FILE}" 2>/dev/null || echo "")"

	stale_age_secs=$(( now_epoch - last_main_squash_at_utc ))
	if [ "${stale_age_secs}" -lt "${stale_threshold_secs}" ]; then
		return 0
	fi

	if [[ "${integration_stale_last_alerted_at_utc}" =~ ^[0-9]+$ ]] \
		&& [ $(( now_epoch - integration_stale_last_alerted_at_utc )) -lt "${stale_realert_secs}" ]; then
		return 0
	fi

	jq --argjson now_epoch "${now_epoch}" '.integration_stale_last_alerted_at_utc = $now_epoch' \
		"${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
	echo "INTEGRATION_STALE_ALERT_SENT tracking_issue=${TRACKING_NUM} integration_branch=${integration_branch} default_branch=${default_branch} ahead_by=${ahead_by} stale_hours=$(( stale_age_secs / 3600 )) threshold_hours=${ORCH_INTEGRATION_STALE_ALERT_HOURS}"
	tg_notify "Integration branch '${integration_branch}' for project #${TRACKING_NUM} has been ahead of '${default_branch}' for at least $(( stale_age_secs / 3600 )) hour(s) (ahead_by=${ahead_by})." "WARNING"
}

_build_branch_rebuild_audit_json() {
  local integration_branch="$1"
  local default_branch="$2"
  local final_pr="$3"
  local final_pr_head_sha="$4"
  local resolver_retry_escalated_at="$5"
  local rebuild_at="$6"
  local default_branch_head_sha="$7"
  local replay_commits_json="$8"
  local outcome="$9"
  local branch_protected="${10:-}"
  local failure_detail="${11:-}"
  local completed_at="${12:-}"
  # _attempt_branch_rebuild_after_escalation populates this once the
  # integration branch ref has been resolved. Earlier audit writes (before the
  # branch ref is loaded) intentionally fall back to null instead of
  # misreporting the final PR head as the pre-rebuild branch head.
  local pre_rebuild_branch_head_sha="${branch_rebuild_pre_delete_sha:-}"

  [ -n "${replay_commits_json}" ] || replay_commits_json='[]'

  jq -cn \
    --arg repo "${GITHUB_REPOSITORY}" \
    --argjson tracking_issue "${TRACKING_NUM}" \
    --arg integration_branch "${integration_branch}" \
    --arg default_branch "${default_branch}" \
    --arg last_rebuild_at "${rebuild_at}" \
    --arg trigger_reason "resolver_escalated_threshold" \
    --arg resolver_escalated_at "${resolver_retry_escalated_at}" \
    --arg final_pr "${final_pr}" \
    --arg final_pr_head_sha "${final_pr_head_sha}" \
    --arg pre_rebuild_branch_head_sha "${pre_rebuild_branch_head_sha}" \
    --arg default_branch_head_sha "${default_branch_head_sha}" \
    --argjson replay_commits "${replay_commits_json}" \
    --arg outcome "${outcome}" \
    --arg branch_protected "${branch_protected}" \
    --arg failure_detail "${failure_detail}" \
    --arg completed_at "${completed_at}" '
      {
        schema_version: "v1",
        repository: $repo,
        tracking_issue_number: $tracking_issue,
        integration_branch: $integration_branch,
        default_branch: $default_branch,
        last_rebuild_at: $last_rebuild_at,
        trigger_reason: $trigger_reason,
        resolver_escalated_at: (if $resolver_escalated_at == "" then null else $resolver_escalated_at end),
        final_pr_number: (if $final_pr == "" then null else ($final_pr | tonumber) end),
        final_pr_head_sha: (if $final_pr_head_sha == "" then null else $final_pr_head_sha end),
        pre_rebuild_branch_head_sha: (if $pre_rebuild_branch_head_sha == "" then null else $pre_rebuild_branch_head_sha end),
        default_branch_head_sha: (if $default_branch_head_sha == "" then null else $default_branch_head_sha end),
        replay_commits: $replay_commits,
        outcome: $outcome,
        branch_protected: (
          if $branch_protected == "true" then true
          elif $branch_protected == "false" then false
          else null end
        ),
        failure_detail: (if $failure_detail == "" then null else $failure_detail end),
        completed_at: (if $completed_at == "" then null else $completed_at end)
      }
    '
}

_check_branch_rebuild_threshold() {
  local integration_branch="$1"
  local resolver_retry_escalated_at="$2"

  BRANCH_REBUILD_SKIP_REASON=""
  BRANCH_REBUILD_LAST_REBUILD_AT=""
  BRANCH_REBUILD_ESCALATED_ERROR=""

  if [ "${BRANCH_REBUILD_ENABLED}" != "true" ]; then
    BRANCH_REBUILD_SKIP_REASON="disabled"
    return 1
  fi

  case "${integration_branch}" in
    orchestrator/project-*)
      ;;
    *)
      BRANCH_REBUILD_SKIP_REASON="unsupported_branch"
      return 1
      ;;
  esac

  if [ -z "${resolver_retry_escalated_at}" ]; then
    BRANCH_REBUILD_SKIP_REASON="missing_escalated_at"
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild threshold check could not find the resolver escalation timestamp in the final PR retry state."
    return 1
  fi

  local now_ts
  local escalated_ts
  local threshold_secs
  now_ts="$(date -u +%s)"
  if ! [[ "${now_ts}" =~ ^[0-9]+$ ]]; then
    echo "::warning::[branch-rebuild] date -u +%s returned a non-numeric current time during threshold evaluation." >&2
    BRANCH_REBUILD_SKIP_REASON="invalid_current_time"
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild threshold check could not read the current UTC epoch time."
    return 1
  fi
  escalated_ts="$(_iso8601_to_epoch "${resolver_retry_escalated_at}" || echo "")"
  if ! [[ "${escalated_ts}" =~ ^[0-9]+$ ]]; then
    BRANCH_REBUILD_SKIP_REASON="invalid_escalated_at"
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild threshold check could not parse the resolver escalation timestamp from the final PR retry state."
    return 1
  fi

  threshold_secs=$(( BRANCH_REBUILD_THRESHOLD_HOURS * 3600 ))
  if [ $(( now_ts - escalated_ts )) -lt "${threshold_secs}" ]; then
    BRANCH_REBUILD_SKIP_REASON="threshold_not_met"
    return 1
  fi

  local audit_response
  local audit_hit
  local last_rebuild_at
  local last_outcome
  local last_rebuild_ts
  local cooldown_secs
  audit_response="$(_branch_rebuild_audit_get "${integration_branch}" || echo "")"
  if [ -z "${audit_response}" ]; then
    BRANCH_REBUILD_SKIP_REASON="audit_unavailable"
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild is enabled but ai-memory audit storage is unavailable, disabled, or warning-bearing; refusing rebuild for '${integration_branch}'."
    return 1
  fi

  audit_hit="$(printf '%s' "${audit_response}" | jq -r 'if .ok == true and .hit == true and (.audit | type == "object") then "true" else "false" end' 2>/dev/null || echo "false")"
  if [ "${audit_hit}" = "true" ]; then
    last_rebuild_at="$(printf '%s' "${audit_response}" | jq -r '.audit.last_rebuild_at // ""' 2>/dev/null || echo "")"
    last_outcome="$(printf '%s' "${audit_response}" | jq -r '.audit.outcome // ""' 2>/dev/null || echo "")"
    if [ -n "${last_rebuild_at}" ]; then
      last_rebuild_ts="$(_iso8601_to_epoch "${last_rebuild_at}" || echo "")"
      if ! [[ "${last_rebuild_ts}" =~ ^[0-9]+$ ]]; then
        BRANCH_REBUILD_SKIP_REASON="invalid_audit_timestamp"
        BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild audit for '${integration_branch}' has an invalid last_rebuild_at timestamp; refusing rebuild."
        return 1
      fi

      cooldown_secs=$(( BRANCH_REBUILD_COOLDOWN_HOURS * 3600 ))
      BRANCH_REBUILD_LAST_REBUILD_AT="${last_rebuild_at}"
      if [ "${last_outcome}" != "skipped_preflight" ] && [ $(( now_ts - last_rebuild_ts )) -lt "${cooldown_secs}" ]; then
        BRANCH_REBUILD_SKIP_REASON="cooldown_active"
        return 1
      fi
    fi
  fi

  return 0
}

_derive_branch_rebuild_replay_commits() {
  BRANCH_REBUILD_REPLAY_COMMITS_JSON='[]'
  BRANCH_REBUILD_REPLAY_FAILURE_DETAIL=""

  local merged_issue_nums_json
  merged_issue_nums_json="$(jq -c '[.waves[]?.issues[]? | select(((.status // "") | ascii_downcase) == "merged" and (.github_issue != null)) | (.github_issue | tonumber?)] | map(select(. != null)) | unique' "${STATE_FILE}" 2>/dev/null || echo '[]')"
  if ! printf '%s' "${merged_issue_nums_json}" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
    BRANCH_REBUILD_REPLAY_FAILURE_DETAIL="No merged wave issues with GitHub issue numbers were available to replay."
    return 1
  fi

  local candidate_details_json
  candidate_details_json="$(_fetch_candidate_issue_details_graphql "${merged_issue_nums_json}")"
  if ! printf '%s' "${candidate_details_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    BRANCH_REBUILD_REPLAY_FAILURE_DETAIL="Unable to batch-fetch linked PR metadata for merged issues."
    return 1
  fi

  local replay_plan_json
  replay_plan_json="$(jq -cn --argjson issues "${merged_issue_nums_json}" --argjson details "${candidate_details_json}" '
    def missing($issue; $reason; $pr_number):
      {issue_number: $issue, error: $reason}
      + (if $pr_number == null then {} else {pr_number: $pr_number} end);
    def entry($issue):
      ($details[$issue|tostring] // null) as $detail
      | if $detail == null then
          missing($issue; "missing_issue_details"; null)
        else
          ($detail.linked_pr // null) as $pr
          | if $pr == null then
              missing($issue; "missing_linked_pr"; null)
            elif (($pr.merged // false) != true) then
              missing($issue; "linked_pr_not_merged"; ($pr.number // null))
            elif ((($pr.merge_commit_sha // "") | test("^[0-9A-Fa-f]{7,64}$")) | not) then
              missing($issue; "missing_merge_commit_sha"; ($pr.number // null))
            elif (($pr.merged_at // "") | length) == 0 then
              missing($issue; "missing_merged_at"; ($pr.number // null))
            else
              {
                issue_number: $issue,
                pr_number: ($pr.number | tonumber),
                merge_commit_sha: $pr.merge_commit_sha,
                merged_at: $pr.merged_at
              }
            end
        end;
    [($issues[] | tonumber)] | unique as $ordered
    | ($ordered | map(entry(.))) as $items
    | {
        ok: (all($items[]; (has("error") | not))),
        items: (if all($items[]; (has("error") | not)) then ($items | sort_by(.merged_at, .pr_number)) else [] end),
        missing: [ $items[] | select(has("error")) ]
      }
  ' 2>/dev/null || echo '')"
  if [ -z "${replay_plan_json}" ]; then
    BRANCH_REBUILD_REPLAY_FAILURE_DETAIL="Unable to transform merged-issue PR metadata into a replay plan."
    return 1
  fi

  if ! printf '%s' "${replay_plan_json}" | jq -e '.ok == true' >/dev/null 2>&1; then
    BRANCH_REBUILD_REPLAY_FAILURE_DETAIL="$(printf '%s' "${replay_plan_json}" | jq -r '[.missing[]? | "issue #\(.issue_number): \(.error)\(if .pr_number then " (PR #\(.pr_number))" else "" end)"] | if length > 0 then join("; ") else "missing replay metadata" end' 2>/dev/null || echo 'missing replay metadata')"
    return 1
  fi

  BRANCH_REBUILD_REPLAY_COMMITS_JSON="$(printf '%s' "${replay_plan_json}" | jq -c '.items // []' 2>/dev/null || echo '[]')"
  return 0
}

_mark_branch_rebuild_failed() {
  local integration_branch="$1"
  local default_branch="$2"
  local final_pr="$3"
  local reason="$4"
  local runbook_url=""

  jq --arg reason "${reason}" \
    '.status = "failed" |
     .final_merge_status = "failed" |
     .final_merge_error = $reason |
     .integration_sync_status = "branch_rebuild_failed" |
     .integration_sync_last_error = $reason' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment || true
  runbook_url="$(sync_rebuild_runbook_url "${default_branch}")"
  post_tracking_comment "## ❌ Integration branch rebuild failed

Last-resort rebuild of \`${integration_branch}\` failed while recovering final PR #${final_pr} into \`${default_branch}\`.

Reason: ${reason}

Runbook: [Rebuild integration branch](${runbook_url})"
  tg_notify "❌ Integration branch rebuild failed for #${TRACKING_NUM} (PR #${final_pr}, branch '${integration_branch}'): ${reason}" "CRITICAL"
}

_attempt_branch_rebuild_after_escalation() {
  local integration_branch="$1"
  local default_branch="$2"
  local final_pr="$3"
  local final_pr_head_sha="$4"
  local resolver_retry_state="$5"

  BRANCH_REBUILD_HANDLED="false"
  BRANCH_REBUILD_TERMINAL_FAILURE="false"
  BRANCH_REBUILD_ESCALATED_ERROR=""

  local resolver_retry_escalated_at=""
  resolver_retry_escalated_at="$(printf '%s' "${resolver_retry_state}" | jq -r '.escalated_at // ""' 2>/dev/null || echo "")"
  if ! _check_branch_rebuild_threshold "${integration_branch}" "${resolver_retry_escalated_at}"; then
    return 0
  fi

  local rebuild_started_at=""
  local default_branch_ref_uri=""
  local default_branch_head_sha=""
  local integration_branch_uri=""
  local branch_payload=""
  local branch_protected="false"
  local branch_rebuild_pre_delete_sha=""
  local audit_json=""
  local final_audit_json=""
  local completed_at=""
  local failure_detail=""

  rebuild_started_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if ! _derive_branch_rebuild_replay_commits; then
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' was skipped because replay metadata is incomplete: ${BRANCH_REBUILD_REPLAY_FAILURE_DETAIL}"
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "" "[]" "skipped_missing_replay" "false" "${BRANCH_REBUILD_REPLAY_FAILURE_DETAIL}" "${rebuild_started_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    return 0
  fi

  default_branch_ref_uri="$(printf '%s' "${default_branch}" | jq -sRr '@uri')"
  default_branch_head_sha="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/git/ref/heads/${default_branch_ref_uri}" --jq '.object.sha // ""' 2>/dev/null || echo "")"
  if ! [[ "${default_branch_head_sha}" =~ ^[0-9A-Fa-f]{7,64}$ ]]; then
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' was skipped because the current '${default_branch}' head SHA could not be resolved."
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_preflight" "false" "Unable to resolve the current ${default_branch} head SHA before rebuild." "${rebuild_started_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    return 0
  fi

  integration_branch_uri="$(printf '%s' "${integration_branch}" | jq -sRr '@uri')"
  # Existing final-PR fetch already supplies head SHA/body/mergeable. The
  # branch endpoint is the smallest extra API shape that adds branch-protection
  # state without a second git/ref + protection probe pair.
  branch_payload="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/branches/${integration_branch_uri}" 2>/dev/null || echo "")"
  if ! printf '%s' "${branch_payload}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' was skipped because branch protection metadata could not be loaded."
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_preflight" "false" "Unable to load branch protection metadata before rebuild." "${rebuild_started_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    return 0
  fi

  branch_protected="$(printf '%s' "${branch_payload}" | jq -r '.protected // false' 2>/dev/null || echo false)"
  branch_rebuild_pre_delete_sha="$(printf '%s' "${branch_payload}" | jq -r '.commit.sha // ""' 2>/dev/null || echo "")"
  if ! [[ "${branch_rebuild_pre_delete_sha}" =~ ^[0-9A-Fa-f]{7,64}$ ]]; then
    branch_rebuild_pre_delete_sha=""
  fi
  if [ "${branch_protected}" = "true" ]; then
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' is blocked because the branch is protected."
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_protected" "true" "Branch is protected; refusing delete/recreate rebuild flow." "${rebuild_started_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    tg_notify "⚠️ Branch rebuild for #${TRACKING_NUM} is blocked because '${integration_branch}' is protected. Leaving the project escalated for manual follow-up." "WARNING"
    return 0
  fi

  if ! git fetch --no-tags origin "refs/heads/${default_branch}:refs/remotes/origin/${default_branch}" >/dev/null 2>&1 \
    || ! git fetch --no-tags origin "refs/heads/${integration_branch}:refs/remotes/origin/${integration_branch}" >/dev/null 2>&1; then
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' was skipped because the local pre-rebuild fetch failed."
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_preflight" "false" "Unable to fetch ${default_branch} and ${integration_branch} refs locally before rebuild." "${rebuild_started_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    return 0
  fi

  audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "started" "false" "" "")"
  if [ -z "${audit_json}" ] || ! _branch_rebuild_audit_put "${integration_branch}" "${audit_json}" >/dev/null 2>&1; then
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' was skipped because the pre-delete audit snapshot could not be persisted."
    echo "::warning::[branch-rebuild] Refusing destructive rebuild for ${integration_branch} because the pre-delete audit snapshot could not be persisted." >&2
    return 0
  fi

  local delete_err=""
  if ! delete_err="$(gh_retry gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/refs/heads/${integration_branch_uri}" 2>&1 >/dev/null)"; then
    if printf '%s' "${delete_err}" | grep -Eqi 'protected|protected branch|refusing to delete'; then
      BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' is blocked because the branch could not be deleted (protected or ref-locked)."
      final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_protected" "true" "${delete_err}" "${rebuild_started_at}")"
      _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
      tg_notify "⚠️ Branch rebuild for #${TRACKING_NUM} could not delete '${integration_branch}' because GitHub reported it protected or ref-locked. Leaving the project escalated for manual follow-up." "WARNING"
      return 0
    fi

    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' was skipped because the branch ref could not be deleted."
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_preflight" "false" "${delete_err}" "${rebuild_started_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    return 0
  fi

  local create_err=""
  if ! create_err="$(gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/git/refs" -f ref="refs/heads/${integration_branch}" -f sha="${default_branch_head_sha}" 2>&1 >/dev/null)"; then
    completed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "failed" "false" "Deleted branch ref but failed to recreate '${integration_branch}' from ${default_branch}@${default_branch_head_sha}: ${create_err}" "${completed_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    _mark_branch_rebuild_failed "${integration_branch}" "${default_branch}" "${final_pr}" "Failed to recreate '${integration_branch}' from ${default_branch}@${default_branch_head_sha}: ${create_err}"
    BRANCH_REBUILD_HANDLED="true"
    BRANCH_REBUILD_TERMINAL_FAILURE="true"
    return 0
  fi

  local worktree_dir=""
  local replay_log=""
  local replay_rc=0
  worktree_dir="$(mktemp -d "${TMPDIR:-/tmp}/branch-rebuild-wt.XXXXXX" 2>/dev/null || true)"
  replay_log="$(mktemp "${TMPDIR:-/tmp}/branch-rebuild-log.XXXXXX" 2>/dev/null || true)"
  if [ -z "${worktree_dir}" ] || [ -z "${replay_log}" ]; then
    [ -n "${worktree_dir}" ] && rm -rf "${worktree_dir}" >/dev/null 2>&1 || true
    [ -n "${replay_log}" ] && rm -f "${replay_log}" >/dev/null 2>&1 || true
    completed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "failed" "false" "Recreated '${integration_branch}' but could not allocate a temporary worktree or replay log." "${completed_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    _mark_branch_rebuild_failed "${integration_branch}" "${default_branch}" "${final_pr}" "Recreated '${integration_branch}' but could not allocate a temporary worktree or replay log."
    BRANCH_REBUILD_HANDLED="true"
    BRANCH_REBUILD_TERMINAL_FAILURE="true"
    return 0
  fi

  if ! git fetch --no-tags origin "refs/heads/${integration_branch}:refs/remotes/origin/${integration_branch}" >/dev/null 2>&1; then
    completed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    BRANCH_REBUILD_ESCALATED_ERROR="Branch rebuild for '${integration_branch}' recreated the remote ref but could not fetch it locally; leaving the project escalated for retry."
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "skipped_preflight" "false" "Recreated '${integration_branch}' but could not fetch the new remote branch ref locally; leaving the project escalated for retry." "${completed_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
    rm -rf "${worktree_dir}" >/dev/null 2>&1 || true
    rm -f "${replay_log}" >/dev/null 2>&1 || true
    return 0
  fi

	if ! git worktree add --detach "${worktree_dir}" "refs/remotes/origin/${integration_branch}" >/dev/null 2>&1; then
	  completed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
	  final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "failed" "false" "Recreated '${integration_branch}' but could not check out the rebuilt branch in a temporary worktree." "${completed_at}")"
	  _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
	  rm -rf "${worktree_dir}" >/dev/null 2>&1 || true
    rm -f "${replay_log}" >/dev/null 2>&1 || true
    _mark_branch_rebuild_failed "${integration_branch}" "${default_branch}" "${final_pr}" "Recreated '${integration_branch}' but could not check out the rebuilt branch in a temporary worktree."
    BRANCH_REBUILD_HANDLED="true"
	  BRANCH_REBUILD_TERMINAL_FAILURE="true"
	  return 0
	fi
	worktree_registry_register "$(basename -- "${worktree_dir}")" "${worktree_dir}" "refs/remotes/origin/${integration_branch}" "pr-${final_pr}" "orchestrate-poll"

  # Replay exit codes: 20=worktree inaccessible, 21=missing/invalid
  # merge SHA, 22=missing commit object, 23=cherry-pick failed,
  # 24=push failed, 25=invalid parent-count parse,
  # 26=git identity config failed.
  (
    set +e
    cd "${worktree_dir}" || exit 20
    git config user.name "codex-bot" >>"${replay_log}" 2>&1 || exit 26
    git config user.email "codex@users.noreply.github.com" >>"${replay_log}" 2>&1 || exit 26
    while IFS= read -r replay_item; do
      [ -n "${replay_item}" ] || continue
      merge_commit_sha="$(printf '%s' "${replay_item}" | jq -r '.merge_commit_sha // ""' 2>/dev/null || echo "")"
      [ -n "${merge_commit_sha}" ] || exit 21
      if ! [[ "${merge_commit_sha}" =~ ^[0-9A-Fa-f]{7,64}$ ]]; then
        echo "invalid merge_commit_sha ${merge_commit_sha}" >> "${replay_log}"
        exit 21
      fi

      parent_line="$(git rev-list --parents -n 1 "${merge_commit_sha}" 2>>"${replay_log}" || true)"
      if [ -z "${parent_line}" ]; then
        echo "missing commit object ${merge_commit_sha}" >> "${replay_log}"
        exit 22
      fi

      parent_count="$(printf '%s\n' "${parent_line}" | awk '{print NF - 1}')"
      if ! [[ "${parent_count}" =~ ^[0-9]+$ ]]; then
        echo "invalid parent count for ${merge_commit_sha}: ${parent_count}" >> "${replay_log}"
        exit 25
      fi
      if [ "${parent_count}" -gt 1 ]; then
        git cherry-pick -m 1 --allow-empty "${merge_commit_sha}" >>"${replay_log}" 2>&1 || exit 23
      else
        git cherry-pick --allow-empty "${merge_commit_sha}" >>"${replay_log}" 2>&1 || exit 23
      fi
    done < <(printf '%s' "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" | jq -c '.[]' 2>/dev/null)

    git push origin "HEAD:refs/heads/${integration_branch}" >>"${replay_log}" 2>&1 || exit 24
    exit 0
	# Capture replay-specific exit codes without letting `set -e` bypass
	# the deregister/remove cleanup and structured failure handling.
	) || replay_rc=$?

  if [ "${replay_rc}" -ne 0 ]; then
    local replay_failure_context=""
    git -C "${worktree_dir}" cherry-pick --abort >/dev/null 2>&1 || true
    case "${replay_rc}" in
      20) replay_failure_context="temporary worktree became inaccessible" ;;
      21) replay_failure_context="replay plan entry was missing or had an invalid merge commit SHA" ;;
      22) replay_failure_context="replay commit object was not available in the local clone" ;;
      23) replay_failure_context="git cherry-pick failed while replaying merged sub-PR commits" ;;
      24) replay_failure_context="git push failed after replaying merged sub-PR commits" ;;
      25) replay_failure_context="replay parent-count parsing failed before cherry-pick mode selection" ;;
      26) replay_failure_context="git identity configuration failed before replay cherry-picks" ;;
      *) replay_failure_context="branch rebuild replay failed" ;;
    esac
    failure_detail="$(tail -n 20 "${replay_log}" 2>/dev/null | tr '\r\n' '  ' | sed 's/[[:space:]]\+/ /g' | cut -c1-2000)"
    if [ -n "${failure_detail}" ]; then
      failure_detail="${replay_failure_context}: ${failure_detail}"
    else
      failure_detail="${replay_failure_context}."
    fi
    completed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "failed" "false" "${failure_detail}" "${completed_at}")"
    _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true
	worktree_registry_deregister "$(basename -- "${worktree_dir}")"
	git worktree remove --force "${worktree_dir}" >/dev/null 2>&1 || rm -rf "${worktree_dir}"
	rm -f "${replay_log}" >/dev/null 2>&1 || true
	_mark_branch_rebuild_failed "${integration_branch}" "${default_branch}" "${final_pr}" "${failure_detail}"
    BRANCH_REBUILD_HANDLED="true"
    BRANCH_REBUILD_TERMINAL_FAILURE="true"
    return 0
  fi

	worktree_registry_deregister "$(basename -- "${worktree_dir}")"
	git worktree remove --force "${worktree_dir}" >/dev/null 2>&1 || rm -rf "${worktree_dir}"
	rm -f "${replay_log}" >/dev/null 2>&1 || true

  completed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  final_audit_json="$(_build_branch_rebuild_audit_json "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_escalated_at}" "${rebuild_started_at}" "${default_branch_head_sha}" "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" "success" "false" "" "${completed_at}")"
  _branch_rebuild_audit_put "${integration_branch}" "${final_audit_json}" >/dev/null 2>&1 || true

  local replay_count
  replay_count="$(printf '%s' "${BRANCH_REBUILD_REPLAY_COMMITS_JSON}" | jq -r 'length' 2>/dev/null || echo 0)"
  jq '.integration_sync_status = "healing" |
      .integration_sync_last_error = "" |
      .integration_conflict_unresolved_ticks = 0 |
      .integration_conflict_dispatch_count = 0 |
      .integration_conflict_dispatch_ts = 0 |
      .integration_conflict_total_dispatches = 0' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment || true
  post_tracking_comment "## 🔁 Integration branch rebuilt

Recreated \`${integration_branch}\` from \`${default_branch}\` and replayed ${replay_count} merged sub-PR commit(s) after resolver escalation persisted past ${BRANCH_REBUILD_THRESHOLD_HOURS}h. Waiting for GitHub to recompute mergeability on final PR #${final_pr}."
  BRANCH_REBUILD_HANDLED="true"
  BRANCH_REBUILD_TERMINAL_FAILURE="false"
  return 0
}

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

  # Pre-flight merge probe (Fix D): the orchestrator only sees GitHub's
  # PR-mergeable signal, which is computed asynchronously and can lag by
  # minutes/hours on large PRs. heal_integration_branch_conflict is
  # regularly called with that signal stale — mergeable=false when the
  # real merge is already clean (e.g. after a recent [ai-merge-resolve]
  # or sub-issue squash-merge advanced the integration branch). When
  # that happens, the downstream ai-review.yml dispatch recomputes
  # MERGE_CONFLICT at runtime and falls through to the regular autofix
  # path, but the orchestrator has already incremented
  # integration_conflict_total_dispatches, eating one of the
  # INTEGRATION_CONFLICT_LIFETIME_MAX lifetime slots without engaging
  # the resolver (orchestrator/project-40, 2026-05-12: 7 of 10 lifetime
  # dispatches were this stale-signal case). Catching clean-merge here
  # skips dispatch and the counter increment.
  #
  # Fail-open on any probe error (git unavailable, merge-tree
  # unsupported, refs unreachable): fall through to the existing
  # dispatch path so behaviour is unchanged on unsupported runners.
  # The "clean" branch only fires when we positively verified a clean
  # three-way merge — we never mark clean on probe failure.
  if command -v git >/dev/null 2>&1 \
     && git merge-tree --write-tree --name-only --no-messages HEAD HEAD >/dev/null 2>&1; then
    if git fetch --no-tags --quiet origin \
         "+refs/heads/${integration_branch}:refs/remotes/origin/${integration_branch}" \
         "+refs/heads/${default_branch}:refs/remotes/origin/${default_branch}" 2>/dev/null \
       && git rev-parse --verify --quiet "refs/remotes/origin/${integration_branch}" >/dev/null 2>&1 \
       && git rev-parse --verify --quiet "refs/remotes/origin/${default_branch}" >/dev/null 2>&1 \
       && git merge-tree --write-tree --name-only --no-messages \
            "refs/remotes/origin/${default_branch}" \
            "refs/remotes/origin/${integration_branch}" >/dev/null 2>&1; then
      echo "  [integration-heal] Pre-flight merge probe: ${integration_branch} merges cleanly into ${default_branch}; clearing conflict state without dispatching for PR #${final_pr}."
      mark_integration_sync_clean "${default_branch}"
      return 0
    fi
  fi

  local final_pr_payload=""
  local final_pr_head_sha=""
  local final_pr_body=""
  local final_pr_mergeable=""
  # GitHub API hygiene audit: this function already re-reads
  # `repos/.../pulls/${final_pr}` later for `.mergeable` during the
  # lifetime-cap recovery branch. The resolver escape-valve gate also
  # needs `.head.sha` + `.body` on every conflict tick, so fetch the full
  # PR JSON once here and reuse `.mergeable` below instead of adding
  # another per-field call.
  if final_pr_payload="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}")"; then
    if printf '%s' "${final_pr_payload}" | jq -e . >/dev/null 2>&1; then
      final_pr_head_sha="$(printf '%s' "${final_pr_payload}" | jq -r '.head.sha // ""' 2>/dev/null || echo "")"
      final_pr_body="$(printf '%s' "${final_pr_payload}" | jq -r '.body // ""' 2>/dev/null || echo "")"
      final_pr_mergeable="$(printf '%s' "${final_pr_payload}" | jq -r 'if .mergeable != null then .mergeable else empty end' 2>/dev/null || echo "")"
    else
      echo "::warning::[integration-heal] Final PR #${final_pr} metadata fetch returned non-JSON; skipping resolver escape-valve gate this tick."
      final_pr_payload=""
    fi
  else
    echo "::warning::[integration-heal] Could not load final PR #${final_pr} metadata; skipping resolver escape-valve gate this tick."
  fi

  if [ -n "${final_pr_body}" ] && [ -n "${final_pr_head_sha}" ]; then
    local resolver_retry_state=""
    local resolver_retry_head_sha=""
    local resolver_retry_escalated="false"
    resolver_retry_state="$(printf '%s' "${final_pr_body}" | extract_autofix_resolver_retry_state_from_pr_body || true)"
    if [ -n "${resolver_retry_state}" ]; then
      resolver_retry_head_sha="$(printf '%s' "${resolver_retry_state}" | jq -r '.head_sha // ""' 2>/dev/null || echo "")"
      resolver_retry_escalated="$(printf '%s' "${resolver_retry_state}" | jq -r '.escalated // false' 2>/dev/null || echo false)"
      if [ "${resolver_retry_escalated}" = "true" ] && [ "${resolver_retry_head_sha}" = "${final_pr_head_sha}" ]; then
        _attempt_branch_rebuild_after_escalation "${integration_branch}" "${default_branch}" "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_state}"
        if [ "${BRANCH_REBUILD_HANDLED:-false}" = "true" ]; then
          if [ "${BRANCH_REBUILD_TERMINAL_FAILURE:-false}" = "true" ]; then
            return 1
          fi
          return 0
        fi
        if [ -n "${BRANCH_REBUILD_ESCALATED_ERROR:-}" ]; then
          echo "  [integration-heal] ${BRANCH_REBUILD_ESCALATED_ERROR}" >&2
        fi
        echo "  [integration-heal] Resolver escape threshold already tripped for final PR #${final_pr} at head ${final_pr_head_sha}; skipping redispatch until the PR head changes."
        jq --arg err "${BRANCH_REBUILD_ESCALATED_ERROR:-resolver escape threshold reached for final PR #${final_pr} at head ${final_pr_head_sha}}" \
          '.integration_sync_status = "escalated" |
           .integration_sync_last_error = $err' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment || true
        return 0
      fi
      if [ -n "${resolver_retry_head_sha}" ] && [ "${resolver_retry_head_sha}" != "${final_pr_head_sha}" ]; then
        echo "  [integration-heal] Final PR #${final_pr} head advanced from ${resolver_retry_head_sha} to ${final_pr_head_sha} since the persisted resolver retry state; resetting per-head conflict counters."
        jq '.integration_conflict_unresolved_ticks = 0 |
            .integration_conflict_dispatch_count = 0 |
            .integration_conflict_dispatch_ts = 0' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi
    fi
  fi

  local now_ts
  now_ts="$(date -u +%s)"
  local last_ts
  last_ts="$(jq -r '.integration_conflict_dispatch_ts // 0' "${STATE_FILE}")"
  local dispatch_count
  dispatch_count="$(jq -r '.integration_conflict_dispatch_count // 0' "${STATE_FILE}")"
  local unresolved_ticks
  unresolved_ticks="$(jq -r '.integration_conflict_unresolved_ticks // 0' "${STATE_FILE}")"
  local total_dispatches
  total_dispatches="$(jq -r '.integration_conflict_total_dispatches // 0' "${STATE_FILE}")"

  # Lifetime cap: once we've issued INTEGRATION_CONFLICT_LIFETIME_MAX
  # total resolver+judge dispatches across all retry episodes for this
  # integration branch, force a terminal "failed" state regardless of
  # whether the per-episode counters would still permit another
  # attempt.  Catches the alternating resolver/judge loop where each
  # judge invocation resets unresolved_ticks to 0 but the merge stays
  # dirty as main keeps moving (orchestrator/project-1479, 2026-04-25).
  if [ "${total_dispatches}" -ge "${INTEGRATION_CONFLICT_LIFETIME_MAX}" ]; then
    # Race-recovery (Fix A): before terminalizing, check whether the
    # apparent cap exhaustion is actually a timing artifact. The
    # counter is incremented at dispatch time (see line ~3556), not at
    # dispatch completion, so a long-running resolver invocation can
    # still be in flight when the next poll tick fires this cap
    # (orchestrator/project-40, 2026-05-12: dispatch #10 completed
    # success ~22 min after the cap alert was posted, leaving the PR
    # mergeable but the state terminalized to failed).
    #
    # Two recovery paths:
    #   (1) PR is now mergeable — an earlier dispatch landed its
    #       [ai-merge-resolve] commit between the previous tick and
    #       now. Clear conflict state and return so
    #       finalize_integration_merge_if_needed can merge on the next
    #       tick.
    #   (2) An autofix run is still in flight against the integration
    #       branch — defer the terminalization decision one tick so
    #       the in-flight dispatch gets to finish what it started.
    #
    # Both checks fail-open on API error (treat as "no recovery
    # signal" → fall through to existing terminalization), so a
    # GitHub outage cannot silently extend the cap indefinitely.
    local _ihbc_pr_mergeable="${final_pr_mergeable}"
    if [ -z "${_ihbc_pr_mergeable}" ]; then
      _ihbc_pr_mergeable="$(gh_retry _safe_gh_jq \
        "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" \
        --jq '.mergeable // false' 2>/dev/null || {
          echo "::warning::Unable to re-query mergeable status for PR #${final_pr}; treating it as no recovery signal during lifetime-cap handling." >&2
          echo "false"
        })"
    fi
    if [ "${_ihbc_pr_mergeable}" = "true" ]; then
      echo "  [integration-heal] Lifetime cap reached but PR #${final_pr} is now mergeable (late-finishing resolver dispatch landed); clearing conflict state instead of terminalizing."
      mark_integration_sync_clean "${default_branch}"
      return 0
    fi
    if _has_active_autofix_run "${final_pr}" "${integration_branch}"; then
      echo "  [integration-heal] Lifetime cap reached but an autofix run is still in flight for PR #${final_pr}; deferring terminalization one tick."
      return 0
    fi
    jq --arg err "lifetime dispatch cap (${INTEGRATION_CONFLICT_LIFETIME_MAX}) reached" \
      '.status = "failed" |
       .final_merge_status = "failed" |
       .integration_sync_status = "failed" |
       .integration_sync_last_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    post_tracking_comment "## ❌ Integration self-healing capped

Final PR #${final_pr} (\`${integration_branch}\` -> \`${default_branch}\`) hit the lifetime dispatch cap of ${INTEGRATION_CONFLICT_LIFETIME_MAX} resolver+judge attempts. Manual intervention required."
    set_failed_completion_status_comment \
      "Integration self-healing hit the lifetime dispatch cap of ${INTEGRATION_CONFLICT_LIFETIME_MAX} resolver+judge attempt(s) for final PR #${final_pr}. Manual intervention required. See the \"❌ Integration self-healing capped\" comment for the diagnostic detail."
    tg_notify "❌ Integration self-healing capped at ${INTEGRATION_CONFLICT_LIFETIME_MAX} dispatches for #${TRACKING_NUM} (PR #${final_pr}). Manual intervention required."
    return 1
  fi

  jq --arg err "${error_msg}" \
    '.integration_sync_status = "conflict" |
     .integration_sync_last_error = $err' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

  # Pick the effective retry budget. For integration-branch sync
  # conflicts (head ref matches orchestrator/project-*) the first-line
  # resolver lacks merged-sub-issue intent context, so we honour the
  # tighter INTEGRATION_SYNC_CONFLICT_MAX_RETRIES knob (default 1) to
  # escalate to the integration judge sooner. For all other dispatch
  # paths we keep the historical INTEGRATION_CONFLICT_MAX_RETRIES
  # behaviour so non-integration-sync callers are unaffected.
  local effective_max_retries="${INTEGRATION_CONFLICT_MAX_RETRIES}"
  case "${integration_branch}" in
    orchestrator/project-*)
      effective_max_retries="${INTEGRATION_SYNC_CONFLICT_MAX_RETRIES}"
      # Hot-file conflict short-circuit (Q2c): when the merge produces
      # ≤3 conflicting files and at least one is in the committed
      # hot-files seed at .github/ai/hot_files.json, the first-line
      # resolver has nothing useful to add — these are the god-file
      # collisions that need the judge's sub-issue intent context.
      # Skip the resolver entirely by forcing the retry budget to 0,
      # which makes the unresolved_ticks check fire the judge on the
      # very first tick.  Fail-open on any probe error.
      local _ihb_conflict_files
      if _ihb_conflict_files="$(_list_integration_conflict_files "${integration_branch}" "${default_branch}" 2>/dev/null)"; then
        local _ihb_conflict_count
        _ihb_conflict_count="$(printf '%s\n' "${_ihb_conflict_files}" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
        if [[ "${_ihb_conflict_count}" =~ ^[0-9]+$ ]] && [ "${_ihb_conflict_count}" -gt 0 ] && [ "${_ihb_conflict_count}" -le 3 ] && [ -f ".github/ai/hot_files.json" ]; then
          local _ihb_hot_files
          _ihb_hot_files="$(jq -r '.hot_files[]? // empty' .github/ai/hot_files.json 2>/dev/null || echo "")"
          if [ -n "${_ihb_hot_files}" ]; then
            local _ihb_hot_hit="false"
            local _ihb_cf
            while IFS= read -r _ihb_cf; do
              [ -n "${_ihb_cf}" ] || continue
              if printf '%s\n' "${_ihb_hot_files}" | grep -Fxq -- "${_ihb_cf}"; then
                _ihb_hot_hit="true"
                break
              fi
            done <<< "${_ihb_conflict_files}"
            if [ "${_ihb_hot_hit}" = "true" ]; then
              echo "  [integration-heal] Hot-file conflict detected (${_ihb_conflict_count} file(s), includes hot file); skipping first-line resolver and routing to judge directly."
              effective_max_retries=0
            fi
          fi
        fi
      fi
      ;;
  esac
  # Circuit breaker: after MAX retries, escalate to judge instead of
  # dispatching one more resolver run.
  if [ "${unresolved_ticks}" -ge "${effective_max_retries}" ]; then
    if invoke_judge_for_integration_conflict "${final_pr}" "${integration_branch}" "${default_branch}"; then
      # Reset unresolved ticks so the resolver loop can resume after
      # the judge's push. Keep dispatch_count as audit trail.
      # integration_conflict_total_dispatches counts judge invocations
      # too — they share the lifetime cap with resolver dispatches.
      total_dispatches=$((total_dispatches + 1))
      jq --argjson total "${total_dispatches}" \
        '.integration_sync_status = "healing" |
         .integration_conflict_unresolved_ticks = 0 |
         .integration_conflict_total_dispatches = $total |
         .integration_sync_last_error = ""' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment || true
      post_tracking_comment "## 🛠️ Integration judge invoked

Final PR #${final_pr} (\`${integration_branch}\` -> \`${default_branch}\`) did not become mergeable after ${effective_max_retries} automated resolver attempts. The judge has been invoked with full PR context to resolve conflicts. The poller will retry merge on the next tick."
      return 0
    fi
    jq --arg err "judge escalation failed: ${error_msg}" \
      '.status = "failed" |
       .final_merge_status = "failed" |
       .integration_sync_status = "failed" |
       .integration_sync_last_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    post_tracking_comment "## ❌ Integration self-healing exhausted

Final PR #${final_pr} (\`${integration_branch}\` -> \`${default_branch}\`) could not be made mergeable after ${effective_max_retries} automated attempts AND a judge escalation that itself failed. Manual intervention required."
    set_failed_completion_status_comment \
      "Integration self-healing could not make final PR #${final_pr} mergeable after ${effective_max_retries} automated attempt(s), and judge escalation failed. Manual intervention required. See the \"❌ Integration self-healing exhausted\" comment for the diagnostic detail."
    tg_notify "❌ Integration self-healing exhausted for #${TRACKING_NUM} (PR #${final_pr}). Manual intervention required."
    return 1
  fi

  # Cooldown gate: don't re-dispatch resolver too frequently.
  # Exponential backoff (Q2a): the cooldown doubles each dispatch (1×,
  # 2×, 4×, 8×, 16×) up to a 16× cap. With the default 900s base, the
  # gate is 15m on the first dispatch, climbing to 4h after four
  # retries. This stops the multi-hour fixed-interval loop where main
  # keeps moving and each dispatch fires on the same 15-minute beat.
  local elapsed=$((now_ts - last_ts))
  local _backoff_shift="${dispatch_count}"
  [[ "${_backoff_shift}" =~ ^[0-9]+$ ]] || _backoff_shift=0
  if [ "${_backoff_shift}" -gt 4 ]; then
    _backoff_shift=4
  fi
  local _backoff_multiplier=$(( 1 << _backoff_shift ))
  local effective_cooldown=$(( CONFLICT_DISPATCH_COOLDOWN_SECS * _backoff_multiplier ))
  if [ "${last_ts}" -gt 0 ] && [ "${elapsed}" -lt "${effective_cooldown}" ]; then
    echo "  [integration-heal] Dispatch cooldown active (${elapsed}s < ${effective_cooldown}s, base=${CONFLICT_DISPATCH_COOLDOWN_SECS}s × ${_backoff_multiplier}); deferring resolver dispatch for PR #${final_pr}."
    return 0
  fi

  # Force-refresh the resolver toolchain on the integration branch
  # from default_branch BEFORE dispatching, so any bug fixes shipped
  # to default_branch (e.g. PR #1581's --list-violated-files
  # expansion) take effect on this dispatch instead of being trapped
  # behind the deadlock where the integration branch's stale
  # resolver scripts cannot resolve the conflicts that block
  # default_branch from being merged in.  Fail-open: any git error
  # logs ::warning:: and falls through to the dispatch below.  See
  # the helper docstring above for the full rationale and the
  # function's contract.
  _refresh_integration_resolver_tooling "${integration_branch}" "${default_branch}"

  # Dispatch the existing review/autofix workflow against the final PR.
  local dispatch_rc=0
  _dispatch_review_for_conflicts "${final_pr}" "${integration_branch}" || dispatch_rc=$?

  if [ "${dispatch_rc}" -eq 2 ]; then
    jq --argjson ts "${now_ts}" \
      '.integration_sync_status = "healing" |
       .integration_conflict_dispatch_ts = $ts' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
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
      total_dispatches=$((total_dispatches + 1))
      jq --argjson count "${dispatch_count}" --argjson ts "${now_ts}" --argjson total "${total_dispatches}" \
        '.integration_sync_status = "healing" |
         .integration_conflict_dispatch_count = $count |
         .integration_conflict_dispatch_ts = $ts |
         .integration_conflict_total_dispatches = $total' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment || true
      # Only post a user-facing comment on the FIRST dispatch of this
      # conflict episode to avoid the every-tick spam pattern seen on
      # #832. Subsequent dispatches log to the state comment instead.
      if [ "${unresolved_ticks}" -eq 1 ]; then
        post_tracking_comment "## 🔧 Integration self-healing started

Detected a real merge conflict while syncing \`${default_branch}\` into \`${integration_branch}\`. Dispatched the review/autofix workflow against final PR #${final_pr} for automated resolution. Will retry up to ${effective_max_retries} times before escalating to the judge."
      fi
      tg_notify "🔧 Integration conflict on #${TRACKING_NUM}: dispatched resolver for PR #${final_pr} (attempt ${dispatch_count}, unresolved_ticks=${unresolved_ticks})." "WARNING"
      ;;
    *)
      echo "::warning::[integration-heal] Could not dispatch review workflow for PR #${final_pr}."
      ;;
  esac

  return 0
}

# Called after conflict recovery determines the final merge is clean.
# Idempotent.
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
    post_tracking_comment "## ✅ Integration self-healing resolved

Integration conflicts are cleared; final merge into \`${default_branch}\` will proceed on the next poll tick."
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
    evaluate_sync_superseded_by_main "${integration_branch}" "${default_branch}"
    if [ "${SYNC_SUPERSEDED_BY_MAIN}" = "true" ]; then
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
        post_state_comment || true
        post_tracking_comment "## ✅ Integration branch superseded by ${default_branch}

The integration branch \`${integration_branch}\` is marked as **superseded-by-main**. Sync is intentionally skipped in future poll cycles to avoid repeated conflict churn.

Runbook (if you need to rebuild the integration branch): [Rebuild integration branch](${runbook_url})"
      fi
      return 0
    fi

    if [ "${SYNC_SUPERSEDED_CONFIDENT:-true}" != "true" ]; then
      echo "::warning::Unable to revalidate superseded-by-main state due to transient API/read errors; keeping sync paused for now." >&2
      return 0
    fi

    jq '.sync = ((.sync // {}) + {
      "status": "active",
      "superseded_notified": false,
      "superseded_reason": "",
      "superseded_at": "",
      "last_sync_outcome": "active",
      "affected_paths": []
    })' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    sync_status="active"
  fi

  if ! integration_branch_exists "${integration_branch}"; then
    mark_integration_branch_missing_failed "${integration_branch}"
    return 1
  fi

  evaluate_sync_superseded_by_main "${integration_branch}" "${default_branch}"
  if [ "${SYNC_SUPERSEDED_CONFIDENT:-true}" = "true" ] && [ "${SYNC_SUPERSEDED_BY_MAIN}" = "true" ]; then
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
    post_state_comment || true
    post_tracking_comment "## ✅ Integration branch superseded by ${default_branch}

Skipping sync of \`${default_branch}\` into \`${integration_branch}\` because all tracked child PRs are terminal and the branch is now treated as superseded by \`${default_branch}\`.

Reason: ${SYNC_SUPERSEDED_REASON}

Runbook (if you need to rebuild the integration branch): [Rebuild integration branch](${runbook_url})"
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
      post_state_comment || true
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
  conflict_fingerprint="$(merge_tree_conflict_fingerprint "${conflict_paths_json}" "${default_ref}" "${integration_ref}")"

  jq --arg fp "${conflict_fingerprint}" --argjson paths "${conflict_paths_json}" \
    '.sync = ((.sync // {}) + {
      "status": "conflict",
      "last_sync_outcome": "conflict",
      "last_conflict_paths": $paths,
      "last_conflict_fingerprint": $fp
    })' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

  if [ "${sync_status}" != "conflict" ] || [ "${prev_conflict_fingerprint}" != "${conflict_fingerprint}" ]; then
    post_state_comment || true
    runbook_url="$(sync_rebuild_runbook_url "${default_branch}")"
    conflict_paths_md="$(format_conflict_paths_markdown "${conflict_paths_json}")"
    post_tracking_comment "## ⚠️ Integration sync conflict

Unable to sync \`${default_branch}\` into \`${integration_branch}\` due to merge conflicts. The project can continue, but final merge may require manual conflict resolution.

Conflicting paths:
${conflict_paths_md}

Runbook: [Rebuild integration branch](${runbook_url})"
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
	local validation_history_gate_json='{}'
	local validation_history_gate_reason=""
	local validation_history_gate_sha=""
	local validation_history_gate_wait_message=""

	# Default behavior: failed finalize attempts consume retry budget.
	# Transient "not-ready-yet" paths below opt out explicitly.
	FINAL_MERGE_BUDGET_ELIGIBLE="1"

  local final_merge_status
  final_merge_status="$(jq -r '.final_merge_status // "pending"' "${STATE_FILE}")"

  # Pinned "merged" state must be re-verified against the live integration
  # branch on every finalize tick. Without this re-check, an early auto-merge
  # of the eager final PR (e.g. via review_autofix.yml's auto-merge step when
  # an integration-conflict self-healing dispatch raced ahead) pins the state
  # to "merged" forever — even after subsequent wave PRs land on the
  # integration branch and never reach default. The pin then silently lets
  # mark_validation_complete declare ai:validated against a stale default
  # branch. See shubhodeep1/binance-blessings#135 for the regression.
  #
  # "superseded-by-main" is a legitimate terminal state (sync deliberately
  # gave up on the integration branch) and is NOT re-evaluated here.
  if [ "${final_merge_status}" = "merged" ] && [ -n "${integration_branch}" ]; then
    local _fimin_ahead_by _fimin_ahead_rc
    if _fimin_ahead_by="$(_integration_branch_ahead_of_default "${integration_branch}" "${default_branch}")"; then
      _fimin_ahead_rc=0
    else
      _fimin_ahead_rc=$?
    fi
    if [ "${_fimin_ahead_rc}" -ne 0 ]; then
      echo "::warning::  [final-merge] State pinned final_merge_status=merged for #${TRACKING_NUM:-?} but the compare API failed during the ahead_by re-check; failing closed and clearing the pin so the next tick can reopen the final PR if integration has drifted."
      jq '.final_merge_status = "pending" | .final_merge_pr = null | .final_merge_error = "compare API error during ahead_by re-check (failed closed)"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      final_merge_status="pending"
      post_state_comment || true
    elif [ "${_fimin_ahead_by:-0}" != "0" ]; then
      echo "  [final-merge] State pinned final_merge_status=merged for #${TRACKING_NUM:-?} but integration branch '${integration_branch}' is ahead of '${default_branch}' by ${_fimin_ahead_by} commit(s); clearing the pin so a fresh final PR can be opened for the new diff."
      jq --arg n "${_fimin_ahead_by}" --arg ib "${integration_branch}" --arg db "${default_branch}" \
        '.final_merge_status = "pending"
         | .final_merge_pr = null
         | .final_merge_error = ("integration branch " + $ib + " was ahead of " + $db + " by " + $n + " commit(s) after prior merge; reopening final PR for the new diff")' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      final_merge_status="pending"
      post_state_comment || true
    fi
  fi

  if [ "${final_merge_status}" = "merged" ] || [ "${final_merge_status}" = "superseded-by-main" ]; then
    return 0
  fi

  local sync_status
  sync_status="$(jq -r '.sync.status // "active"' "${STATE_FILE}")"
	if [ "${sync_status}" = "superseded-by-main" ]; then
		jq --arg reason "$(jq -r --arg default_branch "${default_branch}" '.sync.superseded_reason // ("Integration branch superseded by " + $default_branch + "; final merge intentionally skipped.")' "${STATE_FILE}")" \
			'.final_merge_status = "superseded-by-main" | .final_merge_error = $reason' \
			"${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
		post_state_comment || true
		return 0
	fi

	if [ -z "${integration_branch}" ]; then
		# Legacy/default-branch flows can validly omit integration_branch.
		# In that mode there is no integration→default final merge to perform,
		# so treat validation completion as merge-satisfied.
		FINAL_MERGE_BUDGET_ELIGIBLE="0"
		return 0
	fi

  final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}")"
  if [ -n "${final_pr}" ] && [ "${final_pr}" != "null" ]; then
    local existing_pr_state
    local existing_pr_merged
    existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    if [ "${existing_pr_state}" = "closed" ] && [ "${existing_pr_merged}" = "true" ]; then
      # Re-check ahead_by here too: even though the recorded final PR is
      # closed+merged, additional wave PRs could have landed on the integration
      # branch in the meantime (same regression path as the early-return
      # above). Without this check the function would set state=merged and
      # return, leaving the new diff stranded. See
      # shubhodeep1/binance-blessings#135.
      local _fimin_rd_ahead_by _fimin_rd_ahead_rc
      if _fimin_rd_ahead_by="$(_integration_branch_ahead_of_default "${integration_branch}" "${default_branch}")"; then
        _fimin_rd_ahead_rc=0
      else
        _fimin_rd_ahead_rc=$?
      fi
      if [ "${_fimin_rd_ahead_rc}" -ne 0 ]; then
        echo "::warning::  [final-merge] Recorded final PR #${final_pr} is closed+merged but the compare API failed during the ahead_by re-check; failing closed and clearing the recorded PR so the next code path can open a fresh one if integration has drifted."
        jq '.final_merge_pr = null | .final_merge_status = "pending" | .final_merge_error = "compare API error during ahead_by re-check after recorded final PR was already merged (failed closed)"' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        final_pr=""
        post_state_comment || true
      elif [ "${_fimin_rd_ahead_by:-0}" != "0" ]; then
        echo "  [final-merge] Recorded final PR #${final_pr} is closed+merged but integration branch '${integration_branch}' is ahead of '${default_branch}' by ${_fimin_rd_ahead_by} commit(s); clearing the recorded PR and falling through to open a fresh final PR for the new diff."
        jq --arg n "${_fimin_rd_ahead_by}" --arg ib "${integration_branch}" --arg db "${default_branch}" \
          '.final_merge_pr = null
           | .final_merge_status = "pending"
           | .final_merge_error = ("integration branch " + $ib + " was ahead of " + $db + " by " + $n + " commit(s) after recorded final PR merged; reopening for the new diff")' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        final_pr=""
        post_state_comment || true
      else
        jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged" | .final_merge_error = ""' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
		mark_integration_branch_squash_fresh
        post_state_comment || true
        return 0
      fi
    fi
  fi

	if ! integration_branch_exists "${integration_branch}"; then
		FINAL_MERGE_BUDGET_ELIGIBLE="0"
		mark_integration_branch_missing_failed "${integration_branch}"
		return 1
	fi

  if [ -z "${final_pr}" ] || [ "${final_pr}" = "null" ]; then
    final_pr="$(ensure_eager_final_pr "${integration_branch}" "${default_branch}" "${project_title}")"
  fi

  if [ -z "${final_pr}" ]; then
    jq --arg err "Unable to create or locate the final integration PR from ${integration_branch} to ${default_branch}." '.final_merge_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_tracking_comment "## ⚠️ Final merge could not start

Unable to create or locate the final integration PR from \`${integration_branch}\` to \`${default_branch}\`."
    return 1
  fi

  jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "pending" | .final_merge_error = ""' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment || true

  local pr_json
  local pr_state
  local pr_mergeable
  local pr_merged
  local pr_draft
  local ready_gate_reason=""
  pr_json="$(_fetch_pr_json "${final_pr}")"
  pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
  pr_mergeable="$(_jq_field "${pr_json}" '.mergeable' 'true|false')"
  pr_merged="$(_jq_field "${pr_json}" '.merged_at != null' 'true|false')"
  pr_draft="$(_jq_field "${pr_json}" '.draft' 'true|false')"
  [ -n "${pr_merged}" ] || pr_merged="false"
  [ -n "${pr_draft}" ] || pr_draft="false"

  if [ "${pr_state}" = "closed" ] && [ "${pr_merged}" = "true" ]; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged" | .final_merge_error = ""' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
	mark_integration_branch_squash_fresh
    post_state_comment || true
    return 0
  fi

  if has_label "${TRACKING_LABELS:-[]}" "ai:ready-to-merge"; then
    ready_gate_reason="tracking-ready-to-merge"
  elif has_label "${TRACKING_LABELS:-[]}" "ai:validated"; then
	validation_history_gate_json="$(validation_history_gate_decision_for_current_sha)"
	validation_history_gate_reason="$(printf '%s' "${validation_history_gate_json}" | jq -r '.reason // ""' 2>/dev/null || echo '')"
	validation_history_gate_sha="$(printf '%s' "${validation_history_gate_json}" | jq -r '.integration_sha // ""' 2>/dev/null || echo '')"
	if [ "$(printf '%s' "${validation_history_gate_json}" | jq -r '.available // false' 2>/dev/null || echo false)" != "true" ]; then
		echo "  [final-merge] Validation history unavailable for integration SHA ${validation_history_gate_sha:-unknown}; falling back to legacy ai:validated gate (reason=${validation_history_gate_reason:-unknown})."
		ready_gate_reason="tracking-validated-legacy"
	elif [ "$(printf '%s' "${validation_history_gate_json}" | jq -r '.allow // false' 2>/dev/null || echo false)" = "true" ]; then
		ready_gate_reason="tracking-validated-legacy"
	else
		validation_history_gate_wait_message="$(validation_history_gate_next_action "${validation_history_gate_reason}" "${validation_history_gate_sha}")"
	fi
  elif [ "${ENABLE_VALIDATION}" != "true" ]; then
    ready_gate_reason="validation-disabled-legacy"
  fi

  if [ "${pr_state}" = "open" ] && [ -z "${ready_gate_reason}" ]; then
    FINAL_MERGE_BUDGET_ELIGIBLE="0"
	if [ -n "${validation_history_gate_wait_message}" ]; then
		echo "  [final-merge] PR #${final_pr} is blocked by validation history for integration SHA ${validation_history_gate_sha:-unknown} (reason=${validation_history_gate_reason:-unknown})."
		update_eager_pr_validation_status_section "${final_pr}" "${validation_history_gate_wait_message}" || true
	elif [ "${pr_draft}" = "true" ]; then
      echo "  [final-merge] PR #${final_pr} is still draft; waiting for the tracking issue readiness gate."
    else
      echo "  [final-merge] PR #${final_pr} is open but not yet allowed to merge; waiting for the tracking issue readiness gate."
    fi
	if [ -z "${validation_history_gate_wait_message}" ]; then
		update_eager_pr_validation_status_section "${final_pr}" || true
	fi
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_draft}" = "true" ]; then

    if ! gh_retry gh pr ready "${final_pr}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
      FINAL_MERGE_BUDGET_ELIGIBLE="0"
      echo "::warning::  [final-merge] Unable to promote draft PR #${final_pr} via gate=${ready_gate_reason}; will retry next poll." >&2
      return 1
    fi
    echo "EAGER_DRAFT_PR_PROMOTED pr=${final_pr} gate=${ready_gate_reason} tracking_issue=${TRACKING_NUM}"
    update_eager_pr_validation_status_section "${final_pr}" || true
    pr_json="$(_fetch_pr_json "${final_pr}")"
    pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
    pr_mergeable="$(_jq_field "${pr_json}" '.mergeable' 'true|false')"
    pr_merged="$(_jq_field "${pr_json}" '.merged_at != null' 'true|false')"
    pr_draft="$(_jq_field "${pr_json}" '.draft' 'true|false')"
    [ -n "${pr_merged}" ] || pr_merged="false"
    [ -n "${pr_draft}" ] || pr_draft="false"
  fi

  # Mergeability gate: if the final PR is not mergeable, hand off to
  # the self-healing flow and defer finalize to the next tick. This is
  # the primary fix for the #832-style stall: previously this code path
  # set final_merge_status=conflict and halted with no recovery.
  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" = "false" ]; then
    FINAL_MERGE_BUDGET_ELIGIBLE="0"
    echo "  [final-merge] PR #${final_pr} is not mergeable; invoking self-healing flow."
    heal_integration_branch_conflict "${integration_branch}" "${default_branch}" "${project_title}" "final PR #${final_pr} mergeable=false" || true
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" != "true" ]; then
    FINAL_MERGE_BUDGET_ELIGIBLE="0"
    echo "  [final-merge] PR #${final_pr} mergeability is '${pr_mergeable:-unknown}'. Will retry next poll."
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" = "true" ] && ! _pr_checks_completed "${final_pr}" "" "${default_branch}"; then
    FINAL_MERGE_BUDGET_ELIGIBLE="0"
    echo "  [final-merge] Required checks not complete for PR #${final_pr}. Will retry next poll."
    return 1
  fi

  local merge_err=""
  if merge_err="$(gh_retry gh pr merge "${final_pr}" --repo "${GITHUB_REPOSITORY}" --squash --delete-branch 2>&1 >/dev/null)"; then
    jq --argjson final_pr "${final_pr}" \
      '.final_merge_pr = $final_pr |
       .final_merge_status = "merged" |
       .final_merge_error = "" |
       .integration_sync_status = "clean" |
       .integration_sync_last_error = "" |
       .integration_conflict_unresolved_ticks = 0' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
	mark_integration_branch_squash_fresh
    post_state_comment || true
    post_tracking_comment "## ✅ Final merge complete

Integration branch \`${integration_branch}\` was squash-merged into \`${default_branch}\` via PR #${final_pr}."
    return 0
  fi

  pr_json="$(_fetch_pr_json "${final_pr}")"
  pr_state="$(_jq_field "${pr_json}" '.state' 'open|closed|merged')"
  pr_mergeable="$(_jq_field "${pr_json}" '.mergeable' 'true|false')"
  pr_merged="$(_jq_field "${pr_json}" '.merged_at != null' 'true|false')"
  [ -n "${pr_merged}" ] || pr_merged="false"

  if [ "${pr_state}" = "closed" ] && [ "${pr_merged}" = "true" ]; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged" | .final_merge_error = ""' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
	mark_integration_branch_squash_fresh
    post_state_comment || true
    return 0
  fi

  if [ -n "${merge_err}" ]; then
    merge_err="$(printf '%s' "${merge_err}" | head -c 5000)"
    jq --arg err "${merge_err}" '.final_merge_error = $err' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  fi

  # Post-merge-attempt conflict path: squash merge was rejected by
  # GitHub despite our pre-merge mergeability check (race with a push
  # to default). Hand off to the healing flow instead of halting.
  if [ "${pr_mergeable}" = "false" ]; then
    FINAL_MERGE_BUDGET_ELIGIBLE="0"
    echo "  [final-merge] Post-attempt mergeability=false on PR #${final_pr}; invoking self-healing flow."
    heal_integration_branch_conflict "${integration_branch}" "${default_branch}" "${project_title}" "final PR #${final_pr} became unmergeable during merge" || true
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" != "true" ]; then
    FINAL_MERGE_BUDGET_ELIGIBLE="0"
    echo "  [final-merge] PR #${final_pr} mergeability is '${pr_mergeable:-unknown}' after merge attempt. Will retry next poll."
    return 1
  fi

  jq --arg err "Final PR #${final_pr} could not be merged automatically (state=${pr_state:-unknown}, mergeable=${pr_mergeable:-unknown})." '.final_merge_error = $err' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_tracking_comment "## ⚠️ Final merge blocked

Final PR #${final_pr} could not be merged automatically. Review branch protections/checks and merge manually if needed."
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

extract_comprehensive_release_metadata() {
  local comments_json="$1"

  if ! echo "${comments_json}" | jq -rc '
    [ .[] | ((.body // "") | gsub("\\\\n"; "\n")) ]
    | reverse as $bodies
    | (
        [ $bodies[]
          | select(test("<!--[[:space:]]*COMPREHENSIVE_RELEASE_METADATA_V1[[:space:]]*-->"))
          | (
              .
              | (
                  capture("(?s)<!--[[:space:]]*COMPREHENSIVE_RELEASE_METADATA_V1[[:space:]]*-->(?<block>.*?)(?:<!--[[:space:]]*/COMPREHENSIVE_RELEASE_METADATA_V1[[:space:]]*-->|\\z)")?
                  | .block
                )
              // ""
            )
          | select(type == "string" and length > 0)
        ]
        | .[0] // ""
      ) as $metadata_block
    | {
        version_tag: (
          [ ($metadata_block | split("\n")[])
            | (capture("(?i)^[[:space:]]*(?:[-*][[:space:]]*)?(?:version[ _-]?tag)[[:space:]]*[:=][[:space:]]*`?(?<value>[^`]+?)`?[[:space:]]*$") | .value)?
            | select(type == "string" and length > 0)
          ]
          | .[0] // ""
        ),
        test_repo: (
          [ ($metadata_block | split("\n")[])
            | (capture("(?i)^[[:space:]]*(?:[-*][[:space:]]*)?(?:test[ _-]?repo)[[:space:]]*[:=][[:space:]]*`?(?<value>[^`]+?)`?[[:space:]]*$") | .value)?
            | select(type == "string" and length > 0)
          ]
          | .[0] // ""
        )
      }
  ' 2>/dev/null; then
    echo "::warning::Failed to extract comprehensive release metadata from tracking comments; continuing with defaults." >&2
    echo '{"version_tag":"","test_repo":""}'
  fi
}

dispatch_comprehensive_release_workflow() {
  local version_tag="${1:-}"
  local test_repo="${2:-}"
  local run_args=("test-and-mark-stable.yml" "--repo" "${GITHUB_REPOSITORY}" "--ref" "stable" "-f" "dry_run=false")

  if [ -n "${version_tag}" ]; then
    run_args+=("-f" "version_tag=${version_tag}")
  fi
  if [ -n "${test_repo}" ]; then
    run_args+=("-f" "test_repo=${test_repo}")
  fi

  local _dispatch_err
  _dispatch_err="$(mktemp)"
  if gh_retry gh workflow run "${run_args[@]}" >/dev/null 2>"${_dispatch_err}"; then
    COMPREHENSIVE_RELEASE_DISPATCH_ERROR=""
    rm -f "${_dispatch_err}"
    return 0
  fi

  COMPREHENSIVE_RELEASE_DISPATCH_ERROR="$(cat "${_dispatch_err}" 2>/dev/null || true)"
  rm -f "${_dispatch_err}"
  return 1
}

handle_comprehensive_release_callback_if_needed() {
  local project_status="$1"
  local tracking_labels="$2"
  local comments_json="$3"
  local callback_handled

  if ! has_label "${tracking_labels}" "ai:comprehensive-test-pending"; then
    return 0
  fi

  callback_handled="$(jq -r '.comprehensive_release_callback.handled // false' "${STATE_FILE}" 2>/dev/null || echo "false")"
  if [ "${callback_handled}" = "true" ]; then
    echo "Comprehensive release callback already handled for project #${TRACKING_NUM}; skipping dispatch."
  elif [ "${project_status}" = "complete" ]; then
    local metadata_json
    local version_tag
    local test_repo
    local msg

    metadata_json="$(extract_comprehensive_release_metadata "${comments_json}")"
    version_tag="$(echo "${metadata_json}" | jq -r '.version_tag // ""' 2>/dev/null || echo "")"
    test_repo="$(echo "${metadata_json}" | jq -r '.test_repo // ""' 2>/dev/null || echo "")"

    if ! [[ "${version_tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      version_tag=""
    fi

    if ! [[ "${test_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      test_repo=""
    fi

    if dispatch_comprehensive_release_workflow "${version_tag}" "${test_repo}"; then
      msg="Comprehensive release callback dispatched for project #${TRACKING_NUM}."
      msg+=$'\n'"Workflow: test-and-mark-stable.yml"
      msg+=$'\n'"dry_run: false"
      if [ -n "${version_tag}" ]; then
        msg+=$'\n'"version_tag: ${version_tag}"
      fi
      if [ -n "${test_repo}" ]; then
        msg+=$'\n'"test_repo: ${test_repo}"
      fi
      tg_notify "${msg}" "DEBUG"

      jq --arg status "${project_status}" --arg handled_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '.comprehensive_release_callback = {handled: true, status: $status, handled_at: $handled_at}' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment || true
    else
      msg="Comprehensive release callback failed for project #${TRACKING_NUM}."
      msg+=$'\n'"Workflow: test-and-mark-stable.yml"
      msg+=$'\n'"dry_run: false"
      if [ -n "${COMPREHENSIVE_RELEASE_DISPATCH_ERROR:-}" ]; then
        msg+=$'\n'"Error: ${COMPREHENSIVE_RELEASE_DISPATCH_ERROR}"
      fi
      tg_notify "${msg}" "CRITICAL"
      return 0
    fi
  elif [ "${project_status}" = "failed" ] || [ "${project_status}" = "validation-failed" ]; then
    tg_notify "Comprehensive pipeline aborted for project #${TRACKING_NUM} (status: ${project_status}). Release workflow not dispatched." "CRITICAL"

    jq --arg status "${project_status}" --arg handled_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '.comprehensive_release_callback = {handled: true, status: $status, handled_at: $handled_at}' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
  else
    echo "::warning::Skipping comprehensive release callback for unexpected project status '${project_status}'."
    return 0
  fi

  if gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:comprehensive-test-pending" >/dev/null 2>&1; then
    TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"
  else
    echo "::warning::Failed to remove ai:comprehensive-test-pending from tracking issue #${TRACKING_NUM}."
  fi
}

# Check whether the validation workflow (ai-validate.yml or internal-validate.yml)
# has any currently active (in_progress or queued) runs.  Used to avoid
# redispatching when a previous dispatch is still executing.
has_active_validation_run() {
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  local active_count

  # Single-attempt probe (no gh_retry): the primary wf_name is almost
  # always absent in this repo — the default `ai-validate.yml` does
  # not exist here (only `validate.yml` / `internal-validate.yml`
  # ship in-tree) — so wrapping with gh_retry would burn ~31 s of
  # exponential backoff on a 404 every single poll cycle before
  # falling through to the internal-validate fallback path below.
  # _safe_gh_jq captures stdout to a tempfile and emits empty on
  # non-2xx, so the `|| echo '0'` fallback stays clean.  Rate-limit
  # (403/429) during this probe degrades to a one-cycle miss, which
  # is acceptable because the poll cycle retries on the next tick
  # and the actual dispatch/mutation calls still go through
  # gh_retry.  (Copilot review on PR #1044.)
  active_count="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/workflows/${wf_name}/runs?per_page=5" \
    --jq '[.workflow_runs[] | select(.status == "in_progress" or .status == "queued")] | length' || echo '0')"
  if [ "${active_count}" -gt 0 ]; then
    return 0
  fi

  # Fallback: check internal-validate.yml if primary name differs.
  # Same single-attempt rationale — on this repo the fallback is the
  # one that actually resolves, so we want it to fire fast without
  # retry backoff on the preceding 404.
  if [ "${wf_name}" != "internal-validate.yml" ]; then
    active_count="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/workflows/internal-validate.yml/runs?per_page=5" \
      --jq '[.workflow_runs[] | select(.status == "in_progress" or .status == "queued")] | length' || echo '0')"
    if [ "${active_count}" -gt 0 ]; then
      return 0
    fi
  fi

  return 1
}

# Return a JSON object describing the most recent *completed* validation
# workflow run that was created on or after the last dispatch timestamp
# recorded in state. Fields: run_id, run_attempt, conclusion, raw_status,
# run_url, run_timestamp.
#
# `raw_status` is sourced from `gh run view --json jobs,conclusion,outputs`
# when the selected run exposes an id. The workflow-run list endpoint is still
# the source of truth for freshness filtering so the poller does not read a
# stale validation result from an earlier cycle.
get_last_validation_run_info() {
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  local last_dispatch_ts
  last_dispatch_ts="$(jq -r '.validation_last_dispatch_ts // 0' "${STATE_FILE}")"

  # Single-attempt probe (no gh_retry): same 404-hot-loop concern as
  # has_active_validation_run above — the default wf_name
  # (`ai-validate.yml`) is absent in this repo, so gh_retry would
  # burn ~31 s per poll cycle before the internal-validate fallback.
  # See has_active_validation_run for the full rationale.
  # (Copilot review on PR #1044.)
  local runs_json
  runs_json="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/workflows/${wf_name}/runs?status=completed&per_page=5" \
    --jq '.workflow_runs' || echo '[]')"
  [ -n "${runs_json}" ] || runs_json='[]'

  # Fallback to internal-validate.yml if no completed runs found
  if [ "$(echo "${runs_json}" | jq 'length')" -eq 0 ] && [ "${wf_name}" != "internal-validate.yml" ]; then
    runs_json="$(_safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/workflows/internal-validate.yml/runs?status=completed&per_page=5" \
      --jq '.workflow_runs' || echo '[]')"
    [ -n "${runs_json}" ] || runs_json='[]'
  fi

  # Select the most recent run created after our last dispatch timestamp
  local selected_run
  selected_run="$(echo "${runs_json}" | jq -c --argjson ts "${last_dispatch_ts}" '
    [.[]
      | . + {
          _created_ts: (((.created_at // "") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601?) // 0)
        }
      | select(._created_ts >= $ts)
    ]
    | sort_by(.created_at // "")
    | last // {}
  ' 2>/dev/null || echo '{}')"
  if [ -z "${selected_run}" ] || [ "${selected_run}" = "null" ] || [ "${selected_run}" = "{}" ]; then
    echo '{"run_id":"","run_attempt":0,"conclusion":"","raw_status":"","run_url":"","run_timestamp":""}'
    return 0
  fi

  local run_id
  local run_attempt
  local conclusion
  local run_url
  local run_timestamp
  local raw_status=""
  local run_view_json='{}'
  run_id="$(printf '%s' "${selected_run}" | jq -r '(.id // .databaseId // "") | tostring' 2>/dev/null || echo '')"
  run_attempt="$(printf '%s' "${selected_run}" | jq -r '(.run_attempt // 0) | tonumber? // 0' 2>/dev/null || echo '0')"
  conclusion="$(printf '%s' "${selected_run}" | jq -r '.conclusion // ""' 2>/dev/null || echo '')"
  run_url="$(printf '%s' "${selected_run}" | jq -r '.html_url // ""' 2>/dev/null || echo '')"
  run_timestamp="$(printf '%s' "${selected_run}" | jq -r '.updated_at // .created_at // ""' 2>/dev/null || echo '')"

  if [[ "${run_id}" =~ ^[0-9]+$ ]] && [ "${conclusion}" != "success" ]; then
    if run_view_json="$(gh_retry gh run view "${run_id}" --repo "${GITHUB_REPOSITORY}" --json jobs,conclusion,outputs 2>/dev/null)"; then
      if [ -n "${run_view_json}" ] && [ "${run_view_json}" != "null" ]; then
        conclusion="$(printf '%s' "${run_view_json}" | jq -r '.conclusion // empty' 2>/dev/null || echo "${conclusion}")"
        raw_status="$(printf '%s' "${run_view_json}" | jq -r '((.outputs // {}) | .raw_status // .["raw_status"] // "")' 2>/dev/null || echo '')"
      fi
      if [ "${conclusion}" != "success" ] && [ -z "${raw_status}" ]; then
        echo "::warning::validation_raw_status_fallback helper=get_last_validation_run_info reason=missing_outputs run_id=${run_id} conclusion=${conclusion}" >&2
      fi
    else
      echo "::warning::validation_raw_status_fallback helper=get_last_validation_run_info reason=run_view_failed run_id=${run_id} conclusion=${conclusion}" >&2
    fi
  fi

  printf '%s' "${selected_run}" | jq -c \
    --arg run_id "${run_id}" \
    --argjson run_attempt "${run_attempt}" \
    --arg conclusion "${conclusion}" \
    --arg raw_status "${raw_status}" \
    --arg run_url "${run_url}" \
    --arg run_timestamp "${run_timestamp}" '
      {
        run_id: $run_id,
        run_attempt: $run_attempt,
        conclusion: (if $conclusion == "" then (.conclusion // "") else $conclusion end),
        raw_status: $raw_status,
        run_url: $run_url,
        run_timestamp: $run_timestamp
      }
    ' 2>/dev/null || echo '{"run_id":"","run_attempt":0,"conclusion":"","raw_status":"","run_url":"","run_timestamp":""}'
}

# Return the conclusion of the most recent *completed* validation workflow run
# that was created on or after the last dispatch timestamp recorded in state.
# Used as a fallback when the ai:validated / ai:validation-failed label is
# missing despite the workflow having completed successfully.
get_last_validation_run_conclusion() {
  local run_info
  run_info="$(get_last_validation_run_info)"
  printf '%s' "${run_info}" | jq -r '.conclusion // ""' 2>/dev/null || echo ""
}

infer_validation_raw_status() {
  local reason="$1"

  if [ -n "${LAST_VAL_RAW_STATUS:-}" ] && [ "${LAST_VAL_RAW_STATUS}" != "null" ]; then
    echo "${LAST_VAL_RAW_STATUS}"
    return 0
  fi

  if printf '%s' "${reason}" | grep -qiE 'raw_status["=:[:space:]]*harness_error|(^|[[:space:][:punct:]])harness_error($|[[:space:][:punct:]])|Runtime validation harness error|Validation failed due to harness error|Validation harness generation failed|Validation harness tracking violation|Runtime validation harness (generation|pre-flight|tracking)[[:space:]-]*(failed|violation)|harness pre-flight error'; then
    echo "harness_error"
    return 0
  fi

  if printf '%s' "${reason}" | grep -qi 'Runtime validation found fixable issues'; then
    echo "needs_fixes"
    return 0
  fi

  echo ""
}

validation_reason_one_line() {
  local reason="$1"
  printf '%s' "${reason}" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-200
}

dispatch_validation_if_needed() {
  local validation_cycle="$1"
  local integration_branch
  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  local last_dispatch_cycle
  local last_dispatch_ts
  local now_epoch
  local stale_threshold_secs=3600  # 1 hour: if no label appears after dispatch, allow redispatch
  local validation_dispatch_safe_despite_failures

  # Defensive preflight: refuse to dispatch validate while the current
  # wave's PRs are not all merged into the integration branch
  # (WAVE_COMPLETE != true). This is belt-and-suspenders against the rare
  # case where a wave PR transitions from merged back to a non-terminal
  # state between the judge call and the dispatch (e.g. a consumer-side
  # revert or label-reconciliation race): that regression drives
  # all_merged=false -> WAVE_COMPLETE=false, which the gate below catches.
  # When it happens we skip dispatch this cycle and let the next poll tick
  # re-evaluate once wave PR state settles.
  #
  # Do NOT blanket-gate on ANY_FAILED. ANY_FAILED is broad: it is set for
  # a wave issue legitimately closed WITHOUT a merged PR — e.g. a
  # judge-fix-up whose premise turned out false, so no code change was
  # needed. Such a "closed" issue keeps all_merged=true (so
  # WAVE_COMPLETE=true) while setting ANY_FAILED=true, so a blanket
  # ANY_FAILED gate defers dispatch on every poll cycle and permanently
  # deadlocks the project in ai:validating: validation never dispatches ->
  # never earns ai:validated -> integration never merges -> tracking issue
  # never closes.
  #
  # But ANY_FAILED still covers explicit failure phases such as
  # ai:plan-failed / ai:implement-diagnose-failed and the dedicated
  # ai:implementation-failed status. Those must continue to block
  # validation dispatch even when they reconcile to WAVE_COMPLETE=true.
  # check-wave-status therefore emits
  # validation_dispatch_safe_despite_failures=true only when every failed
  # issue is an adjudicated closed-without-merge case (live issue closed or
  # ai:closed, with no blocking terminal-failure phase). The gate below uses
  # that finer signal instead of the coarse ANY_FAILED boolean.
  # Real-world repro: hylifegroup.com#3 wedged for 10 days on a wave issue
  # closed ai:closed with no PR (judge-fix-up that needed no code change).
  #
  # Gate on WAVE_COMPLETE only, NOT on PROJECT_COMPLETE (and deliberately
  # not on ANY_FAILED, per the paragraph above). Validation
  # runs against the integration branch (ref=integration_branch in
  # dispatch_validation_workflow below), so the integration→default merge
  # is deliberately NOT a precondition for dispatching validation — that
  # merge is performed afterward by mark_validation_complete →
  # finalize_integration_merge_if_needed once the run earns the
  # ai:validated label. PROJECT_COMPLETE additionally folds in
  # integration_contained_in_default (ahead_by==0); gating on it here
  # deadlocks any project that uses a separate integration branch: ahead_by
  # stays > 0 until the final merge lands, but that merge waits for
  # ai:validated, and ai:validated waits for a validation run that this
  # gate would never dispatch (validation needs the merge, the merge needs
  # validation). Default-branch-only projects never hit it because
  # ahead_by ≡ 0. This is the validation-dispatch sibling of the judge
  # hard-guard fix for bitsafe.io#325 (see the "Hard guard: judge cannot
  # declare complete while waves remain" comment in the judge-verdict
  # handler), which removed the same over-broad ahead_by==0 gate there.
  #
  # The judge-complete path reaches this helper after the main wave-status
  # block has already set WAVE_COMPLETE / ANY_FAILED; validating /
  # revalidate paths hit it earlier in the loop, so recompute the live gate
  # on demand there and fail closed if the probe itself cannot run.
  if [ -z "${WAVE_COMPLETE+set}" ] || [ -z "${ANY_FAILED+set}" ]; then
    if ! refresh_validation_dispatch_wave_gate; then
      echo "::warning::[validation-dispatch] unable to recompute wave-merge gate for issue #${TRACKING_NUM:-?}; deferring validate dispatch this cycle." >&2
      return 0
    fi
  fi
  validation_dispatch_safe_despite_failures="${VALIDATION_DISPATCH_SAFE_DESPITE_FAILURES:-}"
  if [ -z "${validation_dispatch_safe_despite_failures}" ] && [ -n "${WAVE_STATUS:-}" ]; then
    validation_dispatch_safe_despite_failures="$(echo "${WAVE_STATUS}" | jq -r '.validation_dispatch_safe_despite_failures // false' 2>/dev/null || echo false)"
  fi
  if [ "${WAVE_COMPLETE:-false}" != "true" ]; then
    echo "Preflight: WAVE_COMPLETE=${WAVE_COMPLETE:-unset} ANY_FAILED=${ANY_FAILED:-unset}; deferring validate dispatch this cycle (wave PRs not yet all merged into the integration branch)."
    return 0
  fi
  if [ "${ANY_FAILED:-false}" = "true" ] && [ "${validation_dispatch_safe_despite_failures:-false}" != "true" ]; then
    echo "Preflight: WAVE_COMPLETE=${WAVE_COMPLETE:-unset} ANY_FAILED=${ANY_FAILED:-unset} validation_dispatch_safe_despite_failures=${validation_dispatch_safe_despite_failures:-unset}; deferring validate dispatch this cycle (wave includes failed issue statuses other than adjudicated closed-without-merge)."
    return 0
  fi

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

  if ! phase_cap_can_dispatch "ai:validating" "dispatch_validation" "${TRACKING_NUM:-validation}"; then
    return 0
  fi

  if dispatch_validation_workflow "${validation_cycle}" "${integration_branch}"; then
    phase_cap_note_dispatch "ai:validating"
    jq --argjson cycle "${validation_cycle}" --argjson ts "$(date +%s)" \
      '.validation_last_dispatch_cycle = $cycle | .validation_last_dispatch_ts = $ts' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    if [ -n "${integration_branch}" ]; then
      post_tracking_comment "## 🧪 Runtime validation dispatched

- Cycle: ${validation_cycle}
- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`
- Ref: \`${integration_branch}\`"
    else
      post_tracking_comment "## 🧪 Runtime validation dispatched

- Cycle: ${validation_cycle}
- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`"
    fi
    tg_notify "🧪 Validation dispatched for project #${TRACKING_NUM} (cycle ${validation_cycle})." "DEBUG"
    return 0
  fi

  return 1
}

mark_validation_failed() {
  local reason="$1"
  local _tracking_labels
  local validation_raw_status
  local validation_cycle=""
  local validation_history_reason=""
  local harness_reason_one_line

  # Check validation recovery budget before going terminal
  local val_recovery_count
  val_recovery_count="$(jq -r '.validation_recovery_count // 0' "${STATE_FILE}")"
  if ! [[ "${val_recovery_count}" =~ ^[0-9]+$ ]]; then
    val_recovery_count="0"
  fi

	validation_raw_status="$(infer_validation_raw_status "${reason}")"
	validation_cycle="$(jq -r '.validation_cycle // 1' "${STATE_FILE}" 2>/dev/null || echo 1)"
	validation_history_reason="$(validation_reason_one_line "${reason}")"
	append_validation_history_for_current_sha \
		"failed" \
		"${validation_raw_status}" \
		"${LAST_VAL_CONCLUSION:-failure}" \
		"${validation_cycle}" \
		"${validation_history_reason}" \
		"orchestrate_poll.mark_validation_failed"
	if [ "${validation_raw_status}" = "harness_error" ]; then
		harness_reason_one_line="$(validation_reason_one_line "${reason}")"
		echo "HARNESS_ERROR_DETECTED reason=${harness_reason_one_line}"

    jq --arg reason "${reason}" --arg raw_status "${validation_raw_status}" '
      .status = "failed" |
      .validation_failure_reason = $reason |
      .validation_failure_class = null |
      .validation_last_raw_status = $raw_status |
      .validation_active_fix_issues = [] |
      .validation_fix_issues_batch_cycles = 0 |
      .validation_completed_cycle = null |
      .judge_last_fingerprint = "" |
      .judge_fingerprint_repeat_count = 0
    ' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    _tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"
    handle_comprehensive_release_callback_if_needed "failed" "${_tracking_labels}" "${COMMENTS:-[]}"
    set_tracking_phase_label "ai:validation-failed"
    gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:validate-failed" >/dev/null || true
    ensure_label_exists "ai:harness-broken" >/dev/null 2>&1 || true
    gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:harness-broken" >/dev/null 2>&1 || true
    post_tracking_comment "## ❌ Runtime validation harness error

${reason}

The latest validation run reported \`raw_status=harness_error\`, so the orchestrator is classifying this as a harness/infrastructure defect instead of consuming \`MAX_VALIDATION_RECOVERY_ATTEMPTS\`, \`MAX_VALIDATE_CYCLES\`, or judge repeat-fingerprint budget. Repair the harness, then run \`/revalidate\` to resume runtime validation."
    COMPLETION_STATUS_STATE_CHANGED="false"
    update_completion_status_comment "failed" \
      "## Completion status"$'\n\n'"**State:** \`failed\`"$'\n\n'"Runtime validation is blocked by a harness/infrastructure defect (\`raw_status=harness_error\`). Recovery counters were left unchanged. Repair the harness, then use \`/revalidate\` to resume validation. See the \"❌ Runtime validation harness error\" comment for the diagnostic detail." \
      || true
    if [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
      post_state_comment || true
    fi
    tg_cleanup_msgs "${TRACKING_NUM}"
    tg_notify "Project #${TRACKING_NUM} validation harness is broken (raw_status=harness_error). Recovery counters unchanged; manual repair plus /revalidate required." "CRITICAL"
    return 0
  fi

  # Deterministic-failure short-circuit: validate_process.sh embeds
  # a machine-readable marker in the failure comment body when the
  # cause is environment-deterministic (e.g. exit 17 = python3 < 3.9
  # on the runner image). Retrying the same workflow will not change
  # the outcome, so skip the recovery budget and go straight to
  # terminal failure with a clear note.
  local _deterministic_class=""
  if printf '%s' "${reason}" | grep -qE 'AI_VALIDATION_FAILURE_CLASS:deterministic_[a-z_]+'; then
    _deterministic_class="$(printf '%s' "${reason}" \
      | grep -oE 'AI_VALIDATION_FAILURE_CLASS:deterministic_[a-z_]+' \
      | head -n 1 \
      | sed 's/^AI_VALIDATION_FAILURE_CLASS://')"
    echo "Validation failed deterministically (class=${_deterministic_class}); skipping recovery budget (current=${val_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS})."
    jq --arg reason "${reason}" --arg dclass "${_deterministic_class}" --arg raw_status "${validation_raw_status}" \
      '.status = "failed" |
       .validation_failure_reason = $reason |
       .validation_failure_class = $dclass |
       .validation_last_raw_status = (if $raw_status == "" then null else $raw_status end) |
       .validation_active_fix_issues = [] |
       .validation_fix_issues_batch_cycles = 0 |
       .validation_completed_cycle = null' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    _tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"
    handle_comprehensive_release_callback_if_needed "failed" "${_tracking_labels}" "${COMMENTS:-[]}"
    set_tracking_phase_label "ai:validation-failed"
    gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:validate-failed" >/dev/null || true
    gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:harness-broken" >/dev/null 2>&1 || true
    post_tracking_comment "## ❌ Runtime validation failed (deterministic)

${reason}

Failure class \`${_deterministic_class}\` is environment-deterministic; retrying this workflow on the same runner image will not help. Skipping the recovery budget. Manual intervention required."
    COMPLETION_STATUS_STATE_CHANGED="false"
    update_completion_status_comment "failed" \
      "## Completion status"$'\n\n'"**State:** \`failed\`"$'\n\n'"Runtime validation failed deterministically (class \`${_deterministic_class}\`). Manual intervention required. See the \"❌ Runtime validation failed (deterministic)\" comment for the diagnostic detail." \
      || true
    if [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
      post_state_comment || true
    fi
    tg_cleanup_msgs "${TRACKING_NUM}"
    tg_notify "Project #${TRACKING_NUM} validation failed deterministically (class=${_deterministic_class}). Manual intervention required." "CRITICAL"
    return 0
  fi

  if [ "${val_recovery_count}" -lt "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" ]; then
    echo "Validation failed but recovery budget remains ($((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}). Transitioning back to judge."
    jq --arg reason "${reason}" --argjson count "$((val_recovery_count + 1))" --arg raw_status "${validation_raw_status}" \
      '.status = "in_progress" |
       .validation_recovery_count = $count |
       .validation_failure_reason = $reason |
       .validation_failure_class = null |
       .validation_last_raw_status = (if $raw_status == "" then null else $raw_status end) |
       .validation_active_fix_issues = [] |
       .validation_fix_issues_batch_cycles = 0 |
       .validation_cycle = 1 |
       .validation_last_dispatch_cycle = 0 |
       .validation_completed_cycle = null' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    set_tracking_phase_label "ai:validation-recovery"
    gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:validate-failed" >/dev/null || true
    gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:harness-broken" >/dev/null 2>&1 || true
    post_tracking_comment "## 🔄 Validation failed — recovery attempt $((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}

${reason}

Transitioning back to judge for re-evaluation."
    COMPLETION_STATUS_STATE_CHANGED="false"
    update_completion_status_comment "in-progress" \
      "## Completion status"$'\n\n'"**State:** \`in-progress\`"$'\n\n'"Runtime validation failed and recovery attempt $((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS} is in progress. Waiting for judge re-evaluation before validation can resume." \
      || true
    if [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
      post_state_comment || true
    fi
    tg_notify "Validation recovery ($((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}) for #${TRACKING_NUM}: transitioning back to judge." "WARNING"
    return 0
  fi

  # Recovery budget exhausted — terminal failure
  jq --arg reason "${reason}" --arg raw_status "${validation_raw_status}" '.status = "failed" | .validation_failure_reason = $reason | .validation_failure_class = null | .validation_last_raw_status = (if $raw_status == "" then null else $raw_status end) | .validation_active_fix_issues = [] | .validation_fix_issues_batch_cycles = 0 | .validation_completed_cycle = null' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment || true
  _tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"
  handle_comprehensive_release_callback_if_needed "failed" "${_tracking_labels}" "${COMMENTS:-[]}"
  set_tracking_phase_label "ai:validation-failed"
  gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:validate-failed" >/dev/null || true
  gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:harness-broken" >/dev/null 2>&1 || true
  post_tracking_comment "## ❌ Runtime validation failed

${reason}

Validation recovery exhausted (${val_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS}). Manual intervention required."
  COMPLETION_STATUS_STATE_CHANGED="false"
  update_completion_status_comment "failed" \
    "## Completion status"$'\n\n'"**State:** \`failed\`"$'\n\n'"Runtime validation failed after ${val_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS} recovery attempt(s). Manual intervention required. See the \"❌ Runtime validation failed\" comment for the diagnostic detail." \
    || true
  if [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
    post_state_comment || true
  fi
  tg_cleanup_msgs "${TRACKING_NUM}"
  tg_notify "Project #${TRACKING_NUM} validation failed after ${val_recovery_count} recovery attempt(s). Manual intervention required." "CRITICAL"
}

mark_validation_complete() {
  local validation_cycle="$1"
  local integration_branch
  local default_branch
  local project_title
  local _tracking_labels
  local merge_attempt_count
  local _final_pr
  local _final_status
  local _final_err
  local validation_history_raw_status="${LAST_VAL_RAW_STATUS:-}"
  local validation_history_conclusion="${LAST_VAL_CONCLUSION:-}"

  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  default_branch="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  project_title="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

  # Seed final_merge_attempt_count on legacy state blobs that predate this field.
  ensure_integration_conflict_state_fields
	if [ -z "${validation_history_raw_status}" ] || [ "${validation_history_raw_status}" = "null" ]; then
		validation_history_raw_status="pass"
	fi
	if [ -z "${validation_history_conclusion}" ] || [ "${validation_history_conclusion}" = "null" ]; then
		validation_history_conclusion="success"
	fi
	append_validation_history_for_current_sha \
		"passed" \
		"${validation_history_raw_status}" \
		"${validation_history_conclusion}" \
		"${validation_cycle}" \
		"validation passed" \
		"orchestrate_poll.mark_validation_complete"

  if ! finalize_integration_merge_if_needed "${integration_branch}" "${default_branch}" "${project_title}"; then
    # Budget-ineligible final-merge deferrals/failures should return without
    # consuming the bounded retry budget. Layer 2: check whether the
    # deferral has persisted long enough on the same head SHA to warrant
    # a CRITICAL alert (silent-loop guard for stuck advisory checks).
    if [ "${FINAL_MERGE_BUDGET_ELIGIBLE:-1}" != "1" ]; then
      echo "  [final-merge] budget-ineligible deferral/failure; retry budget unchanged."
      _check_final_merge_ineligibility_alert || true
      return 0
    fi

    # Per the validation-gate contract (README §14): a project must NOT
    # advance to status=complete until the integration branch is squash-
    # merged into the default branch. Count this failed attempt, defer
    # to the next poll tick until the budget is exhausted, then escalate
    # to ai:blocked for human intervention instead of silently advancing.
    merge_attempt_count="$(jq -r '.final_merge_attempt_count // 0' "${STATE_FILE}")"
    if ! [[ "${merge_attempt_count}" =~ ^[0-9]+$ ]]; then
      merge_attempt_count="0"
    fi
    merge_attempt_count=$((merge_attempt_count + 1))
    jq --argjson n "${merge_attempt_count}" '.final_merge_attempt_count = $n' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

    if [ "${merge_attempt_count}" -lt "${MAX_FINAL_MERGE_ATTEMPTS}" ]; then
      echo "  [final-merge] attempt ${merge_attempt_count}/${MAX_FINAL_MERGE_ATTEMPTS} did not land; deferring completion to next poll tick."
      post_state_comment || true
      tg_notify "Project #${TRACKING_NUM}: final integration→${default_branch} merge attempt ${merge_attempt_count}/${MAX_FINAL_MERGE_ATTEMPTS} did not land; will retry next tick." "WARNING"
      return 0
    fi

    # Budget exhausted — refuse to mark complete and escalate to a human.
    _final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
    _final_err="$(jq -r '.final_merge_error // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
    jq '.status = "failed"
        | .final_merge_status = "failed"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    _final_status="failed"
    post_state_comment || true
    _tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"
    handle_comprehensive_release_callback_if_needed "failed" "${_tracking_labels}" "${COMMENTS:-[]}"
    set_tracking_phase_label "ai:blocked"
    post_tracking_comment "## ❌ Final integration merge could not complete

Runtime validation passed, but the final squash merge of \`${integration_branch}\` into \`${default_branch}\` did not land after ${merge_attempt_count}/${MAX_FINAL_MERGE_ATTEMPTS} attempts.

- Final PR: ${_final_pr:-unknown}
- Final merge status: ${_final_status}
- Last recorded error: ${_final_err:-No specific error recorded; check final PR for branch protection or required-check failures.}

Manual intervention required: resolve the blocking condition on the final PR (merge conflicts, required checks, branch protections) and re-trigger the poller, or merge manually."
    set_failed_completion_status_comment \
      "Runtime validation passed, but the final squash merge of \`${integration_branch}\` into \`${default_branch}\` did not land after ${merge_attempt_count}/${MAX_FINAL_MERGE_ATTEMPTS} attempt(s). Manual intervention required. See the \"❌ Final integration merge could not complete\" comment for the diagnostic detail."
    tg_cleanup_msgs "${TRACKING_NUM}"
    tg_notify "Project #${TRACKING_NUM} blocked: validation passed but integration→${default_branch} merge did not land after ${MAX_FINAL_MERGE_ATTEMPTS} attempts. Manual intervention required." "CRITICAL"
    return 0
  fi

  # Merge landed; reset the retry counter and mark the project complete.
  # Layer 2: also clear the ineligibility tracking keys so a future stall
  # on the same project starts a fresh clock.
  jq --argjson cycle "${validation_cycle}" \
    '.status = "complete"
     | .validation_completed_cycle = $cycle
     | .validation_failure_reason = null
     | .validation_failure_class = null
     | .validation_last_raw_status = "pass"
     | .final_merge_attempt_count = 0
     | .final_merge_ineligible_blocked_at_sha = ""
     | .final_merge_ineligible_first_blocked_at_utc = 0
     | .final_merge_ineligible_alert_sent_for_sha = ""' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment || true
  _tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"
  handle_comprehensive_release_callback_if_needed "complete" "${_tracking_labels}" "${COMMENTS:-[]}"
  set_tracking_phase_label "ai:validated"
  gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:harness-broken" >/dev/null 2>&1 || true
  # Final transition for the pinned completion-status comment: project
  # is validated, all wave PRs are merged, and the integration squash
  # merge has landed in default.
  COMPLETION_STATUS_STATE_CHANGED="false"
  update_completion_status_comment "validated" \
    "## Completion status"$'\n\n'"**State:** \`validated\`"$'\n\n'"All wave PRs merged. Integration branch squash-merged into default. Runtime validation passed (cycle ${validation_cycle}). Tracking issue kept open for manual review." \
    || true
  if [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
    post_state_comment || true
  fi
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

extract_implement_fixup_blockers_from_comment() {
  local comment_body="$1"
  # Normalise literal \n sequences so marker lines are anchored to real lines.
  printf '%s' "${comment_body}" | sed 's/\\n/\n/g' | sed -n '/^<!-- IMPLEMENT_FIXUP_BLOCKERS_V1$/,/^IMPLEMENT_FIXUP_BLOCKERS_V1 -->$/p' | sed '1d;$d'
}

sync_implementation_fixup_blockers() {
  local source_issue_num="$1"
  local issue_local_id="$2"
  local wave_idx="$3"
  local comments_json="$4"
  local latest_blocker_comment_json
  local blocker_comment_id
  local blocker_comment_body
  local blocker_payload
  local parsed_source_issue
  local normalized_fixups
  local existing_source_issue
  local existing_fixups

  SYNC_IMPLEMENT_FIXUP_BLOCKERS_CHANGED="false"

  if [ -z "${issue_local_id}" ] || [ "${issue_local_id}" = "null" ]; then
    return 0
  fi

  latest_blocker_comment_json="$(echo "${comments_json}" | jq -c '[.[] | select((.body // "") | contains("IMPLEMENT_FIXUP_BLOCKERS_V1"))] | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)]) // empty')"
  if [ -z "${latest_blocker_comment_json}" ]; then
    return 0
  fi

  blocker_comment_id="$(echo "${latest_blocker_comment_json}" | jq -r '.id // 0')"
  if ! [[ "${blocker_comment_id}" =~ ^[0-9]+$ ]]; then
    return 0
  fi

  blocker_comment_body="$(echo "${latest_blocker_comment_json}" | jq -r '.body // ""')"
  blocker_payload="$(extract_implement_fixup_blockers_from_comment "${blocker_comment_body}")"
  if [ -z "${blocker_payload}" ]; then
    return 0
  fi

  parsed_source_issue="$(printf '%s' "${blocker_payload}" | jq -r '.blocks_source_issue // empty' 2>/dev/null || echo "")"
  if [ -z "${parsed_source_issue}" ] || ! [[ "${parsed_source_issue}" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  if [ "${parsed_source_issue}" != "${source_issue_num}" ]; then
    return 0
  fi

  normalized_fixups="$(printf '%s' "${blocker_payload}" | jq -c '
    if (.fixup_issue_numbers | type) == "array" then
      .fixup_issue_numbers
      | map(select(type == "number"))
      | map(if floor == . then . else empty end)
      | unique
    else
      []
    end
  ' 2>/dev/null || echo '[]')"

  existing_source_issue="$(jq -r --arg local_id "${issue_local_id}" --argjson wave_idx "${wave_idx}" '
    .waves[$wave_idx].issues[] | select(.id == $local_id) | .blocks_source_issue // empty
  ' "${STATE_FILE}" 2>/dev/null | head -n1 || echo "")"
  existing_fixups="$(jq -c --arg local_id "${issue_local_id}" --argjson wave_idx "${wave_idx}" '
    .waves[$wave_idx].issues[] | select(.id == $local_id) | (.fixup_issue_numbers // [])
    | if type == "array" then map(select(type == "number")) | map(if floor == . then . else empty end) | unique else [] end
  ' "${STATE_FILE}" 2>/dev/null || echo '[]')"

  if [ "${existing_source_issue}" = "${source_issue_num}" ] && [ "${existing_fixups}" = "${normalized_fixups}" ]; then
    return 0
  fi

  jq \
    --arg local_id "${issue_local_id}" \
    --argjson wave_idx "${wave_idx}" \
    --argjson source_issue "${source_issue_num}" \
    --argjson fixups "${normalized_fixups}" \
    '(.waves[$wave_idx].issues[] | select(.id == $local_id)) |=
      (. + {
        blocks_source_issue: $source_issue,
        fixup_issue_numbers: ($fixups | if type == "array" then . else [] end)
      })' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

  SYNC_IMPLEMENT_FIXUP_BLOCKERS_CHANGED="true"

  return 0
}

sync_validation_fix_issues_from_comments() {
  local comments_json="$1"
  local latest_fix_comment_json
  local fix_comment_id
  local fix_comment_body
  local last_fix_comment_id
  local new_fix_issues_json
  local new_fix_count

  latest_fix_comment_json="$(echo "${comments_json}" | jq -c '[.[] | select((.body // "") | startswith("## 🧪 Runtime validation found fixable issues"))] | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)]) // empty')"
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
       .validation_failure_reason = null |
       .validation_failure_class = null |
       .validation_last_raw_status = "needs_fixes" |
       .validation_active_fix_issues = $active_fix_issues |
       .validation_fix_issues_batch_cycles = 0 |
       .validation_seen_fix_issues = ((.validation_seen_fix_issues // []) + $active_fix_issues | unique)' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  else
    echo "::warning::Validation fix comment ${fix_comment_id} did not include extractable issue numbers; treating as validation failure."
    mark_validation_failed "Validation workflow produced a fixable-issues comment with no extractable issue numbers (comment ${fix_comment_id})."
    return 0
  fi

  set_tracking_phase_label "ai:validation-fixing"
  gh_retry gh issue edit "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" --remove-label "ai:harness-broken" >/dev/null 2>&1 || true
  post_state_comment || true
}

# ---------------------------------------------------------------
# Stall recovery: workflow run status checks
# ---------------------------------------------------------------

declare -g _ACTIONS_RUNS_BLOB_CACHE=''
declare -g _ACTIONS_RUNS_BLOB_READY='false'
declare -g PHASE_CAPS_ENABLED='false'
declare -g PHASE_CAPS_STATUS='missing'
declare -g PHASE_CAPS_GLOBAL_MAX='-1'
declare -g PHASE_CAPS_GLOBAL_RUNNING='0'
declare -g PHASE_CAPS_MAX_BY_STATE='{}'
declare -g PHASE_CAPS_RUNNING_BY_STATE='{}'

_load_actions_runs_cached() {
  local empty_blob='{"workflow_runs":[]}'
  if [ "${_ACTIONS_RUNS_BLOB_READY}" = "true" ]; then
    printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE:-${empty_blob}}"
    return 0
  fi

  local repo="${GITHUB_REPOSITORY:-}"
  if [ -z "${repo}" ]; then
    _ACTIONS_RUNS_BLOB_CACHE="${empty_blob}"
    _ACTIONS_RUNS_BLOB_READY="true"
    printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"
    return 0
  fi

  local cache_json='{}'
  local cache_payload='{}'
  local cache_hit='false'
  local cached_runs='[]'
  local cached_etag=''
  local cached_fetched_at=''
  local cache_age_seconds='999999'
  local now_epoch
  now_epoch="$(date +%s)"

  if type _memory_enabled >/dev/null 2>&1 && _memory_enabled && [ -f "scripts/ai_memory.py" ]; then
    cache_json="$(python3 scripts/ai_memory.py actions-runs-cache get --repo "${repo}" || echo '{}')"
    cache_hit="$(printf '%s' "${cache_json}" | jq -r 'if (.ok == true and .hit == true and (.cache | type == "object")) then "true" else "false" end' 2>/dev/null || echo 'false')"
    if [ "${cache_hit}" = "true" ]; then
      cache_payload="$(printf '%s' "${cache_json}" | jq -c '.cache // {}' 2>/dev/null || echo '{}')"
      cached_runs="$(printf '%s' "${cache_payload}" | jq -c '.runs // []' 2>/dev/null || echo '[]')"
      cached_etag="$(printf '%s' "${cache_payload}" | jq -r '.etag // ""' 2>/dev/null || echo '')"
      cached_fetched_at="$(printf '%s' "${cache_payload}" | jq -r '.fetched_at // ""' 2>/dev/null || echo '')"
      if [ -n "${cached_fetched_at}" ]; then
        local cached_epoch
        cached_epoch="$(jq -nr --arg ts "${cached_fetched_at}" '$ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601' 2>/dev/null || echo '0')"
        if [[ "${cached_epoch}" =~ ^[0-9]+$ ]] && [ "${cached_epoch}" -le "${now_epoch}" ]; then
          cache_age_seconds="$(( now_epoch - cached_epoch ))"
        fi
      fi
    fi
  fi

  if [ "${cache_hit}" = "true" ] && [ "${cache_age_seconds}" -lt "${ACTIONS_RUNS_CACHE_TTL_SECONDS}" ]; then
    _ACTIONS_RUNS_BLOB_CACHE="$(jq -cn --argjson runs "${cached_runs}" '{workflow_runs: $runs}' 2>/dev/null || echo "${empty_blob}")"
    _ACTIONS_RUNS_BLOB_READY="true"
    printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"
    return 0
  fi

  local response_file
  local response_body_file
  local response_err
  local status_line=''
  local body_json=''
  local response_etag=''
	response_file="$(mktemp "${TMPDIR:-/tmp}/actions-runs-response.XXXXXX" 2>/dev/null || true)"
	response_body_file="$(mktemp "${TMPDIR:-/tmp}/actions-runs-response-body.XXXXXX" 2>/dev/null || true)"
	response_err="$(mktemp "${TMPDIR:-/tmp}/actions-runs-response-err.XXXXXX" 2>/dev/null || true)"
	if [ -z "${response_file}" ] || [ -z "${response_body_file}" ] || [ -z "${response_err}" ]; then
		echo "::warning::rate_limit_audit_fallback helper=_load_actions_runs_cached reason=mktemp_failed repo=${repo}" >&2
		if [ "${cache_hit}" = "true" ]; then
			_ACTIONS_RUNS_BLOB_CACHE="$(jq -cn --argjson runs "${cached_runs}" '{workflow_runs: $runs}' 2>/dev/null || echo "${empty_blob}")"
		else
			_ACTIONS_RUNS_BLOB_CACHE="${empty_blob}"
		fi
		_ACTIONS_RUNS_BLOB_READY="true"
		printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"
		return 0
	fi

  local -a api_cmd
  # Existing gh wrappers in this script are body-only and do not expose HTTP
  # response headers or 304 status lines. This direct `gh api -i` call is used
  # so one conditional fetch can carry If-None-Match and parse ETag metadata.
	api_cmd=(gh_retry gh api -i "repos/${repo}/actions/runs?status=in_progress&per_page=50")
  if [ -n "${cached_etag}" ]; then
    api_cmd+=(-H "If-None-Match: ${cached_etag}")
  fi

  local api_rc=0
  # Capture the real exit code via `|| api_rc=$?`: the `if ! cmd; then api_rc=$?`
  # idiom records the exit status of the negated condition (always 0), not the
  # failing fetch's, which would make the fail-open diagnostic below always
  # report api_rc=0 and unable to separate a transient failure from an HTTP/auth
  # error.
  "${api_cmd[@]}" >"${response_file}" 2>"${response_err}" || api_rc=$?

  if [ -s "${response_file}" ] && head -n 1 "${response_file}" | grep -q '^HTTP/'; then
    status_line="$(head -n 1 "${response_file}")"
    response_etag="$(awk '{ line = $0; sub(/\r$/, "", line); if (tolower(line) ~ /^etag:[[:space:]]*/) { sub(/^[^:]*:[[:space:]]*/, "", line); print line; exit } }' "${response_file}" 2>/dev/null || echo '')"
    body_json="$(awk 'f{print} /^\r?$/{f=1}' "${response_file}" 2>/dev/null)"
  else
    body_json="$(cat "${response_file}" 2>/dev/null || echo '')"
  fi

  if [ -n "${body_json}" ]; then
    printf '%s\n' "${body_json}" > "${response_body_file}"
  fi

  if printf '%s' "${status_line}" | grep -Eq ' 304( |$)'; then
    if [ "${cache_hit}" = "true" ]; then
      local ttl_put_file
      ttl_put_file="$(mktemp "${TMPDIR:-/tmp}/actions-runs-refresh.XXXXXX" 2>/dev/null || true)"
      if [ -n "${ttl_put_file}" ]; then
        jq -cn --argjson runs "${cached_runs}" '{workflow_runs: $runs}' > "${ttl_put_file}" 2>/dev/null || echo '{"workflow_runs":[]}' > "${ttl_put_file}"
        if type _memory_enabled >/dev/null 2>&1 && _memory_enabled && [ -f "scripts/ai_memory.py" ]; then
          python3 scripts/ai_memory.py actions-runs-cache put \
            --repo "${repo}" \
            --runs-file "${ttl_put_file}" \
            --etag "${response_etag:-${cached_etag}}" \
            --ttl-seconds "${ACTIONS_RUNS_CACHE_TTL_SECONDS}" >/dev/null || true
        fi
        rm -f "${ttl_put_file}"
      else
        echo "::warning::rate_limit_audit_fallback helper=_load_actions_runs_cached reason=mktemp_failed_ttl_put_file repo=${repo}" >&2
      fi
      _ACTIONS_RUNS_BLOB_CACHE="$(jq -cn --argjson runs "${cached_runs}" '{workflow_runs: $runs}' 2>/dev/null || echo "${empty_blob}")"
      _ACTIONS_RUNS_BLOB_READY="true"
      rm -f "${response_file}" "${response_body_file}" "${response_err}"
      printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"
      return 0
    fi
  fi

	if [ "${api_rc}" -eq 0 ] && [ -n "${body_json}" ] && printf '%s' "${body_json}" | jq -e 'type == "object" and (.workflow_runs | type == "array")' >/dev/null 2>&1; then
		local runs_only
		runs_only="$(printf '%s' "${body_json}" | jq -c '.workflow_runs // []' 2>/dev/null || echo '[]')"
		local queued_runs='[]'
		queued_runs="$(gh_retry _safe_gh_jq "repos/${repo}/actions/runs?status=queued&per_page=50" --jq '.workflow_runs' || echo '[]')"
		[ -n "${queued_runs}" ] || queued_runs='[]'
		# Include a small completed window so stall-judge diagnostics can inspect
		# recent review/autofix workflow outcomes (conclusions).
		local completed_runs='[]'
		completed_runs="$(gh_retry _safe_gh_jq "repos/${repo}/actions/runs?status=completed&per_page=20" --jq '.workflow_runs' || echo '[]')"
		[ -n "${completed_runs}" ] || completed_runs='[]'
		runs_only="$(jq -cn --argjson in_progress "${runs_only}" --argjson queued "${queued_runs}" --argjson completed "${completed_runs}" '($in_progress + $queued + $completed)' 2>/dev/null || echo '[]')"
		printf '%s\n' "${body_json}" | jq -c --argjson runs "${runs_only}" '.workflow_runs = $runs | .total_count = ($runs | length)' > "${response_body_file}" 2>/dev/null || true
		if [ ! -s "${response_body_file}" ]; then
			jq -cn --argjson runs "${runs_only}" '{workflow_runs: $runs}' > "${response_body_file}" 2>/dev/null || echo '{"workflow_runs":[]}' > "${response_body_file}"
		fi
	if type _memory_enabled >/dev/null 2>&1 && _memory_enabled && [ -f "scripts/ai_memory.py" ]; then
		python3 scripts/ai_memory.py actions-runs-cache put \
			--repo "${repo}" \
			--runs-file "${response_body_file}" \
	        --etag "${response_etag}" \
	        --ttl-seconds "${ACTIONS_RUNS_CACHE_TTL_SECONDS}" >/dev/null || true
	fi
		_ACTIONS_RUNS_BLOB_CACHE="$(jq -cn --argjson runs "${runs_only}" '{workflow_runs: $runs}' 2>/dev/null || echo "${empty_blob}")"
    _ACTIONS_RUNS_BLOB_READY="true"
    rm -f "${response_file}" "${response_body_file}" "${response_err}"
    printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"
    return 0
  fi

  # Reaching here means the per-tick actions-runs fetch was NOT confirmed: the
  # `gh api` call failed (auth/transient) or returned an unparseable body, so we
  # fail open to the stale cache (if any) or an empty blob. Emit one structured
  # diagnostic (CLAUDE.md §8) so an investigation can tell a genuine "zero
  # in-flight runs" from "the fetch never succeeded" — the ambiguity behind an
  # unattributable `Active issue set is empty: total=0`. A genuine-empty SUCCESS
  # returns earlier and never reaches this line, so the warning fires only on an
  # unconfirmed fetch. Fail-open behaviour itself is unchanged.
  local _fetch_err_line='' _fetch_status=''
  # Sanitize both fields: the HTTP status line carries a trailing CR (CRLF) and
  # gh stderr can be multi-line; an unstripped CR/newline would garble this
  # single-line ::warning:: contract.
  _fetch_err_line="$(head -n 1 "${response_err}" 2>/dev/null | tr -d '\r' | cut -c1-200 || true)"
  _fetch_status="$(printf '%s' "${status_line:-none}" | tr -d '\r' | cut -c1-80 || true)"
  printf '%s\n' "::warning::rate_limit_audit_fallback helper=_load_actions_runs_cached reason=fetch_unconfirmed repo=${repo} api_rc=${api_rc} status='${_fetch_status:-none}' cache_hit=${cache_hit} err='${_fetch_err_line}'" >&2
  if [ "${cache_hit}" = "true" ]; then
    _ACTIONS_RUNS_BLOB_CACHE="$(jq -cn --argjson runs "${cached_runs}" '{workflow_runs: $runs}' 2>/dev/null || echo "${empty_blob}")"
  else
    _ACTIONS_RUNS_BLOB_CACHE="${empty_blob}"
  fi
  _ACTIONS_RUNS_BLOB_READY="true"
  rm -f "${response_file}" "${response_body_file}" "${response_err}"
  printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"
  return 0
}

phase_cap_state_for_action() {
  local action="$1"
  case "${action}" in
    retrigger_pipeline)
      echo "ai:clarification"
      ;;
    auto_respond_clarify|retrigger_plan)
      echo "ai:planning"
      ;;
    auto_approve|retrigger_implement)
      echo "ai:implementing"
      ;;
    *)
      echo ""
      ;;
  esac
}

phase_cap_running_for_state() {
  local state="$1"
  jq -r --arg state "${state}" '.[$state] // 0' <<<"${PHASE_CAPS_RUNNING_BY_STATE:-"{}"}" 2>/dev/null || echo 0
}

phase_cap_can_dispatch() {
  local state="$1"
  local action="$2"
  local issue_num="$3"
  local state_limit
  local state_running
  local global_limit
  local global_running

  if [ "${PHASE_CAPS_ENABLED:-false}" != "true" ]; then
    return 0
  fi

  if [ "${PHASE_CAPS_STATUS:-missing}" = "actions_runs_unavailable" ]; then
    echo "phase_capped state=${state} action=${action} issue=${issue_num} reason=actions_runs_unavailable"
    return 1
  fi

  state_limit="$(jq -r --arg state "${state}" '.[$state] // -1' <<<"${PHASE_CAPS_MAX_BY_STATE:-"{}"}" 2>/dev/null || echo -1)"
  [[ "${state_limit}" =~ ^-?[0-9]+$ ]] || state_limit='-1'
  state_running="$(phase_cap_running_for_state "${state}")"
  [[ "${state_running}" =~ ^[0-9]+$ ]] || state_running='0'
  if [ "${state_limit}" -ge 0 ] && [ "${state_running}" -ge "${state_limit}" ]; then
    echo "phase_capped state=${state} action=${action} issue=${issue_num} limit=${state_limit} running=${state_running}"
    return 1
  fi

  global_limit="${PHASE_CAPS_GLOBAL_MAX:--1}"
  [[ "${global_limit}" =~ ^-?[0-9]+$ ]] || global_limit='-1'
  global_running="${PHASE_CAPS_GLOBAL_RUNNING:-0}"
  [[ "${global_running}" =~ ^[0-9]+$ ]] || global_running='0'
  if [ "${global_limit}" -ge 0 ] && [ "${global_running}" -ge "${global_limit}" ]; then
    echo "phase_capped state=${state} action=${action} issue=${issue_num} limit=${global_limit} running=${global_running} scope=global"
    return 1
  fi

  return 0
}

phase_cap_note_dispatch() {
  local state="$1"
  local global_running
  local updated_running_by_state

  if [ "${PHASE_CAPS_ENABLED:-false}" != "true" ] || [ -z "${state}" ]; then
    return 0
  fi

  if updated_running_by_state="$(jq -c --arg state "${state}" '.[$state] = ((.[$state] // 0) + 1)' <<<"${PHASE_CAPS_RUNNING_BY_STATE:-"{}"}" 2>/dev/null)"; then
    PHASE_CAPS_RUNNING_BY_STATE="${updated_running_by_state}"
  fi
  global_running="${PHASE_CAPS_GLOBAL_RUNNING:-0}"
  [[ "${global_running}" =~ ^[0-9]+$ ]] || global_running='0'
  PHASE_CAPS_GLOBAL_RUNNING="$(( global_running + 1 ))"
  return 0
}

prime_phase_concurrency_snapshot() {
  local caps_path="${1:-.github/ai/concurrency_caps.yml}"
  local actions_runs_blob
  local actions_runs_blob_file=""
  local empty_actions_runs='{"workflow_runs":[]}'
  local actions_runs_fetch_error=""
  local actions_runs_fetch_err_file=""
  local actions_runs_fetch_rc=0
  local snapshot_json
  local snapshot_error
  local -a caps_cmd

  actions_runs_blob_file="$(mktemp "${TMPDIR:-/tmp}/phase_caps_actions_runs_blob.XXXXXX")"
  actions_runs_fetch_err_file="$(mktemp "${TMPDIR:-/tmp}/phase_caps_actions_runs.XXXXXX")"
  # Prime the shared per-tick actions-runs cache in the parent shell before
  # reading the blob. Command substitution would run the loader in a subshell,
  # trap _ACTIONS_RUNS_BLOB_CACHE there, and force the next caller to re-fetch
  # the same three actions/runs endpoints. Capture stdout via a temp file so
  # the focused unit tests' shell-mock loader (which only prints JSON) still
  # feeds the snapshot builder.
  if _load_actions_runs_cached >"${actions_runs_blob_file}" 2>"${actions_runs_fetch_err_file}"; then
    actions_runs_blob="$(cat "${actions_runs_blob_file}" 2>/dev/null || true)"
    [ -n "${actions_runs_blob}" ] || actions_runs_blob="${_ACTIONS_RUNS_BLOB_CACHE:-${empty_actions_runs}}"
  else
    actions_runs_fetch_rc=$?
    actions_runs_fetch_error="$(tr '\n' ' ' < "${actions_runs_fetch_err_file}" | sed 's/[[:space:]]\+/ /g' | cut -c1-200)"
    [ -n "${actions_runs_fetch_error}" ] || actions_runs_fetch_error="actions_runs_fetch_failed_rc_${actions_runs_fetch_rc}"
    actions_runs_blob="${empty_actions_runs}"
  fi
  rm -f "${actions_runs_blob_file}" 2>/dev/null || true
  rm -f "${actions_runs_fetch_err_file}" 2>/dev/null || true
  caps_cmd=(python3 scripts/orchestrate_lib.py concurrency-caps --caps-path "${caps_path}" --threshold-minutes "${STALL_THRESHOLD_MINUTES:-120}")
  if [ -n "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ]; then
    caps_cmd+=(--implementing-threshold-minutes "${STALL_THRESHOLD_IMPLEMENTING_MINUTES}")
  fi
  if ! snapshot_json="$("${caps_cmd[@]}" 2>/dev/null <<<"${actions_runs_blob}")"; then
    snapshot_json='{"enabled":false,"status":"invalid","error":"snapshot_build_failed","global_max_concurrent":null,"max_concurrent_by_state":{},"running_by_state":{},"global_running":0}'
  fi
  [ -n "${snapshot_json}" ] || snapshot_json='{"enabled":false,"status":"invalid","error":"empty_snapshot","global_max_concurrent":null,"max_concurrent_by_state":{},"running_by_state":{},"global_running":0}'
  if [ "${actions_runs_fetch_rc}" -ne 0 ] && jq -e '.enabled == true' <<<"${snapshot_json}" >/dev/null 2>&1; then
    snapshot_json="$(jq -c --arg error "${actions_runs_fetch_error}" '.status = "actions_runs_unavailable" | .error = $error' <<<"${snapshot_json}" 2>/dev/null || echo "${snapshot_json}")"
  fi

  PHASE_CAPS_ENABLED="$(jq -r 'if .enabled == true then "true" else "false" end' <<<"${snapshot_json}" 2>/dev/null || echo 'false')"
  PHASE_CAPS_STATUS="$(jq -r '.status // "invalid"' <<<"${snapshot_json}" 2>/dev/null || echo 'invalid')"
  PHASE_CAPS_GLOBAL_MAX="$(jq -r 'if .global_max_concurrent == null then -1 else .global_max_concurrent end' <<<"${snapshot_json}" 2>/dev/null || echo '-1')"
  PHASE_CAPS_GLOBAL_RUNNING="$(jq -r '.global_running // 0' <<<"${snapshot_json}" 2>/dev/null || echo '0')"
  PHASE_CAPS_MAX_BY_STATE="$(jq -c '.max_concurrent_by_state // {}' <<<"${snapshot_json}" 2>/dev/null || echo '{}')"
  PHASE_CAPS_RUNNING_BY_STATE="$(jq -c '.running_by_state // {}' <<<"${snapshot_json}" 2>/dev/null || echo '{}')"
  if [ -n "${RUNTIME_DIR:-}" ]; then
    printf '%s\n' "${snapshot_json}" > "${RUNTIME_DIR}/running_runs_by_state.json" 2>/dev/null || true
  fi

  snapshot_error="$(jq -r '.error // empty' <<<"${snapshot_json}" 2>/dev/null || echo '')"
  case "${PHASE_CAPS_STATUS}" in
    malformed|invalid|unreadable|yaml_unavailable|actions_runs_unavailable)
      if [ -n "${snapshot_error}" ]; then
        echo "::warning::phase_concurrency_caps status=${PHASE_CAPS_STATUS} path=${caps_path} error=${snapshot_error}" >&2
      else
        echo "::warning::phase_concurrency_caps status=${PHASE_CAPS_STATUS} path=${caps_path}" >&2
      fi
      ;;
  esac
}

# _direct_inflight_review_run_on_branch — authoritative cache-miss fallback for
# the retrigger_review empty-commit guard's in-flight-review check.
#
# The primary in-flight check at both push sites (execute_stall_recovery_action
# and run_standalone_stall_recovery) reads the per-tick _load_actions_runs_cached
# blob.  That blob is the same source build_active_issue_set consumes, and the
# active-set guard comment already documents it can miss a live run (cache TTL /
# 304-reuse of empty cached_runs, the per-status 50-item listing window,
# pagination, or head_branch=null on workflow_dispatch).  A false negative is
# uniquely destructive at this one call site: the empty-commit push advances the
# branch under a still-editing review_autofix run, tripping its
# AUTOFIX_PRE_EDITOR_STALE_BASE -> soft_exit and discarding a full review pass.
#
# §15 audit: this is NOT a new unconditional per-issue call.  Callers invoke it
# only when the cached scan already returned no live review run AND they are
# about to perform the destructive empty-commit push — the fail-open cache-miss
# fallback §15 explicitly sanctions ("fall back to the smallest safe legacy
# call").  Audited alternatives and why they cannot supply the data: (a)
# _load_actions_runs_cached is the very blob that just missed the run, so
# re-reading it is futile; (b) the adjacent failed-autofix-redispatch loop
# fetches `--limit 1` completed-only runs per workflow, so it structurally
# cannot observe an in_progress/queued run.  This therefore issues a single
# branch-scoped `gh run list` (server-side --branch filter, so it is NOT subject
# to the global 50-item listing cap the cached blob is) rather than the 3-call
# per-workflow loop, keeping the added cost to one REST request on the rare
# recovery-push path.
#
# Args: $1 = head branch.  Echoes the databaseId of the freshest matching
# in_progress/queued review run younger than REVIEW_RUN_MAX_RUNTIME_MINUTES,
# else nothing.  Freshness mirrors build_active_issue_set's review-run window
# so a review still legitimately editing past STALL_THRESHOLD_MINUTES is not
# clobbered, while a genuinely hung run older than the review budget does not
# block recovery forever.  Limitation: `gh run list
# --json` exposes workflowName/name but not the workflow file path, so a
# consumer that renamed the review workflow's display name away from the
# canonical names ("AI Review" / "Internal Review" / "Review Autofix" /
# "Internal: AI Review & Autofix" / "Codex PR Self-Healing Semantic Agent" —
# the last two are this repo's actual internal-review.yml / review_autofix.yml
# display names; without them the guard could never match an upstream review
# run, the PR #3823 / issue #3816 false stall-recovery) is matched
# only if workflowName still resolves; on a miss the guard fails open (push
# proceeds) — no worse than the pre-fix behaviour, and the cached scan's own
# path-based match still covers that case whenever the cache itself hits (in
# which case this fallback is never reached).  Fails open (echoes nothing) on any
# gh/jq/date error so a transient API failure never blocks recovery.
_direct_inflight_review_run_on_branch()
{
	local _di_branch="$1"
	local _di_now_epoch _di_stall_secs _di_runs_json
	[ -n "${_di_branch}" ] || return 0
	_di_now_epoch="$(date +%s 2>/dev/null || echo "")"
	[[ "${_di_now_epoch}" =~ ^[0-9]+$ ]] || return 0
	# This helper only ever matches review-family runs (AI Review /
	# Internal Review / Review Autofix / Internal: AI Review & Autofix /
	# Codex PR Self-Healing Semantic Agent; name filter below),
	# so its freshness window is the review-run budget, not the generic stall
	# threshold — see REVIEW_RUN_MAX_RUNTIME_MINUTES.
	_di_stall_secs=$(( REVIEW_RUN_MAX_RUNTIME_MINUTES * 60 ))
	_di_runs_json="$(gh_retry gh run list --repo "${GITHUB_REPOSITORY}" \
		--branch "${_di_branch}" \
		--limit 30 \
		--json databaseId,status,name,workflowName,startedAt,createdAt \
		2>/dev/null || echo "")"
	[ -n "${_di_runs_json}" ] || return 0
	printf '%s' "${_di_runs_json}" | jq -r \
		--argjson now "${_di_now_epoch}" \
		--argjson threshold "${_di_stall_secs}" '
		(if type == "array" then . else [] end)
		| [ .[]?
			| select((.status // "") == "in_progress" or (.status // "") == "queued")
			| select(
				((.name // "") == "AI Review" or (.name // "") == "Internal Review" or (.name // "") == "Review Autofix"
				 or (.name // "") == "Internal: AI Review & Autofix" or (.name // "") == "Codex PR Self-Healing Semantic Agent")
				or ((.workflowName // "") == "AI Review" or (.workflowName // "") == "Internal Review" or (.workflowName // "") == "Review Autofix"
				    or (.workflowName // "") == "Internal: AI Review & Autofix" or (.workflowName // "") == "Codex PR Self-Healing Semantic Agent")
			  )
			| ([.startedAt, .createdAt] | map(select(type == "string" and . != ""))[0] // "") as $ts
			| (if $ts != ""
			   then (try ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch $now)
			   else $now end) as $start_epoch
			| select(($now - $start_epoch) < $threshold)
		  ] | (.[0].databaseId // empty)
	' 2>/dev/null || echo ""
}

# Build a set of issue numbers that have *genuinely active* (in_progress or
# queued) workflow runs — i.e., runs that started recently enough that they
# could still be making progress.
#
# Runs that have been active longer than their effective phase threshold are
# treated as zombie/hung runs and excluded. Implement runs can use the tighter
# STALL_THRESHOLD_IMPLEMENTING_MINUTES backstop when S2 kill mode is active.
#
# Exception: review-family runs (AI Review / Internal Review / Review
# Autofix, matched by canonical name or caller-workflow path) use the longer
# REVIEW_RUN_MAX_RUNTIME_MINUTES window, because they can legitimately run well
# past STALL_THRESHOLD_MINUTES (up to the codex-agent job timeout).  Without
# this, a review still editing at 120-240 min was dropped from the active set
# and re-triggered with a destructive empty commit — the PR #3082 stall loop.
#
# Outputs a newline-separated list of issue numbers.
workflow_run_cache_load() {
  local run_json="$1"
  local parsed workflow_name workflow_path head_branch run_id start_epoch

  if [ "${WORKFLOW_RUN_CACHE_KEY:-}" = "${run_json}" ]; then
    return 0
  fi

  parsed="$(printf '%s' "${run_json}" | jq -r '
    def parse_epoch(value):
      ((value // empty | tostring | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601?) // empty);
    [
      (.name // ""),
      (.path // ""),
      (.head_branch // ""),
      ((.id // "") | tostring),
      ((parse_epoch(.run_started_at) // parse_epoch(.created_at) // "") | tostring)
    ] | @tsv
  ' 2>/dev/null || true)"
  if [ -z "${parsed}" ]; then
    WORKFLOW_RUN_CACHE_KEY=""
    WORKFLOW_RUN_CACHE_NAME=""
    WORKFLOW_RUN_CACHE_PATH=""
    WORKFLOW_RUN_CACHE_HEAD_BRANCH=""
    WORKFLOW_RUN_CACHE_ID=""
    WORKFLOW_RUN_CACHE_START_EPOCH=""
    return 1
  fi

  IFS=$'\t' read -r workflow_name workflow_path head_branch run_id start_epoch <<< "${parsed}"
  WORKFLOW_RUN_CACHE_KEY="${run_json}"
  WORKFLOW_RUN_CACHE_NAME="${workflow_name}"
  WORKFLOW_RUN_CACHE_PATH="${workflow_path}"
  WORKFLOW_RUN_CACHE_HEAD_BRANCH="${head_branch}"
  WORKFLOW_RUN_CACHE_ID="${run_id}"
  WORKFLOW_RUN_CACHE_START_EPOCH="${start_epoch}"
  return 0
}

workflow_run_is_implement() {
  local run_json="$1"
  local workflow_name workflow_path

  workflow_run_cache_load "${run_json}" || return 1
  workflow_name="${WORKFLOW_RUN_CACHE_NAME:-}"
  workflow_path="${WORKFLOW_RUN_CACHE_PATH:-}"

  case "${workflow_path}" in
    */implement.yml|*/internal-implement.yml|implement.yml|internal-implement.yml)
      return 0
      ;;
  esac

  case "${workflow_name}" in
    *AI\ Implement*|*Internal\ Implement*)
      return 0
      ;;
  esac

  return 1
}

workflow_run_is_review_family() {
  local run_json="$1"
  local workflow_name workflow_path

  workflow_run_cache_load "${run_json}" || return 1
  workflow_name="${WORKFLOW_RUN_CACHE_NAME:-}"
  workflow_path="${WORKFLOW_RUN_CACHE_PATH:-}"

  case "${workflow_path}" in
    */ai-review.yml|*/internal-review.yml|*/review_autofix.yml|ai-review.yml|internal-review.yml|review_autofix.yml)
      return 0
      ;;
  esac

  case "${workflow_name}" in
    AI\ Review|Internal\ Review|Review\ Autofix|Internal:\ AI\ Review\ \&\ Autofix|Codex\ PR\ Self-Healing\ Semantic\ Agent)
      return 0
      ;;
  esac

  return 1
}

workflow_run_stall_threshold_seconds() {
  local run_json="$1"
  local stall_minutes="${STALL_THRESHOLD_MINUTES:-120}"

  workflow_run_cache_load "${run_json}" || {
    printf '%s' "$(( stall_minutes * 60 ))"
    return 0
  }

  if workflow_run_is_review_family "${run_json}"; then
    stall_minutes="${REVIEW_RUN_MAX_RUNTIME_MINUTES:-${stall_minutes}}"
  elif [ -n "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ] && workflow_run_is_implement "${run_json}"; then
    stall_minutes="${STALL_THRESHOLD_IMPLEMENTING_MINUTES}"
  fi

  printf '%s' "$(( stall_minutes * 60 ))"
}

workflow_run_is_fresh() {
  local run_json="$1"
  local now_epoch="$2"
  local start_epoch threshold_secs

  workflow_run_cache_load "${run_json}" || return 1
  start_epoch="${WORKFLOW_RUN_CACHE_START_EPOCH:-}"
  [[ "${start_epoch}" =~ ^[0-9]+$ ]] || return 1

  threshold_secs="$(workflow_run_stall_threshold_seconds "${run_json}")"
  [[ "${threshold_secs}" =~ ^[0-9]+$ ]] || threshold_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))

  [ $(( now_epoch - start_epoch )) -lt "${threshold_secs}" ]
}

build_active_issue_set() {
  local now_epoch
  now_epoch="$(date +%s)"
  local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))
  local review_stall_secs=$(( REVIEW_RUN_MAX_RUNTIME_MINUTES * 60 ))

  # Fetch active runs from the shared per-tick actions-runs loader.
  # This preserves one conditional API retrieval per tick.
  local actions_runs_blob
  actions_runs_blob="$(_load_actions_runs_cached)"

  # Preserve historical status selection semantics (in_progress/queued, max 50
  # each) while sourcing from one shared payload.
  local runs_json
  runs_json="$(printf '%s' "${actions_runs_blob}" | jq -c '[.workflow_runs[]? | select((.status // "") == "in_progress")] | .[:50]' 2>/dev/null || echo '[]')"
  [ -n "${runs_json}" ] || runs_json='[]'
  local queued_json
  queued_json="$(printf '%s' "${actions_runs_blob}" | jq -c '[.workflow_runs[]? | select((.status // "") == "queued")] | .[:50]' 2>/dev/null || echo '[]')"
  [ -n "${queued_json}" ] || queued_json='[]'


  # Merge both lists
  local all_runs
  all_runs="$(echo "${runs_json}" "${queued_json}" | jq -s 'add // []' 2>/dev/null || echo '[]')"

  # Filter out zombie runs using the effective per-run threshold.
  # Review-family runs use REVIEW_RUN_MAX_RUNTIME_MINUTES inside
  # workflow_run_stall_threshold_seconds(), so they stay in the active set
  # longer than other workflows without duplicating the jq filter here.
  local fresh_runs='[]'
  local -a fresh_run_rows=()
  local run_json
  while IFS= read -r run_json; do
    [ -n "${run_json}" ] || continue
    if workflow_run_is_fresh "${run_json}" "${now_epoch}"; then
      fresh_run_rows+=("${run_json}")
    fi
  done < <(printf '%s' "${all_runs}" | jq -c '.[]?' 2>/dev/null || true)

  if [ "${#fresh_run_rows[@]}" -gt 0 ]; then
    fresh_runs="$(printf '%s\n' "${fresh_run_rows[@]}" | jq -s '.' 2>/dev/null || echo '[]')"
  fi

  local fresh_count
  fresh_count="${#fresh_run_rows[@]}"
  local total_count
  total_count="$(printf '%s' "${all_runs}" | jq 'length' 2>/dev/null || echo '0')"
  [[ "${total_count}" =~ ^[0-9]+$ ]] || total_count=0
  if [ "${total_count}" -gt "${fresh_count}" ]; then
    echo "  Active runs: ${total_count} total, ${fresh_count} fresh ($(( total_count - fresh_count )) zombie runs excluded; general stale cutoff >$(( stall_secs / 60 ))m, review-family cutoff >$(( review_stall_secs / 60 ))m)." >&2
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
# A zombie is a pipeline run (clarify, plan, implement, orchestrate, etc.)
# that has been in_progress for longer than its effective stall threshold.
# Cancelling prevents resource waste and avoids conflicts with the recovery
# action (e.g., two implement runs on the same branch).
#
# Review-family workflows (ai-review.yml, internal-review.yml,
# review_autofix.yml) are explicitly excluded: they share the issue's head
# branch (ai/issue-N) but are not part of the orchestrator's pipeline, and
# legitimate review/edit passes can take longer than STALL_THRESHOLD_MINUTES
# without being stuck.  Cancelling them mid-flight produces sporadic
# `exit code 143 / runner has received a shutdown signal` failures on the
# consumer repo's review jobs.  Mirrors the inclusion list at the
# `workflow_outcomes` query site so the two filters stay in lockstep.
cancel_zombie_runs_for_issue() {
  local issue_num="$1"
  local now_epoch
  now_epoch="$(date +%s)"

  # Reuse the shared actions-runs blob and preserve prior in_progress+50 scope.
  local actions_runs_blob
  actions_runs_blob="$(_load_actions_runs_cached)"

  local runs_json
  runs_json="$(printf '%s' "${actions_runs_blob}" | jq -c '[.workflow_runs[]? | select((.status // "") == "in_progress")] | .[:50]' 2>/dev/null || echo '[]')"
  [ -n "${runs_json}" ] || runs_json='[]'

  local -a zombie_run_ids=()
  local run_json run_id head_branch
  while IFS= read -r run_json; do
    [ -n "${run_json}" ] || continue
    if workflow_run_is_fresh "${run_json}" "${now_epoch}"; then
      continue
    fi

    workflow_run_cache_load "${run_json}" || continue
    head_branch="${WORKFLOW_RUN_CACHE_HEAD_BRANCH:-}"
    if ! printf '%s\n' "${head_branch}" | grep -Eq "(^|/)(ai/(issue-)?|ai-(implement-)?)${issue_num}([^0-9]|$)"; then
      continue
    fi

    if workflow_run_is_review_family "${run_json}"; then
      continue
    fi

    run_id="${WORKFLOW_RUN_CACHE_ID:-}"
    if [[ "${run_id}" =~ ^[0-9]+$ ]]; then
      zombie_run_ids+=("${run_id}")
    fi
  done < <(printf '%s' "${runs_json}" | jq -c '.[]?' 2>/dev/null || true)

  if [ "${#zombie_run_ids[@]}" -gt 0 ]; then
    for run_id in "${zombie_run_ids[@]}"; do
      echo "  Cancelling zombie workflow run ${run_id} for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/cancel" -X POST 2>/dev/null || true
    done
  fi
}

# Find and close the PR linked to an issue (if any).
close_linked_pr() {
  local issue_num="$1"
  local close_reason="${2:-Closed by orchestrator stall recovery.}"
  local pr_nums
  pr_nums="$(_find_all_linked_prs "${issue_num}" 2>/dev/null || true)"

  # Diagnostic: previously this helper only consulted the timeline
  # cross-reference event and silently no-op'd when it returned nothing,
  # which is exactly how PR #2568 was orphaned in prod (issue #2552 was
  # re-issued but the PR stayed open).  Log every candidate source so any
  # future miss leaves a trail in the workflow log.
  if [ -z "${pr_nums}" ]; then
    echo "  close_linked_pr: no linked PRs found for issue #${issue_num} (timeline/branch/body lookups all empty)." >&2
    return 0
  fi

  local pr_num scanned=0 closed=0
  while IFS= read -r pr_num; do
    [[ "${pr_num}" =~ ^[0-9]+$ ]] || continue
    scanned=$((scanned + 1))
    local pr_state
    pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" --jq '.state' | grep -xE 'open|closed|merged' || echo "")"
    if [ "${pr_state}" = "open" ]; then
      echo "  close_linked_pr: closing linked PR #${pr_num} for issue #${issue_num} (state=open)."
      if gh_retry gh pr close "${pr_num}" --repo "${GITHUB_REPOSITORY}" \
          --comment "${close_reason}" 2>/dev/null; then
        closed=$((closed + 1))
      fi
    else
      echo "  close_linked_pr: skipping PR #${pr_num} for issue #${issue_num} (state=${pr_state:-unknown})."
    fi
  done <<< "${pr_nums}"
  echo "  close_linked_pr: issue=#${issue_num} scanned=${scanned} closed=${closed}"
}

# surface_reissue_closed_without_pr — Emit a Gap-2 signal when stall
# recovery is about to close a re-issued task that never produced a PR.
# Observed in prod for issue #2591 (re-issue of #2552) which closed with
# ai:closed after stall recovery exhausted, without ai/issue-2591 ever
# receiving a PR.  Per spec (Q3=A) this function SURFACES only — it does
# not block the subsequent close_and_reissue; forward progress continues.
#
# Must be called BEFORE the issue is closed so the issue comment lands
# on an open issue.
#
# Args:
#   issue_num       — number of the re-issue being closed
#   phase           — last observed pipeline phase label (e.g. ai:done)
#   stall_minutes   — how long it was stuck
#   recovery_count  — prior stall-recovery attempts
#   source          — "main" or "standalone" (recovery loop identifier)
#
# No-op when:
#   - issue body lacks the "Re-issued from #<n>" marker (i.e. it is the
#     original task, not itself a re-issue — still a gap but not Gap 2)
#   - issue has at least one linked PR per _find_all_linked_prs
#
# Fail-open on every underlying call; the surfacing is best-effort and
# must never block stall recovery.
surface_reissue_closed_without_pr()
{
	local issue_num="$1"
	local phase="${2:-}"
	local stall_minutes="${3:-0}"
	local recovery_count="${4:-0}"
	local source_label="${5:-unknown}"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0

	local issue_body parent_num
	issue_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' 2>/dev/null || echo "")"
	parent_num="$(printf '%s' "${issue_body}" | grep -oE 'Re-issued from #[0-9]+' | head -n1 | grep -oE '[0-9]+' | head -n1 || true)"
	if [ -z "${parent_num}" ]; then
		return 0
	fi

	local pr_nums
	pr_nums="$(_find_all_linked_prs "${issue_num}" 2>/dev/null || true)"
	if [ -n "${pr_nums}" ]; then
		return 0
	fi

	# Stable structured log prefix — documented in agents.md so
	# downstream alerting can grep it without parsing free-form text.
	echo "REISSUE_CLOSED_WITHOUT_PR issue=${issue_num} parent=${parent_num} phase=${phase} stall_minutes=${stall_minutes} recovery_count=${recovery_count} source=${source_label}"
	echo "::warning title=Re-issue closed without PR::Re-issue #${issue_num} (from #${parent_num}) closed without producing a PR; phase=${phase}, stuck ${stall_minutes}m, attempts=${recovery_count}, source=${source_label}."

	gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<COMMENT_EOF
⚠️ **Re-issue closed without producing a PR**

This issue was created by orchestrator stall recovery as a re-issue of #${parent_num}, but no PR was ever opened against \`ai/issue-${issue_num}\` before stall recovery exhausted. Surfacing the close-out for operational review.

- Last phase: \`${phase}\`
- Stalled for: ${stall_minutes} minutes
- Recovery attempts: ${recovery_count}
- Source loop: ${source_label}

The orchestrator will still create the next re-issue in the chain so forward progress continues, but a human should review issue #${parent_num} and the re-issue chain before further automation.
COMMENT_EOF
)" >/dev/null 2>&1 || true

	if declare -F memory_record_run_event >/dev/null 2>&1; then
		local meta
		meta="$(jq -cn \
			--arg repo "${GITHUB_REPOSITORY:-}" \
			--arg issue "${issue_num}" \
			--arg parent "${parent_num}" \
			--arg phase "${phase}" \
			--arg stall "${stall_minutes}" \
			--arg attempts "${recovery_count}" \
			--arg source "${source_label}" \
			'{repository:$repo,issue_number:$issue,parent_issue_number:$parent,last_phase:$phase,stall_minutes:$stall,recovery_count:$attempts,source:$source}' 2>/dev/null || echo '{}')"
		memory_record_run_event \
			--run-id "${GITHUB_RUN_ID:-local}" \
			--workflow "orchestrate_poll" \
			--event-type "reissue_closed_without_pr" \
			--status "warning" \
			--message "Re-issue #${issue_num} (from #${parent_num}) closed without producing a PR" \
			--issue-number "${issue_num}" \
			--actor "${GITHUB_ACTOR:-orchestrator}" \
			--metadata-json "${meta}" >/dev/null 2>&1 || true
	fi
}

# count_noop_ancestors <issue_num> [max_depth]
#
# Walks the "Re-issued from #N" ancestor chain of <issue_num> up to
# max_depth hops (default 3) and returns (echoes) the number of
# consecutive ancestors whose issue-comments contain the implement.yml
# no-op warning signature ("produced no repository changes").
#
# Input:  issue number (integer); optional max_depth (positive integer).
# Output: integer count on stdout (0 on any failure / fail-open).
# API cost: up to 2 * max_depth calls — one GET /issues/{n} and one
#           GET /issues/{n}/comments per hop. Stops early on the first
#           non-no-op ancestor or when the chain ends.
#
# This is an issue-local belt-and-braces cap used by all three
# orchestrator re-issue paths (main stall, standalone stall, no-op
# impl-failed). It catches the failure mode where the state-based
# MAX_IMPL_NOOP_REISSUES counter is stale — tracking issue #1292
# produced 30+ duplicate sub-issues because the wave-status iterator
# never refreshed get_impl_noop_count.  Fails open: any API/parse
# error aborts the walk and the partial count so far is returned,
# so callers fall through to their existing re-issue behaviour.
count_noop_ancestors()
{
	local issue_num="$1"
	local max_depth="${2:-3}"
	local noop_marker="produced no repository changes"
	local count=0
	local current="${issue_num}"
	local hop parent_body parent_num parent_has_noop

	[[ "${issue_num}" =~ ^[0-9]+$ ]] || { echo "0"; return 0; }
	[[ "${max_depth}" =~ ^[1-9][0-9]*$ ]] || max_depth=3

	for hop in $(seq 1 "${max_depth}"); do
		parent_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
		parent_num="$(printf '%s' "${parent_body}" | grep -oE 'Re-issued from #[0-9]+' | head -n1 | grep -oE '[0-9]+' | head -n1 || true)"
		if [ -z "${parent_num}" ]; then
			break
		fi
		[[ "${parent_num}" =~ ^[0-9]+$ ]] || break
		parent_has_noop="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${parent_num}/comments" --paginate \
			--jq "[.[] | select((.body // \"\") | test(\"${noop_marker}\"; \"i\"))] | length" 2>/dev/null \
			| awk 'BEGIN{s=0} /^[0-9]+$/ {s+=$1} END{print s}' || echo "")"
		[[ "${parent_has_noop}" =~ ^[0-9]+$ ]] || parent_has_noop=0
		if [ "${parent_has_noop}" -eq 0 ]; then
			break
		fi
		count=$((count + 1))
		current="${parent_num}"
	done

	echo "${count}"
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
    echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0,"phase_attempts":{},"conflict_override_count":{},"judge_escalate_streak":{}}'
    return
  fi

  local extracted
  extracted="$(printf '%s' "${state_raw}" | sed -n "/^${STANDALONE_STATE_MARKER_OPEN}$/,/^${STANDALONE_STATE_MARKER_CLOSE}$/p" | sed '1d;$d')"
  if [ -z "${extracted}" ]; then
    echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0,"phase_attempts":{},"conflict_override_count":{},"judge_escalate_streak":{}}'
    return
  fi

  if ! echo "${extracted}" | jq -e . >/dev/null 2>&1; then
    echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0,"phase_attempts":{},"conflict_override_count":{},"judge_escalate_streak":{}}'
    return
  fi

  echo "${extracted}" | jq -c '
    {
      schema_version: 1,
      last_seen_phase: (.last_seen_phase // ""),
      status_since_ts: ((.status_since_ts // 0) | tonumber),
      stall_recovery_count: ((.stall_recovery_count // 0) | tonumber),
      phase_attempts: (if (.phase_attempts | type) == "object" then .phase_attempts else {} end),
      conflict_override_count: (if (.conflict_override_count | type) == "object" then .conflict_override_count else {} end),
      judge_escalate_streak: (if (.judge_escalate_streak | type) == "object" then .judge_escalate_streak else {} end),
      updated_ts: ((.updated_ts // 0) | tonumber)
    }
  ' 2>/dev/null || echo '{"schema_version":1,"last_seen_phase":"","status_since_ts":0,"stall_recovery_count":0,"phase_attempts":{},"conflict_override_count":{},"judge_escalate_streak":{}}'
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
  local effective_max_recoveries="${MAX_STALL_RECOVERIES_PER_ISSUE}"

  [[ "${recovery_count}" =~ ^[0-9]+$ ]] || recovery_count="0"
  if [ "${phase}" = "ai:done" ]; then
    effective_max_recoveries="${MAX_STALL_RECOVERIES_DONE}"
  fi
  if [ "${recovery_count}" -ge "${effective_max_recoveries}" ]; then
    echo "skip"
    return
  fi

  local action
  action="$(python3 - "$phase" "$recovery_count" "$effective_max_recoveries" "$ENABLE_STALL_HUMAN_TERMINALIZATION" "$MAX_STALL_RECOVERIES_DONE" <<'PY'
import sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import resolve_stall_recovery_action

phase = sys.argv[1]
recovery_count = int(sys.argv[2])
max_recoveries = int(sys.argv[3])
enable_human_terminalization = sys.argv[4].lower() == "true"
max_done = int(sys.argv[5])
print(resolve_stall_recovery_action(
    phase,
    recovery_count,
    max_recoveries=max_recoveries,
    enable_stall_human_terminalization=enable_human_terminalization,
    max_recoveries_by_phase={"ai:done": max_done},
))
PY
)" || true
  if [ -z "${action}" ]; then
    echo "retrigger_pipeline"
  else
    echo "${action}"
  fi
}

normalize_stall_recovery_action() {
  local phase="$1"
  local recovery_count="$2"
  local candidate_action="${3:-}"

  local action
  action="$(python3 - "$phase" "$recovery_count" "$candidate_action" "$MAX_STALL_RECOVERIES_PER_ISSUE" "$ENABLE_STALL_HUMAN_TERMINALIZATION" "$MAX_STALL_RECOVERIES_DONE" <<'PY'
import sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import resolve_effective_stall_recovery_action

phase = sys.argv[1]
recovery_count = int(sys.argv[2])
candidate_action = sys.argv[3]
max_recoveries = int(sys.argv[4])
enable_human_terminalization = sys.argv[5].lower() == "true"
max_done = int(sys.argv[6])
print(resolve_effective_stall_recovery_action(
    phase,
    recovery_count,
    candidate_action,
    max_recoveries=max_recoveries,
    enable_stall_human_terminalization=enable_human_terminalization,
    max_recoveries_by_phase={"ai:done": max_done},
))
PY
)" || true

  if [ -z "${action}" ]; then
    action="$(recovery_action_for_phase "${phase}" "${recovery_count}")"
    if [ -z "${action}" ]; then
      action="retrigger_pipeline"
    fi
  fi
  echo "${action}"
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
        if brace_depth == 0:
            continue
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
  local phase_cap_state=""

  STALL_RECOVERY_SHOULD_INCREMENT="false"
  STALL_RECOVERY_EFFECTIVE_ACTION="${action}"

  phase_cap_state="$(phase_cap_state_for_action "${action}")"
  if [ -n "${phase_cap_state}" ] && ! phase_cap_can_dispatch "${phase_cap_state}" "${action}" "${issue_num}"; then
    echo "STALL_SKIP issue=${issue_num} reason=phase_capped phase=${phase} action=${action}"
    return 1
  fi

  case "${action}" in
    retrigger_pipeline)
      echo "  Re-triggering pipeline for issue #${issue_num}..."
      local _retrigger_pipeline_rc=0
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/reclarify

_Orchestrator stall recovery: this issue never entered the AI pipeline.
Re-triggering the clarification phase. If the issue description is
sufficient, proceed directly to planning and implementation._
STALL_EOF
)" >/dev/null 2>&1 || _retrigger_pipeline_rc=$?
      if [ "${_retrigger_pipeline_rc}" -eq 0 ]; then
        phase_cap_note_dispatch "ai:clarification"
      fi
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
      local _auto_respond_rc=0
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="${answer_body}" >/dev/null 2>&1 || _auto_respond_rc=$?
      if [ "${_auto_respond_rc}" -eq 0 ]; then
        phase_cap_note_dispatch "ai:planning"
      fi
      tg_notify "Stall recovery: auto-responded to clarification on issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    retrigger_plan)
      echo "  Re-triggering plan for issue #${issue_num}..."
      local _retrigger_plan_rc=0
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/answer

_Orchestrator stall recovery: planning phase stalled. Re-triggering
plan generation._
STALL_EOF
)" >/dev/null 2>&1 || _retrigger_plan_rc=$?
      if [ "${_retrigger_plan_rc}" -eq 0 ]; then
        phase_cap_note_dispatch "ai:planning"
      fi
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
      local _auto_approve_rc=0
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: auto-approving plan. This is an
orchestrator-managed issue that does not require human approval._
STALL_EOF
)" >/dev/null 2>&1 || _auto_approve_rc=$?
      if [ "${_auto_approve_rc}" -eq 0 ]; then
        phase_cap_note_dispatch "ai:implementing"
      fi
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
      # The implement workflow precheck (implement.yml) skips when
      # ai:implementing is present ("another implement run is in progress")
      # and also skips when ai:awaiting-approval is absent. A stalled issue
      # still carries ai:implementing from the previous run, so we must
      # swap the label back to ai:awaiting-approval BEFORE posting
      # /approved; otherwise the re-triggered workflow will no-op and the
      # stall recovery loops forever.
      if ! gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --remove-label 'ai:implementing' --add-label 'ai:awaiting-approval' >/dev/null 2>&1; then
        echo "::warning::Failed to swap ai:implementing → ai:awaiting-approval for issue #${issue_num}; /approved retrigger may no-op if label state is unchanged."
      fi
      local _retrigger_implement_rc=0
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: implementation phase appears stalled.
Re-triggering implementation. If a previous attempt crashed or timed
out, start fresh from the approved plan._
STALL_EOF
)" >/dev/null 2>&1 || _retrigger_implement_rc=$?
      if [ "${_retrigger_implement_rc}" -eq 0 ]; then
        phase_cap_note_dispatch "ai:implementing"
      fi
      tg_notify "Stall recovery: re-triggered implementation for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    retrigger_review)
      echo "  Re-triggering review for issue #${issue_num}..."
      local pr_num
      pr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
      if [[ "${pr_num}" =~ ^[0-9]+$ ]]; then
        local head_ref
        # Belt-and-braces conflict check (Q1:A, Q2:A, Q3:B).  The
        # pre-dispatch guard in run_standalone_stall_recovery should have
        # redirected this action when the PR is conflicted, but the
        # managed-path recover_stalled_issue dispatches straight here
        # without that guard.  Single PR fetch (reused below for head_ref)
        # so this adds no extra API calls on the happy path.
        local _rtr_pr_json
        local _rtr_mergeable
        local _rtr_merge_state
        local _rtr_head_sha
        _rtr_pr_json="$(_fetch_pr_json "${pr_num}")"
        head_ref="$(_jq_field "${_rtr_pr_json}" '.head.ref')"
        _rtr_mergeable="$(_jq_field "${_rtr_pr_json}" '.mergeable' 'true|false')"
        _rtr_merge_state="$(_jq_field "${_rtr_pr_json}" '.mergeable_state')"
        _rtr_head_sha="$(_jq_field "${_rtr_pr_json}" '.head.sha')"
        if [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ] && \
           { [ "${_rtr_mergeable}" = "false" ] || [ "${_rtr_merge_state}" = "dirty" ]; }; then
          echo "  Issue #${issue_num} PR #${pr_num} has merge conflicts (mergeable=${_rtr_mergeable:-unknown}, mergeable_state=${_rtr_merge_state:-unknown}) — routing to resolve_merge_conflict instead of pushing an empty commit."
          STALL_JUDGE_TARGET_PR="${pr_num}"
          STALL_JUDGE_HEAD_REF="${head_ref}"
          local _rtr_rc=0
          execute_stall_recovery_action "${issue_num}" "${phase}" "resolve_merge_conflict" "${recovery_count}" "${local_id}" "${stall_minutes}" || _rtr_rc=$?
          # Q3:B — conflict override does not consume a retrigger-style
          # recovery attempt.  resolve_merge_conflict sets
          # STALL_RECOVERY_SHOULD_INCREMENT="true" on its happy path (line
          # ~4405); reset it here so the override is budget-neutral.
          #
          # Per-head_sha cap (Q1b): track how many overrides have fired
          # for this exact head_sha and stop being budget-neutral after
          # MAX_BUDGET_NEUTRAL_OVERRIDES so the stall ladder can advance.
          # head_sha changes on every new commit, so once the resolver
          # pushes a fix, the counter restarts for the new sha.
          local _rtr_dispatched="${STALL_RECOVERY_SHOULD_INCREMENT:-false}"
          STALL_RECOVERY_SHOULD_INCREMENT="false"
          if [ "${_rtr_dispatched}" = "true" ] && [ -n "${_rtr_head_sha}" ] && [ "${_rtr_head_sha}" != "null" ] && [ -n "${STATE_FILE:-}" ] && [ -f "${STATE_FILE}" ]; then
            local _rtr_override_count
            _rtr_override_count="$(jq -r --arg sha "${_rtr_head_sha}" '.conflict_override_count[$sha] // 0' "${STATE_FILE}" 2>/dev/null || echo "0")"
            [[ "${_rtr_override_count}" =~ ^[0-9]+$ ]] || _rtr_override_count="0"
            if [ "${_rtr_override_count}" -ge "${MAX_BUDGET_NEUTRAL_OVERRIDES}" ]; then
              echo "  Issue #${issue_num} PR #${pr_num} head_sha ${_rtr_head_sha} has hit budget-neutral override cap (${_rtr_override_count} >= ${MAX_BUDGET_NEUTRAL_OVERRIDES}); consuming stall budget on this attempt."
              STALL_RECOVERY_SHOULD_INCREMENT="true"
            fi
            local _rtr_next_count=$(( _rtr_override_count + 1 ))
            jq --arg sha "${_rtr_head_sha}" --argjson n "${_rtr_next_count}" \
              '.conflict_override_count = ((.conflict_override_count // {}) | .[$sha] = $n)' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" 2>/dev/null && mv "${STATE_FILE}.tmp" "${STATE_FILE}" || rm -f "${STATE_FILE}.tmp" 2>/dev/null || true
          fi
          STALL_RECOVERY_EFFECTIVE_ACTION="resolve_merge_conflict"
          return "${_rtr_rc}"
        fi
        if [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ]; then
          # Failed-autofix redispatch (Q1/Q2/Q3:A).  If the most recent
          # ai-review / internal-review / review_autofix run for this
          # branch completed with failure/cancelled/timed_out, the PR
          # has no in-flight worker and an empty-commit push will not
          # re-dispatch the workflow (empty commits do not fire
          # pull_request.synchronize).  Call _dispatch_review_for_conflicts
          # directly; it already guards against duplicate dispatch
          # (cycle tracker + _has_active_autofix_run).  Fall through to
          # the empty-commit path on dispatch-failure (rc=1); treat
          # already-dispatched/active (rc=2) as success.
          local _rtr_failed_conclusion=""
          local _rtr_failed_wf=""
          for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
            local _rtr_wf_conclusion
            _rtr_wf_conclusion="$(gh_retry gh run list --repo "${GITHUB_REPOSITORY}" \
              --workflow "${wf_candidate}" \
              --branch "${head_ref}" \
              --limit 1 \
              --json status,conclusion \
              --jq '[.[] | select(.status == "completed")] | .[0].conclusion // empty' \
              2>/dev/null || echo "")"
            case "${_rtr_wf_conclusion}" in
              failure|cancelled|timed_out)
                _rtr_failed_conclusion="${_rtr_wf_conclusion}"
                _rtr_failed_wf="${wf_candidate}"
                break
                ;;
            esac
          done
          if [ -n "${_rtr_failed_conclusion}" ]; then
            echo "  Issue #${issue_num} PR #${pr_num} last ${_rtr_failed_wf} run concluded '${_rtr_failed_conclusion}' — dispatching review workflow directly instead of pushing an empty commit."
            local _rtr_dispatch_rc=0
            _dispatch_review_for_conflicts "${pr_num}" "${head_ref}" || _rtr_dispatch_rc=$?
            if [ "${_rtr_dispatch_rc}" -eq 0 ]; then
              tg_notify "Stall recovery: re-dispatched review workflow for PR #${pr_num} (issue #${issue_num}, last run='${_rtr_failed_conclusion}', stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"PR: $(_gh_url "pull/${pr_num}")"$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
              STALL_RECOVERY_SHOULD_INCREMENT="true"
              STALL_RECOVERY_EFFECTIVE_ACTION="redispatch_review_autofix"
              return 0
            fi
            if [ "${_rtr_dispatch_rc}" -eq 2 ]; then
              echo "  Issue #${issue_num} PR #${pr_num} dispatch helper skipped (already active or already dispatched this cycle); not incrementing stall recovery."
              STALL_RECOVERY_EFFECTIVE_ACTION="redispatch_review_autofix"
              return 0
            fi
            echo "  Issue #${issue_num} PR #${pr_num} dispatch helper returned rc=${_rtr_dispatch_rc}; falling back to empty-commit push."
          fi
        fi
        if [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ]; then
          # In-flight review guard.  An empty-commit push here flips the
          # mid-run stale-base gate on any review_autofix run that is
          # currently editing this branch: the orchestrator's commit
          # subject does not match the [ai-autofix] / [ai-merge-resolve]
          # prefixes in check_external_branch_advance.sh (line 151-160),
          # so the gate prints ADVANCE=external, the pre-review
          # (review_autofix.yml:2972-3009) and pre-editor
          # (review_autofix.yml:3268-3309) steps set
          # AUTOFIX_STALE_BASE_SKIP=true, and the editor commit / push /
          # mark-ready-to-merge / re-trigger tail steps that are gated on
          # AUTOFIX_STALE_BASE_SKIP != 'true' are all skipped — the
          # editor's work is discarded and a brand-new review cycle
          # starts from the empty commit.
          #
          # The active-workflow guard in recover_stalled_issue (via
          # issue_has_active_workflow consuming build_active_issue_set)
          # is the primary protection, but a single missed run in that
          # 50-item blob
          # (cache TTL, pagination edge, head_branch=null on
          # workflow_dispatch) defeats every downstream stall check.
          # This is a targeted defense-in-depth at the only push site
          # that materially harms in-flight review_autofix runs.
          #
          # Reads from the per-tick _ACTIONS_RUNS_BLOB_CACHE populated
          # by _load_actions_runs_cached so this adds zero API calls
          # (§15).  Workflow filter mirrors
          # cancel_zombie_runs_for_issue — both .name and .path so
          # consumer-repo caller workflows that rename the display name
          # are still caught.  Run matching prefers head_branch, with a
          # blank-head_branch head_sha fallback for workflow_dispatch
          # runs.  Zombie filter (REVIEW_RUN_MAX_RUNTIME_MINUTES) mirrors
          # build_active_issue_set's review-run window so a genuinely hung
          # run does not block recovery indefinitely while a review that is
          # still legitimately editing (past STALL_THRESHOLD_MINUTES but
          # within its job budget) is not clobbered.  Malformed or blank timestamps
          # on a matching review run are treated as fresh so we
          # conservatively avoid invalidating a potentially live
          # autofix pass.  Other jq/cache errors still fail open, and
          # if date +%s is unavailable we skip the guard entirely:
          # empty result falls through to the legacy empty-commit path.
          local _rtr_inflight_blob _rtr_inflight_id _rtr_direct_inflight_id _rtr_now_epoch _rtr_stall_secs _rtr_origin_head_sha _rtr_push_succeeded
          _rtr_inflight_blob="$(_load_actions_runs_cached 2>/dev/null || echo '{"workflow_runs":[]}')"
          _rtr_now_epoch="$(date +%s 2>/dev/null || echo "")"
          # This block selects only review-family runs (name/path filter
          # below), so the freshness window is the review-run budget, not the
          # generic stall threshold — see REVIEW_RUN_MAX_RUNTIME_MINUTES.
          _rtr_stall_secs=$(( REVIEW_RUN_MAX_RUNTIME_MINUTES * 60 ))
          _rtr_push_succeeded="false"
          if [[ "${_rtr_now_epoch}" =~ ^[0-9]+$ ]]; then
            _rtr_inflight_id="$(printf '%s' "${_rtr_inflight_blob}" | jq -r \
              --arg br "${head_ref}" \
              --arg sha "${_rtr_head_sha}" \
              --argjson now "${_rtr_now_epoch}" \
              --argjson threshold "${_rtr_stall_secs}" '
              [.workflow_runs[]?
               | select((.status // "") == "in_progress" or (.status // "") == "queued")
               | select(
                   ((.head_branch // "") == $br)
                   or ((.head_branch // "") == "" and $sha != "" and (.head_sha // "") == $sha)
                 )
               | select(
                   (.name // "") == "AI Review"
                   or (.name // "") == "Internal Review"
                   or (.name // "") == "Review Autofix"
                   or (.name // "") == "Internal: AI Review & Autofix"
                   or (.name // "") == "Codex PR Self-Healing Semantic Agent"
                   or ((.path // "") | endswith("ai-review.yml"))
                   or ((.path // "") | endswith("internal-review.yml"))
                   or ((.path // "") | endswith("review_autofix.yml"))
                 )
               | ([.run_started_at, .created_at]
                  | map(select(type == "string" and . != ""))[0] // "") as $ts
               | (if $ts != ""
                  then (try ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch $now)
                  else $now
                  end) as $start_epoch
               | select(($now - $start_epoch) < $threshold)
              ] | (.[0].id // empty)
            ' 2>/dev/null || echo "")"
          else
            _rtr_inflight_id=""
          fi
          if [ -n "${_rtr_inflight_id}" ]; then
            echo "  Issue #${issue_num} PR #${pr_num} has in-flight review run #${_rtr_inflight_id} on ${head_ref} (fresh, <${REVIEW_RUN_MAX_RUNTIME_MINUTES}m); skipping empty-commit push to avoid invalidating its stale-base gate."
            STALL_RECOVERY_EFFECTIVE_ACTION="retrigger_review_skipped_inflight"
            return 1
          fi
          # Cache-miss fallback (CLAUDE.md §15 fail-open): the cached scan above
          # found no live review run, but it reads the same blob the active-set
          # guard can miss (cache TTL / 50-item window / head_branch=null).
          # Before the destructive empty-commit push, confirm against an
          # authoritative branch-scoped run listing; a false negative here
          # discards a full in-flight review pass (RC1 of the #11/#12 incident).
          _rtr_direct_inflight_id="$(_direct_inflight_review_run_on_branch "${head_ref}")"
          if [ -n "${_rtr_direct_inflight_id}" ]; then
            echo "  Issue #${issue_num} PR #${pr_num} has in-flight review run #${_rtr_direct_inflight_id} on ${head_ref} (direct check — cached scan missed it); skipping empty-commit push to avoid invalidating its stale-base gate."
            STALL_RECOVERY_EFFECTIVE_ACTION="retrigger_review_skipped_inflight"
            return 1
          fi
          if git fetch origin "${head_ref}:refs/remotes/origin/${head_ref}" 2>/dev/null; then
            _rtr_origin_head_sha="$(git rev-parse --verify "refs/remotes/origin/${head_ref}" 2>/dev/null || echo "")"
            if [[ "${_rtr_head_sha}" =~ ^[0-9a-f]{40}$ ]] && [[ "${_rtr_origin_head_sha}" =~ ^[0-9a-f]{40}$ ]] && \
               [ "${_rtr_origin_head_sha}" != "${_rtr_head_sha}" ]; then
              echo "  Issue #${issue_num} PR #${pr_num} head advanced from ${_rtr_head_sha} to ${_rtr_origin_head_sha} after the PR-state snapshot; skipping empty-commit push to avoid racing newer review work."
              return 1
            fi
            if git checkout "origin/${head_ref}" 2>/dev/null; then
              git config user.name "codex-bot"
              git config user.email "codex@users.noreply.github.com"
              git commit --allow-empty -m "[orchestrator] stall recovery: re-trigger review for issue #${issue_num}" 2>/dev/null || true
              if git push origin "HEAD:${head_ref}" 2>/dev/null; then
                tg_notify "Stall recovery: re-triggered review for PR #${pr_num} (issue #${issue_num}, stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"PR: $(_gh_url "pull/${pr_num}")"$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
                _rtr_push_succeeded="true"
              fi
              git checkout --detach HEAD 2>/dev/null || true
            else
              echo "  Issue #${issue_num} PR #${pr_num} checkout origin/${head_ref} failed after fetch; skipping empty-commit push."
            fi
          fi
          if [ "${_rtr_push_succeeded}" != "true" ]; then
            echo "  Issue #${issue_num} PR #${pr_num} empty-commit retrigger did not reach a successful push; skipping recovery increment."
            return 1
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
      # Belt-and-braces ancestor-chain no-op cap.  If the
      # "Re-issued from #N" chain already contains
      # MAX_IMPL_NOOP_REISSUES consecutive no-op ancestors, stop
      # spawning re-issues and let the wave-completion judge verify
      # — the work described is almost certainly already on the
      # integration branch.  Fails open: on any API error
      # count_noop_ancestors returns 0 and we fall through to the
      # legacy close+re-issue flow.
      local stall_anc_noop_count
      stall_anc_noop_count="$(count_noop_ancestors "${issue_num}" "${MAX_IMPL_NOOP_REISSUES}")"
      [[ "${stall_anc_noop_count}" =~ ^[0-9]+$ ]] || stall_anc_noop_count=0
      if [ "${stall_anc_noop_count}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
        echo "  [stall-recovery] Ancestor-chain no-op cap reached for #${issue_num} (${stall_anc_noop_count}/${MAX_IMPL_NOOP_REISSUES}). Closing without re-issue — judge will verify."
        ensure_label_exists "ai:closed"
        close_linked_pr "${issue_num}" \
          "Closed by orchestrator stall recovery — ancestor-chain no-op cap reached (${stall_anc_noop_count}/${MAX_IMPL_NOOP_REISSUES})."
        gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:done' --remove-label 'ai:implementing' \
          --remove-label 'ai:planning' --remove-label 'ai:clarification' \
          --remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
          --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          -c "Closing: stall recovery detected ${stall_anc_noop_count} consecutive no-op ancestor(s) in the Re-issued from chain (cap ${MAX_IMPL_NOOP_REISSUES}). The code described likely already exists on the integration branch; the wave-completion judge will verify." 2>/dev/null || true
        tg_notify "Stall recovery: ancestor-chain no-op cap hit for issue #${issue_num} (${stall_anc_noop_count}/${MAX_IMPL_NOOP_REISSUES}). Closed — judge will verify."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="false"
        return 0
      fi

      echo "  Closing and re-issuing stalled issue #${issue_num}..."
      surface_reissue_closed_without_pr "${issue_num}" "${phase}" "${stall_minutes}" "${recovery_count}" "main"
      close_linked_pr "${issue_num}" \
        "Closed by orchestrator stall recovery — issue #${issue_num} was stuck in '${phase}' for ${stall_minutes}m. A replacement issue will be created."

      local orig_title orig_body
      orig_title="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title // ""' || echo "")"
      orig_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' || echo "")"

      ensure_label_exists "ai:closed"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --remove-label 'ai:done' --remove-label 'ai:implementing' \
        --remove-label 'ai:planning' --remove-label 'ai:clarification' \
        --remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
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
      ensure_label_exists "ai:orchestrator-managed"
      new_url="$(gh_retry gh issue create --repo "${GITHUB_REPOSITORY}" \
        --title "${orig_title}" \
        --body "${new_body}" \
        --label "ai:clarification" \
        --label "ai:orchestrator-managed" 2>/dev/null || echo "")"
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
      # Healthy-PR guard.  Before hard-closing the linked PR (which is
      # destructive — once closed, the autofix loop cannot recover and
      # the issue is permanently terminated), check whether the linked
      # PR has produced productive autofix activity recently.  If yes,
      # the stall is almost certainly downstream of the linked PR (e.g.
      # one rb_judge LLM call failed) rather than the PR itself being
      # stuck, so route to dispatch_rb_judge — which is already a
      # defined ladder rung — instead of skip.
      #
      # "Productive activity" = at least one `[ai-autofix]` or
      # `[judge-fix]` commit on the PR head within the last
      # SKIP_HEALTHY_PR_LOOKBACK_HOURS (default 24h).  Disable by
      # setting SKIP_HEALTHY_PR_GUARD_ENABLED=false.
      local _skip_pr_num=""
      if [ -n "${STALL_MANAGED_LINKED_PR_CACHE:-}" ]; then
        _skip_pr_num="$(printf '%s' "${STALL_MANAGED_LINKED_PR_CACHE}" \
          | jq -r --arg n "${issue_num}" '.[$n].number // empty' 2>/dev/null || echo "")"
      fi
      if ! [[ "${_skip_pr_num}" =~ ^[0-9]+$ ]]; then
        _skip_pr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
      fi
      if [ "${SKIP_HEALTHY_PR_GUARD_ENABLED:-true}" = "true" ] \
          && [[ "${_skip_pr_num}" =~ ^[0-9]+$ ]]; then
        local _skip_lookback_hours="${SKIP_HEALTHY_PR_LOOKBACK_HOURS:-24}"
        if ! [[ "${_skip_lookback_hours}" =~ ^[0-9]+$ ]] || [ "${_skip_lookback_hours}" -lt 1 ]; then
          _skip_lookback_hours=24
        fi
        local _skip_since_iso
        # Try GNU `date -d` (Linux) first, then BSD `date -v` (macOS).
        # If both fail (e.g. an exotic runner image without either flag),
        # fall back to an empty cutoff — the jq filter below treats an
        # empty `$since` as "match any committer date", which is the
        # intentional fail-open: we'd rather route a still-active PR to
        # `dispatch_rb_judge` (recoverable) than hard-close it via skip
        # because we couldn't compute a 24h window.  In practice both
        # date variants are present on every Actions-supported runner.
        _skip_since_iso="$(date -u -d "${_skip_lookback_hours} hours ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
          || date -u -v-"${_skip_lookback_hours}"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
          || echo "")"
        local _skip_commits_json
        _skip_commits_json="$(gh_retry gh api --paginate \
          "repos/${GITHUB_REPOSITORY}/pulls/${_skip_pr_num}/commits?per_page=100" \
          2>/dev/null | jq -s 'add // []' 2>/dev/null || echo '[]')"
        local _skip_recent_autofix
        _skip_recent_autofix="$(printf '%s' "${_skip_commits_json}" | jq --arg since "${_skip_since_iso}" '
          [ .[]?
            | select(($since == "") or ((.commit.committer.date // "") >= $since))
            | (.commit.message // "")
            | select(test("^(\\[ai-autofix\\]|\\[judge-fix\\])"))
          ] | length
        ' 2>/dev/null || echo 0)"
        if [[ "${_skip_recent_autofix}" =~ ^[0-9]+$ ]] && [ "${_skip_recent_autofix}" -gt 0 ]; then
          echo "  skip→dispatch_rb_judge: PR #${_skip_pr_num} has ${_skip_recent_autofix} productive autofix commit(s) in the last ${_skip_lookback_hours}h; routing to dispatch_rb_judge instead of hard-closing."
          tg_notify "Stall recovery: deferring skip for issue #${issue_num} — linked PR #${_skip_pr_num} has ${_skip_recent_autofix} productive autofix commit(s) in last ${_skip_lookback_hours}h; routing to dispatch_rb_judge."$'\n'"Issue: $(_gh_url "issues/${issue_num}")"$'\n'"PR: $(_gh_url "pull/${_skip_pr_num}")" "WARNING"
          local _skip_redirect_rc=0
		  _dispatch_rb_judge_for_pr "${_skip_pr_num}" "${issue_num}" || _skip_redirect_rc=$?
		  if [ "${_skip_redirect_rc}" -eq 1 ]; then
		    return 1
		  fi
		  if [ "${_skip_redirect_rc}" -eq 2 ] || [ "${_skip_redirect_rc}" -eq 3 ]; then
		    return 0
		  fi
          STALL_RECOVERY_SHOULD_INCREMENT="true"
          return 0
        fi
      fi

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
      local escalated_labels
      escalated_labels="$(get_issue_labels_json "${issue_num}")"
      if has_label "${escalated_labels}" "ai:needs-human"; then
        tg_notify "Stall judge escalated issue #${issue_num} for human intervention (phase ${phase}, stuck ${stall_minutes}m)."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "CRITICAL"
      else
        echo "::warning::Stall judge could not verify ai:needs-human label on #${issue_num}; continuing bounded recovery retries." >&2
        tg_notify "Stall judge escalation could not verify ai:needs-human on issue #${issue_num} (phase ${phase}, stuck ${stall_minutes}m)."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      fi
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
	gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${target_pr}/update-branch" \
          -X PUT -f expected_head_sha="${head_sha}" >/dev/null 2>&1 || true
      fi
      _dispatch_review_for_conflicts "${target_pr}" "${head_ref}" || dispatch_rc=$?
      if [ "${dispatch_rc}" -eq 1 ]; then
        return 1
      fi
      if [ "${dispatch_rc}" -eq 2 ]; then
        return 0
      fi
      STALL_RECOVERY_SHOULD_INCREMENT="true"
      ;;

    dispatch_rb_judge)
      # Autonomous escape from ai:review-blocked: dispatch the
      # standalone review-blocked judge workflow for the linked PR.
      # The judge (scripts/review_rb_judge.sh) then decides merge,
      # fix, or close_and_reissue.  Linked PR lookup prefers the
      # shared stall cache (STALL_MANAGED_LINKED_PR_CACHE) and falls
      # back to the legacy per-issue REST cross-ref helper.
      local rb_pr_num=""
      if [ -n "${STALL_MANAGED_LINKED_PR_CACHE:-}" ]; then
        rb_pr_num="$(printf '%s' "${STALL_MANAGED_LINKED_PR_CACHE}" \
          | jq -r --arg n "${issue_num}" '.[$n].number // empty' 2>/dev/null || echo "")"
      fi
      if ! [[ "${rb_pr_num}" =~ ^[0-9]+$ ]]; then
        rb_pr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
      fi
      if ! [[ "${rb_pr_num}" =~ ^[0-9]+$ ]]; then
        # No linked PR — the judge has nothing to act on.  Increment
        # the recovery count so the ladder can progress to the next
        # rung (escalate_human after MAX_STALL_RECOVERIES_PER_ISSUE).
        # Returning 1 without incrementing would trap the issue in an
        # infinite dispatch_rb_judge loop because the count never
        # advances.  Operator visibility is preserved via the warning
        # and Telegram note below.
        echo "::warning::dispatch_rb_judge: no linked PR found for issue #${issue_num}; counting as an attempt so the ladder can escalate."
        tg_notify "Stall recovery: dispatch_rb_judge for issue #${issue_num} could not find a linked PR (stuck ${stall_minutes}m, attempt $((recovery_count + 1))). Counting as an attempt — will escalate to escalate_human at the end of the ladder."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        return 0
      fi
      local rb_dispatch_rc=0
	      _dispatch_rb_judge_for_pr "${rb_pr_num}" "${issue_num}" || rb_dispatch_rc=$?
	      if [ "${rb_dispatch_rc}" -eq 1 ]; then
	        return 1
	      fi
	      if [ "${rb_dispatch_rc}" -eq 2 ] || [ "${rb_dispatch_rc}" -eq 3 ]; then
	        # Already dispatched this cycle.  Don't increment recovery
	        # count — let the next cycle re-check; the judge run is in
	        # flight or the phase cap deferred a fresh dispatch.
	        return 0
	      fi
      tg_notify "Stall recovery: dispatched review-blocked judge for issue #${issue_num} (PR #${rb_pr_num}, stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")"$'\n'"PR: $(_gh_url "pull/${rb_pr_num}")" "WARNING"
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
  local _judge_state_file_ready="false"
  local _judge_state_file="${STATE_FILE:-}"
  local _judge_state_file_from_override="false"

  if [ -z "${local_id}" ] && [ -n "${STALL_JUDGE_STATE_FILE_OVERRIDE:-}" ]; then
    _judge_state_file="${STALL_JUDGE_STATE_FILE_OVERRIDE}"
    _judge_state_file_from_override="true"
  fi

  if [ -n "${_judge_state_file:-}" ] && [ -f "${_judge_state_file}" ]; then
    _judge_state_file_ready="true"
  elif [ -n "${_judge_state_file:-}" ] && [ -z "${local_id}" ] && [ "${_judge_state_file_from_override}" != "true" ]; then
    if printf '{}\n' > "${_judge_state_file}" 2>/dev/null; then
      _judge_state_file_ready="true"
    else
      echo "::warning::[stall-judge] could not initialize standalone STATE_FILE for issue #${issue_num}; replay cache and decision-streak backstops will not persist." >&2
    fi
  fi

  local fallback_action
  fallback_action="$(recovery_action_for_phase "${phase}" "${recovery_count}")"

  local comments_issue_num="${issue_num}"
  if [ -n "${local_id}" ] && [[ "${TRACKING_NUM:-}" =~ ^[0-9]+$ ]]; then
    comments_issue_num="${TRACKING_NUM}"
  fi

  local issue_comments_json
  issue_comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${comments_issue_num}/comments?per_page=100" | jq -s 'add // []' 2>/dev/null || echo '[]')"
  local recent_comments
  # Filter out ORCHESTRATOR_STATE_V1/V2 snapshots (they are ~57KB each and
  # are noise for the judge — it wants phase-change / recovery narrative,
  # not state dumps) and cap each remaining body at 2000 characters. Without
  # these two caps, 8 state snapshots produce ~260KB of argv which trips
  # Linux MAX_ARG_STRLEN (128KB per argv entry) when passed to the final
  # diagnostics jq via `--argjson recent_comments`, causing execve to
  # return E2BIG and the diagnostics blob to come out empty.
  recent_comments="$(printf '%s' "${issue_comments_json}" | jq -c '
    [.[]
      | select(((.body // "") | (startswith("<!-- ORCHESTRATOR_STATE_V1") or startswith("<!-- ORCHESTRATOR_STATE_V2"))) | not)
      | {
          author: (.user.login // ""),
          created_at: (.created_at // ""),
          body: ((.body // "") | if length > 2000 then .[:1988] + "…[truncated]" else . end)
        }
    ] | (if length > 8 then .[-8:] else . end)
  ' 2>/dev/null || echo '[]')"

  local linked_pr
  linked_pr="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"

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
  workflows_json="$(_load_actions_runs_cached)"
  workflow_outcomes="$(printf '%s' "${workflows_json}" | jq -c --arg head_ref "${head_ref}" --arg head_sha "${head_sha}" '
    [.workflow_runs[]?
      | select((.name // "") == "AI Review"
               or (.name // "") == "Internal Review"
               or (.name // "") == "Review Autofix"
               or (.name // "") == "Internal: AI Review & Autofix"
               or (.name // "") == "Codex PR Self-Healing Semantic Agent"
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

  # Judge decision cache (Q1d): memoise judge output by
  # sha256({issue_num, head_sha, phase, last_conclusion,
  # recent_comments_hash_excluding_prior_stall_judge_comments}). When the same
  # inputs reproduce, replay the
  # cached action instead of burning a fresh LLM call.  After
  # MAX_JUDGE_REPLAY consecutive replays without
  # progress, bypass the cache and escalate so the issue isn't stuck on a
  # bad decision forever. The cache is only consulted when the judge state file
  # exists; missing-state cycles fall through to the LLM path.
  local _judge_last_conclusion=""
  local _judge_cacheable_comments='[]'
  local _judge_recent_comments_hash=""
  local _judge_cache_key=""
  local _judge_cache_hit_action=""
  local _judge_cache_replay_count=0
  local _judge_force_escalate="false"
  if [ "${_judge_state_file_ready}" = "true" ]; then
    _judge_last_conclusion="$(printf '%s' "${workflow_outcomes}" | jq -r '.[0].conclusion // ""' 2>/dev/null || echo "")"
    _judge_cacheable_comments="$(printf '%s' "${recent_comments}" | jq -c '[.[] | select(((.body // "") | startswith("## 🧑‍⚖️ Stall Judge")) | not)]' 2>/dev/null || echo '[]')"
    _judge_recent_comments_hash="$(printf '%s' "${_judge_cacheable_comments}" | sha256sum 2>/dev/null | awk '{print $1}')"
    _judge_cache_key="$(printf '%s|%s|%s|%s|%s' "${issue_num}" "${head_sha:-}" "${phase}" "${_judge_last_conclusion}" "${_judge_recent_comments_hash}" | sha256sum 2>/dev/null | awk '{print $1}')"
    if [ -n "${_judge_cache_key}" ]; then
      _judge_cache_hit_action="$(jq -r --arg k "${_judge_cache_key}" '.judge_decision_cache[$k].action // ""' "${_judge_state_file}" 2>/dev/null || echo "")"
      _judge_cache_replay_count="$(jq -r --arg k "${_judge_cache_key}" '.judge_decision_cache[$k].replay_count // 0' "${_judge_state_file}" 2>/dev/null || echo "0")"
      [[ "${_judge_cache_replay_count}" =~ ^[0-9]+$ ]] || _judge_cache_replay_count=0
      if [ -n "${_judge_cache_hit_action}" ] && [ "${_judge_cache_replay_count}" -ge "${MAX_JUDGE_REPLAY}" ]; then
        _judge_force_escalate="true"
      fi
    fi
  fi

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
    }' 2>/dev/null || echo '')"

  # Defensive guard: if the diagnostics builder above silently failed
  # (empty output or non-JSON), the judge would otherwise receive a
  # blank blob and escalate blind. Emit a structured warning so the
  # failure is visible in the workflow log, and fall back to a minimal
  # payload that still carries the essential decision fields.
  if [ -z "${diagnostics}" ] || ! printf '%s' "${diagnostics}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    echo "::warning::Stall judge diagnostics builder failed for issue #${issue_num} (empty or non-JSON output); using minimal fallback payload. Likely cause: an --argjson input exceeded Linux MAX_ARG_STRLEN or contained invalid JSON." >&2
    diagnostics="$(jq -cn \
      --arg issue_number "${issue_num}" \
      --arg local_id "${local_id}" \
      --arg phase "${phase}" \
      --argjson stall_minutes "${stall_minutes:-0}" \
      --argjson recovery_count "${recovery_count:-0}" \
      --arg target_pr "${target_pr}" \
      --arg pr_state "${pr_state}" \
      --arg pr_mergeable "${pr_mergeable}" \
      --arg head_ref "${head_ref}" \
      --arg base_ref "${base_ref}" \
      '{
        issue_number: ($issue_number | tonumber? // null),
        local_id: (if $local_id == "" then null else $local_id end),
        phase: (if $phase == "" then null else $phase end),
        stall_minutes: $stall_minutes,
        recovery_count: $recovery_count,
        linked_pr: {
          number: (if $target_pr == "" then null else ($target_pr | tonumber? // null) end),
          state: (if $pr_state == "" then null else $pr_state end),
          mergeable: (if $pr_mergeable == "" then null else ($pr_mergeable == "true") end),
          head_ref: (if $head_ref == "" then null else $head_ref end),
          base_ref: (if $base_ref == "" then null else $base_ref end)
        },
        diagnostics_build_failed: true
      }' 2>/dev/null || echo '{"diagnostics_build_failed":true}')"
  fi

  local stall_judge_prompt_file="${RUNTIME_DIR}/stall_judge_prompt_${issue_num}.txt"
  local stall_judge_output_file="${RUNTIME_DIR}/stall_judge_output_${issue_num}.txt"
  local stall_judge_semble_query_file="${RUNTIME_DIR}/stall_judge_semble_query_${issue_num}.txt"
  local stall_judge_semble_prefetch=""
  local static_file="${RUNTIME_DIR}/judge_static.txt"

  {
    printf '%s\n' 'Stall recovery judge context.'
    append_judge_semble_query_text "Issue summary:" "issue #${issue_num}; local id ${local_id}; phase ${phase}; stall minutes ${stall_minutes}; recovery count ${recovery_count}; fallback action ${fallback_action}" 900
    append_judge_semble_query_text "Linked PR summary:" "target_pr ${target_pr}; pr_state ${pr_state}; mergeable ${pr_mergeable}; head_ref ${head_ref}; base_ref ${base_ref}" 900
    append_judge_semble_query_text "Diagnostics JSON:" "${diagnostics}" 7000
  } > "${stall_judge_semble_query_file}"
  stall_judge_semble_prefetch="$(render_judge_semble_prefetch_from_query_file "${stall_judge_semble_query_file}" "Stall Judge Context")"

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
    SEMBLE_PREFETCH="${stall_judge_semble_prefetch}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt
    echo
    echo "=== STALL DIAGNOSTICS JSON ==="
    echo
    echo "${diagnostics}"
  } > "${stall_judge_prompt_file}"

  # Centralised in scripts/write_codex_config.sh.
  bash scripts/write_codex_config.sh \
    --model "${MODEL_EDITOR}" \
    --reasoning "${MODEL_REASONING_EFFORT_JUDGE:-xhigh}"

  local judge_success="false"
  local attempt
  local _judge_from_cache="false"
  if [ -n "${_judge_cache_hit_action}" ] && [ "${_judge_force_escalate}" = "true" ]; then
    echo "  [stall-judge] Cache replay cap reached for issue #${issue_num} (key=${_judge_cache_key}, replays=${_judge_cache_replay_count} >= ${MAX_JUDGE_REPLAY}); forcing escalate_human."
    local _judge_escalate_justification
    _judge_escalate_justification="Cached judge decision '${_judge_cache_hit_action}' replayed ${_judge_cache_replay_count} times without progress; bypassing cache and escalating."
    jq -cn --arg a "escalate_human" --arg j "${_judge_escalate_justification}" '{action:$a, justification:$j}' > "${stall_judge_output_file}"
    judge_success="true"
    _judge_from_cache="true"
  elif [ -n "${_judge_cache_hit_action}" ]; then
    echo "  [stall-judge] Cache hit for issue #${issue_num} (key=${_judge_cache_key}); replaying cached decision '${_judge_cache_hit_action}' (replay $((_judge_cache_replay_count + 1)) of max ${MAX_JUDGE_REPLAY})."
    local _judge_replay_justification
    _judge_replay_justification="Cached judge decision (input hash unchanged across cycles); replay $((_judge_cache_replay_count + 1)) of ${MAX_JUDGE_REPLAY}."
    jq -cn --arg a "${_judge_cache_hit_action}" --arg j "${_judge_replay_justification}" '{action:$a, justification:$j}' > "${stall_judge_output_file}"
    judge_success="true"
    _judge_from_cache="true"
    local _judge_replay_next=$(( _judge_cache_replay_count + 1 ))
    jq --arg k "${_judge_cache_key}" --argjson n "${_judge_replay_next}" \
      '.judge_decision_cache = ((.judge_decision_cache // {}) | .[$k].replay_count = $n)' \
      "${_judge_state_file}" > "${_judge_state_file}.tmp" 2>/dev/null && mv "${_judge_state_file}.tmp" "${_judge_state_file}" || rm -f "${_judge_state_file}.tmp" 2>/dev/null || true
  else
    for attempt in 1 2; do
      if [ -n "${MOCK_STALL_JUDGE_JSON:-}" ]; then
        printf '%s\n' "${MOCK_STALL_JUDGE_JSON}" > "${stall_judge_output_file}"
      else
        sanitize_codex_prompt_file "${stall_judge_prompt_file}"
        codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${stall_judge_prompt_file}" > "${stall_judge_output_file}" 2>> "${RUNTIME_DIR}/stall_judge.log" || true
      fi
      if grep -q '[^[:space:]]' "${stall_judge_output_file}"; then
        judge_success="true"
        break
      fi
      sleep $(( 8 * attempt ))
    done
  fi

  rm -f "${stall_judge_semble_query_file}"

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

  # Consecutive-judge-escalation backstop.  When the judge deliberately and
  # repeatedly chooses escalate_human, honor it even while
  # ENABLE_STALL_HUMAN_TERMINALIZATION is off — otherwise
  # resolve_effective_stall_recovery_action downgrades escalate_human back to
  # the ladder action (e.g. ai:done -> retrigger_review) on every tick and the
  # issue loops forever without ever surfacing to a human.
  #
  # The input-hash replay cap (_judge_force_escalate, above) is meant to catch
  # this, but its judge cache key includes head_sha and the retrigger_review
  # action pushes an empty commit each cycle (see the empty-commit push in
  # execute_stall_recovery_action).  That churns head_sha, changes the cache
  # key every tick, and keeps replay_count pinned at 0 — so the input-hash
  # backstop never engages against a head_sha-churning action.  This streak
  # counter keys off the judge's *decision* instead of the input hash, so
  # head_sha churn cannot defeat it.  Honoring escalate_human is benign: the
  # escalate_human handler only adds ai:needs-human (the highest-priority phase
  # label, which pauses further stall recovery for the issue) and sends a
  # CRITICAL notification — it never closes or discards the PR.
  #
  # Streak persists in the judge state file keyed by issue; it resets to 0 whenever the
  # judge picks any non-escalate action so a one-off escalation cannot latch.
  # Threshold reuses MAX_JUDGE_REPLAY for parity with the input-hash cap.
  local _judge_escalate_streak=0
  if [ "${_judge_state_file_ready}" = "true" ]; then
    if [ "${judge_action}" = "escalate_human" ]; then
      _judge_escalate_streak="$(jq -r --arg n "${issue_num}" '.judge_escalate_streak[$n] // 0' "${_judge_state_file}" 2>/dev/null || echo "0")"
      [[ "${_judge_escalate_streak}" =~ ^[0-9]+$ ]] || _judge_escalate_streak=0
      _judge_escalate_streak=$(( _judge_escalate_streak + 1 ))
      jq --arg n "${issue_num}" --argjson v "${_judge_escalate_streak}" \
        '.judge_escalate_streak = ((.judge_escalate_streak // {}) | .[$n] = $v)' \
        "${_judge_state_file}" > "${_judge_state_file}.tmp" 2>/dev/null && mv "${_judge_state_file}.tmp" "${_judge_state_file}" || rm -f "${_judge_state_file}.tmp" 2>/dev/null || true
    else
      jq --arg n "${issue_num}" \
        '.judge_escalate_streak = ((.judge_escalate_streak // {}) | del(.[$n]))' \
        "${_judge_state_file}" > "${_judge_state_file}.tmp" 2>/dev/null && mv "${_judge_state_file}.tmp" "${_judge_state_file}" || rm -f "${_judge_state_file}.tmp" 2>/dev/null || true
    fi
  fi

  local effective_action
  if [ "${judge_action}" = "escalate_human" ] && \
     { [ "${_judge_force_escalate}" = "true" ] || [ "${_judge_escalate_streak}" -ge "${MAX_JUDGE_REPLAY}" ]; }; then
    effective_action="escalate_human"
  else
    effective_action="$(normalize_stall_recovery_action "${phase}" "${recovery_count}" "${judge_action}")"
  fi
  STALL_JUDGE_TARGET_PR="$(echo "${judge_json}" | jq -r '.target_pr // empty')"
  if [ -z "${STALL_JUDGE_TARGET_PR}" ] && [[ "${target_pr}" =~ ^[0-9]+$ ]]; then
    STALL_JUDGE_TARGET_PR="${target_pr}"
  fi
  STALL_JUDGE_HEAD_REF="$(echo "${judge_json}" | jq -r '.head_ref // empty')"
  if [ -z "${STALL_JUDGE_HEAD_REF}" ] && [ -n "${head_ref}" ]; then
    STALL_JUDGE_HEAD_REF="${head_ref}"
  fi

  # Cache the fresh judge decision so future identical inputs replay it
  # rather than burning another LLM call. Skip when this run was itself
  # served from the cache (replay count was already bumped above).
  if [ "${_judge_from_cache}" = "false" ] && [ -n "${_judge_cache_key}" ] && [ -n "${judge_action}" ] && [ "${_judge_state_file_ready}" = "true" ]; then
    jq --arg k "${_judge_cache_key}" --arg action "${judge_action}" \
      '.judge_decision_cache = ((.judge_decision_cache // {}) | .[$k] = {"action": $action, "replay_count": 0})' \
      "${_judge_state_file}" > "${_judge_state_file}.tmp" 2>/dev/null && mv "${_judge_state_file}.tmp" "${_judge_state_file}" || rm -f "${_judge_state_file}.tmp" 2>/dev/null || true
  fi

  local judge_comment
  judge_comment="## 🧑‍⚖️ Stall Judge — Issue #${issue_num} attempt $((recovery_count + 1))

**Decision (judge):** ${judge_action}
**Decision (effective):** ${effective_action}
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
  tg_notify "Stall judge evaluated issue #${issue_num}: judged=${judge_action}, effective=${effective_action}. ${judge_justification}"$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"

  if [ "${effective_action}" != "${judge_action}" ]; then
    echo "::notice::Stall judge action '${judge_action}' normalized to '${effective_action}' for issue #${issue_num}."
  fi

  case "${effective_action}" in
    retrigger_pipeline|auto_respond_clarify|retrigger_plan|auto_approve|retrigger_implement|retrigger_review|attempt_merge|close_and_reissue|escalate_human|skip)
      execute_stall_recovery_action "${issue_num}" "${phase}" "${effective_action}" "${recovery_count}" "${local_id}" "${stall_minutes}"
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
      echo "::warning::Unknown effective stall action '${effective_action}' for issue #${issue_num}; falling back to ${fallback_action}."
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

# Batch-fetch labels, recent comments and latest-linked-PR state for a
# list of candidate issue numbers via a single GraphQL query per batch
# (aliased issue selectors).  Replaces the per-candidate REST /labels,
# /comments and /timeline round-trips with ceil(N / batch_size) GraphQL
# calls.  Returns a JSON object keyed by stringified issue number:
#   { "123": {"state": "open|closed",
#             "labels": ["ai:clarification"],
#             "comments": [{"id":N,"body":"...","created_at":"..."},...],
#             "linked_pr": {"number":N,"state":"OPEN|CLOSED|MERGED","merged":bool,
#                           "merged_at":"ISO8601"|null,"merge_commit_sha":"<oid>"|null,
#                           "head_ref":"branch"|null,"head_sha":"<oid>"|null,
#                           "mergeable":"<enum>"|null,"merge_state_status":"<enum>"|null,
#                           "headPushedAt":"ISO8601"|null} | null },
#     ... }
# `headPushedAt` is the linked PR's head commit pushedDate (coalesced
# to committedDate when pushedDate is null, e.g. for squashed commits).
# `mergeable` and `merge_state_status` mirror GitHub's GraphQL enum strings.
# Consumed by the fresh-push stall-recovery guard (see
# _check_fresh_push_guard) to suppress recovery dispatches while
# autofix-driven activity is still landing on the PR.
# Comment shape mirrors the REST response (.id / .body / .created_at)
# so existing parsers (e.g. _extract_standalone_state_comment_id_from_comments)
# keep working unchanged.
#
# `linked_pr` is derived from the most recent CrossReferencedEvent
# whose source is a pull request AND whose `willCloseTarget` flag is
# true, so reference-only links (`Refs #N`) do not populate the
# stall-recovery/cache state.  It is consumed by the merged-PR
# stall-recovery guard (ENABLE_STALL_MERGED_PR_GUARD) to avoid firing
# /reclarify (and friends) on issues whose work has already been
# merged.
#
# Trade-off: we fetch `comments(last: 100)` which covers only the 100
# newest comments rather than the full pagination walk the REST path
# did.  In practice the standalone-state marker comment is written
# every poll cycle (so it's always in the recent window), and the
# ai:clarification-questions marker is added near the top of the
# clarification phase before high comment counts accrue.  If an issue
# drifts past 100 comments *and* has an ancient marker, the
# label-based detection path still catches it.  Similarly
# `timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT])` covers
# the 50 most-recent cross-references, which is plenty for AI pipeline
# issues that rarely accumulate more than a handful.
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
          state
          labels(first: 50) { nodes { name } }
          comments(last: 100) { nodes { databaseId body createdAt } }
          timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT]) {
            nodes {
              ... on CrossReferencedEvent {
                willCloseTarget
                source {
                  __typename
                  ... on PullRequest {
                    number state merged
                    mergedAt
                    headRefName
                    headRefOid
                    mergeable
                    mergeStateStatus
                    mergeCommit { oid }
                    commits(last: 1) { nodes { commit { pushedDate committedDate } } }
                  }
                }
              }
            }
          }
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
            state: (((.value.state // "OPEN") | ascii_downcase) | if . == "closed" then "closed" else "open" end),
            labels: [(.value.labels.nodes // [])[]?.name],
            comments: [(.value.comments.nodes // [])[]? | {
              id: .databaseId,
              body: .body,
              created_at: .createdAt
            }],
            linked_pr: (
              [
                (.value.timelineItems.nodes // [])[]?
                | select(.willCloseTarget == true and (.source // null) != null)
                | .source
                | select(.__typename == "PullRequest")
                  | {
                      number: .number,
                      state: .state,
                      merged: (.merged // false),
                      merged_at: (.mergedAt // null),
                      merge_commit_sha: (.mergeCommit.oid // null),
                      head_ref: (.headRefName // null),
                      head_sha: (.headRefOid // null),
                      mergeable: (.mergeable // null),
                      merge_state_status: (.mergeStateStatus // null),
                      headPushedAt: (
                      ((.commits.nodes // [])[0].commit.pushedDate)
                      // ((.commits.nodes // [])[0].commit.committedDate)
                      // null
                    )
                  }
              ] | last // null
            )
          }
        }
      ) | from_entries
    ' 2>/dev/null || echo '{}')"

    merged="$(jq -s '.[0] * .[1]' <(printf '%s\n' "${merged}") <(printf '%s\n' "${batch_transformed}") 2>/dev/null || echo "${merged}")"

    start="${end}"
  done

  echo "${merged}"
}

# _fetch_linked_pr_status_graphql — Batch-fetch latest-linked-PR state
# for a list of issue numbers via a single GraphQL query per batch.
# Lighter than _fetch_candidate_issue_details_graphql (only timeline
# items, no labels/comments) and used by the orchestrator-managed
# stall recovery loop, which already has its own label/state source
# of truth.
#
# Input: JSON array of issue numbers, e.g. "[123, 456]"
# Output: JSON object keyed by stringified issue number:
#   { "123": {"number":N,"state":"OPEN|CLOSED|MERGED","merged":bool,"headPushedAt":"ISO8601"|null},
#     "456": null, ... }
# `headPushedAt` is the linked PR's head commit pushedDate (coalesced
# to committedDate when pushedDate is null).  Consumed by
# _check_fresh_push_guard alongside the merged-PR guard.
# A value of `null` means the issue has no cross-referenced PR (or the
# batch call failed for that batch and the issue fell out of the cache).
# Callers must treat missing/null entries as "no merged PR known" and
# proceed normally — the guard fails open.
_fetch_linked_pr_status_graphql() {
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
          timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT]) {
            nodes {
              ... on CrossReferencedEvent {
                willCloseTarget
                source {
                  __typename
                  ... on PullRequest {
                    number state merged
                    repository { nameWithOwner }
                    commits(last: 1) { nodes { commit { pushedDate committedDate } } }
                  }
                }
              }
            }
          }
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
      # Fail open: leave this batch's issues out of the cache so the
      # caller treats them as "no merged PR known" and proceeds.
      start="${end}"
      continue
    fi

    batch_transformed="$(printf '%s' "${batch_resp}" | jq -c --arg repo "${owner}/${name}" '
      (.data.repository // {}) | to_entries | map(
        select(.value != null and (.value.number? != null)) | {
          key: (.value.number | tostring),
          value: (
            [
              (.value.timelineItems.nodes // [])[]?
              | select(.willCloseTarget == true and (.source // null) != null)
              | .source
              | select(.__typename == "PullRequest")
              | select((.repository.nameWithOwner // "") == $repo)
              | {
                  number: .number,
                  state: .state,
                  merged: (.merged // false),
                  headPushedAt: (
                    ((.commits.nodes // [])[0].commit.pushedDate)
                    // ((.commits.nodes // [])[0].commit.committedDate)
                    // null
                  )
                }
            ] | last // null
          )
        }
      ) | from_entries
    ' 2>/dev/null || echo '{}')"

    merged="$(jq -s '.[0] * .[1]' <(printf '%s\n' "${merged}") <(printf '%s\n' "${batch_transformed}") 2>/dev/null || echo "${merged}")"

    start="${end}"
  done

  echo "${merged}"
}

# _single_issue_linked_pr_status_graphql — convenience wrapper around
# _fetch_linked_pr_status_graphql for cache-miss paths.  Returns the
# same per-issue JSON entry ({number,state,merged,headPushedAt} or
# null) while keeping the common path batched.
_single_issue_linked_pr_status_graphql() {
  local issue_num="$1"
  if ! [[ "${issue_num}" =~ ^[0-9]+$ ]]; then
    echo "null"
    return
  fi

  local _single_resp
  _single_resp="$(_fetch_linked_pr_status_graphql "[$issue_num]")"
  printf '%s' "${_single_resp}" | jq -c --arg n "${issue_num}" '.[$n] // null' 2>/dev/null || echo "null"
}

# _pr_json_closes_issue — conservative closing-keyword check used only
# after the authoritative GraphQL linked-PR lookups missed.  Return
# codes: 0=yes, 1=no, 2=unknown/error.  Unknown preserves the candidate
# PR so guards fail closed on transient payload/parse problems.
_pr_json_closes_issue() {
  local issue_num="$1"
  local pr_json="$2"

  if ! [[ "${issue_num}" =~ ^[0-9]+$ ]]; then
    return 2
  fi
  if [ -z "${pr_json}" ] || [ "${pr_json}" = "{}" ]; then
    return 2
  fi

  local _rc=0
  printf '%s' "${pr_json}" | jq -e --arg n "${issue_num}" '
    if type != "object" then
      false
    else
      (.body // "") as $body
      # Keep this conservative REST fallback aligned with
      # scripts/lint_pr_body_auto_close.py: no colon forms, no keyword
      # substrings inside larger words, and support short + URL refs.
      | ($body | test(
          "(?i)(^|[^[:alnum:]_-])"
          + "(close[sd]?|fix(es|ed)?|resolve[sd]?)"
          + "[[:space:]]+"
          + "(#"
          + $n
          + "|[[:alnum:]_.-]+/[[:alnum:]_.-]+#"
          + $n
          + "|https?://github\\.com/[[:alnum:]_.-]+/[[:alnum:]_.-]+/issues/"
          + $n
          + ")([^[:alnum:]_-]|$)"
        ))
    end
  ' >/dev/null 2>&1
  _rc=$?
  if [ "${_rc}" -eq 0 ]; then
    return 0
  fi

  if [ "${_rc}" -eq 1 ]; then
    return 1
  fi
  return 2
}

# _check_merged_pr_guard — Shared guard used by both the standalone and
# orchestrator-managed stall recovery paths.  Given an issue number and
# a pre-fetched linked-PR JSON object (from either
# _fetch_candidate_issue_details_graphql or
# _fetch_linked_pr_status_graphql), returns 0 if the linked PR is
# merged (caller should skip the stall action) and 1 otherwise.
#
# On a hit, exports STALL_MERGED_PR_NUM with the PR number so the
# caller can log + notify + reconcile labels.
#
# The guard is feature-flagged via ENABLE_STALL_MERGED_PR_GUARD and
# fails open (returns 1) if the guard is disabled or the linked_pr
# payload is missing/empty — i.e. the guard NEVER causes a stall
# recovery to fire when it otherwise wouldn't have.
#
# Args:
#   $1 — issue number (for logging only)
#   $2 — linked_pr JSON (shape: {"number":N,"state":"MERGED","merged":bool} or "null"/empty)
_check_merged_pr_guard() {
  local issue_num="$1"
  local linked_json="$2"
  STALL_MERGED_PR_NUM=""

  if [ "${ENABLE_STALL_MERGED_PR_GUARD}" != "true" ]; then
    return 1
  fi

  if [ -z "${linked_json}" ] || [ "${linked_json}" = "null" ] || [ "${linked_json}" = "{}" ]; then
    return 1
  fi

  local merged_flag
  merged_flag="$(printf '%s' "${linked_json}" | jq -r '.merged // false' 2>/dev/null || echo "false")"
  if [ "${merged_flag}" != "true" ]; then
    return 1
  fi

  local pr_num
  pr_num="$(printf '%s' "${linked_json}" | jq -r '.number // empty' 2>/dev/null || echo "")"
  if ! [[ "${pr_num}" =~ ^[0-9]+$ ]]; then
    return 1
  fi

  STALL_MERGED_PR_NUM="${pr_num}"
  return 0
}

# _check_fresh_push_guard — Shared guard used by both stall recovery
# paths.  Returns 0 when the linked PR's head commit was pushed within
# the last _FRESH_PUSH_SUPPRESS_SECS (50 minutes, hardcoded) AND the
# phase is one where autofix-driven commits are expected
# (ai:done, ai:ready-to-merge).  Returns 1 otherwise.
#
# Rationale: the existing `issue_has_active_workflow` guard only
# catches cycles where a queued/in_progress workflow run is currently
# visible on the PR branch.  It misses the race where autofix just
# pushed a commit but the new `pull_request.synchronize` run hasn't
# materialised yet (or was cancelled by the autofix-retrigger dedup
# and the follow-on dispatch is still in-flight).  A fresh pushedDate
# is a more reliable "work landed recently" signal.
#
# The window is deliberately not configurable (per project decision
# Q2=B on issue investigate-stall-recovery-dx7zm — a tunable knob
# would invite consumers to widen it indefinitely and mask hung
# autofix loops).  The earlier 30-minute default originally chosen
# there predated typical review_autofix cycle times of 35-45 minutes
# observed on busy consumer repos (e.g. tele-funtoken-msg-scoring
# PRs #3057, #3062), so a single cycle outlasted that old window and
# the guard never fired between cycles.  The shipped 50-minute window
# fits one full autofix cycle; longer windows would start hiding
# genuinely hung loops.
#
# Fails open (returns 1 — guard does NOT fire) when:
#   - phase is outside {ai:done, ai:ready-to-merge}
#   - linked_json is empty / "null" / "{}"
#   - headPushedAt is missing or unparseable
#   - computed age is negative (clock skew)
# i.e. the guard NEVER causes a stall recovery to fire when it
# otherwise would not have; it only suppresses dispatches that would
# have fired within the fresh-push window.
#
# On a hit, exports FRESH_PUSH_PR_NUM and FRESH_PUSH_AGE_SECS so the
# caller can emit a stable `STALL_SKIP reason=fresh_push` log line.
#
# Args:
#   $1 — issue number (for logging only)
#   $2 — linked_pr JSON entry ({number,state,merged,headPushedAt} or "null"/empty)
#   $3 — phase label (e.g. "ai:done")
_FRESH_PUSH_SUPPRESS_SECS=3000
_check_fresh_push_guard() {
  local issue_num="$1"
  local linked_json="$2"
  local phase="$3"
  FRESH_PUSH_PR_NUM=""
  FRESH_PUSH_AGE_SECS=""

  case "${phase}" in
    "ai:done"|"ai:ready-to-merge") ;;
    *) return 1 ;;
  esac

  if [ -z "${linked_json}" ] || [ "${linked_json}" = "null" ] || [ "${linked_json}" = "{}" ]; then
    return 1
  fi

  local pushed_at_iso
  pushed_at_iso="$(printf '%s' "${linked_json}" | jq -r '.headPushedAt // empty' 2>/dev/null || echo "")"
  [ -n "${pushed_at_iso}" ] || return 1

  local pushed_at_epoch
  pushed_at_epoch="$(date -d "${pushed_at_iso}" +%s 2>/dev/null || echo "")"
  [[ "${pushed_at_epoch}" =~ ^[0-9]+$ ]] || return 1

  local now_epoch
  now_epoch="$(date +%s)"
  local age_secs=$(( now_epoch - pushed_at_epoch ))
  [ "${age_secs}" -lt 0 ] && return 1

  if [ "${age_secs}" -lt "${_FRESH_PUSH_SUPPRESS_SECS}" ]; then
    local pr_num
    pr_num="$(printf '%s' "${linked_json}" | jq -r '.number // empty' 2>/dev/null || echo "")"
    [[ "${pr_num}" =~ ^[0-9]+$ ]] || return 1
    FRESH_PUSH_PR_NUM="${pr_num}"
    FRESH_PUSH_AGE_SECS="${age_secs}"
    return 0
  fi

  return 1
}

# _check_fresh_push_guard_with_fallback — _check_fresh_push_guard hardened
# against a transiently empty issue→PR cross-reference.  Returns 0 (suppress
# the stall recovery for this cycle) when EITHER the primary cross-ref entry
# OR a deterministic `ai/issue-<n>` branch re-resolution shows the linked PR's
# head commit was pushed within the fresh-push window; returns 1 otherwise.
#
# Both stall paths feed this guard from the cross-reference timeline only,
# which can be suppressed (issue #2552 / PR #2568).  When that happens the
# primary entry has no headPushedAt and the inner guard fails open — the exact
# lock-step blindness that produced the false-positive ai:done recovery.  This
# wrapper adds the branch-name fallback (Layer 1 of the two-layer freshness
# fix; the detect_stalls re-anchor is Layer 2) so a missing cross-reference no
# longer blinds the guard.
#
# Sets the same FRESH_PUSH_PR_NUM / FRESH_PUSH_AGE_SECS exports as the inner
# guard, plus FRESH_PUSH_SOURCE ("cross_ref" | "branch_fallback") so callers
# can annotate their STALL_SKIP line.  The branch re-resolution (1 REST call)
# fires only for ai:done / ai:ready-to-merge issues whose primary entry lacked
# a usable headPushedAt, so the cache-hit path adds zero API calls (§15).
# Fail-open preserved end to end: any resolution failure leaves the recovery
# free to proceed exactly as before.
#
# Args: $1 issue number, $2 linked_pr JSON entry (or "null"/empty), $3 phase.
_check_fresh_push_guard_with_fallback() {
  local issue_num="$1"
  local linked_json="$2"
  local phase="$3"
  FRESH_PUSH_SOURCE="cross_ref"
  if _check_fresh_push_guard "${issue_num}" "${linked_json}" "${phase}"; then
    return 0
  fi
  case "${phase}" in
    "ai:done"|"ai:ready-to-merge") ;;
    *) return 1 ;;
  esac
  # Only re-resolve when the primary entry lacked a parseable headPushedAt; if
  # it carried one and was simply not fresh (a genuine stall), the branch
  # lookup would resolve the same PR and burn an API call for no benefit.
  local _primary_iso=""
  if [ -n "${linked_json}" ] && [ "${linked_json}" != "null" ] && [ "${linked_json}" != "{}" ]; then
    _primary_iso="$(printf '%s' "${linked_json}" | jq -r '.headPushedAt // empty' 2>/dev/null || echo "")"
  fi
  local _primary_epoch=""
  if [ -n "${_primary_iso}" ]; then
    _primary_epoch="$(date -d "${_primary_iso}" +%s 2>/dev/null || echo "")"
  fi
  [[ "${_primary_epoch}" =~ ^[0-9]+$ ]] && return 1
  local _fb_entry=""
  _fb_entry="$(_resolve_linked_pr_fresh_by_branch "${issue_num}")"
  local _fb_iso=""
  if [ -n "${_fb_entry}" ]; then
    _fb_iso="$(printf '%s' "${_fb_entry}" | jq -r '.headPushedAt // empty' 2>/dev/null || echo "")"
  fi
  # Non-silenced diagnostic so a recurrence is traceable — the primary cross-
  # ref fetch swallows its own failures via 2>/dev/null upstream.
  echo "STALL_FRESH_PUSH_FALLBACK issue=${issue_num} phase=${phase} source=branch_name resolved=${_fb_iso:-none}"
  if [ -n "${_fb_entry}" ] && _check_fresh_push_guard "${issue_num}" "${_fb_entry}" "${phase}"; then
    FRESH_PUSH_SOURCE="branch_fallback"
    return 0
  fi
  return 1
}

# _check_open_pr_conflict_guard — Detect whether the latest linked PR of a
# stalled issue is in a merge-conflict state (mergeable=false OR
# mergeStateStatus=DIRTY per Q2:A).  Used by run_standalone_stall_recovery
# to redirect retrigger_review → resolve_merge_conflict BEFORE the empty-
# commit push fires, because an autofix run on a conflicting branch cannot
# resolve the conflict and the next stall cycle will just repeat the loop
# until MAX_STALL_RECOVERIES_PER_ISSUE is hit.
#
# Inputs:
#   $1 — issue number (for logging only)
#   $2 — linked_pr JSON entry ({number,state,head_ref,mergeable,
#        merge_state_status,...}) from _candidate_details_json, or
#        "null"/"" when the cache missed.
#
# Output:
#   Return 0 when the PR is confirmed conflicted AND has a usable head_ref.
#   Exports STALL_CONFLICT_PR_NUM and STALL_CONFLICT_HEAD_REF on hit.
#   Return 1 otherwise (no conflict, cache miss without REST fallback,
#   missing head_ref).
#
# API calls: 0 on cache hit.  Callers may perform a REST fallback when the
# cache is empty — kept out of this helper so it stays side-effect free.
# GraphQL `mergeable` values: MERGEABLE | CONFLICTING | UNKNOWN.
# GraphQL `mergeStateStatus` values: CLEAN|DIRTY|BLOCKED|BEHIND|...
# REST `mergeable` values: true|false|null. REST `mergeable_state` values:
# clean|dirty|blocked|behind|unknown|unstable|has_hooks|draft.
# This helper accepts both representations so it can consume either cache
# shape or a REST-fallback payload rehydrated into the same field names.
_check_open_pr_conflict_guard() {
  local issue_num="$1"
  local linked_json="$2"
  STALL_CONFLICT_PR_NUM=""
  STALL_CONFLICT_HEAD_REF=""

  if [ -z "${linked_json}" ] || [ "${linked_json}" = "null" ] || [ "${linked_json}" = "{}" ]; then
    return 1
  fi

  local state
  state="$(printf '%s' "${linked_json}" | jq -r '.state // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
  # Only open PRs can be conflict-resolved.  Merged/closed short-circuit.
  if [ "${state}" != "open" ]; then
    return 1
  fi

  local mergeable
  local merge_state
  mergeable="$(printf '%s' "${linked_json}" | jq -r '.mergeable // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
  merge_state="$(printf '%s' "${linked_json}" | jq -r '(.merge_state_status // .mergeable_state // empty)' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"

  # Q2:A — treat either "REST mergeable=false" / "GraphQL CONFLICTING" OR
  # "mergeable_state/mergeStateStatus=dirty" as conflict.  Matches the
  # rebase-bot signal at line ~7241 and the managed-path guard at ~5946.
  local is_conflict="false"
  case "${mergeable}" in
    false|conflicting) is_conflict="true" ;;
  esac
  if [ "${merge_state}" = "dirty" ]; then
    is_conflict="true"
  fi
  if [ "${is_conflict}" != "true" ]; then
    return 1
  fi

  local pr_num
  local head_ref
  pr_num="$(printf '%s' "${linked_json}" | jq -r '.number // empty' 2>/dev/null || echo "")"
  head_ref="$(printf '%s' "${linked_json}" | jq -r '(.head_ref // .head.ref // .headRefName // empty)' 2>/dev/null || echo "")"
  [[ "${pr_num}" =~ ^[0-9]+$ ]] || return 1
  [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ] || return 1

  STALL_CONFLICT_PR_NUM="${pr_num}"
  STALL_CONFLICT_HEAD_REF="${head_ref}"
  return 0
}

# _reconcile_merged_pr_issue — Tag an issue whose linked PR is merged
# with ai:merged so close_merged_issues_sweep will close it on the
# next cycle, and emit a one-line healing note + Telegram alert.  Used
# by both stall recovery paths when _check_merged_pr_guard fires.
# Fails open: label-edit errors are swallowed so a transient label
# hiccup never blocks the stall recovery short-circuit.
_reconcile_merged_pr_issue() {
  local issue_num="$1"
  local phase="$2"
  local action="$3"
  local pr_num="$4"

  if declare -F ensure_label_exists >/dev/null 2>&1; then
    ensure_label_exists "ai:merged" >/dev/null 2>&1 || true
  fi
  gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
    --add-label "ai:merged" >/dev/null 2>&1 || true

  if declare -F add_healing_note >/dev/null 2>&1; then
    add_healing_note "Issue #${issue_num}: skipped stall recovery '${action}' (phase=${phase}) — linked PR #${pr_num} already merged; tagged ai:merged for close_merged_issues_sweep"
  fi
  if declare -F tg_notify >/dev/null 2>&1; then
    tg_notify "Stall recovery: skipped '${action}' for issue #${issue_num} (phase=${phase}) because linked PR #${pr_num} is already merged. Tagged ai:merged."$'\n'"Issue: $(_gh_url "issues/${issue_num}")"$'\n'"PR: $(_gh_url "pull/${pr_num}")" "WARNING"
  fi
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

  # ai:review-blocked is included so the standalone stall loop picks up
  # issues stuck at that phase (e.g. autofix workflow failure poisoning
  # the linked issue) and can dispatch the review-blocked judge via the
  # dispatch_rb_judge recovery action.  See STALL_RECOVERY_ACTIONS
  # ["ai:review-blocked"] in scripts/orchestrate_lib.py.
  local pipeline_labels='["ai:clarification","ai:planning","ai:awaiting-approval","ai:implementing","ai:done","ai:ready-to-merge","ai:review-blocked"]'
  local labeled_issues='[]'
  local lbl
  for lbl in ai:clarification ai:planning ai:awaiting-approval ai:implementing ai:done ai:ready-to-merge ai:review-blocked; do
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
  local phase_attempts_count
  local effective_max_recoveries
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

    # Phases the standalone loop does NOT act on.  ai:review-blocked was
    # historically in this skip list because it had a dedicated inline
    # handler (review_rb_judge.sh runs at the end of review_autofix.yml
    # when max_iterations is reached).  That handler has no standalone
    # trigger, so phases that land in ai:review-blocked without ever
    # invoking the judge (e.g. the empty-editor failure mode) never
    # escape.  ai:review-blocked is now recovered here via the
    # dispatch_rb_judge action (see execute_stall_recovery_action).
    if [ "${phase}" = "ai:needs-human" ] || [ "${phase}" = "ai:blocked" ] || [ "${phase}" = "ai:implementation-failed" ] || [ "${phase}" = "ai:validating" ] || [ "${phase}" = "ai:validation-fixing" ] || [ "${phase}" = "ai:merged" ] || [ "${phase}" = "ai:closed" ] || [ "${phase}" = "ai:validated" ] || [[ "${phase}" == ai:*-failed ]]; then
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
    recovery_count="$(echo "${updated_state}" | jq -r '.stall_recovery_count | tonumber? // 0')"
    phase_attempts_count="$(printf '%s' "${updated_state}" | jq -r --arg phase "${phase}" '.phase_attempts[$phase] | tonumber? // 0' 2>/dev/null || echo "0")"
    [[ "${phase_attempts_count}" =~ ^[0-9]+$ ]] || phase_attempts_count="0"
    effective_max_recoveries="${MAX_STALL_RECOVERIES_PER_ISSUE}"
    if [ "${phase}" = "ai:done" ]; then
      effective_max_recoveries="${MAX_STALL_RECOVERIES_DONE}"
    fi
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

    if [ "${recovery_count}" -ge "${effective_max_recoveries}" ] || [ "${phase_attempts_count}" -ge "${effective_max_recoveries}" ]; then
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

    # ---- Merged-PR + fresh-push guards ----
    # Check merged first so recently merged PRs are reconciled (ai:merged)
    # before fresh-push suppression can short-circuit this issue for the cycle.
    local _std_linked_json
    _std_linked_json="$(printf '%s' "${_candidate_details_json}" | jq -c --arg n "${issue_num}" '.[$n].linked_pr // null' 2>/dev/null || echo "null")"
    if [ -z "${_std_linked_json}" ] || [ "${_std_linked_json}" = "null" ] || [ "${_std_linked_json}" = "{}" ]; then
      # Cache miss — retry a narrow single-issue GraphQL lookup before
      # falling back to the legacy timeline/body heuristics.
      _std_linked_json="$(_single_issue_linked_pr_status_graphql "${issue_num}")"
    fi
    if _check_merged_pr_guard "${issue_num}" "${_std_linked_json}"; then
      echo "  [standalone-stall] Issue #${issue_num} linked PR #${STALL_MERGED_PR_NUM} is MERGED — skipping '${action}' and tagging ai:merged."
      _reconcile_merged_pr_issue "${issue_num}" "${phase}" "${action}" "${STALL_MERGED_PR_NUM}"
      if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
        write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
      fi
      continue
    fi

    # Fresh-push guard complements issue_has_active_workflow above; see
    # _check_fresh_push_guard for rationale. Consumes linked_pr already
    # populated in _candidate_details_json (0 additional API calls).
    if _check_fresh_push_guard_with_fallback "${issue_num}" "${_std_linked_json}" "${phase}"; then
      local _fp_src_suffix=""
      [ "${FRESH_PUSH_SOURCE:-cross_ref}" = "branch_fallback" ] && _fp_src_suffix=" source=branch_fallback"
      echo "  [standalone-stall] Issue #${issue_num} linked PR #${FRESH_PUSH_PR_NUM} was pushed ${FRESH_PUSH_AGE_SECS}s ago — skipping recovery (fresh push)."
      echo "STALL_SKIP issue=${issue_num} reason=fresh_push pr=${FRESH_PUSH_PR_NUM} pushed_age_secs=${FRESH_PUSH_AGE_SECS} phase=${phase} action=${action}${_fp_src_suffix}"
      if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
        write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
      fi
      continue
    fi

    # ---- Merged-PR guard: don't fire early-phase actions on issues whose
    # linked PR is already merged.  Primary path consumes the linked_pr
    # payload the batched GraphQL prefetch already put in
    # _candidate_details_json (0 additional API calls).  On cache miss
    # (batch failure, partial response, issue not in the cache) it falls
    # back to a per-issue REST probe — timeline + PR payload — so a
    # GraphQL/prefetch hiccup cannot silently regress the merged-PR
    # guard back into the /reclarify loop from GH issue #1074.  Mirrors
    # the REST-fallback merged handling added to the orchestrator-managed
    # path in recover_stalled_issue.  Fails open if both cache and REST
    # are empty — the stall action then runs as before.
    case "${action}" in
      retrigger_pipeline|auto_respond_clarify|retrigger_plan|auto_approve|retrigger_implement)
        # True cache miss — the batched lookup AND the single-issue
        # GraphQL retry both missed, so fall back to the legacy
        # timeline → PR-payload path.  Gated on the guard flag so
        # disabling it still gives full opt-out.
        if { [ -z "${_std_linked_json}" ] || [ "${_std_linked_json}" = "null" ]; } && [ "${ENABLE_STALL_MERGED_PR_GUARD}" = "true" ]; then
          local _std_lpr_num _std_lpr_json _std_lpr_merged _std_body_check_rc
          _std_lpr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
          if [[ "${_std_lpr_num}" =~ ^[0-9]+$ ]]; then
            _std_lpr_json="$(_fetch_pr_json "${_std_lpr_num}")"
            if _pr_json_closes_issue "${issue_num}" "${_std_lpr_json}"; then
              _std_body_check_rc=0
            else
              _std_body_check_rc=$?
            fi
            if [ "${_std_body_check_rc}" -eq 1 ]; then
              _std_lpr_num=""
            fi
            _std_lpr_merged="$(_jq_field "${_std_lpr_json}" '.merged_at != null' 'true|false')"
            if [ -n "${_std_lpr_num}" ] && [ "${_std_lpr_merged}" = "true" ]; then
              # Synthesise the same shape the cache would have produced
              # so _check_merged_pr_guard can consume it uniformly.
              _std_linked_json="$(jq -cn --argjson n "${_std_lpr_num}" '{number: $n, state: "MERGED", merged: true}' 2>/dev/null || echo "null")"
            fi
          fi
        fi
        if _check_merged_pr_guard "${issue_num}" "${_std_linked_json}"; then
          echo "  [standalone-stall] Issue #${issue_num} linked PR #${STALL_MERGED_PR_NUM} is MERGED — skipping '${action}' and tagging ai:merged."
          _reconcile_merged_pr_issue "${issue_num}" "${phase}" "${action}" "${STALL_MERGED_PR_NUM}"
          if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
            write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
          fi
          continue
        fi
        ;;
    esac

    # ---- Merge-conflict pre-dispatch guard (Q1:A, Q2:A, Q3:B) ----
    # When recovery_action_for_phase picks retrigger_review (ai:done phase),
    # the case-dispatch below pushes an empty commit to the PR head to
    # re-trigger Review Autofix.  If the PR already has merge conflicts
    # with base, that empty commit cannot resolve them — autofix runs on
    # the branch as-is and exits without progress, so the next stall
    # cycle repeats the same recovery until the attempt budget is burned.
    # Redirect to the conflict resolver workflow BEFORE dispatching.
    #
    # Primary data source: the linked_pr payload already resolved from
    # _candidate_details_json (enriched with head_ref, mergeable, and
    # merge_state_status in _fetch_candidate_issue_details_graphql) — 0
    # extra API calls on the fast path.  Cache miss triggers a single
    # REST fallback so a GraphQL hiccup cannot silently regress this
    # guard.  Fails open: when both cache and REST are empty the action
    # proceeds as before and the belt-and-braces check in
    # execute_stall_recovery_action retrigger_review catches it.
    #
    # Q3:B — this override does NOT increment STALL_RECOVERY_SHOULD_INCREMENT
    # so conflict resolution has its own budget and does not burn the
    # retrigger-style recovery allowance.
    if [ "${action}" = "retrigger_review" ]; then
      local _std_conflict_linked="${_std_linked_json}"
      # Cycle-local PR-JSON cache for this iteration.  When the REST
      # fallback below fetches the PR payload, stash it here so the
      # downstream retrigger_review case (when the guard fails open)
      # can reuse the JSON instead of issuing a second gh api call —
      # matches CLAUDE.md §15 GitHub API hygiene.
      local _STD_ITER_PR_NUM_CACHED=""
      local _STD_ITER_PR_JSON_CACHED=""
      _STD_ITER_PR_NUM_CACHED="$(printf '%s' "${_std_conflict_linked}" | jq -r '.number // empty' 2>/dev/null || echo "")"
      # API hygiene: when GraphQL already gave us PR number + head ref,
      # seed a minimal JSON payload so retrigger_review can skip a
      # redundant gh api pulls/{n} fetch on the non-retry fast path.
      local _std_cached_head_ref=""
      _std_cached_head_ref="$(printf '%s' "${_std_conflict_linked}" | jq -r '(.head_ref // .head.ref // .headRefName // empty)' 2>/dev/null || echo "")"
      if [[ "${_STD_ITER_PR_NUM_CACHED}" =~ ^[0-9]+$ ]] && [ -n "${_std_cached_head_ref}" ] && [ "${_std_cached_head_ref}" != "null" ]; then
        _STD_ITER_PR_JSON_CACHED="$(jq -cn --argjson n "${_STD_ITER_PR_NUM_CACHED}" --arg hr "${_std_cached_head_ref}" '{number: $n, head: {ref: $hr}}' 2>/dev/null || echo "")"
      fi

      # Widen the REST-fallback trigger: GitHub computes mergeability
      # asynchronously — a push kicks off a background job and the API
      # briefly returns mergeable=null / mergeable_state=unknown (REST)
      # or mergeable=UNKNOWN / mergeStateStatus=UNKNOWN (GraphQL).  If
      # the cache shows UNKNOWN we treat it as a cache miss and hit
      # REST with retries so we get a definitive signal before
      # deciding.  Without this widening, the guard fails open on
      # UNKNOWN and the legacy empty-commit dispatch runs — which
      # itself triggers another mergeable recomputation, perpetuating
      # the loop (observed for PR #1375/#1380 vs. settled PR #1413).
      local _std_conflict_should_retry="false"
      if [ -z "${_std_conflict_linked}" ] || [ "${_std_conflict_linked}" = "null" ] || [ "${_std_conflict_linked}" = "{}" ]; then
        _std_conflict_should_retry="true"
      else
        local _std_cache_mergeable _std_cache_merge_state
        _std_cache_mergeable="$(printf '%s' "${_std_conflict_linked}" | jq -r '.mergeable // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
        _std_cache_merge_state="$(printf '%s' "${_std_conflict_linked}" | jq -r '(.merge_state_status // .mergeable_state // empty)' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
        if [ -z "${_std_cache_mergeable}" ] || [ "${_std_cache_mergeable}" = "unknown" ] || [ -z "${_std_cache_merge_state}" ] || [ "${_std_cache_merge_state}" = "unknown" ]; then
          _std_conflict_should_retry="true"
        fi
      fi

      if [ "${_std_conflict_should_retry}" = "true" ]; then
        # Retry schedule per Q1:A + Q3:A (user-adjusted backoffs):
        # 5 attempts total (initial + 4 retries), sleeping 5,10,15,20s
        # between retries (50s worst case).  GitHub's API contract is
        # that a GET /pulls/{n} request kicks off mergeability
        # recomputation, so the subsequent retries are likely to
        # return the definitive state.  On all-attempts-still-unknown
        # we fail open and the legacy retrigger_review case fires,
        # reusing the cached PR JSON (no duplicate gh api call).
        local _std_conflict_pr_num_try _std_conflict_pr_json_try _std_attempt
        local _std_backoff_sleeps=(5 10 15 20)
        _std_conflict_pr_num_try="${_STD_ITER_PR_NUM_CACHED:-}"
        if ! [[ "${_std_conflict_pr_num_try}" =~ ^[0-9]+$ ]]; then
          _std_conflict_pr_num_try="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
        fi
        if [[ "${_std_conflict_pr_num_try}" =~ ^[0-9]+$ ]]; then
          for _std_attempt in 0 1 2 3 4; do
            _std_conflict_pr_json_try="$(_fetch_pr_json "${_std_conflict_pr_num_try}")"
            if [ -n "${_std_conflict_pr_json_try}" ] && [ "${_std_conflict_pr_json_try}" != "{}" ]; then
              _STD_ITER_PR_NUM_CACHED="${_std_conflict_pr_num_try}"
              _STD_ITER_PR_JSON_CACHED="${_std_conflict_pr_json_try}"
              _std_conflict_linked="$(printf '%s' "${_std_conflict_pr_json_try}" | jq -c '{
                number: (.number // null),
                state: (.state // null),
                head_ref: (.head.ref // null),
                head_sha: (.head.sha // null),
                mergeable: (if .mergeable == null then null else (.mergeable | tostring) end),
                merge_state_status: (.mergeable_state // null)
              }' 2>/dev/null || echo "null")"
              local _std_attempt_mergeable _std_attempt_merge_state
              _std_attempt_mergeable="$(printf '%s' "${_std_conflict_linked}" | jq -r '.mergeable // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
              _std_attempt_merge_state="$(printf '%s' "${_std_conflict_linked}" | jq -r '(.merge_state_status // .mergeable_state // empty)' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
              # Settled when either merge_state is already DIRTY
              # (conflict known even while mergeable is UNKNOWN) OR
              # mergeable is definitive (true|false) with a settled
              # merge_state_status (non-empty and non-unknown).
              if [ "${_std_attempt_merge_state}" = "dirty" ] || { { [ "${_std_attempt_mergeable}" = "true" ] || [ "${_std_attempt_mergeable}" = "false" ]; } && [ -n "${_std_attempt_merge_state}" ] && [ "${_std_attempt_merge_state}" != "unknown" ]; }; then
                echo "  [standalone-stall] Issue #${issue_num} PR #${_std_conflict_pr_num_try} mergeability settled on attempt $((_std_attempt + 1)): mergeable=${_std_attempt_mergeable} state=${_std_attempt_merge_state}"
                break
              fi
              echo "  [standalone-stall] Issue #${issue_num} PR #${_std_conflict_pr_num_try} mergeability still unsettled on attempt $((_std_attempt + 1)) (mergeable=${_std_attempt_mergeable:-null} state=${_std_attempt_merge_state:-unknown}); retrying..."
            else
              echo "  [standalone-stall] Issue #${issue_num} PR #${_std_conflict_pr_num_try} fetch failed on attempt $((_std_attempt + 1)); failing open."
              break
            fi
            if [ "${_std_attempt}" -lt 4 ]; then
              sleep "${_std_backoff_sleeps[${_std_attempt}]}"
            fi
          done
        fi
      fi

      if _check_open_pr_conflict_guard "${issue_num}" "${_std_conflict_linked}"; then
        local _std_conflict_head_sha=""
        local _std_override_count="0"
        local _std_next_count="0"
        local _std_consume_budget="false"
        _std_conflict_head_sha="$(printf '%s' "${_std_conflict_linked}" | jq -r '(.head_sha // .head.sha // .headRefOid // empty)' 2>/dev/null || echo "")"
        if [ -n "${_std_conflict_head_sha}" ] && [ "${_std_conflict_head_sha}" != "null" ]; then
          _std_override_count="$(printf '%s' "${updated_state}" | jq -r --arg sha "${_std_conflict_head_sha}" '.conflict_override_count[$sha] // 0' 2>/dev/null || echo "0")"
          [[ "${_std_override_count}" =~ ^[0-9]+$ ]] || _std_override_count="0"
          if [ "${_std_override_count}" -ge "${MAX_BUDGET_NEUTRAL_OVERRIDES}" ]; then
            echo "  [standalone-stall] Issue #${issue_num} PR #${STALL_CONFLICT_PR_NUM} head_sha ${_std_conflict_head_sha} has hit budget-neutral override cap (${_std_override_count} >= ${MAX_BUDGET_NEUTRAL_OVERRIDES}); consuming stall budget on this attempt."
            _std_consume_budget="true"
          fi
          _std_next_count=$(( _std_override_count + 1 ))
        fi
        local _std_conflict_rc=0
        _dispatch_review_for_conflicts "${STALL_CONFLICT_PR_NUM}" "${STALL_CONFLICT_HEAD_REF}" || _std_conflict_rc=$?
        case "${_std_conflict_rc}" in
          0)
            if [ -n "${_std_conflict_head_sha}" ] && [ "${_std_conflict_head_sha}" != "null" ]; then
              updated_state="$(printf '%s' "${updated_state}" | jq -c --arg sha "${_std_conflict_head_sha}" --argjson n "${_std_next_count}" '.conflict_override_count = ((.conflict_override_count // {}) | .[$sha] = $n)' 2>/dev/null || echo "${updated_state}")"
            fi
			if [ "${_std_consume_budget}" = "true" ]; then
			  updated_state="$(printf '%s' "${updated_state}" | jq -c --arg phase "${phase}" --argjson now "$(date +%s)" '
				.stall_recovery_count = ((.stall_recovery_count | tonumber? // 0) + 1)
				| .phase_attempts = (if (.phase_attempts | type) == "object" then .phase_attempts else {} end)
				| .phase_attempts[$phase] = ((.phase_attempts[$phase] | tonumber? // 0) + 1)
				| .status_since_ts = $now
				| .updated_ts = $now
			  ' 2>/dev/null || echo "${updated_state}")"
			fi
            echo "  [standalone-stall] Issue #${issue_num} linked PR #${STALL_CONFLICT_PR_NUM} has merge conflicts — dispatched conflict resolver instead of '${action}'."
            echo "STALL_RECOVERY issue=${issue_num} reason=open_pr_merge_conflict pr=${STALL_CONFLICT_PR_NUM} phase=${phase} action=dispatch_conflict_resolver override_from=${action}"
            tg_notify_issue "${issue_num}" "Standalone stall recovery: PR #${STALL_CONFLICT_PR_NUM} has merge conflicts — dispatched conflict resolver (phase=${phase}, stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
            if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
              write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
            fi
            continue
            ;;
          2)
            echo "  [standalone-stall] Issue #${issue_num} linked PR #${STALL_CONFLICT_PR_NUM} has merge conflicts but resolver already dispatched this cycle — skipping '${action}'."
            echo "STALL_SKIP issue=${issue_num} reason=open_pr_merge_conflict_dispatch_skipped pr=${STALL_CONFLICT_PR_NUM} phase=${phase} action=${action}"
            if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
              write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
            fi
            continue
            ;;
          *)
            echo "::warning::Standalone stall recovery: conflict-resolver dispatch failed for issue #${issue_num} PR #${STALL_CONFLICT_PR_NUM} rc=${_std_conflict_rc}; skipping '${action}' this cycle."
            echo "STALL_RECOVERY issue=${issue_num} reason=open_pr_merge_conflict_dispatch_failed pr=${STALL_CONFLICT_PR_NUM} phase=${phase} action=${action} rc=${_std_conflict_rc}"
            if [ -z "${state_comment_id}" ] || [ "${updated_state}" != "${state_json}" ]; then
              write_standalone_state_json "${issue_num}" "${updated_state}" "${state_comment_id}"
            fi
            continue
            ;;
        esac
      fi
    fi

    if [ "${action}" != "skip" ] && [ "${action}" != "attempt_merge" ] && [ "${action}" != "escalate_human" ]; then
      cancel_zombie_runs_for_issue "${issue_num}"
    fi

    local _std_phase_cap_state=""
    _std_phase_cap_state="$(phase_cap_state_for_action "${action}")"
    if [ -n "${_std_phase_cap_state}" ] && ! phase_cap_can_dispatch "${_std_phase_cap_state}" "${action}" "${issue_num}"; then
      echo "STALL_SKIP issue=${issue_num} reason=phase_capped phase=${phase} action=${action}"
      continue
    fi

    took_action="false"
    STALL_RECOVERY_SHOULD_INCREMENT="false"
    STALL_RECOVERY_EFFECTIVE_ACTION="${action}"

    case "${action}" in
      run_stall_judge)
        local _std_judge_state_file=""
        _std_judge_state_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/standalone_stall_judge_${issue_num}.XXXXXX.json" 2>/dev/null || true)"
        if [ -z "${_std_judge_state_file}" ]; then
          _std_judge_state_file="${TMPDIR:-/tmp}/standalone_stall_judge_${issue_num}.json"
        fi
        if ! printf '%s\n' "${updated_state}" > "${_std_judge_state_file}" 2>/dev/null; then
          rm -f "${_std_judge_state_file}" 2>/dev/null || true
          echo "::warning::[standalone-stall] could not seed standalone judge state file for issue #${issue_num}; judge streak persistence may be skipped this cycle." >&2
        fi
        STALL_JUDGE_STATE_FILE_OVERRIDE="${_std_judge_state_file}"
        if invoke_stall_judge "${issue_num}" "${phase}" "${recovery_count}" "${elapsed_minutes}" ""; then
          took_action="true"
          action="${STALL_RECOVERY_EFFECTIVE_ACTION:-run_stall_judge}"
        fi
        unset STALL_JUDGE_STATE_FILE_OVERRIDE
        if [ -f "${_std_judge_state_file}" ]; then
          updated_state="$(printf '%s' "${updated_state}" | jq -c --slurpfile judge_state "${_std_judge_state_file}" '.judge_escalate_streak = (($judge_state[0].judge_escalate_streak // {}) | if type == "object" then . else {} end)' 2>/dev/null || echo "${updated_state}")"
          rm -f "${_std_judge_state_file}" 2>/dev/null || true
        fi
        ;;
      retrigger_pipeline)
        local _std_retrigger_pipeline_rc=0
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/reclarify

_Standalone stall recovery: this issue did not enter the AI pipeline.
Re-triggering clarification._
STALL_EOF
)" >/dev/null 2>&1 || _std_retrigger_pipeline_rc=$?
        if [ "${_std_retrigger_pipeline_rc}" -eq 0 ]; then
          phase_cap_note_dispatch "ai:clarification"
        fi
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
        local _std_auto_respond_rc=0
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="${answer_body}" >/dev/null 2>&1 || _std_auto_respond_rc=$?
        if [ "${_std_auto_respond_rc}" -eq 0 ]; then
          phase_cap_note_dispatch "ai:planning"
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: auto-responded to clarification (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      retrigger_plan)
        local _std_retrigger_plan_rc=0
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/answer

_Standalone stall recovery: planning stalled. Re-triggering plan generation._
STALL_EOF
)" >/dev/null 2>&1 || _std_retrigger_plan_rc=$?
        if [ "${_std_retrigger_plan_rc}" -eq 0 ]; then
          phase_cap_note_dispatch "ai:planning"
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered planning (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      auto_approve)
        local _std_auto_approve_rc=0
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/approved

_Standalone stall recovery: plan approval stalled. Auto-approving to proceed._
STALL_EOF
)" >/dev/null 2>&1 || _std_auto_approve_rc=$?
        if [ "${_std_auto_approve_rc}" -eq 0 ]; then
          phase_cap_note_dispatch "ai:implementing"
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: auto-approved plan (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      retrigger_implement)
        local _std_retrigger_implement_rc=0
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/approved

_Standalone stall recovery: implementation stalled. Re-triggering implementation._
STALL_EOF
)" >/dev/null 2>&1 || _std_retrigger_implement_rc=$?
        if [ "${_std_retrigger_implement_rc}" -eq 0 ]; then
          phase_cap_note_dispatch "ai:implementing"
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered implementation (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      retrigger_review)
        local pr_num
        local pr_lookup_ok="false"
        local head_ref
        local head_sha
        local pr_json
        local _std_rtr_inflight_blob _std_rtr_inflight_id _std_rtr_direct_inflight_id _std_rtr_now_epoch _std_rtr_stall_secs _std_rtr_origin_head_sha _std_rtr_push_succeeded
        _std_rtr_push_succeeded="false"
        if [[ "${_STD_ITER_PR_NUM_CACHED:-}" =~ ^[0-9]+$ ]]; then
          pr_num="${_STD_ITER_PR_NUM_CACHED}"
          pr_lookup_ok="true"
        elif pr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null)"; then
          pr_lookup_ok="true"
        else
          pr_lookup_ok="false"
        fi
        if [ "${pr_lookup_ok}" = "true" ] && [[ "${pr_num}" =~ ^[0-9]+$ ]]; then
          # API hygiene (CLAUDE.md §15): if the merge-conflict guard
          # above fetched the same PR's JSON during its retry loop,
          # reuse that payload instead of issuing a redundant gh api
          # call.  _STD_ITER_PR_NUM_CACHED/_JSON_CACHED are iteration-
          # scoped and reset at the top of each iteration's guard
          # block, so stale cache carryover across issues cannot
          # happen.
          if [ "${_STD_ITER_PR_NUM_CACHED:-}" = "${pr_num}" ] && [ -n "${_STD_ITER_PR_JSON_CACHED:-}" ] && [ "${_STD_ITER_PR_JSON_CACHED}" != "{}" ]; then
            pr_json="${_STD_ITER_PR_JSON_CACHED}"
          else
            pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" 2>/dev/null || echo "")"
          fi
          head_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.ref?) then .head.ref else empty end' 2>/dev/null | tail -n1)"
          head_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' 2>/dev/null | tail -n1)"
          if [ -n "${head_ref}" ] && { [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; }; then
            # The standalone conflict guard seeds _STD_ITER_PR_JSON_CACHED
            # with a minimal {number, head.ref} payload to avoid a
            # redundant pulls/{n} fetch on the fast path.  The
            # retrigger-review empty-commit guard also needs head.sha for
            # workflow_dispatch matching and remote-head rechecks, so
            # refresh to the full PR payload only when the cached shape
            # omitted it.
            pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" 2>/dev/null || echo "${pr_json}")"
            head_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.ref?) then .head.ref else empty end' 2>/dev/null | tail -n1)"
            head_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' 2>/dev/null | tail -n1)"
          fi
          if [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ]; then
            # Mirror execute_stall_recovery_action(retrigger_review):
            # if issue_has_active_workflow misses a live review run on
            # this PR, an empty-commit push here can still trip the
            # stale-base gate and discard in-flight editor work.
			_std_rtr_inflight_blob="$(_load_actions_runs_cached 2>/dev/null || echo '{"workflow_runs":[]}')"
			_std_rtr_now_epoch="$(date +%s 2>/dev/null || echo "")"
			_std_rtr_stall_secs=$(( REVIEW_RUN_MAX_RUNTIME_MINUTES * 60 ))
			if [[ "${_std_rtr_now_epoch}" =~ ^[0-9]+$ ]]; then
			  _std_rtr_inflight_id="$(printf '%s' "${_std_rtr_inflight_blob}" | jq -r \
			    --arg br "${head_ref}" \
                --arg sha "${head_sha}" \
                --argjson now "${_std_rtr_now_epoch}" \
                --argjson threshold "${_std_rtr_stall_secs}" '
                [.workflow_runs[]?
                 | select((.status // "") == "in_progress" or (.status // "") == "queued")
                 | select(
                     ((.head_branch // "") == $br)
                     or ((.head_branch // "") == "" and $sha != "" and (.head_sha // "") == $sha)
                   )
                 | select(
                     (.name // "") == "AI Review"
                     or (.name // "") == "Internal Review"
                     or (.name // "") == "Review Autofix"
                     or (.name // "") == "Internal: AI Review & Autofix"
                     or (.name // "") == "Codex PR Self-Healing Semantic Agent"
                     or ((.path // "") | endswith("ai-review.yml"))
                     or ((.path // "") | endswith("internal-review.yml"))
                     or ((.path // "") | endswith("review_autofix.yml"))
                   )
                 | ([.run_started_at, .created_at]
                    | map(select(type == "string" and . != ""))[0] // "") as $ts
                 | (if $ts != ""
                    then (try ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch $now)
                    else $now
                    end) as $start_epoch
                 | select(($now - $start_epoch) < $threshold)
                ] | (.[0].id // empty)
              ' 2>/dev/null || echo "")"
            else
              _std_rtr_inflight_id=""
            fi
            # Cache-miss fallback (CLAUDE.md §15 fail-open): mirror
            # execute_stall_recovery_action — when the cached scan finds no live
            # review run, confirm against an authoritative branch-scoped listing
            # before the destructive empty-commit push.  Computed only on the
            # cache-miss path so the steady state adds zero API calls.
            _std_rtr_direct_inflight_id=""
            if [ -z "${_std_rtr_inflight_id}" ]; then
              _std_rtr_direct_inflight_id="$(_direct_inflight_review_run_on_branch "${head_ref}")"
            fi
			if [ -n "${_std_rtr_inflight_id}" ]; then
			  echo "  [standalone-stall] Issue #${issue_num} PR #${pr_num} has in-flight review run #${_std_rtr_inflight_id} on ${head_ref} (fresh, <${REVIEW_RUN_MAX_RUNTIME_MINUTES}m); skipping empty-commit push to avoid invalidating its stale-base gate."
			  STALL_RECOVERY_EFFECTIVE_ACTION="retrigger_review_skipped_inflight"
			elif [ -n "${_std_rtr_direct_inflight_id}" ]; then
              echo "  [standalone-stall] Issue #${issue_num} PR #${pr_num} has in-flight review run #${_std_rtr_direct_inflight_id} on ${head_ref} (direct check — cached scan missed it); skipping empty-commit push to avoid invalidating its stale-base gate."
              STALL_RECOVERY_EFFECTIVE_ACTION="retrigger_review_skipped_inflight"
            elif git fetch origin "${head_ref}:refs/remotes/origin/${head_ref}" 2>/dev/null; then
              _std_rtr_origin_head_sha="$(git rev-parse --verify "refs/remotes/origin/${head_ref}" 2>/dev/null || echo "")"
              if [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] && [[ "${_std_rtr_origin_head_sha}" =~ ^[0-9a-f]{40}$ ]] && \
                 [ "${_std_rtr_origin_head_sha}" != "${head_sha}" ]; then
                echo "  [standalone-stall] Issue #${issue_num} PR #${pr_num} head advanced from ${head_sha} to ${_std_rtr_origin_head_sha} after the PR-state snapshot; skipping empty-commit push to avoid racing newer review work."
              elif git checkout "origin/${head_ref}" 2>/dev/null; then
                git config user.name "codex-bot"
                git config user.email "codex@users.noreply.github.com"
                git commit --allow-empty -m "[standalone] stall recovery: re-trigger review for issue #${issue_num}" 2>/dev/null || true
                if git push origin "HEAD:${head_ref}" 2>/dev/null; then
                  _std_rtr_push_succeeded="true"
                fi
                git checkout --detach HEAD 2>/dev/null || true
              else
                echo "  [standalone-stall] Issue #${issue_num} PR #${pr_num} checkout origin/${head_ref} failed after fetch; skipping empty-commit push."
              fi
            fi
          fi
          if [ "${_std_rtr_push_succeeded}" = "true" ]; then
            tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered review flow (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
            STALL_RECOVERY_SHOULD_INCREMENT="true"
            took_action="true"
          elif [ "${STALL_RECOVERY_EFFECTIVE_ACTION}" != "retrigger_review_skipped_inflight" ]; then
            echo "  [standalone-stall] Issue #${issue_num} PR #${pr_num} empty-commit retrigger did not reach a successful push; skipping recovery increment."
          fi
        elif [ "${pr_lookup_ok}" = "true" ]; then
          gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
/approved

_Standalone stall recovery: issue marked done but no linked PR found. Re-triggering implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
          tg_notify_issue "${issue_num}" "Standalone stall recovery: re-triggered review flow (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
          STALL_RECOVERY_SHOULD_INCREMENT="true"
          took_action="true"
        fi
        ;;
      attempt_merge)
        local merge_pr
        merge_pr="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
        if [[ "${merge_pr}" =~ ^[0-9]+$ ]]; then
          local merge_pr_json
          local merge_state
          local merge_mergeable
          merge_pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${merge_pr}" 2>/dev/null || echo "")"
          merge_state="$(printf '%s' "${merge_pr_json}" | jq -r 'if (type == "object" and .state?) then .state else empty end' 2>/dev/null | tail -n1)"
          merge_mergeable="$(printf '%s' "${merge_pr_json}" | jq -r 'if (type == "object" and (.mergeable == true or .mergeable == false)) then .mergeable else empty end' 2>/dev/null | tail -n1)"
          if [ "${merge_state}" = "open" ] && [ "${merge_mergeable}" = "true" ] && _pr_checks_completed "${merge_pr}"; then
            gh_retry gh pr merge "${merge_pr}" --repo "${GITHUB_REPOSITORY}" --squash --auto >/dev/null 2>&1 \
              || gh_retry gh pr merge "${merge_pr}" --repo "${GITHUB_REPOSITORY}" --squash >/dev/null 2>&1 \
              || true
          fi
        fi
        tg_notify_issue "${issue_num}" "Standalone stall recovery: attempted merge retry for ready-to-merge issue (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      escalate_human)
        set_issue_phase_label "${issue_num}" "ai:needs-human" || true
        local escalated_labels
        escalated_labels="$(get_issue_labels_json "${issue_num}")"
        if has_label "${escalated_labels}" "ai:needs-human"; then
          gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<'STALL_EOF'
⚠️ Standalone stall recovery escalated this issue to human intervention.

The issue has been moved to `ai:needs-human`, and autonomous stall recovery is now paused for this issue.
To resume automation, remove `ai:needs-human` and apply the intended pipeline phase label.
STALL_EOF
)" >/dev/null 2>&1 || true
          tg_notify_issue "${issue_num}" "Standalone stall recovery escalated issue #${issue_num} to ai:needs-human after ${elapsed_minutes}m in '${phase}'." "WARNING"
        else
          echo "::warning::Standalone stall recovery could not verify ai:needs-human label on #${issue_num}; continuing bounded recovery retries." >&2
          tg_notify_issue "${issue_num}" "Standalone stall recovery attempted escalation for issue #${issue_num} after ${elapsed_minutes}m in '${phase}', but ai:needs-human could not be verified." "WARNING"
        fi
        STALL_RECOVERY_SHOULD_INCREMENT="true"
        took_action="true"
        ;;
      close_and_reissue)
        # Belt-and-braces ancestor-chain no-op cap. If the
        # "Re-issued from #N" chain already contains
        # MAX_IMPL_NOOP_REISSUES consecutive no-op ancestors, stop
        # spawning standalone re-issues and let the wave-completion
        # judge (or operator) decide — the work described is almost
        # certainly already on the integration branch.  Fails open:
        # on any API error count_noop_ancestors returns 0 and we
        # fall through to the legacy close+re-issue flow.
        local standalone_anc_noop_count
        standalone_anc_noop_count="$(count_noop_ancestors "${issue_num}" "${MAX_IMPL_NOOP_REISSUES:-2}")"
        [[ "${standalone_anc_noop_count}" =~ ^[0-9]+$ ]] || standalone_anc_noop_count=0
        if [ "${standalone_anc_noop_count}" -ge "${MAX_IMPL_NOOP_REISSUES:-2}" ]; then
          echo "  [standalone-stall-recovery] Ancestor-chain no-op cap reached for #${issue_num} (${standalone_anc_noop_count}/${MAX_IMPL_NOOP_REISSUES:-2}). Closing without re-issue — judge will verify."
          ensure_label_exists "ai:closed"
          close_linked_pr "${issue_num}" "Closed by standalone stall recovery — ancestor-chain no-op cap reached (${standalone_anc_noop_count}/${MAX_IMPL_NOOP_REISSUES:-2})."
          gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:done' --remove-label 'ai:implementing' --remove-label 'ai:planning' --remove-label 'ai:clarification' --remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
            --add-label 'ai:closed' 2>/dev/null || true
          gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
            -c "Closing: standalone stall recovery detected ${standalone_anc_noop_count} consecutive no-op ancestor(s) in the Re-issued from chain (cap ${MAX_IMPL_NOOP_REISSUES:-2}). The code described likely already exists on the integration branch; the wave-completion judge will verify." 2>/dev/null || true
          tg_notify_issue "${issue_num}" "Standalone stall recovery: ancestor-chain no-op cap hit for issue #${issue_num} (${standalone_anc_noop_count}/${MAX_IMPL_NOOP_REISSUES:-2}). Closed — judge will verify." "WARNING"
          took_action="true"
          STALL_RECOVERY_SHOULD_INCREMENT="false"
          # Skip the rest of this iteration's close_and_reissue
          # logic (which would spawn a replacement issue).  The
          # outer "took_action && action != close_and_reissue"
          # state-write block is already skipped for close_and_reissue.
          continue
        fi

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
          surface_reissue_closed_without_pr "${issue_num}" "${phase}" "${elapsed_minutes}" "${recovery_count}" "standalone"
          close_linked_pr "${issue_num}" "Closed by standalone stall recovery — issue #${issue_num} was stuck in '${phase}' for ${elapsed_minutes}m."
          ensure_label_exists "ai:closed"
          gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:done' --remove-label 'ai:implementing' --remove-label 'ai:planning' --remove-label 'ai:clarification' --remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
            --add-label 'ai:closed' 2>/dev/null || true
          gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" -c "Closing: standalone stall recovery. Issue was stuck in '${phase}' for ${elapsed_minutes} minutes after $((recovery_count + 1)) recovery attempt(s)." 2>/dev/null || true
          local new_state
		  new_state="$(printf '%s' "${updated_state}" | jq -c --argjson now "$(date +%s)" '
			.last_seen_phase = ""
			| .status_since_ts = $now
			| .stall_recovery_count = 0
			| .phase_attempts = {}
			| .judge_escalate_streak = {}
			| .updated_ts = $now
		  ' 2>/dev/null || echo "${updated_state}")"
		  write_standalone_state_json "${new_num}" "${new_state}" ""
		  tg_notify_issue "${issue_num}" "Standalone stall recovery: closed and re-issued as #${new_num} (phase: ${phase}, stuck ${elapsed_minutes}m)." "WARNING"
		else
		  echo "::warning::Standalone close_and_reissue failed to create replacement issue for #${issue_num}."
		  tg_notify_issue "${issue_num}" "Standalone stall recovery: attempted close-and-reissue but could not create replacement issue." "ERROR"
			  failed_reissue_state="$(printf '%s' "${updated_state}" | jq -c --arg phase "${phase}" --argjson now "$(date +%s)" '
				.stall_recovery_count = ((.stall_recovery_count | tonumber? // 0) + 1)
				| .phase_attempts = (if (.phase_attempts | type) == "object" then .phase_attempts else {} end)
				| .phase_attempts[$phase] = ((.phase_attempts[$phase] | tonumber? // 0) + 1)
				| .status_since_ts = $now
				| .updated_ts = $now
			  ' 2>/dev/null || echo "${updated_state}")"
		  write_standalone_state_json "${issue_num}" "${failed_reissue_state}" "${state_comment_id}"
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
      dispatch_rb_judge)
        # Autonomous escape from ai:review-blocked: dispatch the
        # standalone review-blocked judge workflow for the linked PR.
        # See execute_stall_recovery_action's dispatch_rb_judge case
        # for the authoritative implementation pattern; this mirrors
        # it within the standalone loop's switch.
        #
        # Linked-PR lookup uses the per-iteration _std_linked_json cache
        # (populated at the top of this loop iteration from the
        # batched GraphQL _candidate_details_json — same shape:
        # {number, state, merged, head_ref, ...}), NOT
        # _STD_ITER_PR_NUM_CACHED.  The latter is only declared inside
        # the `if action == retrigger_review` block above; because
        # bash `local` is function-scoped (not block-scoped), a prior
        # iteration's retrigger_review value would otherwise leak into
        # this iteration's dispatch_rb_judge and dispatch against the
        # wrong PR.  Using _std_linked_json avoids both the leak and
        # the redundant REST fallback.
        local _std_rb_pr_num=""
        _std_rb_pr_num="$(printf '%s' "${_std_linked_json:-null}" | jq -r '.number // empty' 2>/dev/null || echo "")"
        if ! [[ "${_std_rb_pr_num}" =~ ^[0-9]+$ ]]; then
          _std_rb_pr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
        fi
        if ! [[ "${_std_rb_pr_num}" =~ ^[0-9]+$ ]]; then
          # No linked PR — see execute_stall_recovery_action's
          # dispatch_rb_judge case for the same rationale: count this
          # as an attempt so the ladder can escalate to escalate_human
          # instead of looping forever.
          echo "::warning::[standalone-stall] dispatch_rb_judge: no linked PR found for issue #${issue_num}; counting as an attempt so the ladder can escalate."
          tg_notify_issue "${issue_num}" "Standalone stall recovery: dispatch_rb_judge could not find a linked PR (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1))). Counting as an attempt — will escalate to escalate_human at the end of the ladder." "WARNING"
          STALL_RECOVERY_SHOULD_INCREMENT="true"
        else
          local _std_rb_rc=0
	          _dispatch_rb_judge_for_pr "${_std_rb_pr_num}" "${issue_num}" || _std_rb_rc=$?
	          if [ "${_std_rb_rc}" -eq 0 ]; then
	            echo "STALL_RECOVERY issue=${issue_num} reason=ai_review_blocked pr=${_std_rb_pr_num} phase=${phase} action=dispatch_rb_judge"
	            tg_notify_issue "${issue_num}" "Standalone stall recovery: dispatched review-blocked judge for PR #${_std_rb_pr_num} (stuck ${elapsed_minutes}m, attempt $((recovery_count + 1)))." "WARNING"
	            STALL_RECOVERY_SHOULD_INCREMENT="true"
	          elif [ "${_std_rb_rc}" -eq 2 ]; then
	            # Already dispatched this cycle — judge is in flight.
	            echo "STALL_SKIP issue=${issue_num} reason=rb_judge_dispatch_deduped pr=${_std_rb_pr_num} phase=${phase} action=dispatch_rb_judge"
	            STALL_RECOVERY_SHOULD_INCREMENT="false"
	          elif [ "${_std_rb_rc}" -eq 3 ]; then
	            echo "STALL_SKIP issue=${issue_num} reason=phase_capped pr=${_std_rb_pr_num} phase=${phase} action=dispatch_rb_judge"
	            STALL_RECOVERY_SHOULD_INCREMENT="false"
	          else
	            echo "::warning::[standalone-stall] dispatch_rb_judge for issue #${issue_num} PR #${_std_rb_pr_num} failed (rc=${_std_rb_rc})."
	            STALL_RECOVERY_SHOULD_INCREMENT="false"
          fi
        fi
        took_action="true"
        ;;
      *)
        echo "::warning::Unknown standalone stall action ${action} for issue #${issue_num}"
        ;;
    esac

	    if [ "${took_action}" = "true" ] && [ "${action}" != "close_and_reissue" ]; then
	      updated_state="$(printf '%s' "${updated_state}" | jq -c --arg phase "${phase}" --arg should_increment "${STALL_RECOVERY_SHOULD_INCREMENT}" --argjson now "$(date +%s)" '
			(if ($should_increment | ascii_downcase) == "true" then
			  .stall_recovery_count = ((.stall_recovery_count | tonumber? // 0) + 1)
			  | .phase_attempts = (if (.phase_attempts | type) == "object" then .phase_attempts else {} end)
			  | .phase_attempts[$phase] = ((.phase_attempts[$phase] | tonumber? // 0) + 1)
			 else . end)
			| .status_since_ts = $now
			| .updated_ts = $now
	      ' 2>/dev/null || echo "${updated_state}")"
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

# Cap on consecutive poll cycles a post-Codex implementation-failed
# reissue may stay deferred with the same blocker status before being
# escalated to ai:needs-human. Prevents fallback-id-only blocker
# issues (which carry no actionable contract) from indefinitely
# parking the source issue while spamming WARNING notifications.
MAX_IMPL_FAILED_DEFER_CYCLES="${MAX_IMPL_FAILED_DEFER_CYCLES:-5}"
if ! [[ "${MAX_IMPL_FAILED_DEFER_CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
  MAX_IMPL_FAILED_DEFER_CYCLES=5
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
  # Trusted authors are "...[bot]" logins (GitHub Apps — installable only
  # by repo admins) OR OWNER/MEMBER/COLLABORATOR author_association (the
  # same trust list clarify.yml applies to /reclarify). Pipeline comments
  # are posted with the GH_PAT, so their author is the PAT owner's human
  # login with OWNER association — a bot-login-only filter excluded every
  # clarification comment and forced the "no recommended answers" path
  # (tele-funtoken-msg-scoring#3754 stall), while a body-marker-only
  # match would let any commenter spoof the marker and steer the
  # stall-recovery auto-answer.
  local clarify_body
  clarify_body="$(printf '%s' "${comments_json}" | jq -r '
    [ .[]
      | select(
          (
            (.user.login // "" | test("\\[bot\\]$")) or
            ((.author_association // "") | IN("OWNER", "MEMBER", "COLLABORATOR"))
          ) and
          ((.body // "") | test("<!-- ai:clarification-questions -->|^Clarification required"))
        )
    ]
    | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)])
    | .body // ""
  ')"

  if [ -z "${clarify_body}" ]; then
    echo ""
    return
  fi

  # Parse Q-blocks and pick the (RECOMMENDED) letter(s) for each.
  # When multiple options are recommended, combine with "+" (e.g. "A+C").
  # Canonical format per question:
  #   **Q1: <question>**
  #   Choices:
  #   - **A** — <desc> (RECOMMENDED)
  #   - **B** — <desc>
  # The bullet regex also tolerates LLM drift: an optional "-"/"*" bullet,
  # optional bold around the letter, and any of "—", "–", "-", ")", ".",
  # ":" as the separator between the letter and the description (so
  # "A. text (Recommended)" — the bullet-less drift from
  # tele-funtoken-msg-scoring#3754 — and "- A) text (Recommended)" are
  # accepted as well as "- **A** — text (RECOMMENDED)").
  printf '%s' "${clarify_body}" | perl -ne '
    BEGIN { @order = (); %rec = (); $qid = undef; }
    if (/^\s*\*?\*?Q(\d+)/i) {
      # New question block — flush previous if it had recommendations
      if (defined $qid && exists $rec{$qid}) {
        push @order, $qid unless grep { $_ eq $qid } @order;
      }
      $qid = $1;
    }
    if (defined $qid && /^\s*(?:[-*]\s*)?(?:\*\*)?([A-Za-z](?:\+[A-Za-z])*)(?:\*\*)?\s*(?:—|–|[-)\.:]).*\(RECOMMENDED\)/i) {
      push @{$rec{$qid}}, uc($1);
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

  # ---- Guards: merged PR first, then fresh push suppression ----
  # Covers the race where autofix just pushed a commit but the next
  # pull_request.synchronize workflow run hasn't materialised yet, so
  # issue_has_active_workflow momentarily returns false.  Scope is
  # limited to ai:done / ai:ready-to-merge phases (PR exists and may
  # still be receiving commits) — all other phases short-circuit
  # inside _check_fresh_push_guard.  Consumes the linked-PR entry the
  # outer recovery loop already populated in
  # STALL_MANAGED_LINKED_PR_CACHE via _fetch_linked_pr_status_graphql
  # (0 additional API calls).  Fails open when the cache is missing or
  # headPushedAt is unavailable.
  local _fresh_lpr_entry="null"
  if [ -n "${STALL_MANAGED_LINKED_PR_CACHE:-}" ]; then
    _fresh_lpr_entry="$(printf '%s' "${STALL_MANAGED_LINKED_PR_CACHE}" | jq -c --arg n "${issue_num}" '.[$n] // null' 2>/dev/null || echo "null")"
  fi
  if _check_merged_pr_guard "${issue_num}" "${_fresh_lpr_entry}"; then
    echo "STALL_SKIP issue=${issue_num} reason=merged_linked_pr pr=${STALL_MERGED_PR_NUM} phase=${phase} action=${action}"
    _reconcile_merged_pr_issue "${issue_num}" "${phase}" "${action}" "${STALL_MERGED_PR_NUM}"
    STALL_HEALING_CHANGED=true
    return 1  # Signal: no action taken (caller should not increment counter)
  fi
  if _check_fresh_push_guard_with_fallback "${issue_num}" "${_fresh_lpr_entry}" "${phase}"; then
    local _fp_src_suffix=""
    [ "${FRESH_PUSH_SOURCE:-cross_ref}" = "branch_fallback" ] && _fp_src_suffix=" source=branch_fallback"
    echo "STALL_SKIP issue=${issue_num} reason=fresh_push pr=${FRESH_PUSH_PR_NUM} pushed_age_secs=${FRESH_PUSH_AGE_SECS} phase=${phase} action=${action}${_fp_src_suffix}"
    return 1  # Signal: no action taken (caller should not increment counter)
  fi

  # ---- Guard: skip pre-implementation recovery when a linked PR already exists ----
  # Early-phase actions (retrigger_pipeline, auto_respond_clarify, retrigger_plan,
  # auto_approve, retrigger_implement) assume the issue has not yet produced a PR.
  # When phase labels are stale/missing (e.g. issue ends up with no ai:* labels even
  # though implementation already happened) the stall detector can misclassify the
  # phase as "no_labels"/"ai:clarification"/"ai:planning"/"ai:awaiting-approval"/
  # "ai:implementing" and fire /reclarify, /answer, /approved, etc. against an
  # issue that already has a PR (either still open OR already merged), triggering
  # a stuck-in-loop scenario like GH issue #1074.
  #
  # Two sub-guards:
  #   1. Merged-PR guard (ENABLE_STALL_MERGED_PR_GUARD): if the latest linked PR
  #      is MERGED, short-circuit + tag ai:merged so close_merged_issues_sweep
  #      closes the issue on the next cycle.
  #   2. Open-PR guard (legacy): if the latest linked PR is OPEN, short-circuit
  #      so the issue can progress on its existing PR.
  #
  # Both sub-guards first consult STALL_MANAGED_LINKED_PR_CACHE, which is
  # populated once per stall loop via a single batched GraphQL call
  # (_fetch_linked_pr_status_graphql).  On cache miss they fall back to the
  # legacy per-issue REST lookup — and the REST fallback also short-circuits
  # on merged PRs, so a GraphQL/prefetch failure cannot silently regress
  # the merged-PR guard back into the /reclarify loop.  Fail-open behaviour
  # preserved: when REST also fails, the stall action runs as before.
  case "${action}" in
    retrigger_pipeline|auto_respond_clarify|retrigger_plan|auto_approve|retrigger_implement)
      local _lpr_cache_entry=""
      if [ -n "${STALL_MANAGED_LINKED_PR_CACHE:-}" ]; then
        _lpr_cache_entry="$(printf '%s' "${STALL_MANAGED_LINKED_PR_CACHE}" | jq -c --arg n "${issue_num}" '.[$n] // null' 2>/dev/null || echo "null")"
      fi

      # --- Merged-PR sub-guard (uses cache only; fails open on miss) ---
      if _check_merged_pr_guard "${issue_num}" "${_lpr_cache_entry}"; then
        echo "STALL_SKIP issue=${issue_num} reason=merged_linked_pr pr=${STALL_MERGED_PR_NUM} phase=${phase} action=${action}"
        _reconcile_merged_pr_issue "${issue_num}" "${phase}" "${action}" "${STALL_MERGED_PR_NUM}"
        STALL_HEALING_CHANGED=true
        return 1  # Signal: no action taken (caller should not increment counter)
      fi

      if [ -z "${_lpr_cache_entry}" ] || [ "${_lpr_cache_entry}" = "null" ] || [ "${_lpr_cache_entry}" = "{}" ]; then
        _lpr_cache_entry="$(_single_issue_linked_pr_status_graphql "${issue_num}")"
        if _check_merged_pr_guard "${issue_num}" "${_lpr_cache_entry}"; then
          echo "STALL_SKIP issue=${issue_num} reason=merged_linked_pr pr=${STALL_MERGED_PR_NUM} phase=${phase} action=${action} source=single_issue_graphql"
          _reconcile_merged_pr_issue "${issue_num}" "${phase}" "${action}" "${STALL_MERGED_PR_NUM}"
          STALL_HEALING_CHANGED=true
          return 1  # Signal: no action taken (caller should not increment counter)
        fi
      fi

      # --- Open-PR sub-guard (uses cache first, then single-issue GraphQL,
      #     then falls back to per-issue REST) ---
      local _lpr_num=""
      local _lpr_state=""
      if [ -n "${_lpr_cache_entry}" ] && [ "${_lpr_cache_entry}" != "null" ]; then
        _lpr_num="$(printf '%s' "${_lpr_cache_entry}" | jq -r '.number // empty' 2>/dev/null || echo "")"
        # GraphQL PR state is uppercase (OPEN/CLOSED/MERGED); normalize to lower for the legacy compare.
        _lpr_state="$(printf '%s' "${_lpr_cache_entry}" | jq -r '.state // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
      fi
      if [ -z "${_lpr_num}" ]; then
        # Cache miss — fall back to the legacy per-issue REST lookup.
        _lpr_num="$(_issue_cross_ref_pr_number_last "${issue_num}" 2>/dev/null || echo "")"
        if [[ "${_lpr_num}" =~ ^[0-9]+$ ]]; then
          local _lpr_json=""
          local _lpr_merged=""
          local _lpr_body_check_rc=""
          _lpr_json="$(_fetch_pr_json "${_lpr_num}")"
          _lpr_state="$(_jq_field "${_lpr_json}" '.state' 'open|closed')"
          _lpr_merged="$(_jq_field "${_lpr_json}" '.merged_at != null' 'true|false')"
          # Skip reference-only PRs ("Refs #N") only after the batched
          # cache and single-issue GraphQL retry both missed.  At that
          # point the PR body is the best available signal; when even the
          # body parse is indeterminate, preserve the candidate PR so the
          # guard fails closed on transient API/payload errors.
          if _pr_json_closes_issue "${issue_num}" "${_lpr_json}"; then
            _lpr_body_check_rc=0
          else
            _lpr_body_check_rc=$?
          fi
          if [ "${_lpr_body_check_rc}" -eq 1 ]; then
            _lpr_num=""
          else
            # REST-fallback merged-PR sub-guard: catches merged PRs that
            # the batched GraphQL prefetch missed or failed to fetch
            # (transient network error, partial batch, issue number that
            # wasn't in the stalls list, etc.).  Without this, the
            # merged-PR short-circuit silently regresses on any cache
            # miss and the /reclarify loop from GH issue #1074 could
            # recur.  Uses the same _reconcile_merged_pr_issue helper
            # as the cache-hit path at the top of this guard, and
            # respects ENABLE_STALL_MERGED_PR_GUARD so disabling the
            # flag still gives full opt-out.  No extra API calls — the
            # REST fallback has already fetched the PR payload on the
            # preceding line.
            if [ "${ENABLE_STALL_MERGED_PR_GUARD}" = "true" ] && [ "${_lpr_merged}" = "true" ]; then
              echo "STALL_SKIP issue=${issue_num} reason=merged_linked_pr pr=${_lpr_num} phase=${phase} action=${action} source=rest_fallback"
              _reconcile_merged_pr_issue "${issue_num}" "${phase}" "${action}" "${_lpr_num}"
              STALL_HEALING_CHANGED=true
              return 1  # Signal: no action taken (caller should not increment counter)
            fi
          fi
        fi
      fi
      if [[ "${_lpr_num}" =~ ^[0-9]+$ ]] && [ "${_lpr_state}" = "open" ]; then
        # --- State-aware open-PR guard ---
        # Instead of always skipping, check the PR's merge state and review
        # status to dispatch the appropriate corrective action.
        local _opr_json=""
        local _opr_mergeable=""
        local _opr_mergeable_state=""
        local _opr_head_ref=""
        local _opr_review_comments=""

        # Reuse existing PR JSON if available (REST fallback already fetched it),
        # otherwise fetch once (1 API call, not per-field).
        if [ -n "${_lpr_json:-}" ] && [ "${_lpr_json}" != "{}" ]; then
          _opr_json="${_lpr_json}"
        else
          _opr_json="$(_fetch_pr_json "${_lpr_num}")"
        fi
        _opr_mergeable="$(_jq_field "${_opr_json}" '.mergeable' 'true|false')"
        _opr_mergeable_state="$(_jq_field "${_opr_json}" '.mergeable_state')"
        _opr_head_ref="$(_jq_field "${_opr_json}" '.head.ref')"

        # Sub-case 1: PR has merge conflicts → dispatch conflict resolver
        if { [ "${_opr_mergeable}" = "false" ] || [ "${_opr_mergeable_state}" = "dirty" ]; } && [ -n "${_opr_head_ref}" ]; then
          local _opr_dispatch_rc=0
          _dispatch_review_for_conflicts "${_lpr_num}" "${_opr_head_ref}" || _opr_dispatch_rc=$?
          if [ "${_opr_dispatch_rc}" -eq 0 ]; then
            echo "STALL_RECOVERY issue=${issue_num} reason=open_pr_merge_conflict pr=${_lpr_num} phase=${phase} action=dispatch_conflict_resolver"
            add_healing_note "Issue #${issue_num}: open PR #${_lpr_num} has merge conflicts (phase=${phase}); dispatching conflict resolver instead of '${action}'"
            STALL_HEALING_CHANGED=true
            tg_notify "Stall recovery: PR #${_lpr_num} for issue #${issue_num} has merge conflicts. Dispatched conflict resolver."$'\n'"Issue: $(_gh_url "issues/${issue_num}")"$'\n'"PR: $(_gh_url "pull/${_lpr_num}")" "WARNING"
            return 0  # Signal: corrective action taken
          elif [ "${_opr_dispatch_rc}" -eq 2 ]; then
            echo "STALL_SKIP issue=${issue_num} reason=open_pr_merge_conflict_dispatch_skipped pr=${_lpr_num} phase=${phase} action=${action}"
          else
            echo "STALL_RECOVERY issue=${issue_num} reason=open_pr_merge_conflict_dispatch_failed pr=${_lpr_num} phase=${phase} action=${action} rc=${_opr_dispatch_rc}"
          fi
          return 1  # Signal: no action taken (skipped/failed dispatch)
        fi

        # Sub-case 2: PR has unresolved review comments → dispatch review/autofix
        # Check review decision (CHANGES_REQUESTED or pending reviews).
        # Audited existing calls: _fetch_pr_json returns the full PR payload
        # but not review comments.  We need one additional REST call to check
        # review state.  This is acceptable because it only fires when an open
        # PR exists AND the stall threshold is exceeded (rare), not per-cycle.
        _opr_review_comments="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${_lpr_num}/reviews" \
          --jq '[.[] | select(.user != null and .user.login != null)] | sort_by(.user.login, (.submitted_at // "")) | group_by(.user.login) | map(last) | map(select(.state == "CHANGES_REQUESTED")) | length' 2>/dev/null || echo "0")"
        if ! [[ "${_opr_review_comments}" =~ ^[0-9]+$ ]]; then
          _opr_review_comments="0"
        fi
        if [ "${_opr_review_comments}" -gt 0 ] && [ -n "${_opr_head_ref}" ]; then
          local _opr_autofix_rc=0
          _dispatch_review_for_conflicts "${_lpr_num}" "${_opr_head_ref}" || _opr_autofix_rc=$?
          if [ "${_opr_autofix_rc}" -eq 0 ]; then
            echo "STALL_RECOVERY issue=${issue_num} reason=open_pr_changes_requested pr=${_lpr_num} phase=${phase} action=dispatch_autofix"
            add_healing_note "Issue #${issue_num}: open PR #${_lpr_num} has ${_opr_review_comments} review(s) requesting changes (phase=${phase}); dispatching autofix instead of '${action}'"
            STALL_HEALING_CHANGED=true
            tg_notify "Stall recovery: PR #${_lpr_num} for issue #${issue_num} has changes-requested reviews. Dispatched autofix."$'\n'"Issue: $(_gh_url "issues/${issue_num}")"$'\n'"PR: $(_gh_url "pull/${_lpr_num}")" "WARNING"
            return 0  # Signal: corrective action taken
          elif [ "${_opr_autofix_rc}" -eq 2 ]; then
            echo "STALL_SKIP issue=${issue_num} reason=open_pr_changes_requested_dispatch_skipped pr=${_lpr_num} phase=${phase} action=${action}"
          else
            echo "STALL_RECOVERY issue=${issue_num} reason=open_pr_changes_requested_dispatch_failed pr=${_lpr_num} phase=${phase} action=${action} rc=${_opr_autofix_rc}"
          fi
          return 1  # Signal: no action taken (skipped/failed dispatch)
        fi

        # Sub-case 3: PR is open but clean/progressing → skip (legacy behavior)
        echo "STALL_SKIP issue=${issue_num} reason=open_linked_pr pr=${_lpr_num} phase=${phase} action=${action}"
        add_healing_note "Issue #${issue_num}: skipped early-phase stall recovery '${action}' (phase=${phase}) — open linked PR #${_lpr_num} already exists"
        STALL_HEALING_CHANGED=true
        tg_notify "Stall recovery: skipped '${action}' for issue #${issue_num} (phase=${phase}) because open linked PR #${_lpr_num} already exists."$'\n'"Issue: $(_gh_url "issues/${issue_num}")"$'\n'"PR: $(_gh_url "pull/${_lpr_num}")" "WARNING"
        return 1  # Signal: no action taken (caller should not increment counter)
      fi
      ;;
  esac

  # Cancel any zombie runs for this issue before retrying.
  if [ "${action}" != "skip" ] && [ "${action}" != "attempt_merge" ] && [ "${action}" != "escalate_human" ]; then
    cancel_zombie_runs_for_issue "${issue_num}"
  fi

  local _recover_phase_cap_state=""
  _recover_phase_cap_state="$(phase_cap_state_for_action "${action}")"
  if [ -n "${_recover_phase_cap_state}" ] && ! phase_cap_can_dispatch "${_recover_phase_cap_state}" "${action}" "${issue_num}"; then
    echo "STALL_SKIP issue=${issue_num} reason=phase_capped phase=${phase} action=${action}"
    return 1
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
		active="$(gh_retry gh run list --repo "${GITHUB_REPOSITORY}" \
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

	# Forward ALLOW_WORKFLOW_EDITS so the dispatched review run respects the
	# repo-level opt-out semantics (vars.ALLOW_WORKFLOW_EDITS != 'false').
	# Without this flag the dispatched workflow falls back to its own
	# workflow_dispatch input default, which silently suppresses workflow
	# edits even when the repo variable allows them. See
	# review_autofix.yml:51 and internal-review.yml:15.
	local allow_workflow_edits_flag="${ALLOW_WORKFLOW_EDITS:-true}"
	for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
		if gh_retry gh workflow run "${wf_candidate}" \
			--repo "${GITHUB_REPOSITORY}" \
			--ref "${head_ref}" \
			-f pr_number="${pr_number}" \
			-f allow_workflow_edits="${allow_workflow_edits_flag}" 2>/dev/null; then
			echo "  ${log_prefix} Dispatched ${wf_candidate} on ${head_ref} (allow_workflow_edits=${allow_workflow_edits_flag})."
			# Record in cycle-local tracker to prevent duplicate dispatches
			echo "${pr_number}" >> "${_CONFLICT_DISPATCH_TRACKER}"
			return 0
		fi
	done

	echo "::warning::${log_prefix} Could not dispatch review workflow via workflow_dispatch."
	return 1
}

# _dispatch_rb_judge_for_pr <pr_number> [issue_number]
#
# Trigger review_rb_judge_dispatch.yml via workflow_dispatch for the
# given PR.  That workflow calls review_autofix.yml with
# force_rb_judge=true, which short-circuits retrigger_guard to run only
# the review-blocked judge (scripts/review_rb_judge.sh).  The judge
# decides merge, fix, or close_and_reissue — giving ai:review-blocked
# issues an autonomous escape path.
#
# Returns:
#   0 — dispatched successfully this cycle.
#   1 — dispatch failed (e.g. invalid pr_number, workflow missing, PAT
#       scope insufficient).
#   2 — skipped (cycle-local duplicate-dispatch guard: the same PR
#       already had a judge dispatch queued earlier this tick).  Callers
#       must treat this as an in-flight success — do NOT increment the
#       stall recovery count, and do NOT re-attempt in the same tick.
#   3 — skipped by the optional ai:review-blocked concurrency cap for
#       this tick. Callers must treat this as a benign deferral and leave
#       the recovery count unchanged.
#
# API hygiene: reuses the cycle-local _CONFLICT_DISPATCH_TRACKER to
# prevent duplicate dispatches within the same poll cycle, same as
# _dispatch_review_for_conflicts.  ref resolves from the repo default
# branch (main) because the judge does not need the PR's head ref to
# run — it operates on the PR metadata directly via gh api.
_dispatch_rb_judge_for_pr()
{
	local pr_number="$1"
	local issue_num="${2:-${pr_number}}"
	local log_prefix="[rb-judge-dispatch] PR #${pr_number}"

	if ! [[ "${pr_number}" =~ ^[0-9]+$ ]]; then
		echo "::warning::${log_prefix} invalid pr_number; skipping."
		return 1
	fi

	# Cycle-local duplicate-dispatch guard.  Reuses the conflict
	# tracker file because both dispatches target review_autofix.yml
	# (directly or indirectly) and cancel-in-progress concurrency
	# would otherwise kill one with the other.
	if grep -qx "rb-judge:${pr_number}" "${_CONFLICT_DISPATCH_TRACKER}" 2>/dev/null; then
		echo "  ${log_prefix} Already dispatched in this poll cycle. Skipping."
		return 2
	fi

	if ! phase_cap_can_dispatch "ai:review-blocked" "dispatch_rb_judge" "${issue_num}"; then
		return 3
	fi

	echo "  ${log_prefix} Dispatching review_rb_judge_dispatch.yml..."
	if gh_retry gh workflow run review_rb_judge_dispatch.yml \
		--repo "${GITHUB_REPOSITORY}" \
		-f pr_number="${pr_number}" 2>/dev/null; then
		echo "  ${log_prefix} Dispatched review_rb_judge_dispatch.yml."
		phase_cap_note_dispatch "ai:review-blocked"
		echo "rb-judge:${pr_number}" >> "${_CONFLICT_DISPATCH_TRACKER}"
		return 0
	fi

	echo "::warning::${log_prefix} Could not dispatch review_rb_judge_dispatch.yml. Ensure the workflow exists on the default branch and the GH_PAT has workflow-dispatch scope."
	return 1
}

# _runtime_blocker_dispatch_eligible <local_id> <wave_num> [candidate_details_json]
#
# Evaluate whether a managed issue may be dispatched this tick based on the
# orchestrator state's dependency_edges. Returns 0 when dispatch may proceed,
# 1 when the issue should be deferred, and fails open (0 + warning) only on
# helper invocation/JSON-shape faults so the poller does not deadlock on
# checker infrastructure failures.
_runtime_blocker_dispatch_eligible()
{
	local local_id="$1"
	local wave_num="$2"
	local candidate_details_json="${3:-}"
	local candidate_truth_json='{}'
	local blocker_result=""
	local blocker_stderr=""
	local blocker_err_file=""
	local eligible=""
	local signal="dispatch_deferred_blocker"
	local reason="blocked_by_dependency"
	local detail=""
	local blocker_summary=""

	[ "${RUNTIME_BLOCKER_CHECK_ENABLED:-false}" = "true" ] || return 0

	if [ -n "${candidate_details_json}" ] && [ "${candidate_details_json}" != "{}" ]; then
		candidate_truth_json="$(printf '%s' "${candidate_details_json}" | jq -c '
			with_entries(
				.value |= (
					if type == "object"
						then {
							state: (.state // null),
							labels: (.labels // []),
							linked_pr: (.linked_pr // null)
						}
						else {}
					end
				)
			)
		' 2>/dev/null || echo '{}')"
	fi

	if ! blocker_err_file="$(mktemp "${TMPDIR:-/tmp}/runtime-blocker.XXXXXX" 2>/dev/null)"; then
		echo "::warning::[runtime-blocker] failed to create temp file for ${local_id}; failing open."
		return 0
	fi
	if [ -z "${blocker_err_file}" ]; then
		echo "::warning::[runtime-blocker] failed to create temp file for ${local_id}; failing open."
		return 0
	fi
	if ! blocker_result="$(python3 scripts/blocker_check.py \
		--state-file "${STATE_FILE}" \
		--local-id "${local_id}" \
		--candidate-details-json "${candidate_truth_json}" 2>"${blocker_err_file}")"; then
			blocker_stderr="$(tr '\n' ' ' < "${blocker_err_file}" | sed 's/[[:space:]]\+/ /g' | cut -c1-300)"
		rm -f "${blocker_err_file}"
		echo "::warning::[runtime-blocker] blocker_check.py failed for ${local_id}; failing open.${blocker_stderr:+ stderr=${blocker_stderr}}"
		return 0
	fi
	rm -f "${blocker_err_file}"

	if ! printf '%s' "${blocker_result}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		echo "::warning::[runtime-blocker] blocker_check.py returned invalid JSON for ${local_id}; failing open."
		return 0
	fi

	eligible="$(printf '%s' "${blocker_result}" | jq -r '.eligible // false' 2>/dev/null || echo 'false')"
	if [ "${eligible}" = "true" ]; then
		return 0
	fi

	signal="$(printf '%s' "${blocker_result}" | jq -r '.signal // "dispatch_deferred_blocker"' 2>/dev/null || echo 'dispatch_deferred_blocker')"
	reason="$(printf '%s' "${blocker_result}" | jq -r '.reason // "blocked_by_dependency"' 2>/dev/null || echo 'blocked_by_dependency')"
	detail="$(printf '%s' "${blocker_result}" | jq -r '.detail // empty' 2>/dev/null || echo '')"
	blocker_summary="$(printf '%s' "${blocker_result}" | jq -r '[.blockers[]? | "\(.local_id):\(.status):\(.source)"] | join(",")' 2>/dev/null || echo '')"
	[ -n "${blocker_summary}" ] || blocker_summary="(none)"
	echo "${signal} local_id=${local_id} wave=${wave_num} reason=${reason} blockers=${blocker_summary}${detail:+ detail=${detail}}"
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

if _is_truthy "${RUNTIME_BLOCKER_CHECK_ENABLED:-true}"; then
  RUNTIME_BLOCKER_CHECK_ENABLED="true"
else
  RUNTIME_BLOCKER_CHECK_ENABLED="false"
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
FEATURE_SWEEP_DONE="false"
prime_phase_concurrency_snapshot ".github/ai/concurrency_caps.yml"
write_state_snapshot_actions_runs_export || true

for ((tidx=0; tidx<COUNT; tidx++)); do
  TRACKING_NUM="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].number")"
  TRACKING_TITLE="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].title")"
  unset FORCE_MERGE_LABEL_EVENT_JSON_CACHE
  unset _INTEGRATION_BACKPRESSURE_EFFECTIVE_THRESHOLD_CACHE
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
  COMMENTS_FETCH_OK="false"
  COMMENTS='[]'
  if gh_retry_to_file "${_comments_raw}" gh api --paginate \
    "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments?per_page=100"; then
    _comments_merged="$(mktemp "${TMPDIR:-/tmp}/comments_merged.XXXXXX")"
    if jq -s 'add // []' "${_comments_raw}" > "${_comments_merged}" 2>/dev/null \
      && jq -e 'type == "array"' "${_comments_merged}" >/dev/null 2>&1; then
      COMMENTS="$(cat "${_comments_merged}")"
      COMMENTS_FETCH_OK="true"
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
    post_state_comment || true
    echo "::warning::Detected malformed latest ORCHESTRATOR_STATE_V1 for issue #${TRACKING_NUM}; restored from older valid state and posted healed canonical state."
  fi

  if [ -z "${STATE_JSON}" ] || [ "${STATE_JSON}" = "null" ]; then
    # A failed (or unvalidated) comments fetch is NOT evidence that the
    # orchestrator state is missing.  The state comment may exist but be
    # temporarily unreadable — e.g. a paginated fetch of a tracking issue
    # with hundreds of comments that exhausted its retries, or a truncated
    # response that failed JSON validation above (COMMENTS_FETCH_OK="false"
    # with COMMENTS="[]").  The rest of the poller already treats
    # COMMENTS_FETCH_OK != true as "comments unavailable this cycle, fail
    # open and retry" (see update_completion_status_comment and
    # completion_status_comment_failed_state_observation).
    #
    # State reconstruction, by contrast, is destructive: rebuild_tracking_state
    # resets current_wave to 1 and re-creates GitHub issues for any local id
    # the best-effort, eventually-consistent child-issue search fails to map,
    # spawning duplicate issues for already-completed waves.  Only reconstruct
    # when we actually read the comments and confirmed no valid state comment
    # exists; when the fetch itself failed, skip this tracking issue and let a
    # later poll cycle read the real state.
    if [ "${COMMENTS_FETCH_OK}" != "true" ]; then
      echo "::warning::Comments fetch failed for tracking issue #${TRACKING_NUM}; cannot confirm orchestrator state is missing. Skipping state reconstruction this cycle (will retry next poll)."
      continue
    fi
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
    TRACKING_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" --jq '.body' || echo "")"
    if [ -z "${TRACKING_BODY}" ]; then
      echo "::warning::Could not fetch body for tracking issue #${TRACKING_NUM}, skipping."
      continue
    fi

    # Search for child issues whose body contains the tracking reference
    CHILD_ISSUES="$(gh_retry gh api "search/issues" \
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

    # Capture the helper's stderr so a deliberate refusal (e.g.
    # ReconstructionUnsafeError when the body marks completed work the issue
    # map cannot account for) is surfaced in the log instead of discarded.
    REBUILD_ERR_FILE="${RUNTIME_DIR}/rebuild_err_${TRACKING_NUM}.txt"
    if python3 scripts/orchestrate_lib.py rebuild-state \
      --body-file "${REBUILD_BODY_FILE}" \
      --issue-map-json "${ISSUE_MAP_JSON}" \
      --tracking-issue "${TRACKING_NUM}" > "${STATE_FILE}" 2>"${REBUILD_ERR_FILE}"; then

      rm -f "${REBUILD_ERR_FILE}"
      if [ -s "${STATE_FILE}" ] && jq -e '.schema_version' "${STATE_FILE}" >/dev/null 2>&1; then
        STATE_JSON="$(cat "${STATE_FILE}")"
        # Post the reconstructed state so future poll cycles find it
        post_state_comment || true
        echo "  State reconstructed and posted for tracking issue #${TRACKING_NUM}."
        tg_notify "Auto-recovery: rebuilt missing orchestrator state for tracking issue #${TRACKING_NUM}." "DEBUG"
      else
        echo "::warning::State reconstruction produced invalid output for #${TRACKING_NUM}, skipping."
        continue
      fi
    else
      REBUILD_ERR_MSG="$(head -c 500 "${REBUILD_ERR_FILE}" 2>/dev/null | tr '\n' ' ')"
      rm -f "${REBUILD_ERR_FILE}"
      # A non-zero rebuild (e.g. a deliberate ReconstructionUnsafeError refusal)
      # leaves an empty STATE_FILE from the stdout redirect; drop it so no
      # downstream reader trips over a 0-byte "state" file.
      rm -f "${STATE_FILE}"
      echo "::warning::State reconstruction failed for tracking issue #${TRACKING_NUM}, skipping. ${REBUILD_ERR_MSG}"
      continue
    fi
  fi

  if ! is_valid_orchestrator_state_json "${STATE_JSON}"; then
    echo "::warning::STATE_JSON for issue #${TRACKING_NUM} is not a valid orchestrator state object; skipping"
    continue
  fi

  echo "${STATE_JSON}" > "${STATE_FILE}"
  unset PROJECT_COMPLETE WAVE_COMPLETE ANY_FAILED WAVE_STATUS VALIDATION_DISPATCH_SAFE_DESPITE_FAILURES
  COMPLETION_STATUS_STATE_CHANGED="false"
  PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
  TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"

  # ---------------------------------------------------------------
  # External-finalize detect: if the orchestrator previously recorded
  # a final integration PR and an operator (or any other actor)
  # squash-merged it outside the orchestrator's wave-by-wave flow,
  # `finalize_integration_merge_if_needed` is never reached from the
  # `in_progress` arm — it only fires from the `merge_conflict` branch
  # and from the judge-`complete` verdict.  Without this hook, the
  # poller keeps cycling on wave-dispatch indefinitely (e.g.
  # orchestrator/project-2734: PR #2750 merged 2026-05-18T21:31:50Z
  # but `final_merge_status` stayed `pending`, the wave-2 dispatch
  # gate re-fired every ~30 min, and the Telegram channel collected
  # several `Wave 2 dispatch BLOCKED` alerts per hour).
  #
  # This recovery must run BEFORE sync_default_into_integration_branch:
  # external squash merges commonly delete the integration branch, and
  # the sync path would otherwise mark the project failed before this
  # block can observe the already-merged final PR.
  #
  # Mirror the same pinned-final-PR recovery shape that
  # `finalize_integration_merge_if_needed` already uses once a final PR
  # is recorded in state: read `final_merge_pr`, fetch the PR once via
  # the shared `_fetch_pr_json` helper, and only transition when
  # `.state` + `.merged_at` confirm closed-and-merged.  One REST read
  # per poll tick when a final PR is pinned; skipped entirely when
  # `final_merge_pr` is unset (most projects pre-finalize),
  # `final_merge_status` is already terminal, or the project is already
  # on the dedicated `merge_conflict` / validation-completion paths.
  # Validated projects must keep flowing through
  # `mark_validation_complete` so `validation_completed_cycle` and the
  # `ai:validated` label stay aligned with the final merge result.
  if [ "${PROJECT_STATUS}" != "complete" ] \
    && [ "${PROJECT_STATUS}" != "failed" ] \
    && [ "${PROJECT_STATUS}" != "validation-failed" ] \
    && [ "${PROJECT_STATUS}" != "merge_conflict" ] \
    && [ "${PROJECT_STATUS}" != "validating" ] \
    && [ "${PROJECT_STATUS}" != "validation-fixing" ]; then
    _orch_extfin_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || true)"
    _orch_extfin_status="$(jq -r '.final_merge_status // "pending"' "${STATE_FILE}" 2>/dev/null || echo "pending")"
    if [ -n "${_orch_extfin_pr}" ] && [ "${_orch_extfin_pr}" != "null" ] \
      && [ "${_orch_extfin_status}" = "pending" ]; then
      if ! [[ "${_orch_extfin_pr}" =~ ^[0-9]+$ ]]; then
        echo "::warning::[external-finalize] ignoring non-numeric final_merge_pr in state for issue #${TRACKING_NUM}: ${_orch_extfin_pr}"
      else
        _orch_extfin_pr_json="$(_fetch_pr_json "${_orch_extfin_pr}")"
        _orch_extfin_pr_state="$(_jq_field "${_orch_extfin_pr_json}" '.state' 'open|closed|merged')"
        _orch_extfin_pr_merged="$(_jq_field "${_orch_extfin_pr_json}" '.merged_at != null' 'true|false')"
        if [ -z "${_orch_extfin_pr_state}" ] || [ -z "${_orch_extfin_pr_merged}" ]; then
          echo "::warning::[external-finalize] unable to inspect PR #${_orch_extfin_pr}; leaving final_merge_status pending."
        elif [ "${_orch_extfin_pr_state}" = "closed" ] && [ "${_orch_extfin_pr_merged}" = "true" ]; then
          # Completeness gate (P2 from docs/postmortems/2026-05-18-project-2734-stall.md).
          # An externally-merged integration PR is necessary but NOT sufficient
          # evidence of project completion: a human (or another Claude session)
          # can squash-merge the eager integration PR with only Wave 1 content
          # while later waves remain undispatched (see project #2734, where 7
          # of 9 sub-issues were never created). Without this gate the orchestrator
          # would broadcast "✅ Project complete" while most of the planned work
          # has shipped no code. Refuse to transition and alert the operator
          # once per missing-issue-set; reviving some-but-not-all waves
          # re-alerts because the set's signature changes.
          #
          # Filter is narrow on purpose: only `status == "not_created"`
          # entries are caught. Sub-issues that were dispatched (have a
          # github_issue number) and are merely in a transient
          # `pending`/`active`/`in_progress` status while the orchestrator's
          # own per-tick label reconciliation lags behind the GitHub merge
          # event are NOT caught — those self-resolve on the next tick.
          # The project #2734 incident specifically had Wave 2-7 sub-issues
          # with `status == "not_created"` and `github_issue == null` for
          # the entire 26-hour stall window, which is the case this gate
          # exists to catch.
          _orch_extfin_incomplete_json="$(jq -c \
            '[.waves[]?.issues[]?
              | select(.status == "not_created")
              | {id: (.id // "<no-id>"), status: (.status // "unknown"), github_issue: (.github_issue // null)}]' \
            "${STATE_FILE}" 2>/dev/null || echo "[]")"
          _orch_extfin_incomplete_count="$(echo "${_orch_extfin_incomplete_json}" | jq 'length' 2>/dev/null || echo 0)"
          if [ "${_orch_extfin_incomplete_count}" -gt 0 ]; then
            _orch_extfin_missing_list="$(echo "${_orch_extfin_incomplete_json}" \
              | jq -r '.[] | "  - " + .id + " [status=" + .status + "] github_issue=" + (.github_issue | tostring)')"
            echo "::warning::[external-finalize-partial] PR #${_orch_extfin_pr} merged but ${_orch_extfin_incomplete_count} sub-issue(s) not in terminal-success state — refusing to transition project to complete."
            echo "${_orch_extfin_missing_list}"
            # Dedup the Telegram alert by hashing the missing-issue set.
            _orch_extfin_missing_sig="$(echo "${_orch_extfin_incomplete_json}" | jq -S -c '.' 2>/dev/null | sha256sum | awk '{print $1}')"
            _orch_extfin_prev_sig="$(jq -r '.external_finalize_partial_alert_sig // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
            if [ "${_orch_extfin_missing_sig}" != "${_orch_extfin_prev_sig}" ]; then
              _orch_extfin_tg_msg="⚠️ Project #${TRACKING_NUM}: integration PR #${_orch_extfin_pr} merged externally, but ${_orch_extfin_incomplete_count} sub-issue(s) are still not terminal:"$'\n'"${_orch_extfin_missing_list}"$'\n'"Refusing to mark project complete. Either revive the missing waves or close them with rationale."
              _orch_extfin_tg_msg+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
              tg_notify "${_orch_extfin_tg_msg}" "WARNING" || true
              if jq --arg sig "${_orch_extfin_missing_sig}" \
                '.external_finalize_partial_alert_sig = $sig' \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
                :
              else
                rm -f "${STATE_FILE}.tmp" || true
                echo "::warning::[external-finalize-partial] failed to persist alert-dedup signature; may re-alert on next tick."
              fi
            fi
            continue
          fi
          echo "  [external-finalize] PR #${_orch_extfin_pr} merged outside the wave-by-wave flow; transitioning project to complete."
          if ! jq --argjson final_pr "${_orch_extfin_pr}" \
            '.final_merge_pr = $final_pr
             | .final_merge_status = "merged"
             | .final_merge_error = ""
             | .status = "complete"
             | .judge_cycle = ((.judge_cycle // 0) + 1)' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" || ! mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
            rm -f "${STATE_FILE}.tmp" || true
            echo "::warning::[external-finalize] failed to persist merged state for PR #${_orch_extfin_pr}; leaving final_merge_status pending."
            continue
          fi
          post_state_comment || true
          handle_comprehensive_release_callback_if_needed "complete" "${TRACKING_LABELS}" "${COMMENTS:-[]}"
          set_tracking_phase_label "ai:merged"
          post_tracking_comment "## ✅ Project complete — integration PR #${_orch_extfin_pr} merged externally

The orchestrator detected that the integration PR was squash-merged outside the wave-by-wave dispatch flow (the typical pattern when an operator finalizes a project ahead of the planner). Transitioning status to \`complete\`; future poll ticks will skip this project and any open wave-dispatch alerts can be ignored."
          tg_cleanup_msgs "${TRACKING_NUM}"
          MSG="✅ Project #${TRACKING_NUM} completed (integration PR #${_orch_extfin_pr} merged externally)."
          MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
          if [ -n "${GITHUB_RUN_ID:-}" ]; then
            MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
          fi
          tg_send_msg "${MSG}" >/dev/null
          PROJECT_STATUS="complete"
          continue
        fi
      fi
    fi
  fi

  # Validation-owned states must bypass sync so mark_validation_complete
  # can own externally merged/deleted final-PR completion without the
  # integration-branch missing/conflict path preempting it.
  DEFAULT_BRANCH_TRACKING="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  INTEGRATION_BRANCH_TRACKING="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  if [ -n "${INTEGRATION_BRANCH_TRACKING}" ] \
    && [ "${PROJECT_STATUS}" != "complete" ] \
    && [ "${PROJECT_STATUS}" != "failed" ] \
    && [ "${PROJECT_STATUS}" != "merge_conflict" ] \
    && [ "${PROJECT_STATUS}" != "validating" ] \
    && [ "${PROJECT_STATUS}" != "validation-fixing" ] \
    && [ "${PROJECT_STATUS}" != "validation-failed" ]; then
    if ! sync_default_into_integration_branch "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}"; then
      continue
    fi
    PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
    if [ "${PROJECT_STATUS}" = "failed" ]; then
      continue
    fi
  fi

  if [ "${PROJECT_STATUS}" = "merge_conflict" ]; then
    FINAL_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
    FINAL_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
    FINAL_PROJECT_TITLE="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"
    FINAL_PR_FOR_TRACKING_BODY="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"

    TRACKING_BODY_SYNC_STATE_CHANGED="false"
    if [ -n "${FINAL_INTEGRATION_BRANCH}" ] || [[ "${FINAL_PR_FOR_TRACKING_BODY}" =~ ^[0-9]+$ ]]; then
      reconcile_tracking_issue_body_from_state "${FINAL_PR_FOR_TRACKING_BODY}" "${FINAL_INTEGRATION_BRANCH}" || true
      if [ "${TRACKING_BODY_SYNC_STATE_CHANGED:-false}" = "true" ]; then
        post_state_comment || true
      fi
    fi

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
    post_state_comment || true
    handle_comprehensive_release_callback_if_needed "complete" "${TRACKING_LABELS}" "${COMMENTS:-[]}"
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
    # `sync_validation_fix_issues_from_comments` has three exit paths:
    #   (A) early return when no new fix comment is present — no state
    #       or label mutation;
    #   (B) new-fix-comment path — bumps `validation_last_fix_comment_id`
    #       and calls `set_tracking_phase_label "ai:validation-fixing"`,
    #       which removes other phase labels via the label contract;
    #   (C) extractable-count == 0 path — calls `mark_validation_failed`
    #       which mutates labels AND transitions status (in_progress or
    #       failed) without touching `validation_last_fix_comment_id`.
    #
    # The previously-fetched TRACKING_LABELS is only stale on (B) and
    # (C).  Detect (B) via the marker delta and (C) via any status
    # change, then re-fetch.  Path (A) leaves both unchanged so the
    # re-fetch is skipped — preserves the optimization without the
    # correctness gap flagged in the Copilot review on PR #1044.
    _project_status_before_sync="${PROJECT_STATUS}"
    _last_fix_comment_id_before="$(jq -r '.validation_last_fix_comment_id // 0' "${STATE_FILE}" 2>/dev/null || echo 0)"
    sync_validation_fix_issues_from_comments "${COMMENTS}"
    PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
    _last_fix_comment_id_after="$(jq -r '.validation_last_fix_comment_id // 0' "${STATE_FILE}" 2>/dev/null || echo 0)"
    if [ "${_last_fix_comment_id_after}" != "${_last_fix_comment_id_before}" ] \
      || [ "${PROJECT_STATUS}" != "${_project_status_before_sync}" ]; then
      TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"
    fi
    unset _project_status_before_sync _last_fix_comment_id_before _last_fix_comment_id_after
  fi

  if [ "${PROJECT_STATUS}" = "validating" ] || [ "${PROJECT_STATUS}" = "validation-fixing" ]; then
    VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
    if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
      VALIDATION_CYCLE="1"
    fi

    LAST_VAL_RUN_INFO='{}'
    LAST_VAL_CONCLUSION=''
    LAST_VAL_RAW_STATUS=''
    if [ "${PROJECT_STATUS}" = "validating" ]; then
      LAST_VAL_RUN_INFO="$(get_last_validation_run_info)"
      LAST_VAL_CONCLUSION="$(printf '%s' "${LAST_VAL_RUN_INFO}" | jq -r '.conclusion // ""' 2>/dev/null || echo '')"
      LAST_VAL_RAW_STATUS="$(printf '%s' "${LAST_VAL_RUN_INFO}" | jq -r '.raw_status // ""' 2>/dev/null || echo '')"
    fi

    if has_label "${TRACKING_LABELS}" "ai:validation-failed" || has_label "${TRACKING_LABELS}" "ai:validate-failed"; then
      # Extract the detailed failure diagnosis from the most recent validation
      # comment posted by validate_process.sh (matches headings like
      # "Runtime validation failed", "Runtime validation harness error",
      # "Runtime validation infeasible", "Runtime validation found fixable issues",
      # or "Validate workflow failure").
      VALIDATION_FAIL_BODY="$(echo "${COMMENTS}" | jq -r '
        [.[] | select((.body // "") | test("## [❌🧪⚠️]+ (Runtime validation|Validate workflow failure)"))] | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)]) | .body // ""
      ')"
      if [ -n "${VALIDATION_FAIL_BODY}" ] && [ "${VALIDATION_FAIL_BODY}" != "" ]; then
        mark_validation_failed "${VALIDATION_FAIL_BODY}"
      else
        mark_validation_failed "Validation workflow reported failure (label ai:validation-failed or ai:validate-failed)."
      fi
      continue
    fi

    if has_label "${TRACKING_LABELS}" "ai:validated"; then
      mark_validation_complete "${VALIDATION_CYCLE}"
      continue
    fi

    # Fallback: if the last validation workflow run completed successfully
    # and no ai:validation-failed/ai:validate-failed label exists, treat as validated.
    # This handles the case where validate_process.sh completed but the
    # ai:validated label was lost or never persisted (silent gh API failure).
    if [ "${PROJECT_STATUS}" = "validating" ]; then
      if [ "${LAST_VAL_CONCLUSION}" = "success" ]; then
        echo "Fallback: last validation run concluded success without ai:validated label. Applying label and marking complete."
        set_tracking_phase_label "ai:validated"
        post_tracking_comment "## ℹ️ Validation completion detected via workflow run fallback

The \`ai:validated\` label was missing but the last validation workflow run concluded successfully. Applying label and completing."
        mark_validation_complete "${VALIDATION_CYCLE}"
        continue
      fi
    fi

    if [ "${PROJECT_STATUS}" = "validating" ]; then
      if has_label "${TRACKING_LABELS}" "ai:validation-fixing"; then
        jq '.status = "validation-fixing"' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment || true
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

      # Single consolidated API call: state + state_reason + labels.  Previous
      # implementation fetched only labels here and never consulted the issue's
      # state, so any fix-up issue closed without the ai:closed label (manual
      # close, "not planned" close, external rename, etc.) was invisible to the
      # closure detector and the batch would loop "still in progress" forever.
      FIX_INFO_JSON="$(get_issue_state_labels_json "${fix_num}")"
      FIX_LABELS="$(echo "${FIX_INFO_JSON}" | jq -c '.labels // []')"
      FIX_STATE="$(echo "${FIX_INFO_JSON}" | jq -r '.state // "open"')"
      FIX_STATE_REASON="$(echo "${FIX_INFO_JSON}" | jq -r '.state_reason // ""')"
      FIX_IS_MERGED="false"
      FIX_THIS_CLOSED_WITHOUT_MERGE="false"
      # Capture the evidence-lookup exit code in a dedicated variable instead
      # of reading $? after the else branch — more robust to future edits that
      # might insert a command between the call and the exit-status capture.
      FIX_EVIDENCE_STATUS=0

      if has_label "${FIX_LABELS}" "ai:merged"; then
        FIX_IS_MERGED="true"
      elif [ "${FIX_STATE}" = "closed" ] || has_label "${FIX_LABELS}" "ai:closed"; then
        # Issue is closed (live GitHub state) or carries an ai:closed label.
        # Walk the timeline once for merged-PR evidence; on success backfill
        # ai:merged, otherwise treat the issue as closed-without-merge
        # regardless of which of the two signals raised it.  Widening this
        # gate to include the ai:closed label catches the case the original
        # loop missed (fix-up issue closed with the ai:closed label but state
        # cache still says open) while still short-circuiting the evidence
        # walk for issues that are genuinely still open (the common case).
        if validation_fix_issue_has_merged_pr_evidence "${fix_num}"; then
          FIX_EVIDENCE_STATUS=0
          echo "Validation fix-up issue #${fix_num}: closed with merged PR evidence; backfilling ai:merged."
          # Pass the labels we already fetched at the top of this
          # iteration so backfill skips its internal
          # get_issue_labels_json round-trip.
          if backfill_validation_fix_issue_merged_label "${fix_num}" "${FIX_LABELS}"; then
            echo "Validation fix-up issue #${fix_num}: ai:merged label backfilled."
          else
            echo "::warning::Validation fix-up issue #${fix_num}: merged PR detected but ai:merged backfill failed." >&2
          fi
          FIX_IS_MERGED="true"
        else
          FIX_EVIDENCE_STATUS=$?
          if [ "${FIX_EVIDENCE_STATUS}" -eq 1 ]; then
            echo "Validation fix-up issue #${fix_num}: no merged PR evidence detected (state=${FIX_STATE}, state_reason=${FIX_STATE_REASON:-none})."
            FIX_THIS_CLOSED_WITHOUT_MERGE="true"
          else
            # Exit code 2 = transient timeline/API lookup failure.  Keep this
            # issue in-progress so the next poll cycle can retry instead of
            # forcing a false validation failure.
            echo "::warning::Validation fix-up issue #${fix_num}: merged PR lookup failed; leaving issue pending for retry." >&2
          fi
        fi
      else
        # Open fix-up issue.  When the issue is at ai:ready-to-merge,
        # proactively check the timeline for a merged linked PR and
        # backfill ai:merged in-cycle.  Without this, the consumer-side
        # issue_pr_status.yml workflow intentionally skips orchestrator-
        # managed children (anti-#1469 guard at issue_pr_status.yml:253-
        # 262) so the ai:ready-to-merge -> ai:merged transition would
        # otherwise wait for STALL_THRESHOLD_MINUTES (default 120m) to
        # elapse before _reconcile_merged_pr_issue reaches it via stall
        # recovery.  Bounded API cost: timeline walk only fires for
        # fix-up issues already at ai:ready-to-merge — every other open
        # phase still short-circuits with no API round-trip, preserving
        # the original optimisation noted on the legacy comment below.
        # Fail-open: a transient timeline lookup (exit 2) or label-edit
        # failure leaves FIX_IS_MERGED=false so the next cycle retries.
        if has_label "${FIX_LABELS}" "ai:ready-to-merge"; then
          # Capture exit code via if/else (the script runs under
          # `set -euo pipefail`, so a bare `cmd; status=$?` on a
          # non-zero return would abort).  This mirrors the
          # exit-code-aware pattern used by the closed-issue branch
          # above (~lines 7707-7730): 0=found, 1=no evidence, 2=
          # transient timeline/API failure.  Surfacing exit 2 as a
          # warning makes transient lookup failures diagnosable
          # instead of silently masquerading as "still awaiting".
          #
          # We deliberately do NOT set STALL_HEALING_CHANGED here:
          # that flag is reset to false ~line 10022 (top-level reset
          # in this same per-tracking-issue iteration, run AFTER this
          # block) before the post_state_comment / post_healing_summary
          # readers at lines 10105 and 10108.  The other sites that set
          # the flag (lines 7012-7203) all live inside
          # recover_stalled_issue, which is invoked from line 10071 —
          # i.e. AFTER the reset — so their writes survive.  Re-scoping
          # the flag to make a write-from-this-block survive is a
          # cross-cutting refactor that would change the lifetime of
          # every existing reconcile site, out of scope per CLAUDE.md
          # §5 (minimal change set) and §12 (PR review mode).  Cross-
          # cycle visibility is preserved regardless: the tail-of-cycle
          # close_merged_issues_sweep closes the reconciled issue in
          # the same poll, and any follow-on state-tracking on the next
          # cycle observes the post-close state correctly.
          # Reuse FIX_EVIDENCE_STATUS (already initialised to 0 at the
          # top of the iteration ~line 7694) so this branch matches the
          # closed-issue branch's pattern exactly and we don't introduce
          # a parallel variable name.  `local` is not used because this
          # block runs at top-level inside the `for ((tidx...))` loop
          # (not inside a function); see the comment at the proactive-
          # backfill site for the same reason STALL_HEALING_CHANGED
          # cannot be set here.
          if validation_fix_issue_has_merged_pr_evidence "${fix_num}"; then
            FIX_EVIDENCE_STATUS=0
            echo "Validation fix-up issue #${fix_num}: ai:ready-to-merge with merged PR evidence; proactively backfilling ai:merged."
            if backfill_validation_fix_issue_merged_label "${fix_num}" "${FIX_LABELS}"; then
              echo "Validation fix-up issue #${fix_num}: ai:merged label backfilled (proactive)."
              FIX_IS_MERGED="true"
            else
              echo "::warning::Validation fix-up issue #${fix_num}: proactive ai:merged backfill failed; will retry next cycle." >&2
              # Do not say "awaiting PR merge" here — the PR is already
              # merged; only the local ai:merged label transition is
              # pending.  Misleading wording made incident triage harder.
              echo "Validation fix-up issue #${fix_num}: PR is already merged, but ai:merged reconciliation is still pending; will retry next cycle."
            fi
          else
            FIX_EVIDENCE_STATUS=$?
            if [ "${FIX_EVIDENCE_STATUS}" -eq 2 ]; then
              echo "::warning::Validation fix-up issue #${fix_num}: unable to check merged PR evidence due to a timeline/API lookup failure; will retry next cycle." >&2
            fi
            echo "Validation fix-up issue #${fix_num}: still open; awaiting PR merge."
          fi
        else
          # Issue still open at a non-ai:ready-to-merge phase — nothing to
          # backfill this cycle.  Skipping the timeline walk here removes a
          # per-issue API round-trip (+ pagination) that the original loop
          # made every poll cycle for issues that were clearly still in
          # progress.
          echo "Validation fix-up issue #${fix_num}: still open; awaiting PR merge."
        fi
      fi

      if [ "${FIX_THIS_CLOSED_WITHOUT_MERGE}" = "true" ]; then
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
      # Per-batch stall ceiling: a single batch of fix-up issues cannot sit in
      # "in progress" forever.  Increment a cycle counter stored alongside the
      # active fix issues and escalate through mark_validation_failed when it
      # exceeds MAX_VALIDATION_FIX_BATCH_CYCLES.  The counter is reset when a
      # new fix-issues comment arrives (sync_validation_fix_issues_from_comments)
      # and when mark_validation_failed clears the active list.
      FIX_BATCH_CYCLES="$(jq -r '.validation_fix_issues_batch_cycles // 0' "${STATE_FILE}")"
      if ! [[ "${FIX_BATCH_CYCLES}" =~ ^[0-9]+$ ]]; then
        FIX_BATCH_CYCLES="0"
      fi
      FIX_BATCH_CYCLES=$(( FIX_BATCH_CYCLES + 1 ))

      jq --argjson c "${FIX_BATCH_CYCLES}" \
        '.validation_fix_issues_batch_cycles = $c' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

      if [ "${FIX_BATCH_CYCLES}" -gt "${MAX_VALIDATION_FIX_BATCH_CYCLES}" ]; then
        ACTIVE_FIX_ISSUES_SUMMARY="$(echo "${ACTIVE_FIX_ISSUES_JSON}" | jq -r 'map("#\(.)") | join(", ")' 2>/dev/null || echo "<unavailable>")"
        mark_validation_failed "Validation fix-up batch stalled: ${FIX_BATCH_CYCLES} poll cycles elapsed without all fix-up issues reaching ai:merged (MAX_VALIDATION_FIX_BATCH_CYCLES=${MAX_VALIDATION_FIX_BATCH_CYCLES}). Active issues: ${ACTIVE_FIX_ISSUES_SUMMARY}."
        continue
      fi

      echo "Validation fix-up issues are still in progress (batch cycle ${FIX_BATCH_CYCLES}/${MAX_VALIDATION_FIX_BATCH_CYCLES})."
      post_state_comment || true
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
       .validation_active_fix_issues = [] |
       .validation_fix_issues_batch_cycles = 0' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
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
  # with ai:validation-failed or ai:validate-failed label), a /revalidate
  # comment posted AFTER
  # the latest state comment resets counters and re-dispatches validation.
  if [ "${PROJECT_STATUS}" = "failed" ] \
    && (has_label "${TRACKING_LABELS}" "ai:validation-failed" || has_label "${TRACKING_LABELS}" "ai:validate-failed"); then
    REVALIDATE_COMMENT_JSON="$(echo "${COMMENTS}" | jq -c '
      (to_entries
        | map(select((.value.body // "") | (
            startswith("<!-- ORCHESTRATOR_STATE_V1")
            or test("^<!-- ORCHESTRATOR_STATE_V2 part=([0-9]+)/\\1 manifest=[0-9a-f]{64} -->")
            or startswith("<!-- revalidate-dedup:")
        )))
        | last
        | .key // -1) as $last_revalidate_boundary_idx |
      [to_entries[]
        | select(.key > $last_revalidate_boundary_idx and ((.value.body // "") | test("^\\s*/revalidate(\\s|$)"; "m")))
        | .value
      ]
      | last // empty
    ')"

    if [ -n "${REVALIDATE_COMMENT_JSON}" ] && [ "${REVALIDATE_COMMENT_JSON}" != "null" ]; then
      REVALIDATE_COMMENT_BODY="$(printf '%s' "${REVALIDATE_COMMENT_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
      REVALIDATE_COMMENT_ID="$(printf '%s' "${REVALIDATE_COMMENT_JSON}" | jq -r '.id // ""' 2>/dev/null || echo "")"
      REVALIDATE_COMMENT_TS="$(printf '%s' "${REVALIDATE_COMMENT_JSON}" | jq -r '.created_at // ""' 2>/dev/null || echo "")"
      REVALIDATE_COMMENT_ACTOR="$(printf '%s' "${REVALIDATE_COMMENT_JSON}" | jq -r '.user.login // ""' 2>/dev/null || echo "")"
      REVALIDATE_COMMENT_URL="$(printf '%s' "${REVALIDATE_COMMENT_JSON}" | jq -r '.html_url // ""' 2>/dev/null || echo "")"
      [ -n "${REVALIDATE_COMMENT_ACTOR}" ] || REVALIDATE_COMMENT_ACTOR="${GITHUB_ACTOR:-unknown}"
      [ -n "${REVALIDATE_COMMENT_TS}" ] || REVALIDATE_COMMENT_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      if [ -z "${REVALIDATE_COMMENT_URL}" ] && [[ "${REVALIDATE_COMMENT_ID}" =~ ^[0-9]+$ ]]; then
        REVALIDATE_COMMENT_URL="$(_gh_url "issues/${TRACKING_NUM}#issuecomment-${REVALIDATE_COMMENT_ID}")"
      fi
      REVALIDATE_REASON="$(REVALIDATE_COMMENT_BODY="${REVALIDATE_COMMENT_BODY}" python3 - <<'PY'
from __future__ import annotations

import os

body = os.environ.get("REVALIDATE_COMMENT_BODY", "").lstrip()
if body.startswith("/revalidate"):
    print(body[len("/revalidate"):].strip())
else:
    print("")
PY
      )"
      REVALIDATE_PRIOR_OUTCOME="$(jq -r '.validation_last_raw_status // .validation_failure_class // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
      if [ -z "${REVALIDATE_PRIOR_OUTCOME}" ]; then
        if has_label "${TRACKING_LABELS}" "ai:harness-broken"; then
          REVALIDATE_PRIOR_OUTCOME="harness_error"
        elif has_label "${TRACKING_LABELS}" "ai:validate-failed"; then
          REVALIDATE_PRIOR_OUTCOME="validate-failed"
        elif has_label "${TRACKING_LABELS}" "ai:validation-failed"; then
          REVALIDATE_PRIOR_OUTCOME="validation-failed"
        else
          REVALIDATE_PRIOR_OUTCOME="failed"
        fi
      fi
      REVALIDATE_PRIOR_CONTEXT="$(jq -r '.validation_failure_reason // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
      if [ -z "${REVALIDATE_PRIOR_CONTEXT}" ] && has_label "${TRACKING_LABELS}" "ai:harness-broken"; then
        REVALIDATE_PRIOR_CONTEXT="tracking issue labeled ai:harness-broken"
      fi

      REVALIDATE_MEMORY_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
      if [ -z "${REVALIDATE_MEMORY_BRANCH}" ] || [ "${REVALIDATE_MEMORY_BRANCH}" = "null" ]; then
        REVALIDATE_MEMORY_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "")"
      fi
      REVALIDATE_INTEGRATION_SHA=""
      if [ -n "${REVALIDATE_MEMORY_BRANCH}" ]; then
        REVALIDATE_INTEGRATION_SHA="$(_branch_head_sha "${REVALIDATE_MEMORY_BRANCH}" || echo "")"
      fi

      REVALIDATE_DEDUPE_WINDOW_SECONDS=300
      REVALIDATE_DEDUPE_HIT="false"
      REVALIDATE_PREVIOUS_TS=""
      if [ -n "${REVALIDATE_INTEGRATION_SHA}" ]; then
        REVALIDATE_EVENTS_JSON="$(memory_revalidate_events_get \
          --repo-root . \
          --memory-branch "${AI_MEMORY_BRANCH:-ai-memory}" \
          --memory-root "${AI_MEMORY_ROOT:-ai-memory}" \
          --repo "${GITHUB_REPOSITORY}" \
          --tracking-issue "${TRACKING_NUM}" \
          --integration-sha "${REVALIDATE_INTEGRATION_SHA}" 2>/dev/null || echo '{"ok": true, "enabled": true, "hit": false, "events": null}')"
        REVALIDATE_RECENT_ENTRY="$(printf '%s' "${REVALIDATE_EVENTS_JSON}" | jq -c --arg actor "${REVALIDATE_COMMENT_ACTOR}" '(.events.entries // []) | map(select((.actor // "") == $actor)) | last // empty' 2>/dev/null || echo "")"
        if [ -n "${REVALIDATE_RECENT_ENTRY}" ] && [ "${REVALIDATE_RECENT_ENTRY}" != "null" ]; then
          REVALIDATE_PREVIOUS_TS="$(printf '%s' "${REVALIDATE_RECENT_ENTRY}" | jq -r '.timestamp_utc // ""' 2>/dev/null || echo "")"
          REVALIDATE_PREVIOUS_EPOCH="$(_iso8601_to_epoch "${REVALIDATE_PREVIOUS_TS}" || echo "")"
          REVALIDATE_COMMENT_EPOCH="$(_iso8601_to_epoch "${REVALIDATE_COMMENT_TS}" || echo "")"
          if [[ "${REVALIDATE_PREVIOUS_EPOCH}" =~ ^[0-9]+$ ]] \
            && [[ "${REVALIDATE_COMMENT_EPOCH}" =~ ^[0-9]+$ ]] \
            && [ "${REVALIDATE_COMMENT_EPOCH}" -ge "${REVALIDATE_PREVIOUS_EPOCH}" ] \
            && [ $(( REVALIDATE_COMMENT_EPOCH - REVALIDATE_PREVIOUS_EPOCH )) -lt "${REVALIDATE_DEDUPE_WINDOW_SECONDS}" ]; then
            REVALIDATE_DEDUPE_HIT="true"
          fi
        fi
      fi

      if [ "${REVALIDATE_DEDUPE_HIT}" = "true" ]; then
        echo "  /revalidate deduped for project #${TRACKING_NUM}: actor=@${REVALIDATE_COMMENT_ACTOR} integration_sha=${REVALIDATE_INTEGRATION_SHA:-unknown} prior_ts=${REVALIDATE_PREVIOUS_TS}."
        post_tracking_comment "<!-- revalidate-dedup:${REVALIDATE_COMMENT_ID:-0}:${REVALIDATE_INTEGRATION_SHA:-unknown} -->

Already processed /revalidate from @${REVALIDATE_COMMENT_ACTOR} at ${REVALIDATE_PREVIOUS_TS}."
        continue
      fi

      echo "  /revalidate requested for project #${TRACKING_NUM}. Resetting validation state."
      jq \
        '.status = "validating" |
         .validation_cycle = 1 |
         .validation_recovery_count = 0 |
         .validation_active_fix_issues = [] |
         .validation_fix_issues_batch_cycles = 0 |
         .validation_last_dispatch_cycle = 0 |
         .validation_completed_cycle = null |
         del(.validation_failure_reason) |
         del(.validation_failure_class) |
         del(.validation_last_raw_status)' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment || true
      gh_retry gh issue edit "${TRACKING_NUM}" \
        --repo "${GITHUB_REPOSITORY}" \
        --remove-label "ai:validation-failed" >/dev/null || true
      gh_retry gh issue edit "${TRACKING_NUM}" \
        --repo "${GITHUB_REPOSITORY}" \
        --remove-label "ai:validate-failed" >/dev/null || true
      gh_retry gh issue edit "${TRACKING_NUM}" \
        --repo "${GITHUB_REPOSITORY}" \
        --remove-label "ai:harness-broken" >/dev/null 2>&1 || true
      set_tracking_phase_label "ai:validating"
      REVALIDATE_FINAL_PR="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
      if [[ "${REVALIDATE_FINAL_PR}" =~ ^[0-9]+$ ]]; then
        update_eager_pr_validation_status_section "${REVALIDATE_FINAL_PR}" "Revalidating after operator reset." || true
      fi
      if [ -n "${REVALIDATE_INTEGRATION_SHA}" ]; then
        REVALIDATE_COMMENT_ID_JSON='null'
        if [[ "${REVALIDATE_COMMENT_ID}" =~ ^[0-9]+$ ]]; then
          REVALIDATE_COMMENT_ID_JSON="${REVALIDATE_COMMENT_ID}"
        fi
        REVALIDATE_ENTRY_FILE="$(mktemp "${TMPDIR:-/tmp}/revalidate_event.XXXXXX")"
        jq -n \
          --arg actor "${REVALIDATE_COMMENT_ACTOR}" \
          --arg timestamp_utc "${REVALIDATE_COMMENT_TS}" \
          --arg prior_outcome "${REVALIDATE_PRIOR_OUTCOME}" \
          --arg prior_context "${REVALIDATE_PRIOR_CONTEXT}" \
          --arg reason "${REVALIDATE_REASON}" \
          --arg source_comment_url "${REVALIDATE_COMMENT_URL}" \
          --argjson source_comment_id "${REVALIDATE_COMMENT_ID_JSON}" '
            {
              actor: $actor,
              timestamp_utc: $timestamp_utc,
              prior_outcome: (if $prior_outcome == "" then null else $prior_outcome end),
              prior_context: (if $prior_context == "" then null else $prior_context end),
              reason: (if $reason == "" then null else $reason end),
              source_comment_id: $source_comment_id,
              source_comment_url: (if $source_comment_url == "" then null else $source_comment_url end)
            }
          ' > "${REVALIDATE_ENTRY_FILE}"
        memory_revalidate_events_append \
          --repo-root . \
          --memory-branch "${AI_MEMORY_BRANCH:-ai-memory}" \
          --memory-root "${AI_MEMORY_ROOT:-ai-memory}" \
          --repo "${GITHUB_REPOSITORY}" \
          --tracking-issue "${TRACKING_NUM}" \
          --integration-sha "${REVALIDATE_INTEGRATION_SHA}" \
          --entry-file "${REVALIDATE_ENTRY_FILE}" >/dev/null 2>&1 || true
        rm -f "${REVALIDATE_ENTRY_FILE}"
      fi
      post_tracking_comment "## 🔁 Validation reset via /revalidate

All validation counters cleared. Re-dispatching validation (cycle 1)."
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
  # NOT from validation (no ai:validation-failed or ai:validate-failed label),
  # a /judge_resume comment posted AFTER the latest state comment resumes
  # the project. Counters are preserved by default and can be reset
  # explicitly via flags: --reset-recovery, --reset-stall, or --force.
  if [ "${PROJECT_STATUS}" = "failed" ] \
    && ! has_label "${TRACKING_LABELS}" "ai:validation-failed" \
    && ! has_label "${TRACKING_LABELS}" "ai:validate-failed"; then
    JUDGE_RESUME_BODY="$(echo "${COMMENTS}" | jq -r '
      (to_entries | map(select((.value.body // "") | (startswith("<!-- ORCHESTRATOR_STATE_V1") or test("^<!-- ORCHESTRATOR_STATE_V2 part=([0-9]+)/\\1 manifest=[0-9a-f]{64} -->")))) | last | .key // -1) as $last_state_idx |
      [to_entries[]
        | select(.key > $last_state_idx and (.value.body | test("^\\s*/judge_resume(\\s|$)"; "m")))
      ]
      | last
      | .value.body // ""
    ')"

    if [ -n "${JUDGE_RESUME_BODY}" ]; then
      PREV_JUDGE_STALL="$(jq -r '.judge_stall_cycles // .judge_cycle' "${STATE_FILE}")"
      PREV_RECOVERY="$(jq -r '.recovery_count // (if .recovery_attempted == true then 1 else 0 end)' "${STATE_FILE}")"

      RESET_STALL="false"
      RESET_RECOVERY="false"
      if echo "${JUDGE_RESUME_BODY}" | grep -Eq '(^|[[:space:]])--force([[:space:]]|$)'; then
        RESET_STALL="true"
        RESET_RECOVERY="true"
      else
        if echo "${JUDGE_RESUME_BODY}" | grep -Eq '(^|[[:space:]])--reset-stall([[:space:]]|$)'; then
          RESET_STALL="true"
        fi
        if echo "${JUDGE_RESUME_BODY}" | grep -Eq '(^|[[:space:]])--reset-recovery([[:space:]]|$)'; then
          RESET_RECOVERY="true"
        fi
      fi

      NEW_JUDGE_STALL="${PREV_JUDGE_STALL}"
      STALL_ACTION="preserved (${PREV_JUDGE_STALL})"
      if [ "${RESET_STALL}" = "true" ]; then
        NEW_JUDGE_STALL="0"
        STALL_ACTION="reset (${PREV_JUDGE_STALL} -> 0)"
      fi

      NEW_RECOVERY="${PREV_RECOVERY}"
      RECOVERY_ACTION="preserved (${PREV_RECOVERY})"
      if [ "${RESET_RECOVERY}" = "true" ]; then
        NEW_RECOVERY="0"
        RECOVERY_ACTION="reset (${PREV_RECOVERY} -> 0)"
      fi

      RESUME_SUMMARY="judge_stall_cycles: ${STALL_ACTION}; recovery_count: ${RECOVERY_ACTION}"
      echo "  /judge_resume requested for project #${TRACKING_NUM}. ${RESUME_SUMMARY}. Status failed -> in_progress."

      jq \
        --argjson new_judge_stall "${NEW_JUDGE_STALL}" \
        --argjson new_recovery "${NEW_RECOVERY}" \
        '.status = "in_progress" |
         .judge_stall_cycles = $new_judge_stall |
         .recovery_count = $new_recovery' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment || true
      post_tracking_comment "## ▶️ Project resumed via /judge_resume

Counter handling: ${RESUME_SUMMARY}
Status: failed -> in_progress

The poller will resume processing on the next cycle."
      tg_notify "/judge_resume: project #${TRACKING_NUM} resumed from failed state. ${RESUME_SUMMARY}." "WARNING"
      # Fall through to normal processing below instead of continuing
      PROJECT_STATUS="in_progress"
    fi
  fi

  if [ "${PROJECT_STATUS}" = "complete" ] || [ "${PROJECT_STATUS}" = "failed" ] || [ "${PROJECT_STATUS}" = "validation-failed" ]; then
    handle_comprehensive_release_callback_if_needed "${PROJECT_STATUS}" "${TRACKING_LABELS}" "${COMMENTS:-[]}"
    if [ "${PROJECT_STATUS}" = "failed" ] || [ "${PROJECT_STATUS}" = "validation-failed" ]; then
      if completion_status_comment_failed_state_observation; then
        _completion_status_failed_observation_rc=0
      else
        _completion_status_failed_observation_rc=$?
      fi
      if [ "${_completion_status_failed_observation_rc}" -eq 1 ]; then
        set_failed_completion_status_comment \
          "Project is in a terminal \`failed\` state. Manual intervention required. See the latest failure comment on this tracking issue for the diagnostic detail."
      fi
      if has_label "${TRACKING_LABELS}" "ai:force-merge" \
        && project_is_validation_origin_terminal_failure "${PROJECT_STATUS}" "${TRACKING_LABELS}"; then
        _terminal_force_merge_final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
        if [[ "${_terminal_force_merge_final_pr}" =~ ^[0-9]+$ ]]; then
          compute_cycle_integration_ahead_by
          maybe_apply_force_merge_bypass "${_terminal_force_merge_final_pr}" "${CWS_INTEGRATION_BRANCH}" "${CWS_AHEAD_BY}" || true
        fi
      fi
    fi
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

  # Compute the integration branch's ahead_by vs the default branch once per
  # cycle before any merge paths run so backpressure, check-wave-status, and the
  # staleness alert all reuse the same compare probe.
  compute_cycle_integration_ahead_by
  INTEGRATION_BACKPRESSURE_BLOCK_MERGES="false"
  if integration_backpressure_active_for_ahead_by "${CWS_AHEAD_BY}"; then
    INTEGRATION_BACKPRESSURE_BLOCK_MERGES="true"
  fi

  # ---------------------------------------------------------------
  # Backward scan: check prior waves for non-terminal issues
  # ---------------------------------------------------------------
  # Safety net: if a fix-up issue was added to a prior wave (or a
  # status update was missed), detect it here and update state /
  # attempt auto-merge so the issue doesn't stay orphaned forever.
  WAVE_IDX=$(( CURRENT_WAVE - 1 ))

  PRIOR_WAVE_REMEDIATED="false"
  if [ "${WAVE_IDX}" -gt 0 ]; then
    PRIOR_NON_TERMINAL_BATCH="$(jq -c --argjson wave_idx "${WAVE_IDX}" '
      [
        .waves[0:$wave_idx][]?.issues[]?
        | select(.status != "merged" and .status != "closed" and .status != "skipped")
        | (.github_issue | tostring)
        | select(test("^[0-9]+$"))
        | tonumber
      ] | unique
    ' "${STATE_FILE}" 2>/dev/null || echo '[]')"
    PRIOR_LABELS_JSON="$(_fetch_issue_labels_batch_graphql "${PRIOR_NON_TERMINAL_BATCH}")"
    if ! printf '%s' "${PRIOR_LABELS_JSON}" | jq -e 'type == "object"' >/dev/null 2>&1; then
      PRIOR_LABELS_JSON='{}'
    fi

    for prior_idx in $(seq 0 $(( WAVE_IDX - 1 ))); do
      PRIOR_NON_TERMINAL="$(jq -r --argjson wi "${prior_idx}" \
        '.waves[$wi].issues[] | select(.status != "merged" and .status != "closed" and .status != "skipped") | .github_issue' \
        "${STATE_FILE}" 2>/dev/null || echo "")"
      for pw_inum in ${PRIOR_NON_TERMINAL}; do
        [ -n "${pw_inum}" ] && [ "${pw_inum}" != "null" ] || continue
        echo "  [backward-scan] Prior wave $((prior_idx + 1)) issue #${pw_inum} is non-terminal. Checking labels..."
        if echo "${PRIOR_LABELS_JSON}" | jq -e --arg key "${pw_inum}" 'has($key)' >/dev/null 2>&1; then
          PW_LABELS="$(echo "${PRIOR_LABELS_JSON}" | jq -c --arg key "${pw_inum}" '.[$key] // []')"
        else
          PW_LABELS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels" --jq '[.[].name]' || echo '[]')"
          PRIOR_LABELS_JSON="$(echo "${PRIOR_LABELS_JSON}" | jq -c --arg key "${pw_inum}" --argjson labels "${PW_LABELS:-[]}" '. + {($key): $labels}' 2>/dev/null || echo "${PRIOR_LABELS_JSON}")"
        fi
        [ -n "${PW_LABELS}" ] || PW_LABELS='[]'
        PW_LOCAL_ID="$(jq -r --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
          '.waves[$wi].issues[] | select((.github_issue | tostring) == $inum) | .id' "${STATE_FILE}" | head -n 1)"

        if echo "${PW_LABELS}" | jq -e 'index("ai:merged")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is now ai:merged. Updating state."
          jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
            '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "merged"' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          PRIOR_WAVE_REMEDIATED="true"
          # Capture intent fingerprints for the late-detected merged
          # sub-issue (going-forward only — see capture helper docs).
          _bws_integ="$(jq -r '.integration_branch // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
          if [ -n "${_bws_integ}" ]; then
            _bws_pr="$(_subissue_closing_pr_number "${pw_inum}" || echo "")"
            if [[ "${_bws_pr}" =~ ^[0-9]+$ ]]; then
              capture_intent_fingerprints_for_merged_subissue "${pw_inum}" "${_bws_pr}" || true
            fi
            unset _bws_pr
          fi
          unset _bws_integ
        elif echo "${PW_LABELS}" | jq -e 'index("ai:closed")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is now ai:closed. Updating state."
          jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
            '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "closed"' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          PRIOR_WAVE_REMEDIATED="true"
        elif echo "${PW_LABELS}" | jq -e 'index("ai:ready-to-merge")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is ai:ready-to-merge. Attempting auto-merge..."
          PW_PR="$(_linked_prs_by_branch_name "${pw_inum}" 2>/dev/null | sort -rn | head -n1 || true)"
          if ! [[ "${PW_PR}" =~ ^[0-9]+$ ]]; then
            PW_PR="$(_linked_prs_by_body_reference "${pw_inum}" 2>/dev/null | sort -rn | head -n1 || true)"
          fi
          if ! [[ "${PW_PR}" =~ ^[0-9]+$ ]]; then
            PW_PR="$(_subissue_closing_pr_number "${pw_inum}" || echo "")"
          fi
          if [[ "${PW_PR}" =~ ^[0-9]+$ ]]; then
            _pw_pr_json="$(_fetch_pr_json "${PW_PR}")"
            PW_PR_STATE="$(_jq_field "${_pw_pr_json}" '.state' 'open|closed|merged')"
            PW_PR_MERGEABLE="$(_jq_field "${_pw_pr_json}" '.mergeable' 'true|false')"
            PW_PR_MERGED="$(_jq_field "${_pw_pr_json}" '.merged_at != null' 'true|false')"
            _pw_head_sha="$(_jq_field "${_pw_pr_json}" '.head.sha')"
            # Defensive prior-wave reconcile (added 2026-04-27): when the
            # linked PR is already merged, the wave-current poller's
            # reconcile_managed_issue_labels never ran for this issue
            # (it only iterates current-wave ISSUE_NUMS), so the
            # ai:ready-to-merge -> ai:merged transition that
            # close_merged_issues_sweep depends on never fired. Promote
            # the label here, mirror the ai:merged-branch state mutation,
            # and capture intent fingerprints just like the late-detected
            # merged path above (lines 8176-8194). Idempotent: gh issue
            # edit --add-label is a no-op if ai:merged is already on the
            # issue. Merged-detection uses .merged_at != null because
            # GitHub's REST API returns .state == "closed" for merged
            # PRs (not "merged") — same convention as the standalone
            # stall-recovery merged-PR guard at line ~6463.
            if [ "${PW_PR_MERGED}" = "true" ]; then
              echo "  [backward-scan] #${pw_inum} ai:ready-to-merge but linked PR #${PW_PR} is already merged — promoting to ai:merged."
              ensure_label_exists "ai:merged" >/dev/null 2>&1 || true
              gh_retry gh issue edit "${pw_inum}" --repo "${GITHUB_REPOSITORY}" \
                --add-label "ai:merged" --remove-label "ai:ready-to-merge" >/dev/null 2>&1 \
                || echo "::warning::[backward-scan] #${pw_inum} ai:merged label edit failed; close_merged_issues_sweep will retry next cycle."
              jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
                '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "merged"' \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
              PRIOR_WAVE_REMEDIATED="true"
              _bws_integ="$(jq -r '.integration_branch // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
              if [ -n "${_bws_integ}" ]; then
                _bws_pr="$(_subissue_closing_pr_number "${pw_inum}" || echo "")"
                if [[ "${_bws_pr}" =~ ^[0-9]+$ ]]; then
                  capture_intent_fingerprints_for_merged_subissue "${pw_inum}" "${_bws_pr}" || true
                fi
                unset _bws_pr
              fi
              unset _bws_integ
            elif [ "${PW_PR_STATE}" = "open" ] && [ "${PW_PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${PW_PR}" "${_pw_head_sha}"; then
              if [ "${INTEGRATION_BACKPRESSURE_BLOCK_MERGES:-false}" = "true" ]; then
                _integration_backpressure_effective_threshold _bws_effective_threshold
                echo "  [backward-scan] Backpressure active (ahead_by=${CWS_AHEAD_BY}, threshold=${ORCH_INTEGRATION_MAX_AHEAD_COMMITS}, effective_threshold=${_bws_effective_threshold}); deferring auto-merge of PR #${PW_PR} for prior-wave issue #${pw_inum}."
                continue
              fi
              if gh_retry gh pr merge "${PW_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null \
                || gh_retry gh pr merge "${PW_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null; then
                refresh_integration_backpressure_gate_after_merge || true
              fi
            elif [ "${PW_PR_STATE}" = "open" ] && [ "${PW_PR_MERGEABLE}" = "false" ]; then
              gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PW_PR}/update-branch" \
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
      post_state_comment || true
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
    # Backstop against re-minting an issue that already exists on GitHub when
    # the loaded state has lost the local_id -> github_issue mapping.  A stale
    # or rewound state snapshot (e.g. one whose write recording the original
    # issue never durably landed, so the poller acted on an older snapshot)
    # can present a wave entry with github_issue == null AND no
    # issue_number_map entry even though the issue was already created — the
    # project #3542 duplicate-Phase-1 failure mode.  The in-state map check
    # below only catches duplicates the current snapshot already knows about,
    # so before creating we consult a live child-issue search (built lazily on
    # first need and reused for the rest of the loop).
    #
    # §15 API hygiene: this reuses the reconstruction path's child-issue
    # search shape (the ISSUE_MAP_JSON build earlier in this file).  Deferred
    # creation runs outside the reconstruction branch, so that map is not in
    # scope here and cannot be shared; the search is issued at most once per
    # wave-creation cycle and only when an uncreated id is missing from the
    # in-state map.
    DEFERRED_EXISTING_MAP_JSON='{}'
    DEFERRED_EXISTING_LOOKUP_DONE=false
    DEFERRED_EXISTING_LOOKUP_OK=false
    DEFERRED_BLOCKER_DETAILS_JSON='{}'
    if [ "${RUNTIME_BLOCKER_CHECK_ENABLED}" = "true" ] \
      && jq -e '.dependency_edges | type == "array" and length > 0' "${STATE_FILE}" >/dev/null 2>&1; then
      _deferred_wave_issue_nums_json="$(jq -c ".waves[${WAVE_IDX}].issues | [.[].github_issue | select(type == \"number\")]" "${STATE_FILE}" 2>/dev/null || echo '[]')"
      # Current-wave deferred creation runs before the later
      # _current_wave_details_json cache population. Prefetch once here so
      # blocker gating sees live labels/linked-PR truth and does not lag an
      # extra poll tick when a blocker terminalized between cycles.
      DEFERRED_BLOCKER_DETAILS_JSON="$(_fetch_candidate_issue_details_graphql "${_deferred_wave_issue_nums_json}")"
      if ! printf '%s' "${DEFERRED_BLOCKER_DETAILS_JSON}" | jq -e 'type == "object"' >/dev/null 2>&1; then
        DEFERRED_BLOCKER_DETAILS_JSON='{}'
      fi
    fi
    while IFS= read -r local_id; do
      [ -n "${local_id}" ] || continue

      # Check if already in issue_number_map (created in a prior cycle but state not synced)
      EXISTING_NUM="$(jq -r ".issue_number_map[\"${local_id}\"] // empty" "${STATE_FILE}")"
      if [ -n "${EXISTING_NUM}" ]; then
        echo "  ${local_id}: already mapped to #${EXISTING_NUM}, updating wave entry."
        jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${EXISTING_NUM} | .status = \"pending\")" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        ISSUE_NUMS="${ISSUE_NUMS} ${EXISTING_NUM}"
        DEFERRED_STATE_CHANGED=true
        continue
      fi

      # Backstop: before minting a brand-new issue, confirm no issue already
      # exists on GitHub for this Local ID (open OR closed).  The issue_number_map
      # check above only catches duplicates the current snapshot already knows
      # about; a stale/rewound snapshot can have both github_issue == null AND no
      # map entry while the real issue (e.g. an already-merged, closed one)
      # exists on GitHub.  Look it up once, lazily, and adopt the existing issue
      # instead of creating a duplicate.
      if [ "${DEFERRED_EXISTING_LOOKUP_DONE}" != "true" ]; then
        DEFERRED_EXISTING_LOOKUP_DONE=true
        if _deferred_child_items="$(gh_retry gh api "search/issues" \
          -f q="repo:${GITHUB_REPOSITORY} is:issue \"Tracking issue: #${TRACKING_NUM}\" in:body" \
          --jq '.items // []' 2>/dev/null)"; then
          if DEFERRED_EXISTING_MAP_JSON="$(printf '%s' "${_deferred_child_items}" | jq '
            reduce .[] as $issue ({};
              (try ($issue.body | capture("Local ID: `(?<id>[^`]+)`")) catch null) as $cap |
              if $cap == null then .
              else
                ($cap.id) as $lid |
                if (has($lid) and .[$lid] <= $issue.number) then .
                else . + {($lid): $issue.number} end
              end
            )
          ' 2>/dev/null)" && printf '%s' "${DEFERRED_EXISTING_MAP_JSON}" | jq -e 'type == "object"' >/dev/null 2>&1; then
            DEFERRED_EXISTING_LOOKUP_OK=true
          else
            DEFERRED_EXISTING_MAP_JSON='{}'
          fi
        fi
      fi

      GH_EXISTING_NUM="$(printf '%s' "${DEFERRED_EXISTING_MAP_JSON}" | jq -r --arg id "${local_id}" '.[$id] // empty' 2>/dev/null || true)"
      if [ -n "${GH_EXISTING_NUM}" ] && [[ "${GH_EXISTING_NUM}" =~ ^[0-9]+$ ]]; then
        echo "  ${local_id}: already exists on GitHub as #${GH_EXISTING_NUM} (absent from issue_number_map); adopting it instead of creating a duplicate."
        jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${GH_EXISTING_NUM} | .status = \"pending\") | .issue_number_map[\"${local_id}\"] = ${GH_EXISTING_NUM} | del(.pending_issue_defs[\"${local_id}\"])" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        ISSUE_NUMS="${ISSUE_NUMS} ${GH_EXISTING_NUM}"
        DEFERRED_STATE_CHANGED=true
        continue
      fi

      # Fail closed: if the existence lookup itself failed this cycle we cannot
      # rule out an already-created issue, so skip creation and retry on the
      # next poll rather than risk a duplicate (matching how the reconstruction
      # path treats an unreadable API as "try again later" instead of acting on
      # incomplete information).
      if [ "${DEFERRED_EXISTING_LOOKUP_OK}" != "true" ]; then
        echo "::warning::Could not verify whether ${local_id} already exists on GitHub (child-issue lookup failed this cycle); skipping creation to avoid a duplicate. Will retry next poll."
        continue
      fi

      # Get issue definition from pending_issue_defs
      ISSUE_DEF="$(jq -c ".pending_issue_defs[\"${local_id}\"] // empty" "${STATE_FILE}")"
      if [ -z "${ISSUE_DEF}" ]; then
        echo "::warning::No pending definition for ${local_id} in wave ${CURRENT_WAVE}, skipping."
        continue
      fi

      if ! _runtime_blocker_dispatch_eligible "${local_id}" "${CURRENT_WAVE}" "${DEFERRED_BLOCKER_DETAILS_JSON}"; then
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

      if ! phase_cap_can_dispatch "ai:clarification" "create_issue" "${local_id}"; then
        continue
      fi

      ensure_label_exists "ai:clarification"
      ensure_label_exists "ai:orchestrator-managed"
      NEW_URL="$(gh_retry gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${DEF_TITLE}" \
        --body "${FULL_BODY}" \
        --label "ai:clarification" \
        --label "ai:orchestrator-managed" 2>/dev/null || echo "")"

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
      phase_cap_note_dispatch "ai:clarification"
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
      post_state_comment || true
      # Only announce genuinely-created issues.  A pure-adoption cycle — where
      # the backstop above healed the state map against an issue that already
      # existed on GitHub — persists the corrected state but must not claim it
      # "Created" anything.
      _deferred_created_trimmed="$(printf '%s' "${DEFERRED_CREATED_NUMS}" | tr -s '[:space:]' ' ' | sed 's/^ *//; s/ *$//')"
      if [ -n "${_deferred_created_trimmed}" ]; then
        post_tracking_comment "## 🔧 Deferred Issue Creation (Wave ${CURRENT_WAVE})

Issues in this wave had not been created yet (likely from an interrupted initial setup). Created them now:

$(for inum in ${DEFERRED_CREATED_NUMS}; do
  [ -n "${inum}" ] && echo "- #${inum}"
done)

These issues will enter the AI pipeline (clarify → plan → implement → review)."
        tg_notify "Deferred issue creation for wave ${CURRENT_WAVE} of project #${TRACKING_NUM}. Created missing GitHub issues." "WARNING"
      fi
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

  _wave_issue_nums=()
  for inum in ${ISSUE_NUMS}; do
    [ -n "${inum}" ] && [ "${inum}" != "null" ] || continue
    _wave_issue_nums+=("${inum}")
  done

  _wave_issue_nums_json='[]'
  if [ "${#_wave_issue_nums[@]}" -gt 0 ]; then
    _wave_issue_nums_json="$(printf '%s\n' "${_wave_issue_nums[@]}" | jq -R 'select(length > 0) | select(test("^[0-9]+$")) | tonumber' | jq -s '.')"
  fi

  # Batch-fetch current-wave issue details once per cycle.  The state
  # field feeds ISSUE_STATES_JSON cache-first population below; labels
  # still come from the dedicated label batch helper so the two cache
  # paths remain independently fail-open.
  _current_wave_details_json="$(_fetch_candidate_issue_details_graphql "${_wave_issue_nums_json}")"
  if ! printf '%s' "${_current_wave_details_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    _current_wave_details_json='{}'
  fi

  LABELS_JSON="$(_fetch_issue_labels_batch_graphql "${_wave_issue_nums_json}")"
  if ! printf '%s' "${LABELS_JSON}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    LABELS_JSON='{}'
  fi

  if [ "${LABELS_JSON}" = "{}" ] && [ "${#_wave_issue_nums[@]}" -gt 0 ]; then
    echo "  [labels] GraphQL batch failed, falling back to per-issue REST"
  fi

  for inum in "${_wave_issue_nums[@]}"; do
    if echo "${LABELS_JSON}" | jq -e --arg key "${inum}" 'has($key)' >/dev/null 2>&1; then
      continue
    fi
    LABELS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels" --jq '[.[].name]' || echo '[]')"
    [ -n "${LABELS}" ] || LABELS='[]'
    LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${inum}" --argjson labels "${LABELS}" '. + {($key): $labels}' 2>/dev/null || echo "${LABELS_JSON}")"
  done

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

    ISSUE_STATE=""
    if printf '%s' "${_current_wave_details_json}" | jq -e --arg key "${inum}" 'has($key)' >/dev/null 2>&1; then
      ISSUE_STATE="$(printf '%s' "${_current_wave_details_json}" | jq -r --arg key "${inum}" '.[$key].state // empty' 2>/dev/null | grep -xE 'open|closed' || true)"
    fi
    if [ -z "${ISSUE_STATE}" ]; then
      ISSUE_STATE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}" --jq '.state' | grep -xE 'open|closed' || echo "open")"
    fi
    [ -n "${ISSUE_STATE}" ] || ISSUE_STATE="open"
    ISSUE_STATES_JSON="$(echo "${ISSUE_STATES_JSON}" | jq -c --arg key "${inum}" --arg state "${ISSUE_STATE}" '. + {($key): $state}' 2>/dev/null || echo "${ISSUE_STATES_JSON}")"

    LINKED_PR_NUM="$(_issue_cross_ref_pr_number_last "${inum}" 2>/dev/null || echo "")"
    PR_STATE="unknown"
    PR_MERGED="false"
    if [[ "${LINKED_PR_NUM}" =~ ^[0-9]+$ ]]; then
      _linked_pr_json="$(_fetch_pr_json "${LINKED_PR_NUM}")"
      PR_STATE="$(_jq_field "${_linked_pr_json}" '.state' 'open|closed|merged')"
      PR_MERGED="$(_jq_field "${_linked_pr_json}" '.merged_at != null' 'true|false')"
      [ -n "${PR_MERGED}" ] || PR_MERGED="false"
    fi
    PR_STATES_JSON="$(echo "${PR_STATES_JSON}" | jq -c --arg key "${inum}" --arg state "${PR_STATE}" --arg merged "${PR_MERGED}" '. + {($key): {state: $state, merged: ($merged == "true")}}' 2>/dev/null || echo "${PR_STATES_JSON}")"

    BEFORE_LABELS="$(echo "${LABELS_JSON}" | jq -c --arg key "${inum}" '.[$key] // []')"
    AFTER_LABELS="$(reconcile_managed_issue_labels "${inum}" "${BEFORE_LABELS}" "${ISSUE_STATE}" "${PR_STATE}" "${PR_MERGED}")"
    if [ "${BEFORE_LABELS}" != "${AFTER_LABELS}" ]; then
      RECONCILE_LABELS_CHANGED=true
      LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${inum}" --argjson labels "${AFTER_LABELS}" '. + {($key): $labels}' 2>/dev/null || echo "${LABELS_JSON}")"
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

  # CWS_* was computed before the merge paths ran so this wave-status block,
  # backpressure, and the staleness alert all reuse the same compare result.
  check_integration_branch_staleness "${CWS_INTEGRATION_BRANCH}" "${CWS_DEFAULT_BRANCH:-}" "${CWS_AHEAD_BY}" || true

  WAVE_STATUS="$(python3 scripts/orchestrate_lib.py check-wave-status \
    --state-file "${STATE_FILE}" \
    --labels-json "${LABELS_JSON}" \
    --issue-states-json "${ISSUE_STATES_JSON}" \
    --pr-states-json "${PR_STATES_JSON}" \
    --integration-ahead-by "${CWS_AHEAD_BY}")"

  write_state_snapshot_tracker_export "${TRACKING_NUM}" "${TRACKING_TITLE}" || true

  echo "Wave status: ${WAVE_STATUS}"
  WAVE_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.wave_complete')"
  ANY_FAILED="$(echo "${WAVE_STATUS}" | jq -r '.any_failed')"
  PROJECT_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.project_complete')"

  # Ensure the integration→default PR exists as soon as the integration
  # branch is ahead of default, even before validation completes. Fail
  # closed on compare ambiguity by requiring a numeric ahead_by > 0.
  EAGER_FINAL_MERGE_STATUS="$(jq -r '.final_merge_status // "pending"' "${STATE_FILE}" 2>/dev/null || echo "pending")"
  EAGER_SYNC_STATUS="$(jq -r '.sync.status // "active"' "${STATE_FILE}" 2>/dev/null || echo "active")"
  if [ -n "${CWS_INTEGRATION_BRANCH}" ] \
    && [ -n "${CWS_DEFAULT_BRANCH:-}" ] \
    && [[ "${CWS_AHEAD_BY:-}" =~ ^[0-9]+$ ]] \
    && [ "${CWS_AHEAD_BY}" -gt 0 ] \
    && [ "${EAGER_SYNC_STATUS}" != "superseded-by-main" ] \
    && [ "${EAGER_FINAL_MERGE_STATUS}" != "superseded-by-main" ]; then
    EAGER_PROJECT_TITLE="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}" 2>/dev/null || echo "Orchestrator project")"
    EAGER_FINAL_PR="$(ensure_eager_final_pr "${CWS_INTEGRATION_BRANCH}" "${CWS_DEFAULT_BRANCH}" "${EAGER_PROJECT_TITLE}" || true)"
    if [[ "${EAGER_FINAL_PR}" =~ ^[0-9]+$ ]]; then
      EAGER_FINAL_PR_BEFORE="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
      EAGER_FINAL_ERROR_BEFORE="$(jq -r '.final_merge_error // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
      if [ "${EAGER_FINAL_PR_BEFORE}" != "${EAGER_FINAL_PR}" ] \
        || [ "${EAGER_FINAL_MERGE_STATUS}" != "pending" ] \
        || [ -n "${EAGER_FINAL_ERROR_BEFORE}" ]; then
        jq --argjson final_pr "${EAGER_FINAL_PR}" '.final_merge_pr = $final_pr | .final_merge_status = "pending" | .final_merge_error = ""' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment || true
      fi
      update_eager_pr_validation_status_section "${EAGER_FINAL_PR}" || true
    fi
  fi
  EAGER_FINAL_PR_EFFECTIVE="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [[ "${EAGER_FINAL_PR_EFFECTIVE}" =~ ^[0-9]+$ ]]; then
    maybe_apply_force_merge_bypass "${EAGER_FINAL_PR_EFFECTIVE}" "${CWS_INTEGRATION_BRANCH}" "${CWS_AHEAD_BY}" || true
  fi
  reconcile_integration_backpressure_label "${CWS_INTEGRATION_BRANCH}" "${CWS_DEFAULT_BRANCH:-}" "${CWS_AHEAD_BY}" "${EAGER_FINAL_PR_EFFECTIVE}" || true

  # ---------------------------------------------------------------
  # Maintain the pinned "completion status" comment on the tracking
  # issue every cycle. The V2 state comment chain (post_state_comment)
  # remains the canonical state record; this comment is a separate,
  # human-readable summary the operator can read at a glance to see
  # which wave PRs and integration→default merge are still blocking
  # completion. Idempotent via body-hash cache so this is cheap to
  # call every tick. See update_completion_status_comment for the
  # marker contract and API hygiene notes.
  # ---------------------------------------------------------------
  COMPLETION_STATUS_STATE_CHANGED="false"
  _csc_validation_recovery_count="$(jq -r '.validation_recovery_count // 0' "${STATE_FILE}" 2>/dev/null || echo 0)"
  if ! [[ "${_csc_validation_recovery_count}" =~ ^[0-9]+$ ]]; then
    _csc_validation_recovery_count=0
  fi
  _completion_status_text="in-progress"
  if [ "${ANY_FAILED}" = "true" ]; then
    _completion_status_text="failed"
  elif [ "${PROJECT_STATUS:-}" = "in_progress" ] && [ "${_csc_validation_recovery_count}" -gt 0 ]; then
    _completion_status_text="in-progress"
  elif [ "${PROJECT_COMPLETE}" = "true" ]; then
    _completion_status_text="ready"
  elif [ "${WAVE_COMPLETE}" = "true" ]; then
    # Final-wave PRs merged into the integration branch but the
    # integration→default squash merge has not landed yet (autofix may
    # still be running on the integration PR).
    _completion_status_text="waiting"
  fi

  _csc_integration_ahead_by="$(echo "${WAVE_STATUS}" | jq -r '.integration_ahead_by // ""')"
  _csc_integration_contained="$(echo "${WAVE_STATUS}" | jq -r '.integration_contained_in_default // false')"
  _csc_final_pr="${EAGER_FINAL_PR_EFFECTIVE:-}"
  if ! [[ "${_csc_final_pr}" =~ ^[0-9]+$ ]]; then
    _csc_final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
  fi
  _csc_total_waves="$(jq -r '.total_waves // 0' "${STATE_FILE}" 2>/dev/null || echo 0)"
  _csc_current_wave="$(echo "${WAVE_STATUS}" | jq -r '.wave // 0')"
  _csc_skipped_lines="$(echo "${WAVE_STATUS}" | jq -r '
    [.issues[]
      | select(.status == "skipped")
      | "- #\(.github_issue // "?"): \(.status) (\(.decision_source // "unknown"))"]
    | join("\n")')"
  _csc_pending_lines="$(echo "${WAVE_STATUS}" | jq -r '
    [.issues[]
      | select(.status != "merged" and .status != "closed" and .status != "skipped" and .status != "implementation-failed")
      | "- #\(.github_issue // "?"): \(.status)"]
    | join("\n")')"
  _csc_failed_lines="$(echo "${WAVE_STATUS}" | jq -r '
    [.issues[]
      | select(.status == "closed" or .status == "implementation-failed")
      | "- #\(.github_issue // "?"): \(.status) (\(.decision_source // "unknown"))"]
    | join("\n")')"

  _csc_body="## Completion status"$'\n\n'
  _csc_body+="**State:** \`${_completion_status_text}\`"$'\n'
  _csc_body+="**Wave:** ${_csc_current_wave}/${_csc_total_waves}"$'\n'
  if [ -n "${_csc_pending_lines}" ]; then
    _csc_body+=$'\n'"**Wave issues still pending merge:**"$'\n'"${_csc_pending_lines}"$'\n'
  else
    _csc_body+=$'\n'"All wave issues in this wave have merged or are accounted for."$'\n'
  fi
  if [ "${PROJECT_STATUS:-}" = "in_progress" ] && [ "${_csc_validation_recovery_count}" -gt 0 ]; then
    _csc_body+=$'\n'"Runtime validation is in recovery attempt ${_csc_validation_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS}; waiting for judge re-evaluation before validation can resume."$'\n'
  fi
  if [ "${_csc_integration_contained}" = "true" ]; then
    _csc_body+=$'\n'"Integration branch is contained in default — integration→default merge has landed."$'\n'
  elif [ -n "${_csc_integration_ahead_by}" ] && [ "${_csc_integration_ahead_by}" != "0" ]; then
    _csc_body+=$'\n'"Integration branch is ahead of default by **${_csc_integration_ahead_by}** commit(s). The integration→default merge has not landed yet (autofix may still be running on the integration PR)."$'\n'
  else
    _csc_body+=$'\n'"Integration status is unknown this cycle (compare API unavailable). Project completion remains gated until the next successful poll re-check."$'\n'
  fi
  if [ "${INTEGRATION_BACKPRESSURE_BLOCK_MERGES:-false}" = "true" ]; then
    _integration_backpressure_effective_threshold _csc_effective_threshold
    if [[ "${_csc_final_pr}" =~ ^[0-9]+$ ]]; then
      _csc_body+=$'\n'"Integration backpressure is active (\`ai:integration-backpressure\`): the poller is pausing additional sub-issue merges while the integration branch backlog stays at or above \`${_csc_effective_threshold}\` commit(s). Review the open integration PR #${_csc_final_pr} ($(_gh_url "pull/${_csc_final_pr}")); this clears automatically once the backlog shrinks below the threshold."$'\n'
    else
      _csc_body+=$'\n'"Integration backpressure is active (\`ai:integration-backpressure\`): the poller is pausing additional sub-issue merges while the integration branch backlog stays at or above \`${_csc_effective_threshold}\` commit(s). This clears automatically once the backlog shrinks below the threshold."$'\n'
    fi
  fi
  if [ -n "${_csc_failed_lines}" ]; then
    _csc_body+=$'\n'"**Wave issues closed without merge / implementation-failed:**"$'\n'"${_csc_failed_lines}"$'\n'
    _csc_body+=$'\n'"Project cannot complete until these are resolved (re-open, re-merge, or skip)."$'\n'
  fi
  if [ -n "${_csc_skipped_lines}" ]; then
    _csc_body+=$'\n'"**Wave issues skipped:**"$'\n'"${_csc_skipped_lines}"$'\n'
    _csc_body+=$'\n'"Skipped issues are already accounted for and do not block completion."$'\n'
  fi
  _csc_body+=$'\n'"_The orchestrator poller updates this comment every cycle (~5 min) until the project completes. Marker: \`<!-- orchestrator:completion-status -->\`._"

  update_completion_status_comment "${_completion_status_text}" "${_csc_body}" || true

  # Once-per-project Telegram escalation when a wave PR has been closed
  # without merging (or implementation-failed). The stall/review-blocked
  # paths further down also surface their own alerts on specific
  # failures, but this is the earliest deterministic signal — fire here
  # so the operator sees the alert as soon as ANY_FAILED first goes
  # true. Guarded by a state-file flag so we never alert more than once
  # per project for the same condition.
  if [ "${ANY_FAILED}" = "true" ]; then
    _csc_alert_sent="$(jq -r '.completion_status_failure_alert_sent // false' "${STATE_FILE}" 2>/dev/null || echo false)"
    if [ "${_csc_alert_sent}" != "true" ]; then
      tg_notify "Project #${TRACKING_NUM}: one or more wave PR(s) closed without merge — see the pinned 'Completion status' comment on the tracking issue for the full list. The project cannot complete until these are resolved." "CRITICAL"
      if jq '.completion_status_failure_alert_sent = true' "${STATE_FILE}" > "${STATE_FILE}.tmp" \
        && mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
        COMPLETION_STATUS_STATE_CHANGED="true"
      else
        rm -f "${STATE_FILE}.tmp" || true
        echo "::warning::[completion-status] failed to persist completion_status_failure_alert_sent for issue #${TRACKING_NUM:-?}; the once-per-project alert may repeat on a later tick."
      fi
    fi
    unset _csc_alert_sent
  fi

  unset _completion_status_text _csc_integration_ahead_by _csc_integration_contained
  unset _csc_total_waves _csc_current_wave _csc_pending_lines _csc_failed_lines _csc_skipped_lines _csc_body _csc_validation_recovery_count _csc_effective_threshold

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
      # Capture intent fingerprints for sub-issues that just transitioned
      # to merged on a project with an integration branch, so the
      # integration-sync conflict resolver can verify it preserved the
      # sub-issue's intent. Going-forward only (Q4:A): no backfill of
      # already-merged sub-issues from prior poll cycles.
      if [ "${_ws_status}" = "merged" ] && [[ "${_ws_gh}" =~ ^[0-9]+$ ]]; then
        _intent_integ="$(jq -r '.integration_branch // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
        if [ -n "${_intent_integ}" ]; then
          _intent_pr="$(_subissue_closing_pr_number "${_ws_gh}" || echo "")"
          if [[ "${_intent_pr}" =~ ^[0-9]+$ ]]; then
            capture_intent_fingerprints_for_merged_subissue "${_ws_gh}" "${_intent_pr}" || true
          fi
          unset _intent_pr
        fi
        unset _intent_integ
      fi
    fi
  done < <(echo "${WAVE_STATUS}" | jq -c '.issues[]')

  TRACKING_BODY_SYNC_STATE_CHANGED="false"
  if [ -n "${CWS_INTEGRATION_BRANCH}" ] || [[ "${EAGER_FINAL_PR_EFFECTIVE}" =~ ^[0-9]+$ ]]; then
    reconcile_tracking_issue_body_from_state "${EAGER_FINAL_PR_EFFECTIVE}" "${CWS_INTEGRATION_BRANCH}" || true
  fi

  # ---------------------------------------------------------------
  # Auto-merge: merge PRs that are ready-to-merge
  # ---------------------------------------------------------------
  # Resolve the active integration branch once per poll cycle. Used by
  # probe_sibling_merge_conflicts() to enumerate sibling PRs targeting
  # the same integration branch with a single cached gh pr list call.
  RTM_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  [ "${RTM_INTEGRATION_BRANCH}" = "null" ] && RTM_INTEGRATION_BRANCH=""
  echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "ready-to-merge") | .github_issue' | while read -r rtm_issue; do
    [[ "${rtm_issue}" =~ ^[0-9]+$ ]] || continue
    echo "  Issue #${rtm_issue} is ready-to-merge, finding linked PR..."
    RTM_PR="$(_issue_cross_ref_pr_number_last "${rtm_issue}" 2>/dev/null || echo "")"
    if [[ "${RTM_PR}" =~ ^[0-9]+$ ]]; then
      _rtm_pr_json="$(_fetch_pr_json "${RTM_PR}")"
      PR_STATE="$(_jq_field "${_rtm_pr_json}" '.state' 'open|closed|merged')"
      PR_MERGEABLE="$(_jq_field "${_rtm_pr_json}" '.mergeable' 'true|false')"
      _rtm_head_sha="$(_jq_field "${_rtm_pr_json}" '.head.sha')"
      _rtm_head_ref="$(_jq_field "${_rtm_pr_json}" '.head.ref')"
      if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
        if [ "${INTEGRATION_BACKPRESSURE_BLOCK_MERGES:-false}" = "true" ]; then
          _integration_backpressure_effective_threshold _rtm_effective_threshold
          echo "  [backpressure] Deferring merge of PR #${RTM_PR} for issue #${rtm_issue}: integration branch ahead_by=${CWS_AHEAD_BY} meets effective threshold ${_rtm_effective_threshold} (configured floor ORCH_INTEGRATION_MAX_AHEAD_COMMITS=${ORCH_INTEGRATION_MAX_AHEAD_COMMITS})."
          continue
        fi
        if _pr_checks_completed "${RTM_PR}" "${_rtm_head_sha}"; then
          # Pre-merge sibling-conflict probe: refuse to squash into the
          # integration branch when another open sibling PR would
          # textually conflict. This short-circuits the wave-internal
          # collision pattern (two siblings editing README.md /
          # orchestrate_poll_process.sh / agents.md in parallel) BEFORE
          # the merge lands, so the loser never enters an autofix
          # rebase loop.
          if [ -n "${RTM_INTEGRATION_BRANCH}" ] && [ -n "${_rtm_head_ref}" ] && [ "${_rtm_head_ref}" != "null" ]; then
            if ! probe_sibling_merge_conflicts "${RTM_PR}" "${_rtm_head_ref}" "${RTM_INTEGRATION_BRANCH}"; then
              _rtm_defer_count="$(_bump_merge_deferral_count "${WAVE_IDX}" "${rtm_issue}")"
              [[ "${_rtm_defer_count}" =~ ^[0-9]+$ ]] || _rtm_defer_count=0
              echo "  [merge-probe] Deferring merge of PR #${RTM_PR} for issue #${rtm_issue} (defer ${_rtm_defer_count}/${MAX_MERGE_DEFERRALS}) — sibling conflict detected."
              if [ "${_rtm_defer_count}" -ge "${MAX_MERGE_DEFERRALS}" ]; then
                tg_notify "PR #${RTM_PR} (issue #${rtm_issue}) has exceeded MAX_MERGE_DEFERRALS=${MAX_MERGE_DEFERRALS} with persistent sibling merge-tree conflicts. Human review required."$'\n'"PR: $(_gh_url "pull/${RTM_PR}")"$'\n'"Issue: $(_gh_url "issues/${rtm_issue}")" "WARNING"
		      fi
		      continue
		    fi
		  fi
		  # Pre-merge alignment: opportunistically sync integration with
		  # main and rebase the sub-issue head onto the integration tip
		  # so the squash-merge captures only the sub-issue's intent and
		  # integration->main drift is bounded.  Three return codes:
		  #   0 — already aligned, proceed with merge this tick
		  #   1 — rebase conflict; defer + bump _bump_merge_deferral_count
		  #   2 — alignment force-pushed a new SHA; defer one tick (no
		  #       bump) so the new SHA's CI re-runs before squash-merge.
		  #       _pr_checks_completed above evaluated against the
		  #       pre-rebase head SHA, and the non-`--auto` fallback
		  #       merge path (`elif gh pr merge --squash`) does NOT
		  #       wait for required checks server-side, so a same-tick
		  #       merge could otherwise race ahead of the new head's
		  #       checks rerun.
		  if [ -n "${RTM_INTEGRATION_BRANCH}" ] && [ -n "${_rtm_head_ref}" ] && [ "${_rtm_head_ref}" != "null" ]; then
		    _premerge_rc=0
		    _sync_integration_and_rebase_subissue "${RTM_PR}" "${_rtm_head_ref}" "${RTM_INTEGRATION_BRANCH}" || _premerge_rc=$?
		    case "${_premerge_rc}" in
		      0)
		        : # already aligned; proceed with merge below
		        ;;
		      2)
		        echo "  [premerge-rebase] Deferring merge of PR #${RTM_PR} for issue #${rtm_issue} one tick — alignment pushed a new SHA, letting CI re-run before squash-merge."
		        continue
		        ;;
		      *)
		        _rtm_defer_count="$(_bump_merge_deferral_count "${WAVE_IDX}" "${rtm_issue}")"
		        [[ "${_rtm_defer_count}" =~ ^[0-9]+$ ]] || _rtm_defer_count=0
		        echo "  [premerge-rebase] Deferring merge of PR #${RTM_PR} for issue #${rtm_issue} (defer ${_rtm_defer_count}/${MAX_MERGE_DEFERRALS}) — pre-merge rebase conflicts."
		        if [ "${_rtm_defer_count}" -ge "${MAX_MERGE_DEFERRALS}" ]; then
		          tg_notify "PR #${RTM_PR} (issue #${rtm_issue}) has exceeded MAX_MERGE_DEFERRALS=${MAX_MERGE_DEFERRALS} with persistent pre-merge rebase conflicts. Human review required."$'\n'"PR: $(_gh_url "pull/${RTM_PR}")"$'\n'"Issue: $(_gh_url "issues/${rtm_issue}")" "WARNING"
		        fi
		        continue
		        ;;
		    esac
		  fi
		  echo "  Merging PR #${RTM_PR} (squash)..."
		  if gh_retry gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
		    echo "  PR #${RTM_PR} merge initiated."
		    refresh_integration_backpressure_gate_after_merge || true
		  elif gh_retry gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
		    echo "  PR #${RTM_PR} merged directly."
		    refresh_integration_backpressure_gate_after_merge || true
		  else
		    echo "::warning::Could not merge PR #${RTM_PR} for issue #${rtm_issue}. May need manual merge or branch protection prevents it."
		  fi
		fi
      elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
        echo "  PR #${RTM_PR} is not mergeable (mergeable=${PR_MERGEABLE}). Attempting branch update..."
        if gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}/update-branch" \
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
    IP_PR="$(_issue_cross_ref_pr_number_last "${ip_issue}" 2>/dev/null || echo "")"
    if ! [[ "${IP_PR}" =~ ^[0-9]+$ ]]; then
      echo "  Issue #${ip_issue}: no linked PR found in timeline."
      continue
    fi
    _ip_pr_json="$(_fetch_pr_json "${IP_PR}")"
    IP_PR_STATE="$(_jq_field "${_ip_pr_json}" '.state' 'open|closed|merged')"
    IP_MERGEABLE="$(_jq_field "${_ip_pr_json}" '.mergeable' 'true|false')"
    if [ "${IP_PR_STATE}" != "open" ] || [ "${IP_MERGEABLE}" != "false" ]; then
      echo "  Issue #${ip_issue}: PR #${IP_PR} state=${IP_PR_STATE} mergeable=${IP_MERGEABLE}, skipping."
      continue
    fi
    echo "  Issue #${ip_issue} has PR #${IP_PR} with merge conflicts. Running Codex conflict resolution..."

    _ip_head_sha="$(_jq_field "${_ip_pr_json}" '.head.sha')"

    # Try the GitHub API update-branch first (creates a merge commit
    # if the merge is clean; fails when there are real conflicts).
    if gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}/update-branch" \
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
  if [ "${FEATURE_SWEEP_DONE}" != "true" ]; then
    echo "  [feature-sweep] Scanning open ai/issue-* PRs for base-branch drift..."
    _FEATURE_SWEEP_JSON="$(gh_retry gh pr list \
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
        if [ "${_fs_state}" = "dirty" ] || [ "${_fs_mergeable}" = "conflicting" ] || [ "${_fs_mergeable}" = "false" ]; then
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
    FEATURE_SWEEP_DONE="true"
  fi

  # ---------------------------------------------------------------
  # Handle review-blocked issues: invoke judge to decide
  # ---------------------------------------------------------------
  ANY_REVIEW_BLOCKED="$(echo "${WAVE_STATUS}" | jq -r '.any_review_blocked')"
  if [ "${ANY_REVIEW_BLOCKED}" = "true" ]; then
    echo "Detected review-blocked issues in wave ${CURRENT_WAVE}. Invoking judge to unblock..."

    # Ensure codex config exists for the judge.
    # Centralised in scripts/write_codex_config.sh.
    bash scripts/write_codex_config.sh \
      --model "${MODEL_EDITOR}" \
      --reasoning "${MODEL_REASONING_EFFORT_JUDGE:-xhigh}"

    MAX_REVIEW_BLOCKED_RETRIES="${MAX_REVIEW_BLOCKED_RETRIES:-2}"
    REVIEW_BLOCKED_STATE_CHANGED=false

    while read -r rb_issue; do
      [[ "${rb_issue}" =~ ^[0-9]+$ ]] || continue
      echo "  Processing review-blocked issue #${rb_issue}..."

      # Track retries per issue
      RETRY_COUNT="$(jq -r ".review_blocked_retries[\"${rb_issue}\"] // 0" "${STATE_FILE}")"
      echo "  Retry count for #${rb_issue}: ${RETRY_COUNT}/${MAX_REVIEW_BLOCKED_RETRIES}"

      # Find linked PR
      RB_PR="$(_issue_cross_ref_pr_number_last "${rb_issue}" 2>/dev/null || echo "")"
      if ! [[ "${RB_PR}" =~ ^[0-9]+$ ]]; then
        echo "  No linked PR found for issue #${rb_issue}, skipping."
        continue
      fi
      echo "  Linked PR: #${RB_PR}"

      RB_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
      [ "${RB_INTEGRATION_BRANCH}" = "null" ] && RB_INTEGRATION_BRANCH=""
      RB_INTEGRATION_BRANCH_VALID="false"
      if [ -n "${RB_INTEGRATION_BRANCH}" ] && integration_branch_exists "${RB_INTEGRATION_BRANCH}"; then
        RB_INTEGRATION_BRANCH_VALID="true"
      fi

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
        if [ "${RB_INTEGRATION_BRANCH_VALID}" = "true" ]; then
          echo "  PR #${RB_PR} is already merged. Judge fixes will target a follow-up PR against ${RB_INTEGRATION_BRANCH}."
        elif [ -n "${RB_INTEGRATION_BRANCH}" ]; then
          echo "::warning::PR #${RB_PR} is already merged and integration branch context '${RB_INTEGRATION_BRANCH}' is invalid. Follow-up PR creation will be refused unless safely retargeted."
        else
          echo "  PR #${RB_PR} is already merged. Judge fixes will target a follow-up PR against ${DEFAULT_BRANCH:-main}."
        fi
      fi

      # ------------------------------------------------------------------
      # Pre-judge auto-unstick / dirty-first dispatch
      # ------------------------------------------------------------------
      # The review-blocked judge decides merge/fix/close_and_reissue — it
      # is NOT a merge-conflict resolver, and its "fix" path does not
      # touch the merge state.  Two situations must be handled BEFORE the
      # judge is invoked, because otherwise the PR sits stuck for one or
      # more poll cycles while retries are consumed unproductively:
      #
      #   1. PR is dirty (mergeable=false): the only way to unstick it is
      #      to dispatch review_autofix.yml, which runs the in-workflow
      #      Codex conflict resolver on a clean runner.  Do that here and
      #      skip the judge for this tick — the next tick will re-enter
      #      this loop against (hopefully) a resolved PR.
      #
      #   2. The PR head commit is external (not codex-bot / not a
      #      GitHub Actions bot): an external actor has pushed a fix
      #      since the ai:review-blocked label was applied.  This is
      #      exactly the "push a new commit to re-trigger" contract the
      #      review_autofix.yml failure comment promises — but when the
      #      pushing identity uses the GITHUB_TOKEN (Claude Code on the
      #      web, a custom wrapper action) GitHub deliberately suppresses
      #      the pull_request.synchronize event, so internal-review.yml
      #      never wakes up and the PR stays stuck.  Bridge that gap by
      #      dispatching review_autofix.yml explicitly and clearing the
      #      ai:review-blocked label so the PR re-enters the normal
      #      phase loop.
      #
      # Both paths funnel through _dispatch_review_for_conflicts, which
      # already has cycle-local dedup (guard 1) and active-run detection
      # (guard 2) so duplicate dispatches are cheap no-ops.  Gated by
      # REVIEW_BLOCKED_AUTO_UNSTICK (default "true") for emergency
      # kill-switch use.
      REVIEW_BLOCKED_AUTO_UNSTICK="${REVIEW_BLOCKED_AUTO_UNSTICK:-true}"
      if is_truthy "${REVIEW_BLOCKED_AUTO_UNSTICK}" && [ "${RB_PR_STATE}" = "open" ] && [ "${RB_PR_MERGED}" != "true" ]; then
        RB_PR_MERGEABLE="$(_jq_field "${_rb_pr_json}" '.mergeable' 'true|false')"
        RB_PR_MERGEABLE_STATE="$(echo "${_rb_pr_json}" | jq -r '.mergeable_state // ""')"
        RB_HEAD_REF_PRECHECK="$(echo "${_rb_pr_json}" | jq -r '.head.ref // ""')"
        RB_HEAD_SHA_PRECHECK="$(echo "${_rb_pr_json}" | jq -r '.head.sha // ""')"

        # Classify the head commit's author.  Orchestrator-produced
        # commits use the codex-bot identity; anything else is
        # "external" and a signal that someone (human or external
        # automation) has intervened and wants review retriggered.
        RB_HEAD_IS_EXTERNAL="false"
        _rb_head_author_login=""
        _rb_head_author_name=""
        _rb_head_author_email=""
        if [ -n "${RB_HEAD_SHA_PRECHECK}" ] && [ "${RB_HEAD_SHA_PRECHECK}" != "null" ]; then
          _rb_head_commit_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/commits/${RB_HEAD_SHA_PRECHECK}" || echo "{}")"
          _rb_head_commit_lookup_ok="$(echo "${_rb_head_commit_json}" | jq -r 'if (.sha != null and .commit != null and .commit.author != null) then "true" else "false" end')"
          if [ "${_rb_head_commit_lookup_ok}" = "true" ]; then
            _rb_head_author_login="$(echo "${_rb_head_commit_json}" | jq -r '.author.login // ""')"
            _rb_head_author_name="$(echo "${_rb_head_commit_json}" | jq -r '.commit.author.name // ""')"
            _rb_head_author_email="$(echo "${_rb_head_commit_json}" | jq -r '.commit.author.email // ""')"
            # Default to external unless the head commit can be
            # confidently classified as internal automation.
            RB_HEAD_IS_EXTERNAL="true"
            case "${_rb_head_author_login}" in
              codex|codex-bot|github-actions|github-actions\[bot\]) RB_HEAD_IS_EXTERNAL="false" ;;
              "")
                # Keep authenticated GitHub login authoritative. Only
                # when login is missing do we use name/email bot hints.
                case "${_rb_head_author_name}" in
                  codex|codex-bot|"GitHub Actions") RB_HEAD_IS_EXTERNAL="false" ;;
                esac
                case "${_rb_head_author_email}" in
                  codex@users.noreply.github.com|github-actions\[bot\]@users.noreply.github.com|*+github-actions\[bot\]@users.noreply.github.com) RB_HEAD_IS_EXTERNAL="false" ;;
                esac
                ;;
            esac
          else
            echo "::warning::[review-blocked] Unable to fetch head-commit author metadata for PR #${RB_PR}; skipping external-commit pre-dispatch this tick."
          fi
        fi

        RB_SHOULD_PREDISPATCH="false"
        RB_PREDISPATCH_REASON=""
        if [ "${RB_PR_MERGEABLE}" = "false" ] || [ "${RB_PR_MERGEABLE_STATE}" = "dirty" ]; then
          RB_SHOULD_PREDISPATCH="true"
          RB_PREDISPATCH_REASON="merge conflicts (mergeable=${RB_PR_MERGEABLE:-unknown}, mergeable_state=${RB_PR_MERGEABLE_STATE:-unknown})"
        elif [ "${RB_HEAD_IS_EXTERNAL}" = "true" ]; then
          RB_SHOULD_PREDISPATCH="true"
          RB_PREDISPATCH_REASON="external head commit ${RB_HEAD_SHA_PRECHECK:0:7} by ${_rb_head_author_login:-${_rb_head_author_name:-unknown}}"
        fi

        if [ "${RB_SHOULD_PREDISPATCH}" = "true" ] && [ -n "${RB_HEAD_REF_PRECHECK}" ] && [ "${RB_HEAD_REF_PRECHECK}" != "null" ]; then
          echo "  [review-blocked] Pre-judge dispatch for PR #${RB_PR}: ${RB_PREDISPATCH_REASON}"
          _predispatch_rc=0
          _dispatch_review_for_conflicts "${RB_PR}" "${RB_HEAD_REF_PRECHECK}" || _predispatch_rc=$?
          if [ "${_predispatch_rc}" -eq 0 ]; then
            # Dispatched successfully.  When the trigger was an external
            # head commit, also clear the ai:review-blocked label so the
            # PR re-enters the normal phase loop on subsequent ticks —
            # the dispatched review_autofix run will re-apply the label
            # itself if it hits trouble again, so this is a safe reset.
            if [ "${RB_HEAD_IS_EXTERNAL}" = "true" ]; then
              gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                --remove-label 'ai:review-blocked' 2>/dev/null || true
              echo "  [review-blocked] Cleared ai:review-blocked on issue #${rb_issue} (external commit detected)."
              tg_notify "Review-blocked PR #${RB_PR} (issue #${rb_issue}) auto-unstuck: external head commit detected, review workflow dispatched."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
            else
              tg_notify "Review-blocked PR #${RB_PR} (issue #${rb_issue}) has merge conflicts; pre-judge dispatched conflict resolver."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
            fi
            REVIEW_BLOCKED_STATE_CHANGED=true
            continue
          elif [ "${_predispatch_rc}" -eq 2 ]; then
            echo "  [review-blocked] Pre-judge dispatch skipped (active run or cycle-local dupe); skipping judge for this tick."
            continue
          else
            echo "::warning::[review-blocked] Pre-judge dispatch failed for PR #${RB_PR}; falling through to judge."
          fi
        fi
      fi

      # Collect full PR context for the judge
      PR_DIFF="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" \
        -H 'Accept: application/vnd.github.diff' 2>/dev/null || echo "(diff unavailable)")"
      RB_PRELOADED_META="$(echo "${_rb_pr_json}" | jq -c '{title: .title, body: .body, head_ref: .head.ref, base_ref: .base.ref, head_sha: .head.sha}' 2>/dev/null || echo '{}')"
      if type gh_pr_with_all_comments >/dev/null 2>&1; then
        RB_PR_CONTEXT_JSON="$(gh_pr_with_all_comments "${GITHUB_REPOSITORY%%/*}" "${GITHUB_REPOSITORY##*/}" "${RB_PR}" "${RB_PRELOADED_META}" || echo '{}')"
      elif type _gh_pr_with_all_comments_rest >/dev/null 2>&1; then
        RB_PR_CONTEXT_JSON="$(_gh_pr_with_all_comments_rest "${GITHUB_REPOSITORY%%/*}" "${GITHUB_REPOSITORY##*/}" "${RB_PR}" "${RB_PRELOADED_META}" || echo '{}')"
      else
        printf '%s\n' "::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments mode=legacy_rest_hydration reason=helper_unavailable owner=${GITHUB_REPOSITORY%%/*} repo=${GITHUB_REPOSITORY##*/} pr=${RB_PR}" >&2
        RB_PR_ISSUE_COMMENTS="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${RB_PR}/comments" 2>/dev/null | jq -cs 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}] | sort_by((.created_at // ""), (.author // ""), (.body // ""))' 2>/dev/null || echo '[]')"
        RB_PR_REVIEW_COMMENTS="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/comments" 2>/dev/null | jq -cs 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}] | sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))' 2>/dev/null || echo '[]')"
        RB_PR_CONTEXT_JSON="$(jq -cn --argjson meta "${RB_PRELOADED_META}" --argjson comments "${RB_PR_ISSUE_COMMENTS}" --argjson review_comments "${RB_PR_REVIEW_COMMENTS}" '{meta: $meta, comments: $comments, review_comments: $review_comments}' 2>/dev/null || echo '{}')"
      fi
      PR_COMMENTS="$(printf '%s' "${RB_PR_CONTEXT_JSON}" | jq -c '.comments // []' 2>/dev/null || echo "[]")"
      PR_REVIEW_COMMENTS="$(printf '%s' "${RB_PR_CONTEXT_JSON}" | jq -c '.review_comments // []' 2>/dev/null || echo "[]")"
      PR_META="$(printf '%s' "${RB_PR_CONTEXT_JSON}" | jq -c '.meta // {}' 2>/dev/null || echo "{}")"
      if [ "${PR_META}" = "{}" ]; then
        PR_META="$(echo "${_rb_pr_json}" | jq '{title: .title, body: .body, head_ref: .head.ref, base_ref: .base.ref, head_sha: .head.sha}' 2>/dev/null || echo "{}")"
      fi
      ISSUE_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rb_issue}" --jq '.body' || echo "")"
      RB_JUDGE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/rb_judge_semble_query_${rb_issue}.txt"
      RB_JUDGE_SEMBLE_PREFETCH=""
      {
        printf '%s\n' 'Review-blocked judge context.'
        append_judge_semble_query_text "Issue body:" "${ISSUE_BODY}" 2500
        append_judge_semble_query_text "PR metadata JSON:" "${PR_META}" 2000
        append_judge_semble_query_text "PR diff excerpt:" "${PR_DIFF}" 5000
        append_judge_semble_query_text "PR issue comments JSON:" "${PR_COMMENTS}" 2500
        append_judge_semble_query_text "PR review comments JSON:" "${PR_REVIEW_COMMENTS}" 2500
      } > "${RB_JUDGE_SEMBLE_QUERY_FILE}"
      RB_JUDGE_SEMBLE_PREFETCH="$(render_judge_semble_prefetch_from_query_file "${RB_JUDGE_SEMBLE_QUERY_FILE}" "Review-Blocked Judge Context")"

      # Determine if this is a final decision (retries exhausted) or a fix attempt
      IS_FINAL="false"
      if [ "${RETRY_COUNT}" -ge "${MAX_REVIEW_BLOCKED_RETRIES}" ]; then
        IS_FINAL="true"
        echo "  Retries exhausted — judge will make final decision (merge or close+reissue)."
      fi

      # ------------------------------------------------------------------
      # Pre-judge branch preparation for combined decide + apply path
      # ------------------------------------------------------------------
      # The review-blocked judge originally ran in two sequential codex
      # calls: the first decided merge|fix|close_and_reissue (read-only),
      # then — for the "fix" branch — a second call re-ingested the
      # identical ~30 KB of PR diff / comments / review findings plus an
      # "APPLY FIXES NOW" suffix and applied fixes on the checked-out
      # branch. Both calls consumed the same large context.
      #
      # Combine them: check out the target branch BEFORE the judge call
      # and instruct the judge to apply fixes directly if it chooses
      # action="fix". When action is merge/close_and_reissue, any
      # accidental file modifications are discarded by
      # rb_cleanup_combined_workspace below.
      #
      #   RB_COMBINED_MODE="true"  → branch checked out, judge may apply fixes
      #   RB_COMBINED_MODE="false" → no branch prep (IS_FINAL, or prep failed);
      #                              fix application is skipped the same way
      #                              the old two-call flow skipped when it
      #                              could not determine a target branch.
      RB_COMBINED_MODE="false"
      RB_COMBINED_BRANCH_INFO=""
      RB_TARGET_MERGED="false"
      HEAD_REF=""
      BASE_REF=""
      FOLLOWUP_BRANCH=""
      ORCH_FOLLOWUP_OWNED="false"
      ORCH_FOLLOWUP_TRACKING_NUM=""
      ORCH_FOLLOWUP_INTEGRATION_BRANCH=""
      ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS="false"
      FOLLOWUP_PR_BLOCKED="false"

      if [ "${IS_FINAL}" != "true" ]; then
        HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
        BASE_REF="$(echo "${PR_META}" | jq -r '.base_ref')"
        : "${BASE_REF:=${DEFAULT_BRANCH:-main}}"

        # Re-fetch PR state just before branch prep: the initial check at
        # line 5054 and the auto-unstick block above can together span
        # several seconds worth of gh API calls, during which an external
        # actor (or a human merging via the GitHub UI) may have merged the
        # PR. Without this re-check, a merge that lands between the initial
        # fetch and this branch-prep decision sends us down the open-PR
        # HEAD_REF checkout path instead of the merged-PR follow-up branch
        # path, so the judge's fixes never produce a follow-up PR. The
        # race-check at line 5614 catches the same race later but is too
        # late to correct the branch-prep decision. Keep _rb_pr_json in
        # sync so downstream consumers (e.g. RB_PR_MERGEABLE_STATE) see
        # the same snapshot.
        # Snapshot the pre-refetch derived flags so we can roll back if the
        # refreshed payload is invalid. _rb_pr_json itself is only replaced
        # on success, so downstream consumers of _rb_pr_json stay in sync
        # with RB_PR_STATE/RB_PR_MERGED regardless of which path we take.
        _rb_prev_pr_state="${RB_PR_STATE:-}"
        _rb_prev_pr_merged="${RB_PR_MERGED:-false}"
        _rb_pr_json_refetched="$(_fetch_pr_json "${RB_PR}")"
        if printf '%s' "${_rb_pr_json_refetched}" | jq -e 'type == "object" and ((.state // "") | IN("open","closed","merged"))' >/dev/null 2>&1; then
          _rb_pr_json="${_rb_pr_json_refetched}"
          RB_PR_STATE="$(_jq_field "${_rb_pr_json}" '.state' 'open|closed|merged')"
          RB_PR_MERGED="$(_jq_field "${_rb_pr_json}" '.merged_at != null' 'true|false')"
          : "${RB_PR_MERGED:=false}"
        else
          # Transient API failure or malformed response: keep the earlier
          # snapshot entirely (both the JSON blob and the derived flags) so
          # downstream consumers of _rb_pr_json — mergeable_state, head.sha,
          # etc. — stay in sync with RB_PR_STATE/RB_PR_MERGED instead of
          # getting a `{}` payload while the flags still say "merged".
          echo "::warning::[review-blocked] Failed to refresh PR #${RB_PR} state with a valid payload during branch prep; keeping earlier snapshot."
          RB_PR_STATE="${_rb_prev_pr_state}"
          RB_PR_MERGED="${_rb_prev_pr_merged}"
        fi
        unset _rb_prev_pr_state _rb_prev_pr_merged _rb_pr_json_refetched

        if [ "${RB_PR_MERGED}" = "true" ]; then
          RB_TARGET_MERGED="true"
          resolve_active_orchestrator_context_for_issue "${rb_issue}" "${TRACKING_NUM:-}"
          ORCH_FOLLOWUP_OWNED="${RESOLVED_ORCHESTRATOR_OWNED}"
          ORCH_FOLLOWUP_TRACKING_NUM="${RESOLVED_TRACKING_ISSUE}"
          ORCH_FOLLOWUP_INTEGRATION_BRANCH="${RESOLVED_INTEGRATION_BRANCH}"
          ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS="${RESOLVED_INTEGRATION_BRANCH_EXISTS}"

          if [ "${ORCH_FOLLOWUP_OWNED}" = "true" ]; then
            if [ "${ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS}" = "true" ] && [ -n "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
              BASE_REF="${ORCH_FOLLOWUP_INTEGRATION_BRANCH}"
              echo "  Follow-up PR for issue #${rb_issue} is orchestrator-managed (tracking #${ORCH_FOLLOWUP_TRACKING_NUM}). Retargeting base to ${BASE_REF}."
            elif [ -n "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
              # Integration branch name is present in orchestrator state but
              # the branch itself is not reachable (deleted/renamed externally
              # or transient API/mock lookup failure). Block the follow-up PR
              # to avoid accidentally targeting the default branch and bypassing
              # integration-branch safety checks.
              FOLLOWUP_PR_BLOCKED="true"
              FOLLOWUP_BLOCK_REASON="Issue #${rb_issue} is orchestrator-managed (tracking #${ORCH_FOLLOWUP_TRACKING_NUM}), but integration branch '${ORCH_FOLLOWUP_INTEGRATION_BRANCH:-<missing>}' is unavailable. Aborting follow-up PR creation to avoid targeting ${DEFAULT_BRANCH:-main}."
              echo "::warning::${FOLLOWUP_BLOCK_REASON}"
              ORIGINAL_TRACKING_NUM="${TRACKING_NUM:-}"
              if [ -n "${ORCH_FOLLOWUP_TRACKING_NUM:-}" ]; then
                TRACKING_NUM="${ORCH_FOLLOWUP_TRACKING_NUM}"
              fi
              post_tracking_comment "## ⚠️ Follow-up PR blocked

${FOLLOWUP_BLOCK_REASON}"
              tg_notify "${FOLLOWUP_BLOCK_REASON}" "WARNING"
              TRACKING_NUM="${ORIGINAL_TRACKING_NUM}"
            else
              # Tracking issue exists but no integration branch is configured.
              # Keep the default base inherited from PR metadata (falling back
              # to ${DEFAULT_BRANCH:-main}) so follow-up work still targets
              # the original PR base when no integration-branch context exists.
              echo "  Follow-up PR for issue #${rb_issue}: orchestrator-managed without integration branch context; keeping default base '${BASE_REF}'."
            fi
          fi

          if [ "${FOLLOWUP_PR_BLOCKED}" != "true" ]; then
            FOLLOWUP_BRANCH="fix/${rb_issue}-followup-$(date +%s)"
            echo "  PR already merged. Creating follow-up branch ${FOLLOWUP_BRANCH} from ${BASE_REF}."
            # Try three strategies in order to obtain a checkout source for
            # the follow-up branch:
            #   1. Fetch ${BASE_REF} from origin and use the remote-tracking ref.
            #      This is the production-preferred path — it guarantees the
            #      follow-up branch is based on the current upstream tip.
            #   2. If the fetch fails or the remote ref didn't land, fall back
            #      to a pre-existing local ref for ${BASE_REF}. This covers
            #      (a) test harnesses whose origin URL is not real and
            #      (b) production runs where the base branch was already
            #      fetched earlier in the same poll cycle.
            #   3. Last-resort fallback: current HEAD. In this mode the
            #      follow-up branch's content may not be based on ${BASE_REF},
            #      so the resulting PR diff may be larger than intended.
            #      A warning is emitted so operators can intervene.
            _rb_co_src=""
            _rb_co_src_desc=""
            if git fetch --no-tags origin "refs/heads/${BASE_REF}:refs/remotes/origin/${BASE_REF}" 2>/dev/null \
              && git rev-parse --verify "refs/remotes/origin/${BASE_REF}" >/dev/null 2>&1; then
              _rb_co_src="refs/remotes/origin/${BASE_REF}"
              _rb_co_src_desc="${BASE_REF} (fetched from origin)"
            elif git rev-parse --verify "refs/heads/${BASE_REF}" >/dev/null 2>&1; then
              _rb_co_src="refs/heads/${BASE_REF}"
              _rb_co_src_desc="${BASE_REF} (existing local ref)"
              echo "  [follow-up] Using existing local ref for ${BASE_REF} (fetch from origin did not produce a fresh remote-tracking ref)."
            elif git rev-parse --verify HEAD >/dev/null 2>&1; then
              _rb_co_src="HEAD"
              _rb_co_src_desc="current HEAD (NOT ${BASE_REF}; fetch and local lookup both failed)"
              echo "::warning::Could not fetch or locate base ref '${BASE_REF}' for issue #${rb_issue}; creating follow-up branch from current HEAD as a best-effort fallback. The resulting PR diff may be larger than intended."
            fi
            if [ -n "${_rb_co_src}" ] \
              && git checkout -B "${FOLLOWUP_BRANCH}" "${_rb_co_src}" 2>/dev/null; then
              RB_COMBINED_MODE="true"
              # Reflect the actual checkout source in the judge prompt
              # context. When the HEAD fallback fires, the follow-up branch
              # is not based on ${BASE_REF}; saying otherwise misleads the
              # judge into applying fixes on a wrong base.
              RB_COMBINED_BRANCH_INFO="You are on a follow-up branch (${FOLLOWUP_BRANCH}) based on ${_rb_co_src_desc}. The original PR #${RB_PR} was already merged. If you choose action=\"fix\", apply ONLY the fixes identified during review — do not re-apply the original PR's changes."
            elif git checkout -B "${FOLLOWUP_BRANCH}" 2>/dev/null; then
              # Fallback: if checkout with the computed source ref fails,
              # keep the follow-up path alive by branching from local HEAD.
              # The follow-up PR still targets ${BASE_REF}; only local start
              # point may differ from the ideal fetched/local base ref.
              echo "::warning::Could not check out ${_rb_co_src_desc} for ${FOLLOWUP_BRANCH}; creating follow-up branch from current HEAD."
              RB_COMBINED_MODE="true"
              RB_COMBINED_BRANCH_INFO="You are on a follow-up branch (${FOLLOWUP_BRANCH}) derived from the local checkout (checkout of ${_rb_co_src_desc} failed). The original PR #${RB_PR} was already merged. If you choose action=\"fix\", apply ONLY the fixes identified during review — do not re-apply the original PR's changes."
            else
              echo "::warning::Could not prepare follow-up branch ${FOLLOWUP_BRANCH} for issue #${rb_issue}; combined-mode fix not possible."
            fi
            unset _rb_co_src _rb_co_src_desc
          fi
        elif [ -n "${HEAD_REF}" ] && [ "${HEAD_REF}" != "null" ]; then
          if git fetch --no-tags origin "refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}" 2>/dev/null \
            && git checkout -B "${HEAD_REF}" "refs/remotes/origin/${HEAD_REF}" 2>/dev/null; then
            RB_COMBINED_MODE="true"
            RB_COMBINED_BRANCH_INFO="You are now on the PR branch (${HEAD_REF})."
          elif git checkout -B "${HEAD_REF}" 2>/dev/null; then
            # Fallback: same rationale as the merged-PR fallback above —
            # keep the combined-mode fix path alive when the initial fetch
            # can't reach origin, at the cost of starting the branch from
            # the local HEAD instead of origin/${HEAD_REF}.
            echo "::warning::git fetch for ${HEAD_REF} failed; reusing local HEAD as PR branch base."
            RB_COMBINED_MODE="true"
            RB_COMBINED_BRANCH_INFO="You are now on the PR branch (${HEAD_REF}), derived from the local checkout (a fresh fetch of ${HEAD_REF} failed)."
          else
            echo "::warning::Could not check out PR branch ${HEAD_REF} for issue #${rb_issue}; combined-mode fix not possible."
          fi
        else
          echo "::warning::Cannot determine PR head branch for #${RB_PR}; combined-mode fix not possible."
        fi
      fi

      # Reset workspace state (tracked files only) after a combined judge
      # call whose decision was NOT fix. Untracked files are left alone so
      # pre-fetched scripts and artifacts are not swept.
      rb_cleanup_combined_workspace() {
        if [ "${RB_COMBINED_MODE}" = "true" ]; then
          git reset --hard HEAD 2>/dev/null || true
          git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
        fi
      }

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
        SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt
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
          echo "IMPORTANT: This is the FINAL attempt. You MUST choose 'merge',"
          echo "'merge_with_followup', or 'close_and_reissue'. The 'fix' option is"
          echo "NOT available because previous fix attempts did not resolve the issues."
          echo "Pick the action that best serves the project: merge if the PR is fully"
          echo "good as-is; merge_with_followup if the PR is shippable (no build/test"
          echo "breakage, no critical correctness/security defects) but a deferred gap"
          echo "remains that should be tracked in a fresh issue (preferred over"
          echo "close_and_reissue when the PR's existing changes are worth keeping);"
          echo "close_and_reissue only if the approach is fundamentally wrong and"
          echo "the PR's work should be discarded."
        fi
        if [ "${RB_COMBINED_MODE}" = "true" ]; then
          echo
          echo "=== COMBINED DECIDE + APPLY INSTRUCTIONS ==="
          echo "${RB_COMBINED_BRANCH_INFO}"
          echo
          echo "This is a single combined judge call: decide AND (if fix) apply in one session."
          echo "- If you choose action=\"fix\": apply the fixes directly to the repository files"
          echo "  in this session. Do not defer to a follow-up step. Focus only on the issues"
          echo "  that blocked the review. Do not create new files unless absolutely required."
          echo "  After applying fixes, emit the JSON with action=\"fix\" and fix_description"
          echo "  describing what you changed."
          echo "- If you choose action=\"merge\", action=\"merge_with_followup\", or"
          echo "  action=\"close_and_reissue\": DO NOT modify any files. Emit the JSON with"
          echo "  the chosen action and an empty fix_description. For merge_with_followup,"
          echo "  populate the followup_issue { title, body } payload."
        fi
      } > "${RB_JUDGE_PROMPT_FILE}"
      rm -f "${RB_JUDGE_SEMBLE_QUERY_FILE}"

      # Run the judge
      RB_JUDGE_SUCCESS=false
      for attempt in 1 2; do
        echo "  Review-blocked judge attempt ${attempt}/2..."
        sanitize_codex_prompt_file "${RB_JUDGE_PROMPT_FILE}"
        cat "${RB_JUDGE_PROMPT_FILE}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access > "${RB_JUDGE_OUTPUT_FILE}" 2>/dev/null || true
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
        # Reset worktree + switch back to default branch if we entered
        # combined mode — otherwise the next rb_issue iteration could
        # fail to check out its target branch.
        rb_cleanup_combined_workspace
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
        rb_cleanup_combined_workspace
        continue
      fi

      emit_judge_lessons_learned_records "orchestrate_review_blocked_judge" "${rb_issue}" "${RB_PR}" "${RB_JUDGE_JSON}"

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

      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${RB_PR}/comments" \
        -f body="${RB_COMMENT}" >/dev/null 2>&1 || true

      case "${RB_ACTION}" in
        merge)
          echo "  Judge says merge PR #${RB_PR} as-is."
          # Discard any accidental file modifications the combined call
          # made on the pre-checked-out branch; merge path operates via
          # GitHub API only.
          rb_cleanup_combined_workspace
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
          # Pass the PR's base ref (3rd arg) so the gate uses the required-
          # checks filter — branch protection ∪ ORCH_FINAL_MERGE_REQUIRED_CHECKS
          # — instead of the legacy block-on-ANY-failing-check mode. Reuses
          # the already-fetched PR JSON (no extra API call, §15). A red
          # non-required/environmental check (e.g. CodeQL with code scanning
          # disabled) no longer deadlocks the review-blocked merge.
          _rb_merge_base="$(_jq_field "${_rb_merge_json}" '.base.ref')"
		  if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}" "${_rb_merge_sha}" "${_rb_merge_base}"; then
		    if gh_retry gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
		      echo "  PR #${RB_PR} merge initiated (auto)."
		      RB_MERGED="true"
		    elif gh_retry gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
		      echo "  PR #${RB_PR} merged directly."
		      RB_MERGED="true"
		    else
		      echo "::warning::Could not merge PR #${RB_PR}."
		    fi
          elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
            echo "  PR #${RB_PR} is not mergeable. Attempting branch update..."
            if gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
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
            gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
              --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
            RB_FORCE_MERGED="false"
            _rb_fm_json="$(_fetch_pr_json "${RB_PR}")"
            PR_STATE="$(_jq_field "${_rb_fm_json}" '.state' 'open|closed|merged')"
            PR_MERGEABLE="$(_jq_field "${_rb_fm_json}" '.mergeable' 'true|false')"
            _rb_fm_sha="$(_jq_field "${_rb_fm_json}" '.head.sha')"
            # Required-checks filter via the PR's base ref (see the merge)
            # branch above) — no extra API call, reuses _rb_fm_json.
            _rb_fm_base="$(_jq_field "${_rb_fm_json}" '.base.ref')"
				if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}" "${_rb_fm_sha}" "${_rb_fm_base}"; then
				  if gh_retry gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto \
				    || gh_retry gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
				    RB_FORCE_MERGED="true"
				  else
				    echo "::warning::Could not merge PR #${RB_PR} in force-merge path."
				  fi
            elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
              echo "  PR #${RB_PR} is not mergeable (force-merge path). Attempting branch update..."
              if gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
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
              gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                --remove-label 'ai:review-blocked' 2>/dev/null || true
              REVIEW_BLOCKED_STATE_CHANGED=true
            else
            # Branch was prepared upfront (before the judge call) and the
            # combined codex call has already applied the fixes in-session
            # when action=fix. Skip the duplicated branch prep and the
            # second codex call; proceed straight to commit/push.
            #
            # Upfront-set globals still valid here:
            #   RB_TARGET_MERGED, HEAD_REF, BASE_REF, FOLLOWUP_BRANCH,
            #   ORCH_FOLLOWUP_OWNED, ORCH_FOLLOWUP_TRACKING_NUM,
            #   ORCH_FOLLOWUP_INTEGRATION_BRANCH,
            #   ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS, FOLLOWUP_PR_BLOCKED
            #
            # Race edge case: the PR was open when we prepped upfront
            # (RB_TARGET_MERGED=false, we checked out HEAD_REF) but was
            # merged during the combined codex call. RB_PR_MERGED_NOW is
            # the race-check fetch right above us. In that case we skip
            # commit/push this tick; the next tick sees the merged state
            # at the top of the loop and prep runs the merged-path
            # (follow-up branch) cleanly.
            #
            # No fix is applied in this path, so skip the retry-counter
            # increment below — an external merge race must not consume
            # a review-blocked retry slot.
            RB_SKIP_RETRY_INCREMENT="false"
            STATE_FOLLOWUP_INTEGRATION_BRANCH=""
            STATE_FOLLOWUP_INTEGRATION_BRANCH_EXISTS="false"
            FOLLOWUP_ACTIVE_INTEGRATION_CONTEXT="false"
            if [ "${RB_TARGET_MERGED}" = "true" ]; then
              STATE_FOLLOWUP_INTEGRATION_BRANCH="$(jq -r '.integration_branch // empty' "${STATE_FILE}" 2>/dev/null || echo "")"
              if [ -n "${STATE_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                FOLLOWUP_ACTIVE_INTEGRATION_CONTEXT="true"
                if integration_branch_exists "${STATE_FOLLOWUP_INTEGRATION_BRANCH}"; then
                  STATE_FOLLOWUP_INTEGRATION_BRANCH_EXISTS="true"
                fi
              fi
            fi
            if [ "${RB_COMBINED_MODE}" = "true" ] \
               && [ "${RB_TARGET_MERGED}" != "true" ] \
               && [ "${RB_PR_MERGED_NOW}" = "true" ]; then
              echo "::warning::Race detected: PR #${RB_PR} merged during combined judge call (prepped as open, now merged). Deferring fix application to the next poll tick."
              rb_cleanup_combined_workspace
              REVIEW_BLOCKED_STATE_CHANGED=true
              RB_SKIP_RETRY_INCREMENT="true"
            elif [ "${RB_COMBINED_MODE}" != "true" ]; then
              echo "::warning::Combined-mode branch prep did not succeed for PR #${RB_PR}; cannot apply judge fixes this tick."
              git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
              REVIEW_BLOCKED_STATE_CHANGED=true
            else
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
                  rm -f unattended_system_instructions.md ai_pipeline.md agents.md probably_unnecessary_but_read_if_stuck.md
                  rm -f scripts/git_ref_health_check.sh \
                    scripts/generate_symbol_diff_summary.py scripts/label_helpers.sh scripts/tg_helpers.sh \
                    scripts/codex_model_catalog.json
                  rm -rf .github/prompts .github/scripts
                  rm -f .github/ai/orchestrate_schema.v1.json
                  ;;
              esac
              unset _orig_origin_url

              # Check if there are changes to commit
              if [ -n "$(git status --porcelain)" ]; then
                git config user.name "codex-bot"
                git config user.email "codex@users.noreply.github.com"
                if [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ]; then
                  # Use a single add call so empty/minimal repos do not fail on
                  # exclude-only pathspecs.
                  # NOTE: do not list .gitignored directories (node_modules)
                  # as `:!` exclude pathspecs here. `git add -A -- . ':!<dir>'`
                  # treats the exclude path as an explicit name and fails with
                  # "The following paths are ignored by one of your .gitignore
                  # files" + exit 1 when that dir exists on disk. .gitignore
                  # already excludes them; the pathspec exclude is redundant
                  # and turns into a hard failure once a step creates
                  # node_modules/.
                  git add -A -- . ':!.github/prompts' ':!.github/scripts'
                else
                  # Keep workflow-edit guard exclusions while avoiding brittle
                  # tracked/untracked split staging pathspec failures. Same
                  # gitignore-dir exclusion caveat as above applies.
                  git add -A -- . ':!scripts' ':!prompts' ':!.github/ai' ':!.github/workflows' ':!.github/prompts' ':!.github/scripts'
                fi
                echo "Staged files before commit:"
                git diff --cached --name-only | sed 's/^/ - /' || true
                if [ "${ALLOW_WORKFLOW_EDITS:-true}" != "true" ] && git diff --cached --name-only | grep -E '^(scripts/|prompts/|\.github/ai/|\.github/workflows/)'; then
                  echo "Error: scripts/, prompts/, .github/ai/, or .github/workflows is staged while ALLOW_WORKFLOW_EDITS=false"
                  exit 1
                fi
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
                  if [ "${RB_INTEGRATION_BRANCH_VALID}" = "true" ] \
                    && { [ "${BASE_REF}" = "${DEFAULT_BRANCH:-main}" ] || [ "${BASE_REF}" = "main" ]; }; then
                    echo "::warning::Refusing follow-up PR creation for merged PR #${RB_PR}: integration branch '${RB_INTEGRATION_BRANCH}' is active but computed base is '${BASE_REF}'."
                    tg_notify "Refused merged follow-up PR creation for review-blocked issue #${rb_issue} (PR #${RB_PR}): integration branch '${RB_INTEGRATION_BRANCH}' is active but computed base was '${BASE_REF}'."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                    RB_FOLLOWUP_REFUSED="true"
                    REVIEW_BLOCKED_STATE_CHANGED=true
                  elif git push origin "HEAD:${FOLLOWUP_BRANCH}" 2>/dev/null; then
                    echo "  Pushed follow-up branch ${FOLLOWUP_BRANCH}."

                    if [ "${STATE_FOLLOWUP_INTEGRATION_BRANCH_EXISTS}" = "true" ] && [ -n "${STATE_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                      if [ "${BASE_REF}" != "${STATE_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                        echo "::warning::Detected follow-up PR base '${BASE_REF}' for issue #${rb_issue}; retargeting to state integration branch '${STATE_FOLLOWUP_INTEGRATION_BRANCH}'."
                        BASE_REF="${STATE_FOLLOWUP_INTEGRATION_BRANCH}"
                      fi
                    elif [ "${ORCH_FOLLOWUP_OWNED}" = "true" ] && [ "${ORCH_FOLLOWUP_INTEGRATION_BRANCH_EXISTS}" = "true" ] && [ -n "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                      if [ "${BASE_REF}" != "${ORCH_FOLLOWUP_INTEGRATION_BRANCH}" ]; then
                        echo "::warning::Detected follow-up PR base '${BASE_REF}' for orchestrator-owned issue #${rb_issue}; retargeting to '${ORCH_FOLLOWUP_INTEGRATION_BRANCH}'."
                        BASE_REF="${ORCH_FOLLOWUP_INTEGRATION_BRANCH}"
                      fi
                    fi

                    if [ "${FOLLOWUP_ACTIVE_INTEGRATION_CONTEXT}" = "true" ] && { [ "${BASE_REF}" = "${DEFAULT_BRANCH:-main}" ] || [ "${BASE_REF}" = "main" ]; }; then
                      FOLLOWUP_GUARD_REASON="Issue #${rb_issue} has active integration context ('${STATE_FOLLOWUP_INTEGRATION_BRANCH:-<missing>}'); refusing to create follow-up PR against '${BASE_REF}'."
                      echo "::warning::${FOLLOWUP_GUARD_REASON}"
                      ORIGINAL_TRACKING_NUM="${TRACKING_NUM:-}"
                      if [ -n "${ORCH_FOLLOWUP_TRACKING_NUM:-}" ]; then
                        TRACKING_NUM="${ORCH_FOLLOWUP_TRACKING_NUM}"
                      fi
                      post_tracking_comment "## ⚠️ Follow-up PR blocked

${FOLLOWUP_GUARD_REASON}"
                      tg_notify "${FOLLOWUP_GUARD_REASON}" "WARNING"
                      TRACKING_NUM="${ORIGINAL_TRACKING_NUM}"
                      FOLLOWUP_PR_URL=""
                      RB_FOLLOWUP_REFUSED="true"
                      REVIEW_BLOCKED_STATE_CHANGED=true
                    else
                      FOLLOWUP_PR_URL="$(gh_retry gh pr create \
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
                      gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
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
                    gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
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
                  gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' 2>/dev/null || true
                  tg_notify "Orchestrator judge found no fixes needed for merged PR #${RB_PR} (issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                else
                  echo "  Treating as merge decision."
                  ensure_label_exists "ai:ready-to-merge"
                  gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
                  _rb_nofix_json="$(_fetch_pr_json "${RB_PR}")"
                  PR_STATE="$(_jq_field "${_rb_nofix_json}" '.state' 'open|closed|merged')"
                  PR_MERGEABLE="$(_jq_field "${_rb_nofix_json}" '.mergeable' 'true|false')"
                  _rb_nofix_sha="$(_jq_field "${_rb_nofix_json}" '.head.sha')"
                  # Required-checks filter via the PR's base ref (see the
                  # merge) branch above) — no extra API call, reuses _rb_nofix_json.
                  _rb_nofix_base="$(_jq_field "${_rb_nofix_json}" '.base.ref')"
                  if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}" "${_rb_nofix_sha}" "${_rb_nofix_base}"; then
                    if gh_retry gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto \
                      || gh_retry gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
                      tg_notify "Orchestrator judge merged PR #${RB_PR} (no fix changes needed, issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                    else
                      echo "::warning::Could not merge PR #${RB_PR} in no-fix merge path."
                    fi
                  elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
                    echo "  PR #${RB_PR} is not mergeable in no-fix merge path. Skipping merge notification."
                  fi
                fi
              fi

              # Switch back to default branch for remaining processing,
              # discarding any unstaged tracked edits that were not
              # included in the fix commit (e.g., excluded paths).
              rb_cleanup_combined_workspace
            fi

            # Increment retry counter (skipped when the merge-race path
            # above deferred the fix without applying anything).
            if [ "${RB_SKIP_RETRY_INCREMENT:-false}" != "true" ]; then
              jq ".review_blocked_retries[\"${rb_issue}\"] = $((RETRY_COUNT + 1))" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
              REVIEW_BLOCKED_STATE_CHANGED=true
            fi
          fi
          fi
          ;;

        merge_with_followup)
          echo "  Judge says merge PR #${RB_PR} and open a follow-up issue for the deferred gap."
          # Discard any accidental file modifications from the combined
          # judge call; this path operates via GitHub API only.
          rb_cleanup_combined_workspace

          # Parse follow-up details before any state changes. Missing
          # details would silently downgrade the action to a plain merge
          # with no tracking issue, defeating the purpose of the new
          # action — refuse and leave the issue in ai:review-blocked
          # for stall recovery / retry.
          FOLLOWUP_TITLE="$(echo "${RB_JUDGE_JSON}" | jq -r '.followup_issue.title // empty')"
          FOLLOWUP_BODY="$(echo "${RB_JUDGE_JSON}" | jq -r '.followup_issue.body // empty' | sed 's/\\n/\n/g')"
          if [ -z "${FOLLOWUP_TITLE}" ] || [ -z "${FOLLOWUP_BODY}" ]; then
            # Structured log + WARNING tg_notify so the refusal is
            # observable to operators and downstream log analysis
            # (parity with the standalone rb_judge.sh's
            # judge_skip_reason=missing_followup_details emission;
            # the orchestrator doesn't write GITHUB_OUTPUT but
            # surfaces the refusal via the same MWF_REFUSAL prefix
            # so workflow-log-analysis can grep for it).
            echo "::error::Judge chose merge_with_followup for PR #${RB_PR} (issue #${rb_issue}) but provided no follow-up details (followup_issue.title or .body empty). Refusing — leaving issue in ai:review-blocked for stall recovery."
            echo "MWF_REFUSAL action=merge_with_followup pr=${RB_PR} issue=${rb_issue} reason=missing_followup_details"
            tg_notify "Orchestrator merge_with_followup REFUSED for PR #${RB_PR} (issue #${rb_issue}): judge provided no follow-up issue details (followup_issue.title or .body empty). Issue stays in ai:review-blocked; stall recovery will retry. Manual fallback: open the follow-up issue manually and reference PR #${RB_PR}."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
            REVIEW_BLOCKED_STATE_CHANGED=true
          else
            # Re-fetch PR data (state may have changed since the judge
            # ran) and run the MERGE_CONFIRMED ladder. Mirrors the
            # standalone review_rb_judge.sh script's merge_with_followup
            # branch — see that file for the rationale on each gate.
            _rb_mwf_json="$(_fetch_pr_json "${RB_PR}")"
            # GitHub's REST /pulls/{N} returns .state as one of `open`
            # or `closed` (never `merged` — merged PRs are state=closed
            # + merged=true). Constrain the validator to the actual API
            # vocabulary; .merged below disambiguates the two.
            PR_STATE="$(_jq_field "${_rb_mwf_json}" '.state' 'open|closed')"
            PR_MERGEABLE="$(_jq_field "${_rb_mwf_json}" '.mergeable' 'true|false')"
            # Detect merged via `(.merged_at != null) or (.merged ==
            # true)` — matches the orchestrator's existing
            # `.merged_at != null` pattern (see line ~8844 etc.) and
            # survives REST payloads that omit either field
            # individually.
            PR_MERGED_NOW="$(_jq_field "${_rb_mwf_json}" '(.merged_at != null) or (.merged == true)' 'true|false')"
            [ -n "${PR_MERGED_NOW}" ] || PR_MERGED_NOW="false"
            _rb_mwf_sha="$(_jq_field "${_rb_mwf_json}" '.head.sha')"
            # Required-checks filter via the PR's base ref (see the merge)
            # branch above) — no extra API call, reuses _rb_mwf_json.
            _rb_mwf_base="$(_jq_field "${_rb_mwf_json}" '.base.ref')"

            MERGE_CONFIRMED="false"
            if [ "${PR_MERGED_NOW}" = "true" ]; then
              echo "  PR #${RB_PR} already merged (.merged=true) before merge_with_followup ran."
              MERGE_CONFIRMED="true"
            elif [ "${PR_STATE}" = "closed" ]; then
              echo "::warning::PR #${RB_PR} closed without merge — skipping follow-up creation; deferred gap not tracked because source PR's changes never landed."
            elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
              # _pr_checks_completed gates the merge attempt the same
              # way the other orchestrator review-blocked merge actions
              # do. Passing the PR's base ref (3rd arg) selects the
              # required-checks filter — pending check-runs always block,
              # but a FAILED non-required/advisory check (e.g. CodeQL
              # when code scanning is disabled) no longer blocks, matching
              # the final integration-merge path and unblocking the
              # review-blocked-judge merge that previously deadlocked on a
              # permanently-red environmental check.
              if ! _pr_checks_completed "${RB_PR}" "${_rb_mwf_sha}" "${_rb_mwf_base}"; then
                echo "::warning::PR #${RB_PR} mergeable=true but check-runs still pending/failing — leaving issue in ai:review-blocked. Next orchestrator poll cycle will re-fire the judge after checks complete; that run will hit the PR_MERGED_NOW=true short path (after the existing \`merge)\` action's auto-merge enrollment lands the PR)."
              elif [ -z "${_rb_mwf_sha}" ]; then
                # Defensive: `_pr_checks_completed` may have re-fetched
                # the SHA locally and returned 0 while our outer
                # `_rb_mwf_sha` is still empty (transient API failure
                # at the initial fetch). Refuse the merge here so we
                # never call `gh pr merge` without --match-head-commit
                # — an unbound merge could let a concurrent push slip
                # in. Leave the issue in ai:review-blocked for the
                # next poll cycle, which re-fetches PR metadata.
                echo "::warning::PR #${RB_PR} head SHA could not be resolved from the PR-meta fetch — refusing merge_with_followup to avoid an unbound merge (no --match-head-commit guard against concurrent pushes). Leaving issue in ai:review-blocked."
              elif [ "${ENABLE_AUTO_MERGE}" = "true" ]; then
                # Sync merge only — NEVER --auto enrollment. The whole
                # point of the conservative ladder is to ensure follow-
                # up creation happens only against a definitively-
                # merged base. `gh pr merge --squash` (no --auto)
                # returns 0 iff the merge actually landed (all required
                # checks satisfied, no conflicts, no merge-queue
                # rejection). When checks are still pending it returns
                # non-zero — we leave the issue in ai:review-blocked
                # and let the orchestrator's next ~5min poll re-fire
                # the judge. That run will hit the PR_MERGED_NOW=true
                # short path (after the existing `merge)` action has
                # had a chance to enroll auto-merge and the merge has
                # actually landed asynchronously) and create the
                # follow-up against the now-real base ref. This
                # eliminates the orphan-follow-up risk of --auto
                # enrollment; the trade-off is that protected-branch
                # repos need one extra poll cycle to materialize the
                # follow-up. Matches scripts/review_rb_judge.sh's
                # merge_with_followup ladder behaviourally — both
                # paths gate on check-runs via the SAME shared
                # `_pr_checks_completed` helper (scripts/pr_checks_lib.sh),
                # so the required-checks filter is identical, and both
                # bind the merge via --match-head-commit.
                #
                # NOTE: gh pr merge is intentionally NOT wrapped with
                # gh_retry — sync merge failures here are typically
                # non-transient (pending required checks, branch
                # protection rules, merge-queue mode) and retrying
                # them only adds backoff cost before falling through
                # to the same warning path. Matches the standalone
                # review_rb_judge.sh's pattern.
                #
                # `--match-head-commit "${_rb_mwf_sha}"` binds the
                # merge to the head SHA the PR-meta fetch observed.
                # The `elif [ -z "${_rb_mwf_sha}" ]` branch above
                # ensures we never reach here with an empty SHA, so
                # the merge is always bound — no concurrent-push
                # window between judge decision and merge.
                _rb_mwf_match_arg=(--match-head-commit "${_rb_mwf_sha}")
                if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash "${_rb_mwf_match_arg[@]}" 2>/dev/null; then
                  echo "  PR #${RB_PR} merged synchronously."
                  MERGE_CONFIRMED="true"
                else
                  echo "::warning::PR #${RB_PR} sync merge failed (typically: required checks still pending, branch protection rules, merge queue, permissions, or 422). Leaving issue in ai:review-blocked — next poll cycle will re-fire the judge after merge lands; that run hits the PR_MERGED_NOW=true short path and creates the follow-up."
                fi
              else
                echo "::warning::PR #${RB_PR} is mergeable but ENABLE_AUTO_MERGE=false — manual merge required. Leaving issue in ai:review-blocked so the follow-up is not opened against unmerged code; operator should merge manually and the next orchestrator poll cycle will create the follow-up via the PR_MERGED_NOW=true short path."
              fi
            elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
              echo "::warning::PR #${RB_PR} has merge conflicts (mergeable=false); judge cannot merge as-is. Leaving issue in ai:review-blocked so conflicts can be resolved before follow-up creation."
            else
              echo "::warning::PR #${RB_PR} state=${PR_STATE} mergeable=${PR_MERGEABLE} merged=${PR_MERGED_NOW}, cannot confirm merge (checks pending / mergeability still computing / PR not open). Leaving issue in ai:review-blocked."
            fi

            if [ "${MERGE_CONFIRMED}" = "true" ]; then
              # Build follow-up body with full orchestrator metadata
              # footer (mirrors close_and_reissue's pattern). The
              # follow-up is a standalone orchestrator-managed issue
              # that flows through clarify -> plan -> implement on its
              # own; we do NOT remap the original wave slot (the
              # original issue is merged, not closed, so the wave's
              # accounting of the merged issue should stay).
              RB_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
              FULL_FOLLOWUP_BODY="${FOLLOWUP_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: ${RB_INTEGRATION_BRANCH}
- Source PR: #${RB_PR} (review-blocked judge merged with deferred gap tracked here)
- Parent issue: #${rb_issue}
- Type: review-blocked-followup
- Managed by: AI Orchestrator"

              ensure_label_exists "ai:clarification"
              ensure_label_exists "ai:orchestrator-managed"
              # Create follow-up FIRST. If it fails (transient API /
              # disabled-issues / permissions / token scope), do NOT
              # advance the linked issue's labels — leave it in
              # ai:review-blocked so stall recovery / the next judge
              # run retries follow-up creation. Otherwise the PR is
              # merged but the deferred gap has no durable tracking.
              FOLLOWUP_URL=""
              FOLLOWUP_NUM=""
              if FOLLOWUP_URL="$(gh_retry gh issue create \
                  --repo "${GITHUB_REPOSITORY}" \
                  --title "${FOLLOWUP_TITLE}" \
                  --body "${FULL_FOLLOWUP_BODY}" \
                  --label "ai:clarification" \
                  --label "ai:orchestrator-managed")"; then
                FOLLOWUP_URL_CLEAN="$(printf '%s\n' "${FOLLOWUP_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
                FOLLOWUP_NUM="$(basename "${FOLLOWUP_URL_CLEAN%%[?#]*}")"
                echo "  Created follow-up issue #${FOLLOWUP_NUM}: ${FOLLOWUP_TITLE}"
              else
                _create_rc=$?
                echo "::error::Failed to create follow-up issue for merge_with_followup (rc=${_create_rc}; PR #${RB_PR} merge confirmed but deferred gap untracked). Leaving issue #${rb_issue} in ai:review-blocked so stall recovery / next judge run can retry. Manual fallback: open an issue describing the gap and reference PR #${RB_PR}."
                tg_notify "Orchestrator merge_with_followup: PR #${RB_PR} (issue #${rb_issue}) merged but follow-up issue creation failed (rc=${_create_rc}). Deferred gap is currently untracked — stall recovery will retry. Manual fallback may be required."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                FOLLOWUP_URL=""
                FOLLOWUP_NUM=""
              fi

              if [ -n "${FOLLOWUP_URL}" ] && [[ "${FOLLOWUP_NUM}" =~ ^[0-9]+$ ]]; then
                # Phase-swap the linked issue only after BOTH merge AND
                # follow-up creation are confirmed.
                ensure_label_exists "ai:ready-to-merge"
                gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                  --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true

                tg_notify "Orchestrator judge merge_with_followup: PR #${RB_PR} merged (issue #${rb_issue}); follow-up issue #${FOLLOWUP_NUM} created for deferred gap. ${RB_JUSTIFICATION}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Parent issue: $(_gh_url "issues/${rb_issue}")"$'\n'"Follow-up: $(_gh_url "issues/${FOLLOWUP_NUM}")" "DEBUG"
              fi
              REVIEW_BLOCKED_STATE_CHANGED=true
            else
              # MERGE_CONFIRMED=false — leave issue in ai:review-blocked
              # so stall recovery / next judge run handles it.
              REVIEW_BLOCKED_STATE_CHANGED=true
            fi
          fi
          ;;

        close_and_reissue)
          echo "  Judge says close PR #${RB_PR} and reissue."
          # Discard any accidental file modifications from the combined
          # judge call; close_and_reissue operates via GitHub API only.
          rb_cleanup_combined_workspace
          # Close the PR
          gh_retry gh pr close "${RB_PR}" --repo "${GITHUB_REPOSITORY}" \
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
            ensure_label_exists "ai:orchestrator-managed"
            NEW_URL="$(gh_retry gh issue create \
              --repo "${GITHUB_REPOSITORY}" \
              --title "${NEW_ISSUE_TITLE}" \
              --body "${FULL_NEW_BODY}" \
              --label "ai:clarification" \
              --label "ai:orchestrator-managed")"
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
          rb_cleanup_combined_workspace
          ;;
      esac
    done < <(echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "review-blocked") | .github_issue')

    # Persist updated state if any review-blocked issues were handled
    if [ "${REVIEW_BLOCKED_STATE_CHANGED}" = "true" ]; then
      post_state_comment || true

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
        LABELS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels" --jq '[.[].name]' || echo '[]')"
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
        LABELS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rnum}/labels" --jq '[.[].name]' || echo '[]')"
        [ -z "${LABELS}" ] && LABELS='[]'
        LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${rnum}" --argjson labels "${LABELS}" '. + {($key): $labels}' 2>/dev/null || echo "${LABELS_JSON}")"
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

    # Look up the wave issue entry for this source issue.
    IF_ENTRY_JSON="$(jq -c --arg if_issue "${if_issue}" --argjson wave_idx "${WAVE_IDX}" \
      '.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue) | .' \
      "${STATE_FILE}" | head -n 1)"
    IF_LOCAL_ID="$(echo "${IF_ENTRY_JSON}" | jq -r '.id // ""' 2>/dev/null || echo "")"

    IF_MODE="no-op-implementation"
    IF_BLOCKERS_JSON='[]'
    IF_BLOCKERS_SOURCE="none"
    IF_DEFER_REISSUE="false"
    IF_DEFER_REASON=""

    IF_STORED_BLOCKERS_JSON="$(echo "${IF_ENTRY_JSON}" | jq -c '
      if type == "object" then
        (
          ((if (.depends_on | type) == "array" then .depends_on else [] end)
           +
           (if (.reissue_depends_on | type) == "array" then .reissue_depends_on else [] end))
          | map(tostring | ltrimstr("#") | tonumber?)
          | map(select(. != null))
          | unique
        )
      else
        []
      end
    ' 2>/dev/null || echo '[]')"

    IF_COMMENTS_JSON='[]'
    if IF_COMMENTS_JSON="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${if_issue}/comments?sort=created&direction=desc&per_page=100" 2>/dev/null | jq -s 'add // []' 2>/dev/null)" \
      && echo "${IF_COMMENTS_JSON}" | jq -e 'type == "array"' >/dev/null 2>&1; then
      :
    else
      IF_COMMENTS_JSON='[]'
      IF_MODE="post-codex-validation"
      IF_DEFER_REISSUE="true"
      IF_DEFER_REASON="unable to fetch issue comments for post-codex blocker detection"
    fi

    IF_POST_CODEX_ANY_COMMENT_JSON="$(echo "${IF_COMMENTS_JSON}" | jq -c '[.[] | select((.body // "") | test("^## Post-Codex validation"))] | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)]) // empty' 2>/dev/null || true)"
    IF_POST_CODEX_FIX_COMMENT_JSON="$(echo "${IF_COMMENTS_JSON}" | jq -c '[.[] | select((.body // "") | startswith("## Post-Codex validation diagnosed follow-up fixes"))] | max_by([(.created_at // ""), ((.id // 0) | tonumber? // 0)]) // empty' 2>/dev/null || true)"

    IF_PARSED_BLOCKERS_JSON='[]'
    IF_PARSED_BLOCKER_COUNT=0
    IF_STORED_BLOCKER_COUNT="$(echo "${IF_STORED_BLOCKERS_JSON}" | jq 'length' 2>/dev/null || echo "0")"
    IF_HAS_POST_CODEX_CONTEXT="false"
    if [ -n "${IF_POST_CODEX_ANY_COMMENT_JSON}" ]; then
      IF_HAS_POST_CODEX_CONTEXT="true"
    fi

    if [ -n "${IF_POST_CODEX_FIX_COMMENT_JSON}" ]; then
      IF_FIX_COMMENT_BODY="$(echo "${IF_POST_CODEX_FIX_COMMENT_JSON}" | jq -r '.body // ""')"
      IF_PARSED_BLOCKERS_JSON="$(extract_fix_issues_from_comment "${IF_FIX_COMMENT_BODY}" | jq -R 'select(length > 0) | tonumber' | jq -s 'unique')"
      IF_PARSED_BLOCKER_COUNT="$(echo "${IF_PARSED_BLOCKERS_JSON}" | jq 'length' 2>/dev/null || echo "0")"
    fi

    if [ "${IF_PARSED_BLOCKER_COUNT}" -gt 0 ]; then
      IF_MODE="post-codex-validation"
      IF_BLOCKERS_JSON="${IF_PARSED_BLOCKERS_JSON}"
      IF_BLOCKERS_SOURCE="post-codex-comment"
    elif [ "${IF_STORED_BLOCKER_COUNT}" -gt 0 ]; then
      IF_MODE="post-codex-validation"
      IF_BLOCKERS_JSON="${IF_STORED_BLOCKERS_JSON}"
      IF_BLOCKERS_SOURCE="state"
    elif [ "${IF_HAS_POST_CODEX_CONTEXT}" = "true" ]; then
      IF_MODE="post-codex-validation"
      IF_DEFER_REISSUE="true"
      IF_DEFER_REASON="post-codex blocker metadata missing or malformed"
    fi

    IF_BLOCKER_COUNT="$(echo "${IF_BLOCKERS_JSON}" | jq 'length' 2>/dev/null || echo "0")"
    if [ "${IF_BLOCKER_COUNT}" -gt 0 ] && [[ -n "${IF_LOCAL_ID}" && "${IF_LOCAL_ID}" != "null" ]]; then
      IF_STATE_TMP="${STATE_FILE}.impl_dep.tmp"
      if jq --arg if_issue "${if_issue}" --argjson wave_idx "${WAVE_IDX}" --argjson blockers "${IF_BLOCKERS_JSON}" '
        (.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue)) |= (
          if (.depends_on | type) == "array" then
            .depends_on = ((.depends_on + $blockers) | unique)
          else
            .reissue_depends_on = (((.reissue_depends_on // []) + $blockers) | unique)
          end
        )
      ' "${STATE_FILE}" > "${IF_STATE_TMP}"; then
        if ! cmp -s "${STATE_FILE}" "${IF_STATE_TMP}"; then
          mv "${IF_STATE_TMP}" "${STATE_FILE}"
          IMPL_FAILED_STATE_CHANGED=true
        else
          rm -f "${IF_STATE_TMP}"
        fi
      else
        rm -f "${IF_STATE_TMP}" 2>/dev/null || true
      fi
    fi

    if [ "${IF_MODE}" = "post-codex-validation" ] && [ "${IF_BLOCKER_COUNT}" -gt 0 ]; then
      IF_BLOCKER_OPEN_COUNT=0
      IF_BLOCKER_UNKNOWN_COUNT=0
      IF_BLOCKER_STATUS_SUMMARY=""

      while IFS= read -r blocker_issue; do
        [ -n "${blocker_issue}" ] || continue
        BLOCKER_STATE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${blocker_issue}" --jq '.state' || echo "")"
        case "${BLOCKER_STATE}" in
          open)
            IF_BLOCKER_OPEN_COUNT=$((IF_BLOCKER_OPEN_COUNT + 1))
            IF_BLOCKER_STATUS_SUMMARY+="#${blocker_issue}=open "
            ;;
          closed)
            IF_BLOCKER_STATUS_SUMMARY+="#${blocker_issue}=closed "
            ;;
          *)
            IF_BLOCKER_UNKNOWN_COUNT=$((IF_BLOCKER_UNKNOWN_COUNT + 1))
            IF_BLOCKER_STATUS_SUMMARY+="#${blocker_issue}=unknown "
            ;;
        esac
      done < <(echo "${IF_BLOCKERS_JSON}" | jq -r '.[]')

      if [ "${IF_BLOCKER_UNKNOWN_COUNT}" -gt 0 ]; then
        IF_DEFER_REISSUE="true"
        IF_DEFER_REASON="blocker status lookup incomplete"
      elif [ "${IF_BLOCKER_OPEN_COUNT}" -gt 0 ]; then
        IF_DEFER_REISSUE="true"
        IF_DEFER_REASON="blocker fix-up issue(s) still open"
      fi

      if [ "${IF_DEFER_REISSUE}" = "true" ]; then
        IF_BLOCKERS_CSV="$(echo "${IF_BLOCKERS_JSON}" | jq -r 'map("#" + tostring) | join(", ")')"
        IF_DEFER_SIGNATURE="${IF_BLOCKER_STATUS_SUMMARY}|${IF_DEFER_REASON}"
        IF_PREV_SIGNATURE="$(jq -r --arg key "${if_issue}" '.implementation_failed_defer_state[$key].summary // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
        IF_PREV_COUNT="$(jq -r --arg key "${if_issue}" '.implementation_failed_defer_state[$key].count // 0' "${STATE_FILE}" 2>/dev/null || echo "0")"
        IF_PREV_ESCALATED="$(jq -r --arg key "${if_issue}" '.implementation_failed_defer_state[$key].escalated // false' "${STATE_FILE}" 2>/dev/null || echo "false")"
        [[ "${IF_PREV_COUNT}" =~ ^[0-9]+$ ]] || IF_PREV_COUNT=0
        if [ "${IF_DEFER_SIGNATURE}" = "${IF_PREV_SIGNATURE}" ]; then
          IF_DEFER_COUNT=$((IF_PREV_COUNT + 1))
          IF_ESCALATED_FLAG="${IF_PREV_ESCALATED}"
        else
          IF_DEFER_COUNT=1
          # Signature changed — fresh defer window, reset the escalation flag.
          IF_ESCALATED_FLAG="false"
        fi
        IF_SHOULD_ESCALATE="false"
        if [ "${IF_DEFER_COUNT}" -ge "${MAX_IMPL_FAILED_DEFER_CYCLES}" ] && [ "${IF_ESCALATED_FLAG}" != "true" ]; then
          IF_SHOULD_ESCALATE="true"
          IF_ESCALATED_FLAG="true"
        fi
        if jq --arg key "${if_issue}" --arg summary "${IF_DEFER_SIGNATURE}" --argjson count "${IF_DEFER_COUNT}" --argjson escalated "${IF_ESCALATED_FLAG}" '
          .implementation_failed_defer_state //= {}
          | .implementation_failed_defer_state[$key] = {summary: $summary, count: $count, escalated: $escalated}
        ' "${STATE_FILE}" > "${STATE_FILE}.tmp"; then
          mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          IMPL_FAILED_STATE_CHANGED=true
        else
          rm -f "${STATE_FILE}.tmp" 2>/dev/null || true
        fi

        echo "  Deferring implementation-failed reissue for #${if_issue} (${IF_LOCAL_ID}): mode=${IF_MODE}; blockers=${IF_BLOCKERS_CSV}; statuses=${IF_BLOCKER_STATUS_SUMMARY}; reason=${IF_DEFER_REASON}; cycle=${IF_DEFER_COUNT}/${MAX_IMPL_FAILED_DEFER_CYCLES}; escalated=${IF_ESCALATED_FLAG}."
        if [ "${IF_SHOULD_ESCALATE}" = "true" ]; then
          ensure_label_exists "ai:needs-human"
          gh_retry gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:needs-human" >/dev/null 2>&1 || true
          gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${if_issue}/comments" \
            -f body="$(printf '## Post-Codex implementation-failed deferral escalated\n\nThis source issue has been deferred for %s consecutive poll cycles with the same blocker status (%s). Blocker fix-ups [%s] are not advancing on their own — escalating to `ai:needs-human` for manual review. Reason: %s.' "${IF_DEFER_COUNT}" "${IF_BLOCKER_STATUS_SUMMARY}" "${IF_BLOCKERS_CSV}" "${IF_DEFER_REASON}")" >/dev/null 2>&1 || true
          tg_notify "Implementation-failed reissue for #${if_issue} (${IF_LOCAL_ID}) escalated to ai:needs-human after ${IF_DEFER_COUNT} deferred cycles."$'\n'"Mode: ${IF_MODE}"$'\n'"Blockers: ${IF_BLOCKERS_CSV}"$'\n'"Statuses: ${IF_BLOCKER_STATUS_SUMMARY}"$'\n'"Reason: ${IF_DEFER_REASON}"$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "CRITICAL"
        elif [ "${IF_DEFER_SIGNATURE}" != "${IF_PREV_SIGNATURE}" ]; then
          tg_notify "Deferred implementation-failed reissue for #${if_issue} (${IF_LOCAL_ID})."$'\n'"Mode: ${IF_MODE}"$'\n'"Blockers: ${IF_BLOCKERS_CSV}"$'\n'"Statuses: ${IF_BLOCKER_STATUS_SUMMARY}"$'\n'"Reason: ${IF_DEFER_REASON}"$'\n'"Cycle: ${IF_DEFER_COUNT}/${MAX_IMPL_FAILED_DEFER_CYCLES}"$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "WARNING"
        fi
        continue
      fi
    elif [ "${IF_MODE}" = "post-codex-validation" ] && [ "${IF_DEFER_REISSUE}" = "true" ]; then
      IF_DEFER_SIGNATURE="|${IF_DEFER_REASON}"
      IF_PREV_SIGNATURE="$(jq -r --arg key "${if_issue}" '.implementation_failed_defer_state[$key].summary // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
      IF_PREV_COUNT="$(jq -r --arg key "${if_issue}" '.implementation_failed_defer_state[$key].count // 0' "${STATE_FILE}" 2>/dev/null || echo "0")"
      IF_PREV_ESCALATED="$(jq -r --arg key "${if_issue}" '.implementation_failed_defer_state[$key].escalated // false' "${STATE_FILE}" 2>/dev/null || echo "false")"
      [[ "${IF_PREV_COUNT}" =~ ^[0-9]+$ ]] || IF_PREV_COUNT=0
      if [ "${IF_DEFER_SIGNATURE}" = "${IF_PREV_SIGNATURE}" ]; then
        IF_DEFER_COUNT=$((IF_PREV_COUNT + 1))
        IF_ESCALATED_FLAG="${IF_PREV_ESCALATED}"
      else
        IF_DEFER_COUNT=1
        IF_ESCALATED_FLAG="false"
      fi
      IF_SHOULD_ESCALATE="false"
      if [ "${IF_DEFER_COUNT}" -ge "${MAX_IMPL_FAILED_DEFER_CYCLES}" ] && [ "${IF_ESCALATED_FLAG}" != "true" ]; then
        IF_SHOULD_ESCALATE="true"
        IF_ESCALATED_FLAG="true"
      fi
      if jq --arg key "${if_issue}" --arg summary "${IF_DEFER_SIGNATURE}" --argjson count "${IF_DEFER_COUNT}" --argjson escalated "${IF_ESCALATED_FLAG}" '
        .implementation_failed_defer_state //= {}
        | .implementation_failed_defer_state[$key] = {summary: $summary, count: $count, escalated: $escalated}
      ' "${STATE_FILE}" > "${STATE_FILE}.tmp"; then
        mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        IMPL_FAILED_STATE_CHANGED=true
      else
        rm -f "${STATE_FILE}.tmp" 2>/dev/null || true
      fi

      echo "  Deferring implementation-failed reissue for #${if_issue} (${IF_LOCAL_ID}): mode=${IF_MODE}; reason=${IF_DEFER_REASON}; cycle=${IF_DEFER_COUNT}/${MAX_IMPL_FAILED_DEFER_CYCLES}; escalated=${IF_ESCALATED_FLAG}."
      if [ "${IF_SHOULD_ESCALATE}" = "true" ]; then
        ensure_label_exists "ai:needs-human"
        gh_retry gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:needs-human" >/dev/null 2>&1 || true
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${if_issue}/comments" \
          -f body="$(printf '## Post-Codex implementation-failed deferral escalated\n\nThis source issue has been deferred for %s consecutive poll cycles with the same reason (%s). Escalating to `ai:needs-human` for manual review.' "${IF_DEFER_COUNT}" "${IF_DEFER_REASON}")" >/dev/null 2>&1 || true
        tg_notify "Implementation-failed reissue for #${if_issue} (${IF_LOCAL_ID}) escalated to ai:needs-human after ${IF_DEFER_COUNT} deferred cycles."$'\n'"Mode: ${IF_MODE}"$'\n'"Reason: ${IF_DEFER_REASON}"$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "CRITICAL"
      elif [ "${IF_DEFER_SIGNATURE}" != "${IF_PREV_SIGNATURE}" ]; then
        tg_notify "Deferred implementation-failed reissue for #${if_issue} (${IF_LOCAL_ID})."$'\n'"Mode: ${IF_MODE}"$'\n'"Reason: ${IF_DEFER_REASON}"$'\n'"Cycle: ${IF_DEFER_COUNT}/${MAX_IMPL_FAILED_DEFER_CYCLES}"$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "WARNING"
      fi
      continue
    fi

    # Reissue path reached: clear any stale defer-state entry so a
    # later failure starts a fresh dedup/escalation window.
    if jq -e --arg key "${if_issue}" '(.implementation_failed_defer_state // {}) | has($key)' "${STATE_FILE}" >/dev/null 2>&1; then
      if jq --arg key "${if_issue}" '
        if (.implementation_failed_defer_state | type) == "object"
          then .implementation_failed_defer_state |= del(.[$key])
          else .
        end
      ' "${STATE_FILE}" > "${STATE_FILE}.tmp"; then
        mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        IMPL_FAILED_STATE_CHANGED=true
      else
        rm -f "${STATE_FILE}.tmp" 2>/dev/null || true
      fi
    fi

    if [ "${IF_MODE}" = "no-op-implementation" ]; then
      # ---- No-op loop guard: if this task has already been re-issued
      # MAX_IMPL_NOOP_REISSUES times without producing changes, the code
      # likely already exists on main.  Close the issue and let the
      # wave-completion judge verify instead of looping forever.
      NOOP_COUNT="$(get_impl_noop_count "${IF_LOCAL_ID}")"
      # The current failure is itself a no-op, so the observed count
      # includes this cycle even though we haven't bumped yet.
      OBSERVED_NOOP_COUNT=$((NOOP_COUNT + 1))
      # Belt-and-braces: also walk the "Re-issued from #N" ancestor
      # chain on GitHub. This catches the failure mode where the
      # state-based counter is stale — e.g. the tracking-issue state
      # comment was truncated, or the wave iterator never refreshed
      # this task.  That exact failure caused tracking issue #1292 to
      # spawn 30+ duplicate sub-issues for local_id
      # validation-render-self-heal in ~5 hours.  Fails open: on any
      # API error count_noop_ancestors echoes 0 and this guard is a
      # no-op relative to the state-based cap below.
      ANCESTOR_NOOP_COUNT="$(count_noop_ancestors "${if_issue}" "${MAX_IMPL_NOOP_REISSUES}")"
      [[ "${ANCESTOR_NOOP_COUNT}" =~ ^[0-9]+$ ]] || ANCESTOR_NOOP_COUNT=0
      # Cap semantics: MAX_IMPL_NOOP_REISSUES controls how many re-issues
      # are allowed after prior no-op failures. Either signal trips it.
      if [ "${NOOP_COUNT}" -ge "${MAX_IMPL_NOOP_REISSUES}" ] || [ "${ANCESTOR_NOOP_COUNT}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
        echo "  Issue #${if_issue} (${IF_LOCAL_ID}) hit implementation no-op cap (state=${OBSERVED_NOOP_COUNT}, ancestors=${ANCESTOR_NOOP_COUNT}, cap=${MAX_IMPL_NOOP_REISSUES}). Closing as likely already resolved — judge will verify."
        bump_impl_noop_count "${IF_LOCAL_ID}"
        gh_retry gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:done' --remove-label 'ai:implementing' \
          --remove-label 'ai:planning' --remove-label 'ai:clarification' \
          --remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
          --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
          -c "Closing: implementation produced no changes (state-counter=${OBSERVED_NOOP_COUNT}, ancestor-chain=${ANCESTOR_NOOP_COUNT}, cap=${MAX_IMPL_NOOP_REISSUES}). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
        tg_notify "Issue #${if_issue} (${IF_LOCAL_ID}) hit impl no-op cap (state=${OBSERVED_NOOP_COUNT}, ancestors=${ANCESTOR_NOOP_COUNT}). Closed as likely already resolved — judge will verify."$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "WARNING"
        IMPL_FAILED_STATE_CHANGED=true
        continue
      fi

      echo "  Issue #${if_issue} has implementation-failed (no-op state=${OBSERVED_NOOP_COUNT}, ancestors=${ANCESTOR_NOOP_COUNT}, cap=${MAX_IMPL_NOOP_REISSUES}). Closing and re-issuing..."

      # Increment no-op counter before re-issuing
      bump_impl_noop_count "${IF_LOCAL_ID}"
    else
      echo "  Issue #${if_issue} has implementation-failed (${IF_MODE}; blockers resolved). Closing and re-issuing..."
    fi

    # Read the original issue to preserve its content
    IF_TITLE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.title' || echo "")"
    IF_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.body' || echo "")"

    sync_implementation_fixup_blockers "${if_issue}" "${IF_LOCAL_ID}" "${WAVE_IDX}" "${IF_COMMENTS_JSON}" || true
    if [ "${SYNC_IMPLEMENT_FIXUP_BLOCKERS_CHANGED:-false}" = "true" ]; then
      IMPL_FAILED_STATE_CHANGED=true
    fi

    # Close the failed issue
    ensure_label_exists "ai:closed"
    gh_retry gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
      --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
    if [ "${IF_MODE}" = "post-codex-validation" ]; then
      gh_retry gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: implementation failed in post-Codex validation. Blocker fix-up issues are no longer open, so this source task is being re-issued with blocker-sequenced guidance." 2>/dev/null || true
    else
      gh_retry gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: implementation produced no changes. Re-issuing with additional guidance." 2>/dev/null || true
    fi

    # Create replacement issue with extra guidance
    if [ "${IF_MODE}" = "post-codex-validation" ]; then
      IF_BLOCKERS_MD="$(echo "${IF_BLOCKERS_JSON}" | jq -r '.[] | "- #\(.)"')"
      if [ -z "${IF_BLOCKERS_MD}" ]; then
        IF_BLOCKERS_MD="- (none recorded)"
      fi
      NEW_BODY="$(cat <<REISSUE_EOF
${IF_BODY}

---

**⚠️ Re-issued from #${if_issue}** — the previous implementation attempt failed during post-Codex syntax/validation checks.

**Post-Codex blocker context:**
${IF_BLOCKERS_MD}

**Guidance for the implementation model:**
- Review and respect the blocker fix-up outcomes listed above before re-implementing this source task.
- Preserve compatibility with those blocker fixes; do not undo or duplicate them.
- Run syntax/validation checks for changed files before finishing to avoid another post-Codex failure.
- You MUST create or modify files as described in the approved plan. Do NOT only describe changes.
REISSUE_EOF
)"
    else
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
    fi

    ensure_label_exists "ai:clarification"
    ensure_label_exists "ai:orchestrator-managed"
    NEW_ISSUE_URL="$(gh_retry gh issue create --repo "${GITHUB_REPOSITORY}" \
      --title "${IF_TITLE}" \
      --body "${NEW_BODY}" \
      --label "ai:clarification" \
      --label "ai:orchestrator-managed" 2>/dev/null || echo "")"
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
  post_state_comment || true

  # Add labels for any replacement issues created in this cycle.
  REISSUED_NUMS="$(jq -r '.waves['"${WAVE_IDX}"'].issues[].github_issue' "${STATE_FILE}" 2>/dev/null | sort -u)"
  for rnum in ${REISSUED_NUMS}; do
    if [ -z "${rnum}" ] || [ "${rnum}" = "null" ]; then continue; fi
    if echo "${LABELS_JSON}" | jq -e --arg key "${rnum}" 'has($key)' >/dev/null 2>&1; then continue; fi
    LABELS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${rnum}/labels" --jq '[.[].name]' || echo '[]')"
    [ -z "${LABELS}" ] && LABELS='[]'
    LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${rnum}" --argjson labels "${LABELS}" '. + {($key): $labels}' 2>/dev/null || echo "${LABELS_JSON}")"
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
      --enable-stall-human-terminalization "${ENABLE_STALL_HUMAN_TERMINALIZATION}"
    )
    if [ -n "${PHASE_THRESHOLDS_JSON:-}" ]; then
      _stall_check_args+=(--phase-thresholds-json "${PHASE_THRESHOLDS_JSON}")
    fi
    _stall_check_args+=(--max-recoveries-by-phase-json "$(printf '{\"ai:done\":%s}' "${MAX_STALL_RECOVERIES_DONE}")")

    # Extract per-issue linked-PR headPushedAt from the per-tick wave
    # details prefetch (_fetch_candidate_issue_details_graphql already
    # ran upstream at line ~9471 — zero additional API calls per §15).
    # check-stalls re-anchors the ai:done stall clock to
    # max(status_since_ts, headPushedAt_epoch) so a multi-cycle
    # review_autofix loop is not repeatedly flagged as stalled.  Other
    # phases keep their legacy status_since_ts anchor.  Fail-open:
    # missing or empty mapping leaves the legacy behaviour intact.
    _head_pushed_at_json='{}'
    if [ -n "${_current_wave_details_json:-}" ] && [ "${_current_wave_details_json}" != "{}" ]; then
      if ! _head_pushed_at_json="$(printf '%s' "${_current_wave_details_json}" | jq -c '
        to_entries
        | map(
            select(
              .value.linked_pr != null
              and (.value.linked_pr | type == "object")
              and .value.linked_pr.headPushedAt != null
              and (.value.linked_pr.headPushedAt | type == "string")
              and (.value.linked_pr.headPushedAt | length > 0)
            )
            | {key: .key, value: .value.linked_pr.headPushedAt}
          )
        | from_entries
      ' 2>/dev/null)"; then
        echo "::warning::headPushedAt extraction failed during stall check; using empty fallback mapping." >&2
        _head_pushed_at_json='{}'
      fi
      [ -n "${_head_pushed_at_json}" ] || _head_pushed_at_json='{}'
      if [ "${_head_pushed_at_json}" = "{}" ]; then
        _head_pushed_at_candidate_count="$(printf '%s' "${_current_wave_details_json}" | jq -r '[to_entries[] | select(.value.linked_pr != null and (.value.linked_pr | type == "object"))] | length' 2>/dev/null || printf '')"
        if [[ "${_head_pushed_at_candidate_count}" =~ ^[0-9]+$ ]] && [ "${_head_pushed_at_candidate_count}" -gt 0 ]; then
          echo "::warning::headPushedAt extraction produced an empty mapping for ${_head_pushed_at_candidate_count} linked PR candidate(s); using legacy stall-clock fallback." >&2
        fi
      fi
    fi
    # Layer-2 branch fallback: the re-anchor map above is built solely from
    # the issue→PR cross-reference timeline in _current_wave_details_json —
    # the same brittle source the Layer-1 fresh-push guard uses.  When that
    # cross-reference is transiently empty or malformed for an ai:done issue,
    # its headPushedAt is unusable and detect_stalls falls back to the
    # status_since-only clock, re-flagging a PR that was just pushed.  Re-
    # resolve those (and only those) issues by their deterministic
    # ai/issue-<n> head branch so the re-anchor is not blinded in lock-step
    # with the fresh-push guard while preserving the intentionally narrow
    # ai:done-only re-anchor scope.  Bounded by design: fires only for ai:done
    # issues whose primary headPushedAt is missing, empty, or unparseable,
    # which on the happy path (cross-reference present and parseable) is zero,
    # so this adds 0 API calls per tick in steady state (§15).  Fail-open: any
    # resolution failure leaves the legacy status_since anchor in place for
    # that issue.
    if [ -n "${_current_wave_details_json:-}" ] && [ "${_current_wave_details_json}" != "{}" ]; then
      _reanchor_fallback_issues="$(printf '%s' "${_current_wave_details_json}" | jq -r '
        to_entries[]
        | select((.value.labels // []) | any(. == "ai:done"))
        | .value.linked_pr as $linked_pr
        | select(
            if ($linked_pr == null) then
              true
            elif (($linked_pr | type) != "object") then
              true
            elif ($linked_pr.headPushedAt == null) then
              true
            elif (($linked_pr.headPushedAt | type) != "string") then
              true
            elif (($linked_pr.headPushedAt | length) == 0) then
              true
            else
              ((try ($linked_pr.headPushedAt | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch null) == null)
            end
          )
        | .key
      ' 2>/dev/null || true)"
      if [ -n "${_reanchor_fallback_issues}" ]; then
        while IFS= read -r _rf_issue; do
          [[ "${_rf_issue}" =~ ^[0-9]+$ ]] || continue
          _rf_entry="$(_resolve_linked_pr_fresh_by_branch "${_rf_issue}")"
          [ -n "${_rf_entry}" ] || continue
          _rf_iso="$(printf '%s' "${_rf_entry}" | jq -r '.headPushedAt // empty' 2>/dev/null || echo "")"
          [ -n "${_rf_iso}" ] || continue
          # Always-visible (not ::debug::) so a recovered tick leaves a trace
          # even though the issue is no longer flagged and the Layer-1
          # STALL_FRESH_PUSH_FALLBACK line therefore never fires for it.
          echo "STALL_REANCHOR_FALLBACK issue=${_rf_issue} source=branch_name resolved=${_rf_iso}"
          _head_pushed_at_json="$(printf '%s' "${_head_pushed_at_json}" | jq -c --arg k "${_rf_issue}" --arg v "${_rf_iso}" '. + {($k): $v}' 2>/dev/null || printf '%s' "${_head_pushed_at_json}")"
        done <<< "${_reanchor_fallback_issues}"
      fi
    fi
    _stall_check_args+=(--head-pushed-at-json "${_head_pushed_at_json}")

    STALLS_JSON="$(python3 scripts/orchestrate_lib.py check-stalls \
      "${_stall_check_args[@]}" 2>/dev/null || echo '{"ok":false,"stalls":[],"count":0}')"

    STALL_COUNT="$(echo "${STALLS_JSON}" | jq -r '.count')"
    [[ "${STALL_COUNT}" =~ ^[0-9]+$ ]] || STALL_COUNT=0

    STALL_STATE_CHANGED=false
    STALL_HEALING_CHANGED=false

    if [ "${STALL_COUNT}" -gt 0 ]; then
      echo "Detected ${STALL_COUNT} stalled issue(s). Checking for active workflow runs..."

      # Prime the shared actions-runs blob in the parent shell before the
      # command-substitution call below; otherwise the loader's global cache
      # assignments stay trapped in the subshell and the diagnostic branch
      # cannot inspect the same payload.
      _load_actions_runs_cached >/dev/null || true

      # Build the set of issues with active workflows (reusing the one API
      # call batch primed above to avoid per-issue API calls).
      ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"
      if [ -n "${ACTIVE_WORKFLOW_ISSUES}" ]; then
        echo "Issues with active workflow runs: $(echo "${ACTIVE_WORKFLOW_ISSUES}" | tr '\n' ' ')"
      else
        # Diagnostic: when one or more issues stalled but the active set
        # came back empty, emit cache provenance so a stall recovery
        # that fires over an actually in_progress workflow can be traced
        # back to cache state (304-reuse of empty cached_runs, fresh
        # cache hit on empty data, head_branch=null on workflow_dispatch
        # extraction, or API-failure fallback in _load_actions_runs_cached).
        # Reads directly from the per-tick memoised
        # _ACTIONS_RUNS_BLOB_CACHE primed just above, so
        # this adds zero API calls (§15).  Fails open on jq errors:
        # counts fall back to "?" and the diagnostic still prints,
        # but parse_error=true keeps that path distinguishable from a
        # genuinely empty cache.  The execute_stall_recovery_action(
        # retrigger_review) defense-in-depth in-flight review guard
        # still protects the
        # empty-commit push if the cache misses a live review run; this
        # logging just makes the cache state observable next time.
        (
          _diag_blob="${_ACTIONS_RUNS_BLOB_CACHE}"
          _diag_parse_error="false"
          if [ -z "${_diag_blob}" ]; then
            _diag_blob='{"workflow_runs":[]}'
            _diag_parse_error="true"
          fi
          _diag_total="$(printf '%s' "${_diag_blob}" | jq -r 'if (.workflow_runs | type) == "array" then (.workflow_runs | length) else error("workflow_runs_missing_or_not_array") end' 2>/dev/null)" || { _diag_total="?"; _diag_parse_error="true"; }
          _diag_in_progress="$(printf '%s' "${_diag_blob}" | jq -r 'if (.workflow_runs | type) == "array" then [.workflow_runs[] | select((.status // "") == "in_progress")] | length else error("workflow_runs_missing_or_not_array") end' 2>/dev/null)" || { _diag_in_progress="?"; _diag_parse_error="true"; }
          _diag_queued="$(printf '%s' "${_diag_blob}" | jq -r 'if (.workflow_runs | type) == "array" then [.workflow_runs[] | select((.status // "") == "queued")] | length else error("workflow_runs_missing_or_not_array") end' 2>/dev/null)" || { _diag_queued="?"; _diag_parse_error="true"; }
          if [ "${_diag_parse_error}" = "true" ]; then
            echo "Active issue set is empty (cache: total=${_diag_total}, in_progress=${_diag_in_progress}, queued=${_diag_queued}, parse_error=true)."
          else
            echo "Active issue set is empty (cache: total=${_diag_total}, in_progress=${_diag_in_progress}, queued=${_diag_queued})."
          fi
        )
      fi

      # Prefetch linked-PR state for every stalled issue in batched
      # GraphQL calls, so recover_stalled_issue's merged-PR and open-PR
      # sub-guards both consume the data from an in-memory cache
      # instead of hitting the GitHub API per issue.  The prefetch is
      # intentionally NOT gated on ENABLE_STALL_MERGED_PR_GUARD: the
      # open-PR sub-guard is pre-existing and always benefits from the
      # batched lookup, so we always populate the cache whenever there
      # are stalled issues to inspect.  When ENABLE_STALL_MERGED_PR_GUARD
      # is false, _check_merged_pr_guard itself short-circuits (see its
      # feature-flag check), but the cache still serves the open-PR
      # sub-guard.  Fails open: an empty/failed prefetch leaves the
      # legacy per-issue REST lookup in place as a fallback.
      # _fetch_linked_pr_status_graphql batches at batch_size=25 so
      # larger stall sets produce multiple calls.
      STALL_MANAGED_LINKED_PR_CACHE='{}'
      _stall_issue_nums_json="$(echo "${STALLS_JSON}" | jq -c '[.stalls[].github_issue | select(type == "number")] | unique')"
      _stall_issue_count="$(printf '%s' "${_stall_issue_nums_json}" | jq 'length' 2>/dev/null || echo 0)"
      if [ "${_stall_issue_count}" -gt 0 ]; then
        STALL_MANAGED_LINKED_PR_CACHE="$(_fetch_linked_pr_status_graphql "${_stall_issue_nums_json}")"
        _stall_graphql_batches="$(( ( _stall_issue_count + 24 ) / 25 ))"
        _stall_graphql_call_label="calls"
        if [ "${_stall_graphql_batches}" -eq 1 ]; then
          _stall_graphql_call_label="call"
        fi
        echo "Prefetched linked-PR state for $(printf '%s' "${STALL_MANAGED_LINKED_PR_CACHE}" | jq 'length' 2>/dev/null || echo 0) stalled issue(s) (${_stall_graphql_batches} batched GraphQL ${_stall_graphql_call_label})."
      fi
      export STALL_MANAGED_LINKED_PR_CACHE

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

phase = '${STALL_PHASE}'
increment_stall_recovery(state, '${STALL_LOCAL_ID}', phase if phase else None)

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

    if [ "${STALL_STATE_CHANGED}" = "true" ] || [ "${STALL_HEALING_CHANGED}" = "true" ] || [ "${TIMESTAMP_STATE_CHANGED}" = "true" ] || [ "${RECONCILE_STATE_CHANGED}" = "true" ] || [ "${RECONCILE_LABELS_CHANGED}" = "true" ] || [ "${TRACKING_BODY_SYNC_STATE_CHANGED:-false}" = "true" ] || [ "${COMPLETION_STATUS_STATE_CHANGED:-false}" = "true" ]; then
      post_state_comment || true
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

  SKIP_JUDGE_FOR_CLEAN_WAVE="false"
  if [ "${ENABLE_CLEAN_WAVE_JUDGE_SKIP}" = "true" ] \
    && [ "${INVOKE_JUDGE_FOR_STUCK}" != "true" ] \
    && [ "${WAVE_COMPLETE}" = "true" ] \
    && [ "${ANY_FAILED}" != "true" ] \
    && [ "${PROJECT_COMPLETE}" != "true" ] \
    && [ "${CURRENT_WAVE}" -lt "${TOTAL_WAVES}" ]; then
    SKIP_JUDGE_FOR_CLEAN_WAVE="true"
    JUDGE_STATUS="in_progress"
    JUDGE_JUSTIFICATION="clean_wave_skip_enabled"
    JUDGE_ASSESSMENT="Clean wave completed with deferred future-wave issue definitions; advancing without judge invocation."
    NEW_ISSUES_COUNT=0
    REVERT_COUNT=0
    JUDGE_JSON='{"status":"in_progress","justification":"clean_wave_skip_enabled","assessment":"Clean wave completed with deferred future-wave issue definitions; advancing without judge invocation.","new_issues":[],"issues_to_revert":[]}'
    echo "Clean wave skip eligible on wave ${CURRENT_WAVE}; advancing to next wave without judge invocation."
  fi

  # ---------------------------------------------------------------
  # Fast-path: skip LLM judge for clean project completions.
  # When all waves are complete, no failures occurred, and no stuck
  # issues exist, the verdict is deterministic ("complete") — running
  # the LLM judge wastes tokens and risks empty-output failures.
  # Gated by the same ENABLE_CLEAN_WAVE_JUDGE_SKIP flag.
  # ---------------------------------------------------------------
  if [ "${SKIP_JUDGE_FOR_CLEAN_WAVE}" != "true" ] \
    && [ "${ENABLE_CLEAN_WAVE_JUDGE_SKIP}" = "true" ] \
    && [ "${INVOKE_JUDGE_FOR_STUCK}" != "true" ] \
    && [ "${WAVE_COMPLETE}" = "true" ] \
    && [ "${ANY_FAILED}" != "true" ] \
    && [ "${ANY_REVIEW_BLOCKED}" != "true" ] \
    && [ "${PROJECT_COMPLETE}" = "true" ]; then
    SKIP_JUDGE_FOR_CLEAN_WAVE="true"
    JUDGE_STATUS="complete"
    JUDGE_JUSTIFICATION="clean_project_completion_skip"
    JUDGE_ASSESSMENT="All ${TOTAL_WAVES} wave(s) completed with every issue merged and no failures. Skipping LLM judge — verdict is deterministic."
    NEW_ISSUES_COUNT=0
    REVERT_COUNT=0
    JUDGE_JSON='{"status":"complete","justification":"clean_project_completion_skip","assessment":"All waves completed with every issue merged and no failures. Skipping LLM judge — verdict is deterministic.","new_issues":[],"issues_to_revert":[]}'
    echo "Clean project completion on wave ${CURRENT_WAVE}/${TOTAL_WAVES}; finalizing without judge invocation."
  fi

  if [ "${SKIP_JUDGE_FOR_CLEAN_WAVE}" != "true" ]; then

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
  # Final-PR-phase cap bypass (Orchestrator PR autofix flow contract):
  # When the integration→default-branch PR is open and pending merge,
  # the judge stall cycle cap is bypassed so the final PR can run
  # unlimited 3-autofix→judge→3-autofix→… cycles until it is mergeable.
  # The cap remains in force for everything else (intermediate-PR
  # phase, sub-issue stalls, recovery loops) to preserve the existing
  # infinite-loop guarantee.  Set ORCH_PR_AUTOFIX_FLOW_ENABLED=false
  # (repo var) to revert to the legacy uniform-cap behavior.
  ORCH_FLOW_ENABLED_FOR_CAP="${ORCH_PR_AUTOFIX_FLOW_ENABLED:-true}"
  FINAL_PR_PHASE_CAP_BYPASS="false"
  if [ "${ORCH_FLOW_ENABLED_FOR_CAP}" = "true" ]; then
    # Single jq invocation reads both fields and emits them joined by ":"
    # (neither field can contain ":" — final_merge_pr is a JSON number,
    # final_merge_status is one of pending|merged|failed|conflict|
    # superseded-by-main).  Bash parameter expansion splits the result.
    _final_pr_data="$(jq -r '[.final_merge_pr // "", .final_merge_status // "pending"] | join(":")' "${STATE_FILE}" 2>/dev/null || echo ":pending")"
    _final_pr_for_cap="${_final_pr_data%%:*}"
    _final_pr_status_for_cap="${_final_pr_data#*:}"
    if [ -n "${_final_pr_for_cap}" ] && [ "${_final_pr_for_cap}" != "null" ] && [ "${_final_pr_status_for_cap}" = "pending" ]; then
      FINAL_PR_PHASE_CAP_BYPASS="true"
    fi
  fi
  if [ "${FINAL_PR_PHASE_CAP_BYPASS}" = "true" ] && [ "$((JUDGE_STALL_CYCLES + 1))" -gt "${MAX_JUDGE}" ]; then
    echo "[final-merge] judge cap bypassed (final-PR loop active: PR #${_final_pr_for_cap}, status=${_final_pr_status_for_cap}); JUDGE_STALL_CYCLES=$((JUDGE_STALL_CYCLES + 1)) > MAX_JUDGE=${MAX_JUDGE}, proceeding to judge invocation."
  fi
  if [ "${FINAL_PR_PHASE_CAP_BYPASS}" != "true" ] && [ "$((JUDGE_STALL_CYCLES + 1))" -gt "${MAX_JUDGE}" ]; then
    echo "::error::Judge stall cycle limit reached ($((JUDGE_STALL_CYCLES + 1)) > ${MAX_JUDGE}). Marking project as failed."
    jq '.status = "failed"' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment || true
    handle_comprehensive_release_callback_if_needed "failed" "${TRACKING_LABELS}" "${COMMENTS:-[]}"
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -f body="## Project Failed — Judge stall cycle limit exceeded

Judge has used ${JUDGE_STALL_CYCLES} stall cycle(s) (recovery/fix-ups) out of ${MAX_JUDGE} allowed (total judge evaluations: ${JUDGE_CYCLE}).
Clean wave advances do not count against this limit.
Manual intervention required." >/dev/null
    set_failed_completion_status_comment \
      "Judge stall cycle limit exceeded (${JUDGE_STALL_CYCLES}/${MAX_JUDGE}). Manual intervention required. See the \"Project Failed — Judge stall cycle limit exceeded\" comment for the diagnostic detail."
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
  echo "Judge reasoning effort for cycle ${JUDGE_INVOCATION_CYCLE}: ${MODEL_REASONING_EFFORT_JUDGE:-xhigh}"
  # Centralised in scripts/write_codex_config.sh.
  bash scripts/write_codex_config.sh \
    --model "${MODEL_EDITOR}" \
    --reasoning "${MODEL_REASONING_EFFORT_JUDGE:-xhigh}"

  if ! prepare_tracking_judge_checkout "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}"; then
    continue
  fi

  # Collect PR diffs for context, split by issue status so the
  # byte-stable "merged PR diffs" block can sit inside the cacheable
  # prefix of the judge prompt (before the volatile WAVE STATUS).
  #
  # Correctness notes:
  #   - Status is read from STATE_FILE (.waves[WAVE_IDX].issues[].status),
  #     which is authoritative and set to "merged" by the judge merge-
  #     recording logic at L4405. No extra GH API call needed.
  #   - Issue numbers are sorted before iteration so the concatenation
  #     order is deterministic across ticks even after orphan-sweep
  #     appends (L4601) or deferred issue creation.
  #   - Merged PR diffs are byte-stable for a given head SHA: the
  #     `gh api ... Accept: vnd.github.diff` endpoint returns the same
  #     bytes on every request. Once all PRs in the current wave are
  #     merged, this block is stable across ticks and extends the
  #     cacheable prefix by the sum of the diffs (typically 5-15 K
  #     tokens for mature projects).
  MERGED_PR_SUMMARIES=""
  OPEN_PR_SUMMARIES=""
  _sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"
  for inum in ${_sorted_issue_nums}; do
    [ -n "${inum}" ] || continue
    PR_NUM="$(_issue_cross_ref_pr_number_last "${inum}" 2>/dev/null || echo "")"
    if [[ "${PR_NUM}" =~ ^[0-9]+$ ]]; then
      # Fetch the diff into a temp file before truncating: piping
      # gh api directly into `head -500` causes SIGPIPE on gh api once
      # head has read enough lines, which gh_retry then treats as a
      # transient failure and retries with exponential backoff.
      _pr_diff_tmp="$(mktemp)"
      if gh_retry_to_file "${_pr_diff_tmp}" gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUM}" \
        -H 'Accept: application/vnd.github.diff'; then
        PR_DIFF="$(head -n 500 "${_pr_diff_tmp}" 2>/dev/null || echo "(diff unavailable)")"
      else
        echo "::warning::Failed to fetch PR #${PR_NUM} diff for judge context (issue #${inum}); falling back to '(diff unavailable)'." >&2
        PR_DIFF="(diff unavailable)"
      fi
      rm -f "${_pr_diff_tmp}"
      unset _pr_diff_tmp
      _issue_status="$(jq -r --arg num "${inum}" --argjson wi "${WAVE_IDX}" \
        '.waves[$wi].issues[] | select((.github_issue | tostring) == $num) | .status // ""' \
        "${STATE_FILE}" 2>/dev/null | head -n1)"
      if [ "${_issue_status}" = "merged" ]; then
        MERGED_PR_SUMMARIES+="
--- PR #${PR_NUM} (Issue #${inum}) ---
${PR_DIFF}

"
      else
        OPEN_PR_SUMMARIES+="
--- PR #${PR_NUM} (Issue #${inum}, status=${_issue_status:-unknown}) ---
${PR_DIFF}

"
      fi
    fi
  done
  unset _sorted_issue_nums _issue_status

  # Fetch CI status on default branch
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  CI_STATUS="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/commits/${DEFAULT_BRANCH}/check-runs" \
    --jq '[.check_runs[] | {name: .name, conclusion: .conclusion}]' || echo "[]")"

  # Get original project description. Prefer the byte-stable snapshot
  # captured at project creation in orchestrate_lib.build_tracking_state
  # (state field .project_body_snapshot). Fall back to fetching the live
  # tracking issue body for projects created before this field existed.
  # Using the snapshot keeps the judge-prompt prefix byte-stable across
  # ticks so provider-side prompt caching stays effective, and removes
  # one GH API call per judge tick.
  PROJECT_BODY="$(jq -r '.project_body_snapshot // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [ -z "${PROJECT_BODY}" ]; then
    PROJECT_BODY="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" --jq '.body' || echo "")"
  fi

  JUDGE_VALIDATION_RAW_STATUS="$(jq -r '.validation_last_raw_status // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  JUDGE_VALIDATION_FAILURE_CLASS="$(jq -r '.validation_failure_class // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  JUDGE_VALIDATION_FAILURE_REASON="$(jq -r '.validation_failure_reason // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  JUDGE_VALIDATION_REASON_SUMMARY="$(validation_reason_one_line "${JUDGE_VALIDATION_FAILURE_REASON}")"
  if has_label "${TRACKING_LABELS}" "ai:harness-broken"; then
    JUDGE_HARNESS_BROKEN_PRESENT="true"
  else
    JUDGE_HARNESS_BROKEN_PRESENT="false"
  fi

  JUDGE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/judge_semble_query.txt"
  JUDGE_SEMBLE_PREFETCH=""
  {
    printf '%s\n' 'Project judge context.'
    append_judge_semble_query_text "Project spec:" "${PROJECT_BODY}" 3000
    append_judge_semble_query_text "Merged PR summaries:" "${MERGED_PR_SUMMARIES}" 3500
    append_judge_semble_query_text "Open PR summaries:" "${OPEN_PR_SUMMARIES}" 3500
    append_judge_semble_query_text "Wave status JSON:" "${WAVE_STATUS}" 2500
    append_judge_semble_query_text "CI status JSON:" "${CI_STATUS}" 1500
  } > "${JUDGE_SEMBLE_QUERY_FILE}"
  JUDGE_SEMBLE_PREFETCH="$(render_judge_semble_prefetch_from_query_file "${JUDGE_SEMBLE_QUERY_FILE}" "Judge Context")"

  # Build one stable static prefix per run for provider-side prompt caching.
  assemble_judge_static_context "${RUNTIME_DIR}/judge_static.txt"

  # Build judge prompt
  {
    cat "${RUNTIME_DIR}/judge_static.txt"
    echo
    echo "=== JUDGE TASK ==="
    echo
    SEMBLE_PREFETCH="${JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge.txt
    echo
    echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
    echo
    echo "=== PROJECT SPEC ==="
    echo
    echo "${PROJECT_BODY}"
    echo
    # Merged PR diffs sit here — BEFORE the volatile WAVE STATUS block —
    # so the provider-side prompt prefix cache can span them. The block
    # is byte-stable across consecutive ticks within a wave because:
    #   (1) merged commit diffs are immutable by head SHA,
    #   (2) issue numbers are sorted before iteration,
    #   (3) no new merged PRs appear until the next wave starts.
    # Open/in-flight PR diffs are moved to the volatile tail because
    # they change whenever the PR branch advances.
    echo "=== MERGED PR DIFFS (truncated; cache-stable) ==="
    echo
    if [ -n "${MERGED_PR_SUMMARIES}" ]; then
      echo "${MERGED_PR_SUMMARIES}"
    else
      echo "(no merged PRs in current wave yet)"
    fi
    echo
    echo "=== WAVE ${CURRENT_WAVE} COMPLETION STATUS ==="
    echo
    echo "${WAVE_STATUS}" | jq '.'
    echo
    echo "=== OPEN PR DIFFS (truncated; in-flight) ==="
    echo
    if [ -n "${OPEN_PR_SUMMARIES}" ]; then
      echo "${OPEN_PR_SUMMARIES}"
    else
      echo "(no open PRs in current wave)"
    fi
    echo
    echo "=== CI STATUS ON ${DEFAULT_BRANCH} ==="
    echo
    echo "${CI_STATUS}" | jq '.'
    echo
    echo "=== ORCHESTRATOR STATE ==="
    echo
    echo "Judge checkout source: ${JUDGE_EXECUTION_SOURCE}"
    echo "Judge checkout ref: ${JUDGE_EXECUTION_REF}"
    echo "Judge context sentinel present: ${JUDGE_CONTEXT_SENTINEL_PRESENT}"
    if [ "${JUDGE_CONTEXT_SENTINEL_PRESENT}" = "true" ] && [ -n "${JUDGE_CONTEXT_SENTINEL_VALUE}" ]; then
      echo "Judge context sentinel value: ${JUDGE_CONTEXT_SENTINEL_VALUE}"
    fi
    echo "Judge cycle: $((JUDGE_CYCLE + 1))"
    echo "Current wave just completed: ${CURRENT_WAVE} of ${TOTAL_WAVES}"
    echo "Project complete (all waves dispatched and merged): ${PROJECT_COMPLETE}"
    echo "Recovery count: ${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}"
    echo "Latest validation raw status: ${JUDGE_VALIDATION_RAW_STATUS:-unknown}"
    echo "Latest validation failure class: ${JUDGE_VALIDATION_FAILURE_CLASS:-none}"
    echo "Harness-broken label present: ${JUDGE_HARNESS_BROKEN_PRESENT}"
    if [ -n "${JUDGE_VALIDATION_REASON_SUMMARY}" ]; then
      echo "Latest validation summary: ${JUDGE_VALIDATION_REASON_SUMMARY}"
    fi
    if [ "${JUDGE_VALIDATION_RAW_STATUS}" = "harness_error" ] || [ "${JUDGE_HARNESS_BROKEN_PRESENT}" = "true" ]; then
      echo "Judge note: the latest validation failure is classified as a harness/infrastructure defect unless repository evidence proves merged product code caused it."
    fi
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
  rm -f "${JUDGE_SEMBLE_QUERY_FILE}"

  # Run judge via Codex
  JUDGE_SUCCESS=false
  JUDGE_JSON=""
  judge_silent_rounds=0
  max_attempts=2
  if nag_reminder_enabled; then
    judge_nag_attempt_limit="$(nag_silent_round_threshold)"
    if [ "${judge_nag_attempt_limit}" -gt "${max_attempts}" ]; then
      max_attempts="${judge_nag_attempt_limit}"
    fi
  fi
  for attempt in $(seq 1 "${max_attempts}"); do
    judge_attempt_prompt_file="${JUDGE_PROMPT_FILE}.attempt_${attempt}"
    judge_effective_prompt_file="${JUDGE_PROMPT_FILE}"
    if cp "${JUDGE_PROMPT_FILE}" "${judge_attempt_prompt_file}" 2>/dev/null; then
      judge_effective_prompt_file="${judge_attempt_prompt_file}"
    else
      echo "::warning::Could not create per-attempt judge prompt file for attempt ${attempt}; continuing with the base prompt." >&2
    fi
    # Prompt assembly happens before the current judge turn runs, so feed the
    # projected consecutive-silent count for the attempt we are about to
    # launch.
    judge_nag_counter_for_attempt=$((judge_silent_rounds + 1))
    judge_nag_block="$(maybe_inject_nag "orchestrate-poll-judge" "${judge_nag_counter_for_attempt}")"
    if [ -n "${judge_nag_block}" ]; then
      if [ "${judge_effective_prompt_file}" = "${judge_attempt_prompt_file}" ]; then
        printf '\n%s\n' "${judge_nag_block}" >> "${judge_effective_prompt_file}"
        judge_silent_rounds=0
      fi
    fi
    echo "Judge attempt ${attempt}/${max_attempts}..."
    sanitize_codex_prompt_file "${judge_effective_prompt_file}"
    # The pipeline may return 141 (SIGPIPE) when the prompt is larger
    # than the OS pipe buffer and codex closes stdin before cat finishes.
    # This is harmless — check the output file regardless of exit code.
    cat "${judge_effective_prompt_file}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access > "${JUDGE_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/judge_log.txt" >&2) || true
    rm -f "${judge_attempt_prompt_file}"
    judge_json_candidate="$(extract_judge_json_with_status "${JUDGE_OUTPUT_FILE}")"
    if [ -n "${judge_json_candidate}" ]; then
      JUDGE_JSON="${judge_json_candidate}"
      JUDGE_SUCCESS=true
      judge_silent_rounds=0
      break
    fi
    judge_silent_rounds=$((judge_silent_rounds + 1))
    if [ "${attempt}" -lt "${max_attempts}" ]; then
      sleep $(( 10 * attempt + RANDOM % 10 ))
    fi
  done

	  if [ "${JUDGE_SUCCESS}" != "true" ]; then
	    echo "::error::Judge failed for tracking issue #${TRACKING_NUM}"
	    tg_notify "Orchestrator Judge failed for #${TRACKING_NUM}. Manual review needed." "CRITICAL"
	    continue
	  fi
	  archive_transcript "${GITHUB_RUN_ID:-local-run}" "judge" "${JUDGE_OUTPUT_FILE}"

	  # ---------------------------------------------------------------
	  # Parse judge output
  # ---------------------------------------------------------------
  if [ -z "${JUDGE_JSON}" ]; then
    echo "::error::Could not parse judge output for #${TRACKING_NUM}"
    tg_notify "Orchestrator Judge output unparseable for #${TRACKING_NUM}. Manual review needed." "CRITICAL"
    continue
  fi

  emit_judge_lessons_learned_records "orchestrate_judge" "${TRACKING_NUM}" "" "${JUDGE_JSON}"

  JUDGE_STATUS="$(echo "${JUDGE_JSON}" | jq -r '.status')"
  JUDGE_JUSTIFICATION_RAW="$(echo "${JUDGE_JSON}" | jq -r '.justification // ""')"
  if [ -n "${JUDGE_JUSTIFICATION_RAW}" ]; then
    JUDGE_JUSTIFICATION="${JUDGE_JUSTIFICATION_RAW}"
  else
    JUDGE_JUSTIFICATION="no justification provided"
  fi
  JUDGE_ASSESSMENT="$(echo "${JUDGE_JSON}" | jq -r '.assessment // ""')"
  NEW_ISSUES_COUNT="$(echo "${JUDGE_JSON}" | jq '.new_issues | length')"
  REVERT_COUNT="$(echo "${JUDGE_JSON}" | jq '.issues_to_revert | length')"

  JUDGE_JUSTIFICATION_NORM="$(normalize_judge_justification_for_fingerprint "${JUDGE_JUSTIFICATION_RAW}")"
  if [ -n "${JUDGE_JUSTIFICATION_NORM}" ]; then
    JUDGE_FINGERPRINT="$(judge_justification_fingerprint "${JUDGE_JUSTIFICATION_NORM}" 2>/dev/null || true)"
  else
    JUDGE_FINGERPRINT=""
  fi
  PREV_JUDGE_FINGERPRINT="$(jq -r '.judge_last_fingerprint // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  PREV_JUDGE_FINGERPRINT_REPEAT_COUNT="$(jq -r '.judge_fingerprint_repeat_count // 0' "${STATE_FILE}" 2>/dev/null || echo "0")"
  if ! [[ "${PREV_JUDGE_FINGERPRINT_REPEAT_COUNT}" =~ ^[0-9]+$ ]]; then
    PREV_JUDGE_FINGERPRINT_REPEAT_COUNT="0"
  fi
  LAST_VALIDATION_RAW_STATUS_FOR_JUDGE="$(jq -r '.validation_last_raw_status // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [ "${LAST_VALIDATION_RAW_STATUS_FOR_JUDGE}" = "harness_error" ]; then
    JUDGE_FINGERPRINT=""
    JUDGE_FINGERPRINT_REPEAT_COUNT=0
  elif [ -z "${JUDGE_FINGERPRINT}" ]; then
    JUDGE_FINGERPRINT_REPEAT_COUNT=0
  elif [ "${JUDGE_FINGERPRINT}" = "${PREV_JUDGE_FINGERPRINT}" ]; then
    JUDGE_FINGERPRINT_REPEAT_COUNT=$(( PREV_JUDGE_FINGERPRINT_REPEAT_COUNT + 1 ))
  else
    JUDGE_FINGERPRINT_REPEAT_COUNT=1
  fi
  jq --arg fp "${JUDGE_FINGERPRINT}" --argjson repeat_count "${JUDGE_FINGERPRINT_REPEAT_COUNT}" \
    '.judge_last_fingerprint = $fp | .judge_fingerprint_repeat_count = $repeat_count' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

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

  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    -f body="${JUDGE_COMMENT}" >/dev/null

  # ---------------------------------------------------------------
  # Hard guard: judge cannot declare "complete" while waves remain
  # pending. Integration drift (last wave merged but the integration
  # branch is ahead of default) is intentionally NOT covered here —
  # the complete) arm below calls finalize_integration_merge_if_needed,
  # whose ahead_by re-check is the designed recovery path for that
  # case. Folding
  # integration_contained_in_default into this guard (via the
  # PROJECT_COMPLETE flag added in PR #2778) creates a deadlock: the
  # override flips the verdict before the complete) arm can run, so
  # the drift never gets resolved, so the override fires again on the
  # next poll. See bitsafe.io#325 for the regression.
  if [ "${JUDGE_STATUS}" = "complete" ] \
     && { [ "${WAVE_COMPLETE}" != "true" ] || [ "${CURRENT_WAVE}" -lt "${TOTAL_WAVES}" ]; }; then
    echo "::warning::Judge returned 'complete' but project still has pending wave state (wave_complete=${WAVE_COMPLETE}; wave=${CURRENT_WAVE}/${TOTAL_WAVES}; project_complete=${PROJECT_COMPLETE} logged for context). Overriding to 'in_progress'."
    JUDGE_STATUS="in_progress"
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -f body="⚠️ Judge verdict overridden: \`complete\` → \`in_progress\` because the project still has pending wave state (wave_complete=${WAVE_COMPLETE}, wave=${CURRENT_WAVE}/${TOTAL_WAVES}). Waiting for remaining wave work before allowing project completion." >/dev/null
  fi
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
        post_state_comment || true
        handle_comprehensive_release_callback_if_needed "complete" "${TRACKING_LABELS}" "${COMMENTS:-[]}"

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

      post_tracking_comment "## ✅ Judge declared project complete — cycle $((JUDGE_CYCLE + 1))

**Reason:** ${JUDGE_JUSTIFICATION}

All waves have merged and the judge is satisfied. Transitioning to runtime validation (cycle ${VALIDATION_CYCLE}) to confirm correctness before closing."

      jq --argjson cycle "${VALIDATION_CYCLE}" \
        '.status = "validating" |
         .judge_cycle += 1 |
         .validation_cycle = $cycle |
         .validation_active_fix_issues = [] |
         .validation_fix_issues_batch_cycles = 0 |
         .validation_seen_fix_issues = (.validation_seen_fix_issues // []) |
         .validation_last_fix_comment_id = (.validation_last_fix_comment_id // 0) |
         .validation_last_dispatch_cycle = 0 |
         .validation_failure_reason = null |
         .validation_failure_class = null |
         .validation_last_raw_status = null |
         .validation_completed_cycle = null' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment || true
      set_tracking_phase_label "ai:validating"

      if ! dispatch_validation_if_needed "${VALIDATION_CYCLE}"; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write. Error: ${VALIDATION_DISPATCH_ERROR:-unknown}"
      fi
      ;;

    failed)
      echo "Judge declared failure."

      if [ "${JUDGE_FINGERPRINT_REPEAT_COUNT}" -gt "${JUDGE_REPEAT_FINGERPRINT_MAX}" ]; then
        echo "Judge repeat fingerprint cap exceeded (${JUDGE_FINGERPRINT_REPEAT_COUNT}/${JUDGE_REPEAT_FINGERPRINT_MAX}). Escalating to ai:blocked."

        jq '.status = "failed" | .judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        post_state_comment || true
        handle_comprehensive_release_callback_if_needed "failed" "${TRACKING_LABELS}" "${COMMENTS:-[]}"
        set_tracking_phase_label "ai:blocked"
        post_tracking_comment "## ❌ Judge repeat-fingerprint breaker triggered

The judge produced the same normalized failure fingerprint for ${JUDGE_FINGERPRINT_REPEAT_COUNT} consecutive cycle(s), exceeding JUDGE_REPEAT_FINGERPRINT_MAX=${JUDGE_REPEAT_FINGERPRINT_MAX}.

- Fingerprint: \`${JUDGE_FINGERPRINT}\`
- Normalized justification: ${JUDGE_JUSTIFICATION_NORM:-<empty>}

To avoid repeating the same recovery loop, the orchestrator is not creating additional fix-up issues or running another judge-driven auto-recovery cycle. Manual intervention is required."

        set_failed_completion_status_comment \
          "The judge repeated the same normalized failure fingerprint ${JUDGE_FINGERPRINT_REPEAT_COUNT} time(s), exceeding JUDGE_REPEAT_FINGERPRINT_MAX=${JUDGE_REPEAT_FINGERPRINT_MAX}. Manual intervention required. See the \"❌ Judge repeat-fingerprint breaker triggered\" comment for the diagnostic detail."
        tg_cleanup_msgs "${TRACKING_NUM}"
        tg_notify "Project #${TRACKING_NUM} blocked: repeated judge failure fingerprint exceeded JUDGE_REPEAT_FINGERPRINT_MAX=${JUDGE_REPEAT_FINGERPRINT_MAX}. Manual intervention required." "CRITICAL"
        continue
      fi

      # ---------------------------------------------------------------
      # Auto-recovery: configurable attempts (replaces single-shot boolean)
      # ---------------------------------------------------------------
      if [ "${RECOVERY_COUNT}" -ge "${MAX_RECOVERY_ATTEMPTS}" ]; then
        echo "Recovery attempts exhausted (${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}). Stopping."
        jq '.status = "failed" | .judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        post_state_comment || true
        handle_comprehensive_release_callback_if_needed "failed" "${TRACKING_LABELS}" "${COMMENTS:-[]}"

        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
          -f body="## Project Failed

Recovery was attempted ${RECOVERY_COUNT} time(s) (max ${MAX_RECOVERY_ATTEMPTS}) but the judge still reports failure. Manual intervention required.

**Assessment:** ${JUDGE_ASSESSMENT}" >/dev/null

        set_failed_completion_status_comment \
          "Recovery was attempted ${RECOVERY_COUNT} time(s) (max ${MAX_RECOVERY_ATTEMPTS}), but the judge still reports failure. Manual intervention required. See the latest \"## Project Failed\" tracking comment for the diagnostic detail."
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
          PR_TO_REVERT="$(_issue_cross_ref_pr_number_last "${revert_issue}" 2>/dev/null || echo "")"
          if [[ "${PR_TO_REVERT}" =~ ^[0-9]+$ ]]; then
            echo "  Reverting PR #${PR_TO_REVERT} (issue #${revert_issue})..."
            # Create revert PR via gh
            gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls" \
              -f title="Revert PR #${PR_TO_REVERT} (orchestrator auto-recovery)" \
              -f head="revert-${PR_TO_REVERT}-$(date +%s)" \
              -f base="${DEFAULT_BRANCH}" \
              -f body="Automated revert of PR #${PR_TO_REVERT} by orchestrator judge.

**Reason:** ${JUDGE_JUSTIFICATION}" >/dev/null 2>&1 || {
              # If API-based revert fails, create a revert via git
              echo "  API revert failed; creating revert commit..."
              MERGE_SHA="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${PR_TO_REVERT}" --jq '.merge_commit_sha' || echo "")"
              if [ -n "${MERGE_SHA}" ] && [ "${MERGE_SHA}" != "null" ]; then
                REVERT_BRANCH="revert-${PR_TO_REVERT}-$(date +%s)"
                git checkout -b "${REVERT_BRANCH}" "${DEFAULT_BRANCH}"
                if git revert --no-edit "${MERGE_SHA}"; then
                  git push -u origin "${REVERT_BRANCH}"
                  gh_retry gh pr create \
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
          ensure_label_exists "ai:orchestrator-managed"
          FIX_URL="$(gh_retry gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${FIX_TITLE}" \
            --body "${FULL_FIX_BODY}" \
            --label "ai:clarification" \
            --label "ai:orchestrator-managed")"
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

      post_state_comment || true

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
          ensure_label_exists "ai:orchestrator-managed"
          NEW_URL="$(gh_retry gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${NEW_TITLE}" \
            --body "${FULL_NEW_BODY}" \
            --label "ai:clarification" \
            --label "ai:orchestrator-managed")"
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
        post_state_comment || true
        # Skip wave advancement — next poll cycle will re-check this wave
      else

      # Advance to next wave
      NEXT_WAVE=$(( CURRENT_WAVE + 1 ))
      if [ "${NEXT_WAVE}" -le "${TOTAL_WAVES}" ]; then
        echo "Advancing to wave ${NEXT_WAVE}..."
        NEXT_WAVE_IDX=$(( NEXT_WAVE - 1 ))

        # -----------------------------------------------------------
        # Wave-dispatch integration-state gate.
        #
        # Before creating new sub-issues for the next wave, run the
        # merged-sub-issue fingerprint verifier against the
        # integration branch HEAD.  Catches the case where a prior
        # back-merge (e.g. ``[ai-merge-resolve] merge origin/main``)
        # silently re-introduced a file a merged sub-issue had
        # deleted, leaving integration in a state that contradicts
        # the captured merged_issue_fingerprints contract.  Without
        # this gate, the orchestrator would happily dispatch the
        # next wave on top of broken state, and any planner whose
        # final sanity check spans the regressed paths would emit
        # BLOCKED — wasting the wave on a defect that originated
        # several merges earlier.
        #
        # Fail-open on plumbing errors (no fingerprints captured,
        # verifier script missing, fetch failure, git ref unknown):
        # never block dispatch on a transient or pre-existing
        # capture gap — only hard fingerprint violations gate.
        # -----------------------------------------------------------
        WAVE_GATE_BLOCKED=false
        _gate_integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
        _gate_fp_count="$(jq -r '(.merged_issue_fingerprints // {}) | length' "${STATE_FILE}" 2>/dev/null || echo 0)"
        _gate_violations=""
        if [ -n "${_gate_integration_branch}" ] \
           && [ "${_gate_fp_count}" -gt 0 ] \
           && [ -f "scripts/verify_integration_fingerprints.py" ]; then
          # Resolve a fresh ref for the integration branch. In the normal
          # GitHub Actions path, fetch the branch and pin FETCH_HEAD to a
          # concrete commit SHA immediately so later git operations cannot
          # retarget the verifier to a different commit. Outside that
          # context (e.g. local sandboxes without an origin remote), fail
          # open rather than verify a potentially stale local branch tip.
          _gate_ref=""
          if git remote get-url origin >/dev/null 2>&1; then
            if git fetch --no-tags --quiet origin "${_gate_integration_branch}" >/dev/null 2>&1; then
              _gate_ref="$(git rev-parse --verify FETCH_HEAD 2>/dev/null || true)"
            else
              echo "::warning::Wave-dispatch gate: fetch of integration branch '${_gate_integration_branch}' failed; gate fails open and dispatch proceeds."
            fi
          fi
          if [ -n "${_gate_ref}" ] && git rev-parse --verify "${_gate_ref}" >/dev/null 2>&1; then
            # -----------------------------------------------------------
            # Self-heal: purge `merged_issue_fingerprints` entries the
            # gate cannot reasonably satisfy.  Two stale shapes are
            # caught (see
            # `_purge_stale_fingerprint_entries_on_integration_branch`
            # for the predicates): (1) PR not referenced anywhere on the
            # integration branch; (2) capture predates the PR's merge
            # commit on the branch (pre-merge open-PR snapshot — the
            # original symptom of project #2867 / issue #2872, where
            # capture ran while PR #2894 was open and the REST-fallback
            # half was rewritten before the squash-merge landed).
            # Capture is idempotent and cannot self-correct, so the heal
            # lands here right before the verifier reads the state.
            # PR #2907's `_subissue_closing_pr_number` prevents new bad
            # captures from being written; this pass cleans up any
            # already-persisted ones so the gate stops hard-failing on
            # phantom regressions.  Healthy entries (capture ran after
            # the merge) keep `captured_at` > merge committer date and
            # are NOT touched, so a real post-merge resolver regression
            # still hard-fails the gate as designed.
            # -----------------------------------------------------------
            _gate_purged_lines=""
            _gate_purged_count=0
            while IFS=$'\t' read -r _heal_issue _heal_pr _heal_reason; do
              [ -n "${_heal_issue}" ] || continue
              echo "::warning::FINGERPRINT_STATE_SELFHEAL_V1 issue=#${_heal_issue} pr=#${_heal_pr} reason=${_heal_reason} ref=${_gate_ref}"
              _gate_purged_lines="${_gate_purged_lines}- issue #${_heal_issue} (PR #${_heal_pr}): ${_heal_reason}"$'\n'
              _gate_purged_count=$(( _gate_purged_count + 1 ))
            done < <(_purge_stale_fingerprint_entries_on_integration_branch "${STATE_FILE}" "${_gate_ref}")
            if [ "${_gate_purged_count}" -gt 0 ]; then
              _gate_heal_plural="entries"
              [ "${_gate_purged_count}" -eq 1 ] && _gate_heal_plural="entry"
              _gate_heal_comment="## 🩹 Integration fingerprint state self-heal — ${_gate_purged_count} stale ${_gate_heal_plural} purged

The wave-dispatch gate found \`merged_issue_fingerprints\` ${_gate_heal_plural} that cannot reasonably be satisfied against \`${_gate_integration_branch}\`. Capture is idempotent, so these entries can only have been written by an earlier capture pass that either selected a \`Refs #N\` cross-reference instead of an actual implementation PR (pre-PR-#2907 capture bug) or captured a pre-merge snapshot of a PR that was iterated before squash-merging.

Purged ${_gate_heal_plural}:

\`\`\`
${_gate_purged_lines}\`\`\`

The next poll tick will re-run the gate against the cleaned state. If an underlying sub-issue was never actually implemented (rather than misattributed), it will surface separately via the completion-status check."
              post_tracking_comment "${_gate_heal_comment}" || true
              tg_notify "Project #${TRACKING_NUM}: integration fingerprint state self-heal purged ${_gate_purged_count} stale ${_gate_heal_plural}; see tracking comment." "INFO" || true
            fi
            unset _gate_purged_lines _gate_purged_count _heal_issue _heal_pr _heal_reason _gate_heal_plural _gate_heal_comment

            _gate_fp_file=""
            _gate_log_file=""
            trap 'rm -f "${_gate_fp_file:-}" "${_gate_log_file:-}" 2>/dev/null || true' EXIT
            _gate_fp_file="$(mktemp "${TMPDIR:-/tmp}/wave_dispatch_fp.XXXXXX")"
            _gate_log_file="$(mktemp "${TMPDIR:-/tmp}/wave_dispatch_log.XXXXXX")"
            jq -c '.merged_issue_fingerprints // {}' "${STATE_FILE}" > "${_gate_fp_file}"
            _gate_exit=0
            INTEGRATION_BRANCH_NAME="${_gate_integration_branch}" \
              python3 scripts/verify_integration_fingerprints.py \
                --ref "${_gate_ref}" \
                "${_gate_fp_file}" \
                > "${_gate_log_file}" 2>&1 || _gate_exit=$?
            # Mirror the verifier output to the workflow log so
            # operators can see the violation lines inline.
            cat "${_gate_log_file}" || true
            if [ "${_gate_exit}" -eq 1 ]; then
              WAVE_GATE_BLOCKED=true
              # Keep the first ~20 violation lines for the tracking
              # comment so the human reader doesn't have to dig into
              # the run log to see what broke.
              _gate_violations="$(grep -E '^::error::  -' "${_gate_log_file}" | head -n 20 || true)"
            elif [ "${_gate_exit}" -eq 2 ]; then
              echo "::warning::Wave-dispatch gate: verifier exited 2 (plumbing failure); gate fails open and dispatch proceeds."
            elif [ "${_gate_exit}" -ne 0 ]; then
              echo "::warning::Wave-dispatch gate: verifier exited ${_gate_exit} (unexpected); gate fails open and dispatch proceeds."
            fi
            rm -f "${_gate_fp_file}" "${_gate_log_file}" 2>/dev/null || true
            trap - EXIT
          else
            echo "::warning::Wave-dispatch gate: could not resolve a fresh verification ref for integration branch '${_gate_integration_branch}'; gate fails open and dispatch proceeds."
          fi
        fi

        if [ "${WAVE_GATE_BLOCKED}" = "true" ]; then
          echo "::error::Wave ${NEXT_WAVE} dispatch BLOCKED — integration branch '${_gate_integration_branch}' HEAD violates merged sub-issue fingerprint contract."
          # Consume a stall cycle so the orchestrator's stall-recovery
          # path eventually escalates if the regression is not
          # remediated.
          jq '.judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          _gate_alert="## ⚠️ Wave ${NEXT_WAVE} dispatch BLOCKED — integration regression detected

Integration branch \`${_gate_integration_branch}\` HEAD violates the captured merged sub-issue fingerprint contract. A prior back-merge silently re-introduced a file a merged sub-issue had deleted (or otherwise regressed merged sub-issue intent). Refusing to dispatch new work on top of broken integration state — the next planner run would almost certainly emit BLOCKED for the regressed paths.

**Action:** inspect the integration branch HEAD against the \`merged_issue_fingerprints\` entries in the latest state comment. Re-delete the resurrected paths (or otherwise remediate the regression) on \`${_gate_integration_branch}\`, then the next poll tick will re-run the gate and advance.

Verifier violations (first 20):
\`\`\`
${_gate_violations:-(no violation lines parsed — see workflow run log for the full verifier output)}
\`\`\`"
          post_tracking_comment "${_gate_alert}" || true
          tg_notify "Project #${TRACKING_NUM}: wave ${NEXT_WAVE} dispatch blocked — integration branch HEAD violates merged sub-issue fingerprint contract. See tracking comment." "WARNING" || true
          # Note: post_state_comment runs unconditionally at the end of
          # the in_progress arm below, so no separate call here.
        else

        # -----------------------------------------------------------
        # Deferred issue creation: create issues for this wave now.
        # This triggers clarify.yml via the issues.opened event.
        # -----------------------------------------------------------
        CREATED_NUMS=""
        ACTUALLY_CREATED_COUNT=0
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

	if ! _runtime_blocker_dispatch_eligible "${local_id}" "${NEXT_WAVE}" "${_current_wave_details_json:-}"; then
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

          if ! phase_cap_can_dispatch "ai:clarification" "create_issue" "${local_id}"; then
            continue
          fi

          ensure_label_exists "ai:clarification"
          ensure_label_exists "ai:orchestrator-managed"
          NEW_URL="$(gh_retry gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${DEF_TITLE}" \
            --body "${FULL_BODY}" \
            --label "ai:clarification" \
            --label "ai:orchestrator-managed")"

          NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
          if ! [[ "${NEW_NUM}" =~ ^[0-9]+$ ]]; then
            echo "::warning::Could not parse numeric issue number for pending issue ${local_id}; skipping state update."
            continue
          fi
          echo "  Created #${NEW_NUM}: ${DEF_TITLE} (${local_id})"
          phase_cap_note_dispatch "ai:clarification"
          CREATED_NUMS="${CREATED_NUMS} ${NEW_NUM}"
          ACTUALLY_CREATED_COUNT=$(( ACTUALLY_CREATED_COUNT + 1 ))

          # Update state: record the new issue number and remove from pending
          jq ".issue_number_map[\"${local_id}\"] = ${NEW_NUM} | del(.pending_issue_defs[\"${local_id}\"])" \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

          # Update the wave entry with the github issue number
          jq "(.waves[${NEXT_WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\")" \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        done

        # Clean wave advance: does not consume stall budget.
        jq ".current_wave = ${NEXT_WAVE} | .judge_cycle += 1 | .judge_stall_cycles = 0" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        # Q3a: only post the "Wave N Dispatched" narration when this
        # cycle actually created new issues. When every sub-issue in
        # NEXT_WAVE already exists (i.e. pre-created in an earlier
        # cycle), CREATED_NUMS is non-empty but ACTUALLY_CREATED_COUNT
        # is 0 — posting in that case is duplicate narration that adds
        # nothing for human readers and burns API quota.
        if [ "${ACTUALLY_CREATED_COUNT}" -gt 0 ]; then
          WAVE_COMMENT="## Wave ${NEXT_WAVE} Dispatched

Dependencies from Wave ${CURRENT_WAVE} are met. Created and dispatched:

$(for inum in ${CREATED_NUMS}; do echo "- #${inum}"; done)

These issues will enter the AI pipeline (clarify → plan → implement → review)."

          post_tracking_comment "${WAVE_COMMENT}"
        else
          echo "Wave ${NEXT_WAVE} advance: all sub-issues already exist; suppressing duplicate narration."
        fi

        fi  # end: WAVE_GATE_BLOCKED guard
      else
        # All waves dispatched but judge says in_progress with new issues
        jq '.judge_cycle += 1 | .judge_stall_cycles = ((.judge_stall_cycles // 0) + 1)' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi

      # Post updated state
      post_state_comment || true

      fi  # end: current-wave issue-change guard
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
STANDALONE_PRS="$(gh_retry gh pr list \
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

	# Integration final-merge PRs (head=orchestrator/project-*) already
	# have a dedicated self-healing path in heal_integration_branch_conflict.
	# Do not let the standalone sweep duplicate update-branch / review
	# dispatches or overwrite that path's state transitions.
	if [[ "${S_HEAD}" == orchestrator/project-* ]]; then
		continue
	fi

	if [[ "${S_BASE}" == orchestrator/project-* ]]; then
		continue
	fi

	# Check mergeable state via REST API (dirty == merge conflicts)
	# _safe_gh_jq → clean empty stdout on failure so the `|| echo '{}'`
	# fallback stays valid JSON for the downstream jq reads.
	S_PR_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}" || echo '{}')"
	[ -n "${S_PR_JSON}" ] || S_PR_JSON='{}'
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

	# Stage 1: Try the GitHub API update-branch endpoint (clean merge).
	# A real merge conflict returns HTTP 422 with a JSON body on stdout —
	# retrying is pointless (the conflict won't resolve itself) and the
	# body would leak into the log with no newline. Cap retries at 1 and
	# silence stdout while preserving stderr diagnostics; stage 2 handles
	# the permanent-failure path.
	S_HEAD_SHA="$(echo "${S_PR_JSON}" | jq -r '.head.sha // ""')"
	if [ -n "${S_HEAD_SHA}" ] && GH_RETRY_MAX_ATTEMPTS=1 gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}/update-branch" \
		-X PUT -f expected_head_sha="${S_HEAD_SHA}" >/dev/null; then
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

# ---------------------------------------------------------------
# Standalone PR noop-suspicious recovery sweep
# ---------------------------------------------------------------
# When `.github/workflows/review_autofix.yml`'s "Validate editor no-op
# disposition" step (the `EDITOR_NOOP_SUSPICIOUS=true` branch) decides
# an editor run is noop-suspicious, the "Enable auto-merge" step is
# gated off and the PR stalls forever. Issue-linked PRs may eventually
# recover via the generic standalone-stall recovery path (around line
# 5780), but PRs on `claude/**` branches without a linked issue never
# do — they sit indefinitely until a human re-dispatches by hand. The
# original incident was PR
# `shubhodeep1/tele-funtoken-msg-scoring#3053`: 9 productive autofix
# commits landed, the 10th converged, and the PR was stuck.
#
# This sweep closes that gap. For each open PR with one or more
# `⚠️ **Editor no-op suspicious**` warning comments posted after the
# PR's most recent [ai-autofix] / [judge-fix] commit:
#   - If the warning has been emitted < NOOP_MAX_RETRIES times since
#     the last productive commit: re-dispatch review_autofix.yml via
#     workflow_dispatch (reuses _dispatch_review_for_conflicts, which
#     carries the cycle-local _CONFLICT_DISPATCH_TRACKER guard, so a
#     conflict-sweep dispatch already this tick will not be
#     duplicated). Telegram WARNING.
#   - If the count is >= NOOP_MAX_RETRIES: enter the force-merge
#     fallback. Force-merge fails CLOSED — every precondition is
#     audited individually, and any single gate failure aborts with an
#     ERROR alert (the PR stays open for human review). Gates:
#       A) PR is open, not draft, mergeable=true, mergeable_state is
#          neither dirty nor unknown, and does NOT carry
#          `e2e-smoke-test` or `force-review` labels (operator
#          opt-out).
#       B) The latest "AI autofix editor summary" PR comment passes
#          the reviewer audit arithmetic check
#          (`scripts/validate_editor_audit.sh` — same code
#          review_autofix.yml uses, so the two paths cannot drift).
#       C) At least one prior `[ai-autofix]` / `[judge-fix]` commit
#          exists on the PR (a brand-new editor that has never
#          produced work is not eligible for force-merge).
#       D) All completed required checks on the head SHA are
#          conclusion=success / neutral / skipped — any failure /
#          cancelled / timed_out / action_required aborts the gate.
#     If every gate passes: `gh pr merge --squash --auto`, Telegram
#     WARNING, and one audit-trail comment posted to the PR so the
#     decision is visible on the thread (not only in Telegram).
#
# Retry counter design. The count of noop-suspicious warning comments
# newer than the latest [ai-autofix] / [judge-fix] commit IS the
# retry counter — no new per-PR state JSON is introduced.
#   - Reset on new head SHA: when a productive commit lands, the count
#     of noop-warnings-after-that-commit drops to 0, satisfying the
#     "new SHA invalidates prior noop history" requirement.
#   - Works for both issue-linked AND linked-issue-less PRs uniformly.
#     The standalone-state JSON helpers at line 5346 are keyed by
#     issue number and cannot represent linked-issue-less PRs at all,
#     so adding a counter field there would only solve half the gap.
#   - Robust to poller restarts (state lives in GitHub, not on disk
#     or in memory).
#
# API-call hygiene (§15). The sweep reuses `STANDALONE_PRS` from the
# conflict sweep above — no second `gh pr list`. A pre-filter
# Issues-comments call is needed per PR because no existing call in
# the poll cycle returns PR conversation comments:
#   - _fetch_candidate_issue_details_graphql fetches issue (not PR)
#     comments, and only for issue-linked candidates.
#   - The conflict sweep's `pulls/{n}` REST call returns PR metadata
#     but not comments.
# The common case (PR with no noop warnings) costs just 1 comments
# call and exits. A noop-suspicious PR additionally costs 1 commits
# call, 1 pulls call (force-merge gate), and 1 check-runs call (force-
# merge gate D) — gated so they only fire when the retry threshold has
# been reached.
# ---------------------------------------------------------------
echo ""
echo "========================================"
echo "Standalone PR noop-suspicious recovery sweep"
echo "========================================"

NOOP_WARNING_LITERAL="⚠️ **Editor no-op suspicious**"
NOOP_RECOVERY_DISPATCHED=0
NOOP_FORCE_MERGED=0
NOOP_RECOVERY_BLOCKED=0
NOOP_MAX_RETRIES=3
# Operator-facing opt-outs. `e2e-smoke-test` mirrors the workflow's
# own auto-merge suppression so the smoke-test bait-removal race
# stays sealed; `force-review` lets a human pin a stuck PR for manual
# review without having to disable auto-merge separately. Store the
# list one label per line so labels with spaces stay intact.
NOOP_FORCE_MERGE_SKIP_LABELS=$'e2e-smoke-test\nforce-review'

# Source the shared audit helper once, outside the loop.
if [ -f scripts/validate_editor_audit.sh ]; then
	# shellcheck source=scripts/validate_editor_audit.sh disable=SC1091
	source scripts/validate_editor_audit.sh
fi

for (( nidx=0; nidx<STANDALONE_COUNT; nidx++ )); do
	N_PR="$(echo "${STANDALONE_PRS}" | jq -r ".[${nidx}].number")"
	N_HEAD="$(echo "${STANDALONE_PRS}" | jq -r ".[${nidx}].headRefName")"
	N_BASE="$(echo "${STANDALONE_PRS}" | jq -r ".[${nidx}].baseRefName")"

	if [ -z "${N_PR}" ] || [ "${N_PR}" = "null" ]; then
		continue
	fi
	if [ -z "${N_HEAD}" ] || [ "${N_HEAD}" = "null" ]; then
		continue
	fi
	# Skip integration / orchestrator-managed branches — those have
	# their own merge cadence and should not be force-merged by this
	# sweep. The final integration PR targets the default branch
	# (base=main) while its head is `orchestrator/project-*`, so check
	# both sides: base catches sub-issue PRs targeting the integration
	# branch; head catches the integration→default final PR.
	if [[ "${N_BASE}" == orchestrator/project-* ]] || [[ "${N_HEAD}" == orchestrator/project-* ]]; then
		continue
	fi
	# Skip forward-merge fallback PRs opened by
	# `.github/workflows/forward-merge-stable-to-main.yml` when the
	# automated `stable`→`main` merge hits conflict or branch
	# protection. Head ref is hard-coded as
	# `auto/forward-merge-stable-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`
	# at forward-merge-stable-to-main.yml:255. These PRs MUST be
	# merged via GitHub's "Create a merge commit" button — NOT
	# squash/rebase — so the 2-parent merge keeps `stable`'s tip
	# reachable from `main`. The retry path here would burn API
	# budget invoking review_autofix repeatedly (the workflow's own
	# "Enable auto-merge on PR" step suppresses auto-merge for this
	# head-ref prefix, so the noop-suspicious flag never clears via
	# a productive [ai-autofix] commit); worse, the force-merge
	# fallback below executes `gh pr merge --squash --auto`, which
	# silently strips that ancestry. promote-main-to-stable.yml's
	# pre-flight `git merge-base --is-ancestor HEAD origin/main`
	# check then refuses the next promote run (see
	# `.github/workflows/promote-main-to-stable.yml:115-126` and the
	# CAUTION banner injected into every fallback PR body at
	# `.github/workflows/forward-merge-stable-to-main.yml:265-270`).
	# Mirrors the existing suppressors in `review_autofix.yml`'s
	# codex-agent "Enable auto-merge on PR" step (line ~5127) and
	# the `deterministic-skip-merge` sibling job (line ~697). The
	# pattern is hard-coded — the branch prefix is owned by the
	# forward-merge workflow and never varies per repo.
	if [[ "${N_HEAD}" == auto/forward-merge-stable-* ]]; then
		continue
	fi

	# ── Step 1: pre-filter via comments scan ──
	# Fetch up to 100 most-recent comments. Noop-suspicious warnings
	# are posted by the workflow itself so they always appear in the
	# issue-comments stream for the PR.
	N_COMMENTS_JSON="$(gh_retry _safe_gh_jq --paginate \
		"repos/${GITHUB_REPOSITORY}/issues/${N_PR}/comments?per_page=100" \
		| jq -s 'add // []' 2>/dev/null || echo '[]')"
	[ -n "${N_COMMENTS_JSON}" ] || N_COMMENTS_JSON='[]'

	N_NOOP_LATEST_TS="$(echo "${N_COMMENTS_JSON}" | jq -r \
		--arg lit "${NOOP_WARNING_LITERAL}" \
		'[.[] | select((.body // "") | contains($lit))] | max_by(.created_at // "") | .created_at // empty' \
		2>/dev/null || echo "")"

	if [ -z "${N_NOOP_LATEST_TS}" ]; then
		# No noop-suspicious warning ever posted → not a candidate.
		continue
	fi

	# ── Step 2: find latest productive commit timestamp ──
	# Only [ai-autofix] / [judge-fix] commits count as "productive
	# autofix output" — these are the prefixes review_autofix.yml /
	# review_commit_changes.sh emit when the editor or judge writes
	# real source-file changes. Manual commits, merge commits, and
	# bot-housekeeping commits do NOT reset the noop counter.
	N_COMMITS_JSON="$(gh_retry _safe_gh_jq --paginate \
		"repos/${GITHUB_REPOSITORY}/pulls/${N_PR}/commits?per_page=100" \
		| jq -s 'add // []' 2>/dev/null || echo '[]')"
	[ -n "${N_COMMITS_JSON}" ] || N_COMMITS_JSON='[]'

	N_LATEST_PROD_TS="$(echo "${N_COMMITS_JSON}" | jq -r \
		'[.[] | select((.commit.message // "") | test("\\[ai-autofix\\]|\\[judge-fix\\]"))] | max_by(.commit.committer.date // "") | .commit.committer.date // empty' \
		2>/dev/null || echo "")"

	# Stale-warning check: if the latest noop warning predates the
	# latest productive commit, the PR has already moved on. GitHub API
	# timestamps are ISO 8601 UTC, so bash's lexical < comparison is
	# chronology-safe here. This is the equivalent of "reset on new
	# head SHA" from the design spec.
	if [ -n "${N_LATEST_PROD_TS}" ] && [[ "${N_NOOP_LATEST_TS}" < "${N_LATEST_PROD_TS}" ]]; then
		echo "  PR #${N_PR}: latest noop warning (${N_NOOP_LATEST_TS}) is older than latest productive commit (${N_LATEST_PROD_TS}); stale, skipping."
		continue
	fi

	# Count noop warnings newer than the latest productive commit
	# (or all noop warnings when there is no productive commit yet —
	# treated as the worst case for the retry counter).
	if [ -n "${N_LATEST_PROD_TS}" ]; then
		N_NOOP_COUNT="$(echo "${N_COMMENTS_JSON}" | jq -r \
			--arg lit "${NOOP_WARNING_LITERAL}" \
			--arg since "${N_LATEST_PROD_TS}" \
			'[.[] | select((.body // "") | contains($lit)) | select((.created_at // "") > $since)] | length' \
			2>/dev/null || echo "0")"
	else
		N_NOOP_COUNT="$(echo "${N_COMMENTS_JSON}" | jq -r \
			--arg lit "${NOOP_WARNING_LITERAL}" \
			'[.[] | select((.body // "") | contains($lit))] | length' \
			2>/dev/null || echo "0")"
	fi
	[[ "${N_NOOP_COUNT}" =~ ^[0-9]+$ ]] || N_NOOP_COUNT=0

	echo "  PR #${N_PR} (${N_HEAD}) noop-suspicious count since last productive commit: ${N_NOOP_COUNT}/${NOOP_MAX_RETRIES}"

	# ── Step 3a: under threshold → re-dispatch ──
	if [ "${N_NOOP_COUNT}" -lt "${NOOP_MAX_RETRIES}" ]; then
		_noop_dispatch_rc=0
		_dispatch_review_for_conflicts "${N_PR}" "${N_HEAD}" || _noop_dispatch_rc=$?
		if [ "${_noop_dispatch_rc}" -eq 0 ]; then
			tg_send_msg "Noop-suspicious recovery: re-dispatched review_autofix for PR #${N_PR} (retry $((N_NOOP_COUNT + 1))/${NOOP_MAX_RETRIES})."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "WARNING" >/dev/null 2>&1 || true
			NOOP_RECOVERY_DISPATCHED=$((NOOP_RECOVERY_DISPATCHED + 1))
		elif [ "${_noop_dispatch_rc}" -eq 2 ]; then
			echo "  PR #${N_PR}: autofix already active / dispatched this cycle; skipping."
		else
			echo "::warning::PR #${N_PR}: noop-suspicious redispatch failed (rc=${_noop_dispatch_rc})."
		fi
		continue
	fi

	# ── Step 3b: at threshold → force-merge gate ──
	echo "  PR #${N_PR} hit ${NOOP_MAX_RETRIES}-retry cap. Evaluating force-merge fallback..."

	# Gate A: PR mergeability + opt-out labels.
	N_PR_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${N_PR}" || echo '{}')"
	[ -n "${N_PR_JSON}" ] || N_PR_JSON='{}'

	N_STATE="$(echo "${N_PR_JSON}" | jq -r '.state // ""')"
	N_DRAFT="$(echo "${N_PR_JSON}" | jq -r '.draft // false')"
	N_MERGEABLE="$(echo "${N_PR_JSON}" | jq -r '.mergeable // null')"
	N_MERGEABLE_STATE="$(echo "${N_PR_JSON}" | jq -r '.mergeable_state // ""')"
	N_HEAD_SHA="$(echo "${N_PR_JSON}" | jq -r '.head.sha // ""')"

	if [ "${N_STATE}" != "open" ] || [ "${N_DRAFT}" = "true" ]; then
		tg_send_msg "Noop-suspicious force-merge gate A failed for PR #${N_PR}: state=${N_STATE} draft=${N_DRAFT}. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	if [ "${N_MERGEABLE}" != "true" ] || [ "${N_MERGEABLE_STATE}" = "dirty" ] || [ "${N_MERGEABLE_STATE}" = "unknown" ] || [ -z "${N_MERGEABLE_STATE}" ]; then
		tg_send_msg "Noop-suspicious force-merge gate A failed for PR #${N_PR}: mergeable=${N_MERGEABLE} mergeable_state=${N_MERGEABLE_STATE:-empty}. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	N_LABELS_JSON="$(echo "${N_PR_JSON}" | jq -c '[.labels[]?.name // empty]' 2>/dev/null || echo '[]')"
	_noop_skip_label=""
	while IFS= read -r _lbl; do
		[ -n "${_lbl}" ] || continue
		if echo "${N_LABELS_JSON}" | jq -e --arg l "${_lbl}" 'any(.[]; . == $l)' >/dev/null 2>&1; then
			_noop_skip_label="${_lbl}"
			break
		fi
	done <<< "${NOOP_FORCE_MERGE_SKIP_LABELS}"
	if [ -n "${_noop_skip_label}" ]; then
		tg_send_msg "Noop-suspicious force-merge gate A failed for PR #${N_PR}: carries '${_noop_skip_label}' label (operator opt-out). Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	# Refresh the comments snapshot on the threshold path so Gate B
	# validates the latest editor summary when a new noop warning or
	# summary comment lands mid-tick. Force-merge is safety-sensitive,
	# so a refresh failure fails closed for this cycle rather than
	# evaluating Gate B on a stale pre-filter snapshot.
	N_FRESH_COMMENTS_JSON=""
	if N_FRESH_COMMENTS_JSON="$(gh_retry _safe_gh_jq --paginate \
		"repos/${GITHUB_REPOSITORY}/issues/${N_PR}/comments?per_page=100" \
		| jq -s 'add // []' 2>/dev/null)"; then
		[ -n "${N_FRESH_COMMENTS_JSON}" ] || N_FRESH_COMMENTS_JSON='[]'
		N_COMMENTS_JSON="${N_FRESH_COMMENTS_JSON}"
	else
		echo "::warning::PR #${N_PR}: could not refresh comments snapshot for noop-suspicious Gate B; failing force-merge closed this cycle."
		tg_send_msg "Noop-suspicious force-merge gate B failed for PR #${N_PR}: could not refresh latest PR comments snapshot. Leaving PR open for a later retry."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	# Gate B: reviewer audit health.  Validate against the latest
	# editor summary PR comment using the shared helper — this is the
	# exact same regex/arithmetic logic that review_autofix.yml's
	# "Validate editor no-op disposition" step uses, so a PR the
	# workflow flagged as noop-suspicious-but-audit-healthy here will
	# stay flagged-but-audit-healthy in the helper too.
	N_LATEST_EDITOR_SUMMARY="$(echo "${N_COMMENTS_JSON}" | jq -r \
		'[.[] | select((.body // "") | startswith("AI autofix editor summary"))] | max_by(.created_at // "") | .body // empty' \
		2>/dev/null || echo "")"

	if [ -z "${N_LATEST_EDITOR_SUMMARY}" ]; then
		tg_send_msg "Noop-suspicious force-merge gate B failed for PR #${N_PR}: no editor summary comment found. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	_noop_audit_rc=0
	_noop_tmp_summary="$(mktemp "${TMPDIR:-/tmp}/noop_audit.XXXXXX" 2>/dev/null || true)"
	if [ -z "${_noop_tmp_summary}" ]; then
		_noop_audit_rc=3
		echo "::warning::PR #${N_PR}: could not allocate temp file for editor summary; failing force-merge gate B closed."
	elif ! printf '%s' "${N_LATEST_EDITOR_SUMMARY}" > "${_noop_tmp_summary}"; then
		_noop_audit_rc=3
		echo "::warning::PR #${N_PR}: could not write editor summary to temp file; failing force-merge gate B closed."
	elif type validate_editor_audit_arithmetic >/dev/null 2>&1; then
		validate_editor_audit_arithmetic "${_noop_tmp_summary}" || _noop_audit_rc=$?
	else
		# Helper missing — fail closed.
		_noop_audit_rc=3
		echo "::warning::PR #${N_PR}: validate_editor_audit_arithmetic helper not loaded; failing force-merge gate B closed."
	fi
	rm -f "${_noop_tmp_summary}" 2>/dev/null || true

	if [ "${_noop_audit_rc}" -ne 0 ]; then
		tg_send_msg "Noop-suspicious force-merge gate B failed for PR #${N_PR}: reviewer audit unhealthy (helper rc=${_noop_audit_rc}). Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	# Gate C: at least one [ai-autofix] / [judge-fix] commit must
	# exist. This guards against an editor that was broken from the
	# very first invocation (no productive work, nothing to validate
	# against).
	if [ -z "${N_LATEST_PROD_TS}" ]; then
		tg_send_msg "Noop-suspicious force-merge gate C failed for PR #${N_PR}: no prior [ai-autofix] / [judge-fix] commit found. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	# Gate D: required checks must not be failing.
	if [ -z "${N_HEAD_SHA}" ]; then
		tg_send_msg "Noop-suspicious force-merge gate D failed for PR #${N_PR}: head SHA missing. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	N_CHECKS_JSON="$(gh_retry _safe_gh_jq --paginate --slurp \
		"repos/${GITHUB_REPOSITORY}/commits/${N_HEAD_SHA}/check-runs?per_page=100" \
		|| echo '{}')"

	_failing_check_count="$(printf '%s' "${N_CHECKS_JSON}" | jq -r '
		def _is_blocking: .status == "completed" and (
			.conclusion == "failure" or .conclusion == "cancelled" or
			.conclusion == "timed_out" or .conclusion == "action_required"
		);
		if (type == "array") then
			[.[]? | (.check_runs // [])[] | select(_is_blocking)] | length
		elif (type == "object" and (.check_runs | type == "array")) then
			[.check_runs[] | select(_is_blocking)] | length
		else
			empty
		end
	' 2>/dev/null | tail -n1)"
	if ! [[ "${_failing_check_count}" =~ ^[0-9]+$ ]]; then
		tg_send_msg "Noop-suspicious force-merge gate D failed for PR #${N_PR}: could not query check-runs for head SHA ${N_HEAD_SHA}. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	_failing_check="$(printf '%s' "${N_CHECKS_JSON}" | jq -r '
		def _is_blocking: .status == "completed" and (
			.conclusion == "failure" or .conclusion == "cancelled" or
			.conclusion == "timed_out" or .conclusion == "action_required"
		);
		if (type == "array") then
			([.[]? | (.check_runs // [])[] | select(_is_blocking) | .name] | .[0]) // empty
		elif (type == "object" and (.check_runs | type == "array")) then
			([.check_runs[] | select(_is_blocking) | .name] | .[0]) // empty
		else
			empty
		end
	' 2>/dev/null | tail -n1)"

	if [ -n "${_failing_check}" ]; then
		tg_send_msg "Noop-suspicious force-merge gate D failed for PR #${N_PR}: check '${_failing_check}' is failing/cancelled. Leaving PR for human review."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
		NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))
		continue
	fi

	# All gates passed → force-merge.
	echo "  PR #${N_PR}: all force-merge gates passed. Enabling auto-merge via 'gh pr merge --auto'..."
	if gh_retry gh pr merge "${N_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto >/dev/null 2>&1; then
		NOOP_FORCE_MERGED=$((NOOP_FORCE_MERGED + 1))
		tg_send_msg "Force-merging PR #${N_PR} after ${NOOP_MAX_RETRIES} noop-suspicious retries; reviewer audit was healthy."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "WARNING" >/dev/null 2>&1 || true

		# Audit-trail PR comment.  One per force-merge decision so the
		# rationale is on the PR thread (not only in Telegram).
		_force_merge_body="🤖 **Auto-merge enabled after ${NOOP_MAX_RETRIES} noop-suspicious retries**

The editor converged ${NOOP_MAX_RETRIES} times in a row (every reviewer suggestion was either already-applied or correctly ignored), but \`EDITOR_NOOP_SUSPICIOUS=true\` blocked the normal auto-merge step. The orchestrator-poll force-merge fallback engaged because:

- Reviewer audit arithmetic balanced (validated via \`scripts/validate_editor_audit.sh\` against the latest editor summary).
- No required checks are failing on \`${N_HEAD_SHA}\`.
- ${N_NOOP_COUNT} noop-suspicious warning(s) since the latest \`[ai-autofix]\` / \`[judge-fix]\` commit.

To pause this behavior for manual review, add the \`force-review\` label."
		gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${N_PR}/comments" \
			-f body="${_force_merge_body}" >/dev/null 2>&1 || true
	else
		echo "::warning::PR #${N_PR}: gh pr merge --auto failed; will retry next cycle."
		tg_send_msg "Noop-suspicious force-merge attempted but 'gh pr merge --auto' failed for PR #${N_PR}. Will retry next cycle."$'\n'"PR: $(_gh_url "pull/${N_PR}")" "ERROR" >/dev/null 2>&1 || true
	fi
done

write_state_snapshot_actions_runs_export || true
echo "Noop-suspicious recovery complete. Dispatched: ${NOOP_RECOVERY_DISPATCHED}, force-merged: ${NOOP_FORCE_MERGED}, blocked: ${NOOP_RECOVERY_BLOCKED}."
