#!/usr/bin/env bash
# auto_release_stable.sh — release the `stable` branch when it is ahead of the
# `stable` tag.
#
# Runs from .github/workflows/auto-release-stable.yml on a schedule. Patch
# fixes merged straight into the `stable` branch (the `claude/*-stable` PRs)
# used to sit unreleased until an operator dispatched test-and-mark-stable.yml
# by hand; consumers pin the *tag*, so they never saw the fix. This script
# dispatches the same release gate automatically, and only when there is
# something to release.
#
# Decision order (every exit is a logged, stable prefix):
#   AUTO_RELEASE_SKIPPED reason=disabled          kill switch
#   AUTO_RELEASE_SKIPPED reason=up_to_date        branch tip == tag commit
#   AUTO_RELEASE_SKIPPED reason=branch_not_ahead   branch is behind or diverged
#                                                 from the tag; fail closed
#   AUTO_RELEASE_SKIPPED reason=release_in_flight the gate (any branch), a
#                                                 promote-main-to-stable run
#                                                 or a legacy mark-stable run
#                                                 is queued/running
#   AUTO_RELEASE_SKIPPED reason=last_gate_failed  the gate already failed or
#                                                 timed out on this exact tip
#                                                 AUTO_RELEASE_STABLE_MAX_ATTEMPTS
#                                                 times; a human must look (a
#                                                 cancelled gate is retried and
#                                                 never counts as an attempt)
#   AUTO_RELEASE_DISPATCHED sha=<tip>
#
# Retry budget: a gate that failed on the current tip is re-dispatched on the
# next tick until AUTO_RELEASE_STABLE_MAX_ATTEMPTS completed runs on that tip
# have ended in failure / timed_out / startup_failure. A transient failure (a
# GitHub-side push rejection, a runner loss after the tests ran) therefore
# releases on a later tick by itself; a deterministic failure stops costing
# gate runs after the budget and waits for the branch to move (a workflow-heal
# hotfix on `stable` is such a move) or for a human re-run. Attempts are
# counted from the 30 most recent gate runs the workflow-runs read returns.
#
# API calls per run (§15): 2 ref reads (+1 to dereference an annotated tag),
# 1 compare, 3 workflow-runs lists (the release gate, promote-main-to-stable,
# and the legacy mark-stable path), at most 1 dispatch, plus at most 1
# release-blocker report (`POST repos/<self>/dispatches`) per tick. The
# reporters read no extra data: they reuse the run lists, the tag object
# (`.tagger.date`) and the compare response (commit and base-commit dates).
#
# Release-blocker reporters (stuck-states P4 / release-blocker Phase 2a,
# Refs #8291). Before the matching AUTO_RELEASE_* line, and without changing
# it, the exit code or GITHUB_OUTPUT, the tick sends one `release_blocker`
# heal report (workflow_failure_heal.py build-blocker-payload +
# request-report-identity + wrap-dispatch) when:
#   last_gate_failed      the gate budget is spent on the tip and the newest
#                         failed gate run ended RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS
#                         ago or longer (that run goes in run_refs when it
#                         ended failure or timed_out; a startup_failure run
#                         is named in the evidence only)
#   release_in_flight     the oldest active gate / promote / mark-stable run
#                         started RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS ago or
#                         longer (the run is named in the evidence only: the
#                         intake drops claimed runs that did not fail)
#   stable_release_stale  neither of the above reported, no gate run on the
#                         tip exists other than completed cancelled runs, and
#                         the unreleased
#                         work (max of the tag time and the oldest unreleased
#                         commit) has waited RELEASE_BLOCKER_STABLE_STALE_SECS
#                         or longer; on the dispatch path the gate is still
#                         dispatched afterwards
# Log lines:
#   RELEASE_BLOCKER_REPORTED reason=<r> age=<n> branch=<b> sha=<tip> run_ref=<id|none>
#   RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled|payload_invalid|dispatch_denied|guard_unavailable blocker=<r> ...
# payload_invalid and dispatch_denied also send a Telegram WARNING. A
# reporter failure never fails or delays the tick.
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required)        owner/repo
#   GH_TOKEN (required)                 token able to dispatch workflows
#   AUTO_RELEASE_STABLE_ENABLED         default true
#   AUTO_RELEASE_STABLE_BRANCH          default stable
#   AUTO_RELEASE_STABLE_TAG             default stable
#   AUTO_RELEASE_STABLE_WORKFLOW_FILE   default test-and-mark-stable.yml
#   AUTO_RELEASE_STABLE_MAX_ATTEMPTS    default 3 (failed gate runs per tip
#                                       before the tip is held)
#   RELEASE_BLOCKER_HEAL_ENABLED        default true (kill switch for the
#                                       release-blocker reporters)
#   RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS  default 86400
#   RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS    default 21600
#   RELEASE_BLOCKER_STABLE_STALE_SECS         default 86400
#                                       (a non-integer threshold warns and
#                                       falls back to its default)
#   RELEASE_BLOCKER_HEAL_PY             default scripts/workflow_failure_heal.py
#   TG_BOT_SECRET / TG_ADMIN_CHAT_ID    Telegram WARNING when a report is not
#                                       delivered (no-op when unset)
#   GITHUB_OUTPUT                       receives dispatched=/sha=

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source "${SCRIPT_DIR}/tg_helpers.sh" 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { return 0; }

