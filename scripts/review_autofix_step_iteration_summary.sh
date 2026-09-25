#!/usr/bin/env bash
# Body of the "Append review pipeline iteration summary" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail
# Run summary parsing away from model-writable repository startup files.
unset BASH_ENV ENV
cd "${RUNNER_TEMP:?RUNNER_TEMP is required for safe summary startup}"

count_nonempty_lines()
{
  local path="$1"
  if [ ! -f "${path}" ]; then
    echo 0
    return
  fi
  awk 'NF { c++ } END { print c + 0 }' "${path}" 2>/dev/null || echo 0
}

file_bytes()
{
  local path="$1"
  if [ ! -f "${path}" ]; then
    echo 0
    return
  fi
  wc -c < "${path}" 2>/dev/null | tr -d '[:space:]' || echo 0
}

stat_value()
{
  local path="$1"
  local key="$2"
  local value=""
  if [ -f "${path}" ]; then
    value="$(awk -F= -v key="${key}" '$1 == key { print $2; exit }' "${path}" 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  if [[ "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value}"
  else
    echo 0
  fi
}

ledger_state_count()
{
  local path="$1"
  local state="$2"
  if [ ! -f "${path}" ]; then
    echo 0
    return
  fi
  awk -F $'\t' -v state="${state}" '$2 == state { c++ } END { print c + 0 }' "${path}" 2>/dev/null || echo 0
}

sanitize_nonnegative_int()
{
  local value="${1:-0}"
  if [[ "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value}"
  else
    echo 0
  fi
}

iteration_label="${AUTOFIX_ITERATION_METRIC:-unknown}"
[ -n "${iteration_label}" ] || iteration_label="unknown"

reviewers_run="${REVIEWERS_SUCCESSFUL:-0}"
if ! [[ "${reviewers_run}" =~ ^[0-9]+$ ]]; then
  reviewers_run=0
fi

reviewer_scope_label="full-diff"
reviewer_scope_note="- Reviewer flags: checklist=${REVIEW_REVIEWER_CHECKLIST_ENABLED:-1}, iteration_scoping=${REVIEW_REVIEWER_ITERATION_SCOPING:-1} (plumbed only on this branch; reviewer scope remains full-diff)."

reviewer_bundle_file="${RUNTIME_DIR}/reviewer_bundle.txt"
floor_tags_file="${RUNTIME_DIR}/floor_tags.txt"
consolidator_raw_file="${RUNTIME_DIR}/consolidator_raw.txt"
parser_stats_file="${RUNTIME_DIR}/parser_stats.txt"
ledger_status_file="${RUNTIME_DIR}/ledger_status.txt"
committed_files_file="${COMMITTED_FILES_FILE:-${RUNTIME_DIR}/committed_files.txt}"

bundle_bytes="$(file_bytes "${reviewer_bundle_file}")"
floor_tag_count="$(count_nonempty_lines "${floor_tags_file}")"
consolidator_output_bytes="$(file_bytes "${consolidator_raw_file}")"
consolidator_invoked="unknown"
if [ "${REVIEW_CONSOLIDATOR_ENABLED:-1}" = "0" ]; then
  consolidator_invoked="no"
elif [ -f "${consolidator_raw_file}" ]; then
  if [ "${consolidator_output_bytes}" -gt 0 ]; then
    consolidator_invoked="yes"
  else
    consolidator_invoked="failed"
  fi
fi

parsed_blocks="$(stat_value "${parser_stats_file}" parsed_blocks)"
passthrough_blocks="$(stat_value "${parser_stats_file}" passthrough_blocks)"
line_unverified="$(stat_value "${parser_stats_file}" line_unverified)"

ledger_total="$(count_nonempty_lines "${ledger_status_file}")"
ledger_new="$(ledger_state_count "${ledger_status_file}" NEW)"
ledger_persisting="$(ledger_state_count "${ledger_status_file}" PERSISTING)"
ledger_fixed="$(ledger_state_count "${ledger_status_file}" FIXED)"
ledger_resurgent="$(ledger_state_count "${ledger_status_file}" RESURGENT)"
ledger_accepted_residual="$(ledger_state_count "${ledger_status_file}" accepted-residual)"

editor_invoked="no"
if [ -s "${EDITOR_SUMMARY_FILE:-/dev/null}" ]; then
  editor_invoked="yes"
elif [ -d "${PREVIOUS_REVIEWS_DIR:-}" ] && \
  { compgen -G "${PREVIOUS_REVIEWS_DIR}/editor_attempt_*.txt" >/dev/null || compgen -G "${PREVIOUS_REVIEWS_DIR}/editor_attempt_*.err" >/dev/null; }; then
  editor_invoked="yes"
fi

override_count=0
if [ -f "${EDITOR_SUMMARY_FILE:-/dev/null}" ]; then
  override_count="$(grep -c 'CONSOLIDATOR_OVERRIDDEN:' "${EDITOR_SUMMARY_FILE}" 2>/dev/null || true)"
fi
if ! [[ "${override_count}" =~ ^[0-9]+$ ]]; then
  override_count=0
fi

editor_commit_produced="${EDITOR_COMMIT_PRODUCED:-}"
case "${editor_commit_produced}" in
  true)
    editor_commit_produced="yes"
    ;;
  false|'')
    editor_commit_produced="no"
    ;;
  *)
    editor_commit_produced="unknown"
    ;;
