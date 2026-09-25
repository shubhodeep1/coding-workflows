#!/usr/bin/env bash
# Post the partial finalize comment and persist the review runtime marker.
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

current_head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
if [ -z "${current_head_sha}" ]; then
  echo "::warning::Unable to resolve git HEAD for partial finalize marker; restoring same-head resume may fail open on the next run."
fi

previous_resume_round="${AUTOFIX_RESUME_ROUND:-0}"
if ! [[ "${previous_resume_round}" =~ ^[0-9]+$ ]]; then
  previous_resume_round=0
fi
previous_progress_fingerprint="${AUTOFIX_RESUME_PROGRESS_FINGERPRINT:-}"
previous_resume_head_sha="${AUTOFIX_RESUME_HEAD_SHA:-}"
if [ -n "${current_head_sha}" ] && [ -n "${previous_resume_head_sha}" ] && [ "${previous_resume_head_sha}" != "${current_head_sha}" ]; then
  previous_resume_round=0
  previous_progress_fingerprint=""
fi

resume_round="$((previous_resume_round + 1))"
resume_round_limit="${AUTOFIX_RESUME_ROUND_LIMIT:-${REVIEW_MAX_RESUME_ROUNDS:-3}}"
if ! [[ "${resume_round_limit}" =~ ^[1-9][0-9]*$ ]]; then
  echo "::warning::Invalid REVIEW_MAX_RESUME_ROUNDS='${resume_round_limit}'; falling back to 3."
  resume_round_limit=3
fi

completed_scope_items=()
incomplete_scope_items=()
if [ "${AUTOFIX_PARTIAL_FINALIZE_PHASE:-}" = "reviewers" ]; then
  if compgen -G "${PREVIOUS_REVIEWS_DIR}/status_pass1_*.txt" >/dev/null; then
    completed_scope_items+=(reviewers_pass1)
    if [ -s "${PREVIOUS_REVIEWS_DIR}/consensus_pass1.txt" ]; then
      completed_scope_items+=(reviewers_pass1_consensus)
    fi
    incomplete_scope_items+=(reviewers_pass2)
  elif [ "${ENABLE_REVIEWER_TWO_PASS:-true}" = "false" ] && compgen -G "${PREVIOUS_REVIEWS_DIR}/status_review_*.txt" >/dev/null; then
    completed_scope_items+=(reviewers_started)
    incomplete_scope_items+=(reviewers)
  else
    if [ "${ENABLE_REVIEWER_TWO_PASS:-true}" = "false" ]; then
      incomplete_scope_items+=(reviewers)
    else
      incomplete_scope_items+=(reviewers_pass1 reviewers_pass2)
    fi
  fi
  incomplete_scope_items+=(consolidator parser ledger editor)
else
  if compgen -G "${PREVIOUS_REVIEWS_DIR}/status_review_*.txt" >/dev/null || compgen -G "${PREVIOUS_REVIEWS_DIR}/status_pass1_*.txt" >/dev/null; then
    completed_scope_items+=(reviewers)
  fi
  [ -f "${RUNTIME_DIR}/consolidator_raw.txt" ] && completed_scope_items+=(consolidator)
  [ -f "${RUNTIME_DIR}/parser_stats.txt" ] && completed_scope_items+=(parser)
  [ -f "${RUNTIME_DIR}/ledger_status.txt" ] && completed_scope_items+=(ledger)
  incomplete_scope_items+=(editor)
fi

if [ "${#completed_scope_items[@]}" -gt 0 ]; then
  completed_scope_csv="$(IFS=,; echo "${completed_scope_items[*]}")"
else
  completed_scope_csv="none"
fi
if [ "${#incomplete_scope_items[@]}" -gt 0 ]; then
  incomplete_scope_csv="$(IFS=,; echo "${incomplete_scope_items[*]}")"
else
  incomplete_scope_csv="none"
fi
completed_scope_human="$(printf '%s' "${completed_scope_csv}" | sed 's/,/, /g')"
incomplete_scope_human="$(printf '%s' "${incomplete_scope_csv}" | sed 's/,/, /g')"