AUTO_RELEASE_STABLE_ENABLED="${AUTO_RELEASE_STABLE_ENABLED:-true}"
AUTO_RELEASE_STABLE_BRANCH="${AUTO_RELEASE_STABLE_BRANCH:-stable}"
AUTO_RELEASE_STABLE_TAG="${AUTO_RELEASE_STABLE_TAG:-stable}"
AUTO_RELEASE_STABLE_WORKFLOW_FILE="${AUTO_RELEASE_STABLE_WORKFLOW_FILE:-test-and-mark-stable.yml}"
AUTO_RELEASE_STABLE_MAX_ATTEMPTS="${AUTO_RELEASE_STABLE_MAX_ATTEMPTS:-3}"
RELEASE_BLOCKER_HEAL_ENABLED="${RELEASE_BLOCKER_HEAL_ENABLED:-true}"
RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS="${RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS:-86400}"
RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS="${RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS:-21600}"
RELEASE_BLOCKER_STABLE_STALE_SECS="${RELEASE_BLOCKER_STABLE_STALE_SECS:-86400}"
RELEASE_BLOCKER_HEAL_PY="${RELEASE_BLOCKER_HEAL_PY:-${SCRIPT_DIR}/workflow_failure_heal.py}"

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::auto_release_stable.sh requires ${required_env}."
		exit 1
	fi
done
if ! [[ "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}" -lt 1 ]; then
	echo "::error::AUTO_RELEASE_STABLE_MAX_ATTEMPTS must be a positive integer (got '${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}')."
	exit 1
fi
# The reporter must never stop a release, so a bad threshold (for example a
# typo in a repository variable) warns and falls back instead of exiting.
for release_blocker_threshold_spec in RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS:86400 RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS:21600 RELEASE_BLOCKER_STABLE_STALE_SECS:86400; do
	release_blocker_threshold_name="${release_blocker_threshold_spec%%:*}"
	release_blocker_threshold_default="${release_blocker_threshold_spec#*:}"
	if ! [[ "${!release_blocker_threshold_name}" =~ ^[0-9]{1,10}$ ]]; then
		release_blocker_threshold_shown="$(printf '%s' "${!release_blocker_threshold_name}" | tr -cd 'A-Za-z0-9_.:+-')"
		echo "::warning::${release_blocker_threshold_name} must be a non-negative integer (got '${release_blocker_threshold_shown:0:40}'); using ${release_blocker_threshold_default}."
		printf -v "${release_blocker_threshold_name}" '%s' "${release_blocker_threshold_default}"
	fi
done

emit_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT}"
	fi
}

skip_release()
{
	local reason="$1"
	local detail="${2:-}"
	echo "AUTO_RELEASE_SKIPPED reason=${reason}${detail:+ ${detail}}"
	emit_output dispatched false
	emit_output sha ""
	exit 0
}

is_truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

# --- Release-blocker reporters (Refs #8291) ----------------------------------
# Every function below is called as `<fn> || true`, so errexit is off inside
# them: each step is checked explicitly and none can fail the tick.
release_blocker_fired="false"
RELEASE_BLOCKER_WORK_DIR=""
# Removes the evidence, payload, OIDC token and envelope files on every path.
trap 'if [ -n "${RELEASE_BLOCKER_WORK_DIR}" ]; then rm -rf "${RELEASE_BLOCKER_WORK_DIR}"; fi' EXIT

