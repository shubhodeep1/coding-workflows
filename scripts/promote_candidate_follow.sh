#!/usr/bin/env bash
# promote_candidate_follow.sh — keep the `promote-candidate` branch at the main
# tip between promote cycles (Phase 2 of
# docs/plans/pin-promote-cycle-to-candidate-plan.md).
#
# The 13 `.github/workflows/internal-*.yml` wrappers call their reusable
# workflows at `@promote-candidate`. While a promote cycle is in flight the
# lightweight or annotated tag `promote-cycle-frozen` points at the candidate
# and this script leaves the branch alone; between cycles it fast-forwards the
# branch to the main tip. Every move is `force=false`, so the owner ruleset can
# forbid force updates and deletion. This script never creates branch
# protection; an unprotected branch gets one CRITICAL alert per UTC day.
#
# Run by .github/workflows/promote-candidate-follow.yml (push to main, hourly
# schedule, workflow_dispatch). Environment (defaults live here, §8):
#   GITHUB_REPOSITORY, GH_TOKEN      required
#   PROMOTE_CYCLE_PIN_ENABLED        default true; `false` ignores the freeze tag
#   PROMOTE_CYCLE_FREEZE_MAX_SECS    default 172800; an older freeze is deleted
#   TG_BOT_SECRET / TG_CHAT_ID       alerts through scripts/tg_helpers.sh
#   GITHUB_EVENT_NAME                the unprotected alert fires only on `schedule`
#
# GitHub API calls per run (§14): 3 reads (branch, tag ref, main ref), 1 more
# read only when the tag exists (annotated tag object), at most 1 write (POST,
# PATCH, or DELETE then PATCH). No other workflow reads this branch or tag, so
# no existing call can supply the data.
#
# Every exit prints exactly one line:
#   PROMOTE_CANDIDATE_FOLLOW outcome=<advanced|current|frozen|created|
#     unfrozen_disabled|stale_tag_deleted|unprotected|error> [action=] [from= to=]
#     [reason=] protected=<true|false|unknown>
# Exit 0 for every outcome except `error` (exit 1). The token is never printed.

set -euo pipefail

FOLLOW_BRANCH="promote-candidate"
FOLLOW_TAG="promote-cycle-frozen"
FOLLOW_DEFAULT_FREEZE_MAX_SECS=172800
FOLLOW_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOLLOW_PROTECTED="unknown"

follow_emit()
{
	# $1 outcome, rest: key=value fields
	local outcome="$1"
	shift
	local line="PROMOTE_CANDIDATE_FOLLOW outcome=${outcome}"
	local field
	for field in "$@"; do
		[ -n "${field}" ] && line="${line} ${field}"
	done
	echo "${line} protected=${FOLLOW_PROTECTED}"
}

follow_fail()
{
	follow_emit error "reason=$1"
	exit 1
}

if [ ! -r "${FOLLOW_SCRIPT_DIR}/gh_helpers.sh" ]; then
	follow_fail helpers_missing
fi
# shellcheck source=gh_helpers.sh
# shellcheck disable=SC1091
source "${FOLLOW_SCRIPT_DIR}/gh_helpers.sh"
if [ -r "${FOLLOW_SCRIPT_DIR}/tg_helpers.sh" ]; then
	# shellcheck source=tg_helpers.sh
	# shellcheck disable=SC1091
	source "${FOLLOW_SCRIPT_DIR}/tg_helpers.sh"
fi

follow_alert()
{
	if type tg_send_msg >/dev/null 2>&1; then
		tg_send_msg "$1" CRITICAL >/dev/null 2>&1 || true
	else
		echo "::warning::promote_candidate_follow: tg_helpers.sh not available; alert not sent" >&2
	fi
}

REPO="${GITHUB_REPOSITORY:-}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	follow_fail invalid_repository
fi
if [ -z "${GH_TOKEN:-}" ]; then
	follow_fail gh_token_missing
fi

