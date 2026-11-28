#!/usr/bin/env bash
# Daily issue/PR audit: step bodies for the collect-logs job of
# .github/workflows/workflow-log-analysis.yml (issue #10153).
#
# Usage: bash scripts/issue_pr_audit_run.sh <collect|scrub-credentials|analyze|deliver|telegram>
#
# Each subcommand is one workflow step, so every step holds only the secrets
# it needs:
#   collect            GH_TOKEN (GH_PAT): discover repositories, read issues
#                      and PRs, tier-1 rules, model input (scripts/issue_pr_audit.py).
#   scrub-credentials  removes the checkout's persisted github.token before the
#                      model step (D8). No secret.
#   analyze            the model pass, role ISSUE_PR_AUDIT through the trusted
#                      engine root (CLAUDE_ENGINE_SUPPORT_DIR), codex read-only
#                      as the fallback. No GH_PAT, no Telegram secret. The
#                      report gets the trusted marker and Metrics Appendix and
#                      is checked (scripts/check_issue_pr_audit_report.py),
#                      with one retry.
#   deliver            GH_TOKEN / REPORT_PUSH_TOKEN (GH_PAT): state update,
#                      purge of processed old reports, the run branch
#                      auto/issue-pr-audit-report-<run>-<attempt>, and the PR
#                      through scripts/workflow_log_analysis_deliver_report.sh.
#   telegram           TG_BOT_SECRET / TG_ADMIN_CHAT_ID: one blocked-items
#                      report per UTC day (tg_send_report_msg, plain text).
#
# Every subcommand returns 0 for handled outcomes (disabled, no digest,
# invalid report, delivery failure), so the existing daily job keeps its
# result; the outcome is in the log lines below.
#
# Env (all optional; defaults in parentheses):
#   ISSUE_PR_AUDIT_ENABLED (true)          kill switch
#   ISSUE_PR_AUDIT_TIMEOUT_MINUTES (35)    wall clock of the model pass, retry included
#   ISSUE_PR_AUDIT_TG_ALREADY_SENT (false) the per-day cache marker was found
#   ISSUE_PR_AUDIT_REPORT_URL ()           report PR URL for the Telegram footer
#   WORKFLOW_EDITOR_MODEL (openai/gpt-6-sol) codex fallback model
#   WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS (30) purge window
#   APPLY_ANALYSIS_REPORT_PATH (analysis/recommendation-processing-report.md)
#   ISSUE_PR_AUDIT_* knobs read by scripts/issue_pr_audit.py
#
# Log lines (stable prefixes): ISSUE_PR_AUDIT_START, ISSUE_PR_AUDIT_SKIPPED
# reason=, ISSUE_PR_AUDIT_REPORT outcome=valid|retried|invalid,
# ISSUE_PR_AUDIT_TG outcome=sent|skipped reason=, ISSUE_PR_AUDIT_END outcome=,
# plus the ISSUE_PR_AUDIT_* lines of the Python helpers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IPA_WORKDIR="${ISSUE_PR_AUDIT_WORKDIR:-${RUNNER_TEMP:-/tmp}/issue-pr-audit}"
IPA_STATE_PATH="analysis/issue_pr_audit_state.json"
IPA_PY=(python3 -B "${SCRIPT_DIR}/issue_pr_audit.py" --workdir "${IPA_WORKDIR}")
export PYTHONDONTWRITEBYTECODE=1

ipa_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT}"
	fi
}

ipa_enabled()
{
	case "$(printf '%s' "${ISSUE_PR_AUDIT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		false|0|no|off) return 1 ;;
	esac
	return 0
}

ipa_redact()
{
	sed -E 's#(https?://)[^/@[:space:]]+@#\1***@#g' "$1" >&2 || true
}

IPA_CLEANUP_WORKTREE=""
IPA_CLEANUP_DIR=""
ipa_cleanup()
{
	if [ -n "${IPA_CLEANUP_WORKTREE}" ]; then
		git -C "${REPO_ROOT}" worktree remove --force "${IPA_CLEANUP_WORKTREE}" >/dev/null 2>&1 || true
		git -C "${REPO_ROOT}" worktree prune >/dev/null 2>&1 || true
	fi
	if [ -n "${IPA_CLEANUP_DIR}" ]; then
		rm -rf -- "${IPA_CLEANUP_DIR}"
	fi
}