# Print an API-derived value reduced to safe characters, at most 200 bytes.
release_blocker_safe()
{
	local release_blocker_safe_value
	release_blocker_safe_value="$(printf '%s' "${1:-}" | tr -cd 'A-Za-z0-9_.:/@+=-')"
	printf '%s' "${release_blocker_safe_value:0:200}"
}

# Print floor(now - <iso>) clamped at 0; nothing for a missing or unparseable
# timestamp (callers treat empty as "age unknown").
release_blocker_age_from_iso()
{
	local release_blocker_iso="${1:-}"
	[ -n "${release_blocker_iso}" ] || return 0
	jq -rn --arg ts "${release_blocker_iso}" '($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601?) as $t | [((now - $t) | floor), 0] | max' 2>/dev/null || true
}

# Print the Actions URL of a run in this repository, or `none` when the id is
# not numeric (URLs are rebuilt from ids, never copied from html_url).
release_blocker_run_url()
{
	local release_blocker_url_id="${1:-}"
	if [[ "${release_blocker_url_id}" =~ ^[0-9]{1,20}$ ]]; then
		printf '%s/%s/actions/runs/%s' "$(release_blocker_safe "${GITHUB_SERVER_URL:-https://github.com}")" "${GITHUB_REPOSITORY}" "${release_blocker_url_id}"
	else
		printf 'none'
	fi
}

release_blocker_ensure_work_dir()
{
	if [ -z "${RELEASE_BLOCKER_WORK_DIR}" ]; then
		RELEASE_BLOCKER_WORK_DIR="$(mktemp -d 2>/dev/null)" || RELEASE_BLOCKER_WORK_DIR=""
	fi
	[ -n "${RELEASE_BLOCKER_WORK_DIR}" ] && [ -d "${RELEASE_BLOCKER_WORK_DIR}" ]
}

# Print the common evidence header: the fixed first line keeps the fingerprint
# stable across ticks.
release_blocker_evidence_header()
{
	local release_blocker_header_reason="$1" release_blocker_header_age="$2" release_blocker_header_threshold="$3"
	printf 'reason=%s\n' "${release_blocker_header_reason}"
	printf 'branch=%s tip=%s tag=%s tag_commit=%s\n' "$(release_blocker_safe "${AUTO_RELEASE_STABLE_BRANCH}")" "${branch_tip}" "$(release_blocker_safe "${AUTO_RELEASE_STABLE_TAG}")" "${tag_commit:-none}"
	printf 'age_secs=%s threshold_secs=%s\n' "${release_blocker_header_age}" "${release_blocker_header_threshold}"
}

release_blocker_alert()
{
	local release_blocker_alert_skip="$1" release_blocker_alert_reason="$2"
	local release_blocker_alert_pid release_blocker_alert_waited=0
	# tg_send_msg's curl has no time limit, so the send runs in the background
	# and is abandoned after 30 seconds (150 polls of 0.2 s), as in
	# promote_main_cycle.sh, so a stalled Telegram never delays the tick.
	( tg_send_msg "Release blocker report could not be dispatched (${release_blocker_alert_skip}): ${release_blocker_alert_reason} on $(release_blocker_safe "${AUTO_RELEASE_STABLE_BRANCH}"). Refs #8291" WARNING >/dev/null 2>&1 || true ) &
	release_blocker_alert_pid=$!
	while kill -0 "${release_blocker_alert_pid}" 2>/dev/null && [ "${release_blocker_alert_waited}" -lt 150 ]; do
		sleep 0.2
		release_blocker_alert_waited=$((release_blocker_alert_waited + 1))
	done
	if kill -0 "${release_blocker_alert_pid}" 2>/dev/null; then
		pkill -P "${release_blocker_alert_pid}" 2>/dev/null || true
		kill "${release_blocker_alert_pid}" 2>/dev/null || true
		echo "::warning::Release blocker Telegram alert timed out after 30s; not sent."
	fi
	wait "${release_blocker_alert_pid}" 2>/dev/null || true
}

# report_release_blocker <reason> <age_secs> <evidence_file> [failed_run_id]
# Build, sign, envelope and send one release_blocker heal report. Returns 0.
report_release_blocker()
{
	local rb_reason="$1" rb_age="$2" rb_evidence="$3" rb_run_id="${4:-}"
	local rb_branch="" rb_payload="" rb_identity="" rb_envelope="" rb_error="" rb_identity_status="" rb_detail=""
	rb_branch="$(release_blocker_safe "${AUTO_RELEASE_STABLE_BRANCH}")"
	release_blocker_fired="true"
	if ! is_truthy "${RELEASE_BLOCKER_HEAL_ENABLED}"; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled blocker=${rb_reason}"
		return 0
	fi
	if ! release_blocker_ensure_work_dir; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=${rb_reason} stage=build detail=work_dir_unavailable"
		release_blocker_alert payload_invalid "${rb_reason}"
		return 0
	fi
	rb_payload="${RELEASE_BLOCKER_WORK_DIR}/payload.json"
	rb_identity="${RELEASE_BLOCKER_WORK_DIR}/report_identity.jwt"
	rb_envelope="${RELEASE_BLOCKER_WORK_DIR}/dispatch.json"
	rb_error="${RELEASE_BLOCKER_WORK_DIR}/error.txt"
	local -a rb_build_args=(
		build-blocker-payload
		--repo "${GITHUB_REPOSITORY}"
		--workflow-name "${GITHUB_WORKFLOW:-Auto release stable}"
		--reason "${rb_reason}"
		--evidence-file "${rb_evidence}"
		--age-secs "${rb_age}"
		--head-branch "${AUTO_RELEASE_STABLE_BRANCH}"
		--head-sha "${branch_tip}"
	)
	if [ "${rb_reason}" = "last_gate_failed" ] && [[ "${rb_run_id}" =~ ^[0-9]{1,20}$ ]]; then
		rb_build_args+=(--run-id "${rb_run_id}")
	fi
	if [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]{1,20}$ ]]; then
		rb_build_args+=(
			--reporter-run-id "${GITHUB_RUN_ID}"
			--reporter-run-url "$(release_blocker_run_url "${GITHUB_RUN_ID}")"
		)
		if [[ "${GITHUB_RUN_ATTEMPT:-1}" =~ ^[0-9]{1,10}$ ]]; then
			rb_build_args+=(--reporter-run-attempt "${GITHUB_RUN_ATTEMPT:-1}")
		fi
	fi
	if ! python3 "${RELEASE_BLOCKER_HEAL_PY}" "${rb_build_args[@]}" > "${rb_payload}" 2> "${rb_error}"; then
		rb_detail="$(release_blocker_safe "$(head -c 200 "${rb_error}" 2>/dev/null | tr '\n' ' ' | tr ' ' '_')")"
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=${rb_reason} stage=build detail=${rb_detail:-none}"
		release_blocker_alert payload_invalid "${rb_reason}"
		return 0
	fi
	local -a rb_wrap_args=(wrap-dispatch --payload-json "${rb_payload}")
	if [ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
		rb_identity_status="$(python3 "${RELEASE_BLOCKER_HEAL_PY}" request-report-identity --out "${rb_identity}" 2>/dev/null || echo "identity=absent reason=helper_failed")"
		rb_identity_status="$(printf '%s' "${rb_identity_status}" | head -1 | tr -cd 'A-Za-z0-9_=. -')"
		echo "Release blocker report identity: ${rb_identity_status:-identity=absent reason=unknown} blocker=${rb_reason}"
		if [ -s "${rb_identity}" ]; then
			rb_wrap_args+=(--report-identity-file "${rb_identity}")
		fi
	else
		echo "Release blocker report identity: identity=absent reason=oidc_unavailable blocker=${rb_reason}"
	fi
	if ! python3 "${RELEASE_BLOCKER_HEAL_PY}" "${rb_wrap_args[@]}" > "${rb_envelope}" 2> "${rb_error}"; then
		rm -f "${rb_identity}"
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=${rb_reason} stage=wrap"
		release_blocker_alert payload_invalid "${rb_reason}"
		return 0
	fi
	rm -f "${rb_identity}"
	if ! gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/dispatches" --input "${rb_envelope}" >/dev/null 2> "${rb_error}"; then
		rm -f "${rb_envelope}"
		rb_detail="$(release_blocker_safe "$(head -c 200 "${rb_error}" 2>/dev/null | tr '\n' ' ' | tr ' ' '_')")"
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=dispatch_denied blocker=${rb_reason} detail=${rb_detail:-none}"
		echo "::warning::Release blocker report ${rb_reason} on ${rb_branch} could not be dispatched to ${GITHUB_REPOSITORY}."
		release_blocker_alert dispatch_denied "${rb_reason}"
		return 0
	fi
	rm -f "${rb_envelope}"
	echo "RELEASE_BLOCKER_REPORTED reason=${rb_reason} age=${rb_age} branch=${rb_branch} sha=${branch_tip} run_ref=${rb_run_id:-none}"
	return 0
}

