#!/usr/bin/env bash
#
# ai_engine_fallback_report.sh
#
# Runs as the last step (`if: always()`) of every phase job that can call the
# Claude engine (clarify, plan, orchestrate, orchestrate_clarify_respond,
# orchestrate_poll, security-audit, validate), from the trusted support
# checkout only. It reads the AI engine fallback records that
# scripts/ai_engine.sh `ai_engine_fallback` appended during the job (plan item
# 3e, decision D1) and, for every refused (non-capacity) fallback:
#
#   1. builds one `engine_fallback` heal payload per (role, reason)
#      (scripts/workflow_failure_heal.py build-engine-fallback-payload), so
#      the heal intake opens one issue per role and reason, deduplicated by
#      fingerprint;
#   2. sends one `repository_dispatch` (event type `workflow-failure-heal`)
#      to coding-workflows per pair;
#   3. sends one Telegram WARNING per run listing the refused pairs.
#
# Capacity fallbacks (codex ran) are not reported here: ai_engine.sh already
# sent its once-per-run Telegram note for them.
#
# API calls (CLAUDE.md §15): one dispatch per refused (role, reason) pair, at
# most ENGINE_FALLBACK_REPORT_MAX_PAIRS (10); no reads. None when nothing was
# refused.
#
# The reporter never fails the job: every exit is 0 and every outcome is a
# stable log line prefixed AI_ENGINE_FALLBACK_REPORT.
#
# Env (all optional, with defaults):
#   AI_ENGINE_FALLBACK_RECORDS_FILE  records (default ${RUNNER_TEMP}/ai-engine-fallback-records.tsv)
#   WORKFLOW_HEAL_ENABLED            "false" skips the dispatch (Telegram still sent)
#   WORKFLOW_HEAL_UPSTREAM_REPO      dispatch target (default shubhodeep1/coding-workflows)
#   WORKFLOW_HEAL_PY                 workflow_failure_heal.py (default: beside this script)
#   GH_TOKEN                         token able to dispatch to the upstream repo
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SERVER_URL   the reporting run
#   REPORT_WORKFLOW_NAME             caller workflow name (default GITHUB_WORKFLOW)
#   REPORT_WRAPPER_SHA               coding-workflows commit of the support scripts
#   RUNTIME_DIR                      scratch dir (default RUNNER_TEMP)

set -uo pipefail

log()
{
	echo "AI_ENGINE_FALLBACK_REPORT $*"
}

RECORDS_FILE="${AI_ENGINE_FALLBACK_RECORDS_FILE:-${RUNNER_TEMP:-/tmp}/ai-engine-fallback-records.tsv}"
ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
REPO="${GITHUB_REPOSITORY:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
REPORTER_RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${RUN_ID}"
WORKFLOW_NAME="${REPORT_WORKFLOW_NAME:-${GITHUB_WORKFLOW:-unknown}}"
WRAPPER_SHA="$(printf '%s' "${REPORT_WRAPPER_SHA:-}" | tr '[:upper:]' '[:lower:]')"
MAX_PAIRS=10
MAX_RECORDS_BYTES=65536
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"

if [ -f "${script_dir}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
if ! type tg_send_msg >/dev/null 2>&1 && [ -f "${script_dir}/tg_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/tg_helpers.sh" 2>/dev/null || true
fi

if [ -L "${RECORDS_FILE}" ]; then
	log "skip reason=records_symlink"
	exit 0
fi
if [ ! -f "${RECORDS_FILE}" ]; then
	log "skip reason=no_records"
	exit 0
fi
records_size="$(stat -c %s -- "${RECORDS_FILE}" 2>/dev/null || echo 0)"
if ! [[ "${records_size}" =~ ^[0-9]+$ ]] || [ "${records_size}" -gt "${MAX_RECORDS_BYTES}" ]; then
	log "skip reason=records_oversized bytes=${records_size}"
	exit 0
fi

# Keep only well-formed v1 records whose every field matches its vocabulary;
# the records are written by trusted support code, but are re-checked here.
declare -a pairs=()
total=0
capacity=0
while IFS=$'\t' read -r version epoch role reason class action policy extra || [ -n "${version:-}" ]; do
	[ "${version:-}" = "v1" ] && [ -z "${extra:-}" ] || continue
	[[ "${epoch:-}" =~ ^[0-9]{1,12}$ ]] || continue
	[[ "${role:-}" =~ ^[A-Z][A-Z0-9_]{1,40}$ ]] || continue
	[[ "${reason:-}" =~ ^[A-Za-z0-9_.-]{1,60}$ ]] || continue
	case "${class:-}" in capacity|non_capacity) ;; *) continue ;; esac
	case "${policy:-}" in capacity|always) ;; *) continue ;; esac
	total=$((total + 1))
	case "${action:-}" in
		refused) ;;
		codex) capacity=$((capacity + 1)); continue ;;
		*) continue ;;
	esac
	case " ${pairs[*]:-} " in
		*" ${role}:${reason} "*) continue ;;
	esac
	[ "${#pairs[@]}" -lt "${MAX_PAIRS}" ] || { log "warn reason=pair_cap_reached cap=${MAX_PAIRS}"; break; }
	pairs+=("${role}:${reason}")