esac

budget_total_secs="$(sanitize_nonnegative_int "${CODEX_RUN_BUDGET_TOTAL_SECS:-0}")"
budget_start_epoch="$(sanitize_nonnegative_int "${CODEX_RUN_BUDGET_START_EPOCH:-${JOB_START_EPOCH:-0}}")"
job_end_epoch="$(sanitize_nonnegative_int "$(date -u +%s 2>/dev/null || echo 0)")"
budget_elapsed_secs=0
if [ "${budget_start_epoch}" -gt 0 ] && [ "${job_end_epoch}" -ge "${budget_start_epoch}" ]; then
  budget_elapsed_secs=$((job_end_epoch - budget_start_epoch))
fi
budget_remaining_secs=0
if [ "${budget_total_secs}" -gt 0 ] && [ "${budget_elapsed_secs}" -lt "${budget_total_secs}" ]; then
  budget_remaining_secs=$((budget_total_secs - budget_elapsed_secs))
fi

resume_round="${AUTOFIX_RESUME_ROUND:-${RESUME_ROUND:-0}}"
if ! [[ "${resume_round}" =~ ^[0-9]+$ ]]; then
  resume_round=0
fi
resume_round_limit="$(sanitize_nonnegative_int "${AUTOFIX_RESUME_ROUND_LIMIT:-0}")"
resume_restored="${AUTOFIX_RESUME_RESTORED:-false}"
resume_head_sha="${AUTOFIX_RESUME_HEAD_SHA:-}"
resume_state="${AUTOFIX_RESUME_STATE:-fresh}"
resume_should_continue="${AUTOFIX_RESUME_SHOULD_CONTINUE:-false}"
partial_finalize_validation_tail_can_complete="${AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE:-false}"
partial_finalize_edits_withheld_for_safety="${AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY:-false}"
partial_finalize_withheld_reason="${AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON:-none}"

structured_summary_line="$(
  BUDGET_ELAPSED_SECS="${budget_elapsed_secs}" \
  BUDGET_TOTAL_SECS="${budget_total_secs}" \
  BUDGET_REMAINING_SECS="${budget_remaining_secs}" \
  RESUME_ROUND_SUMMARY="${resume_round}" \
  REVIEWER_BUNDLE_FILE="${reviewer_bundle_file}" \
  FLOOR_TAGS_FILE="${floor_tags_file}" \
  CONSOLIDATOR_RAW_FILE="${consolidator_raw_file}" \
  PARSER_STATS_FILE="${parser_stats_file}" \
  LEDGER_STATUS_FILE="${ledger_status_file}" \
  COMMITTED_FILES_FILE="${committed_files_file}" \
  PYTHONDONTWRITEBYTECODE=1 \
  python3 - <<'PY' || true
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def bool_env(name: str) -> bool:
    return env(name).strip().lower() == "true"


def csv_env_list(name: str) -> list[str]:
    raw = env(name).strip()
    if not raw:
        return []
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return [item for item in values if item != "none"]


def int_env(name: str, default: int = 0) -> int:
    raw = env(name).strip()
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    return value if value >= 0 else default


def env_path(name: str) -> Path | None:
    raw = env(name).strip()
    return Path(raw) if raw else None


def read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def file_exists(path: Path | None) -> bool:
    return path is not None and path.exists()


def file_nonempty(path: Path | None) -> bool:
    return path is not None and path.exists() and path.stat().st_size > 0


def max_attempt_from_text(text: str) -> int:
    matches = [int(value) for value in re.findall(r"\battempt\s+([0-9]+)\b", text)]
    return max(matches) if matches else 0


def reviewer_stall_kill_count(log_text: str) -> int:
    return sum(1 for line in log_text.splitlines() if "recorded codex_stall_killed on " in line)


def reviewer_stall_recovery_next_action(log_text: str) -> str:
    next_action = ""
    for line in log_text.splitlines():
        if not line.startswith("REVIEWER_ADVANCE: "):
            continue
        if "reason=stall_guard" not in line:
            continue
        match = re.search(r"\bnext_action=([A-Za-z0-9_:-]+)", line)
        if match is not None:
            next_action = match.group(1)
    return next_action


def parse_marker_fields(line: str, prefix: str) -> dict[str, str]:
    if not line.startswith(prefix):
        return {}
    fields: dict[str, str] = {}
    for token in line[len(prefix):].strip().split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def marker_int(value: str, default: int = 0) -> int:
    try:
        parsed = int(str(value), 10)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def marker_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


