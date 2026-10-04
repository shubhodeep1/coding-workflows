#!/usr/bin/env bash
# claude_pool_token.sh — fetch the Claude account pool for one Actions job.
#
# Run by .github/actions/claude-pool-token (plan "Token broker", Phase 4):
#   1. read broker_url / oidc_audience / probe_model / gate_utilization from
#      .github/ai/claude_engine.json;
#   2. request the job's GitHub OIDC token for that audience (the job needs
#      `permissions: id-token: write`);
#   3. POST it to the broker; mask every returned token the moment it is
#      parsed (spike S3: whitespace stripped first);
#   4. run the Haiku probe per account (spike S8) in an empty directory and
#      read the five_hour / seven_day utilisation (S7);
#   5. write the accounts, least used under the gate first (ties to the
#      alphabetically first name), to $CLAUDE_ENGINE_POOL_DIR
#      (default $RUNNER_TEMP/claude-pool): `order`, `tokens/<NAME>` (0600)
#      and `token` (the first account), which scripts/ai_engine.sh reads.
#
# Outputs (GITHUB_OUTPUT): available=true|false, reason, accounts, pool_dir.
# Any failure is available=false with a reason and exit 0, so the job runs
# codex (plan D1). Logs carry the CLAUDE_POOL prefix and never a token. The
# OIDC token and the pool tokens never reach argv: curl reads its headers
# from a 0600 file and the CLI reads CLAUDE_CODE_OAUTH_TOKEN from the
# environment of its own subshell.
#
# Inputs (environment):
#   CLAUDE_ENGINE_CONFIG     engine config (default: next to this script)
#   CLAUDE_ENGINE_POOL_DIR   pool directory (default $RUNNER_TEMP/claude-pool)
#   CLAUDE_POOL_PROBE        `false` skips the probes (order = broker order)
#   CLAUDE_POOL_PROBE_TIMEOUT_SECS   per-probe limit (default 120)
#   ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN (runner)
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config="${CLAUDE_ENGINE_CONFIG:-${script_dir}/../.github/ai/claude_engine.json}"
pool_dir="${CLAUDE_ENGINE_POOL_DIR:-${RUNNER_TEMP:-/tmp}/claude-pool}"
probe_enabled="${CLAUDE_POOL_PROBE:-true}"
probe_timeout="${CLAUDE_POOL_PROBE_TIMEOUT_SECS:-120}"
[[ "${probe_timeout}" =~ ^[0-9]+$ ]] && [ "${probe_timeout}" -gt 0 ] || probe_timeout=120
output_file="${GITHUB_OUTPUT:-/dev/null}"
# The pool directory is removed on failure only under the runner's temp root.
case "${RUNNER_TEMP:-/tmp}:${pool_dir}" in
	/*:/*) ;;
	*) printf 'available=false\nreason=pool_dir_invalid\naccounts=0\npool_dir=%s\n' "${pool_dir}" >> "${output_file}"; exit 0 ;;
esac
case "${pool_dir}" in
	"${RUNNER_TEMP:-/tmp}"/claude-pool|"${RUNNER_TEMP:-/tmp}"/claude-pool-*) ;;
	*)
		echo "::error::claude_pool_token.sh: CLAUDE_ENGINE_POOL_DIR must be an absolute …/claude-pool directory" >&2
		printf 'available=false\nreason=pool_dir_invalid\naccounts=0\npool_dir=%s\n' "${pool_dir}" >> "${output_file}"
		exit 0
		;;
esac
case "${pool_dir}" in
	*/../*|*/./*|*/..|*/.|"${RUNNER_TEMP:-/tmp}"/claude-pool-*/*)
		printf 'available=false\nreason=pool_dir_invalid\naccounts=0\npool_dir=%s\n' "${pool_dir}" >> "${output_file}"
		exit 0 ;;
esac

engine_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/claude_engine.py" "$@"
}

finish()
{
	local available="$1" reason="$2" count="${3:-0}"
	echo "CLAUDE_POOL available=${available} reason=${reason} accounts=${count}"
	{
		echo "available=${available}"
		echo "reason=${reason}"
		echo "accounts=${count}"
		echo "pool_dir=${pool_dir}"
	} >> "${output_file}"
	if [ "${available}" != "true" ] && [ ! -L "${pool_dir}" ]; then
		rm -rf -- "${pool_dir}"
	fi
	exit 0
}

[ ! -L "${pool_dir}" ] || finish false pool_dir_invalid
[ -f "${config}" ] || finish false config_missing
broker_url="$(engine_py config --config "${config}" --key broker_url 2>/dev/null)" || finish false config_invalid
[ -z "${broker_url}" ] || [[ "${broker_url}" =~ ^https://claude-pool-broker\.shubhodeep\.workers\.dev/v1/pool$|^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?/v1/pool$ ]] || finish false broker_url_invalid
[ "${GITHUB_ACTIONS:-false}" != true ] || [ -z "${broker_url}" ] || [[ "${broker_url}" == "https://claude-pool-broker.shubhodeep.workers.dev/v1/pool" ]] || finish false broker_url_invalid
audience="$(engine_py config --config "${config}" --key oidc_audience 2>/dev/null)" || finish false config_invalid
probe_model="$(engine_py config --config "${config}" --key probe_model 2>/dev/null)" || finish false config_invalid
gate="$(engine_py config --config "${config}" --key gate_utilization 2>/dev/null)" || finish false config_invalid
[ -n "${broker_url}" ] || finish false broker_not_configured
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
	finish false oidc_unavailable
fi
command -v curl >/dev/null 2>&1 || finish false curl_missing

rm -rf -- "${pool_dir}"
mkdir -p "${pool_dir}/tokens" "${pool_dir}/work" || finish false pool_dir_failed
chmod 0700 "${pool_dir}" "${pool_dir}/tokens" "${pool_dir}/work"
work="${pool_dir}/work"
umask 077

# 2. The OIDC token (ACTIONS_ID_TOKEN_REQUEST_TOKEN authenticates the request).
printf 'Authorization: bearer %s\n' "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" > "${work}/oidc-request-headers"
oidc_code="$(curl -sS -o "${work}/oidc.json" -w '%{http_code}' --max-time 30 \
	-H @"${work}/oidc-request-headers" \
	"${ACTIONS_ID_TOKEN_REQUEST_URL}$([[ "${ACTIONS_ID_TOKEN_REQUEST_URL}" == *\?* ]] && printf '&' || printf '?')audience=${audience}" 2>/dev/null)" || oidc_code="000"
rm -f -- "${work}/oidc-request-headers"
[ "${oidc_code}" = "200" ] || finish false "oidc_request_failed_${oidc_code}"
if ! python3 - "${work}/oidc.json" "${work}/broker-request-headers" <<'PY'
import json, re, sys
try:
	value = json.load(open(sys.argv[1], encoding="utf-8")).get("value")
except (OSError, ValueError, AttributeError):
	sys.exit(1)
if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,8192}", value):
	sys.exit(1)
print(f"::add-mask::{value}")
with open(sys.argv[2], "w", encoding="utf-8") as handle:
	handle.write(f"Authorization: Bearer {value}\n")
PY
then
	rm -f -- "${work}/oidc.json"
	finish false oidc_invalid
fi
rm -f -- "${work}/oidc.json"

# 3. The broker.
broker_code="$(curl -sS -o "${work}/broker.json" -w '%{http_code}' --max-time 30 -X POST \
	-H @"${work}/broker-request-headers" -H 'Content-Length: 0' \
	"${broker_url}" 2>/dev/null)" || broker_code="000"
rm -f -- "${work}/broker-request-headers"
if [ "${broker_code}" != "200" ]; then
	reason="$(python3 -c 'import json,re,sys; e=json.load(open(sys.argv[1])).get("error",""); print(e if re.fullmatch(r"[a-z_]{1,40}", e) else "")' "${work}/broker.json" 2>/dev/null || true)"
	rm -f -- "${work}/broker.json"
	case "${broker_code}" in
		403) finish false "broker_refused_${reason:-unknown}" ;;
		*) finish false "broker_unavailable_${broker_code}" ;;
	esac
fi
# Mask first, then write one 0600 file per account; names were validated.
if ! python3 - "${work}/broker.json" "${pool_dir}" <<'PY'
import json, os, re, sys
try:
	body = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
	sys.exit(1)
accounts = body.get("accounts") if isinstance(body, dict) else None
if not isinstance(accounts, list):
	sys.exit(1)
kept = []
for entry in accounts:
	if not isinstance(entry, dict):
		continue
	name, token = entry.get("name"), entry.get("token")
	if not isinstance(name, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", name) or name in kept:
		continue
	if not isinstance(token, str):
		continue
	token = re.sub(r"\s+", "", token)
	if not token:
		continue
	print(f"::add-mask::{token}", flush=True)
	path = os.path.join(sys.argv[2], "tokens", name)
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as handle:
		handle.write(token)
	kept.append(name)
with open(os.path.join(sys.argv[2], "broker-order"), "w", encoding="utf-8") as handle:
	handle.write("".join(f"{name}\n" for name in kept))
PY
then
	rm -f -- "${work}/broker.json"
	finish false broker_response_invalid
fi
rm -f -- "${work}/broker.json"
mapfile -t accounts < "${pool_dir}/broker-order"
[ "${#accounts[@]}" -gt 0 ] || finish false no_accounts

write_order()
{
	[ "$#" -gt 0 ] && [ -f "${pool_dir}/tokens/$1" ] || return 1
	printf '%s\n' "$@" > "${pool_dir}/order" || return 1
	cp -- "${pool_dir}/tokens/$1" "${pool_dir}/token" || return 1
	chmod 0600 "${pool_dir}/order" "${pool_dir}/token"
}

if [ "${probe_enabled}" = "false" ]; then
	write_order "${accounts[@]}" || finish false pool_write_failed
	finish true probe_skipped "${#accounts[@]}"
fi
command -v claude >/dev/null 2>&1 || finish false cli_missing

# 4. One Haiku probe per account, in an empty directory with no settings.
mkdir -m 0700 "${work}/probe-cwd"
probes="["
separator=""
for name in "${accounts[@]}"; do
	transcript="${work}/probe-${name}.jsonl"
	probe_rc=0
	(
		cd "${work}/probe-cwd" || exit 1
		unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL GH_TOKEN GITHUB_TOKEN
		CLAUDE_CODE_OAUTH_TOKEN="$(cat -- "${pool_dir}/tokens/${name}")"
		export CLAUDE_CODE_OAUTH_TOKEN
		export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1
		timeout --kill-after=10s "${probe_timeout}s" claude -p --model "${probe_model}" \
			--output-format stream-json --verbose --strict-mcp-config --setting-sources "" \
			"Reply OK" < /dev/null > "${transcript}" 2> /dev/null
	) || probe_rc=$?
	record="$(engine_py probe-parse --account "${name}" --exit-code "${probe_rc}" < "${transcript}")" || record="{\"account\": \"${name}\", \"error\": \"probe_failed\"}"
	rm -f -- "${transcript}"
	echo "CLAUDE_POOL probe $(printf '%s' "${record}" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(" ".join(f"{k}={r.get(k)}" for k in ("account","five_hour","seven_day","status","error")))')"
	probes="${probes}${separator}${record}"
	separator=","
done
probes="${probes}]"

# 5. The least used account under the gate first.
verdict="$(printf '%s' "${probes}" | engine_py choose --gate "${gate}")" || finish false choose_failed
outcome="$(printf '%s' "${verdict}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["outcome"])')"
if [ "${outcome}" != "selected" ]; then
	finish false "${outcome}"
fi
mapfile -t ordered < <(printf '%s' "${verdict}" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["order"]))')
for name in "${accounts[@]}"; do
	case " ${ordered[*]} " in
		*" ${name} "*) ;;
		*) rm -f -- "${pool_dir}/tokens/${name}" ;;
	esac
done
write_order "${ordered[@]}" || finish false pool_write_failed
finish true selected "${#ordered[@]}"
