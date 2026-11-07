#!/usr/bin/env bash
# chatgpt_pool_token.sh — fetch the ChatGPT credential for one Actions job.
#
# Run by .github/actions/chatgpt-pool-token (ChatGPT account reviewer trial,
# Phase 1; nothing consumes the credential yet):
#   1. read broker_url / audience from .github/ai/chatgpt_pool.json
#      (reviewer_slot_enabled is not read here; a later phase gates on it);
#   2. request the job's GitHub OIDC token for that audience (the job needs
#      `permissions: id-token: write`);
#   3. POST it to the claude-pool-broker /v1/chatgpt endpoint; mask the
#      returned access and id tokens the moment they are parsed;
#   4. write $CHATGPT_POOL_DIR/auth.json (0600, directory 0700) and
#      $CHATGPT_POOL_DIR/expires_at. A refresh token in the answer is ignored
#      and never written (the broker never sends one).
#
# Outputs (GITHUB_OUTPUT): available=true|false, reason, auth_dir, auth_file,
# expires_at (empty when unavailable). Any failure is available=false with a
# reason and exit 0; the job never fails here. Logs carry the CHATGPT_POOL
# prefix and never a token or the JWT. The OIDC token never reaches argv: curl
# reads its headers from a 0600 file.
#
# Inputs (environment; every one has a default here):
#   CHATGPT_POOL_CONFIG      config (default ../.github/ai/chatgpt_pool.json
#                            next to this script)
#   CHATGPT_POOL_DIR         credential directory (default
#                            $RUNNER_TEMP/chatgpt-pool)
#   CHATGPT_POOL_OIDC_RETRY_ENABLED  bounded retry of the OIDC token request
#                            (default on; unset, empty or `true` in any case
#                            is on, `false` is off, any other value is off
#                            with a warning)
#   CHATGPT_POOL_OIDC_RETRY_SLEEPS   the two sleeps between OIDC attempts, in
#                            seconds (default '2 5'; each 0-60)
#   ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN (runner)
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config="${CHATGPT_POOL_CONFIG:-${script_dir}/../.github/ai/chatgpt_pool.json}"
pool_dir="${CHATGPT_POOL_DIR:-${RUNNER_TEMP:-/tmp}/chatgpt-pool}"
output_file="${GITHUB_OUTPUT:-/dev/null}"
expected_audience="coding-workflows-chatgpt-pool"
production_broker_url="https://claude-pool-broker.shubhodeep.workers.dev/v1/chatgpt"
# Neither raw retry value is ever echoed, so a value cannot inject a workflow
# command or leak into the log.
oidc_retry_raw="${CHATGPT_POOL_OIDC_RETRY_ENABLED:-}"
case "${oidc_retry_raw,,}" in
	""|true) oidc_retry_on=true ;;
	false) oidc_retry_on=false ;;
	*)
		oidc_retry_on=false
		echo "::warning::chatgpt_pool_token.sh: CHATGPT_POOL_OIDC_RETRY_ENABLED is neither true nor false; OIDC retry off" >&2
		;;
esac
read -r -a oidc_retry_sleeps <<< "${CHATGPT_POOL_OIDC_RETRY_SLEEPS:-2 5}" || true
if [ "${#oidc_retry_sleeps[@]}" -ne 2 ] \
	|| ! [[ "${oidc_retry_sleeps[0]}" =~ ^[0-9]{1,2}$ && "${oidc_retry_sleeps[1]}" =~ ^[0-9]{1,2}$ ]] \
	|| [ "$((10#${oidc_retry_sleeps[0]}))" -gt 60 ] || [ "$((10#${oidc_retry_sleeps[1]}))" -gt 60 ]; then
	echo "CHATGPT_POOL oidc_retry_sleeps_invalid fallback=2,5"
	oidc_retry_sleeps=(2 5)
fi

invalid_dir()
{
	echo "CHATGPT_POOL available=false reason=pool_dir_invalid"
	printf 'available=false\nreason=pool_dir_invalid\nauth_dir=\nauth_file=\nexpires_at=\n' >> "${output_file}"
	exit 0
}

