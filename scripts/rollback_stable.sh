#!/usr/bin/env bash
# rollback_stable.sh — roll `@stable` back to an earlier signed release (#9342).
#
# Runs from .github/workflows/rollback-stable.yml (workflow_dispatch, by an
# operator or by alert triage). It moves the `stable` tag and the major tag
# (v<MAJOR>) back to an earlier `vX.Y.Z` release that already carries its
# signed sync-manifest.json and release-manifest.v1.json assets, holds
# automatic re-releases (scripts/stable_hold.sh) and tells every consumer in
# the registry to sync right away. The `stable` BRANCH is never touched.
#
# Order (the hold is written before any tag moves, so nothing re-releases the
# bad commit in between):
#   0. kill switch            STABLE_ROLLBACK_ENABLED
#   1. validate               dispatch ref, reason, target, current version
#   2. dry run                report the plan, change nothing
#   3. idempotency            per tag: skip a tag that already peels to target
#   4. hold                   stable_hold.sh set (failure: stop, tags untouched)
#   5. stop in-flight runs    cancel queued/running release, promote and
#                             auto-release runs, wait until none is active,
#                             then set the hold again
#   6. move tags              stable, then v<MAJOR>, pushed and verified
#   7. notify consumers       coding-workflows-stable-released dispatch
#   8. report                 log line, step summary, CRITICAL Telegram
#
# Log (stable prefix STABLE_ROLLBACK; every exit prints exactly one line):
#   STABLE_ROLLBACK outcome=skipped reason=disabled
#   STABLE_ROLLBACK outcome=refused reason=<bad_ref|bad_reason|bad_target|
#     fetch_failed|current_version_unresolved|hold_unknown|target_not_lower|
#     major_mismatch|target_not_found|not_on_stable|release_assets_missing|
#     release_lookup_failed|no_eligible_target>
#   STABLE_ROLLBACK outcome=dry_run from=<v> to=<v> target_sha=<sha> planned=<list>
#   STABLE_ROLLBACK outcome=hold_failed from=<v> to=<v>
#   STABLE_ROLLBACK outcome=runs_lookup_failed workflow=<file>
#   STABLE_ROLLBACK outcome=release_still_running active_runs=<n>
#   STABLE_ROLLBACK outcome=tag_push_failed tag=<ref>
#   STABLE_ROLLBACK outcome=rolled_back from=<v> to=<v> consumers_dispatched=<n>/<m>
#   STABLE_ROLLBACK outcome=already_rolled_back from=<v> to=<v> consumers_dispatched=<n>/<m>
# Refusals and failures exit 1; skipped, dry_run and the two success outcomes
# exit 0.
#
# A blank target picks the highest same-major release below the current one
# that passes every check. When a hold is already active for the current
# stable version (a re-dispatch after a rollback), a blank target re-confirms
# that version instead of rolling back further; pass an explicit
# target_version to go further back. An explicit target on the current
# stable commit is accepted only while the hold is active for exactly that
# version (a re-run of that rollback); otherwise it is refused as
# target_not_lower, so no hold is written when nothing was rolled back.
#
# API calls: 1 hold read when the target is blank or is the current stable
# commit, 1-STABLE_ROLLBACK_MAX_CANDIDATES
# release reads, 1-4 hold calls, 4 workflow-run lists per drain poll plus one
# cancel POST per active run, and one dispatch POST per consumer. Git reads and
# tag pushes go to `origin` with the checkout credential (GH_PAT).
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required), GH_TOKEN (required, needs actions:write and
#   repository_dispatch on every consumer), GITHUB_REF (required: the dispatch
#   ref, refs/heads/main or refs/heads/stable)
#   GITHUB_SERVER_URL / GITHUB_RUN_ID   build the run URL stored in the hold
#   GITHUB_STEP_SUMMARY                 receives the report
#   STABLE_ROLLBACK_ENABLED             default true
#   STABLE_ROLLBACK_TARGET_VERSION      default '' (pick automatically)
#   STABLE_ROLLBACK_REASON              required, <=200 characters
#   STABLE_ROLLBACK_DRY_RUN             default false
#   STABLE_ROLLBACK_CANCEL_WAIT_SECS    default 300
#   STABLE_ROLLBACK_CANCEL_POLL_SECS    default 10
#   STABLE_ROLLBACK_MAX_CANDIDATES      default 10
#   STABLE_ROLLBACK_CONSUMER_FILE       default .github/ai/consumer_repos.json
#   STABLE_HOLD_TOKEN                   default '' (falls back to GH_TOKEN); the
#                                       workflow passes github.token so hold
#                                       writes trigger no workflow
#   TG_BOT_SECRET / TG_ADMIN_CHAT_ID    Telegram (optional)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source "${SCRIPT_DIR}/tg_helpers.sh" 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }

STABLE_ROLLBACK_ENABLED="${STABLE_ROLLBACK_ENABLED:-true}"
STABLE_ROLLBACK_TARGET_VERSION="${STABLE_ROLLBACK_TARGET_VERSION:-}"
STABLE_ROLLBACK_REASON="${STABLE_ROLLBACK_REASON:-}"
STABLE_ROLLBACK_DRY_RUN="${STABLE_ROLLBACK_DRY_RUN:-false}"
STABLE_ROLLBACK_CANCEL_WAIT_SECS="${STABLE_ROLLBACK_CANCEL_WAIT_SECS:-300}"
STABLE_ROLLBACK_CANCEL_POLL_SECS="${STABLE_ROLLBACK_CANCEL_POLL_SECS:-10}"
STABLE_ROLLBACK_MAX_CANDIDATES="${STABLE_ROLLBACK_MAX_CANDIDATES:-10}"
STABLE_ROLLBACK_CONSUMER_FILE="${STABLE_ROLLBACK_CONSUMER_FILE:-.github/ai/consumer_repos.json}"
STABLE_HOLD_TOKEN="${STABLE_HOLD_TOKEN:-}"
STABLE_ROLLBACK_HOLD_HELPER="${SCRIPT_DIR}/stable_hold.sh"
# Workflows that can move the stable tag forward (or dispatch a run that does).
STABLE_ROLLBACK_RELEASE_WORKFLOWS=(auto-release-stable.yml test-and-mark-stable.yml mark-stable.yml promote-main-to-stable.yml)
STABLE_ROLLBACK_VERSION_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::rollback_stable.sh requires ${required_env}."
		exit 1
	fi
done
if ! [[ "${GITHUB_REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	echo "::error::rollback_stable.sh: GITHUB_REPOSITORY is not owner/repo."
	exit 1
fi
for numeric_env in STABLE_ROLLBACK_CANCEL_WAIT_SECS STABLE_ROLLBACK_CANCEL_POLL_SECS STABLE_ROLLBACK_MAX_CANDIDATES; do
	if ! [[ "${!numeric_env}" =~ ^[0-9]+$ ]] || [ "${!numeric_env}" -lt 1 ]; then
		echo "::error::${numeric_env} must be a positive integer (got '${!numeric_env}')."
		exit 1
	fi
done

run_id="${GITHUB_RUN_ID:-0}"
[[ "${run_id}" =~ ^[0-9]+$ ]] || run_id=0
server_url="${GITHUB_SERVER_URL:-https://github.com}"
[[ "${server_url}" =~ ^https://[A-Za-z0-9.:-]+$ ]] || server_url="https://github.com"
ROLLBACK_RUN_URL="${server_url}/${GITHUB_REPOSITORY}/actions/runs/${run_id}"

# Untrusted dispatch text: control characters become spaces, backticks become
# quotes (step-summary markdown), surrounding blanks are trimmed. It reaches
# JSON only through jq --arg and Telegram only through --data-urlencode.
ROLLBACK_REASON="$(printf '%s' "${STABLE_ROLLBACK_REASON}" | tr '[:cntrl:]' ' ' | tr '`' "'" | sed -E 's/^ +//; s/ +$//')"
ROLLBACK_FROM=""
ROLLBACK_TO=""
ROLLBACK_TARGET_SHA=""
ROLLBACK_CONSUMER_LINES=()

is_truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

# finish <outcome> <exit_code> [k=v ...]: the single exit path.
finish()
{
	local outcome="$1" rc="$2" line consumer_line
	shift 2
	line="STABLE_ROLLBACK outcome=${outcome}${*:+ $*}"
	echo "${line}"
	if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		{
			echo "### Stable rollback"
			echo ""
			echo "- \`${line}\`"
			echo "- Reason: ${ROLLBACK_REASON:-(none)}"
			echo "- Run: ${ROLLBACK_RUN_URL}"
			for consumer_line in "${ROLLBACK_CONSUMER_LINES[@]+"${ROLLBACK_CONSUMER_LINES[@]}"}"; do
				echo "- ${consumer_line}"
			done
		} >> "${GITHUB_STEP_SUMMARY}" || true
	fi
	tg_send_msg "Stable rollback on ${GITHUB_REPOSITORY}: ${outcome}${*:+ $*}. Reason: ${ROLLBACK_REASON:-(none)}. ${ROLLBACK_RUN_URL}" CRITICAL >/dev/null 2>&1 || true
	exit "${rc}"
}

refuse()
{
	finish refused 1 "reason=$1" "${@:2}"
}

# version_lt <a> <b>: 0 when a sorts strictly below b (sort -V).
version_lt()
{
	[ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]
}

# peeled_remote_sha <tag_ref>: the commit a remote tag peels to, or empty.
peeled_remote_sha()
{
	local ref="$1" listing sha
	listing="$(git ls-remote origin "${ref}" "${ref}^{}" 2>/dev/null || true)"
	sha="$(printf '%s\n' "${listing}" | awk -v r="${ref}^{}" '$2 == r { print $1 }')"
	if [ -z "${sha}" ]; then
		sha="$(printf '%s\n' "${listing}" | awk -v r="${ref}" '$2 == r { print $1 }')"
	fi
	printf '%s' "${sha,,}"
}

# hold <args...>: runs stable_hold.sh with the hold token.
hold()
{
	GH_TOKEN="${STABLE_HOLD_TOKEN:-${GH_TOKEN}}" bash "${STABLE_ROLLBACK_HOLD_HELPER}" "$@"
}

# 0. Kill switch.
if ! is_truthy "${STABLE_ROLLBACK_ENABLED}"; then
	finish skipped 0 reason=disabled
fi

# 1. Validate.
case "${GITHUB_REF:-}" in
	refs/heads/main|refs/heads/stable) ;;
	*) refuse bad_ref ;;
esac
if [ -z "${ROLLBACK_REASON}" ] || [ "${#ROLLBACK_REASON}" -gt 200 ]; then
	refuse bad_reason
fi
target_input="$(printf '%s' "${STABLE_ROLLBACK_TARGET_VERSION}" | tr -d '[:space:]')"
if [ -n "${target_input}" ] && ! [[ "${target_input}" =~ ${STABLE_ROLLBACK_VERSION_RE} ]]; then
	refuse bad_target
fi
if ! git fetch --quiet --force --tags origin >/dev/null 2>&1 \
	|| ! git fetch --quiet --force origin "+refs/heads/stable:refs/remotes/origin/stable" >/dev/null 2>&1; then
	refuse fetch_failed
fi
current_sha="$(peeled_remote_sha refs/tags/stable)"
if ! [[ "${current_sha}" =~ ^[0-9a-f]{40}$ ]]; then
	refuse current_version_unresolved
fi
current_version="$(git tag --list 'v*' --points-at "${current_sha}" | grep -E "${STABLE_ROLLBACK_VERSION_RE}" | sort -V | tail -n 1 || true)"
if ! [[ "${current_version}" =~ ${STABLE_ROLLBACK_VERSION_RE} ]]; then
	refuse current_version_unresolved "stable_sha=${current_sha}"
fi
current_major="${current_version%%.*}"
ROLLBACK_FROM="${current_version}"

# check_target <v>: 0 eligible (TARGET_SHA set); else TARGET_REFUSAL set.
# Returns 2 when the release lookup failed: the caller must stop, not try an
# older release.
TARGET_SHA=""
TARGET_REFUSAL=""
check_target()
{
	local v="$1" sha release_json err_file
	TARGET_SHA=""
	TARGET_REFUSAL=""
	if ! sha="$(git rev-parse --verify --quiet "refs/tags/${v}^{commit}" 2>/dev/null)"; then
		TARGET_REFUSAL=target_not_found
		return 1
	fi
	sha="${sha,,}"
	if [ "${sha}" != "${current_sha}" ] && ! version_lt "${v}" "${current_version}"; then
		TARGET_REFUSAL=target_not_lower
		return 1
	fi
	if [ "${v%%.*}" != "${current_major}" ]; then
		TARGET_REFUSAL=major_mismatch
		return 1
	fi
	if ! git merge-base --is-ancestor "${sha}" refs/remotes/origin/stable 2>/dev/null; then
		TARGET_REFUSAL=not_on_stable
		return 1
	fi
	err_file="$(mktemp)"
	if ! release_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/releases/tags/${v}" 2>"${err_file}")"; then
		if grep -qE '(^|[^0-9])404([^0-9]|$)' "${err_file}"; then
			rm -f "${err_file}"
			TARGET_REFUSAL=release_assets_missing
			return 1
		fi
		cat "${err_file}" >&2 || true
		rm -f "${err_file}"
		TARGET_REFUSAL=release_lookup_failed
		return 2
	fi
	rm -f "${err_file}"
	if ! printf '%s' "${release_json}" | jq -e '
		(.draft != true) and (.prerelease != true)
		and ([.assets[]? | select((.state // "uploaded") == "uploaded") | .name] as $names
			| ($names | index("sync-manifest.json")) != null and ($names | index("release-manifest.v1.json")) != null)' >/dev/null 2>&1; then
		TARGET_REFUSAL=release_assets_missing
		return 1
	fi
	TARGET_SHA="${sha}"
	return 0
}

target_version=""
target_hold_confirmed=false
if [ -z "${target_input}" ]; then
	hold_line="$(hold read || true)"
	case "${hold_line}" in
		inactive) ;;
		"active ${current_version}")
			# Already rolled back to the current version: re-confirm it.
			target_input="${current_version}"
			target_hold_confirmed=true
			;;
		"active "*) ;;
		*) refuse hold_unknown ;;
	esac
fi
if [ -n "${target_input}" ]; then
	set +e
	check_target "${target_input}"
	check_rc=$?
	set -e
	if [ "${check_rc}" -ne 0 ]; then
		refuse "${TARGET_REFUSAL}" "to=${target_input}"
	fi
	# The current stable commit is not lower than itself: only a re-run of a
	# rollback to this version (hold active for it) may proceed. An unreadable
	# hold is refused as hold_unknown, like the blank-target path.
	if [ "${TARGET_SHA}" = "${current_sha}" ] && [ "${target_hold_confirmed}" != "true" ]; then
		explicit_hold_line="$(hold read || true)"
		case "${explicit_hold_line}" in
			"active ${target_input}") ;;
			inactive|"active "*) refuse target_not_lower "to=${target_input}" ;;
			*) refuse hold_unknown "to=${target_input}" ;;
		esac
	fi
	target_version="${target_input}"
