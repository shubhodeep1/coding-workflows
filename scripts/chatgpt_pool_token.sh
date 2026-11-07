#!/usr/bin/env bash
# chatgpt_pool_token.sh — fetch one ChatGPT account login (token mode) or the
# accounts' status (status mode) for one Actions job.
#
# Run by .github/actions/chatgpt-pool-token
# (docs/plans/chatgpt-account-reviewer-trial-plan.md, Phase 1):
#   1. read broker_url / oidc_audience from .github/ai/claude_engine.json (the
#      same broker and audience as the Claude pool; base = broker_url minus
#      /v1/pool);
#   2. request the job's GitHub OIDC token for that audience (the job needs
#      `permissions: id-token: write`), with the same bounded retry as
#      scripts/claude_pool_token.sh;
#   3. token mode: POST <base>/v1/chatgpt; mask every string under `tokens`,
#      drop tokens.refresh_token again (the broker already strips it) and
#      write $CHATGPT_POOL_DIR/<NAME>/auth.json (0600, directory 0700);
#      status mode: GET <base>/v1/chatgpt/status and re-serialise an
#      allowlist of metadata keys into the one-line `status` output, so no
#      token can reach a step output even if the broker misbehaves.
#
# Outputs (GITHUB_OUTPUT): available=true|false, reason, account, codex_home,
# status. Every path exits 0: a problem is available=false with a reason.
# Logs carry the CHATGPT_POOL prefix and never a token. The OIDC token never
# reaches argv: curl reads its headers from a 0600 file.
#
# Inputs (environment; every one has a default here, unattended §8):
#   CHATGPT_POOL_MODE              token (default) or status
#   CLAUDE_ENGINE_CONFIG           engine config (default: next to this script)
#   CHATGPT_POOL_DIR               pool directory (default $RUNNER_TEMP/chatgpt-pool)
#   CHATGPT_POOL_OIDC_RETRY_SLEEPS the two sleeps between OIDC attempts, in
#                                  seconds (default '2 5'; each 0-60)
#   GITHUB_OUTPUT                  (default /dev/null)
#   GITHUB_ACTIONS                 (default false; true allows only the
#                                  workers.dev broker URL)
#   ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN (runner)
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config="${CLAUDE_ENGINE_CONFIG:-${script_dir}/../.github/ai/claude_engine.json}"
mode="${CHATGPT_POOL_MODE:-token}"
pool_dir="${CHATGPT_POOL_DIR:-${RUNNER_TEMP:-/tmp}/chatgpt-pool}"
output_file="${GITHUB_OUTPUT:-/dev/null}"
in_actions="${GITHUB_ACTIONS:-false}"
read -r -a oidc_retry_sleeps <<< "${CHATGPT_POOL_OIDC_RETRY_SLEEPS:-2 5}" || true
if [ "${#oidc_retry_sleeps[@]}" -ne 2 ] \
	|| ! [[ "${oidc_retry_sleeps[0]}" =~ ^[0-9]{1,2}$ && "${oidc_retry_sleeps[1]}" =~ ^[0-9]{1,2}$ ]] \
	|| [ "$((10#${oidc_retry_sleeps[0]}))" -gt 60 ] || [ "$((10#${oidc_retry_sleeps[1]}))" -gt 60 ]; then
	echo "CHATGPT_POOL oidc_retry_sleeps_invalid fallback=2,5"
	oidc_retry_sleeps=(2 5)
fi

account=""
codex_home=""
status_json="{}"

write_outputs()
{
	{
		echo "available=$1"
		echo "reason=$2"
		echo "account=${account}"
		echo "codex_home=${codex_home}"
		echo "status=${status_json}"
	} >> "${output_file}"
}