validated_edits_committed="false"
if [ "${EDITOR_COMMIT_PRODUCED:-false}" = "true" ] || [ "${DID_COMMIT:-false}" = "true" ] || [ "${CONFLICT_RESOLVED:-false}" = "true" ]; then
  validated_edits_committed="true"
fi
edits_pushed="${AUTOFIX_EDITS_PUSHED:-false}"
validation_tail_can_complete="${AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE:-true}"
edits_withheld_for_safety="${AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY:-false}"
withheld_reason="${AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON:-none}"
partial_comment_posted="false"

partial_publish_note="This run finalized early after \`${AUTOFIX_PARTIAL_FINALIZE_REASON:-unknown}\` in the \`${AUTOFIX_PARTIAL_FINALIZE_PHASE:-unknown}\` phase so the validated finalize tail could complete before the hard job timeout."
if [ "${edits_withheld_for_safety}" = "true" ]; then
  partial_publish_note="This run finalized early after \`${AUTOFIX_PARTIAL_FINALIZE_REASON:-unknown}\` in the \`${AUTOFIX_PARTIAL_FINALIZE_PHASE:-unknown}\` phase. The remaining job headroom could not cover the existing validation/publish tail, so the workflow posted findings only and withheld any unpushed edits for safety."
fi

resume_eval_file="$(mktemp)"
CURRENT_HEAD_SHA="${current_head_sha}" \
PREVIOUS_PROGRESS_FINGERPRINT="${previous_progress_fingerprint}" \
PREVIOUS_REVIEWS_DIR_PATH="${PREVIOUS_REVIEWS_DIR:-}" \
RUNTIME_DIR_PATH="${RUNTIME_DIR:-}" \
EDITOR_SUMMARY_FILE_PATH="${EDITOR_SUMMARY_FILE:-}" \
COMMITTED_FILES_FILE_PATH="${COMMITTED_FILES_FILE:-}" \
COMPLETED_SCOPE_CSV="${completed_scope_csv}" \
INCOMPLETE_SCOPE_CSV="${incomplete_scope_csv}" \
RESUME_ROUND_VALUE="${resume_round}" \
RESUME_ROUND_LIMIT_VALUE="${resume_round_limit}" \
PARTIAL_PHASE="${AUTOFIX_PARTIAL_FINALIZE_PHASE:-unknown}" \
PARTIAL_REASON="${AUTOFIX_PARTIAL_FINALIZE_REASON:-unknown}" \
VALIDATED_EDITS_COMMITTED="${validated_edits_committed}" \
EDITS_PUSHED="${edits_pushed}" \
VALIDATION_TAIL_CAN_COMPLETE="${validation_tail_can_complete}" \
EDITS_WITHHELD_FOR_SAFETY="${edits_withheld_for_safety}" \
WITHHELD_REASON="${withheld_reason}" \
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY' > "${resume_eval_file}"
from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path


def parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def describe_path(path_str: str, artifact_label: str) -> dict[str, object]:
    path = Path(path_str)
    entry: dict[str, object] = {"path": artifact_label, "exists": path.exists()}
    if not path.exists():
        return entry
    try:
        data = path.read_bytes()
    except OSError:
        entry["read_error"] = True
        return entry
    entry["size"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    return entry


artifacts: list[dict[str, object]] = []
previous_reviews_dir = os.environ.get("PREVIOUS_REVIEWS_DIR_PATH", "").strip()
runtime_dir = os.environ.get("RUNTIME_DIR_PATH", "").strip()
for pattern in (
    "status_pass1_*.txt",
    "status_review_*.txt",
    "pass1_*.txt",
    "review_*.txt",
    "consensus_pass1.txt",
):
    if previous_reviews_dir:
        for matched_path in sorted(glob.glob(str(Path(previous_reviews_dir) / pattern))):
            artifacts.append(describe_path(matched_path, f"previous_reviews/{Path(matched_path).name}"))
for rel_path in (
    "reviewer_consensus.txt",
    "floor_tags.txt",
    "consolidator_raw.txt",
    "parser_stats.txt",
    "review_issues.txt",
    "ledger_status.txt",
):
    if runtime_dir:
        artifacts.append(describe_path(str(Path(runtime_dir) / rel_path), f"runtime/{rel_path}"))
for env_name, artifact_label in (
    ("EDITOR_SUMMARY_FILE_PATH", "runtime/editor_summary.txt"),
    ("COMMITTED_FILES_FILE_PATH", "runtime/committed_files.txt"),
):
    candidate = os.environ.get(env_name, "").strip()
    if candidate:
        artifacts.append(describe_path(candidate, artifact_label))

resume_round = 0
try:
    resume_round = int(os.environ.get("RESUME_ROUND_VALUE", "0") or 0)
except ValueError:
    resume_round = 0
if resume_round < 0:
    resume_round = 0

resume_round_limit = 3
try:
    resume_round_limit = int(os.environ.get("RESUME_ROUND_LIMIT_VALUE", "3") or 3)
except ValueError:
    resume_round_limit = 3
if resume_round_limit <= 0:
    resume_round_limit = 3

fingerprint_payload = {
    "head_sha": os.environ.get("CURRENT_HEAD_SHA", "").strip(),
    "completed_scope": os.environ.get("COMPLETED_SCOPE_CSV", "").strip(),
    "incomplete_scope": os.environ.get("INCOMPLETE_SCOPE_CSV", "").strip(),
    "validated_edits_committed": parse_bool(os.environ.get("VALIDATED_EDITS_COMMITTED", "false")),
    "edits_pushed": parse_bool(os.environ.get("EDITS_PUSHED", "false")),
    "validation_tail_can_complete": parse_bool(os.environ.get("VALIDATION_TAIL_CAN_COMPLETE", "false")),
    "edits_withheld_for_safety": parse_bool(os.environ.get("EDITS_WITHHELD_FOR_SAFETY", "false")),
    "withheld_reason": os.environ.get("WITHHELD_REASON", "").strip() or "none",
    "artifacts": artifacts,
}
progress_fingerprint = hashlib.sha256(
    json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

previous_progress_fingerprint = os.environ.get("PREVIOUS_PROGRESS_FINGERPRINT", "").strip()
resume_state = "resumable"
resume_should_continue = True
if previous_progress_fingerprint and previous_progress_fingerprint == progress_fingerprint:
    resume_state = "no_progress"
    resume_should_continue = False
elif resume_round >= resume_round_limit:
    resume_state = "round_budget_exhausted"
    resume_should_continue = False

print(f"PROGRESS_FINGERPRINT={progress_fingerprint}")
print(f"RESUME_STATE={resume_state}")
print(f"RESUME_SHOULD_CONTINUE={'true' if resume_should_continue else 'false'}")
print(f"RESUME_TERMINAL={'true' if not resume_should_continue else 'false'}")
print(f"TERMINAL_REASON={resume_state if not resume_should_continue else ''}")
PY
set -a
# shellcheck disable=SC1090
source "${resume_eval_file}"
set +a
rm -f "${resume_eval_file}"

{
  echo "AUTOFIX_RESUME_ROUND=${resume_round}"
  echo "AUTOFIX_RESUME_ROUND_LIMIT=${resume_round_limit}"
  echo "AUTOFIX_RESUME_HEAD_SHA=${current_head_sha}"
  echo "AUTOFIX_RESUME_COMPLETED_SCOPE=${completed_scope_csv}"
  echo "AUTOFIX_RESUME_INCOMPLETE_SCOPE=${incomplete_scope_csv}"
  echo "AUTOFIX_RESUME_PROGRESS_FINGERPRINT=${PROGRESS_FINGERPRINT}"
  echo "AUTOFIX_RESUME_STATE=${RESUME_STATE}"
  echo "AUTOFIX_RESUME_SHOULD_CONTINUE=${RESUME_SHOULD_CONTINUE}"
  echo "AUTOFIX_RESUME_TERMINAL=${RESUME_TERMINAL}"
  echo "AUTOFIX_RESUME_REASON=${AUTOFIX_PARTIAL_FINALIZE_REASON:-unknown}"
  echo "AUTOFIX_RESUME_PHASE=${AUTOFIX_PARTIAL_FINALIZE_PHASE:-unknown}"
} >> "$GITHUB_ENV"

resume_state_human="$(printf '%s' "${RESUME_STATE}" | tr '_' ' ')"
if [ "${RESUME_SHOULD_CONTINUE}" = "true" ]; then
  resume_state_note="Another same-head continuation run can resume from this marker."
else
  resume_state_note="Same-head resume stops here because ${resume_state_human}."
fi

comment_body_file="$(mktemp)"

cat > "${comment_body_file}" <<EOF
<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->
**AI review/autofix partial finalize**

${partial_publish_note}

${resume_state_note}

- Completed scope: ${completed_scope_human}
- Incomplete scope: ${incomplete_scope_human}
- Validated edits committed: ${validated_edits_committed}
- Edits pushed: ${edits_pushed}
- Validation tail can complete: ${validation_tail_can_complete}
- Edits withheld for safety: ${edits_withheld_for_safety}
- Withheld reason: ${withheld_reason}
- Resume state: ${RESUME_STATE}
- Resume round: ${resume_round} of ${resume_round_limit}
- Same-head continuation: ${RESUME_SHOULD_CONTINUE}
- Head SHA: ${current_head_sha:-unknown}
- Run: ${WORKFLOW_RUN_URL}

\`\`\`text
partial_finalize=true
reason=${AUTOFIX_PARTIAL_FINALIZE_REASON:-unknown}
phase=${AUTOFIX_PARTIAL_FINALIZE_PHASE:-unknown}
completed_scope=${completed_scope_csv}
incomplete_scope=${incomplete_scope_csv}
validated_edits_committed=${validated_edits_committed}
edits_pushed=${edits_pushed}
validation_tail_can_complete=${validation_tail_can_complete}
edits_withheld_for_safety=${edits_withheld_for_safety}
withheld_reason=${withheld_reason}
head_sha=${current_head_sha}
resume_round=${resume_round}
resume_round_limit=${resume_round_limit}
resume_state=${RESUME_STATE}
resume_should_continue=${RESUME_SHOULD_CONTINUE}
\`\`\`
EOF

if gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="$(cat "${comment_body_file}")" >/dev/null 2>&1; then
  partial_comment_posted="true"
else
  echo "::warning::Unable to post partial finalize comment after retries."
fi
rm -f "${comment_body_file}"

{
  echo "AUTOFIX_PARTIAL_COMMENT_POSTED=${partial_comment_posted}"
  echo "AUTOFIX_RESUME_COMMENT_POSTED=${partial_comment_posted}"
} >> "$GITHUB_ENV"

partial_marker_dir=".ai/review_runtime/pr-${PR_NUMBER}/round-${resume_round}"
mkdir -p "${partial_marker_dir}"
partial_marker_file="${partial_marker_dir}/partial_finalize.json"
PARTIAL_MARKER_FILE="${partial_marker_file}" \
PARTIAL_COMMENT_POSTED="${partial_comment_posted}" \
CURRENT_HEAD_SHA="${current_head_sha}" \
COMPLETED_SCOPE_CSV="${completed_scope_csv}" \
INCOMPLETE_SCOPE_CSV="${incomplete_scope_csv}" \
VALIDATED_EDITS_COMMITTED="${validated_edits_committed}" \
EDITS_PUSHED="${edits_pushed}" \
VALIDATION_TAIL_CAN_COMPLETE="${validation_tail_can_complete}" \
EDITS_WITHHELD_FOR_SAFETY="${edits_withheld_for_safety}" \
WITHHELD_REASON="${withheld_reason}" \
RESUME_ROUND_VALUE="${resume_round}" \
RESUME_ROUND_LIMIT_VALUE="${resume_round_limit}" \
RESUME_STATE_VALUE="${RESUME_STATE}" \
RESUME_SHOULD_CONTINUE_VALUE="${RESUME_SHOULD_CONTINUE}" \
PROGRESS_FINGERPRINT_VALUE="${PROGRESS_FINGERPRINT}" \
PREVIOUS_REVIEWS_DIR_PATH="${PREVIOUS_REVIEWS_DIR:-}" \
RUNTIME_DIR_PATH="${RUNTIME_DIR:-}" \
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def parse_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def parse_csv(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw or raw == "none":
        return []
    return [item for item in raw.split(",") if item]


def optional_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def copy_artifacts(source_paths: list[Path], destination_dir: Path) -> None:
    copied_any = False
    for source_path in source_paths:
        if not source_path.is_file():
            continue
        if not copied_any:
            destination_dir.mkdir(parents=True, exist_ok=True)
            copied_any = True
        shutil.copy2(source_path, destination_dir / source_path.name)


partial_marker_path = Path(os.environ["PARTIAL_MARKER_FILE"])
partial_marker_dir = partial_marker_path.parent
previous_reviews_dir = optional_path("PREVIOUS_REVIEWS_DIR_PATH")
runtime_dir = optional_path("RUNTIME_DIR_PATH")

previous_review_artifacts: list[Path] = []
if previous_reviews_dir is not None:
    for pattern in (
        "status_pass1_*.txt",
        "status_review_*.txt",
        "pass1_*.txt",
        "review_*.txt",
        "consensus_pass1.txt",
    ):
        previous_review_artifacts.extend(sorted(previous_reviews_dir.glob(pattern)))
copy_artifacts(previous_review_artifacts, partial_marker_dir / "previous_reviews")

runtime_artifacts: list[Path] = []
if runtime_dir is not None:
    for file_name in (
        "reviewer_consensus.txt",
        "floor_tags.txt",
        "consolidator_raw.txt",
        "parser_stats.txt",
        "review_issues.txt",
        "ledger_status.txt",
        "editor_summary.txt",
        "committed_files.txt",
    ):
        runtime_artifacts.append(runtime_dir / file_name)
copy_artifacts(runtime_artifacts, partial_marker_dir / "runtime")

payload = {
    "schema_version": 1,
    "partial_finalize": True,
    "reason": os.environ.get("AUTOFIX_PARTIAL_FINALIZE_REASON", "").strip() or "unknown",
    "phase": os.environ.get("AUTOFIX_PARTIAL_FINALIZE_PHASE", "").strip() or "unknown",
    "head_sha": os.environ.get("CURRENT_HEAD_SHA", "").strip(),
    "completed_scope": parse_csv("COMPLETED_SCOPE_CSV"),
    "incomplete_scope": parse_csv("INCOMPLETE_SCOPE_CSV"),
    "validated_edits_committed": parse_bool("VALIDATED_EDITS_COMMITTED"),
    "edits_pushed": parse_bool("EDITS_PUSHED"),
    "validation_tail_can_complete": parse_bool("VALIDATION_TAIL_CAN_COMPLETE"),
    "edits_withheld_for_safety": parse_bool("EDITS_WITHHELD_FOR_SAFETY"),
    "withheld_reason": os.environ.get("WITHHELD_REASON", "").strip() or "none",
    "resume_round": int(os.environ.get("RESUME_ROUND_VALUE", "0") or 0),
    "resume_round_limit": int(os.environ.get("RESUME_ROUND_LIMIT_VALUE", "0") or 0),
    "resume_state": os.environ.get("RESUME_STATE_VALUE", "").strip() or "resumable",
    "resume_should_continue": parse_bool("RESUME_SHOULD_CONTINUE_VALUE"),
    "progress_fingerprint": os.environ.get("PROGRESS_FINGERPRINT_VALUE", "").strip(),
    "comment_posted": parse_bool("PARTIAL_COMMENT_POSTED"),
}
partial_marker_path.write_text(
    json.dumps(payload, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
echo "AUTOFIX_PARTIAL_MARKER_FILE=${partial_marker_file}" >> "$GITHUB_ENV"
echo "AUTOFIX_RESUME_MARKER_FILE=${partial_marker_file}" >> "$GITHUB_ENV"
