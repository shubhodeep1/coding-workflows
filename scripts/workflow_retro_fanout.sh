#!/usr/bin/env bash
# workflow_retro_fanout.sh — Post weekly retros to consumer repos.
#
# Runs inside the source repo's weekly-retro job (workflow-log-analysis.yml)
# AFTER the source-repo retro. The collect-logs job already gathers workflow
# runs for every repo in .github/ai/consumer_repos.json into
# workflow_log_report.json, so this script loops the roster, builds a per-repo
# retro from that same artifact, skips no-activity repos, and posts each
# narrative to the consumer's "AI Workflow Weekly Retro" tracker issue via
# GH_PAT (§14 requires repo scope on every consumer). Per-repo failures fail
# open: they are logged and the loop continues.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

retro_fanout_flag_enabled() {
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED="${WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED:-true}"
if ! retro_fanout_flag_enabled "${WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED}"; then
	echo "retro-fanout: WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED=${WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED}; skipping."
	exit 0
fi

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

WORKFLOW_LOG_REPORT_FILE="${WORKFLOW_LOG_REPORT_FILE:-${REPO_ROOT}/workflow_log_report.json}"
CONSUMER_REPOS_FILE="${CONSUMER_REPOS_FILE:-${REPO_ROOT}/.github/ai/consumer_repos.json}"
WORKFLOW_RETRO_MODEL="${WORKFLOW_RETRO_MODEL:-openai/gpt-5.4-mini}"
WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY="${WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY:-true}"
MAX_CODEX_ATTEMPTS="${MAX_CODEX_ATTEMPTS:-3}"
CODEX_RETRY_BACKOFF_BASE_SECS="${CODEX_RETRY_BACKOFF_BASE_SECS:-10}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

if ! [[ "${MAX_CODEX_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_CODEX_ATTEMPTS}" -lt 1 ]; then
	echo "::warning::MAX_CODEX_ATTEMPTS must be a positive integer (got: ${MAX_CODEX_ATTEMPTS}); defaulting to 3."
	MAX_CODEX_ATTEMPTS="3"
fi
if ! [[ "${CODEX_RETRY_BACKOFF_BASE_SECS}" =~ ^[0-9]+$ ]] || [ "${CODEX_RETRY_BACKOFF_BASE_SECS}" -lt 1 ]; then
	echo "::warning::CODEX_RETRY_BACKOFF_BASE_SECS must be a positive integer (got: ${CODEX_RETRY_BACKOFF_BASE_SECS}); defaulting to 10."
	CODEX_RETRY_BACKOFF_BASE_SECS="10"
fi

if [ ! -f "${WORKFLOW_LOG_REPORT_FILE}" ]; then
	echo "::warning::retro-fanout: ${WORKFLOW_LOG_REPORT_FILE} not found; skipping consumer fan-out."
	exit 0
fi
if [ ! -f "${CONSUMER_REPOS_FILE}" ]; then
	echo "::warning::retro-fanout: ${CONSUMER_REPOS_FILE} not found; skipping consumer fan-out."
	exit 0
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/label_helpers.sh"
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

TRACKER_TITLE="AI Workflow Weekly Retro"
TRACKER_MARKER="<!-- ai:retro-tracker:v1 -->"
REQUIRED_RETRO_HEADINGS=(
	"## Weekly Retro"
	"### What Worked"
	"### Failure Modes"
	"### Next Week Recommendation"
	"### Metrics Snapshot"
)

FANOUT_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/workflow-retro-fanout.XXXXXX")"
trap 'rm -rf "${FANOUT_RUNTIME_DIR}"' EXIT

mapfile -t FANOUT_REPOS < <(jq -r '.[]' "${CONSUMER_REPOS_FILE}" 2>/dev/null | sort -u)
if [ "${#FANOUT_REPOS[@]}" -eq 0 ]; then
	echo "::warning::retro-fanout: no repositories parsed from ${CONSUMER_REPOS_FILE}; skipping."
	exit 0
fi

fanout_attempted=0
fanout_failed=0

# Per-consumer opt-out honoring the same WORKFLOW_RETRO_ENABLED contract the
# per-repo wrappers use. One GET per consumer per week (§15: no existing call
# in this path returns consumer repo vars); every failure mode fails open to
# enabled because the fleet default is on.
consumer_retro_enabled() {
	local target_repo="$1"
	local var_value=""
	var_value="$(gh api "repos/${target_repo}/actions/variables/WORKFLOW_RETRO_ENABLED" --jq '.value' 2>/dev/null || echo "")"
	if [ "${var_value}" = "null" ]; then
		var_value=""
	fi
	if [ -n "${var_value}" ] && ! retro_fanout_flag_enabled "${var_value}"; then
		return 1
	fi
	return 0
}

run_consumer_retro() {
	local target_repo="$1"
	local safe_slug="${target_repo//\//__}"
	local ctx_file="${FANOUT_RUNTIME_DIR}/${safe_slug}-context.md"
	local json_file="${FANOUT_RUNTIME_DIR}/${safe_slug}-context.json"
	local prompt_file="${FANOUT_RUNTIME_DIR}/${safe_slug}-prompt.txt"
	local body_file="${FANOUT_RUNTIME_DIR}/${safe_slug}-retro.md"
	local comment_file="${FANOUT_RUNTIME_DIR}/${safe_slug}-comment.md"
	local comment_payload="${FANOUT_RUNTIME_DIR}/${safe_slug}-comment-payload.json"
	local tracker_body_file="${FANOUT_RUNTIME_DIR}/${safe_slug}-tracker-body.md"
	local candidates_json="${FANOUT_RUNTIME_DIR}/${safe_slug}-candidates.json"
	local selection_env="${FANOUT_RUNTIME_DIR}/${safe_slug}-selection.env"
	local comments_json="${FANOUT_RUNTIME_DIR}/${safe_slug}-comments.json"

	python3 "${SCRIPT_DIR}/workflow_retro.py" \
		--report "${WORKFLOW_LOG_REPORT_FILE}" \
		--output "${ctx_file}" \
		--json-output "${json_file}" \
		--repo "${target_repo}" \
		--repo-root "${GITHUB_WORKSPACE:-${REPO_ROOT}}" >/dev/null || return 1

	local week_label window_since has_activity
	week_label="$(jq -r '.window.week_label' "${json_file}")"
	window_since="$(jq -r '.window.since' "${json_file}")"
	has_activity="$(jq -r 'if .has_activity == false then "false" else "true" end' "${json_file}")"
	if [ -z "${week_label}" ] || [ "${week_label}" = "null" ]; then
		echo "::warning::retro-fanout: could not parse retro window for ${target_repo}."
		return 1
	fi

	if [ "${has_activity}" != "true" ] && retro_fanout_flag_enabled "${WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY}"; then
		echo "WORKFLOW_RETRO_SKIP_V1: repo=${target_repo} week=${week_label} reason=no_activity total_runs=$(jq -r '.summary.total_runs // 0' "${json_file}") merged_prs=$(jq -r '.summary.merged_pr_count // 0' "${json_file}")"
		echo "WORKFLOW_RETRO_FANOUT_V1: repo=${target_repo} week=${week_label} status=skipped_no_activity"
		return 0
	fi

	local rendered_prompt
	rendered_prompt="$(bash "${SCRIPT_DIR}/render_prompt.sh" "${REPO_ROOT}/prompts/mode-workflow-analysis.txt")" || return 1
	{
		echo "Mode: retro"
		printf '%s\n' "${rendered_prompt}"
		echo
		echo "=== WEEKLY RETRO CONTEXT ==="
		cat "${ctx_file}"
		echo
		echo "=== WEEKLY RETRO JSON ==="
		cat "${json_file}"
	} > "${prompt_file}"

	local attempt codex_exit sleep_secs
	codex_exit=0
	for attempt in $(seq 1 "${MAX_CODEX_ATTEMPTS}"); do
		if command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
			sanitize_codex_prompt_file "${prompt_file}"
		fi
		set +e
		bash "${SCRIPT_DIR}/codex_heartbeat.sh" \
			--phase workflow_weekly_retro \
			--stdout-file "${body_file}" \
			-- codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${WORKFLOW_RETRO_MODEL}" --sandbox danger-full-access < "${prompt_file}"
		codex_exit=$?
		set -e
		if [ "${codex_exit}" -eq 0 ] && grep -q '[^[:space:]]' "${body_file}"; then
			break
		fi
		if [ "${attempt}" -lt "${MAX_CODEX_ATTEMPTS}" ]; then
			sleep_secs=$((CODEX_RETRY_BACKOFF_BASE_SECS * (2 ** (attempt - 1))))
			sleep "${sleep_secs}"
		else
			echo "::warning::retro-fanout: Codex retro pass for ${target_repo} failed after ${MAX_CODEX_ATTEMPTS} attempts (exit=${codex_exit})."
			return 1
		fi
	done

	local heading previous_heading_line heading_line
	previous_heading_line=0
	for heading in "${REQUIRED_RETRO_HEADINGS[@]}"; do
		heading_line="$(grep -nFx "${heading}" "${body_file}" | head -n1 | cut -d: -f1 || true)"
		if [ -z "${heading_line}" ] || [ "${heading_line}" -le "${previous_heading_line}" ]; then
			echo "::warning::retro-fanout: retro output for ${target_repo} is missing or misorders required heading: ${heading}"
			return 1
		fi
		previous_heading_line="${heading_line}"
	done

	# `run_consumer_retro` is invoked via `if ! run_consumer_retro ...`; in bash,
	# that suppresses `set -e` inside the function body, so an unguarded failure
	# here would silently continue instead of marking this repo failed.
	ensure_label_exists "ai:retro" "${target_repo}" || return 1

	cat > "${tracker_body_file}" <<EOF
${TRACKER_MARKER}
# ${TRACKER_TITLE}

This issue is managed by the consumer retro fan-out in
shubhodeep1/coding-workflows/.github/workflows/workflow-log-analysis.yml.

It collects weekly workflow retrospectives for this repository.
EOF

	gh_retry gh issue list \
		--repo "${target_repo}" \
		--state all \
		--label "ai:retro" \
		--limit 50 \
		--json number,title,body,state,updatedAt,url > "${candidates_json}" || return 1

	if ! python3 - "${candidates_json}" "${TRACKER_MARKER}" > "${selection_env}" <<'PY'
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

candidates_path = Path(sys.argv[1])
marker = sys.argv[2]

try:
	candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
	raise SystemExit(f"failed to load retro tracker candidates: {exc}")

selected_candidates = []
for candidate in candidates:
	if not isinstance(candidate, dict):
		continue
	if marker not in str(candidate.get("body") or ""):
		continue
	selected_candidates.append(candidate)

selected_candidates.sort(
	key=lambda candidate: (
		str(candidate.get("state") or "").upper() == "OPEN",
		str(candidate.get("updatedAt") or ""),
		int(candidate.get("number") or 0),
	),
	reverse=True,
)
selected = selected_candidates[0] if selected_candidates else None

number = ""
state = ""
if isinstance(selected, dict):
	number = str(selected.get("number") or "").strip()
	state = str(selected.get("state") or "").strip()

print(f"FANOUT_TRACKER_NUMBER={shlex.quote(number)}")
print(f"FANOUT_TRACKER_STATE={shlex.quote(state)}")
PY
	then
		return 1
	fi

	local FANOUT_TRACKER_NUMBER="" FANOUT_TRACKER_STATE=""
	# shellcheck disable=SC1090
	source "${selection_env}"

	if [ -z "${FANOUT_TRACKER_NUMBER}" ]; then
		local tracker_url
		tracker_url="$(gh_retry gh issue create \
			--repo "${target_repo}" \
			--title "${TRACKER_TITLE}" \
			--label "ai:retro" \
			--body-file "${tracker_body_file}")" || return 1
		FANOUT_TRACKER_NUMBER="${tracker_url##*/}"
	else
		if [ "$(printf '%s' "${FANOUT_TRACKER_STATE}" | tr '[:lower:]' '[:upper:]')" = "CLOSED" ]; then
			gh_retry gh issue reopen "${FANOUT_TRACKER_NUMBER}" --repo "${target_repo}"
		fi
		gh_retry gh issue edit "${FANOUT_TRACKER_NUMBER}" --repo "${target_repo}" --add-label "ai:retro" >/dev/null
	fi

	local comment_marker="<!-- ai:workflow-retro:${week_label} -->"
	{
		echo "${comment_marker}"
		echo
		echo "_Posted by the consumer retro fan-out in shubhodeep1/coding-workflows (workflow-log-analysis.yml)._"
		echo
		cat "${body_file}"
	} > "${comment_file}"

	# Query only the current retro window so the week-scoped comment upsert
	# stays bounded even as the tracker grows over time.
	gh_retry gh api "repos/${target_repo}/issues/${FANOUT_TRACKER_NUMBER}/comments?since=${window_since}&per_page=100" > "${comments_json}" || return 1

	local existing_comment_id existing_comment_body
	existing_comment_id="$(jq -r --arg marker "${comment_marker}" '([.[] | select(((.body // "") | contains($marker))) | {id: ((.id // 0) | tonumber), body: (.body // "")}] | sort_by(.id) | last | .id) // empty' "${comments_json}" 2>/dev/null || echo "")"
	existing_comment_body="$(jq -r --arg marker "${comment_marker}" '([.[] | select(((.body // "") | contains($marker))) | {id: ((.id // 0) | tonumber), body: (.body // "")}] | sort_by(.id) | last | .body) // ""' "${comments_json}" 2>/dev/null || echo "")"

	if [ -n "${existing_comment_id}" ] && [ "${existing_comment_body}" = "$(cat "${comment_file}")" ]; then
		echo "WORKFLOW_RETRO_FANOUT_V1: repo=${target_repo} week=${week_label} status=up_to_date tracker=#${FANOUT_TRACKER_NUMBER}"
		return 0
	fi

	jq -n --rawfile body "${comment_file}" '{body: $body}' > "${comment_payload}" || return 1

	if [ -n "${existing_comment_id}" ] && [[ "${existing_comment_id}" =~ ^[0-9]+$ ]]; then
		if gh_retry gh api -X PATCH "repos/${target_repo}/issues/comments/${existing_comment_id}" --input "${comment_payload}" >/dev/null 2>&1; then
			echo "WORKFLOW_RETRO_FANOUT_V1: repo=${target_repo} week=${week_label} status=refreshed tracker=#${FANOUT_TRACKER_NUMBER}"
			return 0
		fi
		echo "::warning::retro-fanout: failed to refresh retro comment #${existing_comment_id} on ${target_repo}; posting a new comment."
	fi

	gh_retry gh api -X POST "repos/${target_repo}/issues/${FANOUT_TRACKER_NUMBER}/comments" --input "${comment_payload}" >/dev/null || return 1
	echo "WORKFLOW_RETRO_FANOUT_V1: repo=${target_repo} week=${week_label} status=posted tracker=#${FANOUT_TRACKER_NUMBER}"
	return 0
}

for fanout_repo in "${FANOUT_REPOS[@]}"; do
	if ! [[ "${fanout_repo}" =~ ^[^/]+/[^/]+$ ]]; then
		echo "::warning::retro-fanout: skipping invalid repository entry: ${fanout_repo}"
		continue
	fi
	if [ "${fanout_repo}" = "${GITHUB_REPOSITORY}" ]; then
		# The source repo's retro is posted by the dedicated job steps.
		continue
	fi
	if ! consumer_retro_enabled "${fanout_repo}"; then
		echo "WORKFLOW_RETRO_FANOUT_V1: repo=${fanout_repo} status=skipped_disabled"
		continue
	fi
	fanout_attempted=$((fanout_attempted + 1))
	if ! run_consumer_retro "${fanout_repo}"; then
		fanout_failed=$((fanout_failed + 1))
		echo "WORKFLOW_RETRO_FANOUT_V1: repo=${fanout_repo} status=failed"
	fi
done

echo "retro-fanout: attempted=${fanout_attempted} failed=${fanout_failed}"
if [ "${fanout_attempted}" -gt 0 ] && [ "${fanout_failed}" -eq "${fanout_attempted}" ]; then
	echo "::error::retro-fanout: every attempted consumer retro failed (${fanout_failed}/${fanout_attempted})."
	exit 1
fi