ipa_collect()
{
	mkdir -p "${IPA_WORKDIR}"
	rm -f "${IPA_WORKDIR}"/raw.json "${IPA_WORKDIR}"/digest.json "${IPA_WORKDIR}"/state-next.json "${IPA_WORKDIR}"/model-input.md \
		"${IPA_WORKDIR}"/report.md "${IPA_WORKDIR}"/report.valid "${IPA_WORKDIR}"/delivered.flag
	ipa_output day "$(date -u +%Y-%m-%d)"
	echo "ISSUE_PR_AUDIT_START run=${GITHUB_RUN_ID:-0} event=${GITHUB_EVENT_NAME:-unknown}"
	if [ -z "${GH_TOKEN:-}" ]; then
		echo "ISSUE_PR_AUDIT_SKIPPED reason=token_missing"
		return 0
	fi
	cd "${REPO_ROOT}"
	if ! "${IPA_PY[@]}" collect --state "${IPA_STATE_PATH}" --checkout "${REPO_ROOT}"; then
		echo "::warning::Issue/PR audit data collection failed."
		echo "ISSUE_PR_AUDIT_SKIPPED reason=collect_failed"
		return 0
	fi
	# A tier-1 or render error is a handled outcome too: the daily job keeps its result.
	if ! "${IPA_PY[@]}" classify --state "${IPA_STATE_PATH}" || ! "${IPA_PY[@]}" render-input; then
		echo "::warning::Issue/PR audit classification failed."
		echo "ISSUE_PR_AUDIT_SKIPPED reason=collect_failed"
		rm -f "${IPA_WORKDIR}"/digest.json "${IPA_WORKDIR}"/model-input.md
		return 0
	fi
	ipa_output digest_ready true
}

ipa_scrub_credentials()
{
	# The checkout persisted github.token for the log collector's git fetch;
	# the model step must not inherit it (D8). Delivery uses a one-shot helper.
	git -C "${REPO_ROOT}" config --local --unset-all http.https://github.com/.extraheader >/dev/null 2>&1 || true
	echo "INFO: removed the persisted checkout credential before the issue/PR audit model pass."
}

ipa_engine_ready()
{
	[ -n "${CLAUDE_ENGINE_SUPPORT_DIR:-}" ] && [ -f "${CLAUDE_ENGINE_SUPPORT_DIR}/scripts/ai_engine.sh" ] && [ ! -L "${CLAUDE_ENGINE_SUPPORT_DIR}/scripts/ai_engine.sh" ]
}

# $1 prompt file, $2 output file, $3 timeout seconds.
ipa_run_model()
{
	local prompt="$1" out="$2" limit="$3"
	local model="${WORKFLOW_EDITOR_MODEL:-openai/gpt-6-sol}"
	local codex_cmd=(bash "${SCRIPT_DIR}/codex_isolated_exec.sh" run --mode read-only --workdir "${REPO_ROOT}" -- \
		--ask-for-approval never -c model_verbosity=low exec --skip-git-repo-check --model "${model}" --sandbox read-only)
	(
		# The agent gets no GitHub token, no Telegram secret, no OIDC request.
		unset GH_TOKEN GITHUB_TOKEN REPORT_PUSH_TOKEN TG_BOT_SECRET TG_ADMIN_CHAT_ID TG_CHAT_ID \
			AI_COMMENT_SIGNING_SECRET AI_COMMENT_JOB_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL
		export AI_ENGINE_READ_ONLY=true
		if ipa_engine_ready; then
			AI_ENGINE_MODEL_HINT="${model}" AI_ENGINE_EFFORT_HINT="" \
				timeout -k 30 "${limit}" bash -c 'source "$0" || exit 2; claude_run_selected "$@"' \
				"${CLAUDE_ENGINE_SUPPORT_DIR}/scripts/ai_engine.sh" \
				ISSUE_PR_AUDIT "${prompt}" "${out}" "${REPO_ROOT}" --codex-stdio -- "${codex_cmd[@]}"
		else
			echo "AI_ENGINE_FALLBACK role=ISSUE_PR_AUDIT reason=engine_support_missing" >&2
			timeout -k 30 "${limit}" "${codex_cmd[@]}" < "${prompt}" > "${out}"
		fi
	)
}

