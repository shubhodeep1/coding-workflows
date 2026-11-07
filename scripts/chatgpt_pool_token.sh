#!/usr/bin/env bash
# chatgpt_pool_token.sh — fetch one ChatGPT account (or the pool status) for one Actions job.
#
# Run by .github/actions/chatgpt-pool-token
# (docs/plans/chatgpt-account-reviewer-trial-plan.md, Phase 1):
#   1. read broker_url / oidc_audience from .github/ai/claude_engine.json (the
#      same claude-pool-broker Worker and read audience as the Claude pool),
#      pin the URL like scripts/claude_pool_token.sh and swap the /v1/pool path
#      for /v1/chatgpt (mode token, POST) or /v1/chatgpt/status (mode status, GET);
#   2. request the job's GitHub OIDC token for that audience (the job needs
#      `permissions: id-token: write`); one attempt, no retry in this phase;
#   3. mode token: mask every token in the returned auth.json the moment it is
#      parsed, drop tokens.refresh_token if present (the broker already strips
#      it) and write $CHATGPT_POOL_DIR/<NAME>/auth.json (0600, directories 0700);
#      mode status: re-serialise only whitelisted metadata fields into the
#      `status` output; no directory, no token.
#
# Outputs (GITHUB_OUTPUT): available=true|false, reason, account, codex_home,
# status. Any failure is available=false with a reason and exit 0; the job
# never fails here. Logs carry the CHATGPT_POOL prefix and never a token, the
# JWT or auth.json. The OIDC token never reaches argv: curl reads its headers
# from a 0600 file.
#
# Inputs (environment, every one defaulted here):
#   CHATGPT_POOL_MODE     token | status (default token)
#   CHATGPT_POOL_CONFIG   engine config (default: next to this script)
#   CHATGPT_POOL_DIR      account directory root (default $RUNNER_TEMP/chatgpt-pool)
#   ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN (runner)
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mode="${CHATGPT_POOL_MODE:-token}"
config="${CHATGPT_POOL_CONFIG:-${script_dir}/../.github/ai/claude_engine.json}"
pool_dir="${CHATGPT_POOL_DIR:-${RUNNER_TEMP:-/tmp}/chatgpt-pool}"
output_file="${GITHUB_OUTPUT:-/dev/null}"
account=""
codex_home=""
status_json=""
work=""
# The raw mode value is never echoed, so it cannot inject a workflow command.
case "${mode}" in
	token|status) mode_label="${mode}" ;;
	*) mode_label="invalid" ;;
esac

early_exit()
{
	echo "CHATGPT_POOL available=false reason=$1 mode=${mode_label} account=none"
	printf 'available=false\nreason=%s\naccount=\ncodex_home=\nstatus=\n' "$1" >> "${output_file}"
	exit 0
}

