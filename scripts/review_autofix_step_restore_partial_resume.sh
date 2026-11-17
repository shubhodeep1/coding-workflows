#!/usr/bin/env bash
# Body of the "Restore same-head partial resume state" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

current_head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
if [ -z "${current_head_sha}" ]; then
  echo "::warning::Unable to resolve git HEAD for partial-resume restore; continuing without same-head resume state."
fi

resume_round_limit="${REVIEW_MAX_RESUME_ROUNDS:-3}"
if ! [[ "${resume_round_limit}" =~ ^[1-9][0-9]*$ ]]; then
  echo "::warning::Invalid REVIEW_MAX_RESUME_ROUNDS='${resume_round_limit}'; falling back to 3."
  resume_round_limit=3
fi

resume_env_file="$(mktemp)"
CURRENT_HEAD_SHA="${current_head_sha}" \
CURRENT_BASE_REF="${RETARGETED_BASE_REF:-}" \
CURRENT_BASE_UPDATED_AT="${RETARGETED_BASE_UPDATED_AT:-}" \
CURRENT_BASE_RETARGETED="${RETARGETED_BASE_THIS_RUN:-false}" \
RESUME_ROUND_LIMIT="${resume_round_limit}" \
CURRENT_PREVIOUS_REVIEWS_DIR="${PREVIOUS_REVIEWS_DIR:-}" \
CURRENT_RUNTIME_DIR="${RUNTIME_DIR:-}" \
PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY' > "${resume_env_file}"
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def bool_to_env(value: bool) -> str:
    return "true" if value else "false"


def parse_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    parsed: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            parsed.append(item.strip())
    return parsed


def parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def sanitize_round(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def optional_path(value: str) -> Path | None:
    value = value.strip()
    return Path(value) if value else None


def restore_cached_artifacts(source_dir: Path | None, destination_dir: Path | None) -> int:
    restored = 0
    if source_dir is None or destination_dir is None or not source_dir.is_dir():
        return restored
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"::warning::Could not prepare resume artifact directory {destination_dir}: {exc}",
            file=sys.stderr,
        )
        return restored
    for source_path in sorted(source_dir.iterdir()):
        if not source_path.is_file():
            continue
        try:
            shutil.copy2(source_path, destination_dir / source_path.name)
        except OSError as exc:
            print(
                f"::warning::Could not restore same-head artifact {source_path} -> {destination_dir / source_path.name}: {exc}",
                file=sys.stderr,
            )
            continue
        restored += 1
    return restored


current_head_sha = os.environ.get("CURRENT_HEAD_SHA", "").strip()
current_base_ref = os.environ.get("CURRENT_BASE_REF", "").strip()
current_base_updated_at = os.environ.get("CURRENT_BASE_UPDATED_AT", "").strip()
current_base_retargeted = os.environ.get("CURRENT_BASE_RETARGETED") == "true"
resume_round_limit = os.environ.get("RESUME_ROUND_LIMIT", "3").strip() or "3"
pr_number = os.environ.get("PR_NUMBER", "").strip() or "0"
marker_root = Path(f".ai/review_runtime/pr-{pr_number}")
current_previous_reviews_dir = optional_path(os.environ.get("CURRENT_PREVIOUS_REVIEWS_DIR", ""))
current_runtime_dir = optional_path(os.environ.get("CURRENT_RUNTIME_DIR", ""))

selected_payload: dict[str, object] | None = None
selected_marker: Path | None = None
selected_sort_key: tuple[int, int, str] = (-1, -1, "")
if current_head_sha and marker_root.is_dir():
    for marker_path in sorted(marker_root.glob("round-*/partial_finalize.json")):
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        head_sha = payload.get("head_sha")
        if not isinstance(head_sha, str) or head_sha.strip() != current_head_sha:
            continue
        marker_base = payload.get("base_ref")
        if marker_base and marker_base != current_base_ref:
            continue
        # Cache restoration can change mtime, and PR updated_at is
        # not a base-change timestamp. Legacy state is unverifiable.
        if not marker_base:
            continue
        resume_round = sanitize_round(payload.get("resume_round"))
        try:
            marker_mtime_ns = marker_path.stat().st_mtime_ns
        except OSError:
            marker_mtime_ns = -1
        sort_key = (resume_round, marker_mtime_ns, marker_path.as_posix())
        if sort_key > selected_sort_key:
            selected_sort_key = sort_key
            selected_payload = payload
            selected_marker = marker_path