# release_in_flight: the oldest active run across the three lists the tick
# already read, aged from run_started_at (created_at when absent).
release_blocker_check_in_flight()
{
	local rb_fields="" rb_id="" rb_status="" rb_started="" rb_file="" rb_age="" rb_evidence=""
	rb_fields="$(printf '%s\n%s\n%s\n' "${runs_json}" "${promote_runs_json}" "${legacy_runs_json}" | jq -rs --arg gate_file "${AUTO_RELEASE_STABLE_WORKFLOW_FILE}" '
		[$gate_file, "promote-main-to-stable.yml", "mark-stable.yml"] as $files
		| [to_entries[] | .key as $i | .value.workflow_runs[]?
			| select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")
			| {id: ((.id // "") | tostring), status: (.status // ""), started: ((.run_started_at // .created_at // "") | tostring), file: $files[$i]}
			| select(.started != "")]
		| sort_by(.started) | first // empty
		| .id, .status, .started, .file' 2>/dev/null)" || rb_fields=""
	{ IFS= read -r rb_id; IFS= read -r rb_status; IFS= read -r rb_started; IFS= read -r rb_file; } <<< "${rb_fields}" || true
	rb_age="$(release_blocker_age_from_iso "${rb_started:-}")"
	if [ -z "${rb_age}" ]; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=release_in_flight lookup=run_started_at"
		return 0
	fi
	if [ "${rb_age}" -lt "${RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS}" ]; then
		return 0
	fi
	release_blocker_ensure_work_dir || { echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=release_in_flight stage=build detail=work_dir_unavailable"; release_blocker_fired="true"; return 0; }
	rb_evidence="${RELEASE_BLOCKER_WORK_DIR}/evidence.txt"
	{
		release_blocker_evidence_header release_in_flight "${rb_age}" "${RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS}"
		printf 'active_run=%s workflow=%s status=%s started_at=%s active_runs=%s\n' "$(release_blocker_run_url "${rb_id}")" "$(release_blocker_safe "${rb_file}")" "$(release_blocker_safe "${rb_status}")" "$(release_blocker_safe "${rb_started}")" "${active_count}"
	} > "${rb_evidence}" 2>/dev/null || true
	report_release_blocker release_in_flight "${rb_age}" "${rb_evidence}" || true
}

# last_gate_failed: the newest failed gate run on the tip, aged from its
# updated_at (no created_at fallback, which would overstate the age).
release_blocker_check_gate_failed()
{
	local rb_fields="" rb_id="" rb_updated="" rb_conclusion="" rb_claim_id="" rb_age="" rb_evidence=""
	rb_fields="$(printf '%s' "${runs_json}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_sha == $sha and .head_branch == $branch and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "startup_failure"))] | sort_by(.created_at) | last // empty | ((.id // "") | tostring), ((.updated_at // "") | tostring), ((.conclusion // "") | tostring)' 2>/dev/null)" || rb_fields=""
	{ IFS= read -r rb_id; IFS= read -r rb_updated; IFS= read -r rb_conclusion; } <<< "${rb_fields}" || true
	rb_age="$(release_blocker_age_from_iso "${rb_updated:-}")"
	if [ -z "${rb_age}" ]; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=last_gate_failed lookup=updated_at"
		return 0
	fi
	if [ "${rb_age}" -lt "${RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS}" ]; then
		return 0
	fi
	[[ "${rb_id}" =~ ^[0-9]{1,20}$ ]] || rb_id=""
	# Claim the run in run_refs only when it has a failed job: a
	# startup_failure run has no jobs, so the intake's bind_report_claims
	# would drop it as run_not_failed and reject the report (no_bound_runs).
	# It stays named in the evidence; the report then binds its reporter run.
	case "${rb_conclusion}" in
		failure|timed_out) rb_claim_id="${rb_id}" ;;
		*) rb_claim_id="" ;;
	esac
	release_blocker_ensure_work_dir || { echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=last_gate_failed stage=build detail=work_dir_unavailable"; release_blocker_fired="true"; return 0; }
	rb_evidence="${RELEASE_BLOCKER_WORK_DIR}/evidence.txt"
	{
		release_blocker_evidence_header last_gate_failed "${rb_age}" "${RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS}"
		printf 'gate_run=%s conclusion=%s updated_at=%s attempts=%s max=%s\n' "$(release_blocker_run_url "${rb_id}")" "$(release_blocker_safe "${last_conclusion_on_tip:-}")" "$(release_blocker_safe "${rb_updated}")" "${failed_attempts_on_tip}" "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}"
	} > "${rb_evidence}" 2>/dev/null || true
	report_release_blocker last_gate_failed "${rb_age}" "${rb_evidence}" "${rb_claim_id}" || true
}