def marker_fields_from_lines(text: str, prefix: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        candidate = parse_marker_fields(line, prefix)
        if candidate:
            rows.append(candidate)
    return rows


def reviewer_last_slot_state_fields(log_text: str) -> dict[str, str]:
    slot_state_rows = marker_fields_from_lines(log_text, "REVIEWER_SLOT_STATE: ")
    return slot_state_rows[-1] if slot_state_rows else {}


def reviewer_retryable_failure_classes(
    log_text: str,
    output_text: str,
    slot_state_fields: dict[str, str],
) -> list[str]:
    raw = slot_state_fields.get("retryable_failure_classes", "").strip()
    if raw and raw != "none":
        return [value for value in raw.split(",") if value]
    classes: list[str] = []
    for value in re.findall(r"failure classified as retryable \(([^)]+)\)", log_text):
        if value not in classes:
            classes.append(value)
    for value in re.findall(r"retryable failure \(([^)]+)\)", output_text):
        if value not in classes:
            classes.append(value)
    return classes


def reviewer_retryable_failure_count(
    status: str,
    log_text: str,
    output_text: str,
    slot_state_fields: dict[str, str],
    retryable_failure_classes: list[str],
) -> int:
    raw = slot_state_fields.get("retryable_failure_count", "").strip()
    if raw:
        return marker_int(raw)
    count = len(re.findall(r"failure classified as retryable \([^)]+\)", log_text))
    if count > 0:
        return count
    if status in {"failed", "skipped_unmapped", "skipped_budget"} and retryable_failure_classes:
        return 1
    if re.search(r"retryable failure \([^)]+\)", output_text):
        return 1
    return 0


def reviewer_backoff_sleep_secs_total(log_text: str, slot_state_fields: dict[str, str]) -> int:
    raw = slot_state_fields.get("backoff_sleep_secs_total", "").strip()
    if raw:
        return marker_int(raw)
    total_sleep_secs = 0
    for fields in marker_fields_from_lines(log_text, "REVIEWER_BACKOFF: "):
        total_sleep_secs += marker_int(fields.get("sleep_secs", "0"))
    return total_sleep_secs


def reviewer_slot_retry_budget_exhausted(
    log_text: str,
    output_text: str,
    slot_state_fields: dict[str, str],
) -> bool:
    raw = slot_state_fields.get("slot_retry_budget_exhausted", "").strip()
    if raw:
        return marker_bool(raw)
    combined = f"{log_text}\n{output_text}"
    return "retry backoff budget was exhausted" in combined


def reviewer_fallback_model_used(log_text: str, slot_state_fields: dict[str, str]) -> bool:
    raw = slot_state_fields.get("fallback_model_used", "").strip()
    if raw:
        return marker_bool(raw)
    return (
        "REVIEWER_FAILBACK:" in log_text
        or "next_action=failback" in log_text
        or "next_action=retry_extended" in log_text
    )


def reviewer_cache_status(log_text: str, slot_state_fields: dict[str, str]) -> str:
    raw = slot_state_fields.get("cache_status", "").strip()
    if raw:
        return raw
    cache_rows = marker_fields_from_lines(log_text, "REVIEWER_CACHE: ")
    if cache_rows:
        return cache_rows[-1].get("status", "unknown") or "unknown"
    return "unknown"


def reviewer_cache_reuse_attempted(
    log_text: str,
    slot_state_fields: dict[str, str],
) -> bool:
    raw = slot_state_fields.get("cache_reuse_attempted", "").strip()
    if raw:
        return marker_bool(raw)
    for fields in marker_fields_from_lines(log_text, "REVIEWER_CACHE: "):
        if fields.get("status") != "supported":
            continue
        if fields.get("prompt_reused") != "true":
            continue
        if marker_int(fields.get("attempt", "0")) > 1:
            return True
    return False


def reviewer_cache_read_input_tokens(log_text: str, expected_attempt_count: int) -> tuple[int, bool]:
    total_cache_read_input_tokens = 0
    usage_rows = marker_fields_from_lines(log_text, "INFO: openrouter usage ")
    if not usage_rows:
        return total_cache_read_input_tokens, False
    cache_read_input_tokens_available = len(usage_rows) == expected_attempt_count
    for fields in usage_rows:
        raw_cache_read_input_tokens = fields.get("cache_read_input_tokens", "").replace(",", "")
        row_usage_available = fields.get("usage_available", "").strip().lower()
        if row_usage_available == "false" or not raw_cache_read_input_tokens.isdigit():
            cache_read_input_tokens_available = False
            continue
        total_cache_read_input_tokens += int(raw_cache_read_input_tokens)
    return total_cache_read_input_tokens, cache_read_input_tokens_available


def reviewer_failure_class(status: str, log_text: str, output_text: str) -> str:
    stall_guard_logged = reviewer_stall_kill_count(log_text) > 0 or "killed by codex stall guard" in log_text
    if status == "success":
        return "none"
    if status == "pr_closed":
        return "pr_closed"
    if status == "skipped_budget":
        return "soft_deadline"
    if status == "skipped_open":
        return "cached_open"
    if status == "skipped_unmapped":
        if stall_guard_logged:
            return "stall_guard"
        retryable_output = re.findall(r"retryable failure \(([^)]+)\)", output_text)
        if retryable_output:
            return retryable_output[-1]
        retryable_log = re.findall(r"failure classified as retryable \(([^)]+)\)", log_text)
        if retryable_log:
            return retryable_log[-1]
        return "retryable_failure"

    if stall_guard_logged:
        return "stall_guard"
    retryable_log = re.findall(r"failure classified as retryable \(([^)]+)\)", log_text)
    if retryable_log:
        return retryable_log[-1]
    if "idle timeout" in log_text:
        return "idle_timeout"
    if "max wall" in log_text:
        return "max_wall"
    if "codex_stall_killed" in log_text or "killed by codex stall guard" in log_text:
        return "stall_guard"
    if "produced empty output" in log_text:
        return "empty_output"
    if "execution failed" in log_text:
        return "non_retryable"
    if "codex CLI not found" in log_text or "codex CLI not found" in output_text:
        return "codex_cli_missing"
    return "unknown"


runtime_dir = env_path("RUNTIME_DIR") or Path(".")
previous_reviews_dir = env_path("PREVIOUS_REVIEWS_DIR")
editor_summary_file = env_path("EDITOR_SUMMARY_FILE")
committed_files_file = env_path("COMMITTED_FILES_FILE")

reviewer_slots: list[dict[str, object]] = []
if previous_reviews_dir is not None:
    reviewer_status_paths = sorted(glob.glob(str(previous_reviews_dir / "status_review_*.txt")))
    reviewer_slot_prefix = "review"
    if not reviewer_status_paths:
        reviewer_status_paths = sorted(glob.glob(str(previous_reviews_dir / "status_pass1_*.txt")))
        reviewer_slot_prefix = "pass1"
    for status_path_str in reviewer_status_paths:
        status_path = Path(status_path_str)
        slot_name = status_path.stem.removeprefix(f"status_{reviewer_slot_prefix}_")
        status_value = read_text(status_path).strip() or "unknown"
        log_text = read_text(previous_reviews_dir / f"{reviewer_slot_prefix}_{slot_name}.log")
        output_text = read_text(previous_reviews_dir / f"{reviewer_slot_prefix}_{slot_name}.txt")
        slot_state_fields = reviewer_last_slot_state_fields(log_text)
        retryable_failure_classes = reviewer_retryable_failure_classes(log_text, output_text, slot_state_fields)
        attempt_count = max_attempt_from_text(log_text)
        stall_kill_count = reviewer_stall_kill_count(log_text)
        stall_recovery_next_action = reviewer_stall_recovery_next_action(log_text)
        if attempt_count == 0 and status_value in {"success", "failed", "pr_closed", "skipped_unmapped", "skipped_budget"} and (log_text or output_text):
            attempt_count = 1
        cache_read_input_tokens_total, cache_read_input_tokens_available = reviewer_cache_read_input_tokens(
            log_text,
            attempt_count,
        )
        reviewer_slots.append({
            "slot": slot_name,
            "attempt_count": attempt_count,
            "status": status_value,
            "failure_class": reviewer_failure_class(status_value, log_text, output_text),
            "stall_kill_count": stall_kill_count,
            "stall_recovery_next_action": stall_recovery_next_action,
            "stall_recovered": bool(stall_kill_count and status_value == "success"),
            "retryable_failure_count": reviewer_retryable_failure_count(
                status_value,
                log_text,
                output_text,
                slot_state_fields,
                retryable_failure_classes,
            ),
            "retryable_failure_classes": retryable_failure_classes,
            "backoff_sleep_secs_total": reviewer_backoff_sleep_secs_total(log_text, slot_state_fields),
            "slot_retry_budget_exhausted": reviewer_slot_retry_budget_exhausted(log_text, output_text, slot_state_fields),
            "fallback_model_used": reviewer_fallback_model_used(log_text, slot_state_fields),
            "cache_status": reviewer_cache_status(log_text, slot_state_fields),
            "cache_reuse_attempted": reviewer_cache_reuse_attempted(log_text, slot_state_fields),
            "cache_read_input_tokens_total": cache_read_input_tokens_total,
            "cache_read_input_tokens_available": cache_read_input_tokens_available,
        })

reviewers_phase_state = "completed" if reviewer_slots else "skipped"

reviewer_bundle_file = env_path("REVIEWER_BUNDLE_FILE")
floor_tags_file = env_path("FLOOR_TAGS_FILE")
consolidator_raw_file = env_path("CONSOLIDATOR_RAW_FILE")
parser_stats_file = env_path("PARSER_STATS_FILE")
ledger_status_file = env_path("LEDGER_STATUS_FILE")

consolidator_enabled = env("REVIEW_CONSOLIDATOR_ENABLED", "1") != "0"
if not consolidator_enabled:
    consolidator_phase_state = "skipped"
    consolidator_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "disabled"}