else
	lookups=0
	while IFS= read -r candidate; do
		[[ "${candidate}" =~ ${STABLE_ROLLBACK_VERSION_RE} ]] || continue
		[ "${candidate%%.*}" = "${current_major}" ] || continue
		version_lt "${candidate}" "${current_version}" || continue
		if [ "${lookups}" -ge "${STABLE_ROLLBACK_MAX_CANDIDATES}" ]; then
			break
		fi
		set +e
		check_target "${candidate}"
		check_rc=$?
		set -e
		case "${check_rc}" in
			0)
				target_version="${candidate}"
				break
				;;
			2) refuse release_lookup_failed "candidate=${candidate}" ;;
		esac
		case "${TARGET_REFUSAL}" in
			release_assets_missing) lookups=$((lookups + 1)) ;;
		esac
		echo "Skipping ${candidate}: ${TARGET_REFUSAL}."
	done < <(git tag --list 'v*' | grep -E "${STABLE_ROLLBACK_VERSION_RE}" | sort -V -r || true)
	if [ -z "${target_version}" ]; then
		refuse no_eligible_target "from=${current_version}"
	fi
fi
ROLLBACK_TO="${target_version}"
ROLLBACK_TARGET_SHA="${TARGET_SHA}"
target_major="${target_version%%.*}"

