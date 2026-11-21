#!/usr/bin/env bash
# stable_hold.sh — read, set or clear the stable rollback hold (#9342).
#
# After rollback-stable.yml moves the `stable` tag back to an earlier release,
# the hold stops auto-release-stable.yml and the promote-main-to-stable.yml
# cycle from re-releasing the bad code. A deliberate release (the release job
# of test-and-mark-stable.yml or mark-stable.yml) clears it once its tags are
# published.
#
# Storage: file STABLE_HOLD_FILE on branch STABLE_HOLD_BRANCH (an orphan
# branch holding only that file), schema stable_hold.v1:
#   {"schema":"stable_hold.v1","active":bool,"held_at":"<UTC Z>",
#    "from_version":"vX.Y.Z","to_version":"vX.Y.Z","reason":"<=200 chars",
#    "run_url":"https://...","cleared_at":"<UTC Z>"|null,
#    "cleared_by_version":"vX.Y.Z"|null}
# Writes go through the contents / git data API with the caller's token
# (GITHUB_TOKEN in the workflows, so they trigger no workflow). The branch
# only moves forward: no deletion, no force push.
#
# Commands:
#   read                 prints one line: `active <to_version>`, `inactive`
#                        (branch or file missing, or active=false) or
#                        `unknown` (any other error, or a file that does not
#                        match the schema). Always exits 0. Readers treat
#                        `unknown` as held (fail closed).
#   set --from <v> --to <v> --reason <text> --run-url <url>
#                        writes active=true. No write when the hold is already
#                        active for the same to_version. Exits 1 on failure.
#   clear --version <v>  writes active=false, cleared_at, cleared_by_version.
#                        No write when the hold is not active. Exits 1 when the
#                        hold cannot be read or written.
#
# Log lines (stable prefix STABLE_HOLD):
#   STABLE_HOLD outcome=set from=<v> to=<v>
#   STABLE_HOLD outcome=unchanged to=<v>
#   STABLE_HOLD outcome=set_failed reason=<code>
#   STABLE_HOLD outcome=cleared version=<v>
#   STABLE_HOLD outcome=not_active
#   STABLE_HOLD outcome=clear_failed reason=<read_unknown|write_failed>
#
# API calls: read 1 GET. set 1 GET (+1 branch GET when the file is missing,
# +3 POSTs to create the branch the first time) and 1 PUT, repeated at most
# twice on a concurrent-write conflict. clear 1 GET and, only when the hold is
# active, 1 PUT (one retry on conflict).
#
# Environment:
#   GITHUB_REPOSITORY (required), GH_TOKEN (required)
#   STABLE_HOLD_BRANCH   default ai/stable-hold
#   STABLE_HOLD_FILE     default stable-hold.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

STABLE_HOLD_BRANCH="${STABLE_HOLD_BRANCH:-ai/stable-hold}"
STABLE_HOLD_FILE="${STABLE_HOLD_FILE:-stable-hold.json}"
STABLE_HOLD_SCHEMA="stable_hold.v1"
STABLE_HOLD_VERSION_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'

usage()
{
	echo "usage: stable_hold.sh read | set --from <v> --to <v> --reason <text> --run-url <url> | clear --version <v>" >&2
	exit 2
}

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::stable_hold.sh requires ${required_env}." >&2
		exit 1
	fi
