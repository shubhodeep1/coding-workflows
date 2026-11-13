#!/usr/bin/env bash
# cs_bridge_query.sh — send one agent DB bridge request (issue #9440).
#
# Run by scripts/cs_investigate.sh on the Actions runner, once per request the
# CS investigation agent asked for. The agent never runs it: its sandbox has
# no network and no OIDC variables (AD-1).
#
# Usage (direct):  bash scripts/cs_bridge_query.sh < request.json
#   Reads one JSON object (at most 16 KB) on stdin, fetches a GitHub Actions
#   OIDC token with audience `agent-db-bridge`, and POSTs the object to
#   CS_BRIDGE_URL (default
#   https://mongo-explorer-5iucg.ondigitalocean.app/claude-db/query).
#   Writes the response body (at most 1 MB) to stdout.
#   Exit codes: 0 HTTP 2xx; 2 refused (URL not the bridge, body not a JSON
#   object or too large); 3 no OIDC token; 4 HTTP error or transport
#   failure (the body, if any, is still written).
#
# Usage (sourced): defines cs_oidc_fetch <audience> <header_file>, which
#   writes `Authorization: Bearer <token>` to <header_file> (0600) for
#   `curl -H @<header_file>`. Returns 3 when ACTIONS_ID_TOKEN_REQUEST_* is
#   missing or the request fails.
#
# Only the exact bridge host is allowed. http://127.0.0.1:<port> and
# http://localhost:<port> (same path) are allowed only outside Actions
# (GITHUB_ACTIONS != true), for tests. The token never reaches argv, a child
# environment, stdout or stderr; it lives only in a 0600 file under a
# temporary directory that is removed on exit. Log lines carry the prefix
# CS_BRIDGE_QUERY and never the URL, body or token.

CS_BRIDGE_DEFAULT_URL="https://mongo-explorer-5iucg.ondigitalocean.app/claude-db/query"
CS_BRIDGE_MAX_BODY_BYTES=16384
CS_BRIDGE_MAX_RESPONSE_BYTES=1048576

# cs_oidc_fetch <audience> <header_file>
# The request token goes to curl through a header file; 000, 429 and 5xx are
# retried up to 3 attempts (the claude_pool_token.sh pattern).
cs_oidc_fetch()
{
	local audience="${1:-}" header_file="${2:-}" work code attempt=1 sep="?"
	if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
		return 3
	fi
	if [[ ! "${audience}" =~ ^[A-Za-z0-9._:-]{1,128}$ ]] || [ -z "${header_file}" ]; then
		return 3
	fi
	work="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/cs-oidc.XXXXXX")" || return 3
	chmod 0700 "${work}"
	(umask 077 && printf 'Authorization: bearer %s\n' "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" > "${work}/request-headers") || { rm -rf -- "${work}"; return 3; }
	[[ "${ACTIONS_ID_TOKEN_REQUEST_URL}" == *\?* ]] && sep="&"
	while :; do
		code="$(curl -sS -o "${work}/oidc.json" -w '%{http_code}' --max-time 30 \
			-H @"${work}/request-headers" \
			"${ACTIONS_ID_TOKEN_REQUEST_URL}${sep}audience=${audience}" 2>/dev/null)" || code="000"
		[ "${code}" != "200" ] || break
		[ "${attempt}" -lt 3 ] || break
		[[ "${code}" =~ ^(000|429|5[0-9][0-9])$ ]] || break
		echo "CS_BRIDGE_QUERY oidc_retry attempt=$((attempt + 1)) code=${code}" >&2
		sleep "${CS_OIDC_RETRY_SLEEP_SECONDS:-2}"
		attempt=$((attempt + 1))
	done
	rm -f -- "${work}/request-headers"
	if [ "${code}" != "200" ]; then
		rm -rf -- "${work}"
		return 3
	fi
	if ! (umask 077 && python3 -I -B - "${work}/oidc.json" "${header_file}" <<'PY'
import json, re, sys
try:
	with open(sys.argv[1], encoding="utf-8") as handle:
		value = json.load(handle).get("value")
except (OSError, ValueError, AttributeError):
	sys.exit(1)
if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,8192}", value):
	sys.exit(1)
