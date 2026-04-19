#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat <<'USAGE'
Usage: scripts/dev/replay_review_pipeline.sh --runtime-dir <path> [--workspace-dir <path>] [--consolidator-enabled <0|1>] [--disable-consolidator]

Replays the local review artifact chain in this order:
	1) review_floor_rules.sh
	2) review_consolidate.sh
	3) review_parse_consolidator.sh
	4) review_issue_ledger.sh

Required:
	--runtime-dir <path>	Directory containing reviewer_bundle.txt and runtime artifacts.

Optional:
	--workspace-dir <path>	Git workspace used for parser/ledger source anchors (default: current directory).
	--consolidator-enabled <0|1>	Override REVIEW_CONSOLIDATOR_ENABLED for replay (default: env or 1).
	--disable-consolidator	Shortcut for --consolidator-enabled 0.
	-h, --help		Show this help.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

runtime_dir=""
workspace_dir="$(pwd)"
consolidator_enabled="${REVIEW_CONSOLIDATOR_ENABLED:-1}"

while [ "$#" -gt 0 ]; do
	case "$1" in
		--runtime-dir)
			if [ "$#" -lt 2 ]; then
				echo "error: --runtime-dir requires a value" >&2
				exit 2
			fi
			runtime_dir="$2"
			shift 2
			;;
		--workspace-dir)
			if [ "$#" -lt 2 ]; then
				echo "error: --workspace-dir requires a value" >&2
				exit 2
			fi
			workspace_dir="$2"
			shift 2
			;;
		--consolidator-enabled)
			if [ "$#" -lt 2 ]; then
				echo "error: --consolidator-enabled requires a value" >&2
				exit 2
			fi
			consolidator_enabled="$2"
			shift 2
			;;
		--disable-consolidator)
			consolidator_enabled="0"
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "error: unknown argument: $1" >&2
			usage
			exit 2
			;;
	esac
done

if [ -z "${runtime_dir}" ]; then
	echo "error: --runtime-dir is required" >&2
	usage
	exit 2
fi
if [ ! -d "${runtime_dir}" ]; then
	echo "error: runtime dir does not exist: ${runtime_dir}" >&2
	exit 2
fi
if [ ! -f "${runtime_dir}/reviewer_bundle.txt" ]; then
	echo "error: missing reviewer bundle: ${runtime_dir}/reviewer_bundle.txt" >&2
	exit 2
fi
if ! [[ "${consolidator_enabled}" =~ ^[01]$ ]]; then
	echo "error: --consolidator-enabled must be 0 or 1" >&2
	exit 2
fi

for required in \
	"${REPO_ROOT}/scripts/review_floor_rules.sh" \
	"${REPO_ROOT}/scripts/review_consolidate.sh" \
	"${REPO_ROOT}/scripts/review_parse_consolidator.sh" \
	"${REPO_ROOT}/scripts/review_issue_ledger.sh"; do
	if [ ! -f "${required}" ]; then
		echo "error: required stage script is missing: ${required}" >&2
		exit 2
	fi
done

if ! git -C "${workspace_dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	echo "warning: workspace is not a git repository: ${workspace_dir}" >&2
	echo "warning: ledger/path anchoring may be incomplete in replay output" >&2
fi

export PYTHONDONTWRITEBYTECODE=1
export RUNTIME_DIR="${runtime_dir}"
export SUPPORT_PROMPTS_DIR="${SUPPORT_PROMPTS_DIR:-${REPO_ROOT}/prompts}"
export REVIEW_CONSOLIDATOR_ENABLED="${consolidator_enabled}"
export REVIEW_PARSER_FAILOPEN="${REVIEW_PARSER_FAILOPEN:-1}"
export PR_NUMBER="${PR_NUMBER:-0}"
export AUTOFIX_ITERATION="${AUTOFIX_ITERATION:-1}"
export REVIEW_ISSUES_FILE="${REVIEW_ISSUES_FILE:-${runtime_dir}/review_issues.txt}"
export LEDGER_STATUS_FILE="${LEDGER_STATUS_FILE:-${runtime_dir}/ledger_status.txt}"
export FLOOR_TAGS_FILE="${FLOOR_TAGS_FILE:-${runtime_dir}/floor_tags.txt}"
export CONSOLIDATOR_RAW_FILE="${CONSOLIDATOR_RAW_FILE:-${runtime_dir}/consolidator_raw.txt}"
export PARSER_STATS_FILE="${PARSER_STATS_FILE:-${runtime_dir}/parser_stats.txt}"
export REVIEW_LEDGER_ENABLED="${REVIEW_LEDGER_ENABLED:-1}"
export REVIEW_LEDGER_PATH="${REVIEW_LEDGER_PATH:-${runtime_dir}/review_issue_ledger.txt}"

stage_failures=0

run_stage()
{
	local stage="$1"
	shift
	if "$@"; then
		printf 'stage=%s status=ok\n' "${stage}"
	else
		local rc=$?
		stage_failures=$((stage_failures + 1))
		printf 'stage=%s status=failed rc=%s continue=1\n' "${stage}" "${rc}" >&2
	fi
}

summarize_artifact()
{
	local label="$1"
	local path="$2"
	if [ -f "${path}" ]; then
		local bytes
		local lines
		bytes="$(wc -c < "${path}" | tr -d '[:space:]')"
		lines="$(wc -l < "${path}" | tr -d '[:space:]')"
		printf '%s\tpresent=1\tbytes=%s\tlines=%s\tpath=%s\n' "${label}" "${bytes}" "${lines}" "${path}"
	else
		printf '%s\tpresent=0\tbytes=0\tlines=0\tpath=%s\n' "${label}" "${path}"
	fi
}

run_stage "floor" bash "${REPO_ROOT}/scripts/review_floor_rules.sh" "${runtime_dir}/reviewer_bundle.txt" "${FLOOR_TAGS_FILE}"
run_stage "consolidate" bash "${REPO_ROOT}/scripts/review_consolidate.sh"
run_stage "parse" bash "${REPO_ROOT}/scripts/review_parse_consolidator.sh"
run_stage "ledger" bash "${REPO_ROOT}/scripts/review_issue_ledger.sh"

echo "=== Replay Artifacts ==="
summarize_artifact "floor_tags" "${FLOOR_TAGS_FILE}"
summarize_artifact "review_issues" "${REVIEW_ISSUES_FILE}"
summarize_artifact "parser_stats" "${PARSER_STATS_FILE}"
summarize_artifact "ledger_status" "${LEDGER_STATUS_FILE}"

if [ -f "${PARSER_STATS_FILE}" ]; then
	echo "=== Parser Stats ==="
	cat "${PARSER_STATS_FILE}"
fi

if [ "${stage_failures}" -gt 0 ]; then
	echo "replay_completed_with_stage_failures=${stage_failures}" >&2
	exit 1
fi

exit 0
