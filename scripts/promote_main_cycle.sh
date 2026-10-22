#!/usr/bin/env bash
# promote_main_cycle.sh — the daily promote cycle for `main`.
#
# Runs from the `cycle` job of .github/workflows/promote-main-to-stable.yml.
# A promotion of `main` to `stable` happens only after main has been proven
# end to end, in this order:
#
#   1. code change on main since the last promotion (the `stable` tag) and
#      since the last cycle's baseline; a tip whose cycle failed (smoke gate
#      failed or timed out, dispatch failed) is retried on later ticks until
#      PROMOTE_CYCLE_MAX_ATTEMPTS failed cycle runs have covered it without a
#      code change in between, so a transient gate failure recovers by itself
#      and a deterministic one stops costing gate runs after the budget and
#      waits for main to move with real code;
#   2. smoke gate: test-and-mark-stable.yml in gate_only mode on main;
#   3. PROVING apply-analysis run dispatched (one analysis doc, labelled
#      ai:comprehensive-test-pending, marker comment carries the cycle SHAs).
#
# The orchestrator poller carries the cycle from there (verifying run,
# pinned promotion, merge hold) — see README 12f.
#
# Decision order (every exit is a logged, stable prefix):
#   PROMOTE_CYCLE_SKIPPED reason=disabled
#   PROMOTE_CYCLE_SKIPPED reason=another_run_active         a previous cycle job is still running
#   PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight            a labelled tracking issue is open
#   PROMOTE_CYCLE_SKIPPED reason=no_code_changes            nothing but non-code paths changed
#   PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_last_cycle
#   PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run   PROMOTE_CYCLE_MAX_ATTEMPTS
#                                                           failed cycle runs already
#                                                           covered this tip
#   PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor          main diverged from the stable
#                                                           tag / last baseline, or the
#                                                           stable BRANCH is not an
#                                                           ancestor of the main tip (or,
#                                                           pinned, of the frozen
#                                                           candidate; detail ref=heads/
#                                                           stable); cannot fast-forward,
#                                                           so no cycle
#   PROMOTE_CYCLE_SKIPPED reason=insufficient_docs          fewer than PROMOTE_CYCLE_MIN_DOCS docs
#   PROMOTE_CYCLE_SKIPPED reason=guard_unavailable          an API guard failed; fail closed
#   PROMOTE_CYCLE_SKIPPED reason=gate_busy                  another test-and-mark-stable run (a
#                                                           stable release, any branch) stayed
#                                                           active for the whole wait budget
#   PROMOTE_CYCLE_SKIPPED reason=smoke_gate_cancelled run=<id>  the gate was cancelled, not
#                                                           failed: nothing was proven either
#                                                           way, so the tip is retried next tick
#   PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=<id> conclusion=<c> failed_job=<name|unknown> failed_step=<name|unknown>
#   PROMOTE_CYCLE_FAILED reason=smoke_gate_timeout
#   PROMOTE_CYCLE_FAILED reason=smoke_head_checkout_failed  main advanced during the gate and
#                                                           the smoke-tested commit could not
#                                                           be checked out for the dispatch
#   PROMOTE_CYCLE_FAILED reason=pin_head_mismatch          pinned cycle: the gate ran on a
#                                                           commit other than the frozen candidate
#   PROMOTE_CYCLE_DISPATCHED doc=<path> baseline=<sha> smoke=<sha> pinned=<true|false>
#
# Pinned mode (docs/plans/pin-promote-cycle-to-candidate-plan.md, phase 3):
# after guards 1-6 the cycle checks that pinning can run (switch on, every
# internal-*.yml wrapper calls @promote-candidate, every reusable workflow
# resolves its support from promote-candidate, and the promote-candidate
# branch exists and is protected). When it can, the cycle freezes the branch
# at the candidate (the main tip) with the annotated tag promote-cycle-frozen,
# runs the smoke gate on the branch and passes the candidate as baseline and
# smoke SHA, so the verifying run promotes exactly that commit. Any skipped,
# failed or timed-out step after the freeze deletes the tag (EXIT trap). Log:
#   PROMOTE_CYCLE_PIN outcome=skipped reason=<disabled|wrappers_not_pinned|support_not_pinned|branch_missing|branch_unprotected|branch_lookup_failed|freeze_failed>
#   PROMOTE_CYCLE_PIN outcome=stale_tag_deleted age=<n>s|unknown
#   PROMOTE_CYCLE_PIN outcome=candidate_advanced main_tip=<sha> candidate=<sha>
#   PROMOTE_CYCLE_PIN outcome=frozen candidate=<sha>
# A skipped pre-flight or a failed freeze (other than a tag race, which skips
# with cycle_in_flight) runs the cycle unpinned, exactly as before.
#
# Non-code paths (do not count as a change): analysis/**, ai-memory/**,
# docs/**, tests/e2e_smoke_canary.txt, CHANGELOG.md and every *.md — except
# any CLAUDE.md and anything under a .claude/ directory (the root tree and the
# workflow-templates/.claude/ twin the @stable sync ships to consumers), and
# the markdown the pipeline reads into model context (#7662): README.md,
# unattended_system_instructions.md, ai_pipeline.md, agents.md, AGENTS.md,
# codex.md, probably_unnecessary_but_read_if_stuck.md and agents.d/**, which
# therefore count as code. The case pattern lines of is_code_path must stay
# identical to comprehensive_cycle_is_code_path in orchestrate_poll_process.sh.
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required), GH_TOKEN (required)
#   GITHUB_RUN_ID                       this run (excluded from the active-run guard)
#   GITHUB_OUTPUT                       receives outcome=/doc=/baseline_sha=/smoke_run_id=
#   PROMOTE_CYCLE_ENABLED               default true
#   PROMOTE_CYCLE_DEFAULT_BRANCH        default main
#   PROMOTE_CYCLE_STABLE_TAG            default stable
#   PROMOTE_CYCLE_STABLE_BRANCH         default stable (the branch the promotion
#                                       fast-forwards; must be an ancestor of the
#                                       candidate, #7888)
#   PROMOTE_CYCLE_WORKFLOW_FILE         default promote-main-to-stable.yml (self)
#   PROMOTE_CYCLE_GATE_WORKFLOW_FILE    default test-and-mark-stable.yml
#   PROMOTE_CYCLE_GATE_WAIT_SECS        default 18000
#   PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS   default 1800
#   PROMOTE_CYCLE_GATE_POLL_SECS        default 60
#   PROMOTE_CYCLE_MIN_DOCS              default 2 (proving + verifying)
#   PROMOTE_CYCLE_MAX_ATTEMPTS          default 3 (failed cycle runs per tip before
#                                       the tip is held; costs at most that many
#                                       extra compare calls per tick)
#   PROMOTE_CYCLE_TRACKING_LABEL        default ai:comprehensive-test-pending
#   APPLY_ANALYSIS_DISPATCHER           default scripts/apply_analysis_on_main.sh
#   PROMOTE_CYCLE_OPERATOR_STEP_HELPER  default scripts/operator_step_issue.py
#   PROMOTE_CYCLE_PIN_ENABLED           default true; only `false` (any case) turns
#                                       pinned mode off
#   PROMOTE_CYCLE_FREEZE_MAX_SECS       default 172800; age reported for a stale
#                                       promote-cycle-frozen tag
#
# Operator-step drain (plan item 4d): whenever the `stable` tag resolves, the
# cycle first runs `operator_step_issue.py tick` with the tag's commit, before
# the code-change guard (right after a promotion that guard usually skips with
# no_code_changes). Every ai:operator-step entry whose recorded merge commit is
# an ancestor of the tag commit gets a superseding ticked comment. It fetches
# commit history only (--filter=tree:0) for the local ancestor check, and it
# is fail-open: a fetch or tick failure logs a ::warning:: and the cycle
# continues unchanged. It rides PROMOTE_CYCLE_ENABLED and this nightly cron.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