with open(sys.argv[2], "w", encoding="utf-8") as handle:
	handle.write(f"Authorization: Bearer {value}\n")
PY
	); then
		rm -rf -- "${work}"
		rm -f -- "${header_file}"
		return 3
	fi
	chmod 0600 "${header_file}"
	rm -rf -- "${work}"
	return 0
}

# cs_bridge_url_allowed <url> — 0 when <url> is the bridge (or, outside
# Actions, a loopback test bridge with the same path).
cs_bridge_url_allowed()
{
	local url="${1:-}"
	if [[ "${url}" =~ ^https://mongo-explorer-5iucg\.ondigitalocean\.app/claude-db/query$ ]]; then
		return 0
	fi
	if [ "${GITHUB_ACTIONS:-}" != "true" ] && [[ "${url}" =~ ^http://(127\.0\.0\.1|localhost):[0-9]{1,5}/claude-db/query$ ]]; then
		return 0
	fi
	return 1
}

cs_bridge_query_main()
{
	set -uo pipefail
	local url="${CS_BRIDGE_URL:-${CS_BRIDGE_DEFAULT_URL}}" work code rc=0 proto="=https" bytes
	if ! cs_bridge_url_allowed "${url}"; then
		echo "CS_BRIDGE_QUERY outcome=refused reason=host_not_allowed" >&2
		return 2
	fi
	[[ "${url}" == http://* ]] && proto="=http"
	work="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/cs-bridge.XXXXXX")" || return 2
	chmod 0700 "${work}"
	# shellcheck disable=SC2064
	trap "rm -rf -- '${work}'" EXIT
	head -c "$((CS_BRIDGE_MAX_BODY_BYTES + 1))" > "${work}/body"
	bytes="$(wc -c < "${work}/body" | tr -d ' ')"
	if [ "${bytes}" -gt "${CS_BRIDGE_MAX_BODY_BYTES}" ]; then
		echo "CS_BRIDGE_QUERY outcome=refused reason=body_too_large" >&2
		return 2
	fi
	if ! python3 -I -B -c 'import json,sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
sys.exit(0 if isinstance(value, dict) else 1)' "${work}/body" 2>/dev/null; then
		echo "CS_BRIDGE_QUERY outcome=refused reason=body_not_object" >&2
		return 2
	fi
	if ! cs_oidc_fetch agent-db-bridge "${work}/auth"; then
		echo "CS_BRIDGE_QUERY outcome=refused reason=oidc_unavailable" >&2
		return 3
	fi
	code="$(curl -sS -o "${work}/response" -w '%{http_code}' \
		--proto "${proto}" --max-redirs 0 --max-time 60 --max-filesize "${CS_BRIDGE_MAX_RESPONSE_BYTES}" \
		-H @"${work}/auth" -H 'Content-Type: application/json' -H 'Accept: application/json' \
		--data-binary @"${work}/body" "${url}" 2>/dev/null)" || rc=$?
	rm -f -- "${work}/auth"
	[ -f "${work}/response" ] && head -c "${CS_BRIDGE_MAX_RESPONSE_BYTES}" "${work}/response"
	if [ "${rc}" -ne 0 ]; then
		echo "CS_BRIDGE_QUERY outcome=error reason=transport curl_rc=${rc} status=${code:-000}" >&2
		return 4
	fi
	if [[ ! "${code}" =~ ^2[0-9][0-9]$ ]]; then
		echo "CS_BRIDGE_QUERY outcome=error reason=http status=${code}" >&2
		return 4
	fi
	echo "CS_BRIDGE_QUERY outcome=ok status=${code}" >&2
	return 0
}

# Run main only when executed, not when sourced (`return` fails outside a
# sourced file or function).
if ! (return 0 2>/dev/null); then
	cs_bridge_query_main
	exit $?
fi