exports = {
    "AUTOFIX_RESUME_RESTORED": "false",
    "AUTOFIX_RESUME_TERMINAL": "false",
    "AUTOFIX_RESUME_ROUND": "0",
    "AUTOFIX_RESUME_ROUND_LIMIT": resume_round_limit,
    "AUTOFIX_RESUME_STATE": "fresh",
    "AUTOFIX_RESUME_SHOULD_CONTINUE": "false",
    "AUTOFIX_RESUME_HEAD_SHA": current_head_sha,
    "AUTOFIX_RESUME_COMPLETED_SCOPE": "",
    "AUTOFIX_RESUME_INCOMPLETE_SCOPE": "",
    "AUTOFIX_RESUME_PROGRESS_FINGERPRINT": "",
    "AUTOFIX_RESUME_REASON": "",
    "AUTOFIX_RESUME_PHASE": "",
    "AUTOFIX_RESUME_COMMENT_POSTED": "false",
    "AUTOFIX_RESUME_MARKER_FILE": "",
    "AUTOFIX_RESUME_RESTORED_ARTIFACT_COUNT": "0",
}

if selected_payload is not None and selected_marker is not None:
    resume_state = selected_payload.get("resume_state")
    if not isinstance(resume_state, str) or not resume_state.strip():
        resume_state = "resumable"
    resume_state = resume_state.strip()
    resume_should_continue = parse_bool(
        selected_payload.get("resume_should_continue"),
        default=(resume_state == "resumable"),
    )
    restored_artifact_count = 0
    selected_marker_root = selected_marker.parent
    restored_artifact_count += restore_cached_artifacts(
        selected_marker_root / "previous_reviews",
        current_previous_reviews_dir,
    )
    restored_artifact_count += restore_cached_artifacts(
        selected_marker_root / "runtime",
        current_runtime_dir,
    )
    exports.update(
        {
            "AUTOFIX_RESUME_RESTORED": "true",
            "AUTOFIX_RESUME_TERMINAL": bool_to_env(not resume_should_continue),
            "AUTOFIX_RESUME_ROUND": str(sanitize_round(selected_payload.get("resume_round"))),
            "AUTOFIX_RESUME_STATE": resume_state,
            "AUTOFIX_RESUME_SHOULD_CONTINUE": bool_to_env(resume_should_continue),
            "AUTOFIX_RESUME_COMPLETED_SCOPE": ",".join(parse_list(selected_payload.get("completed_scope"))),
            "AUTOFIX_RESUME_INCOMPLETE_SCOPE": ",".join(parse_list(selected_payload.get("incomplete_scope"))),
            "AUTOFIX_RESUME_PROGRESS_FINGERPRINT": str(selected_payload.get("progress_fingerprint", "") or ""),
            "AUTOFIX_RESUME_REASON": str(selected_payload.get("reason", "") or ""),
            "AUTOFIX_RESUME_PHASE": str(selected_payload.get("phase", "") or ""),
            "AUTOFIX_RESUME_COMMENT_POSTED": bool_to_env(parse_bool(selected_payload.get("comment_posted"))),
            "AUTOFIX_RESUME_MARKER_FILE": selected_marker.as_posix(),
            "AUTOFIX_RESUME_RESTORED_ARTIFACT_COUNT": str(restored_artifact_count),
        }
    )

for key, value in exports.items():
    print(f"{key}={value}")
PY

cat "${resume_env_file}" >> "$GITHUB_ENV"

if grep -q '^AUTOFIX_RESUME_RESTORED=true$' "${resume_env_file}"; then
  echo "Restored same-head partial resume state from $(awk -F= '$1 == "AUTOFIX_RESUME_MARKER_FILE" { print $2 }' "${resume_env_file}" | tail -n 1) with $(awk -F= '$1 == "AUTOFIX_RESUME_RESTORED_ARTIFACT_COUNT" { print $2 }' "${resume_env_file}" | tail -n 1) cached artifacts."
else
  echo "No cached same-head partial resume state matched HEAD=${current_head_sha:-<unknown>}."
fi

rm -f "${resume_env_file}"