# stable_release_stale: pending_since = max(tag time, oldest unreleased commit
# date), both from responses the tick already fetched. A fresh patch on top
# of an old tag therefore does not read as stale. Only reported while no gate
# run has started on the tip (the plan's condition): once a gate ran there,
# release_in_flight / last_gate_failed own the blocker, and promoted commits
# keep their `main` dates, so a running release would otherwise read as stale.
# A completed `cancelled` run does not count: it spends no attempt and no
# other reporter owns it, so a tip whose gates keep being cancelled still
# reaches this report.
release_blocker_check_stale()
{
	local rb_tag_time="" rb_tag_age="" rb_commit_age="" rb_age="" rb_pending_since="" rb_last_gate="" rb_last_id="" rb_last_conclusion="" rb_evidence="" rb_gate_runs_on_tip=""
	if [ "${release_blocker_fired}" = "true" ]; then
		return 0
	fi
	rb_gate_runs_on_tip="$(printf '%s' "${runs_json:-}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.head_sha == $sha and .head_branch == $branch and ((.status == "completed" and .conclusion == "cancelled") | not))] | length' 2>/dev/null)" || rb_gate_runs_on_tip=""
	if ! [[ "${rb_gate_runs_on_tip}" =~ ^[0-9]+$ ]]; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=stable_release_stale lookup=gate_runs"
		return 0
	fi
	if [ "${rb_gate_runs_on_tip}" -ne 0 ]; then
		return 0
	fi
	rb_tag_time="${tag_tagger_date:-}"
	[ -n "${rb_tag_time}" ] || rb_tag_time="${compare_base_commit_date:-}"
	rb_tag_age=""
	if [[ "${tag_commit:-}" =~ ^[0-9a-f]{40}$ ]]; then
		rb_tag_age="$(release_blocker_age_from_iso "${rb_tag_time}")"
	fi
	if [ -z "${rb_tag_age}" ]; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=stable_release_stale lookup=tag_time"
		return 0
	fi
	rb_commit_age="$(release_blocker_age_from_iso "${compare_oldest_commit_date:-}")"
	if [ -z "${rb_commit_age}" ]; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=stable_release_stale lookup=commit_date"
		return 0
	fi
	if [ "${rb_tag_age}" -le "${rb_commit_age}" ]; then
		rb_age="${rb_tag_age}"
		rb_pending_since="${rb_tag_time}"
	else
		rb_age="${rb_commit_age}"
		rb_pending_since="${compare_oldest_commit_date}"
	fi
	if [ "${rb_age}" -lt "${RELEASE_BLOCKER_STABLE_STALE_SECS}" ]; then
		return 0
	fi
	rb_last_gate="$(printf '%s' "${runs_json:-}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_sha == $sha and .head_branch == $branch)] | sort_by(.created_at) | last // empty | ((.id // "") | tostring), ((.conclusion // "") | tostring)' 2>/dev/null)" || rb_last_gate=""
	{ IFS= read -r rb_last_id; IFS= read -r rb_last_conclusion; } <<< "${rb_last_gate}" || true
	release_blocker_ensure_work_dir || { echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=stable_release_stale stage=build detail=work_dir_unavailable"; release_blocker_fired="true"; return 0; }
	rb_evidence="${RELEASE_BLOCKER_WORK_DIR}/evidence.txt"
	{
		release_blocker_evidence_header stable_release_stale "${rb_age}" "${RELEASE_BLOCKER_STABLE_STALE_SECS}"
		rb_last_conclusion="$(release_blocker_safe "${rb_last_conclusion:-}")"
		printf 'pending_since=%s tag_time=%s oldest_unreleased_commit=%s last_gate_on_tip=%s conclusion=%s\n' "$(release_blocker_safe "${rb_pending_since}")" "$(release_blocker_safe "${rb_tag_time}")" "$(release_blocker_safe "${compare_oldest_commit_date}")" "$(release_blocker_run_url "${rb_last_id:-}")" "${rb_last_conclusion:-none}"
	} > "${rb_evidence}" 2>/dev/null || true
	report_release_blocker stable_release_stale "${rb_age}" "${rb_evidence}" || true
}

