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
# Capacity waits (action `wait`, only with CLAUDE_POOL_CAPACITY_WAIT_ENABLED=
# true; issue #7251, capacity-wait Phase 1) are recorded instead:
#
#   1. the work item (CLAUDE_CAPACITY_WAIT_ITEM_KIND issue|pr with a number)
#      gets the `ai:claude-capacity-wait` label (only after step 2 recorded
#      the run on the tracker; step 2 runs first) and one
#      `<!-- ai:claude-capacity-wait:v1 stage= source= run= role= -->` comment
#      (skipped when the GH_PAT login already posted one for this run);
#   2. the `kind=capacity` provider-outage tracker is opened or reused
#      (scripts/provider_outage.py open) and gets one
#      `<!-- ai:claude-capacity-wait-item:v1 kind= item= workflow= run= role= -->`
#      comment (skipped when the GH_PAT login already posted one for this
#      item and run);
#   3. one Telegram WARNING is sent only when this call opened the tracker
#      (or provider_outage.py could not read or create one).
#   Markers posted by any other login never count for deduplication.
#
# API calls (CLAUDE.md §15): one dispatch per refused (role, reason) pair, at
# most ENGINE_FALLBACK_REPORT_MAX_PAIRS (10). None when nothing was refused.
# Waits add, once per job: one `user` read, one label add, one paginated item
# comments read and one comment post (issue/pr items only), the tracker lookup
# (and create when none is open) inside provider_outage.py open, one paginated
# tracker comments read and one comment post. The existing report made no
# reads, so none could be reused. None when nothing waited.
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
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_SERVER_URL   the reporting
#                                    run; the payload's reporter_run names it so the intake
#                                    can scope the heal issue to it
#   REPORT_WORKFLOW_NAME             caller workflow name (default GITHUB_WORKFLOW)
#   REPORT_WRAPPER_SHA               coding-workflows commit of the support scripts
#   RUNTIME_DIR                      scratch dir (default RUNNER_TEMP)
#   CLAUDE_CAPACITY_WAIT_ITEM_KIND   issue | pr | run | none (default run: no item
#                                    label; none: tracker and alert only, no
#                                    tracker item marker, for jobs that resume
#                                    themselves)
#   CLAUDE_CAPACITY_WAIT_ITEM_NUMBER digits; anything else means kind run
#   CLAUDE_CAPACITY_WAIT_STAGE       stage token [a-z_-]{1,30} (default none)
#   CLAUDE_CAPACITY_WAIT_SOURCE_COMMENT  digits (default none)
#   CLAUDE_CAPACITY_WAIT_TRACKER_PY  provider_outage.py (default: beside this script)
#   GITHUB_WORKFLOW_REF              names the waiting workflow file
#   AI_ENGINE_FALLBACK_REPORT_WAITS_ONLY  "true" ignores refused fallbacks (implement
#                                    and review report those through their own
#                                    heal reporters; issue #7370)

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
TRACKER_PY="${CLAUDE_CAPACITY_WAIT_TRACKER_PY:-${script_dir}/provider_outage.py}"
CAPACITY_WAIT_LABEL="ai:claude-capacity-wait"

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
waits=0
wait_roles=""
first_wait_reason=""
while IFS=$'\t' read -r version epoch role reason class action policy extra || [ -n "${version:-}" ]; do
	[ "${version:-}" = "v1" ] && [ -z "${extra:-}" ] || continue
	[[ "${epoch:-}" =~ ^[0-9]{1,12}$ ]] || continue
	[[ "${role:-}" =~ ^[A-Z][A-Z0-9_]{1,40}$ ]] || continue
	[[ "${reason:-}" =~ ^[A-Za-z0-9_.-]{1,60}$ ]] || continue
	case "${class:-}" in capacity|non_capacity) ;; *) continue ;; esac
	case "${policy:-}" in capacity|always) ;; *) continue ;; esac
	total=$((total + 1))
	case "${action:-}" in
		refused) [ "${AI_ENGINE_FALLBACK_REPORT_WAITS_ONLY:-false}" != "true" ] || continue ;;
		codex) capacity=$((capacity + 1)); continue ;;
		wait)
			# A wait is only ever a capacity fallback (scripts/ai_engine.sh).
			[ "${class}" = "capacity" ] || continue
			waits=$((waits + 1))
			[ -n "${first_wait_reason}" ] || first_wait_reason="${reason}"
			case ",${wait_roles}," in
				*",${role},"*) ;;
				*) [ "$(printf '%s' "${wait_roles}" | tr -cd ',' | wc -c)" -ge 9 ] || wait_roles="${wait_roles:+${wait_roles},}${role}" ;;
			esac
			continue
			;;
		*) continue ;;
	esac
	case " ${pairs[*]:-} " in
		*" ${role}:${reason} "*) continue ;;
	esac
	[ "${#pairs[@]}" -lt "${MAX_PAIRS}" ] || { log "warn reason=pair_cap_reached cap=${MAX_PAIRS}"; break; }
	pairs+=("${role}:${reason}")