# The pool directory is removed on failure only under the runner's temp root.
case "${RUNNER_TEMP:-/tmp}:${pool_dir}" in
	/*:/*) ;;
	*) write_outputs false pool_dir_invalid; exit 0 ;;
esac
case "${pool_dir}" in
	"${RUNNER_TEMP:-/tmp}"/chatgpt-pool|"${RUNNER_TEMP:-/tmp}"/chatgpt-pool-*) ;;
	*)
		echo "::error::chatgpt_pool_token.sh: CHATGPT_POOL_DIR must be an absolute …/chatgpt-pool directory" >&2
		write_outputs false pool_dir_invalid
		exit 0
		;;
esac
case "${pool_dir}" in
	*/../*|*/./*|*/..|*/.|"${RUNNER_TEMP:-/tmp}"/chatgpt-pool-*/*)
		write_outputs false pool_dir_invalid
		exit 0 ;;
esac

engine_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/claude_engine.py" "$@"
}

finish()
{
	local available="$1" reason="$2"
	if [ "${available}" != "true" ]; then
		account=""
		codex_home=""
	fi
	echo "CHATGPT_POOL available=${available} reason=${reason} account=${account}"
	write_outputs "${available}" "${reason}"
	if { [ "${available}" != "true" ] || [ "${mode}" != "token" ]; } && [ ! -L "${pool_dir}" ]; then
		rm -rf -- "${pool_dir}"
	fi
	exit 0
}

[ ! -L "${pool_dir}" ] || finish false pool_dir_invalid
case "${mode}" in
	token|status) ;;
	*) mode=invalid; finish false mode_invalid ;;
esac
[ -f "${config}" ] || finish false config_missing
broker_url="$(engine_py config --config "${config}" --key broker_url 2>/dev/null)" || finish false config_invalid
[ -z "${broker_url}" ] || [[ "${broker_url}" =~ ^https://claude-pool-broker\.shubhodeep\.workers\.dev/v1/pool$|^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?/v1/pool$ ]] || finish false broker_url_invalid
[ "${in_actions}" != true ] || [ -z "${broker_url}" ] || [[ "${broker_url}" == "https://claude-pool-broker.shubhodeep.workers.dev/v1/pool" ]] || finish false broker_url_invalid
audience="$(engine_py config --config "${config}" --key oidc_audience 2>/dev/null)" || finish false config_invalid
[ -n "${broker_url}" ] || finish false broker_not_configured
broker_base="${broker_url%/v1/pool}"
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
	finish false oidc_unavailable
fi
command -v curl >/dev/null 2>&1 || finish false curl_missing

rm -rf -- "${pool_dir}"
mkdir -p "${pool_dir}/work" || finish false pool_dir_failed
chmod 0700 "${pool_dir}" "${pool_dir}/work"
work="${pool_dir}/work"
umask 077

# The OIDC token. At most 3 attempts, only on 000 (no response), 429 or 5xx;
# every other code fails at once. Retry lines carry only the attempt number
# and the 3-digit code, never the URL.
printf 'Authorization: bearer %s\n' "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" > "${work}/oidc-request-headers"
oidc_attempt=1
while :; do
	oidc_code="$(curl -sS -o "${work}/oidc.json" -w '%{http_code}' --max-time 30 \
		-H @"${work}/oidc-request-headers" \
		"${ACTIONS_ID_TOKEN_REQUEST_URL}$([[ "${ACTIONS_ID_TOKEN_REQUEST_URL}" == *\?* ]] && printf '&' || printf '?')audience=${audience}" 2>/dev/null)" || oidc_code="000"
	[[ "${oidc_code}" =~ ^[0-9]{3}$ ]] || oidc_code="000"
	[ "${oidc_code}" != "200" ] || break
	[ "${oidc_attempt}" -lt 3 ] || break
	[[ "${oidc_code}" =~ ^(000|429|5[0-9][0-9])$ ]] || break
	echo "CHATGPT_POOL oidc_retry attempt=$((oidc_attempt + 1)) code=${oidc_code}"
	sleep "$((10#${oidc_retry_sleeps[oidc_attempt - 1]}))"
	oidc_attempt=$((oidc_attempt + 1))
done
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

# The broker.
if [ "${mode}" = "token" ]; then
	broker_code="$(curl -sS -o "${work}/broker.json" -w '%{http_code}' --max-time 30 -X POST \
		-H @"${work}/broker-request-headers" -H 'Content-Length: 0' \
		"${broker_base}/v1/chatgpt" 2>/dev/null)" || broker_code="000"
else
	broker_code="$(curl -sS -o "${work}/broker.json" -w '%{http_code}' --max-time 30 \
		-H @"${work}/broker-request-headers" \
		"${broker_base}/v1/chatgpt/status" 2>/dev/null)" || broker_code="000"
fi
rm -f -- "${work}/broker-request-headers"
[[ "${broker_code}" =~ ^[0-9]{3}$ ]] || broker_code="000"
if [ "${broker_code}" != "200" ]; then
	reason="$(python3 -c 'import json,re,sys; e=json.load(open(sys.argv[1])).get("error",""); print(e if isinstance(e, str) and re.fullmatch(r"[a-z_]{1,40}", e) else "")' "${work}/broker.json" 2>/dev/null || true)"
	rm -f -- "${work}/broker.json"
	case "${broker_code}" in
		403) finish false "broker_refused_${reason:-unknown}" ;;
		503)
			# No usable account is a refusal, not an outage (the issue's done condition).
			[ "${reason}" != "chatgpt_pool_empty" ] || finish false broker_refused_chatgpt_pool_empty
			finish false broker_unavailable_503
			;;
		*) finish false "broker_unavailable_${broker_code}" ;;
	esac
fi

if [ "${mode}" = "status" ]; then
	# Re-serialise only allowlisted metadata keys (plan D7).
	if ! python3 - "${work}/broker.json" "${work}/status.json" "${work}/usable" <<'PY'
import json, re, sys
try:
	body = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
	sys.exit(1)
if not isinstance(body, dict) or not isinstance(body.get("accounts"), list):
	sys.exit(1)
TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{1,20}Z")
OUTCOMES = {"ok", "timeout", "auth_failed", "quota_exhausted", "run_failed"}
def number(value):
	return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
def fraction(value):
	value = number(value)
	return value if value is not None and 0 <= value <= 1 else None
def when(value):
	return value if isinstance(value, str) and TIME_RE.fullmatch(value) else None
gate = number(body.get("gate"))
accounts = []
seen = set()
for entry in body["accounts"]:
	if not isinstance(entry, dict):
		continue
	name = entry.get("account")
	if not isinstance(name, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", name) or name in seen:
		continue
	seen.add(name)
	generation = entry.get("generation")
	usage = entry.get("usage")
	if isinstance(usage, dict):
		resets = usage.get("resets") if isinstance(usage.get("resets"), dict) else {}
		usage = {
			"five_hour": fraction(usage.get("five_hour")),
			"weekly": fraction(usage.get("weekly")),
			"resets": {"five_hour": when(resets.get("five_hour")), "weekly": when(resets.get("weekly"))},
		}
	else:
		usage = None
	reason = entry.get("reason")
	accounts.append({
		"account": name,
		"generation": generation if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0 else 0,
		"last_refresh": when(entry.get("last_refresh")),
		"last_outcome": entry.get("last_outcome") if entry.get("last_outcome") in OUTCOMES else None,
		"forced_refresh": entry.get("forced_refresh") is True,
		"checked_at": when(entry.get("checked_at")),
		"usage": usage,
		"usable": entry.get("usable") is True,
		"reason": reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,40}", reason) else "unknown",
	})
with open(sys.argv[2], "w", encoding="utf-8") as handle:
	handle.write(json.dumps({"gate": gate, "accounts": accounts}, separators=(",", ":")))
with open(sys.argv[3], "w", encoding="utf-8") as handle:
	handle.write(str(sum(1 for item in accounts if item["usable"])))
PY
	then
		rm -f -- "${work}/broker.json"
		finish false broker_response_invalid
	fi
	rm -f -- "${work}/broker.json"
	status_json="$(cat -- "${work}/status.json")"
	usable_count="$(cat -- "${work}/usable")"
	echo "CHATGPT_POOL status accounts=$(printf '%s' "${status_json}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["accounts"]))') usable=${usable_count}"
	if [ "${usable_count}" -gt 0 ] 2>/dev/null; then
		finish true status_ok
	fi
	finish false status_ok
fi

# Token mode: mask first, then write one 0600 auth.json; the name was validated.
if ! python3 - "${work}/broker.json" "${pool_dir}" "${work}/account" <<'PY'
import json, os, re, sys
try:
	body = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
	sys.exit(1)
if not isinstance(body, dict):
	sys.exit(1)
name, auth = body.get("account"), body.get("auth_json")
if not isinstance(name, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", name):
	sys.exit(1)
if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
	sys.exit(1)
tokens = auth["tokens"]
access = tokens.get("access_token")
if not isinstance(access, str) or not access.strip():
	sys.exit(1)
# Defence in depth: a reader never holds the single-use refresh token.
tokens.pop("refresh_token", None)
def mask(value):
	if isinstance(value, str):
		for part in value.split():
			print(f"::add-mask::{part}", flush=True)
	elif isinstance(value, dict):
		for item in value.values():
			mask(item)
	elif isinstance(value, list):
		for item in value:
			mask(item)
mask(tokens)
mask(auth.get("OPENAI_API_KEY"))
home = os.path.join(sys.argv[2], name)
os.mkdir(home, 0o700)
fd = os.open(os.path.join(home, "auth.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
	json.dump(auth, handle)
with open(sys.argv[3], "w", encoding="utf-8") as handle:
	handle.write(name)
PY
then
	rm -f -- "${work}/broker.json"
	finish false broker_response_invalid
fi
rm -f -- "${work}/broker.json"
account="$(cat -- "${work}/account")"
rm -rf -- "${work}"
codex_home="${pool_dir}/${account}"
finish true selected
