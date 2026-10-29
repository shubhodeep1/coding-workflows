#!/usr/bin/env bash
# Hand a plan that is too large for one pipeline grant to the orchestrator (Q151).
#
# The plan stage's automation-path gate writes an exact files_touched grant
# for a pipeline-authored issue only when the plan lists at most 50 files
# (scripts/files_touched_scope_guard.py, reason plan_too_large_for_grant
# above that). Instead of holding such a plan for a human /approved, the plan
# stage calls this script. It starts an orchestrator project with the Claude
# engine (Q164): the decomposer splits the plan into issues of at most 50
# files each, in waves, on one integration branch.
#
# The source issue stays open (Q152): it gets ai:split-to-project instead of
# ai:awaiting-approval and a marker comment. orchestrate.yml links the tracking
# issue on it, and the poller closes it when the project completes.
#
# Environment:
#   GH_TOKEN                          token for gh (secrets.GH_PAT)
#   REPO                              owner/name of the repository
#   ISSUE_NUMBER                      the source issue
#   ISSUE_JSON_FILE                   the issue as read by the plan step
#   PLAN_FILE                         the plan text the planner wrote
#   PLAN_PATH_COUNT                   files the plan lists (from the gate)
#   PLAN_SPLIT_TO_PROJECT_ENABLED     default true; false holds as before
#   PLAN_SPLIT_MAX_DESCRIPTION_CHARS  default 60000 (dispatch input limit)
#   RUN_URL                           this run, for the comments
#
# Exit status: 0 when a project was started, or may have been (a dispatch
# failure without an HTTP 4xx rejection; the issue keeps ai:split-to-project
# and a WARNING asks a person to check), so the caller must not hold the
# plan; 3 when the plan should be held as before, 1 on a usage error.
# The issue is marked (label, then marker comment) before the dispatch, and
# released (release marker, then label off) when nothing was started. A run
# that loses a concurrent split to another run's earlier marker exits 0 too.
# Log prefix PLAN_SPLIT (outcome=dispatched|dispatch_unconfirmed|deduplicated|skipped reason=...).
set -uo pipefail

SPLIT_LABEL="ai:split-to-project"
SOURCE_MARKER_PREFIX="<!-- ai:plan-split:v1 issue="
# orchestrate.yml posts this when its run fails before a tracking issue
# exists, so a later /reclarify may split the plan again.
RELEASE_MARKER_PREFIX="<!-- ai:plan-split-released:v1 issue="
TRACKING_MARKER_PREFIX="<!-- ai:plan-split-source:v1 issue="

if [ -f scripts/gh_helpers.sh ]; then
	# shellcheck disable=SC1091
	source scripts/gh_helpers.sh >/dev/null 2>&1 || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

split_log()
{
	echo "PLAN_SPLIT issue=${ISSUE_NUMBER:-none} $*"
}

skip()
{
	split_log "outcome=skipped reason=$1${2:+ $2}"
	exit 3
}

flag_enabled()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
		""|1|true|yes|on) return 0 ;;
	esac
	return 1
}