if ! is_truthy "${AUTO_RELEASE_STABLE_ENABLED}"; then
	skip_release disabled
fi

branch_tip="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${AUTO_RELEASE_STABLE_BRANCH}" | jq -r '.object.sha // empty')"
if ! [[ "${branch_tip}" =~ ^[0-9a-f]{40}$ ]]; then
	echo "::error::Could not resolve branch ${AUTO_RELEASE_STABLE_BRANCH} on ${GITHUB_REPOSITORY}."
	exit 1
fi

# The stable tag is annotated (`git tag -a` in the release job), so the ref
# points at a tag object that must be dereferenced to its commit.
tag_ref_error_file="$(mktemp)"
tag_missing="false"
if ! tag_ref_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${AUTO_RELEASE_STABLE_TAG}" 2>"${tag_ref_error_file}")"; then
	if grep -qE '(^|[^0-9])404([^0-9]|$)' "${tag_ref_error_file}"; then
		tag_ref_json=""
		tag_missing="true"
	else
		cat "${tag_ref_error_file}" >&2
		rm -f "${tag_ref_error_file}"
		skip_release guard_unavailable "lookup=tag:${AUTO_RELEASE_STABLE_TAG}"
	fi
fi
rm -f "${tag_ref_error_file}"
tag_object_type="$(printf '%s' "${tag_ref_json}" | jq -r '.object.type // empty' 2>/dev/null || true)"
tag_object_sha="$(printf '%s' "${tag_ref_json}" | jq -r '.object.sha // empty' 2>/dev/null || true)"
tag_commit=""
# Release-blocker staleness inputs, captured from responses this tick already
# reads (no extra API call): the annotated tag's tagger date, and the compare
# response's oldest unreleased commit date and base (tag) commit date.
tag_tagger_date=""
compare_oldest_commit_date=""
compare_base_commit_date=""
if [ "${tag_object_type}" = "tag" ] && [[ "${tag_object_sha}" =~ ^[0-9a-f]{40}$ ]]; then
	if ! tag_object_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/tags/${tag_object_sha}")" \
		|| ! tag_commit="$(printf '%s' "${tag_object_json}" | jq -er '.object.sha | select(test("^[0-9a-f]{40}$"))')"; then
		skip_release guard_unavailable "lookup=tag-object:${tag_object_sha}"
	fi
	tag_tagger_date="$(printf '%s' "${tag_object_json}" | jq -r '.tagger.date // empty | tostring' 2>/dev/null || true)"