# 3. Idempotency, per tag.
stable_done=false
major_done=false
[ "${ROLLBACK_TARGET_SHA}" = "${current_sha}" ] && stable_done=true
[ "$(peeled_remote_sha "refs/tags/${target_major}")" = "${ROLLBACK_TARGET_SHA}" ] && major_done=true

# 2. Dry run: report the plan, change nothing.
if is_truthy "${STABLE_ROLLBACK_DRY_RUN}"; then
	planned="hold"
	if [ "${stable_done}" != "true" ] || [ "${major_done}" != "true" ]; then
		planned+=",cancel_release_runs"
	fi
	[ "${stable_done}" = "true" ] || planned+=",tag:stable"
	[ "${major_done}" = "true" ] || planned+=",tag:${target_major}"
	planned+=",notify_consumers"
	finish dry_run 0 "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}" "target_sha=${ROLLBACK_TARGET_SHA}" "planned=${planned}"
fi

# 4. Hold first: nothing may re-release while the tags move back.
if ! hold set --from "${ROLLBACK_FROM}" --to "${ROLLBACK_TO}" --reason "${ROLLBACK_REASON}" --run-url "${ROLLBACK_RUN_URL}"; then
	finish hold_failed 1 "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}"
fi

# active_release_run_ids <workflow_file>: prints active run ids; 1 on error.
active_release_run_ids()
{
	local runs_json
	runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/$1/runs?per_page=30")" || return 1
	printf '%s' "${runs_json}" | jq -r '.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending") | .id | tostring'
}