elif file_exists(consolidator_raw_file):
    consolidator_phase_state = "completed"
    consolidator_slot = {
        "attempt_count": 1,
        "status": "success" if file_nonempty(consolidator_raw_file) else "failed",
        "failure_class": "none" if file_nonempty(consolidator_raw_file) else "empty_output",
    }
else:
    consolidator_phase_state = "skipped"
    consolidator_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "not_invoked"}

if file_exists(parser_stats_file):
    parser_phase_state = "completed"
    parser_slot = {"attempt_count": 1, "status": "success", "failure_class": "none"}
else:
    parser_phase_state = "skipped"
    parser_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "not_invoked"}

if file_exists(ledger_status_file):
    ledger_phase_state = "completed"
    ledger_slot = {"attempt_count": 1, "status": "success", "failure_class": "none"}
else:
    ledger_phase_state = "skipped"
    ledger_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "not_invoked"}

editor_attempt_numbers: set[int] = set()
if previous_reviews_dir is not None:
    for pattern in ("editor_attempt_*.txt", "editor_attempt_*.err", "editor_attempt_*_changes_lost.txt"):
        for path_str in glob.glob(str(previous_reviews_dir / pattern)):
            match = re.search(r"editor_attempt_(\d+)", Path(path_str).name)
            if match:
                editor_attempt_numbers.add(int(match.group(1)))