[ "${mode_label}" != "invalid" ] || early_exit mode_invalid
# The account directory is removed on failure only under the runner's temp root.
case "${RUNNER_TEMP:-/tmp}:${pool_dir}" in
	/*:/*) ;;
	*) early_exit pool_dir_invalid ;;
esac
case "${pool_dir}" in
	"${RUNNER_TEMP:-/tmp}"/chatgpt-pool|"${RUNNER_TEMP:-/tmp}"/chatgpt-pool-*) ;;
	*)
		echo "::error::chatgpt_pool_token.sh: CHATGPT_POOL_DIR must be an absolute …/chatgpt-pool directory" >&2
		early_exit pool_dir_invalid
		;;
esac
case "${pool_dir}" in
	*/../*|*/./*|*/..|*/.|"${RUNNER_TEMP:-/tmp}"/chatgpt-pool-*/*) early_exit pool_dir_invalid ;;
esac

engine_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/claude_engine.py" "$@"
}

finish()
{
	local available="$1" reason="$2"
	echo "CHATGPT_POOL available=${available} reason=${reason} mode=${mode_label} account=${account:-none}"
	{
		echo "available=${available}"
		echo "reason=${reason}"
		echo "account=${account}"
		echo "codex_home=${codex_home}"
		echo "status=${status_json}"
	} >> "${output_file}"
	if [ -n "${work}" ] && [ -d "${work}" ] && [ ! -L "${work}" ]; then
		rm -rf -- "${work}"
	fi
	if [ "${mode_label}" = "token" ] && [ "${available}" != "true" ] && [ ! -L "${pool_dir}" ]; then
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
[ -n "${broker_url}" ] || finish false broker_not_configured
broker_origin="${broker_url%/v1/pool}"
if [ "${mode_label}" = "token" ]; then
	endpoint="${broker_origin}/v1/chatgpt"
	method="POST"
else
	endpoint="${broker_origin}/v1/chatgpt/status"
	method="GET"
fi
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
	finish false oidc_unavailable
fi
command -v curl >/dev/null 2>&1 || finish false curl_missing

umask 077
work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/chatgpt-pool-work.XXXXXX" 2>/dev/null)" || { work=""; finish false work_dir_failed; }
chmod 0700 "${work}"

# 2. The OIDC token (ACTIONS_ID_TOKEN_REQUEST_TOKEN authenticates the request).
printf 'Authorization: bearer %s\n' "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" > "${work}/oidc-request-headers"
oidc_code="$(curl -sS -o "${work}/oidc.json" -w '%{http_code}' --max-time 30 \
	-H @"${work}/oidc-request-headers" \
	"${ACTIONS_ID_TOKEN_REQUEST_URL}$([[ "${ACTIONS_ID_TOKEN_REQUEST_URL}" == *\?* ]] && printf '&' || printf '?')audience=${audience}" 2>/dev/null)" || oidc_code="000"
rm -f -- "${work}/oidc-request-headers"
[[ "${oidc_code}" =~ ^[0-9]{3}$ ]] || oidc_code="000"
[ "${oidc_code}" = "200" ] || finish false "oidc_request_failed_${oidc_code}"
if ! python3 - "${work}/oidc.json" "${work}/broker-request-headers" <<'PY'
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
	finish false oidc_invalid
fi
rm -f -- "${work}/oidc.json"

# 3. The broker.
if [ "${method}" = "POST" ]; then
	broker_code="$(curl -sS -o "${work}/broker.json" -w '%{http_code}' --max-time 30 -X POST \
		-H @"${work}/broker-request-headers" -H 'Content-Length: 0' \
		"${endpoint}" 2>/dev/null)" || broker_code="000"
else
	broker_code="$(curl -sS -o "${work}/broker.json" -w '%{http_code}' --max-time 30 \
		-H @"${work}/broker-request-headers" \
		"${endpoint}" 2>/dev/null)" || broker_code="000"
fi
rm -f -- "${work}/broker-request-headers"
[[ "${broker_code}" =~ ^[0-9]{3}$ ]] || broker_code="000"
if [ "${broker_code}" != "200" ]; then
	reason="$(python3 -c 'import json,re,sys; e=json.load(open(sys.argv[1])).get("error",""); print(e if isinstance(e,str) and re.fullmatch(r"[a-z_]{1,40}", e) else "")' "${work}/broker.json" 2>/dev/null || true)"
	rm -f -- "${work}/broker.json"
	case "${broker_code}" in
		403) finish false "broker_refused_${reason:-unknown}" ;;
		503)
			[ "${reason}" != "chatgpt_pool_empty" ] || finish false broker_refused_chatgpt_pool_empty
			finish false "broker_unavailable_${broker_code}"
			;;
		*) finish false "broker_unavailable_${broker_code}" ;;
	esac
fi

if [ "${mode_label}" = "status" ]; then
	# Only whitelisted metadata fields, as compact one-line JSON.
	if ! status_json="$(python3 - "${work}/broker.json" <<'PY'
import json, re, sys
try:
	body = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
	sys.exit(1)
if not isinstance(body, dict) or not isinstance(body.get("accounts"), list):
	sys.exit(1)
def number(value):
	return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
def text(value, pattern):
	return value if isinstance(value, str) and re.fullmatch(pattern, value) else None
def reset(value):
	return number(value) if number(value) is not None else text(value, r"[A-Za-z0-9_:.+-]{1,40}")
accounts = []
for entry in body["accounts"][:64]:
	if not isinstance(entry, dict):
		continue
	name = text(entry.get("account"), r"[A-Z0-9_]{1,64}")
	if name is None:
		continue
	usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
	resets = usage.get("resets") if isinstance(usage.get("resets"), dict) else {}
	generation = entry.get("generation")
	accounts.append({
		"account": name,
		"generation": generation if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0 else 0,
		"last_refresh": text(entry.get("last_refresh"), r"[A-Za-z0-9_:.+-]{0,40}"),
		"last_outcome": text(entry.get("last_outcome"), r"[A-Za-z0-9_:.+-]{0,40}"),
		"usage": {
			"five_hour": number(usage.get("five_hour")),
			"weekly": number(usage.get("weekly")),
			"resets": {"five_hour": reset(resets.get("five_hour")), "weekly": reset(resets.get("weekly"))},
		},
		"usable": entry.get("usable") is True,
		"reason": text(entry.get("reason"), r"[a-z_]{0,40}"),
	})
print(json.dumps({"accounts": accounts, "gate": number(body.get("gate"))}, separators=(",", ":")))
PY
)"; then
		status_json=""
		rm -f -- "${work}/broker.json"
		finish false broker_response_invalid
	fi
	rm -f -- "${work}/broker.json"
	finish true status
fi

# Token mode: mask first, then write the one account's auth.json.
if ! python3 - "${work}/broker.json" "${pool_dir}" "${work}/account" <<'PY'
import json, os, re, sys
try:
	body = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
	sys.exit(1)
if not isinstance(body, dict):
	sys.exit(1)
auth = body.get("auth_json")
tokens = auth.get("tokens") if isinstance(auth, dict) else None
if not isinstance(tokens, dict):
	sys.exit(1)
def mask(value):
	if isinstance(value, str):
		for part in value.split():
			if part:
				print(f"::add-mask::{part}", flush=True)
	elif isinstance(value, dict):
		for item in value.values():
			mask(item)
	elif isinstance(value, list):
		for item in value:
			mask(item)
mask(tokens)
mask(auth.get("OPENAI_API_KEY"))
name = body.get("account")
if not isinstance(name, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", name):
	sys.exit(1)
if "refresh_token" in tokens:
	del tokens["refresh_token"]
	print("CHATGPT_POOL refresh_token_dropped=true", flush=True)
root = sys.argv[2]
home = os.path.join(root, name)
try:
	os.makedirs(root, mode=0o700, exist_ok=True)
	os.chmod(root, 0o700)
	if os.path.islink(home):
		sys.exit(1)
	os.makedirs(home, mode=0o700, exist_ok=True)
	os.chmod(home, 0o700)
	fd = os.open(os.path.join(home, "auth.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as handle:
		json.dump(auth, handle)
except OSError:
	sys.exit(1)
with open(sys.argv[3], "w", encoding="utf-8") as handle:
	handle.write(name)
PY
then
	rm -f -- "${work}/broker.json"
	finish false broker_response_invalid
fi
rm -f -- "${work}/broker.json"
account="$(cat -- "${work}/account")"
[[ "${account}" =~ ^[A-Z0-9_]{1,64}$ ]] || { account=""; finish false broker_response_invalid; }
codex_home="${pool_dir}/${account}"
finish true selected