done < "${RECORDS_FILE}"

if [ "${#pairs[@]}" -eq 0 ] && [ "${waits}" -eq 0 ]; then
	log "outcome=none records=${total} codex_fallbacks=${capacity} waits=0"
	exit 0
fi

WORK_DIR="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}"
REPORT_DIR="$(mktemp -d "${WORK_DIR%/}/ai-engine-fallback-report-XXXXXXXX" 2>/dev/null || mktemp -d)"

# wait_marker_present <comments_json> <login> <regex>
# 0 when a comment by <login> matches <regex>; 1 when none does; 2 when the
# comments cannot be parsed. Accepts gh's --slurp list of pages.
wait_marker_present()
{
	python3 -I -B -c '
import json, re, sys
try:
	with open(sys.argv[1], encoding="utf-8") as handle:
		data = json.load(handle)
except (OSError, ValueError):
	sys.exit(2)
if not isinstance(data, list):
	sys.exit(2)
comments = []
for entry in data:
	if isinstance(entry, list):
		comments.extend(entry)
	else:
		comments.append(entry)
pattern = re.compile(sys.argv[3])
for comment in comments:
	if not isinstance(comment, dict):
		continue
	user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
	if user.get("login") == sys.argv[2] and pattern.search(str(comment.get("body") or "")):
		sys.exit(0)
sys.exit(1)
' "$1" "$2" "$3"
}

