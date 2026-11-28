#!/usr/bin/env bash
# alert_triage_route.sh — hourly alert triage: fetch, diagnose, route
# (alert triage plan Phase 2, Refs #9394; docs/plans/alert-triage-auto-heal-plan.md).
#
# Body of .github/workflows/alert-triage.yml. scripts/alert_triage.py makes
# every decision (no model there); this script does the side effects:
#
#   fetch     clone branch ai/alert-inbox at depth 1 into ALERT_TRIAGE_INBOX_DIR
#             (token through GIT_CONFIG_* env, never argv). No branch:
#             `ALERT_TRIAGE skip reason=no_inbox`, present=false, exit 0.
#   prepare   alert_triage.py prepare (2 list calls, bounded evidence GETs).
#   diagnose  one sandboxed ALERT_TRIAGE engine call per model group
#             (Claude, codex fallback through scripts/ai_engine.sh), no GitHub
#             token or OIDC variables in the engine's environment, then
#             alert_triage.py parse. No new call starts past
#             ALERT_TRIAGE_RUN_DEADLINE_SECS; groups not reached stay
#             unmarked and the next run retries them.
#   route     alert_triage.py decide + apply, ONE commit of every done marker
#             and ledger update, a non-force push (3 attempts, re-applied on
#             the new tip), and only after the push succeeds: the heal
#             repository_dispatch (kind alert_triage), the alert-self-fix.yml
#             workflow_dispatch and the escalations (tg_send_msg_direct, never
#             captured, so triage never triages its own alerts). With
#             ALERT_TRIAGE_DRY_RUN (default true) the markers and ledger
#             occurrences are committed but nothing is dispatched or sent.
#
# Variables (every one has a default): ALERT_TRIAGE_DRY_RUN=true,
# ALERT_TRIAGE_SELF_FIX_ENABLED=false, ALERT_TRIAGE_RUN_DEADLINE_SECS=2400,
# ALERT_TRIAGE_ENGINE_TIMEOUT_SECS=600, ALERT_TRIAGE_MODEL=openai/gpt-6-sol,
# ALERT_TRIAGE_DEFAULT_BRANCH=main, ALERT_TRIAGE_INBOX_DIR and
# ALERT_TRIAGE_WORK_DIR under $RUNNER_TEMP, ALERT_TRIAGE_REMOTE_URL (this
# repository on $GITHUB_SERVER_URL). The ALERT_TRIAGE_MAX_* / *_HOURS limits
# are read by alert_triage.py.
#
# Log lines (stdout, stable prefix ALERT_TRIAGE):
#   ALERT_TRIAGE skip reason=no_inbox|no_records
#   ALERT_TRIAGE prepare records= groups= tracked= capped= model_groups= list_calls= evidence_gets=
#   ALERT_TRIAGE diagnose fp=<fp12> outcome=ok|engine_failed|parse_failed|timeout|deadline
#   ALERT_TRIAGE decision fp=<fp12> classification= route= reason= dry_run= records=
#   ALERT_TRIAGE dispatch kind=heal|self_fix fp=<fp12> outcome=sent|failed|skipped_dry_run
#   ALERT_TRIAGE escalate target=group|issue|pr id=<id> outcome=sent|failed|dry_run
#   ALERT_TRIAGE commit outcome=pushed|noop|failed attempts= markers= pruned=

set -euo pipefail