elif [ "${tag_object_type}" = "commit" ]; then
	tag_commit="${tag_object_sha}"
fi
if [ -z "${tag_commit}" ]; then
	if [ "${tag_missing}" = "true" ]; then
		echo "::warning::Tag ${AUTO_RELEASE_STABLE_TAG} does not exist on ${GITHUB_REPOSITORY}; treating the branch as unreleased."
	else
		skip_release guard_unavailable "lookup=tag:${AUTO_RELEASE_STABLE_TAG}"
	fi
fi

if [ "${branch_tip}" = "${tag_commit}" ]; then
	skip_release up_to_date "sha=${branch_tip}"
fi
if [[ "${tag_commit}" =~ ^[0-9a-f]{40}$ ]]; then
	# A differing tip only proves movement. Release only when the branch is
	# strictly ahead of the tag commit; a rewound or diverged branch would
	# retag consumers from history the release gate never validated.
	# One jq reduces the (possibly 250-commit) response to three lines: the
	# status, the oldest unreleased commit date and the base commit date.
	compare_fields="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/compare/${tag_commit}...${branch_tip}" | jq -r '(.status // "" | tostring), ([.commits[]?.commit.committer.date // empty | tostring] | min // ""), (.base_commit.commit.committer.date // "" | tostring) | gsub("[\\r\\n]"; "")')"
	{ IFS= read -r compare_status; IFS= read -r compare_oldest_commit_date; IFS= read -r compare_base_commit_date; } <<< "${compare_fields}" || true
	case "${compare_status}" in
		ahead) ;;
		identical) skip_release up_to_date "sha=${branch_tip}" ;;
		*)
			echo "::warning::${AUTO_RELEASE_STABLE_BRANCH} (${branch_tip}) is '${compare_status:-unknown}' relative to the ${AUTO_RELEASE_STABLE_TAG} tag commit (${tag_commit}); refusing to release a rewound or diverged branch."
			skip_release branch_not_ahead "sha=${branch_tip} tag=${tag_commit} status=${compare_status:-unknown}"
			;;
	esac