PROMOTE_CYCLE_ENABLED="${PROMOTE_CYCLE_ENABLED:-true}"
PROMOTE_CYCLE_DEFAULT_BRANCH="${PROMOTE_CYCLE_DEFAULT_BRANCH:-main}"
PROMOTE_CYCLE_STABLE_TAG="${PROMOTE_CYCLE_STABLE_TAG:-stable}"
PROMOTE_CYCLE_STABLE_BRANCH="${PROMOTE_CYCLE_STABLE_BRANCH:-stable}"
PROMOTE_CYCLE_WORKFLOW_FILE="${PROMOTE_CYCLE_WORKFLOW_FILE:-promote-main-to-stable.yml}"
PROMOTE_CYCLE_GATE_WORKFLOW_FILE="${PROMOTE_CYCLE_GATE_WORKFLOW_FILE:-test-and-mark-stable.yml}"
PROMOTE_CYCLE_GATE_WAIT_SECS="${PROMOTE_CYCLE_GATE_WAIT_SECS:-18000}"
PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS="${PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS:-1800}"
PROMOTE_CYCLE_GATE_POLL_SECS="${PROMOTE_CYCLE_GATE_POLL_SECS:-60}"
PROMOTE_CYCLE_MIN_DOCS="${PROMOTE_CYCLE_MIN_DOCS:-2}"
PROMOTE_CYCLE_MAX_ATTEMPTS="${PROMOTE_CYCLE_MAX_ATTEMPTS:-3}"
PROMOTE_CYCLE_TRACKING_LABEL="${PROMOTE_CYCLE_TRACKING_LABEL:-ai:comprehensive-test-pending}"
APPLY_ANALYSIS_DISPATCHER="${APPLY_ANALYSIS_DISPATCHER:-${SCRIPT_DIR}/apply_analysis_on_main.sh}"
PROMOTE_CYCLE_OPERATOR_STEP_HELPER="${PROMOTE_CYCLE_OPERATOR_STEP_HELPER:-${SCRIPT_DIR}/operator_step_issue.py}"
PROMOTE_CYCLE_PIN_ENABLED="${PROMOTE_CYCLE_PIN_ENABLED:-true}"
PROMOTE_CYCLE_FREEZE_MAX_SECS="${PROMOTE_CYCLE_FREEZE_MAX_SECS:-172800}"
if ! [[ "${PROMOTE_CYCLE_FREEZE_MAX_SECS}" =~ ^[0-9]+$ ]] || [ "${PROMOTE_CYCLE_FREEZE_MAX_SECS}" -lt 1 ]; then
	echo "::warning::PROMOTE_CYCLE_FREEZE_MAX_SECS must be a positive integer; using 172800."
	PROMOTE_CYCLE_FREEZE_MAX_SECS=172800
fi
# Fixed names shared with promote_candidate_follow.sh and the poller.
PROMOTE_CYCLE_PIN_BRANCH="promote-candidate"
PROMOTE_CYCLE_PIN_TAG="promote-cycle-frozen"
# Reusable workflows whose support ref must follow promote-candidate (phase 1).
PROMOTE_CYCLE_PIN_REUSABLE_WORKFLOWS=(cancel_on_pr_close check_failure_triage clarify implement issue_pr_status memory_maintenance orchestrate_clarify_respond orchestrate_poll orchestrate plan review_autofix validate workflow_failure_heal)
GITHUB_RUN_ID="${GITHUB_RUN_ID:-0}"
CYCLE_BASELINE_MARKER="apply-analysis-cycle-baseline-sha"
# Same setting the poller and the dispatcher use: only marker comments from
# github-actions[bot] or these author_associations are cycle state.
COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS="${COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS:-OWNER,MEMBER,COLLABORATOR}"

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::promote_main_cycle.sh requires ${required_env}."
		exit 1
	fi
done
for numeric_env in PROMOTE_CYCLE_GATE_WAIT_SECS PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS PROMOTE_CYCLE_GATE_POLL_SECS PROMOTE_CYCLE_MIN_DOCS PROMOTE_CYCLE_MAX_ATTEMPTS; do
	if ! [[ "${!numeric_env}" =~ ^[0-9]+$ ]] || [ "${!numeric_env}" -lt 1 ]; then
		echo "::error::${numeric_env} must be a positive integer (got '${!numeric_env}')."
		exit 1
	fi
done

emit_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT}"
	fi
}

skip_cycle()
{
	local reason="$1"
	local detail="${2:-}"
	echo "PROMOTE_CYCLE_SKIPPED reason=${reason}${detail:+ ${detail}}"
	emit_output outcome "skipped:${reason}"
	exit 0
}

fail_cycle()
{
	local reason="$1"
	local detail="${2:-}"
	echo "PROMOTE_CYCLE_FAILED reason=${reason}${detail:+ ${detail}}"
	emit_output outcome "failed:${reason}"
	exit 1
}

# sanitize_gate_name <name>: whitespace becomes "_", only [A-Za-z0-9._:()/-]
# is kept, at most 80 characters, and an empty result becomes "unknown". Job
# and step names come from workflow YAML and must not add log fields.
sanitize_gate_name()
{
	local cleaned
	cleaned="$(printf '%s' "${1:-}" | tr '[:space:]' '_' | tr -cd 'A-Za-z0-9._:()/-' | cut -c1-80 || true)"
	printf '%s' "${cleaned:-unknown}"
}