AT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AT_REPO_ROOT="$(dirname "${AT_SCRIPT_DIR}")"
AT_PY="${AT_SCRIPT_DIR}/alert_triage.py"
AT_HEAL_PY="${AT_SCRIPT_DIR}/workflow_failure_heal.py"
AT_BRANCH="ai/alert-inbox"
AT_SELF_REPO="${GITHUB_REPOSITORY:-shubhodeep1/coding-workflows}"
AT_SERVER="${GITHUB_SERVER_URL:-https://github.com}"
AT_TMP="${RUNNER_TEMP:-/tmp}"
AT_INBOX_DIR="${ALERT_TRIAGE_INBOX_DIR:-${AT_TMP}/alert-inbox}"
AT_WORK_DIR="${ALERT_TRIAGE_WORK_DIR:-${AT_TMP}/alert-triage}"
AT_REMOTE_URL="${ALERT_TRIAGE_REMOTE_URL:-${AT_SERVER}/${AT_SELF_REPO}.git}"
AT_RUN_ID="${GITHUB_RUN_ID:-0}"
AT_RUN_URL="${AT_SERVER}/${AT_SELF_REPO}/actions/runs/${AT_RUN_ID}"
export PYTHONDONTWRITEBYTECODE=1

_at_log()
{
	printf 'ALERT_TRIAGE %s\n' "$*"
}

if ! [[ "${AT_SELF_REPO}" =~ ^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$ ]] || ! [[ "${AT_RUN_ID}" =~ ^[0-9]{1,20}$ ]]; then
	_at_log "error reason=bad_run_context"
	exit 1
fi