[[ "${REPO:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "PLAN_SPLIT usage: REPO missing" >&2; exit 1; }
[[ "${ISSUE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || { echo "PLAN_SPLIT usage: ISSUE_NUMBER missing" >&2; exit 1; }
[ -s "${ISSUE_JSON_FILE:-}" ] || skip issue_json_missing
[ -s "${PLAN_FILE:-}" ] || skip plan_missing
if ! flag_enabled "${PLAN_SPLIT_TO_PROJECT_ENABLED:-true}"; then
	case "$(printf '%s' "${PLAN_SPLIT_TO_PROJECT_ENABLED}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
		0|false|no|off) ;;
		*) echo "::warning::PLAN_SPLIT_TO_PROJECT_ENABLED has an unrecognised value; treating it as false and holding the plan." ;;
	esac
	skip disabled
fi
path_count="${PLAN_PATH_COUNT:-0}"
[[ "${path_count}" =~ ^[0-9]+$ ]] || path_count=0
max_chars="${PLAN_SPLIT_MAX_DESCRIPTION_CHARS:-60000}"
[[ "${max_chars}" =~ ^[1-9][0-9]*$ ]] || max_chars=60000

# A project's own child issue is never split again: nesting projects could
# loop, and the decomposer already caps each child at 50 files.
if jq -e '([.labels[]?.name] | any(. == "ai:orchestrator-managed" or . == "ai:orchestrator-tracking"))
	or ((.body // "") | contains("Managed by: AI Orchestrator"))' "${ISSUE_JSON_FILE}" >/dev/null 2>&1; then
	skip orchestrator_managed
fi
if jq -e --arg l "${SPLIT_LABEL}" '[.labels[]?.name] | index($l) != null' "${ISSUE_JSON_FILE}" >/dev/null 2>&1; then
	skip already_split
fi

# Dedupe on a marker comment from the pipeline account, so a repeated plan
# run (or a removed label) never starts a second project. A split marker
# followed by a release marker (the project never started) does not count.
pipeline_login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null || true)"
[ -n "${pipeline_login}" ] || skip identity_unavailable
comments_file="$(mktemp)"
trap 'rm -f "${comments_file}" "${comments_file}.err"' EXIT
if ! gh_retry gh api --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100" \
	--jq '.[] | {login: (.user.login // ""), body: (.body // "")}' > "${comments_file}" 2>/dev/null; then
	skip comments_unavailable
fi
if jq -e -s --arg login "${pipeline_login}" --arg marker "${SOURCE_MARKER_PREFIX}${ISSUE_NUMBER} " \
	--arg release "${RELEASE_MARKER_PREFIX}${ISSUE_NUMBER} -->" '
	[ .[] | select(.login == $login) | .body ] as $bodies
	| ([ range(0; $bodies | length) | select($bodies[.] | contains($marker)) ] | max) as $split
	| ([ range(0; $bodies | length) | select($bodies[.] | contains($release)) ] | max) as $released
	| $split != null and ($released == null or $released < $split)' "${comments_file}" >/dev/null 2>&1; then
	skip already_split
fi

# The library uses internal-orchestrate.yml, consumers ai-orchestrate.yml.
# A wrapper that is missing or switched off holds the plan as before.
wrapper=""
for candidate in internal-orchestrate.yml ai-orchestrate.yml; do
	state="$(gh_retry gh api "repos/${REPO}/actions/workflows/${candidate}" --jq '.state // ""' 2>/dev/null || true)"
	if [ -n "${state}" ]; then
		[ "${state}" = active ] || skip orchestrator_wrapper_inactive "wrapper=${candidate} state=$(printf '%s' "${state}" | tr -cd 'a-z_')"
		wrapper="${candidate}"
		break
	fi
done
[ -n "${wrapper}" ] || skip orchestrator_wrapper_missing
default_branch="$(gh_retry gh api "repos/${REPO}" --jq '.default_branch // ""' 2>/dev/null || true)"
[ -n "${default_branch}" ] || skip default_branch_unavailable

issue_title="$(jq -r '(.title // "") | gsub("[\\r\\n]+"; " ") | sub("\\s+$"; "") | .[0:160]' "${ISSUE_JSON_FILE}")"
issue_url="https://github.com/${REPO}/issues/${ISSUE_NUMBER}"
# The decomposer cannot read issue comments, so the plan travels inline.
description="$(cat <<EOF
${issue_title:-Plan for issue #${ISSUE_NUMBER}} (split from #${ISSUE_NUMBER})

This is an approved implementation plan from issue #${ISSUE_NUMBER} (${issue_url}). The plan stage wrote it, and it lists ${path_count} files, more than one issue may change (50), so it is handed to the orchestrator to be split. Treat the plan below as the authoritative, already-clarified spec: read all of it before decomposing, keep its phases and their order, and give every issue at most 50 entries in files_touched.

Order the phases so that the phase which switches a behaviour on (enables a flag, wires a new path into a workflow, changes a default) comes last, after every phase it depends on.

----- Plan from issue #${ISSUE_NUMBER} -----
$(cat "${PLAN_FILE}")
EOF
)"
[ "${#description}" -le "${max_chars}" ] || skip description_too_large "chars=${#description} max=${max_chars}"

tracking_comment="$(cat <<EOF
${TRACKING_MARKER_PREFIX}${ISSUE_NUMBER} -->
## Split from #${ISSUE_NUMBER}

The plan for #${ISSUE_NUMBER} lists ${path_count} files, more than one pipeline grant covers, so the plan stage started this project (${RUN_URL:-plan run}). #${ISSUE_NUMBER} stays open and is closed when this project completes. Refs #${ISSUE_NUMBER}
EOF
)"

if [ -f scripts/label_helpers.sh ]; then
	# shellcheck disable=SC1091
	source scripts/label_helpers.sh >/dev/null 2>&1 || true
fi
if [ -f scripts/tg_helpers.sh ]; then
	# shellcheck disable=SC1091
	source scripts/tg_helpers.sh >/dev/null 2>&1 || true
fi
split_notify()
{
	if type tg_send_msg >/dev/null 2>&1; then
		tg_send_msg "$1"$'\n'"Issue: ${issue_url}" "$2" >/dev/null 2>&1 || true
	fi
}

# Undo the split marking when no project was started. The release marker is
# posted first (up to 3 attempts: a duplicate release marker is harmless)
# and the label comes off only after it is posted. A label removed without a
# release marker would leave the split marker blocking every later
# /reclarify, so when the marker cannot be posted the label stays on and a
# WARNING names the recovery. Restores ai:awaiting-approval so the plan
# stage's hold is what the issue shows.
release_split()
{
	local release_posted=false attempt
	for attempt in 1 2 3; do
		if gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" \
			-f body="${RELEASE_MARKER_PREFIX}${ISSUE_NUMBER} -->"$'\n'"No orchestrator project was started for this plan ($1), so it is held for approval as before." \
			>/dev/null 2>&1; then
			release_posted=true
			break
		fi
		[ "${attempt}" -lt 3 ] && sleep "${PLAN_SPLIT_RETRY_SLEEP_SECS:-2}"
	done
	if [ "${release_posted}" != true ]; then
		echo "::warning::Could not post the plan-split release marker on issue #${ISSUE_NUMBER} after $1, so ${SPLIT_LABEL} stays on; delete its plan-split comment, if any, remove ${SPLIT_LABEL}, then comment /reclarify."
		split_notify "Plan split for issue #${ISSUE_NUMBER}: no project started ($1) and the release marker could not be posted, so ${SPLIT_LABEL} stays on. Delete its plan-split comment, if any, remove ${SPLIT_LABEL}, then comment /reclarify." "WARNING"
		return 0
	fi
	if ! gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${REPO}" --remove-label "${SPLIT_LABEL}" \
		--add-label "ai:awaiting-approval" >/dev/null 2>&1; then
		echo "::warning::Could not remove ${SPLIT_LABEL} from issue #${ISSUE_NUMBER} after $1; the release marker is posted, so remove the label by hand to let the plan be split again."
	fi
}

# Mark the issue before the dispatch, so the orchestrator run (which links
# its tracking issue on, or releases, only an issue carrying the label) and
# a later plan run always see the split. A failed mark holds the plan.
if type ensure_label_exists >/dev/null 2>&1; then
	ensure_label_exists "${SPLIT_LABEL}" "${REPO}" >/dev/null 2>&1 || true
fi
if ! gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${REPO}" --add-label "${SPLIT_LABEL}" \
	--remove-label "ai:awaiting-approval" >/dev/null 2>&1; then
	# The two label changes are separate writes; put the issue back as far
	# as possible before holding.
	gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${REPO}" --remove-label "${SPLIT_LABEL}" \
		--add-label "ai:awaiting-approval" >/dev/null 2>&1 || true
	skip label_failed "wrapper=${wrapper}"
fi
source_comment="$(cat <<EOF
${SOURCE_MARKER_PREFIX}${ISSUE_NUMBER} wrapper=${wrapper} -->
## Plan handed to the orchestrator

This plan lists ${path_count} files, more than one pipeline grant covers (50), so instead of waiting for approval it is handed to the orchestrator as a project on the Claude engine (\`${wrapper}\`, ${RUN_URL:-plan run}). The project splits it into issues of at most 50 files, merges them on one integration branch and lands them together.

The tracking issue is linked here once it exists. This issue stays open and is closed when the project completes.
EOF
)"
if ! marker_id="$(gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" --jq '.id // ""' \
	-f body="${source_comment}" 2>/dev/null)"; then
	# A concurrent plan run may hold the split (its marker posted since the
	# last release marker); releasing here would clear its reservation and
	# label, so this run leaves the issue to it. An unreadable listing falls
	# back to the release, as no other reservation could be seen. A marker
	# carrying this run's RUN_URL is this run's own comment (created, but
	# the response was lost), not another run's reservation: it is ignored,
	# so the release below follows it and the plan is held.
	if gh_retry gh api --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100" \
		--jq '.[] | {id: .id, login: (.user.login // ""), body: (.body // "")}' > "${comments_file}" 2>/dev/null \
		&& jq -e -s --arg login "${pipeline_login}" --arg marker "${SOURCE_MARKER_PREFIX}${ISSUE_NUMBER} " \
			--arg release "${RELEASE_MARKER_PREFIX}${ISSUE_NUMBER} -->" --arg own "${RUN_URL:+${RUN_URL})}" '
			[ .[] | select(.login == $login) ] as $c
			| ([ range(0; $c | length) | select($c[.].body | contains($release)) ] | max // -1) as $rel
			| [ range($rel + 1; $c | length) | select(($c[.].body | contains($marker))
				and ($own == "" or ($c[.].body | contains($own) | not))) ] | length > 0' \
			"${comments_file}" >/dev/null 2>&1; then
		split_log "outcome=deduplicated reason=concurrent_split_marker_failed wrapper=${wrapper}"
		exit 0
	fi
	release_split "the plan-split comment could not be posted"
	skip marker_failed "wrapper=${wrapper}"
fi

# The marker is the reservation: two plan runs racing past the check above
# both post one, and only the run whose marker is the first since the last
# release marker dispatches. The other withdraws its marker and exits 0
# without holding, since the first run starts (or releases) the project.
if [[ "${marker_id}" =~ ^[0-9]+$ ]] && gh_retry gh api --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100" \
	--jq '.[] | {id: .id, login: (.user.login // ""), body: (.body // "")}' > "${comments_file}" 2>/dev/null; then
	first_marker_id="$(jq -r -s --arg login "${pipeline_login}" --arg marker "${SOURCE_MARKER_PREFIX}${ISSUE_NUMBER} " \
		--arg release "${RELEASE_MARKER_PREFIX}${ISSUE_NUMBER} -->" '
		[ .[] | select(.login == $login) ] as $c
		| ([ range(0; $c | length) | select($c[.].body | contains($release)) ] | max // -1) as $rel
		| [ range($rel + 1; $c | length) | select($c[.].body | contains($marker)) | $c[.].id ] | first // empty' \
		"${comments_file}" 2>/dev/null || true)"
	if [ -n "${first_marker_id}" ] && [ "${first_marker_id}" != "${marker_id}" ]; then
		gh_retry gh api -X DELETE "repos/${REPO}/issues/comments/${marker_id}" >/dev/null 2>&1 \
			|| echo "::warning::Could not delete the duplicate plan-split comment ${marker_id} on issue #${ISSUE_NUMBER}."
		split_log "outcome=deduplicated reason=concurrent_split wrapper=${wrapper} first_marker=${first_marker_id}"
		exit 0
	fi
fi

# No retry: a dispatch that timed out may still have started a project, and
# a second one would implement the plan twice. A rejection GitHub reports
# with an HTTP 4xx status started nothing: the split is released and the
# plan held. Any other failure may have started a project, so the split
# stays (no /approved, no new split) and a person is asked to check.
if ! gh workflow run "${wrapper}" --repo "${REPO}" --ref "${default_branch}" \
	-f "project_description=${description}" -f engine=claude -f "tracking_comment=${tracking_comment}" \
	>/dev/null 2>"${comments_file}.err"; then
	if grep -q 'tracking_comment' "${comments_file}.err" 2>/dev/null; then
		# A consumer whose @stable sync predates the tracking_comment input.
		release_split "the orchestrator wrapper has no tracking_comment input"
		skip wrapper_without_tracking_comment "wrapper=${wrapper}"
	fi
	if grep -Eq 'HTTP 4[0-9][0-9]' "${comments_file}.err" 2>/dev/null; then
		release_split "the dispatch of ${wrapper} was rejected"
		skip dispatch_failed "wrapper=${wrapper}"
	fi
	split_log "outcome=dispatch_unconfirmed wrapper=${wrapper} paths=${path_count}"
	echo "::warning::Dispatch of ${wrapper} for issue #${ISSUE_NUMBER} failed without a clear rejection; it may have started a project. The issue stays under ${SPLIT_LABEL}."
	# The release marker lets a person re-split after removing the label;
	# while the label stays, the label alone blocks a second project.
	gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" \
		-f body="${RELEASE_MARKER_PREFIX}${ISSUE_NUMBER} -->"$'\n'"The dispatch of \`${wrapper}\` failed without a clear rejection, so a project may or may not have started (${RUN_URL:-plan run}). If no tracking issue is linked here, remove \`${SPLIT_LABEL}\` and comment \`/reclarify\` to plan it again." \
		>/dev/null 2>&1 || echo "::warning::Could not post the unconfirmed-dispatch note on issue #${ISSUE_NUMBER}."
	split_notify "Plan split for issue #${ISSUE_NUMBER}: dispatch of ${wrapper} failed without a clear rejection and may have started a project. Check its runs; if none started, remove ${SPLIT_LABEL} and comment /reclarify." "WARNING"
	exit 0
fi
split_log "outcome=dispatched wrapper=${wrapper} paths=${path_count} chars=${#description}"
split_notify "Plan for issue #${ISSUE_NUMBER} lists ${path_count} files; handed to the orchestrator as a Claude project." "DEBUG"
exit 0