ipa_analyze()
{
	if [ ! -s "${IPA_WORKDIR}/digest.json" ] || [ ! -f "${IPA_WORKDIR}/model-input.md" ]; then
		echo "ISSUE_PR_AUDIT_SKIPPED reason=no_digest stage=analyze"
		return 0
	fi
	local minutes="${ISSUE_PR_AUDIT_TIMEOUT_MINUTES:-35}"
	[[ "${minutes}" =~ ^[0-9]{1,3}$ ]] && [ "${minutes}" -ge 1 ] || minutes=35
	local deadline=$(( $(date +%s) + 10#${minutes} * 60 ))
	local prompt="${IPA_WORKDIR}/prompt.md" model_out="${IPA_WORKDIR}/model-output.md" report="${IPA_WORKDIR}/report.md"
	local notes="${IPA_WORKDIR}/check-notes.md" attempt=0 rc=0 check_rc=1 started remaining outcome="invalid"
	local prompt_template="${REPO_ROOT}/prompts/mode-issue-pr-audit.txt"
	rm -f "${notes}" "${report}" "${IPA_WORKDIR}/report.valid"
	while [ "${attempt}" -lt 2 ]; do
		attempt=$((attempt + 1))
		remaining=$(( deadline - $(date +%s) ))
		if [ "${remaining}" -lt 120 ]; then
			echo "::warning::Issue/PR audit model pass is out of time (attempt ${attempt})."
			break
		fi
		if ! bash "${SCRIPT_DIR}/render_prompt.sh" "${prompt_template}" > "${prompt}.head" 2>/dev/null || ! [ -s "${prompt}.head" ]; then
			cat "${prompt_template}" > "${prompt}.head"
		fi
		{
			cat "${prompt}.head"
			printf '\n'
			cat "${IPA_WORKDIR}/model-input.md"
			if [ -s "${notes}" ]; then
				printf '\n## Structure check of your previous answer\n'
				cat "${notes}"
			fi
		} > "${prompt}"
		: > "${model_out}"
		started=$(date +%s)
		rc=0
		ipa_run_model "${prompt}" "${model_out}" "${remaining}" || rc=$?
		if [ "${rc}" -eq 80 ]; then
			echo "AI_ENGINE_CAPACITY_WAIT role=ISSUE_PR_AUDIT outcome=deferred stage=issue_pr_audit"
			break
		fi
		if [ "${rc}" -ne 0 ] || ! grep -q '[^[:space:]]' "${model_out}"; then
			echo "::warning::Issue/PR audit model pass attempt ${attempt} failed (rc=${rc})."
			continue
		fi
		# Neither engine path hands token usage back to this script, so the
		# appendix states the gap instead of leaving the lines out.
		"${IPA_PY[@]}" finalize --model-output "${model_out}" --out "${report}" \
			--metric "model_attempt=${attempt}" --metric "model_seconds=$(( $(date +%s) - started ))" --metric "engine_rc=${rc}" \
			--metric "model_tokens=unavailable" --metric "model_cost=unavailable"
		check_rc=0
		python3 -I -B "${SCRIPT_DIR}/check_issue_pr_audit_report.py" --report "${report}" --digest "${IPA_WORKDIR}/digest.json" --errors-out "${notes}" || check_rc=$?
		if [ "${check_rc}" -eq 0 ]; then
			outcome="valid"
			[ "${attempt}" -gt 1 ] && outcome="retried"
			break
		fi
	done
	echo "ISSUE_PR_AUDIT_REPORT outcome=${outcome} attempts=${attempt}"
	if [ "${outcome}" != "invalid" ]; then
		: > "${IPA_WORKDIR}/report.valid"
		ipa_output report_valid true
	else
		ipa_output report_valid false
	fi
	if [ -s "${report}" ]; then
		ipa_output report_path "${report}"
	fi
	return 0
}

ipa_deliver()
{
	if [ ! -f "${IPA_WORKDIR}/report.valid" ] || [ ! -s "${IPA_WORKDIR}/report.md" ]; then
		echo "ISSUE_PR_AUDIT_SKIPPED reason=report_invalid stage=deliver"
		return 0
	fi
	if [ -z "${REPORT_PUSH_TOKEN:-}" ] || [ -z "${GH_TOKEN:-}" ]; then
		echo "ISSUE_PR_AUDIT_SKIPPED reason=token_missing stage=deliver"
		return 0
	fi
	if ! [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || ! [[ "${GITHUB_RUN_ATTEMPT:-1}" =~ ^[0-9]+$ ]]; then
		echo "ISSUE_PR_AUDIT_SKIPPED reason=run_context_invalid stage=deliver"
		return 0
	fi
	cd "${REPO_ROOT}"
	local today branch report_rel suffix work wt push_log rc=0
	today="$(date -u +%Y-%m-%d)"
	branch="auto/issue-pr-audit-report-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
	report_rel="analysis/issue-pr-audit-${today}.md"
	suffix=2
	while [ -e "${report_rel}" ]; do
		report_rel="analysis/issue-pr-audit-${today}-${suffix}.md"
		suffix=$((suffix + 1))
	done
	work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/issue-pr-audit-deliver.XXXXXX")"
	wt="${work}/wt"
	IPA_CLEANUP_WORKTREE="${wt}"
	IPA_CLEANUP_DIR="${work}"
	trap ipa_cleanup EXIT
	"${IPA_PY[@]}" state-update --out "${work}/state.json" --today "${today}"

	# Purge (D1): processed, not in flight, past the retention window. Any
	# unavailable guard purges nothing.
	local in_flight_file="${work}/in-flight.txt" purged=() purged_doc retention="${WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS:-30}"
	[[ "${retention}" =~ ^[0-9]{1,4}$ ]] && [ "${retention}" -ge 1 ] || retention=30
	if ! APPLY_ANALYSIS_IN_FLIGHT_DOCS=true bash "${SCRIPT_DIR}/apply_analysis_on_main.sh" > "${in_flight_file}" 2>"${work}/in-flight.err"; then
		rm -f "${in_flight_file}"
	fi
	while IFS= read -r purged_doc; do
		[[ "${purged_doc}" =~ ^analysis/issue-pr-audit-[0-9]{4}-[0-9]{2}-[0-9]{2}(-[0-9]+)?\.md$ ]] && purged+=("${purged_doc}")
	done < <("${IPA_PY[@]}" purge --checkout "${REPO_ROOT}" --in-flight-file "${in_flight_file}" --retention-days "${retention}" \
		--processing-report "${APPLY_ANALYSIS_REPORT_PATH:-analysis/recommendation-processing-report.md}" --today "${today}" || true)

	if ! git worktree add --detach "${wt}" HEAD > "${work}/worktree.log" 2>&1; then
		ipa_redact "${work}/worktree.log"
		echo "::error::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=worktree_failed branch=${branch}"
		return 0
	fi
	mkdir -p "${wt}/analysis"
	cp "${IPA_WORKDIR}/report.md" "${wt}/${report_rel}"
	cp "${work}/state.json" "${wt}/${IPA_STATE_PATH}"
	git -C "${wt}" add -- "${report_rel}" "${IPA_STATE_PATH}"
	for purged_doc in ${purged[@]+"${purged[@]}"}; do
		git -C "${wt}" rm -q -- "${purged_doc}" || true
	done
	git -C "${wt}" -c user.name="github-actions[bot]" -c user.email="github-actions[bot]@users.noreply.github.com" \
		commit -q -m "chore: add issue/PR audit report ($(basename "${report_rel}"))" -m "Report-file: ${report_rel}"
	export GIT_CONFIG_COUNT=1
	export GIT_CONFIG_KEY_0=credential.helper
	export GIT_CONFIG_VALUE_0='!f() { printf "username=x-access-token\npassword=%s\n" "${REPORT_PUSH_TOKEN}"; }; f'
	push_log="${work}/push.log"
	if ! git -C "${wt}" push origin "HEAD:refs/heads/${branch}" > "${push_log}" 2>&1; then
		ipa_redact "${push_log}"
		echo "::error::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=push_failed branch=${branch}"
		return 0
	fi
	unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0
	REPORT_BRANCH="${branch}" REPORT_FILE="${report_rel}" PURGED_DOCS="${purged[*]:-}" \
		WLA_DELIVER_BRANCH_PREFIX="auto/issue-pr-audit-report" \
		WLA_DELIVER_DOC_PATTERN='^analysis/issue-pr-audit-[0-9]{4}-[0-9]{2}-[0-9]{2}(-[0-9]+)?\.md$' \
		WLA_DELIVER_EXTRA_FILES="${IPA_STATE_PATH}" \
		WLA_DELIVER_SECTIONS_ONLY=false AUDIT_STATUS="" API_REDUNDANCY_STATUS="" \
		bash "${SCRIPT_DIR}/workflow_log_analysis_deliver_report.sh" > "${work}/deliver.log" 2>&1 || rc=$?
	cat "${work}/deliver.log"
	if [ "${rc}" -ne 0 ]; then
		return 0
	fi
	local pr_number
	pr_number="$(sed -nE 's/^WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=pr_opened .* pr=([0-9]+) .*/\1/p' "${work}/deliver.log" | tail -n 1)"
	: > "${IPA_WORKDIR}/delivered.flag"
	if [[ "${pr_number}" =~ ^[0-9]+$ ]]; then
		ipa_output pr_url "${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/pull/${pr_number}"
	fi
	return 0
}

ipa_telegram()
{
	local today parts_dir decision outcome="completed" sent=0 part msg_id
	today="$(date -u +%Y-%m-%d)"
	if [ ! -s "${IPA_WORKDIR}/digest.json" ]; then
		outcome="failed"
	elif [ ! -f "${IPA_WORKDIR}/report.valid" ] || [ ! -f "${IPA_WORKDIR}/delivered.flag" ]; then
		outcome="degraded"
	fi
	parts_dir="${IPA_WORKDIR}/tg-parts"
	local report_arg=()
	if [ -f "${IPA_WORKDIR}/report.valid" ]; then
		report_arg=(--report "${IPA_WORKDIR}/report.md")
	fi
	local link="${ISSUE_PR_AUDIT_REPORT_URL:-}"
	[ -n "${link}" ] || link="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-0}"
	cd "${REPO_ROOT}"
	decision="$("${IPA_PY[@]}" telegram --state "${IPA_STATE_PATH}" ${report_arg[@]+"${report_arg[@]}"} --link "${link}" \
		--cache-hit "${ISSUE_PR_AUDIT_TG_ALREADY_SENT:-false}" --out-dir "${parts_dir}" --today "${today}" || echo "reason=render_failed")"
	case "${decision}" in
		reason=ok*) ;;
		*)
			echo "ISSUE_PR_AUDIT_TG outcome=skipped ${decision%% *}"
			echo "ISSUE_PR_AUDIT_END outcome=${outcome}"
			return 0
			;;
	esac
	if [ "$(printf '%s' "${ALERT_MSG_LEVEL:-}" | tr '[:lower:]' '[:upper:]')" = "SILENT" ]; then
		echo "ISSUE_PR_AUDIT_TG outcome=skipped reason=silenced"
		echo "ISSUE_PR_AUDIT_END outcome=${outcome}"
		return 0
	fi
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}" ] || [ ! -f "${SCRIPT_DIR}/tg_helpers.sh" ]; then
		echo "ISSUE_PR_AUDIT_TG outcome=skipped reason=telegram_not_configured"
		echo "ISSUE_PR_AUDIT_END outcome=${outcome}"
		return 0
	fi
	# shellcheck disable=SC1091
	source "${SCRIPT_DIR}/tg_helpers.sh"
	for part in "${parts_dir}"/part-*.txt; do
		[ -f "${part}" ] || continue
		msg_id="$(tg_send_report_msg "$(cat "${part}")" || true)"
		if [ -n "${msg_id}" ]; then
			sent=$((sent + 1))
		fi
	done
	if [ "${sent}" -gt 0 ]; then
		mkdir -p "${ISSUE_PR_AUDIT_TG_MARKER_DIR:-${RUNNER_TEMP:-/tmp}/issue-pr-audit-tg-day}"
		printf '%s\n' "${today}" > "${ISSUE_PR_AUDIT_TG_MARKER_DIR:-${RUNNER_TEMP:-/tmp}/issue-pr-audit-tg-day}/day"
		ipa_output sent true
		echo "ISSUE_PR_AUDIT_TG outcome=sent parts=${sent}"
	else
		echo "ISSUE_PR_AUDIT_TG outcome=skipped reason=send_failed"
	fi
	echo "ISSUE_PR_AUDIT_END outcome=${outcome}"
	return 0
}

command="${1:-}"
case "${command}" in
	collect|scrub-credentials|analyze|deliver|telegram) ;;
	*)
		echo "usage: $0 <collect|scrub-credentials|analyze|deliver|telegram>" >&2
		exit 2
		;;
esac
if ! ipa_enabled; then
	echo "ISSUE_PR_AUDIT_SKIPPED reason=disabled stage=${command}"
	exit 0
fi
case "${command}" in
	collect) ipa_collect ;;
	scrub-credentials) ipa_scrub_credentials ;;
	analyze) ipa_analyze ;;
	deliver) ipa_deliver ;;
	telegram) ipa_telegram ;;
esac
