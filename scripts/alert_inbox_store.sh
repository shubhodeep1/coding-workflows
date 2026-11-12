#!/usr/bin/env bash
# alert_inbox_store.sh — store one alert_inbox.v1 record on `ai/alert-inbox`.
#
# Body of .github/workflows/alert-inbox-intake.yml (repository_dispatch,
# event type `alert-inbox`), sent by alert_inbox_capture in
# scripts/alert_inbox_capture.sh. Alert triage plan, Phase 1.
#
# Input: the dispatch payload, read only from $GITHUB_EVENT_PATH with jq
#   {"client_payload": {"schema": "alert_inbox.v1", "record": {11 fields}}}
# The record is untrusted: every field is validated (schema, exact key set,
# regexes, byte caps, captured_at window, source_repo allow-list from
# .github/ai/consumer_repos.json in this checkout or this repository, and the
# canonical run_url) before anything is written.
#
# Output: inbox/<YYYY>/<MM>/<DD>/<HH>/<epoch_ms>-<owner-repo>-<run_id>-<id8>.json
# on the branch ai/alert-inbox, written with the Contents API and GITHUB_TOKEN.
#   - HTTP 409: one retry after 1-3 s.
#   - Branch missing: bootstrapped from a parentless root commit (empty tree,
#     or a one-file marker tree when GitHub rejects the empty tree), then one
#     more PUT. The branch only moves forward: no force-push, no ref PATCH,
#     no deletion.
#   - The path already exists (re-run of the same dispatch): outcome=duplicate.
# API calls per record: 1 normally, 2 on a 409, up to 7 on bootstrap; each
# bounded by curl --max-time 20.
#
# Log line (stderr, stable prefix ALERT_INBOX):
#   ALERT_INBOX store outcome=stored|duplicate|rejected|failed reason=<token>
# Exit 0 for stored / duplicate, 1 otherwise.

set -euo pipefail

_ALERT_INBOX_STORE_BRANCH="ai/alert-inbox"
_ALERT_INBOX_EMPTY_TREE="4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_ALERT_INBOX_STORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_ALERT_INBOX_CONSUMERS_FILE="$(dirname "${_ALERT_INBOX_STORE_DIR}")/.github/ai/consumer_repos.json"

_alert_inbox_store_log()
{
	printf 'ALERT_INBOX store outcome=%s reason=%s\n' "$1" "$2" >&2
}

_alert_inbox_reject()
{
	_alert_inbox_store_log rejected "$1"
	exit 1
}

_alert_inbox_fail()
{
	_alert_inbox_store_log failed "$1"
	exit 1
}

STORE_REPO="${GITHUB_REPOSITORY:-}"
STORE_API="${GITHUB_API_URL:-https://api.github.com}"
STORE_TOKEN="${GITHUB_TOKEN:-}"
STORE_EVENT="${GITHUB_EVENT_PATH:-}"

if ! [[ "${STORE_REPO}" =~ ^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$ ]]; then
	_alert_inbox_fail no_repo
fi
case "${STORE_API}" in
	https://*) ;;
	*) _alert_inbox_fail bad_api_url ;;
esac
if [ -z "${STORE_TOKEN}" ]; then
	_alert_inbox_fail no_token
fi
case "${STORE_TOKEN}" in
	*$'\n'*|*$'\r'*) _alert_inbox_fail no_token ;;
esac
if [ -z "${STORE_EVENT}" ] || [ ! -f "${STORE_EVENT}" ]; then
	_alert_inbox_reject no_event
fi

consumers_json="[]"
if [ -f "${_ALERT_INBOX_CONSUMERS_FILE}" ]; then
	consumers_json="$(jq -c 'if type == "array" then [.[] | strings] else [] end' "${_ALERT_INBOX_CONSUMERS_FILE}" 2>/dev/null)" || consumers_json="[]"
	[ -n "${consumers_json}" ] || consumers_json="[]"
fi

