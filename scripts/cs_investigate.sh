#!/usr/bin/env bash
# cs_investigate.sh — investigate one CS ticket and emit a cs_verdict v1
# (issue #9440). The run step of .github/workflows/cs_investigate.yml.
#
# Flow:
#   1. Validate the inputs (invalid inputs: ::error::, exit 2, no verdict).
#   2. Fetch the ticket from the CS hub with an OIDC token (audience cs-hub):
#      GET <hub>/api/tickets/<id>, then photos GET .../files/<n> for n = 0..4,
#      stopping at the first 404 (AD-2).
#   3. Run the CS_INVESTIGATE engine role read-only (claude_run_selected,
#      codex fallback). The agent replies with either {"bridge_requests":[...]}
#      or the final verdict. Requests are sent from this runner by
#      scripts/cs_bridge_query.sh, one per request on stdin, and the results
#      are added to the next round's prompt as untrusted data (AD-1). At most
#      CS_MAX_ROUNDS (3) query rounds; requests after that are refused.
#   4. Validate the verdict against scripts/templates/cs_verdict.v1.schema.json
#      and write `verdict=<compact JSON>` (one line) to $GITHUB_OUTPUT.
#
# After the inputs are valid the script always exits 0: any failure emits
# {"ticket_id":"<id>","outcome":"invalid_verdict"} with a reason in the log.
#
# Environment:
#   TICKET_ID, CS_MODE, CS_ATTEMPT, CS_HUB_URL   workflow inputs
#   CS_HUB_ALLOWED_HOSTS   optional space-separated exact hub hosts (default empty: any)
#   CS_BRIDGE_URL          passed to cs_bridge_query.sh (its default when empty)
#   CS_PHOTO_MAX_BYTES     per-photo cap (default 5242880)
#   CS_INVESTIGATE_MODEL   codex model (default openai/gpt-6-sol)
#   TARGET_DIR             read-only checkout the agent reads (required)
#   CLAUDE_ENGINE_SUPPORT_DIR  staged engine support root (ai_engine.sh)
#   RUNTIME_DIR            scratch directory (default ${RUNNER_TEMP:-/tmp}/cs-investigate)
#
# Log lines: `CS_INVESTIGATE ticket= mode= attempt= round= outcome= reason=`.
# Ticket, photo and bridge data are never logged (PII).

set -uo pipefail

CS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CS_SUPPORT_ROOT="$(cd "${CS_SCRIPT_DIR}/.." && pwd)"
CS_MAX_ROUNDS=3
CS_MAX_PHOTOS=5
CS_TICKET_MAX_BYTES=262144
CS_BRIDGE_RESULT_MAX_BYTES=16384

# shellcheck source=scripts/cs_bridge_query.sh
source "${CS_SCRIPT_DIR}/cs_bridge_query.sh"

cs_py()
{
	python3 -I -B "${CS_SCRIPT_DIR}/cs_verdict.py" "$@"
}

ticket_id="${TICKET_ID:-}"
mode="${CS_MODE:-}"
attempt="${CS_ATTEMPT:-}"
hub_url="${CS_HUB_URL:-}"

# --- 1. inputs ----------------------------------------------------------------

if [[ ! "${ticket_id}" =~ ^[A-Za-z0-9_-]{1,40}$ ]]; then
	echo "::error::cs_investigate: ticket_id is invalid."
	exit 2
fi
case "${mode}" in investigate|verify) ;; *) echo "::error::cs_investigate: mode must be investigate or verify."; exit 2 ;; esac
if [[ ! "${attempt}" =~ ^[1-9]$ ]]; then
	echo "::error::cs_investigate: attempt must be 1-9."
	exit 2