# gate_failure_location <run_id>: prints "failed_job=<j> failed_step=<s>" for
# the first failed (or timed-out) job of the gate run and its first failed
# step, with "unknown" for anything that cannot be read. Never fails.
gate_failure_location()
{
	local run_id="$1"
	local jobs_json="" located="" job_name="" step_name=""
	# Cap this diagnostic read at 2 attempts so a failing jobs read cannot
	# hold up the failure exit (gh_retry defaults to 5 attempts).
	local jobs_attempts="${GH_RETRY_MAX_ATTEMPTS:-2}"
	if ! [[ "${jobs_attempts}" =~ ^[0-9]+$ ]] || [ "${jobs_attempts}" -gt 2 ] || [ "${jobs_attempts}" -lt 1 ]; then
		jobs_attempts=2
	fi
	# API-call audit (CLAUDE.md §15 / unattended §14): the calls this run
	# already makes are the self-runs listing, the in-flight issues listing,
	# the default-branch ref, the stable tag ref and tag object reads, the
	# compare read, the operator-step search/issues and issue-comments reads,
	# the gate idle check, the before-dispatch listing and the poll loop's
	# actions/workflows/<gate>/runs listing. None of them returns job or step
	# data (workflow-run objects carry only status and conclusion), so one
	# jobs read is needed. It is made only on the failure path; a successful
	# or cancelled gate makes no extra call.
	if [[ "${run_id}" =~ ^[0-9]+$ ]]; then
		if ! jobs_json="$(GH_RETRY_MAX_ATTEMPTS="${jobs_attempts}" gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs?per_page=100" 2>/dev/null)"; then
			jobs_json=""
			echo "::notice::promote cycle: jobs read for smoke-gate run ${run_id} failed; failed job and step are logged as unknown" >&2
		fi
	fi
	if [ -n "${jobs_json}" ]; then
		located="$(printf '%s' "${jobs_json}" | jq -r '
			[.jobs[]? | select(.conclusion == "failure" or .conclusion == "timed_out")]
			| first // empty
			| [(.name // ""), ([.steps[]? | select(.conclusion == "failure")] | first | .name // "")]
			| map(tostring | gsub("\\s"; "_"))
			| @tsv' 2>/dev/null || true)"
	fi
	if [ -n "${located}" ]; then
		job_name="${located%%$'\t'*}"
		step_name="${located#*$'\t'}"
	fi
	printf 'failed_job=%s failed_step=%s\n' "$(sanitize_gate_name "${job_name}")" "$(sanitize_gate_name "${step_name}")"
}

is_truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

# is_code_path <path>: 0 when the path counts as a code change.
is_code_path()
{
	local p="$1"
	case "${p}" in
		CLAUDE.md|*/CLAUDE.md|.claude/*|*/.claude/*|README.md|unattended_system_instructions.md|ai_pipeline.md|agents.md|AGENTS.md|codex.md|probably_unnecessary_but_read_if_stuck.md|agents.d/*) return 0 ;;
	esac
	case "${p}" in
		analysis/*|ai-memory/*|docs/*|tests/e2e_smoke_canary.txt|CHANGELOG.md|*.md) return 1 ;;
	esac
	return 0
}

# code_changes_between <base_sha> <head_sha>
# Sets CODE_CHANGES_OUT to the code paths changed between the two commits
# (one per line), or to "" when only non-code paths changed. Returns 2 on
# API failure. Runs in the main shell (no command substitution) so the
# callers' skip_cycle exits really exit. A compare payload truncated by
# GitHub (300 files / 250 commits) is treated as a code change: better a
# spurious cycle than a missed one.
CODE_CHANGES_OUT=""
code_changes_between()
{
	local base="$1"
	local head="$2"
	local payload
	CODE_CHANGES_OUT=""
	CODE_CHANGES_STATUS=""
	if ! payload="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/compare/${base}...${head}")"; then
		return 2
	fi
	local status total_commits file_count
	status="$(printf '%s' "${payload}" | jq -r '.status // empty')"
	total_commits="$(printf '%s' "${payload}" | jq -r '.total_commits // 0')"
	file_count="$(printf '%s' "${payload}" | jq -r '(.files // []) | length')"
	CODE_CHANGES_STATUS="${status}"
	case "${status}" in
		ahead) ;;
		identical|behind)
			return 0
			;;
		*)
			# `diverged` (or an unknown status): head does not descend from
			# base, so the promotion's fast-forward preflight would refuse it
			# anyway. Skip now instead of spending a smoke gate and a proving
			# run on a tip that cannot be promoted.
			return 3
			;;
	esac
	if [ "${file_count}" -ge 300 ] || [ "${total_commits}" -ge 250 ]; then
		CODE_CHANGES_OUT="(compare payload truncated: ${file_count} files, ${total_commits} commits — treated as a code change)"
		return 0
	fi
	local path
	while IFS= read -r path; do
		[ -n "${path}" ] || continue
		if is_code_path "${path}"; then
			CODE_CHANGES_OUT+="${path}"$'\n'
		fi
	done < <(printf '%s' "${payload}" | jq -r '(.files // [])[] | .filename, (.previous_filename // empty)')
	CODE_CHANGES_OUT="${CODE_CHANGES_OUT%$'\n'}"
	return 0
}

resolve_tag_commit()
{
	local tag="$1"
	local ref_json object_type object_sha tag_ref_error_file
	tag_ref_error_file="$(mktemp)"
	if ! ref_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${tag}" 2>"${tag_ref_error_file}")"; then
		if grep -qE '(^|[^0-9])404([^0-9]|$)' "${tag_ref_error_file}"; then
			rm -f "${tag_ref_error_file}"
			return 1
		fi
		cat "${tag_ref_error_file}" >&2
		rm -f "${tag_ref_error_file}"
		return 2
	fi
	rm -f "${tag_ref_error_file}"
	object_type="$(printf '%s' "${ref_json}" | jq -r '.object.type // empty' 2>/dev/null || true)"
	object_sha="$(printf '%s' "${ref_json}" | jq -r '.object.sha // empty' 2>/dev/null || true)"
	if [ "${object_type}" = "tag" ] && [[ "${object_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/tags/${object_sha}" | jq -er '.object.sha | select(test("^[0-9a-f]{40}$"))'
	elif [ "${object_type}" = "commit" ]; then
		[[ "${object_sha}" =~ ^[0-9a-f]{40}$ ]] || return 2
		printf '%s\n' "${object_sha}"
	else
		return 2
	fi
}

# last_cycle_baseline_sha: the baseline SHA recorded by the most recent
# cycle (any outcome), or empty. Exit 2 when the search failed.
# The search matches any comment body, so each candidate issue's comments
# are re-read and only a marker posted by github-actions[bot] or a trusted
# author_association counts; a stray comment cannot fake a completed cycle.
last_cycle_baseline_sha()
{
	local query numbers issue_number sha
	query="repo:${GITHUB_REPOSITORY} is:issue label:ai:orchestrator-tracking in:comments \"${CYCLE_BASELINE_MARKER}:\""
	if ! numbers="$(gh_retry gh api -X GET search/issues -f "q=${query}" -f sort=created -f order=desc -f per_page=10 2>/dev/null | jq -r '.items[]?.number | select(. != null)')"; then
		return 2
	fi
	[ -n "${numbers}" ] || return 0
	while IFS= read -r issue_number; do
		[[ "${issue_number}" =~ ^[0-9]+$ ]] || continue
		if ! sha="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_number}/comments?per_page=100" \
			| jq -r --arg m "${CYCLE_BASELINE_MARKER}" --arg trusted "${COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS}" '
				($trusted | split(",") | map(ascii_upcase | gsub("^\\s+|\\s+$"; ""))) as $ok
				| [.[]? | select((.user.login // "") == "github-actions[bot]" or (((.author_association // "") | ascii_upcase) as $a | $ok | index($a)) != null)
					| .body // "" | capture("(?m)^" + $m + ": (?<sha>[0-9a-f]{40})")? | .sha] | last // empty')"; then
			return 2
		fi
		if [ -n "${sha}" ]; then
			printf '%s\n' "${sha}"
			return 0
		fi
		echo "::warning::Tracking issue #${issue_number} matched the cycle-baseline marker only in comments from untrusted authors; ignoring it." >&2
	done <<< "${numbers}"
	return 0
}

# require_code_changes <base_sha> <head_sha> <skip_reason>
# Exits the cycle (skip) unless a code path changed between the commits.
require_code_changes()
{
	local rc
	set +e
	code_changes_between "$1" "$2"
	rc=$?
	set -e
	if [ "${rc}" -eq 3 ]; then
		skip_cycle base_not_ancestor "base=$1 head=$2 status=${CODE_CHANGES_STATUS:-unknown}"
	fi
	if [ "${rc}" -ne 0 ]; then
		skip_cycle guard_unavailable "compare=$1...$2"
	fi
	if [ -z "${CODE_CHANGES_OUT}" ]; then
		skip_cycle "$3" "base=$1 head=$2"
	fi
}

# resolve_stable_branch_commit: prints the stable branch tip (heads/, never
# the tag of the same name). Exit 2 on any failure, a missing branch included.
resolve_stable_branch_commit()
{
	local sha
	if ! sha="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${PROMOTE_CYCLE_STABLE_BRANCH}" 2>/dev/null | jq -r '.object.sha // empty' 2>/dev/null)"; then
		return 2
	fi
	[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || return 2
	printf '%s\n' "${sha}"
}

# require_stable_branch_ancestor <head_sha> <known_compared_base>
# Exits the cycle (skip) unless the stable branch is an ancestor of
# <head_sha> (identical or ahead), because promote-main-to-stable.yml can
# only fast-forward it (#7888). <known_compared_base> is a commit that
# require_code_changes already compared with <head_sha> (diverged and
# behind were skipped there), so a branch at that commit needs no second
# compare. Runs in the main shell so skip_cycle really exits.
# API calls: 1 ref read, +1 compare when neither shortcut applies.
require_stable_branch_ancestor()
{
	local head="$1"
	local known_base="${2:-}"
	local branch_sha rc
	set +e
	branch_sha="$(resolve_stable_branch_commit)"
	rc=$?
	set -e
	if [ "${rc}" -ne 0 ]; then
		skip_cycle guard_unavailable "lookup=branch:${PROMOTE_CYCLE_STABLE_BRANCH}"
	fi
	if [ "${branch_sha}" = "${head}" ]; then
		return 0
	fi
	if [ -n "${known_base}" ] && [ "${branch_sha}" = "${known_base}" ]; then
		return 0
	fi
	set +e
	code_changes_between "${branch_sha}" "${head}"
	rc=$?
	set -e
	if [ "${rc}" -eq 3 ] || { [ "${rc}" -eq 0 ] && [ "${CODE_CHANGES_STATUS:-}" != "ahead" ] && [ "${CODE_CHANGES_STATUS:-}" != "identical" ]; }; then
		skip_cycle base_not_ancestor "base=${branch_sha} head=${head} status=${CODE_CHANGES_STATUS:-unknown} ref=heads/${PROMOTE_CYCLE_STABLE_BRANCH}"
	fi
	if [ "${rc}" -ne 0 ]; then
		skip_cycle guard_unavailable "compare=${branch_sha}...${head}"
	fi
	return 0
}

# tick_operator_steps <tag_commit>: tick ai:operator-step entries whose merge
# commit the `stable` tag now includes (plan item 4d). Always returns 0.
tick_operator_steps()
{
	local tag_sha="$1"
	local helper="${PROMOTE_CYCLE_OPERATOR_STEP_HELPER}"
	local tick_output tick_rc
	local -a fetch_args=(--quiet --filter=tree:0)
	if ! [[ "${tag_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "::warning::Operator-step tick skipped: invalid stable commit."
		return 0
	fi
	if [ ! -f "${helper}" ]; then
		echo "::warning::Operator-step tick skipped: ${helper} is missing."
		return 0
	fi
	# The cycle checkout is shallow; the ancestor check needs commit history
	# (no trees or blobs). A full clone rejects --unshallow, so ask first.
	if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || true)" = "true" ]; then
		fetch_args+=(--unshallow)
	fi
	if ! git fetch "${fetch_args[@]}" origin "+refs/tags/${PROMOTE_CYCLE_STABLE_TAG}:refs/tags/${PROMOTE_CYCLE_STABLE_TAG}" "${PROMOTE_CYCLE_DEFAULT_BRANCH}" >/dev/null 2>&1; then
		echo "::warning::Operator-step tick: commit history fetch failed; entries whose commits are missing stay open."
	fi
	set +e
	tick_output="$(PYTHONDONTWRITEBYTECODE=1 python3 "${helper}" tick --repo "${GITHUB_REPOSITORY}" --stable-sha "${tag_sha}" --repo-dir "${REPO_ROOT}" 2>&1)"
	tick_rc=$?
	set -e
	if [ "${tick_rc}" -ne 0 ]; then
		echo "::warning::Operator-step tick failed (rc=${tick_rc}): $(printf '%s' "${tick_output}" | tr '\r\n' '  ' | cut -c1-300)"
		return 0
	fi
	printf '%s\n' "${tick_output}"
	return 0
}

# --- Pinned mode (phase 3) ----------------------------------------------------
# API calls (§14): pre-flight makes 1 read (branches/promote-candidate). A
# freeze makes 1 tag-ref read, 2 POSTs (tag object, tag ref) and 1 branch
# PATCH; +1 tag-object read and 1 DELETE when a stale tag exists; +1 branch
# read, 1 compare, 1 POST and 1 tag-ref PATCH when the follower already moved
# the branch past the main tip. The EXIT trap adds 1 DELETE on a skip or fail.
# None of these runs inside a loop.
pin_mode=false
pin_candidate_sha=""
PIN_PREFLIGHT_REASON=""
PIN_FREEZE_DETAIL=""
PIN_TAG_CREATED=false

is_http_404()
{
	grep -qE '(^|[^0-9])404([^0-9]|$)' "$1" 2>/dev/null
}

# pin_preflight: 0 when pinned mode can run; else sets PIN_PREFLIGHT_REASON.
pin_preflight()
{
	PIN_PREFLIGHT_REASON=""
	if [ "$(printf '%s' "${PROMOTE_CYCLE_PIN_ENABLED}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
		PIN_PREFLIGHT_REASON=disabled
		return 1
	fi
	local -a wrappers=()
	local name unpinned branch_json protected err_file
	shopt -s nullglob
	wrappers=(.github/workflows/internal-*.yml)
	shopt -u nullglob
	if [ "${#wrappers[@]}" -eq 0 ]; then
		PIN_PREFLIGHT_REASON=wrappers_not_pinned
		return 1
	fi
	unpinned="$(grep -L '@promote-candidate' "${wrappers[@]}" 2>/dev/null || true)"
	if [ -n "${unpinned}" ]; then
		PIN_PREFLIGHT_REASON=wrappers_not_pinned
		return 1
	fi
	for name in "${PROMOTE_CYCLE_PIN_REUSABLE_WORKFLOWS[@]}"; do
		if [ ! -f ".github/workflows/${name}.yml" ] || ! grep -q 'source=promote_candidate' ".github/workflows/${name}.yml"; then
			PIN_PREFLIGHT_REASON=support_not_pinned
			return 1
		fi
	done
	err_file="$(mktemp)"
	if ! branch_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/branches/${PROMOTE_CYCLE_PIN_BRANCH}" 2>"${err_file}")"; then
		if is_http_404 "${err_file}"; then
			PIN_PREFLIGHT_REASON=branch_missing
		else
			PIN_PREFLIGHT_REASON=branch_lookup_failed
		fi
		rm -f "${err_file}"
		return 1
	fi
	rm -f "${err_file}"
	protected="$(printf '%s' "${branch_json}" | jq -r '.protected // false' 2>/dev/null || true)"
	if [ "${protected}" != "true" ]; then
		PIN_PREFLIGHT_REASON=branch_unprotected
		return 1
	fi
	return 0
}

# pin_delete_tag: 0 when the freeze tag is gone (deleted or already absent).
pin_delete_tag()
{
	local err_file
	err_file="$(mktemp)"
	if gh_retry gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/refs/tags/${PROMOTE_CYCLE_PIN_TAG}" >/dev/null 2>"${err_file}"; then
		rm -f "${err_file}"
		return 0
	fi
	if is_http_404 "${err_file}" || grep -qiE '(^|[^0-9])422([^0-9]|$)|does not exist' "${err_file}"; then
		rm -f "${err_file}"
		return 0
	fi
	rm -f "${err_file}"
	return 1
}

# pin_release_freeze: EXIT trap after the freeze; never changes the exit code.
pin_release_freeze()
{
	[ "${PIN_TAG_CREATED}" = "true" ] || return 0
	if pin_delete_tag; then
		PIN_TAG_CREATED=false
		echo "Released the ${PROMOTE_CYCLE_PIN_TAG} tag (the cycle stopped before dispatching its proving run)."
	else
		echo "::warning::Could not delete the ${PROMOTE_CYCLE_PIN_TAG} tag; the next cycle or the follower's age limit removes it."
	fi
	return 0
}

# pin_create_tag_object <commit_sha>: prints the new annotated tag object SHA.
pin_create_tag_object()
{
	local frozen_at run_id
	frozen_at="$(date +%s)"
	run_id="${GITHUB_RUN_ID}"
	[[ "${run_id}" =~ ^[0-9]+$ ]] || run_id=0
	gh api -X POST "repos/${GITHUB_REPOSITORY}/git/tags" \
		-f "tag=${PROMOTE_CYCLE_PIN_TAG}" \
		-f "message=frozen_at=${frozen_at} cycle_run=${run_id}" \
		-f "object=$1" \
		-f "type=commit" 2>/dev/null | jq -er '.sha | select(test("^[0-9a-f]{40}$"))'
}

# pin_freeze <main_tip>: 0 frozen (pin_candidate_sha set, EXIT trap armed);
# 1 failed (PIN_FREEZE_DETAIL set; caller runs unpinned). A tag race skips the
# cycle with cycle_in_flight. Never call in a command substitution.
pin_freeze()
{
	local tip="$1"
	local err_file tag_ref_json tag_obj frozen_at age_note tag_obj_sha branch_tip compare_status new_obj
	PIN_FREEZE_DETAIL=""
	err_file="$(mktemp)"
	# Guards 1-2 showed no other cycle job and no labelled tracking issue, so a
	# tag that still exists is stale.
	if tag_ref_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${PROMOTE_CYCLE_PIN_TAG}" 2>"${err_file}")"; then
		age_note="age=unknown"
		tag_obj="$(printf '%s' "${tag_ref_json}" | jq -r 'if (.object.type // "") == "tag" then .object.sha else empty end' 2>/dev/null || true)"
		if [[ "${tag_obj}" =~ ^[0-9a-f]{40}$ ]]; then
			frozen_at="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/tags/${tag_obj}" 2>/dev/null | jq -r '.message // ""' 2>/dev/null | sed -nE 's/^frozen_at=([0-9]+)( .*)?$/\1/p' | head -n 1 || true)"
			if [[ "${frozen_at}" =~ ^[0-9]+$ ]]; then
				age_note="age=$(( $(date +%s) - frozen_at ))s"
			fi
		fi
		echo "::warning::Stale ${PROMOTE_CYCLE_PIN_TAG} tag with no cycle in flight (${age_note}, limit ${PROMOTE_CYCLE_FREEZE_MAX_SECS}s); deleting it."
		if ! pin_delete_tag; then
			rm -f "${err_file}"
			PIN_FREEZE_DETAIL=stale_tag_delete
			return 1
		fi
		echo "PROMOTE_CYCLE_PIN outcome=stale_tag_deleted ${age_note}"
	elif ! is_http_404 "${err_file}"; then
		rm -f "${err_file}"
		PIN_FREEZE_DETAIL=tag_lookup
		return 1
	fi
	if ! tag_obj_sha="$(pin_create_tag_object "${tip}")"; then
		rm -f "${err_file}"
		PIN_FREEZE_DETAIL=tag_object_create
		return 1
	fi
	# One attempt: a retried ref create is not idempotent.
	if ! gh api -X POST "repos/${GITHUB_REPOSITORY}/git/refs" -f "ref=refs/tags/${PROMOTE_CYCLE_PIN_TAG}" -f "sha=${tag_obj_sha}" >/dev/null 2>"${err_file}"; then
		if grep -qi 'already exists' "${err_file}"; then
			rm -f "${err_file}"
			skip_cycle cycle_in_flight "tag=${PROMOTE_CYCLE_PIN_TAG}"
		fi
		rm -f "${err_file}"
		# GitHub may have created the ref even though the response failed;
		# guards 1-2 showed no other cycle owns it, so remove it (idempotent).
		pin_delete_tag || echo "::warning::Could not delete a possibly created ${PROMOTE_CYCLE_PIN_TAG} tag; the next cycle or the follower's age limit removes it."
		PIN_FREEZE_DETAIL=tag_ref_create
		return 1
	fi
	PIN_TAG_CREATED=true
	trap pin_release_freeze EXIT
	if gh api -X PATCH "repos/${GITHUB_REPOSITORY}/git/refs/heads/${PROMOTE_CYCLE_PIN_BRANCH}" -f "sha=${tip}" -F force=false >/dev/null 2>"${err_file}"; then
		rm -f "${err_file}"
		pin_candidate_sha="${tip}"
		return 0
	fi
	rm -f "${err_file}"
	# Not a fast-forward: the follower may already have moved the branch past
	# the tip read at cycle start. Then the branch tip is the candidate.
	branch_tip="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${PROMOTE_CYCLE_PIN_BRANCH}" 2>/dev/null | jq -r '.object.sha // empty' 2>/dev/null || true)"
	if ! [[ "${branch_tip}" =~ ^[0-9a-f]{40}$ ]]; then
		PIN_FREEZE_DETAIL=branch_patch
		return 1
	fi
	if [ "${branch_tip}" = "${tip}" ]; then
		pin_candidate_sha="${tip}"
		return 0
	fi
	compare_status="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/compare/${tip}...${branch_tip}" 2>/dev/null | jq -r '.status // empty' 2>/dev/null || true)"
	if [ -z "${compare_status}" ]; then
		PIN_FREEZE_DETAIL=compare_unavailable
		return 1
	fi
	if [ "${compare_status}" != "ahead" ]; then
		PIN_FREEZE_DETAIL=branch_not_fast_forward
		return 1
	fi
	if ! new_obj="$(pin_create_tag_object "${branch_tip}")"; then
		PIN_FREEZE_DETAIL=candidate_tag_object
		return 1
	fi
	# Re-points the tag ref (never a branch) at the new tag object.
	if ! gh api -X PATCH "repos/${GITHUB_REPOSITORY}/git/refs/tags/${PROMOTE_CYCLE_PIN_TAG}" -f "sha=${new_obj}" -F force=true >/dev/null 2>&1; then
		PIN_FREEZE_DETAIL=candidate_tag_ref
		return 1
	fi
	pin_candidate_sha="${branch_tip}"
	echo "PROMOTE_CYCLE_PIN outcome=candidate_advanced main_tip=${tip} candidate=${branch_tip}"
	return 0
}

if ! is_truthy "${PROMOTE_CYCLE_ENABLED}"; then
	skip_cycle disabled
fi

# 1. Another cycle job still running (the schedule fired while a previous
#    tick waits on its smoke gate): skip, do not queue behind it.
self_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_WORKFLOW_FILE}/runs?per_page=20")"
other_active="$(printf '%s' "${self_runs_json}" | jq -r --arg me "${GITHUB_RUN_ID}" '[.workflow_runs[]? | select((.id|tostring) != $me) | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
[[ "${other_active}" =~ ^[0-9]+$ ]] || other_active=0
if [ "${other_active}" -gt 0 ]; then
	skip_cycle another_run_active "active_runs=${other_active}"
fi

# 2. A cycle already in flight (proving or verifying run open).
encoded_labels="$(printf '%s' "ai:orchestrator-tracking,${PROMOTE_CYCLE_TRACKING_LABEL}" | sed 's/:/%3A/g; s/,/%2C/g')"
in_flight="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues?state=open&labels=${encoded_labels}&per_page=100" | jq -r '[.[] | select(has("pull_request") | not) | .number] | join(",")')"
if [ -n "${in_flight}" ]; then
	skip_cycle cycle_in_flight "tracking_issues=${in_flight}"
fi

# 3. Code change since the last promotion.
main_tip="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${PROMOTE_CYCLE_DEFAULT_BRANCH}" | jq -r '.object.sha // empty')"
if ! [[ "${main_tip}" =~ ^[0-9a-f]{40}$ ]]; then
	echo "::error::Could not resolve branch ${PROMOTE_CYCLE_DEFAULT_BRANCH}."
	exit 1
fi
set +e
tag_commit="$(resolve_tag_commit "${PROMOTE_CYCLE_STABLE_TAG}")"
tag_lookup_rc=$?
set -e
case "${tag_lookup_rc}" in
	0)
		# Before the code-change guard: right after a promotion it skips.
		tick_operator_steps "${tag_commit}" || true
		require_code_changes "${tag_commit}" "${main_tip}" no_code_changes
		require_stable_branch_ancestor "${main_tip}" "${tag_commit}"
		echo "Code changes since ${PROMOTE_CYCLE_STABLE_TAG} (${tag_commit:0:7}):"
		printf '%s\n' "${CODE_CHANGES_OUT}" | sed 's/^/  /'
		;;
	1)
		echo "::warning::Tag ${PROMOTE_CYCLE_STABLE_TAG} does not exist; treating main as never promoted."
		require_stable_branch_ancestor "${main_tip}" ""
		;;
	*)
		skip_cycle guard_unavailable "lookup=tag:${PROMOTE_CYCLE_STABLE_TAG}"
		;;
esac

# 4. Not the same tip the last cycle already covered (Q14: retry only after
#    main moves with code), regardless of that cycle's outcome.
set +e
last_baseline="$(last_cycle_baseline_sha)"
last_baseline_rc=$?
set -e
if [ "${last_baseline_rc}" -ne 0 ]; then
	skip_cycle guard_unavailable "search=${CYCLE_BASELINE_MARKER}"
fi
if [[ "${last_baseline}" =~ ^[0-9a-f]{40}$ ]] && [ "${last_baseline}" != "${tag_commit:-}" ]; then
	require_code_changes "${last_baseline}" "${main_tip}" no_code_changes_since_last_cycle
fi

# 5. Not a tip whose smoke gate, dispatch, or scheduled cycle job already
#    failed, timed out, or was cancelled PROMOTE_CYCLE_MAX_ATTEMPTS times.
#    Failed scheduled cycle runs are walked
#    newest first; each one whose head has no code change up to the current
#    tip is one spent attempt, and the walk stops at the first run that a code
#    change separates from the tip (older runs predate that change). The
#    newest failed run keeps the pre-budget handling of a diverged head
#    (skip base_not_ancestor) and of a compare failure (skip guard_unavailable).
failed_cycle_heads="$(printf '%s' "${self_runs_json}" | jq -r --argjson max "${PROMOTE_CYCLE_MAX_ATTEMPTS}" '[.workflow_runs[]? | select(.event == "schedule" and .status == "completed" and (.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out"))] | sort_by(.created_at) | reverse | .[:$max] | .[].head_sha // empty')"
failed_attempts_on_tip=0
last_failed_head=""
while IFS= read -r failed_cycle_head; do
	[[ "${failed_cycle_head}" =~ ^[0-9a-f]{40}$ ]] || continue
	[ -n "${last_failed_head}" ] || last_failed_head="${failed_cycle_head}"
	if [ "${failed_cycle_head}" = "${main_tip}" ]; then
		failed_attempts_on_tip=$((failed_attempts_on_tip + 1))
		continue
	fi
	set +e
	code_changes_between "${failed_cycle_head}" "${main_tip}"
	failed_compare_rc=$?
	set -e
	if [ "${failed_compare_rc}" -eq 3 ]; then
		if [ "${failed_cycle_head}" = "${last_failed_head}" ]; then
			skip_cycle base_not_ancestor "base=${failed_cycle_head} head=${main_tip} status=${CODE_CHANGES_STATUS:-unknown}"
		fi
		break
	fi
	if [ "${failed_compare_rc}" -ne 0 ]; then
		if [ "${failed_cycle_head}" = "${last_failed_head}" ]; then
			skip_cycle guard_unavailable "compare=${failed_cycle_head}...${main_tip}"
		fi
		break
	fi
	[ -z "${CODE_CHANGES_OUT}" ] || break
	failed_attempts_on_tip=$((failed_attempts_on_tip + 1))
done <<< "${failed_cycle_heads}"
if [ "${failed_attempts_on_tip}" -ge "${PROMOTE_CYCLE_MAX_ATTEMPTS}" ]; then
	skip_cycle no_code_changes_since_failed_run "base=${last_failed_head} head=${main_tip} attempts=${failed_attempts_on_tip} max=${PROMOTE_CYCLE_MAX_ATTEMPTS}"
fi
if [ "${failed_attempts_on_tip}" -gt 0 ]; then
	echo "Retrying the cycle on ${main_tip}: ${failed_attempts_on_tip} failed attempt(s) since the last code change, budget ${PROMOTE_CYCLE_MAX_ATTEMPTS}."
fi

# 6. Enough analysis docs for a proving AND a verifying run.
candidate_count="$(APPLY_ANALYSIS_LIST_ONLY=true GITHUB_OUTPUT="" bash "${APPLY_ANALYSIS_DISPATCHER}" | sed -n 's/^APPLY_ANALYSIS_CANDIDATES count=\([0-9]*\).*/\1/p')"
[[ "${candidate_count}" =~ ^[0-9]+$ ]] || candidate_count=0
if [ "${candidate_count}" -lt "${PROMOTE_CYCLE_MIN_DOCS}" ]; then
	skip_cycle insufficient_docs "available=${candidate_count} required=${PROMOTE_CYCLE_MIN_DOCS}"
fi

# 6b. Pinned-mode pre-flight (phase 3). Any missing prerequisite runs the
#     cycle unpinned, exactly as before.
if pin_preflight; then
	pin_mode=true
else
	echo "PROMOTE_CYCLE_PIN outcome=skipped reason=${PIN_PREFLIGHT_REASON}"
fi

# 7. Smoke gate on main.
#
# The gate workflow's e2e-smoke-test job runs under a per-repository
# concurrency group with cancel-in-progress, so dispatching our gate while a
# stable release gate is running would cancel that release. Wait for the
# gate workflow to be idle on every branch first, and skip the tick if it
# never frees within the separate idle-wait budget.
idle_wait_deadline=$(( $(date +%s) + PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS ))
while :; do
	active_gate_runs="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?per_page=30" \
		| jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length' 2>/dev/null || echo "")"
	[[ "${active_gate_runs}" =~ ^[0-9]+$ ]] || skip_cycle guard_unavailable "lookup=workflow-runs:${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}"
	if [ "${active_gate_runs}" -eq 0 ]; then
		break
	fi
	if [ "$(date +%s)" -ge "${idle_wait_deadline}" ]; then
		skip_cycle gate_busy "active_runs=${active_gate_runs} waited=${PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS}s"
	fi
	echo "Waiting: ${active_gate_runs} ${PROMOTE_CYCLE_GATE_WORKFLOW_FILE} run(s) active (a stable release gate must not be cancelled by ours)."
	sleep "${PROMOTE_CYCLE_GATE_POLL_SECS}"
done
# Freeze after the idle wait, right before the gate, so a gate_busy skip never
# touches the tag and the freeze window stays short.
gate_ref="${PROMOTE_CYCLE_DEFAULT_BRANCH}"
cycle_baseline_sha="${main_tip}"
if [ "${pin_mode}" = "true" ]; then
	if pin_freeze "${main_tip}"; then
		gate_ref="${PROMOTE_CYCLE_PIN_BRANCH}"
		cycle_baseline_sha="${pin_candidate_sha}"
		echo "PROMOTE_CYCLE_PIN outcome=frozen candidate=${pin_candidate_sha}"
	else
		pin_release_freeze || true
		trap - EXIT
		pin_mode=false
		echo "PROMOTE_CYCLE_PIN outcome=skipped reason=freeze_failed detail=${PIN_FREEZE_DETAIL:-unknown}"
	fi
fi
# Pinned: the stable branch can move between step 3 and the freeze, and an
# adopted candidate can differ from main_tip, so check it again. A skip here
# releases the freeze tag through the EXIT trap.
if [ "${pin_mode}" = "true" ]; then
	require_stable_branch_ancestor "${pin_candidate_sha}" ""
fi
before_gate_ids="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=50" | jq -c '[.workflow_runs[]?.id]')"
echo "Dispatching ${PROMOTE_CYCLE_GATE_WORKFLOW_FILE} on ${gate_ref} with gate_only=true (smoke gate for ${cycle_baseline_sha})."
gh_retry gh workflow run "${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "${gate_ref}" \
	-f gate_only=true \
	-f "gate_cycle_id=${GITHUB_RUN_ID}"
smoke_sha="${cycle_baseline_sha}"

deadline=$(( $(date +%s) + PROMOTE_CYCLE_GATE_WAIT_SECS ))
gate_run_id=""
gate_conclusion=""
gate_head_sha=""
while [ "$(date +%s)" -lt "${deadline}" ]; do
	sleep "${PROMOTE_CYCLE_GATE_POLL_SECS}"
	runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=50")" || continue
	if [ -z "${gate_run_id}" ]; then
		gate_run_id="$(printf '%s' "${runs_json}" | jq -r --argjson before "${before_gate_ids}" --arg branch "${gate_ref}" --arg title "Test & Mark Stable Release [cycle:${GITHUB_RUN_ID}]" '[.workflow_runs[]? | select((.id as $id | ($before | index($id) | not)) and (.head_branch // "") == $branch and (.display_title // "") == $title)] | sort_by(.created_at) | first | .id // empty')"
		[ -n "${gate_run_id}" ] || continue
		echo "Smoke gate run: ${gate_run_id}"
	fi
	run_state="$(printf '%s' "${runs_json}" | jq -r --argjson id "${gate_run_id}" '[.workflow_runs[]? | select(.id == $id)] | first | ((.status // "") + " " + (.conclusion // "") + " " + (.head_sha // ""))')"
	run_status="$(printf '%s' "${run_state}" | cut -d' ' -f1)"
	run_conclusion="$(printf '%s' "${run_state}" | cut -d' ' -f2)"
	gate_head_sha="$(printf '%s' "${run_state}" | cut -d' ' -f3)"
	if [ "${run_status}" = "completed" ]; then
		gate_conclusion="${run_conclusion}"
		break
	fi
done
if [ -z "${gate_run_id}" ]; then
	fail_cycle smoke_gate_timeout "no run appeared within ${PROMOTE_CYCLE_GATE_WAIT_SECS}s"
fi
emit_output smoke_run_id "${gate_run_id}"
if [ -z "${gate_conclusion}" ]; then
	fail_cycle smoke_gate_timeout "run=${gate_run_id} still running after ${PROMOTE_CYCLE_GATE_WAIT_SECS}s"
fi
if [ "${gate_conclusion}" = "cancelled" ]; then
	# A cancelled gate proved nothing either way (concurrency, a runner loss,
	# an operator). Skip rather than fail so the tip is retried next tick
	# instead of waiting for main to move.
	skip_cycle smoke_gate_cancelled "run=${gate_run_id}"
fi
if [ "${gate_conclusion}" != "success" ]; then
	gate_failure_where="$(gate_failure_location "${gate_run_id}" || echo "failed_job=unknown failed_step=unknown")"
	fail_cycle smoke_gate_failed "run=${gate_run_id} conclusion=${gate_conclusion} ${gate_failure_where}"
fi
# Pinned: the gate ran on the frozen branch, so its head must be the candidate.
if [ "${pin_mode}" = "true" ]; then
	if ! [[ "${gate_head_sha:-}" =~ ^[0-9a-f]{40}$ ]] || [ "${gate_head_sha}" != "${pin_candidate_sha}" ]; then
		fail_cycle pin_head_mismatch "gate_head=${gate_head_sha:-none} candidate=${pin_candidate_sha}"
	fi
	smoke_sha="${pin_candidate_sha}"
# The gate checked out whatever main was when its run started; that commit,
# not the tip read before dispatch, is what was smoke-tested. Anything that
# lands after it shows up as untested in the poller's quiescence check.
elif [[ "${gate_head_sha:-}" =~ ^[0-9a-f]{40}$ ]]; then
	if [ "${gate_head_sha}" != "${main_tip}" ]; then
		echo "::warning::main advanced from ${main_tip} to ${gate_head_sha} before the smoke gate started; recording the gate's head as the smoke-tested commit."
	fi
	smoke_sha="${gate_head_sha}"
fi
echo "Smoke gate passed on ${smoke_sha} (run ${gate_run_id})."

# The dispatcher reads the analysis docs from this checkout and stamps
# GITHUB_SHA into the project description, so both must be the smoke-tested
# commit, not the tip this job happened to check out. Move the working tree
# there when the gate tested a newer commit (or, pinned, when the candidate
# advanced past the tip read at cycle start); refuse the cycle if that fails.
if [ "${smoke_sha}" != "${main_tip}" ]; then
	if ! git fetch --quiet --depth 1 origin "${smoke_sha}" \
		|| ! git checkout --quiet --detach "${smoke_sha}" \
		|| [ "$(git rev-parse HEAD 2>/dev/null || true)" != "${smoke_sha}" ]; then
		fail_cycle smoke_head_checkout_failed "sha=${smoke_sha}"
	fi
	echo "Checked out the smoke-tested commit ${smoke_sha} for the proving dispatch."
fi

# 8. Proving run.
dispatch_output="$(APPLY_ANALYSIS_ROLE=proving \
	APPLY_ANALYSIS_PINNED="${pin_mode}" \
	APPLY_ANALYSIS_CYCLE_BASELINE_SHA="${cycle_baseline_sha}" \
	APPLY_ANALYSIS_SMOKE_SHA="${smoke_sha}" \
	GITHUB_SHA="${smoke_sha}" \
	GITHUB_OUTPUT="" \
	bash "${APPLY_ANALYSIS_DISPATCHER}")"
printf '%s\n' "${dispatch_output}"
dispatched_doc="$(printf '%s\n' "${dispatch_output}" | sed -n 's/^APPLY_ANALYSIS_DISPATCHED doc=\([^ ]*\).*/\1/p')"
if [ -z "${dispatched_doc}" ]; then
	skip_reason="$(printf '%s\n' "${dispatch_output}" | sed -n 's/^APPLY_ANALYSIS_SKIPPED reason=\([^ ]*\).*/\1/p' | tail -n 1)"
	skip_cycle "dispatcher:${skip_reason:-unknown}" "smoke_run=${gate_run_id}"
fi
echo "PROMOTE_CYCLE_DISPATCHED doc=${dispatched_doc} baseline=${cycle_baseline_sha} smoke=${smoke_sha} pinned=${pin_mode}"
# The proving run now carries the cycle: keep the freeze tag until the poller
# releases it on a terminal promotion state.
trap - EXIT
PIN_TAG_CREATED=false
emit_output outcome dispatched
emit_output doc "${dispatched_doc}"
emit_output baseline_sha "${cycle_baseline_sha}"
emit_output pinned "${pin_mode}"
emit_output candidate_sha "${pin_candidate_sha}"