fi

runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${AUTO_RELEASE_STABLE_WORKFLOW_FILE}/runs?per_page=30")"
# Any branch: the gate's e2e-smoke-test job runs under a per-repository
# cancel-in-progress group, so dispatching a stable gate while the promote
# cycle's gate_only run on main is active would cancel that run.
active_count="$(printf '%s' "${runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
# The gate-runs endpoint above cannot report a promotion that has moved
# `stable` but has not dispatched its gate yet, so this requires a separate
# workflow-scoped read.
promote_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/promote-main-to-stable.yml/runs?per_page=30")"
# Any event: a scheduled cycle tick is about to dispatch (or is waiting on)
# its own gate run, which the group above would otherwise cancel.
promote_active_count="$(printf '%s' "${promote_runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
active_count=$((active_count + promote_active_count))
# The legacy manual release path (mark-stable.yml) also writes refs/tags/stable.
legacy_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/mark-stable.yml/runs?per_page=30")"
legacy_active_count="$(printf '%s' "${legacy_runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
active_count=$((active_count + legacy_active_count))
if [ "${active_count}" -gt 0 ]; then
	release_blocker_check_in_flight || true
	release_blocker_check_stale || true
	skip_release release_in_flight "active_runs=${active_count}"
fi
# Restrict to runs on the stable branch: the promote cycle runs the same
# workflow in gate_only mode on the default branch, and when the two branches
# share a tip a failed smoke gate there must not read as a failed release.
# `cancelled` is deliberately not counted: a cancelled gate proved nothing
# (concurrency, a runner loss, an operator), so the next tick simply tries
# again instead of spending an attempt.
failed_attempts_on_tip="$(printf '%s' "${runs_json}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_sha == $sha and .head_branch == $branch and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "startup_failure"))] | length')"
[[ "${failed_attempts_on_tip}" =~ ^[0-9]+$ ]] || failed_attempts_on_tip=0
if [ "${failed_attempts_on_tip}" -ge "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}" ]; then
	last_conclusion_on_tip="$(printf '%s' "${runs_json}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_sha == $sha and .head_branch == $branch and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "startup_failure"))] | sort_by(.created_at) | last | .conclusion // empty')"
	echo "::warning::${AUTO_RELEASE_STABLE_WORKFLOW_FILE} already failed ${failed_attempts_on_tip} time(s) on ${AUTO_RELEASE_STABLE_BRANCH}@${branch_tip} (last '${last_conclusion_on_tip}', budget ${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}); not re-dispatching until the branch moves or a human re-runs the gate."
	release_blocker_check_gate_failed || true
	release_blocker_check_stale || true
	skip_release last_gate_failed "sha=${branch_tip} conclusion=${last_conclusion_on_tip} attempts=${failed_attempts_on_tip} max=${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}"
fi
if [ "${failed_attempts_on_tip}" -gt 0 ]; then
	echo "Retrying ${AUTO_RELEASE_STABLE_WORKFLOW_FILE} on ${AUTO_RELEASE_STABLE_BRANCH}@${branch_tip}: ${failed_attempts_on_tip} failed attempt(s) so far, budget ${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}."
fi

# The gate is still dispatched after a stable_release_stale report.
release_blocker_check_stale || true
echo "Dispatching ${AUTO_RELEASE_STABLE_WORKFLOW_FILE} on ${AUTO_RELEASE_STABLE_BRANCH} (tip ${branch_tip}, tag at ${tag_commit:-none})."
# Fully qualified: `stable` is also a tag, and the release gate accepts
# only refs/heads/stable.
gh_retry gh workflow run "${AUTO_RELEASE_STABLE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "refs/heads/${AUTO_RELEASE_STABLE_BRANCH}"
echo "AUTO_RELEASE_DISPATCHED sha=${branch_tip}"
emit_output dispatched true
emit_output sha "${branch_tip}"