# The credential directory is removed on failure only under the runner's temp root.
case "${RUNNER_TEMP:-/tmp}:${pool_dir}" in
	/*:/*) ;;
	*) invalid_dir ;;
esac
case "${pool_dir}" in
	"${RUNNER_TEMP:-/tmp}"/chatgpt-pool|"${RUNNER_TEMP:-/tmp}"/chatgpt-pool-*) ;;
	*)
		echo "::error::chatgpt_pool_token.sh: CHATGPT_POOL_DIR must be an absolute …/chatgpt-pool directory" >&2
		invalid_dir
		;;
esac
case "${pool_dir}" in
	*/../*|*/./*|*/..|*/.|"${RUNNER_TEMP:-/tmp}"/chatgpt-pool-*/*) invalid_dir ;;
esac

auth_file="${pool_dir}/auth.json"
expires_at=""

finish()
{
	local available="$1" reason="$2"
	echo "CHATGPT_POOL available=${available} reason=${reason}"
	if [ "${available}" != "true" ]; then
		expires_at=""
		if [ ! -L "${pool_dir}" ]; then
			rm -rf -- "${pool_dir}"
		fi
	fi
	{
		echo "available=${available}"
		echo "reason=${reason}"
		if [ "${available}" = "true" ]; then
			echo "auth_dir=${pool_dir}"
			echo "auth_file=${auth_file}"
		else
			echo "auth_dir="
			echo "auth_file="
		fi
		echo "expires_at=${expires_at}"
	} >> "${output_file}"
	exit 0
}

config_value()
{
	PYTHONDONTWRITEBYTECODE=1 python3 - "${config}" "$1" <<'PY'
import json, sys
try:
	with open(sys.argv[1], encoding="utf-8") as handle:
		data = json.load(handle)
except (OSError, ValueError):
	sys.exit(1)
if not isinstance(data, dict):
	sys.exit(1)
value = data.get(sys.argv[2], "")
if not isinstance(value, str) or "\n" in value or "\r" in value:
	sys.exit(1)
print(value)
PY
}

[ ! -L "${pool_dir}" ] || finish false pool_dir_invalid
[ -f "${config}" ] || finish false config_missing
broker_url="$(config_value broker_url 2>/dev/null)" || finish false config_invalid
audience="$(config_value audience 2>/dev/null)" || finish false config_invalid
[ -n "${broker_url}" ] || finish false broker_not_configured
[[ "${broker_url}" =~ ^https://claude-pool-broker\.shubhodeep\.workers\.dev/v1/chatgpt$|^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?/v1/chatgpt$ ]] || finish false broker_url_invalid
[ "${GITHUB_ACTIONS:-false}" != true ] || [ "${broker_url}" = "${production_broker_url}" ] || finish false broker_url_invalid
[[ "${audience}" =~ ^[a-z0-9-]{1,64}$ ]] || finish false config_invalid
[ "${GITHUB_ACTIONS:-false}" != true ] || [ "${audience}" = "${expected_audience}" ] || finish false config_invalid
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
	finish false oidc_unavailable
fi
command -v curl >/dev/null 2>&1 || finish false curl_missing

rm -rf -- "${pool_dir}"
umask 077
mkdir -p "${pool_dir}/work" || finish false pool_dir_failed
chmod 0700 "${pool_dir}" "${pool_dir}/work"
work="${pool_dir}/work"

# The OIDC token (ACTIONS_ID_TOKEN_REQUEST_TOKEN authenticates the request).
printf 'Authorization: bearer %s\n' "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" > "${work}/oidc-request-headers"
# Bounded retry: at most 3 attempts, only on 000 (no response), 429 or 5xx;
# 401, 403 and every other code fail at once. Each retry line carries only the
# attempt number and the 3-digit code, never the URL.
oidc_max_attempts=1
[ "${oidc_retry_on}" != "true" ] || oidc_max_attempts=3
oidc_attempt=1
while :; do
	oidc_code="$(curl -sS -o "${work}/oidc.json" -w '%{http_code}' --max-time 30 \
		-H @"${work}/oidc-request-headers" \
		"${ACTIONS_ID_TOKEN_REQUEST_URL}$([[ "${ACTIONS_ID_TOKEN_REQUEST_URL}" == *\?* ]] && printf '&' || printf '?')audience=${audience}" 2>/dev/null)" || oidc_code="000"
	[ "${oidc_code}" != "200" ] || break
	[ "${oidc_attempt}" -lt "${oidc_max_attempts}" ] || break
	[[ "${oidc_code}" =~ ^(000|429|5[0-9][0-9])$ ]] || break
	echo "CHATGPT_POOL oidc_retry attempt=$((oidc_attempt + 1)) code=${oidc_code}"
	sleep "$((10#${oidc_retry_sleeps[oidc_attempt - 1]}))"
	oidc_attempt=$((oidc_attempt + 1))
done
rm -f -- "${work}/oidc-request-headers"
[[ "${oidc_code}" =~ ^[0-9]{3}$ ]] || oidc_code="000"
[ "${oidc_code}" = "200" ] || finish false "oidc_request_failed_${oidc_code}"
if ! PYTHONDONTWRITEBYTECODE=1 python3 - "${work}/oidc.json" "${work}/broker-request-headers" <<'PY'
import json, re, sys
try:
	value = json.load(open(sys.argv[1], encoding="utf-8")).get("value")
except (OSError, ValueError, AttributeError):
	sys.exit(1)
if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,8192}", value):
	sys.exit(1)
print(f"::add-mask::{value}", flush=True)
with open(sys.argv[2], "w", encoding="utf-8") as handle:
	handle.write(f"Authorization: Bearer {value}\n")
PY
then
	rm -f -- "${work}/oidc.json"
	finish false oidc_invalid
fi
rm -f -- "${work}/oidc.json"

# The broker.
broker_code="$(curl -sS -o "${work}/broker.json" -w '%{http_code}' --max-time 30 -X POST \
	-H @"${work}/broker-request-headers" -H 'Content-Length: 0' \
	"${broker_url}" 2>/dev/null)" || broker_code="000"
rm -f -- "${work}/broker-request-headers"
[[ "${broker_code}" =~ ^[0-9]{3}$ ]] || broker_code="000"
if [ "${broker_code}" != "200" ]; then
	reason="$(PYTHONDONTWRITEBYTECODE=1 python3 -c 'import json,re,sys; e=json.load(open(sys.argv[1])).get("error",""); print(e if isinstance(e,str) and re.fullmatch(r"[a-z_]{1,40}", e) else "")' "${work}/broker.json" 2>/dev/null || true)"
	rm -f -- "${work}/broker.json"
	case "${broker_code}" in
		403) finish false "broker_refused_${reason:-unknown}" ;;
		*) finish false "broker_unavailable_${broker_code}" ;;
	esac
fi
# Validate every field, mask both tokens first, then write auth.json (0600).
if ! PYTHONDONTWRITEBYTECODE=1 python3 - "${work}/broker.json" "${pool_dir}" <<'PY'
import datetime, json, os, re, sys
try:
	body = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
	sys.exit(1)
if not isinstance(body, dict):
	sys.exit(1)
access, id_token, account, expires = body.get("access_token"), body.get("id_token"), body.get("account_id"), body.get("expires_at")
token_re = r"[A-Za-z0-9_.-]{20,16384}"
if not isinstance(access, str) or not re.fullmatch(token_re, access):
	sys.exit(1)
if not isinstance(id_token, str) or not re.fullmatch(token_re, id_token):
	sys.exit(1)
print(f"::add-mask::{access}", flush=True)
print(f"::add-mask::{id_token}", flush=True)
if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account):
	sys.exit(1)
if isinstance(expires, bool) or not isinstance(expires, int) or expires <= 0 or expires > 10**11:
	sys.exit(1)
auth = {
	"auth_mode": "chatgpt",
	"OPENAI_API_KEY": None,
	"tokens": {"id_token": id_token, "access_token": access, "account_id": account},
	"last_refresh": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
for name, content in (("auth.json", json.dumps(auth)), ("expires_at", f"{expires}\n")):
	path = os.path.join(sys.argv[2], name)
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as handle:
		handle.write(content)
PY
then
	rm -f -- "${work}/broker.json"
	finish false broker_response_invalid
fi
rm -f -- "${work}/broker.json"
rm -rf -- "${work}"
expires_at="$(tr -d '[:space:]' < "${pool_dir}/expires_at")"
[[ "${expires_at}" =~ ^[0-9]{1,11}$ ]] || finish false broker_response_invalid
finish true granted