# cancel_new_release_runs <workflow_file> <ids>: one cancel POST per run id
# not yet cancelled by this run (tracked in cancelled_release_run_ids). An id
# is recorded only after its cancel succeeds, so a failed cancel is retried on
# the next drain poll.
cancelled_release_run_ids=" "
cancel_new_release_runs()
{
	local active_id
	while IFS= read -r active_id; do
		[[ "${active_id}" =~ ^[0-9]+$ ]] || continue
		[[ "${cancelled_release_run_ids}" == *" ${active_id} "* ]] && continue
		echo "Cancelling $1 run ${active_id}."
		if GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/actions/runs/${active_id}/cancel" >/dev/null; then
			cancelled_release_run_ids+="${active_id} "
		else
			echo "::warning::Could not cancel $1 run ${active_id}; retrying on the next poll."
		fi
	done <<< "$2"
}

# 5. Stop in-flight releases so none moves the tag forward again.
if [ "${stable_done}" != "true" ] || [ "${major_done}" != "true" ]; then
	for release_workflow in "${STABLE_ROLLBACK_RELEASE_WORKFLOWS[@]}"; do
		if ! active_ids="$(active_release_run_ids "${release_workflow}")"; then
			finish runs_lookup_failed 1 "workflow=${release_workflow}"
		fi
		cancel_new_release_runs "${release_workflow}" "${active_ids}"
	done
	drain_deadline=$(( $(date +%s) + STABLE_ROLLBACK_CANCEL_WAIT_SECS ))
	while :; do
		still_active=0
		for release_workflow in "${STABLE_ROLLBACK_RELEASE_WORKFLOWS[@]}"; do
			if ! active_ids="$(active_release_run_ids "${release_workflow}")"; then
				finish runs_lookup_failed 1 "workflow=${release_workflow}"
			fi
			if [ -n "${active_ids}" ]; then
				# A run that became active after the first pass is cancelled too.
				cancel_new_release_runs "${release_workflow}" "${active_ids}"
				active_count="$(printf '%s\n' "${active_ids}" | grep -c '^[0-9]' || true)"
				still_active=$((still_active + ${active_count:-0}))
			fi
		done
		[ "${still_active}" -eq 0 ] && break
		if [ "$(date +%s)" -ge "${drain_deadline}" ]; then
			finish release_still_running 1 "active_runs=${still_active}" "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}"
		fi
		echo "Waiting for ${still_active} cancelled release run(s) to stop."
		sleep "${STABLE_ROLLBACK_CANCEL_POLL_SECS}"
	done
	# A cancelled release job may have cleared the hold before it stopped.
	if ! hold set --from "${ROLLBACK_FROM}" --to "${ROLLBACK_TO}" --reason "${ROLLBACK_REASON}" --run-url "${ROLLBACK_RUN_URL}"; then
		finish hold_failed 1 "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}"
	fi