editor_attempt_count = max(editor_attempt_numbers) if editor_attempt_numbers else 0
editor_summary_exists = file_exists(editor_summary_file)

if bool_env("AUTOFIX_EDITOR_EMPTY_NOOP"):
    editor_phase_state = "completed"
    editor_slot = {"attempt_count": max(editor_attempt_count, 1), "status": "failed", "failure_class": "empty_noop"}
elif bool_env("EDITOR_CHANGES_LOST"):
    editor_phase_state = "completed"
    editor_slot = {"attempt_count": max(editor_attempt_count, 1), "status": "failed", "failure_class": "changes_lost"}
elif bool_env("EDITOR_NOOP_SUSPICIOUS"):
    editor_phase_state = "completed"
    # Additive classification (CLAUDE.md §6): "refusal" keeps
    # precedence, "recoverable_failure" is new for the
    # EDITOR_NOOP_RECOVERABLE_FAILURE flag set by the validator's
    # Check 1c (every editor attempt failed, no editor output),
    # and "unexpected_noop" stays the fallback value unchanged.
    if bool_env("EDITOR_NOOP_REFUSAL"):
        editor_noop_failure_class = "refusal"
    elif bool_env("EDITOR_NOOP_RECOVERABLE_FAILURE"):
        editor_noop_failure_class = "recoverable_failure"
    else:
        editor_noop_failure_class = "unexpected_noop"
    editor_slot = {
        "attempt_count": max(editor_attempt_count, 1),
        "status": "failed",
        "failure_class": editor_noop_failure_class,
    }
elif editor_attempt_count > 0 or editor_summary_exists:
    editor_phase_state = "completed"
    editor_slot = {"attempt_count": max(editor_attempt_count, 1), "status": "success", "failure_class": "none"}
elif bool_env("PR_CLOSED"):
    editor_phase_state = "skipped"
    editor_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "pr_closed"}
elif bool_env("MAX_ITERATIONS_REACHED"):
    editor_phase_state = "skipped"
    editor_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "max_iterations_reached"}
else:
    editor_phase_state = "skipped"
    editor_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "not_invoked"}

committed_files_text = read_text(committed_files_file)
commit_phase_observed = any(env(key).strip() for key in ("DID_COMMIT", "LEDGER_ONLY_COMMIT", "LEDGER_ONLY_COMMIT_STRICT")) or bool(committed_files_text.strip())

partial_finalize_validation_tail_can_complete = bool_env("AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE")
partial_finalize_edits_withheld_for_safety = bool_env("AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY")
partial_finalize_withheld_reason = env("AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON").strip() or "none"

if bool_env("DID_COMMIT"):
    commit_phase_state = "completed"
    commit_slot = {"attempt_count": 1, "status": "success", "failure_class": "none"}
elif "branch is not writable" in committed_files_text:
    commit_phase_state = "completed"
    commit_slot = {"attempt_count": 1, "status": "skipped", "failure_class": "not_writable"}
elif bool_env("MAX_ITERATIONS_REACHED"):
    commit_phase_state = "skipped"
    commit_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "max_iterations_reached"}
elif bool_env("PR_CLOSED"):
    commit_phase_state = "skipped"
    commit_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "pr_closed"}
elif bool_env("AUTOFIX_STALE_BASE_SKIP"):
    commit_phase_state = "skipped"
    commit_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "stale_base_skip"}
elif partial_finalize_edits_withheld_for_safety:
    commit_phase_state = "completed"
    commit_slot = {"attempt_count": 1, "status": "skipped", "failure_class": "withheld_for_safety"}
elif commit_phase_observed:
    commit_phase_state = "completed"
    commit_slot = {"attempt_count": 1, "status": "no_commit", "failure_class": "no_changes"}
else:
    commit_phase_state = "skipped"
    commit_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "not_invoked"}