PIN_ENABLED=true
_pin_raw="${PROMOTE_CYCLE_PIN_ENABLED:-true}"
if [ "$(printf '%s' "${_pin_raw}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
	PIN_ENABLED=false
fi

FREEZE_MAX_SECS="${PROMOTE_CYCLE_FREEZE_MAX_SECS:-${FOLLOW_DEFAULT_FREEZE_MAX_SECS}}"
if ! [[ "${FREEZE_MAX_SECS}" =~ ^[0-9]{1,12}$ ]] || [ "$(( 10#${FREEZE_MAX_SECS} ))" -eq 0 ]; then
	echo "::warning::PROMOTE_CYCLE_FREEZE_MAX_SECS is not a positive integer; using ${FOLLOW_DEFAULT_FREEZE_MAX_SECS}" >&2
	FREEZE_MAX_SECS="${FOLLOW_DEFAULT_FREEZE_MAX_SECS}"
fi
FREEZE_MAX_SECS="$(( 10#${FREEZE_MAX_SECS} ))"

FOLLOW_TMP="$(mktemp -d "${TMPDIR:-/tmp}/promote_candidate_follow.XXXXXX")" || follow_fail tmpdir_unavailable
trap 'rm -rf "${FOLLOW_TMP}"' EXIT

# follow_api <out-file> <gh api args...>: runs gh_api_retry in this shell so
# GH_API_RETRY_LAST_STATUS stays readable; prints the exit code to FOLLOW_RC.
FOLLOW_RC=0
follow_api()
{
	local out="$1"
	shift
	FOLLOW_RC=0
	gh_api_retry "$@" > "${out}" || FOLLOW_RC=$?
}

follow_is_not_found()
{
	[ "${FOLLOW_RC}" -ne 0 ] && [ "${GH_API_RETRY_LAST_STATUS:-}" = "404" ]
}

# follow_json_get <file> <key>...: print a nested string or boolean field
# (booleans as true/false), or nothing when it is missing or another type.
follow_json_get()
{
	python3 -I -B -c '
import json, sys
try:
	value = json.load(open(sys.argv[1], encoding="utf-8"))
	for key in sys.argv[2:]:
		value = value[key] if isinstance(value, dict) else None
except Exception:
	value = None
if isinstance(value, bool):
	print("true" if value else "false")
elif isinstance(value, str):
	sys.stdout.write(value)
' "$@" 2>/dev/null || true
}

follow_valid_sha()
{
	[[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

# 1. Read the branch, the tag and the main tip.
BRANCH_EXISTS=true
BRANCH_SHA=""
follow_api "${FOLLOW_TMP}/branch.json" "repos/${REPO}/branches/${FOLLOW_BRANCH}"
if [ "${FOLLOW_RC}" -ne 0 ]; then
	if follow_is_not_found; then
		BRANCH_EXISTS=false
	else
		follow_fail branch_read_failed
	fi
else
	BRANCH_SHA="$(follow_json_get "${FOLLOW_TMP}/branch.json" commit sha)"
	follow_valid_sha "${BRANCH_SHA}" || follow_fail branch_read_invalid
	case "$(follow_json_get "${FOLLOW_TMP}/branch.json" protected)" in
		true) FOLLOW_PROTECTED=true ;;
		false) FOLLOW_PROTECTED=false ;;
		*) FOLLOW_PROTECTED=unknown ;;
	esac
fi

TAG_EXISTS=true
TAG_TYPE=""
TAG_OBJECT_SHA=""
follow_api "${FOLLOW_TMP}/tag.json" "repos/${REPO}/git/ref/tags/${FOLLOW_TAG}"
if [ "${FOLLOW_RC}" -ne 0 ]; then
	if follow_is_not_found; then
		TAG_EXISTS=false
	else
		# Fail closed: an unreadable tag could be a frozen cycle.
		follow_fail tag_read_failed
	fi
else
	TAG_TYPE="$(follow_json_get "${FOLLOW_TMP}/tag.json" object type)"
	TAG_OBJECT_SHA="$(follow_json_get "${FOLLOW_TMP}/tag.json" object sha)"
fi

follow_api "${FOLLOW_TMP}/main.json" "repos/${REPO}/git/ref/heads/main"
[ "${FOLLOW_RC}" -eq 0 ] || follow_fail main_read_failed
MAIN_SHA="$(follow_json_get "${FOLLOW_TMP}/main.json" object sha)"
follow_valid_sha "${MAIN_SHA}" || follow_fail main_read_invalid

# 2. Missing branch: create it at the main tip. No fresh protection read.
if [ "${BRANCH_EXISTS}" = "false" ]; then
	follow_api "${FOLLOW_TMP}/create.json" -X POST "repos/${REPO}/git/refs" \
		-f "ref=refs/heads/${FOLLOW_BRANCH}" -f "sha=${MAIN_SHA}"
	[ "${FOLLOW_RC}" -eq 0 ] || follow_fail create_failed
	follow_emit created "to=${MAIN_SHA}"
	exit 0
fi

# 3. Freeze tag.
ACTION=""
if [ "${TAG_EXISTS}" = "true" ]; then
	if [ "${PIN_ENABLED}" = "false" ]; then
		ACTION=unfrozen_disabled
	else
		FROZEN_AT=""
		if [ "${TAG_TYPE}" = "tag" ]; then
			follow_valid_sha "${TAG_OBJECT_SHA}" || follow_fail tag_object_invalid
			follow_api "${FOLLOW_TMP}/tag_object.json" "repos/${REPO}/git/tags/${TAG_OBJECT_SHA}"
			[ "${FOLLOW_RC}" -eq 0 ] || follow_fail tag_object_read_failed
			TAG_MESSAGE="$(follow_json_get "${FOLLOW_TMP}/tag_object.json" message)"
			if [[ "${TAG_MESSAGE}" =~ (^|[^[:alnum:]_])frozen_at=([0-9]{1,12})([^0-9]|$) ]]; then
				FROZEN_AT="$(( 10#${BASH_REMATCH[2]} ))"
			fi
		fi
		NOW="$(date +%s)"
		if [[ "${FROZEN_AT}" =~ ^[0-9]+$ ]] && [[ "${NOW}" =~ ^[0-9]+$ ]] \
			&& [ "${FROZEN_AT}" -le "${NOW}" ] && [ $(( NOW - FROZEN_AT )) -gt "${FREEZE_MAX_SECS}" ]; then
			follow_api "${FOLLOW_TMP}/delete.json" -X DELETE "repos/${REPO}/git/refs/tags/${FOLLOW_TAG}"
			[ "${FOLLOW_RC}" -eq 0 ] || follow_fail stale_tag_delete_failed
			follow_alert "promote-candidate follower (${REPO}): the freeze tag ${FOLLOW_TAG} was older than ${FREEZE_MAX_SECS}s (frozen_at=${FROZEN_AT}) and was deleted; the branch ${FOLLOW_BRANCH} follows main again. Check why the promote cycle did not finish."
			ACTION=stale_tag_deleted
		else
			# Lightweight, unparsable, fresh or future freeze: leave the branch.
			ACTION=frozen
		fi
	fi
fi

# 4. Fast-forward to the main tip (never force, never rewind).
FROM_TO=""
if [ "${ACTION}" != "frozen" ]; then
	if [ "${BRANCH_SHA}" = "${MAIN_SHA}" ]; then
		[ -n "${ACTION}" ] || ACTION=current
	else
		follow_api "${FOLLOW_TMP}/patch.json" -X PATCH "repos/${REPO}/git/refs/heads/${FOLLOW_BRANCH}" \
			-f "sha=${MAIN_SHA}" -F "force=false"
		[ "${FOLLOW_RC}" -eq 0 ] || follow_fail patch_rejected
		[ -n "${ACTION}" ] || ACTION=advanced
		FROM_TO="from=${BRANCH_SHA} to=${MAIN_SHA}"
	fi
fi

# 5. Unprotected branch: one CRITICAL alert per UTC day, no stored state.
if [ "${FOLLOW_PROTECTED}" != "true" ] && [ "${GITHUB_EVENT_NAME:-}" = "schedule" ] \
	&& [ "$(date -u +%H)" = "00" ]; then
	follow_alert "promote-candidate follower (${REPO}): the branch ${FOLLOW_BRANCH} is not protected (protected=${FOLLOW_PROTECTED}). Operator step: add the promote-candidate ruleset (no deletion, no force-push, updates only from the pipeline account)."
	# shellcheck disable=SC2086
	follow_emit unprotected "action=${ACTION}" ${FROM_TO}
	exit 0
fi

# shellcheck disable=SC2086
follow_emit "${ACTION}" ${FROM_TO}
exit 0