fi

# Same logic as the release job's publish_tag_with_remote_verification
# (mark-stable.yml / test-and-mark-stable.yml "Tag version and update stable
# pointer"), copied here so the rollback verifies the remote the same way.
publish_tag_with_remote_verification()
{
	local tag_ref="$1"
	local publication_mode="$2"
	local expected_object_id=""
	local remote_lookup_error=""
	local remote_lookup_error_file=""
	local remote_lookup_output=""
	local remote_object_id=""
	local attempt=1
	local max_attempts=5
	local backoff_seconds=2
	local lookup_rc=0
	local push_error_file=""
	local push_rc=0

	case "${publication_mode}" in
		immutable|moving) ;;
		*)
			echo "::error::Unsupported tag publication mode '${publication_mode}' for ${tag_ref}."
			return 1
			;;
	esac

	if ! expected_object_id="$(git rev-parse "${tag_ref}" 2>/dev/null)"; then
		echo "::error::Could not resolve ${tag_ref} to a local Git object."
		return 1
	fi
	if [[ ! "${expected_object_id}" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
		echo "::error::Could not resolve ${tag_ref} to a valid local Git object ID."
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		echo "Publishing ${tag_ref} (attempt ${attempt}/${max_attempts})..."
		push_error_file="$(mktemp "${RUNNER_TEMP:-/tmp}/release_tag_push_err.XXXXXX")"
		set +e
		if [ "${publication_mode}" = "immutable" ]; then
			git push origin "${tag_ref}" 2>"${push_error_file}"
		else
			git push -f origin "${tag_ref}" 2>"${push_error_file}"
		fi
		push_rc=$?
		set -e
		cat "${push_error_file}" >&2 || true
		if [ "${push_rc}" -eq 0 ]; then
			rm -f "${push_error_file}"
			return 0
		fi
		# A missing workflow scope never clears on retry (run 35639756385): stop now.
		if { grep -qi 'refusing to allow' "${push_error_file}" && grep -qi 'workflow' "${push_error_file}"; } \
			|| grep -qiE 'workflows[^[:alnum:]]{0,2}permission' "${push_error_file}"; then
			rm -f "${push_error_file}"
			echo "::error::RELEASE_TAG_PUBLISH outcome=failed error_class=workflow_scope tag_ref=${tag_ref}: GitHub refused the push because the release credential cannot update workflow files. Give GH_PAT the \`workflow\` scope (classic PAT) or \`workflows: write\` (fine-grained PAT / GitHub App), then re-run the release."
			return 1
		fi
		rm -f "${push_error_file}"

		remote_lookup_error_file="$(mktemp "${RUNNER_TEMP:-/tmp}/release_tag_lsremote_err.XXXXXX")"
		set +e
		remote_lookup_output="$(git ls-remote --exit-code origin "${tag_ref}" 2>"${remote_lookup_error_file}")"
		lookup_rc=$?
		set -e
		remote_lookup_error="$(<"${remote_lookup_error_file}")"
		rm -f "${remote_lookup_error_file}"
		remote_object_id=""
		if [ "${lookup_rc}" -eq 0 ]; then
			remote_object_id="$(printf '%s\n' "${remote_lookup_output}" | awk -v expected_ref="${tag_ref}" '$2 == expected_ref { print $1 }')"
			if [[ "${remote_object_id}" =~ ^[0-9a-fA-F]{40,64}$ ]] && [ "${remote_object_id}" = "${expected_object_id}" ]; then
				echo "Remote ${tag_ref} already matches the local tag object; treating the failed push as accepted."
				return 0
			fi
			if [ "${publication_mode}" = "immutable" ] && [[ "${remote_object_id}" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
				echo "::error::Immutable ${tag_ref} exists remotely at a different object ID; refusing to overwrite it."
				return 1
			fi
		fi

		if [ "${attempt}" -eq "${max_attempts}" ]; then
			echo "::error::Failed to publish and verify ${tag_ref} after ${max_attempts} attempts."
			if [ "${lookup_rc}" -ne 0 ] && [ -n "${remote_lookup_error}" ]; then
				printf '%s\n' "${remote_lookup_error}" >&2
			fi
			return 1
		fi

		echo "::warning::Push result for ${tag_ref} was not confirmed; retrying in ${backoff_seconds}s."
		sleep "${backoff_seconds}"
		backoff_seconds=$((backoff_seconds * 2))
		attempt=$((attempt + 1))
	done
}

# 6. Move the tags (lightweight, pointing at the version's tag object, the
# way the release job writes them).
if [ "${stable_done}" != "true" ]; then
	git tag -f stable "refs/tags/${target_version}" >/dev/null
	if ! publish_tag_with_remote_verification refs/tags/stable moving; then
		finish tag_push_failed 1 "tag=refs/tags/stable" "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}"
	fi
fi
if [ "${major_done}" != "true" ]; then
	git tag -f "${target_major}" "refs/tags/${target_version}" >/dev/null
	if ! publish_tag_with_remote_verification "refs/tags/${target_major}" moving; then
		finish tag_push_failed 1 "tag=refs/tags/${target_major}" "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}"
	fi
fi

# 7. Notify consumers (same registry validation as the release job's "Notify
# consumer repos via repository_dispatch" step). A failure only warns: the
# consumer's 04:00 UTC sync picks the rollback up.
consumers_ok=0
consumers_total=0
if [ ! -f "${STABLE_ROLLBACK_CONSUMER_FILE}" ]; then
	echo "No consumer registry at ${STABLE_ROLLBACK_CONSUMER_FILE} — skipping repository_dispatch."
elif ! jq -e 'type == "array"' "${STABLE_ROLLBACK_CONSUMER_FILE}" >/dev/null 2>&1; then
	echo "::warning::${STABLE_ROLLBACK_CONSUMER_FILE} is not a JSON array of owner/repo strings — skipping repository_dispatch."
else
	consumer_repos="$(jq -r '.[] | tostring | explode | map(if . < 32 or . == 127 then 32 else . end) | implode' "${STABLE_ROLLBACK_CONSUMER_FILE}" 2>/dev/null || true)"
	consumer_index=0
	while IFS= read -r consumer_repo; do
		consumer_index=$((consumer_index + 1))
		[ -z "${consumer_repo}" ] && continue
		# The rejected value is never printed, only its position in the list.
		if [[ ! "${consumer_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
			|| [[ "${consumer_repo}" =~ (^|/)\.{1,2}(/|$) ]]; then
			echo "::warning::Skipping malformed consumer registry entry #${consumer_index} in ${STABLE_ROLLBACK_CONSUMER_FILE} (expected owner/repo)."
			continue
		fi
		consumers_total=$((consumers_total + 1))
		echo "Dispatching coding-workflows-stable-released (${target_version}) to ${consumer_repo} ..."
		# A duplicate event is harmless: the consumer updater renders current
		# stable regardless of the payload, so a retried POST is safe.
		if GH_RETRY_IDEMPOTENT=true gh_retry gh api "repos/${consumer_repo}/dispatches" \
			-f event_type="coding-workflows-stable-released" \
			-f "client_payload[version]=${target_version}" \
			-f "client_payload[sha]=${ROLLBACK_TARGET_SHA}" \
			</dev/null >/dev/null; then
			consumers_ok=$((consumers_ok + 1))
			ROLLBACK_CONSUMER_LINES+=("${consumer_repo}: dispatched")
		else
			echo "::warning::Failed to dispatch to ${consumer_repo} (may lack permissions); its daily sync picks the rollback up."
			ROLLBACK_CONSUMER_LINES+=("${consumer_repo}: dispatch failed (daily sync picks it up)")
		fi
	done <<< "${consumer_repos}"
fi

# 8. Report.
if [ "${stable_done}" = "true" ] && [ "${major_done}" = "true" ]; then
	finish already_rolled_back 0 "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}" "consumers_dispatched=${consumers_ok}/${consumers_total}"
fi
finish rolled_back 0 "from=${ROLLBACK_FROM}" "to=${ROLLBACK_TO}" "consumers_dispatched=${consumers_ok}/${consumers_total}"