# report_capacity_wait: record this job's capacity waits (see the header).
# Never fails the job; every outcome is logged.
report_capacity_wait()
{
	local kind number stage source_comment run_token roles workflow login comments_file
	local label_state="skipped" marker_state="skipped" tracker="none" tracker_marker="skipped" alert_state="none"
	kind="$(printf '%s' "${CLAUDE_CAPACITY_WAIT_ITEM_KIND:-run}" | tr '[:upper:]' '[:lower:]')"
	case "${kind}" in issue|pr|run|none) ;; *) kind="run" ;; esac
	number="${CLAUDE_CAPACITY_WAIT_ITEM_NUMBER:-}"
	[[ "${number}" =~ ^[1-9][0-9]{0,9}$ ]] || number="none"
	[ "${number}" != "none" ] || [ "${kind}" = "none" ] || kind="run"
	case "${kind}" in run|none) number="none" ;; esac
	stage="${CLAUDE_CAPACITY_WAIT_STAGE:-}"
	[[ "${stage}" =~ ^[a-z_-]{1,30}$ ]] || stage="none"
	source_comment="${CLAUDE_CAPACITY_WAIT_SOURCE_COMMENT:-}"
	[[ "${source_comment}" =~ ^[0-9]{1,20}$ ]] || source_comment="none"
	run_token="${RUN_ID}"
	[[ "${run_token}" =~ ^[0-9]{1,20}$ ]] || run_token="none"
	roles="${wait_roles:-UNKNOWN}"
	workflow="${GITHUB_WORKFLOW_REF:-}"
	workflow="${workflow%%@*}"
	workflow="${workflow##*/}"
	workflow="$(printf '%s' "${workflow}" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-80)"
	[ -n "${workflow}" ] || workflow="unknown"

	if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		log "wait outcome=skip reason=missing_context item=${kind}:${number} roles=${roles}"
		return 0
	fi
	login="$(gh_retry gh api user --jq .login 2>/dev/null)" || login=""
	login="$(printf '%s' "${login}" | tr -d '[:space:]')"
	if ! [[ "${login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$ ]]; then
		log "wait outcome=skip reason=identity_unavailable item=${kind}:${number} roles=${roles}"
		echo "::warning::Claude capacity wait could not be recorded: the pipeline account could not be read."
		return 0
	fi
	comments_file="${REPORT_DIR}/wait-comments.json"

	if [ ! -f "${TRACKER_PY}" ] || [ -L "${TRACKER_PY}" ]; then
		log "wait outcome=skip reason=tracker_helper_missing item=${kind}:${number}"
		tracker_marker="failed"
	else
		local alert_file="${REPORT_DIR}/wait-alert.txt" open_out="" open_line=""
		open_out="$(python3 -I -B "${TRACKER_PY}" open --repo "${REPO}" --kind capacity --provider claude-pool --status "${first_wait_reason:-all_gated}" --alert-out "${alert_file}" 2>/dev/null)" || open_out=""
		open_line="$(printf '%s\n' "${open_out}" | grep -E '^alert=[01] outcome=[a-z_]+ tracker=[0-9]*$' | tail -n 1)"
		tracker="${open_line##*tracker=}"
		[[ "${tracker}" =~ ^[1-9][0-9]{0,9}$ ]] || tracker="none"
		tracker_marker="failed"
		if [ "${kind}" = "none" ]; then
			# Kind none (issue #7370): the job resumes itself (poller judges,
			# the unblock judge), so it records no item for the resume pass.
			tracker_marker="skipped"
		elif [ "${tracker}" != "none" ] && gh_retry gh api --paginate --slurp "repos/${REPO}/issues/${tracker}/comments?per_page=100" > "${comments_file}" 2>/dev/null; then
			local tracker_rc=0
			wait_marker_present "${comments_file}" "${login}" "<!-- ai:claude-capacity-wait-item:v1 kind=[a-z]+ item=${number} [^>]*\brun=${run_token} " || tracker_rc=$?
			if [ "${tracker_rc}" -eq 0 ]; then
				tracker_marker="present"
			elif [ "${tracker_rc}" -eq 1 ]; then
				if gh_retry gh api -X POST "repos/${REPO}/issues/${tracker}/comments" -f "body=Waiting for Claude capacity: ${kind} ${number}, workflow ${workflow}, run ${run_token}, roles ${roles}."$'\n\n'"<!-- ai:claude-capacity-wait-item:v1 kind=${kind} item=${number} workflow=${workflow} run=${run_token} role=${roles} -->" >/dev/null 2>&1; then
					tracker_marker="posted"
				fi
			fi
		fi
		[ "${tracker_marker}" != "failed" ] || echo "::warning::Claude capacity wait: could not record the wait on the capacity tracker."
		# provider_outage.py writes the alert file only when this call opened
		# the tracker (or could not read or create one), so this alerts once.
		if [ -s "${alert_file}" ] && [ ! -L "${alert_file}" ]; then
			if type tg_send_msg >/dev/null 2>&1; then
				local tracker_ref="tracker #${tracker}"
				[ "${tracker}" != "none" ] || tracker_ref="tracker unavailable (lookup or create failed)"
				tg_send_msg "Claude pool exhausted in ${REPO}: work is waiting, not falling back. Hourly re-check; ${tracker_ref}."$'\n'"Run: ${REPORTER_RUN_URL}" "WARNING" >/dev/null 2>&1 || true
				alert_state="sent"
			else
				alert_state="unavailable"
			fi
		fi
	fi
	if [ "${kind}" = "issue" ] || [ "${kind}" = "pr" ]; then
		# Label only once the tracker lists this run: the label makes stall
		# recovery leave the item alone, and the resume pass finds waiting work
		# through the tracker markers, so a label without one would strand it.
		if [ "${tracker_marker}" != "posted" ] && [ "${tracker_marker}" != "present" ]; then
			echo "::warning::Claude capacity wait: ${kind} #${number} not labelled ${CAPACITY_WAIT_LABEL} because the tracker record failed."
		elif GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${REPO}/issues/${number}/labels" -f "labels[]=${CAPACITY_WAIT_LABEL}" >/dev/null 2>&1; then
			label_state="applied"
		else
			label_state="failed"
			echo "::warning::Claude capacity wait: could not label ${kind} #${number} ${CAPACITY_WAIT_LABEL}."
		fi
		marker_state="failed"
		if gh_retry gh api --paginate --slurp "repos/${REPO}/issues/${number}/comments?per_page=100" > "${comments_file}" 2>/dev/null; then
			local present_rc=0
			wait_marker_present "${comments_file}" "${login}" "<!-- ai:claude-capacity-wait:v1 [^>]*\brun=${run_token} " || present_rc=$?
			if [ "${present_rc}" -eq 0 ]; then
				marker_state="present"
			elif [ "${present_rc}" -eq 1 ]; then
				if gh_retry gh api -X POST "repos/${REPO}/issues/${number}/comments" -f "body=Claude pool at capacity: this work is waiting for a Claude account instead of falling back to codex/OpenCode (roles ${roles}, stage ${stage})."$'\n\n'"<!-- ai:claude-capacity-wait:v1 stage=${stage} source=${source_comment} run=${run_token} role=${roles} -->" >/dev/null 2>&1; then
					marker_state="posted"
				fi
			fi
		fi
		[ "${marker_state}" != "failed" ] || echo "::warning::Claude capacity wait: could not record the wait marker on ${kind} #${number}."
	fi

	log "wait item=${kind}:${number} roles=${roles} label=${label_state} marker=${marker_state} tracker=${tracker} tracker_marker=${tracker_marker} alert=${alert_state}"
	return 0
}

if [ "${waits}" -gt 0 ]; then
	report_capacity_wait || true
fi
if [ "${#pairs[@]}" -eq 0 ]; then
	rm -rf -- "${REPORT_DIR}" 2>/dev/null || true
	log "outcome=none records=${total} codex_fallbacks=${capacity} waits=${waits}"
	exit 0
fi
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
		--workflow-name "${WORKFLOW_NAME}" --run-id "${RUN_ID}" --run-attempt "${GITHUB_RUN_ATTEMPT:-}" --wrapper-sha "${WRAPPER_SHA}" \
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
log "outcome=reported pairs=${#pairs[@]} dispatched=${dispatched} records=${total} codex_fallbacks=${capacity} waits=${waits}"
exit 0