done < "${RECORDS_FILE}"

if [ "${#pairs[@]}" -eq 0 ]; then
	log "outcome=none records=${total} codex_fallbacks=${capacity}"
	exit 0
fi

WORK_DIR="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}"
REPORT_DIR="$(mktemp -d "${WORK_DIR%/}/ai-engine-fallback-report-XXXXXXXX" 2>/dev/null || mktemp -d)"
dispatched=0
for entry in "${pairs[@]}"; do
	role="${entry%%:*}"
	reason="${entry#*:}"
	if [ "$(printf '%s' "${ENABLED}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
		log "skip_dispatch reason=disabled role=${role} fallback_reason=${reason}"
		continue
	fi
	if [ -z "${REPO}" ] || [ ! -f "${HEAL_PY}" ]; then
		log "skip_dispatch reason=missing_context role=${role} fallback_reason=${reason}"
		continue
	fi
	payload_file="${REPORT_DIR}/payload-${role}-${reason}.json"
	dispatch_file="${REPORT_DIR}/dispatch-${role}-${reason}.json"
	error_file="${REPORT_DIR}/error.txt"
	if ! python3 "${HEAL_PY}" build-engine-fallback-payload --repo "${REPO}" --role "${role}" --reason "${reason}" \
		--workflow-name "${WORKFLOW_NAME}" --run-id "${RUN_ID}" --wrapper-sha "${WRAPPER_SHA}" \
		--reporter-run-url "${REPORTER_RUN_URL}" --records-file "${RECORDS_FILE}" > "${payload_file}" 2> "${error_file}" \
		|| ! python3 "${HEAL_PY}" wrap-dispatch --payload-json "${payload_file}" > "${dispatch_file}" 2>> "${error_file}"; then
		log "skip_dispatch reason=payload_build_failed role=${role} fallback_reason=${reason} detail=$(head -c 200 "${error_file}" | tr '\n' ' ')"
		continue
	fi
	if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${dispatch_file}" >/dev/null 2> "${error_file}"; then
		log "skip_dispatch reason=dispatch_denied role=${role} fallback_reason=${reason} upstream=${UPSTREAM_REPO} detail=$(head -c 300 "${error_file}" | tr '\n' ' ')"
		echo "::warning::AI engine fallback heal report (${role}, ${reason}) could not be dispatched to ${UPSTREAM_REPO}."
		continue
	fi
	dispatched=$((dispatched + 1))
	log "dispatched role=${role} fallback_reason=${reason} workflow=${WORKFLOW_NAME} run=${RUN_ID:-none} upstream=${UPSTREAM_REPO}"
done
rm -rf -- "${REPORT_DIR}" 2>/dev/null || true

pair_list="$(printf '%s, ' "${pairs[@]}")"
pair_list="${pair_list%, }"
if type tg_send_msg >/dev/null 2>&1; then
	tg_send_msg "AI engine: refused Claude fallback (non-capacity) in ${REPO:-unknown} ${WORKFLOW_NAME} run ${RUN_ID:-local}: ${pair_list}. Codex did not run; set AI_ENGINE_FALLBACK_POLICY=always to roll back."$'\n'"Run: ${REPORTER_RUN_URL}" "WARNING" >/dev/null 2>&1 || true
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ ! -L "${GITHUB_STEP_SUMMARY}" ]; then
	{
		echo "### Refused AI engine fallbacks"
		echo
		for entry in "${pairs[@]}"; do
			echo "- \`${entry%%:*}\`: \`${entry#*:}\`"
		done
	} >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
fi
log "outcome=reported pairs=${#pairs[@]} dispatched=${dispatched} records=${total} codex_fallbacks=${capacity}"
exit 0