edits_pushed = bool_env("AUTOFIX_EDITS_PUSHED")
push_allowed = bool_env("CAN_PUSH")
push_needed = bool_env("DID_COMMIT") or bool_env("CONFLICT_RESOLVED")
if edits_pushed:
    push_phase_state = "completed"
    push_slot = {"attempt_count": 1, "status": "success", "failure_class": "none"}
elif bool_env("AUTOFIX_STALE_BASE_SKIP") and push_needed:
    push_phase_state = "skipped"
    push_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "stale_base_skip"}
elif bool_env("PR_CLOSED"):
    push_phase_state = "skipped"
    push_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "pr_closed"}
elif partial_finalize_edits_withheld_for_safety:
    push_phase_state = "skipped"
    push_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "withheld_for_safety"}
elif push_needed and not push_allowed:
    push_phase_state = "skipped"
    push_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "push_not_allowed"}
elif push_needed:
    push_phase_state = "completed"
    push_slot = {"attempt_count": 1, "status": "failed", "failure_class": "push_failed"}
else:
    push_phase_state = "skipped"
    push_slot = {"attempt_count": 0, "status": "skipped", "failure_class": "not_needed"}

max_iterations_reached = bool_env("MAX_ITERATIONS_REACHED")
skip_judge = bool_env("SKIP_JUDGE")
judge_handled = bool_env("JUDGE_HANDLED")
judge_action = env("JUDGE_ACTION").strip()
judge_skip_reason = env("JUDGE_SKIP_REASON").strip()
rb_judge_status = env("RB_JUDGE_STATUS").strip()
partial_finalize = bool_env("AUTOFIX_PARTIAL_FINALIZE_REQUESTED")
partial_finalize_reason = env("AUTOFIX_PARTIAL_FINALIZE_REASON").strip()
partial_finalize_phase = env("AUTOFIX_PARTIAL_FINALIZE_PHASE").strip()
partial_comment_posted = bool_env("AUTOFIX_PARTIAL_COMMENT_POSTED")
resume_restored = bool_env("AUTOFIX_RESUME_RESTORED")
resume_head_sha = env("AUTOFIX_RESUME_HEAD_SHA").strip()
resume_round_limit = int_env("AUTOFIX_RESUME_ROUND_LIMIT")
resume_state = env("AUTOFIX_RESUME_STATE", "fresh").strip() or "fresh"
resume_should_continue = bool_env("AUTOFIX_RESUME_SHOULD_CONTINUE")
resume_source_reason = env("AUTOFIX_RESUME_REASON").strip()
resume_source_phase = env("AUTOFIX_RESUME_PHASE").strip()
resume_comment_posted = bool_env("AUTOFIX_RESUME_COMMENT_POSTED")
resume_completed_scope = csv_env_list("AUTOFIX_RESUME_COMPLETED_SCOPE")
resume_incomplete_scope = csv_env_list("AUTOFIX_RESUME_INCOMPLETE_SCOPE")

incomplete_phases: list[str] = list(resume_incomplete_scope)
if not incomplete_phases and partial_finalize:
    if partial_finalize_phase == "reviewers":
        incomplete_phases = ["reviewers", "consolidator", "parser", "ledger", "editor"]
    elif partial_finalize_phase == "editor":
        incomplete_phases = ["editor"]
    elif partial_finalize_phase:
        incomplete_phases = [partial_finalize_phase]

stall_recovery = {
    "killed_attempts": sum(int(slot.get("stall_kill_count", 0)) for slot in reviewer_slots),
    "advanced_slots": sum(1 for slot in reviewer_slots if str(slot.get("stall_recovery_next_action", "")).strip()),
    "recovered_slots": sum(1 for slot in reviewer_slots if bool(slot.get("stall_recovered"))),
    "partial_finalize_requested": bool(
        partial_finalize and partial_finalize_phase == "reviewers" and any(int(slot.get("stall_kill_count", 0)) > 0 for slot in reviewer_slots)
    ),
}

if not max_iterations_reached or skip_judge:
    rb_judge_phase_state = "skipped"
    rb_judge_slot = {
        "attempt_count": 0,
        "status": "skipped",
        "failure_class": "skip_judge" if skip_judge else "not_invoked",
    }
elif judge_handled:
    rb_judge_phase_state = "completed"
    rb_judge_slot = {"attempt_count": 1, "status": judge_action or "handled", "failure_class": "none"}
elif rb_judge_status == "failed":
    rb_judge_phase_state = "completed"
    rb_judge_slot = {"attempt_count": 1, "status": "failed", "failure_class": "judge_failed"}
elif judge_skip_reason:
    rb_judge_phase_state = "completed"
    rb_judge_slot = {"attempt_count": 1, "status": "skipped", "failure_class": judge_skip_reason}
else:
    rb_judge_phase_state = "completed"
    rb_judge_slot = {"attempt_count": 1, "status": "unhandled", "failure_class": "review_blocked"}