done
if ! [[ "${GITHUB_REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	echo "::error::stable_hold.sh: GITHUB_REPOSITORY is not owner/repo." >&2
	exit 1
fi
if ! [[ "${STABLE_HOLD_BRANCH}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${STABLE_HOLD_BRANCH}" == *..* ]]; then
	echo "::error::stable_hold.sh: STABLE_HOLD_BRANCH is not a plain branch name." >&2
	exit 1
fi
if ! [[ "${STABLE_HOLD_FILE}" =~ ^[A-Za-z0-9._-]+$ ]] || [[ "${STABLE_HOLD_FILE}" == *..* ]]; then
	echo "::error::stable_hold.sh: STABLE_HOLD_FILE is not a plain file name." >&2
	exit 1
fi

HOLD_BRANCH_ENC="${STABLE_HOLD_BRANCH//\//%2F}"
HOLD_CONTENTS_PATH="repos/${GITHUB_REPOSITORY}/contents/${STABLE_HOLD_FILE}"

is_http_404_file()
{
	grep -qE '(^|[^0-9])404([^0-9]|$)' "$1" 2>/dev/null
}

is_write_conflict_file()
{
	grep -qE '(^|[^0-9])(409|422)([^0-9]|$)' "$1" 2>/dev/null
}

utc_now()
{
	date -u +%Y-%m-%dT%H:%M:%SZ
}

# hold_fetch: sets HOLD_STATE to active|inactive|missing|unknown, plus
# HOLD_SHA (blob sha, empty when missing), HOLD_JSON and HOLD_TO.
HOLD_STATE=""
HOLD_SHA=""
HOLD_JSON=""
HOLD_TO=""
hold_fetch()
{
	local err_file payload rc content active
	HOLD_STATE=unknown
	HOLD_SHA=""
	HOLD_JSON=""
	HOLD_TO=""
	err_file="$(mktemp)"
	set +e
	payload="$(gh_retry gh api "${HOLD_CONTENTS_PATH}?ref=${HOLD_BRANCH_ENC}" 2>"${err_file}")"
	rc=$?
	set -e
	if [ "${rc}" -ne 0 ]; then
		if is_http_404_file "${err_file}"; then
			HOLD_STATE=missing
		else
			cat "${err_file}" >&2 || true
		fi
		rm -f "${err_file}"
		return 0
	fi
	rm -f "${err_file}"
	HOLD_SHA="$(printf '%s' "${payload}" | jq -r '.sha // empty' 2>/dev/null || true)"
	if ! [[ "${HOLD_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
		HOLD_SHA=""
		echo "::warning::stable hold: ${STABLE_HOLD_FILE} on ${STABLE_HOLD_BRANCH} has no valid blob sha." >&2
		return 0
	fi
	if ! content="$(printf '%s' "${payload}" | jq -er 'select(.encoding == "base64") | .content | gsub("\\s"; "") | @base64d' 2>/dev/null)"; then
		echo "::warning::stable hold: ${STABLE_HOLD_FILE} on ${STABLE_HOLD_BRANCH} could not be decoded." >&2
		return 0
	fi
	if ! printf '%s' "${content}" | jq -e --arg schema "${STABLE_HOLD_SCHEMA}" 'type == "object" and .schema == $schema and (.active | type) == "boolean"' >/dev/null 2>&1; then
		echo "::warning::stable hold: ${STABLE_HOLD_FILE} on ${STABLE_HOLD_BRANCH} does not match schema ${STABLE_HOLD_SCHEMA}." >&2
		return 0
	fi
	active="$(printf '%s' "${content}" | jq -r '.active')"
	HOLD_JSON="${content}"
	if [ "${active}" != "true" ]; then
		HOLD_STATE=inactive
		return 0
	fi
	HOLD_TO="$(printf '%s' "${content}" | jq -r '.to_version // empty | strings')"
	if ! [[ "${HOLD_TO}" =~ ${STABLE_HOLD_VERSION_RE} ]]; then
		HOLD_TO=""
		echo "::warning::stable hold: active hold has no valid to_version." >&2
		return 0
	fi
	HOLD_STATE=active
	return 0
}

# hold_put <json> <message> <prior_sha|"">: 0 written, 1 failed, 3 conflict.
# One attempt (no gh_retry): a conflict re-reads before writing again.
hold_put()
{
	local json="$1" message="$2" prior_sha="$3" err_file encoded
	encoded="$(printf '%s\n' "${json}" | base64 | tr -d '\n')"
	err_file="$(mktemp)"
	if jq -n --arg message "${message}" --arg content "${encoded}" --arg branch "${STABLE_HOLD_BRANCH}" --arg sha "${prior_sha}" \
		'{message: $message, content: $content, branch: $branch} + (if $sha == "" then {} else {sha: $sha} end)' \
		| gh api -X PUT "${HOLD_CONTENTS_PATH}" --input - >/dev/null 2>"${err_file}"; then
		rm -f "${err_file}"
		return 0
	fi
	cat "${err_file}" >&2 || true
	if is_write_conflict_file "${err_file}"; then
		rm -f "${err_file}"
		return 3
	fi
	rm -f "${err_file}"
	return 1
}

# hold_branch_exists: 0 exists, 1 missing, 2 lookup failed.
hold_branch_exists()
{
	local err_file
	err_file="$(mktemp)"
	if gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${STABLE_HOLD_BRANCH}" >/dev/null 2>"${err_file}"; then
		rm -f "${err_file}"
		return 0
	fi
	if is_http_404_file "${err_file}"; then
		rm -f "${err_file}"
		return 1
	fi
	cat "${err_file}" >&2 || true
	rm -f "${err_file}"
	return 2
}

# hold_bootstrap <json> <message>: creates the orphan branch holding only the
# file. 0 created, 1 failed, 3 the branch appeared meanwhile (re-read).
hold_bootstrap()
{
	local json="$1" message="$2" tree_sha commit_sha err_file
	if ! tree_sha="$(jq -n --arg path "${STABLE_HOLD_FILE}" --arg content "${json}"$'\n' \
		'{tree: [{path: $path, mode: "100644", type: "blob", content: $content}]}' \
		| gh api -X POST "repos/${GITHUB_REPOSITORY}/git/trees" --input - | jq -er '.sha | select(test("^[0-9a-f]{40}$"))')"; then
		return 1
	fi
	if ! commit_sha="$(jq -n --arg message "${message}" --arg tree "${tree_sha}" \
		'{message: $message, tree: $tree, parents: []}' \
		| gh api -X POST "repos/${GITHUB_REPOSITORY}/git/commits" --input - | jq -er '.sha | select(test("^[0-9a-f]{40}$"))')"; then
		return 1
	fi
	err_file="$(mktemp)"
	# One attempt: a retried ref create is not idempotent.
	if jq -n --arg ref "refs/heads/${STABLE_HOLD_BRANCH}" --arg sha "${commit_sha}" '{ref: $ref, sha: $sha}' \
		| gh api -X POST "repos/${GITHUB_REPOSITORY}/git/refs" --input - >/dev/null 2>"${err_file}"; then
		rm -f "${err_file}"
		return 0
	fi
	cat "${err_file}" >&2 || true
	if grep -qi 'already exists' "${err_file}"; then
		rm -f "${err_file}"
		return 3
	fi
	rm -f "${err_file}"
	return 1
}

cmd_read()
{
	hold_fetch
	case "${HOLD_STATE}" in
		active) echo "active ${HOLD_TO}" ;;
		inactive|missing) echo "inactive" ;;
		*) echo "unknown" ;;
	esac
	return 0
}

set_failed()
{
	echo "STABLE_HOLD outcome=set_failed reason=$1"
	exit 1
}

cmd_set()
{
	local from="" to="" reason="" run_url="" json message attempt rc branch_rc
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--from) from="${2:-}"; shift 2 ;;
			--to) to="${2:-}"; shift 2 ;;
			--reason) reason="${2:-}"; shift 2 ;;
			--run-url) run_url="${2:-}"; shift 2 ;;
			*) usage ;;
		esac
	done
	[[ "${from}" =~ ${STABLE_HOLD_VERSION_RE} ]] || set_failed bad_from
	[[ "${to}" =~ ${STABLE_HOLD_VERSION_RE} ]] || set_failed bad_to
	reason="$(printf '%s' "${reason}" | tr '[:cntrl:]' ' ' | sed -E 's/^ +//; s/ +$//')"
	if [ -z "${reason}" ] || [ "${#reason}" -gt 200 ]; then
		set_failed bad_reason
	fi
	[[ "${run_url}" =~ ^https://[A-Za-z0-9.:_/-]+$ ]] || set_failed bad_run_url
	json="$(jq -cn --arg schema "${STABLE_HOLD_SCHEMA}" --arg held_at "$(utc_now)" --arg from "${from}" --arg to "${to}" --arg reason "${reason}" --arg run_url "${run_url}" \
		'{schema: $schema, active: true, held_at: $held_at, from_version: $from, to_version: $to, reason: $reason, run_url: $run_url, cleared_at: null, cleared_by_version: null}')"
	message="stable hold: hold releases after rollback ${from} -> ${to}"
	for attempt in 1 2 3; do
		hold_fetch
		case "${HOLD_STATE}" in
			unknown) set_failed read_unknown ;;
			active)
				if [ "${HOLD_TO}" = "${to}" ]; then
					echo "STABLE_HOLD outcome=unchanged to=${to}"
					return 0
				fi
				echo "Replacing the active hold for ${HOLD_TO} with ${to}." >&2
				;;
		esac
		if [ "${HOLD_STATE}" = "missing" ]; then
			set +e
			hold_branch_exists
			branch_rc=$?
			set -e
			if [ "${branch_rc}" -eq 1 ]; then
				set +e
				hold_bootstrap "${json}" "${message}"
				rc=$?
				set -e
				if [ "${rc}" -eq 0 ]; then
					echo "STABLE_HOLD outcome=set from=${from} to=${to}"
					return 0
				fi
				[ "${rc}" -eq 3 ] && continue
				set_failed branch_create_failed
			fi
			[ "${branch_rc}" -eq 0 ] || set_failed branch_lookup_failed
		fi
		set +e
		hold_put "${json}" "${message}" "${HOLD_SHA}"
		rc=$?
		set -e
		if [ "${rc}" -eq 0 ]; then
			echo "STABLE_HOLD outcome=set from=${from} to=${to}"
			return 0
		fi
		[ "${rc}" -eq 3 ] || set_failed write_failed
		echo "::warning::stable hold: concurrent write on ${STABLE_HOLD_BRANCH} (attempt ${attempt}); re-reading." >&2
	done
	set_failed write_conflict
}