AT_DRY_RUN="true"
[ "$(printf '%s' "${ALERT_TRIAGE_DRY_RUN:-true}" | tr '[:upper:]' '[:lower:]')" = "false" ] && AT_DRY_RUN="false"
AT_SELF_FIX_ENABLED="false"
[ "$(printf '%s' "${ALERT_TRIAGE_SELF_FIX_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ] && AT_SELF_FIX_ENABLED="true"

_at_py()
{
	python3 -B "${AT_PY}" --work-dir "${AT_WORK_DIR}" "$@"
}

# _at_git ARGS — git on the inbox clone; an https remote gets the token as an
# extra header through GIT_CONFIG_* (never on argv, never logged).
_at_git()
{
	local token="${GITHUB_TOKEN:-}"
	if [ -n "${token}" ] && [[ "${AT_REMOTE_URL}" == https://* ]]; then
		GIT_CONFIG_COUNT=1 \
		GIT_CONFIG_KEY_0="http.${AT_SERVER}/.extraheader" \
		GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${token}" | base64 | tr -d '\n')" \
			git -C "${AT_INBOX_DIR}" "$@"
	else
		git -C "${AT_INBOX_DIR}" "$@"
	fi
}

_at_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s\n' "$1" >> "${GITHUB_OUTPUT}"
	fi
}

# --- fetch -----------------------------------------------------------------

at_fetch()
{
	local heads="" rc=0
	# The clone lives under RUNNER_TEMP and belongs to this job only.
	rm -rf -- "${AT_INBOX_DIR}"
	mkdir -p "${AT_INBOX_DIR}"
	git init -q "${AT_INBOX_DIR}"
	git -C "${AT_INBOX_DIR}" remote add origin "${AT_REMOTE_URL}"
	heads="$(_at_git ls-remote --heads origin "refs/heads/${AT_BRANCH}" 2>/dev/null)" || rc=$?
	if [ "${rc}" -ne 0 ]; then
		_at_log "fetch outcome=failed reason=ls_remote"
		_at_output "present=false"
		exit 1
	fi
	if [ -z "${heads}" ]; then
		_at_log "skip reason=no_inbox"
		_at_output "present=false"
		exit 0
	fi
	if ! _at_git fetch -q --no-tags --depth=1 origin "+refs/heads/${AT_BRANCH}:refs/remotes/origin/${AT_BRANCH}" 2>/dev/null \
		|| ! git -C "${AT_INBOX_DIR}" checkout -q -B "${AT_BRANCH}" "refs/remotes/origin/${AT_BRANCH}"; then
		_at_log "fetch outcome=failed reason=fetch"
		_at_output "present=false"
		exit 1
	fi
	_at_log "fetch outcome=ok"
	_at_output "present=true"
}

# --- prepare ------------------------------------------------------------------

at_prepare()
{
	local summary=""
	mkdir -p "${AT_WORK_DIR}"
	if ! summary="$(_at_py prepare --inbox-dir "${AT_INBOX_DIR}" --repo-root "${AT_REPO_ROOT}" --self-repo "${AT_SELF_REPO}")"; then
		_at_log "prepare outcome=failed"
		exit 1
	fi
	_at_log "prepare $(jq -r '"records=\(.records) invalid=\(.invalid) groups=\(.groups) tracked=\(.tracked) capped=\(.capped) model_groups=\(.model_groups) list_calls=\(.list_calls) evidence_gets=\(.evidence_gets)"' <<< "${summary}")"
	if [ "$(jq -r '.lists_ok' <<< "${summary}")" = "false" ]; then
		echo "::warning::Alert triage could not list tracking issues/PRs; all groups deferred."
	fi
	if [ "$(jq -r '.groups + .invalid' <<< "${summary}")" = "0" ]; then
		_at_log "skip reason=no_records"
	fi
}

# --- diagnose -------------------------------------------------------------------

_at_run_engine()
{
	local engine_root="$1" group_dir="$2" prompt="$3" reply="$4" call_timeout="$5" model="$6"
	(
		# The engine never gets a GitHub token, the OIDC request variables or
		# the Telegram secret.
		unset GH_TOKEN GITHUB_TOKEN GH_PAT TG_BOT_SECRET ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL
		export AI_ENGINE_READ_ONLY=true
		export AI_ENGINE_INCLUDE_PATHS="${group_dir}"
		# shellcheck disable=SC2016  # $0 / $@ expand in the child bash, not here.
		timeout -k 30 "${call_timeout}" \
			bash -c 'source "$0" || exit 2; claude_run_selected "$@"' "${engine_root}/scripts/ai_engine.sh" \
			ALERT_TRIAGE "${prompt}" "${reply}" "${AT_REPO_ROOT}" --codex-stdio -- \
			bash "${engine_root}/scripts/codex_isolated_exec.sh" run --mode read-only \
				--workdir "${AT_REPO_ROOT}" --include "${group_dir}" -- \
				--ask-for-approval never \
				exec \
				--skip-git-repo-check \
				--model "${model}" \
				--sandbox read-only
	)
}

at_diagnose()
{
	local groups_file="${AT_WORK_DIR}/groups.json" engine_root="${CLAUDE_ENGINE_SUPPORT_DIR:-}"
	local deadline="${ALERT_TRIAGE_RUN_DEADLINE_SECS:-2400}" engine_timeout="${ALERT_TRIAGE_ENGINE_TIMEOUT_SECS:-600}"
	local model="${ALERT_TRIAGE_MODEL:-openai/gpt-6-sol}" fp="" group_dir="" outcome="" rc=0 budget=0 call_timeout=0 engine_ok="true"
	[[ "${deadline}" =~ ^[0-9]{1,5}$ ]] && [ "${deadline}" -ge 60 ] || deadline=2400
	deadline=$((10#${deadline}))
	[[ "${engine_timeout}" =~ ^[0-9]{1,5}$ ]] && [ "${engine_timeout}" -ge 30 ] || engine_timeout=600
	engine_timeout=$((10#${engine_timeout}))
	[[ "${model}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] || model="openai/gpt-6-sol"
	[ -f "${groups_file}" ] || return 0
	if [ -z "${engine_root}" ] || [ ! -f "${engine_root}/scripts/ai_engine.sh" ] || [ -L "${engine_root}/scripts/ai_engine.sh" ]; then
		engine_ok="false"
	fi
	while IFS= read -r fp; do
		[[ "${fp}" =~ ^[0-9a-f]{64}$ ]] || continue
		group_dir="${AT_WORK_DIR}/groups/${fp}"
		mkdir -p "${group_dir}"
		rm -f -- "${group_dir}/outcome" "${group_dir}/diagnosis.json" "${group_dir}/reply.txt"
		budget=$((deadline - SECONDS))
		if [ "${budget}" -lt 30 ]; then
			# No outcome file: decide defers the group, the next run retries it.
			_at_log "diagnose fp=${fp:0:12} outcome=deadline"
			continue
		fi
		outcome="engine_failed"
		if [ "${engine_ok}" = "true" ] && _at_py compose --group "${fp}" >/dev/null 2>&1; then
			call_timeout="${engine_timeout}"
			[ "${budget}" -ge "${call_timeout}" ] || call_timeout="${budget}"
			rc=0
			_at_run_engine "${engine_root}" "${group_dir}" "${group_dir}/prompt.md" "${group_dir}/reply.txt" "${call_timeout}" "${model}" >/dev/null 2>&1 || rc=$?
			case "${rc}" in
				0)
					if _at_py parse --group "${fp}" --reply "${group_dir}/reply.txt" >/dev/null 2>&1; then
						outcome="ok"
					else
						outcome="parse_failed"
					fi
					;;
				124|137) outcome="timeout" ;;
				*) outcome="engine_failed" ;;
			esac
		fi
		printf '%s\n' "${outcome}" > "${group_dir}/outcome"
		_at_log "diagnose fp=${fp:0:12} outcome=${outcome}"
	done < <(jq -r '.groups[] | select(.needs_model == true) | .fingerprint' "${groups_file}")
}

# --- route ---------------------------------------------------------------------

_at_send()
{
	local target="$1" id="$2" text="$3" msg_id=""
	if [ "${AT_DRY_RUN}" = "true" ]; then
		_at_log "escalate target=${target} id=${id} outcome=dry_run"
		return 0
	fi
	if [ -f "${AT_SCRIPT_DIR}/tg_helpers.sh" ]; then
		msg_id="$(
			# shellcheck disable=SC1091
			source "${AT_SCRIPT_DIR}/tg_helpers.sh" && tg_send_msg_direct "${text}" "ERROR" 2>/dev/null
		)" || msg_id=""
	fi
	if [ -n "${msg_id}" ]; then
		_at_log "escalate target=${target} id=${id} outcome=sent"
	else
		_at_log "escalate target=${target} id=${id} outcome=failed"
		# An item flag is committed before the send (at most once), so a
		# failed item send is not retried: surface it on the run.
		case "${target}" in issue|pr) echo "::warning::Alert triage escalation for ${id} failed and will not be retried." ;; esac
	fi
}

_at_commit_and_push()
{
	local markers="$1" ledger="$2" pruned="$3" attempts=0 patch="${AT_WORK_DIR}/inbox_commit.patch" message=""
	message="alert-triage: run ${AT_RUN_ID} markers=${markers} ledger=${ledger} pruned=${pruned}"
	git -C "${AT_INBOX_DIR}" add -A
	if git -C "${AT_INBOX_DIR}" diff --cached --quiet; then
		_at_log "commit outcome=noop attempts=0 markers=${markers} pruned=${pruned}"
		return 0
	fi
	_at_inbox_commit()
	{
		git -C "${AT_INBOX_DIR}" -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
			commit -q --no-verify -m "${message}"
	}
	_at_inbox_commit
	git -C "${AT_INBOX_DIR}" diff --binary HEAD~1 HEAD > "${patch}"
	while :; do
		attempts=$((attempts + 1))
		if _at_git push -q origin "HEAD:refs/heads/${AT_BRANCH}" 2>/dev/null; then
			_at_log "commit outcome=pushed attempts=${attempts} markers=${markers} pruned=${pruned}"
			return 0
		fi
		if [ "${attempts}" -ge 3 ]; then
			break
		fi
		# Non-fast-forward: re-apply this run's diff on the new tip. Only this
		# job writes done/ and ledger/; the intake only adds new inbox records.
		# The reset below touches only this job's own temp clone.
		if ! _at_git fetch -q --no-tags --depth=1 origin "+refs/heads/${AT_BRANCH}:refs/remotes/origin/${AT_BRANCH}" 2>/dev/null \
			|| ! git -C "${AT_INBOX_DIR}" reset -q --hard "refs/remotes/origin/${AT_BRANCH}" \
			|| ! git -C "${AT_INBOX_DIR}" apply --index "${patch}" 2>/dev/null \
			|| ! _at_inbox_commit; then
			break
		fi
	done
	_at_log "commit outcome=failed attempts=${attempts} markers=${markers} pruned=${pruned}"
	return 1
}

_at_heal_dispatch()
{
	local fp="$1" send_dir="${AT_WORK_DIR}/groups/$1/send"
	mkdir -p "${send_dir}"
	if ! _at_py build-heal-payload --group "${fp}" --self-repo "${AT_SELF_REPO}" --run-id "${AT_RUN_ID}" > "${send_dir}/payload.json" 2>/dev/null \
		|| ! python3 -B "${AT_HEAL_PY}" validate-payload --payload-json "${send_dir}/payload.json" >/dev/null 2>&1; then
		echo "payload_invalid"
		return 1
	fi
	rm -f "${send_dir}/report_identity.jwt"
	python3 -B "${AT_HEAL_PY}" request-report-identity --out "${send_dir}/report_identity.jwt" >/dev/null 2>&1 || true
	if [ ! -s "${send_dir}/report_identity.jwt" ]; then
		echo "identity_unavailable"
		return 1
	fi
	if ! python3 -B "${AT_HEAL_PY}" wrap-dispatch --payload-json "${send_dir}/payload.json" \
		--report-identity-file "${send_dir}/report_identity.jwt" > "${send_dir}/dispatch.json" 2>/dev/null; then
		rm -f "${send_dir}/report_identity.jwt"
		echo "dispatch_envelope_failed"
		return 1
	fi
	rm -f "${send_dir}/report_identity.jwt"
	if ! GH_TOKEN="${GITHUB_TOKEN:-}" gh api -X POST "repos/${AT_SELF_REPO}/dispatches" --input "${send_dir}/dispatch.json" >/dev/null 2>&1; then
		rm -f "${send_dir}/dispatch.json"
		echo "dispatch_failed"
		return 1
	fi
	rm -f "${send_dir}/dispatch.json"
	return 0
}

_at_self_fix_dispatch()
{
	local fp="$1" send_dir="${AT_WORK_DIR}/groups/$1/send" ref="${ALERT_TRIAGE_DEFAULT_BRANCH:-main}"
	mkdir -p "${send_dir}"
	if [ "${AT_SELF_FIX_ENABLED}" != "true" ]; then
		echo "self_fix_disabled"
		return 1
	fi
	if [ ! -f "${AT_REPO_ROOT}/.github/workflows/alert-self-fix.yml" ]; then
		echo "self_fix_missing"
		return 1
	fi
	if ! _at_py build-self-fix-body --group "${fp}" --ref "${ref}" > "${send_dir}/self_fix.json" 2>/dev/null; then
		echo "payload_invalid"
		return 1
	fi
	if ! GH_TOKEN="${GITHUB_TOKEN:-}" gh api -X POST "repos/${AT_SELF_REPO}/actions/workflows/alert-self-fix.yml/dispatches" --input "${send_dir}/self_fix.json" >/dev/null 2>&1; then
		echo "dispatch_failed"
		return 1
	fi
	return 0
}

at_route()
{
	local decisions="${AT_WORK_DIR}/decisions.json" self_fix_present="false" counts="" markers=0 ledger=0 pruned=0
	local fp="" route="" kind="" source_repo="" failure="" line="" target="" id="" text=""
	[ -f "${AT_WORK_DIR}/groups.json" ] || { _at_log "skip reason=no_records"; return 0; }
	[ -f "${AT_REPO_ROOT}/.github/workflows/alert-self-fix.yml" ] && self_fix_present="true"
	if ! _at_py decide --inbox-dir "${AT_INBOX_DIR}" --self-fix-enabled "${AT_SELF_FIX_ENABLED}" \
		--self-fix-present "${self_fix_present}" --run-url "${AT_RUN_URL}" >/dev/null; then
		_at_log "decide outcome=failed"
		exit 1
	fi
	if ! counts="$(_at_py apply --inbox-dir "${AT_INBOX_DIR}" --dry-run "${AT_DRY_RUN}" --run-id "${AT_RUN_ID}")"; then
		_at_log "apply outcome=failed"
		exit 1
	fi
	markers="$(jq -r '.markers' <<< "${counts}")"
	ledger="$(jq -r '.ledger' <<< "${counts}")"
	pruned="$(jq -r '.pruned' <<< "${counts}")"
	while IFS= read -r line; do
		_at_log "decision ${line} dry_run=${AT_DRY_RUN}"
	done < <(jq -r '.groups[] | "fp=\(.fingerprint[0:12]) classification=\(.classification // "none") route=\(.route) reason=\(.reason) records=\(.records | length)"' "${decisions}")

	if ! _at_commit_and_push "${markers}" "${ledger}" "${pruned}"; then
		# Nothing was dispatched: the next run reprocesses the same records.
		_at_send group commit_failed "Alert triage could not record its decisions on ${AT_BRANCH}; nothing was dispatched.
Run: ${AT_RUN_URL}"
		exit 1
	fi

	# Side effects run only after the state is pushed (at most once).
	while IFS=$'\t' read -r fp route; do
		[[ "${fp}" =~ ^[0-9a-f]{64}$ ]] || continue
		case "${route}" in
			heal_dispatch) kind="heal" ;;
			self_fix_dispatch) kind="self_fix" ;;
			*) continue ;;
		esac
		if [ "${AT_DRY_RUN}" = "true" ]; then
			_at_log "dispatch kind=${kind} fp=${fp:0:12} outcome=skipped_dry_run"
			continue
		fi
		failure=""
		if [ "${kind}" = "heal" ]; then
			failure="$(_at_heal_dispatch "${fp}")" && failure=""
		else
			failure="$(_at_self_fix_dispatch "${fp}")" && failure=""
		fi
		if [ -z "${failure}" ]; then
			_at_log "dispatch kind=${kind} fp=${fp:0:12} outcome=sent"
		else
			[[ "${failure}" =~ ^[a-z_]{1,40}$ ]] || failure="dispatch_failed"
			_at_log "dispatch kind=${kind} fp=${fp:0:12} outcome=failed reason=${failure}"
			source_repo="$(jq -r --arg fp "${fp}" '.groups[] | select(.fingerprint == $fp) | .source_repo' "${decisions}" | tr -cd 'A-Za-z0-9._/-' | head -c 201)"
			_at_send group "${fp:0:12}" "Alert triage could not dispatch the ${kind} route for group ${fp:0:12} from ${source_repo:-unknown} (reason ${failure}).
Run: ${AT_RUN_URL}"
		fi
	done < <(jq -r '.groups[] | [.fingerprint, .route] | @tsv' "${decisions}")

	while IFS=$'\t' read -r target id text; do
		[[ "${target}" =~ ^(group|issue|pr)$ ]] || continue
		id="$(printf '%s' "${id}" | tr -cd 'a-z0-9_:' | head -c 40)"
		_at_send "${target}" "${id}" "$(printf '%b' "${text}")"
	done < <(jq -r '(.escalations + .items)[] | [.target, .id, (.text | gsub("\\\\"; ""))] | @tsv' "${decisions}")
}

case "${1:-}" in
	fetch) at_fetch ;;
	prepare) at_prepare ;;
	diagnose) at_diagnose ;;
	route) at_route ;;
	*)
		echo "usage: alert_triage_route.sh fetch|prepare|diagnose|route" >&2
		exit 2
		;;
esac