def determine_finalize_reason() -> str:
    if bool_env("PR_CLOSED"):
        return "pr_closed"
    if bool_env("AUTOFIX_STALE_BASE_SKIP"):
        return "stale_base_skip"
    if resume_state in {"no_progress", "round_budget_exhausted"} and not resume_should_continue:
        return resume_state
    if partial_finalize:
        return "partial_finalize"
    if bool_env("RESOLVER_ACTUATION_REQUIRED") and not bool_env("CONFLICT_RESOLVED"):
        return "conflict_resolver_failed"
    # Additive outcome (CLAUDE.md §6): the reviewer step failed, so the
    # editor never ran and its empty output is only the downstream symptom.
    if bool_env("AUTOFIX_REVIEWERS_FAILED"):
        return "reviewers_failed"
    if bool_env("AUTOFIX_EDITOR_EMPTY_NOOP"):
        return "editor_empty_noop"
    if bool_env("EDITOR_CHANGES_LOST"):
        redispatched = env("CHANGES_LOST_REDISPATCHED").strip()
        if redispatched == "true":
            return "editor_changes_lost_redispatched"
        if redispatched == "skipped_peer_inflight":
            return "editor_changes_lost_peer_inflight"
        return "editor_changes_lost"
    if bool_env("EDITOR_NOOP_SUSPICIOUS") and bool_env("EDITOR_NOOP_REFUSAL"):
        return "editor_noop_refusal"
    # Additive outcome (CLAUDE.md §6): refusal keeps precedence
    # above; the generic editor_noop_suspicious value below is
    # unchanged for runs without either specific flag.
    if bool_env("EDITOR_NOOP_SUSPICIOUS") and bool_env("EDITOR_NOOP_RECOVERABLE_FAILURE"):
        return "editor_noop_recoverable_failure"
    if bool_env("EDITOR_NOOP_SUSPICIOUS"):
        return "editor_noop_suspicious"
    if max_iterations_reached and not skip_judge:
        if judge_handled:
            return f"rb_judge_{judge_action or 'handled'}"
        if rb_judge_status == "failed":
            return "rb_judge_failed"
        if judge_skip_reason:
            return f"rb_judge_skip_{judge_skip_reason}"
        return "rb_judge_review_blocked"
    if push_needed and not push_allowed:
        return "push_not_allowed"
    if push_needed and not edits_pushed:
        return "push_failed"
    if bool_env("LEDGER_ONLY_COMMIT_STRICT"):
        return "clean_review_ledger_only"
    if bool_env("LEDGER_ONLY_COMMIT"):
        return "clean_review_converged"
    if edits_pushed and bool_env("CONFLICT_RESOLVED"):
        return "conflict_resolved_pushed"
    if edits_pushed and bool_env("DID_COMMIT"):
        return "autofix_pushed"
    return "clean_review_no_commit"


finalize_reason = determine_finalize_reason()
phase_order = [
    ("reviewers", reviewers_phase_state),
    ("consolidator", consolidator_phase_state),
    ("parser", parser_phase_state),
    ("ledger", ledger_phase_state),
    ("editor", editor_phase_state),
    ("commit", commit_phase_state),
    ("push", push_phase_state),
    ("rb_judge", rb_judge_phase_state),
    ("finalize", "completed"),
]

summary = {
    "completed_phases": [name for name, state in phase_order if state == "completed"],
    "skipped_phases": [name for name, state in phase_order if state == "skipped"],
    "slot_results": {
        "reviewers": reviewer_slots,
        "consolidator": consolidator_slot,
        "parser": parser_slot,
        "ledger": ledger_slot,
        "editor": editor_slot,
        "commit": commit_slot,
        "push": push_slot,
        "rb_judge": rb_judge_slot,
        "finalize": {"attempt_count": 1, "status": "emitted", "failure_class": "none"},
    },
    "budget_elapsed_secs": int_env("BUDGET_ELAPSED_SECS"),
    "budget_total_secs": int_env("BUDGET_TOTAL_SECS"),
    "budget_remaining_secs": int_env("BUDGET_REMAINING_SECS"),
    "stall_recovery": stall_recovery,
    "finalize_reason": finalize_reason,
    "partial_finalize": partial_finalize,
    "partial_finalize_reason": partial_finalize_reason,
    "partial_finalize_phase": partial_finalize_phase,
    "partial_finalize_validation_tail_can_complete": partial_finalize_validation_tail_can_complete,
    "partial_finalize_edits_withheld_for_safety": partial_finalize_edits_withheld_for_safety,
    "partial_finalize_withheld_reason": partial_finalize_withheld_reason,
    "partial_comment_posted": partial_comment_posted,
    "incomplete_phases": incomplete_phases,
    "edits_pushed": edits_pushed,
    "resume_round": int_env("RESUME_ROUND_SUMMARY"),
    "resume_restored": resume_restored,
    "resume_head_sha": resume_head_sha,
    "resume_round_limit": resume_round_limit,
    "resume_state": resume_state,
    "resume_should_continue": resume_should_continue,
    "resume_source_reason": resume_source_reason,
    "resume_source_phase": resume_source_phase,
    "resume_comment_posted": resume_comment_posted,
    "resume_completed_scope": resume_completed_scope,
    "resume_incomplete_scope": resume_incomplete_scope,
}