clear_failed()
{
	echo "STABLE_HOLD outcome=clear_failed reason=$1"
	exit 1
}

cmd_clear()
{
	local version="" json attempt rc
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--version) version="${2:-}"; shift 2 ;;
			*) usage ;;
		esac
	done
	if ! [[ "${version}" =~ ${STABLE_HOLD_VERSION_RE} ]]; then
		echo "::error::stable_hold.sh clear: --version must be vX.Y.Z." >&2
		exit 2
	fi
	for attempt in 1 2; do
		hold_fetch
		case "${HOLD_STATE}" in
			unknown) clear_failed read_unknown ;;
			inactive|missing)
				echo "STABLE_HOLD outcome=not_active"
				return 0
				;;
		esac
		json="$(printf '%s' "${HOLD_JSON}" | jq -c --arg at "$(utc_now)" --arg v "${version}" '.active = false | .cleared_at = $at | .cleared_by_version = $v')"
		set +e
		hold_put "${json}" "stable hold: cleared by release ${version}" "${HOLD_SHA}"
		rc=$?
		set -e
		if [ "${rc}" -eq 0 ]; then
			echo "STABLE_HOLD outcome=cleared version=${version}"
			return 0
		fi
		[ "${rc}" -eq 3 ] || clear_failed write_failed
		echo "::warning::stable hold: concurrent write on ${STABLE_HOLD_BRANCH} (attempt ${attempt}); re-reading." >&2
	done
	clear_failed write_failed
}

command_name="${1:-}"
[ "$#" -gt 0 ] && shift
case "${command_name}" in
	read) cmd_read ;;
	set) cmd_set "$@" ;;
	clear) cmd_clear "$@" ;;
	*) usage ;;
esac