# One jq pass returns "ok" or the first failing reason token.
verdict="$(jq -r \
	--argjson consumers "${consumers_json}" \
	--arg self_repo "${STORE_REPO}" \
	'def str_re($v; $re): ($v | type) == "string" and ($v | test($re));
	def capped($v; $n): ($v | type) == "string" and ($v | utf8bytelength) <= $n;
	(.client_payload // null) as $p
	| if ($p | type) != "object" then "payload"
	elif $p.schema != "alert_inbox.v1" then "schema"
	elif ($p.record | type) != "object" then "record"
	elif ($p.record | keys) != ["captured_at","id","job","level","message","release_sha","run_id","run_url","sent_to_telegram","source_repo","workflow"] then "record_keys"
	else $p.record as $r
	| if (str_re($r.id; "\\A[0-9a-f]{32}\\z") | not) then "id"
	elif (str_re($r.captured_at; "\\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z\\z") | not) then "captured_at"
	elif ($r.level | type) != "string" or (($r.level == "WARNING" or $r.level == "ERROR" or $r.level == "CRITICAL") | not) then "level"
	elif (str_re($r.source_repo; "\\A[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}\\z") | not) then "source_repo"
	elif ($r.source_repo != $self_repo and (any($consumers[]; . == $r.source_repo) | not)) then "source_repo_not_allowed"
	elif (capped($r.workflow; 200) | not) then "workflow"
	elif (capped($r.job; 100) | not) then "job"
	elif (capped($r.message; 4000) | not) then "message"
	elif (str_re($r.run_id; "\\A[0-9]{1,20}\\z") | not) then "run_id"
	elif $r.run_url != ("https://github.com/" + $r.source_repo + "/actions/runs/" + $r.run_id) then "run_url"
	elif (str_re($r.release_sha; "\\A([0-9a-f]{40})?\\z") | not) then "release_sha"
	elif ($r.sent_to_telegram | type) != "boolean" then "sent_to_telegram"
	else "ok" end
	end' "${STORE_EVENT}" 2>/dev/null)" || verdict="payload"
if [ "${verdict}" != "ok" ]; then
	[[ "${verdict}" =~ ^[a-z_]{1,40}$ ]] || verdict="payload"
	_alert_inbox_reject "${verdict}"
fi

# Validated scalars only (each matched a strict regex above).
rec_id="$(jq -r '.client_payload.record.id' "${STORE_EVENT}")"
captured_at="$(jq -r '.client_payload.record.captured_at' "${STORE_EVENT}")"
source_repo="$(jq -r '.client_payload.record.source_repo' "${STORE_EVENT}")"
run_id="$(jq -r '.client_payload.record.run_id' "${STORE_EVENT}")"

# captured_at must be a real UTC instant that round-trips, within 24 h ago .. 5 min ahead.
# `date` only ever sees whole seconds: the fractional part is parsed differently
# across date implementations (CI run 43255940833 rejected fresh timestamps on
# ubuntu-26.04), so milliseconds are taken from the regex-checked string itself.
captured_secs="$(date -u -d "${captured_at:0:19}Z" +%s 2>/dev/null)" || _alert_inbox_reject captured_at
roundtrip="$(date -u -d "${captured_at:0:19}Z" +%Y-%m-%dT%H:%M:%S 2>/dev/null)" || _alert_inbox_reject captured_at
if [ "${roundtrip}" != "${captured_at:0:19}" ] || ! [[ "${captured_secs}" =~ ^[0-9]{1,13}$ ]]; then
	_alert_inbox_reject captured_at
fi
epoch_ms=$((10#${captured_secs} * 1000 + 10#${captured_at:20:3}))
now_secs="$(date -u +%s)"
[[ "${now_secs}" =~ ^[0-9]{1,13}$ ]] || _alert_inbox_fail clock
now_ms=$((now_secs * 1000))
if [ "${epoch_ms}" -lt $((now_ms - 86400000)) ]; then
	_alert_inbox_reject captured_at_stale
fi
if [ "${epoch_ms}" -gt $((now_ms + 300000)) ]; then
	_alert_inbox_reject captured_at_future
fi

slug="${source_repo//\//-}"
record_path="inbox/${captured_at:0:4}/${captured_at:5:2}/${captured_at:8:2}/${captured_at:11:2}/${epoch_ms}-${slug}-${run_id}-${rec_id:0:8}.json"

work_dir="$(mktemp -d)"
chmod 700 "${work_dir}"
trap 'rm -rf "${work_dir}"' EXIT
hdr_file="${work_dir}/headers"
resp_file="${work_dir}/response"
( umask 077 && printf 'Authorization: Bearer %s\nAccept: application/vnd.github+json\nContent-Type: application/json\nX-GitHub-Api-Version: 2022-11-28\n' "${STORE_TOKEN}" >"${hdr_file}" )

# _alert_inbox_api METHOD URL [BODY_FILE] — sets HTTP_CODE (000 on transport
# failure); the response body is in ${resp_file}.
HTTP_CODE="000"
_alert_inbox_api()
{
	local method="$1" url="$2" body="${3:-}" code="" rc=0
	local -a args=(-sS --max-time 20 -o "${resp_file}" -w '%{http_code}' -X "${method}" -H "@${hdr_file}")
	if [ -n "${body}" ]; then
		args+=(--data-binary "@${body}")
	fi
	: >"${resp_file}"
	code="$(curl "${args[@]}" "${url}" 2>/dev/null)" || rc=$?
	if [ "${rc}" -ne 0 ] || ! [[ "${code}" =~ ^[0-9]{3}$ ]]; then
		code="000"
	fi
	HTTP_CODE="${code}"
}

repo_api="${STORE_API}/repos/${STORE_REPO}"
BRANCH_CREATED=0

# _alert_inbox_ensure_branch — create ai/alert-inbox from a parentless root
# commit when it is missing. Sets BRANCH_CREATED=1 when this run (or a
# concurrent one) created it. Returns 1 on any unexpected response.
_alert_inbox_ensure_branch()
{
	local commit_sha="" tree_sha="" req="${work_dir}/req.json"
	_alert_inbox_api GET "${repo_api}/git/ref/heads/${_ALERT_INBOX_STORE_BRANCH}"
	if [ "${HTTP_CODE}" = "200" ]; then
		return 0
	fi
	if [ "${HTTP_CODE}" != "404" ]; then
		return 1
	fi
	jq -n --arg tree "${_ALERT_INBOX_EMPTY_TREE}" \
		'{message: "alert inbox root", tree: $tree, parents: []}' >"${req}"
	_alert_inbox_api POST "${repo_api}/git/commits" "${req}"
	if [ "${HTTP_CODE}" = "422" ]; then
		# GitHub rejected the empty tree: use a one-file marker tree instead.
		jq -n '{tree: [{path: "README.md", mode: "100644", type: "blob", content: "Alert inbox records (alert_inbox.v1), written by .github/workflows/alert-inbox-intake.yml.\n"}]}' >"${req}"
		_alert_inbox_api POST "${repo_api}/git/trees" "${req}"
		[ "${HTTP_CODE}" = "201" ] || return 1
		tree_sha="$(jq -r '.sha // empty' "${resp_file}" 2>/dev/null)" || tree_sha=""
		[[ "${tree_sha}" =~ ^[0-9a-f]{40}$ ]] || return 1
		jq -n --arg tree "${tree_sha}" \
			'{message: "alert inbox root", tree: $tree, parents: []}' >"${req}"
		_alert_inbox_api POST "${repo_api}/git/commits" "${req}"
	fi
	[ "${HTTP_CODE}" = "201" ] || return 1
	commit_sha="$(jq -r '.sha // empty' "${resp_file}" 2>/dev/null)" || commit_sha=""
	[[ "${commit_sha}" =~ ^[0-9a-f]{40}$ ]] || return 1
	jq -n --arg ref "refs/heads/${_ALERT_INBOX_STORE_BRANCH}" --arg sha "${commit_sha}" \
		'{ref: $ref, sha: $sha}' >"${req}"
	_alert_inbox_api POST "${repo_api}/git/refs" "${req}"
	if [ "${HTTP_CODE}" = "201" ]; then
		BRANCH_CREATED=1
		return 0
	fi
	if [ "${HTTP_CODE}" = "422" ] && jq -e '(.message // "") | test("already exists"; "i")' "${resp_file}" >/dev/null 2>&1; then
		# A concurrent intake created it first.
		BRANCH_CREATED=1
		return 0
	fi
	return 1
}

put_body="${work_dir}/put.json"
content_b64="$(jq -c '.client_payload.record' "${STORE_EVENT}" | base64 -w0)"
jq -n \
	--arg message "alert-inbox: ${slug} run ${run_id}" \
	--arg content "${content_b64}" \
	--arg branch "${_ALERT_INBOX_STORE_BRANCH}" \
	'{message: $message, content: $content, branch: $branch}' >"${put_body}"

contents_url="${repo_api}/contents/${record_path}"
conflict_retried=0
bootstrapped=0
while true; do
	_alert_inbox_api PUT "${contents_url}" "${put_body}"
	case "${HTTP_CODE}" in
		200|201)
			_alert_inbox_store_log stored ok
			printf '%s\n' "${record_path}"
			exit 0
			;;
		409)
			if [ "${conflict_retried}" -eq 0 ]; then
				conflict_retried=1
				sleep $((1 + RANDOM % 3))
				continue
			fi
			_alert_inbox_fail conflict
			;;
		404|422)
			put_code="${HTTP_CODE}"
			_alert_inbox_api GET "${contents_url}?ref=${_ALERT_INBOX_STORE_BRANCH}"
			if [ "${HTTP_CODE}" = "200" ]; then
				_alert_inbox_store_log duplicate exists
				printf '%s\n' "${record_path}"
				exit 0
			fi
			if [ "${bootstrapped}" -eq 0 ]; then
				bootstrapped=1
				if ! _alert_inbox_ensure_branch; then
					_alert_inbox_fail "branch_bootstrap_http_${HTTP_CODE}"
				fi
				# A 404 PUT with the branch present now means a concurrent intake
				# created it in between (per-run concurrency groups): retry once.
				if [ "${BRANCH_CREATED}" -eq 1 ] || [ "${put_code}" = "404" ]; then
					continue
				fi
			fi
			_alert_inbox_fail "http_${put_code}"
			;;
		*)
			_alert_inbox_fail "http_${HTTP_CODE}"
			;;
	esac
done