print("REVIEW_AUTOFIX_RUN_SUMMARY_V1 " + json.dumps(summary, sort_keys=True, separators=(",", ":")))
PY
)"

if [ -z "${structured_summary_line}" ]; then
  structured_summary_line='REVIEW_AUTOFIX_RUN_SUMMARY_V1 {"completed_phases":["finalize"],"skipped_phases":["reviewers","consolidator","parser","ledger","editor","commit","push","rb_judge"],"slot_results":{"reviewers":[],"consolidator":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"parser":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"ledger":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"editor":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"commit":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"push":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"rb_judge":{"attempt_count":0,"status":"skipped","failure_class":"summary_emit_failed"},"finalize":{"attempt_count":1,"status":"emitted","failure_class":"none"}},"budget_elapsed_secs":0,"budget_total_secs":0,"budget_remaining_secs":0,"stall_recovery":{"advanced_slots":0,"killed_attempts":0,"partial_finalize_requested":false,"recovered_slots":0},"finalize_reason":"summary_emit_failed","partial_finalize":false,"partial_finalize_reason":"","partial_finalize_phase":"","partial_finalize_validation_tail_can_complete":false,"partial_finalize_edits_withheld_for_safety":false,"partial_finalize_withheld_reason":"none","partial_comment_posted":false,"incomplete_phases":[],"edits_pushed":false,"resume_round":0,"resume_restored":false,"resume_head_sha":"","resume_round_limit":0,"resume_state":"fresh","resume_should_continue":false,"resume_source_reason":"","resume_source_phase":"","resume_comment_posted":false,"resume_completed_scope":[],"resume_incomplete_scope":[]}'
fi

{
  echo "### Review Pipeline — Iteration ${iteration_label}"
  echo
  echo "| Metric | Value |"
  echo "|---|---|"
  echo "| Reviewers run | ${reviewers_run} |"
  echo "| Reviewer scope | ${reviewer_scope_label} |"
  echo "| Raw bundle size (bytes) | ${bundle_bytes} |"
  echo "| Floor tags | ${floor_tag_count} |"
  echo "| Consolidator model | ${REVIEW_CONSOLIDATOR_MODEL:-openai/gpt-6-sol} |"
  echo "| Consolidator invoked | ${consolidator_invoked} |"
  echo "| Consolidator output bytes | ${consolidator_output_bytes} |"
  echo "| Parsed issue blocks | ${parsed_blocks} |"
  echo "| Passthrough blocks | ${passthrough_blocks} |"
  echo "| Line-unverified blocks | ${line_unverified} |"
  echo "| Ledger entries total | ${ledger_total} |"
  echo "| NEW | ${ledger_new} |"
  echo "| PERSISTING | ${ledger_persisting} |"
  echo "| FIXED | ${ledger_fixed} |"
  echo "| RESURGENT | ${ledger_resurgent} |"
  echo "| accepted-residual | ${ledger_accepted_residual} |"
  echo "| Editor invoked | ${editor_invoked} |"
  echo "| CONSOLIDATOR_OVERRIDDEN count | ${override_count} |"
  echo "| Editor commit produced | ${editor_commit_produced} |"
  echo "| Budget elapsed (secs) | ${budget_elapsed_secs} |"
  echo "| Budget total (secs) | ${budget_total_secs} |"
  echo "| Budget remaining (secs) | ${budget_remaining_secs} |"
  echo "| Partial validation tail can complete | ${partial_finalize_validation_tail_can_complete} |"
  echo "| Partial edits withheld for safety | ${partial_finalize_edits_withheld_for_safety} |"
  echo "| Partial withheld reason | ${partial_finalize_withheld_reason} |"
  echo "| Resume round | ${resume_round} |"
  echo "| Resume restored | ${resume_restored} |"
  echo "| Resume state | ${resume_state} |"
  echo "| Resume round limit | ${resume_round_limit} |"
  echo "| Resume should continue | ${resume_should_continue} |"
  echo "| Resume head SHA | ${resume_head_sha:-none} |"
  echo
  echo "**Acceptance deltas vs prior iteration:**"
  echo "${reviewer_scope_note}"
  if [ "${consolidator_invoked}" = "failed" ]; then
    echo "- Consolidator output was empty while REVIEW_CONSOLIDATOR_ENABLED=${REVIEW_CONSOLIDATOR_ENABLED:-1}; downstream parser/editor stayed fail-open on local artifacts."
  else
    echo "- none"
  fi
  echo
} >> "${GITHUB_STEP_SUMMARY}"

printf '\n### Review Autofix Run Summary\n\n' >> "${GITHUB_STEP_SUMMARY}"
printf '%s\n' "${structured_summary_line}" | tee -a "${GITHUB_STEP_SUMMARY}"
# Keep the structured line for the heal reporter below (RUNTIME_DIR
# survives until the cleanup step). Best-effort: never fail the
# summary over it.
printf '%s\n' "${structured_summary_line}" > "${RUNTIME_DIR}/review_autofix_run_summary_line.txt" 2>/dev/null || true