fi
hub_proto="=https"
if [[ "${hub_url}" =~ ^https://([A-Za-z0-9.-]{1,253})(:[0-9]{1,5})?/?$ ]]; then
	hub_host="${BASH_REMATCH[1]}"
elif [ "${GITHUB_ACTIONS:-}" != "true" ] && [[ "${hub_url}" =~ ^http://(127\.0\.0\.1|localhost):[0-9]{1,5}/?$ ]]; then
	# Loopback hubs are for tests only, never inside Actions.
	hub_host="${BASH_REMATCH[1]}"
	hub_proto="=http"
else
	echo "::error::cs_investigate: hub_url must be https://<host>[:port] with no userinfo, path, query or fragment."
	exit 2
fi
if [ -n "${CS_HUB_ALLOWED_HOSTS:-}" ]; then
	hub_allowed="false"
	for allowed_host in ${CS_HUB_ALLOWED_HOSTS}; do
		[ "${allowed_host}" = "${hub_host}" ] && hub_allowed="true"
	done
	if [ "${hub_allowed}" != "true" ]; then
		echo "::error::cs_investigate: hub_url host is not in CS_HUB_ALLOWED_HOSTS."
		exit 2
	fi
fi
hub_base="${hub_url%/}"
photo_max="${CS_PHOTO_MAX_BYTES:-5242880}"
[[ "${photo_max}" =~ ^[0-9]{1,9}$ ]] && [ "${photo_max}" -gt 0 ] || photo_max=5242880

runtime="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}/cs-investigate}"
rm -rf -- "${runtime}"
mkdir -p "${runtime}/secrets" "${runtime}/ticket" "${runtime}/results" || exit 2
chmod 0700 "${runtime}" "${runtime}/secrets"
ticket_dir="${runtime}/ticket"
trap 'rm -rf -- "${runtime}"' EXIT

round=0
notes=()
photos=()

cs_log()
{
	echo "CS_INVESTIGATE ticket=${ticket_id} mode=${mode} attempt=${attempt} round=${round} outcome=$1 reason=$2"
}

# cs_emit <verdict_json> <outcome> <reason> — write the output and exit 0.
cs_emit()
{
	local verdict="$1" outcome="$2" reason="$3"
	if [[ "${verdict}" == *$'\n'* ]] || [ -z "${verdict}" ]; then
		verdict="{\"ticket_id\":\"${ticket_id}\",\"outcome\":\"invalid_verdict\"}"
		outcome="invalid_verdict"
		reason="output_unsafe"
	fi
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf 'verdict=%s\n' "${verdict}" >> "${GITHUB_OUTPUT}"
	fi
	if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		printf '### CS investigation\n\n- outcome: `%s`\n- reason: `%s`\n' "${outcome}" "${reason}" >> "${GITHUB_STEP_SUMMARY}"
	fi
	cs_log "${outcome}" "${reason}"
	exit 0
}

cs_invalid()
{
	local verdict
	verdict="$(cs_py invalid --ticket-id "${ticket_id}")" || verdict=""
	cs_emit "${verdict}" invalid_verdict "$1"
}

# --- 2. ticket and photos ---------------------------------------------------------

if ! cs_oidc_fetch cs-hub "${runtime}/secrets/hub-auth"; then
	cs_invalid hub_fetch_failed
fi
hub_code="$(curl -sS -o "${runtime}/ticket.json" -w '%{http_code}' \
	--proto "${hub_proto}" --max-redirs 0 --max-time 30 --max-filesize "${CS_TICKET_MAX_BYTES}" \
	-H @"${runtime}/secrets/hub-auth" -H 'Accept: application/json' \
	"${hub_base}/api/tickets/${ticket_id}" 2>/dev/null)" || hub_code="000"
if [ "${hub_code}" != "200" ] || ! python3 -I -B -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "${runtime}/ticket.json" 2>/dev/null; then
	rm -f -- "${runtime}/secrets/hub-auth"
	cs_log fetch "hub_fetch_failed_${hub_code}"
	cs_invalid hub_fetch_failed
fi

n=0
while [ "${n}" -lt "${CS_MAX_PHOTOS}" ]; do
	photo_rc=0
	photo_code="$(curl -sS -o "${runtime}/photo.tmp" -w '%{http_code}' \
		--proto "${hub_proto}" --max-redirs 0 --max-time 60 --max-filesize "${photo_max}" \
		-H @"${runtime}/secrets/hub-auth" \
		"${hub_base}/api/tickets/${ticket_id}/files/${n}" 2>/dev/null)" || photo_rc=$?
	if [ "${photo_rc}" -eq 63 ]; then
		notes+=("photo_${n}_too_large")
		break
	fi
	if [ "${photo_rc}" -ne 0 ]; then
		notes+=("photo_${n}_fetch_failed")
		break
	fi
	if [ "${photo_code}" = "404" ]; then
		break
	fi
	if [ "${photo_code}" != "200" ]; then
		notes+=("photo_${n}_http_${photo_code}")
		break
	fi
	photo_size="$(wc -c < "${runtime}/photo.tmp" | tr -d ' ')"
	if [ "${photo_size}" -gt "${photo_max}" ]; then
		notes+=("photo_${n}_too_large")
		break
	fi
	if ! photo_ext="$(cs_py image-ext "${runtime}/photo.tmp")"; then
		notes+=("photo_${n}_not_an_image")
		break
	fi
	mv -f -- "${runtime}/photo.tmp" "${ticket_dir}/photo-${n}.${photo_ext}"
	photos+=("${ticket_dir}/photo-${n}.${photo_ext}")
	n=$((n + 1))
done
rm -f -- "${runtime}/photo.tmp" "${runtime}/secrets/hub-auth"
cs_log fetch "photos_${#photos[@]}"

# --- 3. engine rounds ---------------------------------------------------------------

engine_root="${CLAUDE_ENGINE_SUPPORT_DIR:-}"
if [ -z "${engine_root}" ] || [ ! -f "${engine_root}/scripts/ai_engine.sh" ] || [ -L "${engine_root}/scripts/ai_engine.sh" ]; then
	cs_invalid engine_support_missing
fi
if [ -z "${TARGET_DIR:-}" ] || [ ! -d "${TARGET_DIR}" ]; then
	cs_invalid target_missing
fi
model="${CS_INVESTIGATE_MODEL:-openai/gpt-6-sol}"
[[ "${model}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] || model="openai/gpt-6-sol"

cs_run_engine()
{
	local prompt="$1" out="$2"
	(
		# The agent never gets a GitHub token or the OIDC request variables.
		unset GH_TOKEN GITHUB_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL
		export AI_ENGINE_READ_ONLY=true
		export AI_ENGINE_INCLUDE_PATHS="${ticket_dir}"
		# shellcheck disable=SC1091
		source "${engine_root}/scripts/ai_engine.sh" || exit 2
		claude_run_selected CS_INVESTIGATE "${prompt}" "${out}" "${TARGET_DIR}" --codex-stdio -- \
			bash "${engine_root}/scripts/codex_isolated_exec.sh" run --mode read-only \
				--workdir "${TARGET_DIR}" --include "${ticket_dir}" -- \
				--ask-for-approval never \
				exec \
				--skip-git-repo-check \
				--model "${model}" \
				--sandbox read-only
	)
}

photo_args=()
for photo in ${photos[@]+"${photos[@]}"}; do
	photo_args+=(--photo "${photo}")
done
note_args=()
for note in ${notes[@]+"${notes[@]}"}; do
	note_args+=(--note "${note}")
done

while :; do
	prompt_file="${runtime}/prompt-${round}.md"
	reply_file="${runtime}/reply-${round}.txt"
	if ! cs_py compose \
		--template "${CS_SUPPORT_ROOT}/prompts/cs_investigate.md" \
		--ticket-id "${ticket_id}" --mode "${mode}" --attempt "${attempt}" \
		--round "${round}" --max-rounds "${CS_MAX_ROUNDS}" \
		--ticket-file "${runtime}/ticket.json" --results-dir "${runtime}/results" \
		${photo_args[@]+"${photo_args[@]}"} ${note_args[@]+"${note_args[@]}"} \
		--out "${prompt_file}"; then
		cs_invalid prompt_failed
	fi
	engine_rc=0
	cs_run_engine "${prompt_file}" "${reply_file}" || engine_rc=$?
	case "${engine_rc}" in
		0) ;;
		76) cs_invalid engine_refused ;;
		80) cs_invalid engine_capacity_wait ;;
		124) cs_invalid engine_timeout ;;
		*) cs_invalid engine_failed ;;
	esac
	requests_dir="${runtime}/results/round-${round}"
	kind="$(cs_py classify --reply "${reply_file}" --requests-dir "${requests_dir}")" || kind="invalid reason=classify_failed"
	case "${kind}" in
		requests\ *)
			if [ "${round}" -ge "${CS_MAX_ROUNDS}" ]; then
				cs_invalid rounds_exhausted
			fi
			for request in "${requests_dir}"/req-*.json; do
				[ -f "${request}" ] || continue
				stem="${request%.json}"
				query_rc=0
				bash "${CS_SCRIPT_DIR}/cs_bridge_query.sh" < "${request}" > "${stem}.raw" || query_rc=$?
				head -c "${CS_BRIDGE_RESULT_MAX_BYTES}" "${stem}.raw" > "${stem}.body"
				rm -f -- "${stem}.raw"
				printf '%s\n' "${query_rc}" > "${stem}.rc"
			done
			cs_log bridge "${kind// /_}"
			round=$((round + 1))
			;;
		verdict)
			verdict_rc=0
			verdict="$(cs_py validate \
				--schema "${CS_SCRIPT_DIR}/templates/cs_verdict.v1.schema.json" \
				--ticket-id "${ticket_id}" --mode "${mode}" --reply "${reply_file}")" || verdict_rc=$?
			if [ "${verdict_rc}" -ne 0 ]; then
				[[ "${verdict}" =~ ^[a-z_]{1,40}$ ]] || verdict="schema_invalid"
				cs_invalid "${verdict}"
			fi
			outcome="$(python3 -I -B -c 'import json,sys; print(json.loads(sys.argv[1])["outcome"])' "${verdict}" 2>/dev/null)" || outcome="unknown"
			cs_emit "${verdict}" "${outcome}" valid
			;;
		*)
			reason="${kind#invalid reason=}"
			[[ "${reason}" =~ ^[a-z_]{1,40}$ ]] || reason="unparsable"
			cs_invalid "${reason}"
			;;
	esac
done
