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
#                                                           and still holds (see the bounded hold)
#   PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=<n> reason=<label> latch_age_secs=<n> max_hours=<n>
#                                                           not an exit: a tracking issue parked on
#                                                           a human latch label for at least
#                                                           PROMOTE_CYCLE_HOLD_MAX_HOURS no longer
#                                                           holds the cycle (WARNING alert sent)
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
#   PROMOTE_CYCLE_PIN outcome=skipped reason=<disabled|released_cycle_holds_pin|wrappers_not_pinned|support_not_pinned|branch_missing|branch_unprotected|branch_lookup_failed|freeze_failed>
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
#   PROMOTE_CYCLE_HOLD_RELEASE_ENABLED  default true; `false` (anything not truthy)
#                                       makes every labelled tracking issue hold, as
#                                       before the bounded hold
#   PROMOTE_CYCLE_HOLD_MAX_HOURS        default 24; a tracking issue whose newest
#                                       human latch label (PROMOTE_CYCLE_HOLD_LATCH_LABELS)
#                                       is at least this old stops holding the cycle.
#                                       A non-positive-integer value falls back to 24
#                                       with a warning.
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID / TG_CHAT_ID, ALERT_MSG_LEVEL
#                                       Telegram settings for the WARNING sent when a
#                                       hold is released (best effort, via tg_helpers.sh)
#                                       and when a release-blocker report is not delivered
#   RELEASE_BLOCKER_HEAL_ENABLED        default true (kill switch for the
#                                       release-blocker reporters below)
#   RELEASE_BLOCKER_CYCLE_SKIP_STREAK   default 3; consecutive scheduled ticks
#                                       (this one included) that skipped gate_busy
#                                       or smoke_gate_cancelled before
#                                       cycle_gate_unavailable is reported (1-10;
#                                       any other value warns and uses 3)
#   RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS default 86400
#   RELEASE_BLOCKER_CYCLE_STALE_SECS    default 259200
#                                       (a non-integer threshold warns and
#                                       falls back to its default)
#   RELEASE_BLOCKER_HEAL_PY             default scripts/workflow_failure_heal.py
#
# Release-blocker reporters (stuck-states P4 / release-blocker Phase 2b,
# Refs #8291). The cycle-side twin of the Phase 2a reporters in
# auto_release_stable.sh. Inside skip_cycle, before the PROMOTE_CYCLE_SKIPPED
# line and without changing it, GITHUB_OUTPUT or the exit code, a tick sends
# at most one `release_blocker` heal report (workflow_failure_heal.py
# build-blocker-payload + request-report-identity + wrap-dispatch, then one
# `POST repos/<self>/dispatches`, head_branch=main, no run_refs) when:
#   cycle_gate_unavailable       this tick skipped gate_busy or
#                                smoke_gate_cancelled and so did the previous
#                                RELEASE_BLOCKER_CYCLE_SKIP_STREAK-1 completed
#                                scheduled ticks (read from their `cycle` job
#                                logs; only when every one of them concluded
#                                success)
#   cycle_held_untrusted_marker  this tick skipped cycle_in_flight and a held
#                                tracking issue (first 5 checked) carries the
#                                poller's trusted "Untrusted apply-analysis
#                                marker" alert, posted at least
#                                RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS ago, with no
#                                trusted baseline marker after it
#   cycle_stale                  neither of the above reported, main is ahead of
#                                the stable tag with code changes, the skip is
#                                not disabled / insufficient_docs /
#                                cycle_in_flight / another_run_active, and the
#                                later of the last cycle's baseline marker and
#                                the oldest unreleased commit is at least
#                                RELEASE_BLOCKER_CYCLE_STALE_SECS old
# Log lines:
#   RELEASE_BLOCKER_REPORTED reason=<r> age=<n> branch=<b> sha=<tip|none> run_ref=none
#   RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled|payload_invalid|dispatch_denied|guard_unavailable blocker=<r> ...
# payload_invalid and dispatch_denied also send a Telegram WARNING. A reporter
# failure never fails or changes the tick. Failed cycle runs (fail_cycle) are
# not reported here: the intake already receives them through workflow_run.
#
# Bounded hold (stuck-states P4, Refs #8291): guard 2 reads the labels of the
# open tracking issues it already lists. An issue without a latch label holds
# the cycle as before. For issues carrying one of ai:harness-broken,
# ai:validation-failed, ai:security-pass-failed, ai:blocked or ai:needs-human,
# one batched GraphQL read fetches their LabeledEvent history; the newest event
# of any present latch label is the latch start. At least
# PROMOTE_CYCLE_HOLD_MAX_HOURS old: the issue is released (logged, WARNING
# alert) and no longer holds. A failed, unverified, incomplete or unparseable
# history keeps the issue holding (fail closed). While the promote-cycle-frozen
# tag exists (and PROMOTE_CYCLE_PIN_ENABLED is not false) a released issue
# still holds: the follower keeps promote-candidate, which every internal-*.yml
# wrapper calls, frozen on the parked cycle's candidate, so a new cycle would
# prove old code (skip detail tag=promote-cycle-frozen, no release alert; the
# follower deletes the tag after PROMOTE_CYCLE_FREEZE_MAX_SECS). A tick that
# released an issue runs unpinned (PROMOTE_CYCLE_PIN reason=released_cycle_holds_pin): the
# released cycle may still own the promote-cycle-frozen tag and its poller
# deletes that tag without an ownership check when it ends, so a new pinned
# cycle sharing the tag would lose its freeze.
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
if [ -r "${SCRIPT_DIR}/tg_helpers.sh" ]; then
	# shellcheck source=tg_helpers.sh
	# shellcheck disable=SC1091
	source "${SCRIPT_DIR}/tg_helpers.sh"
fi

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
PROMOTE_CYCLE_HOLD_RELEASE_ENABLED="${PROMOTE_CYCLE_HOLD_RELEASE_ENABLED:-true}"
PROMOTE_CYCLE_HOLD_MAX_HOURS="${PROMOTE_CYCLE_HOLD_MAX_HOURS:-24}"
# A bad optional variable must not stop the nightly cycle: warn and use 24.
# No leading zero (bash arithmetic would read it as octal) and at most 6
# digits, so the hours-to-seconds product cannot overflow.
if ! [[ "${PROMOTE_CYCLE_HOLD_MAX_HOURS}" =~ ^[1-9][0-9]{0,5}$ ]]; then
	echo "::warning::PROMOTE_CYCLE_HOLD_MAX_HOURS must be a positive integer; using 24."
	PROMOTE_CYCLE_HOLD_MAX_HOURS=24
fi
# Release-blocker reporters (Phase 2b, Refs #8291). Like the hold settings, a
# bad value warns and falls back: a reporter must never stop the cycle.
RELEASE_BLOCKER_HEAL_ENABLED="${RELEASE_BLOCKER_HEAL_ENABLED:-true}"
RELEASE_BLOCKER_CYCLE_SKIP_STREAK="${RELEASE_BLOCKER_CYCLE_SKIP_STREAK:-3}"
RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS="${RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS:-86400}"
RELEASE_BLOCKER_CYCLE_STALE_SECS="${RELEASE_BLOCKER_CYCLE_STALE_SECS:-259200}"
RELEASE_BLOCKER_HEAL_PY="${RELEASE_BLOCKER_HEAL_PY:-${SCRIPT_DIR}/workflow_failure_heal.py}"
# The cap of 10 bounds the job and log reads of the streak check.
if ! [[ "${RELEASE_BLOCKER_CYCLE_SKIP_STREAK}" =~ ^([1-9]|10)$ ]]; then
	release_blocker_streak_shown="$(printf '%s' "${RELEASE_BLOCKER_CYCLE_SKIP_STREAK}" | tr -cd 'A-Za-z0-9_.:+-')"
	echo "::warning::RELEASE_BLOCKER_CYCLE_SKIP_STREAK must be an integer from 1 to 10 (got '${release_blocker_streak_shown:0:40}'); using 3."
	RELEASE_BLOCKER_CYCLE_SKIP_STREAK=3
fi
for release_blocker_threshold_spec in RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS:86400 RELEASE_BLOCKER_CYCLE_STALE_SECS:259200; do
	release_blocker_threshold_name="${release_blocker_threshold_spec%%:*}"
	release_blocker_threshold_default="${release_blocker_threshold_spec#*:}"
	if ! [[ "${!release_blocker_threshold_name}" =~ ^[0-9]{1,10}$ ]]; then
		release_blocker_threshold_shown="$(printf '%s' "${!release_blocker_threshold_name}" | tr -cd 'A-Za-z0-9_.:+-')"
		echo "::warning::${release_blocker_threshold_name} must be a non-negative integer (got '${release_blocker_threshold_shown:0:40}'); using ${release_blocker_threshold_default}."
		printf -v "${release_blocker_threshold_name}" '%s' "${release_blocker_threshold_default}"
	else
		# Canonical decimal: jq --argjson rejects leading zeros (0086400).
		printf -v "${release_blocker_threshold_name}" '%d' "$((10#${!release_blocker_threshold_name}))"
	fi
done
# Human latch labels (all in .github/ai/label_contract.v1.json). A fixed list,
# not a repo variable, so configuration cannot widen what gets released.
PROMOTE_CYCLE_HOLD_LATCH_LABELS=(ai:harness-broken ai:validation-failed ai:security-pass-failed ai:blocked ai:needs-human)
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
	# Release-blocker reporters (Phase 2b): before the skip line, never fatal.
	release_blocker_on_skip "${reason}" "${detail}" || true
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
# CODE_CHANGES_OLDEST_COMMIT_AT gets the committer date of the first commit
# in the compare payload (the oldest one GitHub returned) on an `ahead`
# status, else "" (read by the cycle_stale reporter).
CODE_CHANGES_OUT=""
CODE_CHANGES_OLDEST_COMMIT_AT=""
code_changes_between()
{
	local base="$1"
	local head="$2"
	local payload
	CODE_CHANGES_OUT=""
	CODE_CHANGES_STATUS=""
	CODE_CHANGES_OLDEST_COMMIT_AT=""
	if ! payload="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/compare/${base}...${head}")"; then
		return 2
	fi
	local status total_commits file_count
	status="$(printf '%s' "${payload}" | jq -r '.status // empty')"
	total_commits="$(printf '%s' "${payload}" | jq -r '.total_commits // 0')"
	file_count="$(printf '%s' "${payload}" | jq -r '(.files // []) | length')"
	CODE_CHANGES_STATUS="${status}"
	case "${status}" in
		ahead)
			CODE_CHANGES_OLDEST_COMMIT_AT="$(printf '%s' "${payload}" | jq -r '(.commits // [])[0]? | (.commit.committer.date // .commit.author.date // empty) | tostring' 2>/dev/null || true)"
			;;
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

# last_cycle_baseline_sha [with_time]: the baseline SHA recorded by the most
# recent cycle (any outcome), or empty. Exit 2 when the search failed. With
# `with_time` it prints "<sha> <created_at>" (the marker comment's creation
# time, possibly empty) for the cycle_stale reporter; without it the output
# is the bare SHA.
# The search matches any comment body, so each candidate issue's comments
# are re-read and only a marker posted by github-actions[bot] or a trusted
# author_association counts; a stray comment cannot fake a completed cycle.
last_cycle_baseline_sha()
{
	local query numbers issue_number sha
	local with_time="false"
	if [ "${1:-}" = "with_time" ]; then
		with_time="true"
	fi
	query="repo:${GITHUB_REPOSITORY} is:issue label:ai:orchestrator-tracking in:comments \"${CYCLE_BASELINE_MARKER}:\""
	if ! numbers="$(gh_retry gh api -X GET search/issues -f "q=${query}" -f sort=created -f order=desc -f per_page=10 2>/dev/null | jq -r '.items[]?.number | select(. != null)')"; then
		return 2
	fi
	[ -n "${numbers}" ] || return 0
	while IFS= read -r issue_number; do
		[[ "${issue_number}" =~ ^[0-9]+$ ]] || continue
		if ! sha="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_number}/comments?per_page=100" \
			| jq -r --arg m "${CYCLE_BASELINE_MARKER}" --arg trusted "${COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS}" --arg with_time "${with_time}" '
				($trusted | split(",") | map(ascii_upcase | gsub("^\\s+|\\s+$"; ""))) as $ok
				| [.[]? | select((.user.login // "") == "github-actions[bot]" or (((.author_association // "") | ascii_upcase) as $a | $ok | index($a)) != null)
					| ((.created_at // "") | tostring) as $at
					| .body // "" | capture("(?m)^" + $m + ": (?<sha>[0-9a-f]{40})")? | {sha: .sha, at: $at}] | last // empty
				| if $with_time == "true" then "\(.sha) \(.at | gsub("[^0-9A-Za-z:.+-]"; ""))" else .sha end')"; then
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
	local branch_sha rc saved_out saved_status
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
	# code_changes_between overwrites the tag-compare globals the caller logs.
	saved_out="${CODE_CHANGES_OUT}"
	saved_status="${CODE_CHANGES_STATUS:-}"
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
	CODE_CHANGES_OUT="${saved_out}"
	CODE_CHANGES_STATUS="${saved_status}"
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
# Set by promote_cycle_hold_guard when it released a parked tracking issue.
PROMOTE_CYCLE_HOLD_RELEASED_ISSUES=""
# Set once a hold-release alert timed out; later alerts this tick are skipped
# so the waits cannot add up against the job cap.
PROMOTE_CYCLE_HOLD_ALERT_TIMED_OUT=false

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
	# A released parked cycle may still own the shared freeze tag, and its
	# poller deletes the tag when it ends; never share the tag with it.
	if [ -n "${PROMOTE_CYCLE_HOLD_RELEASED_ISSUES}" ]; then
		PIN_PREFLIGHT_REASON=released_cycle_holds_pin
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
	# tag that still exists is stale. (A tick that released a parked issue
	# never reaches here: pin_preflight runs it unpinned.)
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

# --- Bounded hold (stuck-states P4) -------------------------------------------

# promote_cycle_hold_alert <text>: best-effort WARNING; never fails the cycle.
# tg_send_msg's curl has no time limit, so the send runs in the background and
# is abandoned after 30 seconds.
promote_cycle_hold_alert()
{
	local pid waited=0 limit=150
	if [ "${PROMOTE_CYCLE_HOLD_ALERT_TIMED_OUT}" = "true" ]; then
		echo "::warning::promote_main_cycle: an earlier hold-release alert timed out; this one is not sent" >&2
		return 0
	fi
	if type tg_send_msg >/dev/null 2>&1; then
		( tg_send_msg "$1" WARNING >/dev/null 2>&1 || true ) &
		pid=$!
		# 150 polls of 0.2 s = 30 s.
		while kill -0 "${pid}" 2>/dev/null && [ "${waited}" -lt "${limit}" ]; do
			sleep 0.2
			waited=$((waited + 1))
		done
		if kill -0 "${pid}" 2>/dev/null; then
			pkill -P "${pid}" 2>/dev/null || true
			kill "${pid}" 2>/dev/null || true
			PROMOTE_CYCLE_HOLD_ALERT_TIMED_OUT=true
			echo "::warning::promote_main_cycle: hold-release alert timed out after 30s; not sent" >&2
		fi
		wait "${pid}" 2>/dev/null || true
	else
		echo "::warning::promote_main_cycle: tg_helpers.sh not available; hold-release alert not sent" >&2
	fi
	return 0
}

# promote_cycle_hold_history <owner> <name> <issue_number>...
# Batching contract (§14):
#   input   up to 100 issue numbers (guard 2's listing is per_page=100), each
#           matching ^[1-9][0-9]*$; owner and name matching ^[A-Za-z0-9_.-]+$.
#   output  the raw GraphQL response on stdout; one alias i<n> per issue with
#           its number and its last 100 LABELED_EVENT timeline items.
#   calls   exactly 1 (gh api graphql through gh_retry).
#   failure non-zero exit; the caller keeps every candidate holding (fail
#           closed), never a per-issue fallback loop.
# Audited calls: guard 2's REST issues listing carries current labels only, no
# label-event times, and no other call in this script returns timeline data.
promote_cycle_hold_history()
{
	local owner="$1"
	local name="$2"
	shift 2
	local aliases="" issue_number count=0 query response err_file err_line
	if ! [[ "${owner}" =~ ^[A-Za-z0-9_.-]+$ ]] || ! [[ "${name}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
		return 1
	fi
	for issue_number in "$@"; do
		[[ "${issue_number}" =~ ^[1-9][0-9]*$ ]] || return 1
		count=$((count + 1))
		aliases+=" i${issue_number}:issue(number:${issue_number}){number timelineItems(itemTypes:[LABELED_EVENT],last:100){pageInfo{hasPreviousPage} nodes{... on LabeledEvent{createdAt label{name}}}}}"
	done
	if [ "${count}" -lt 1 ] || [ "${count}" -gt 100 ]; then
		return 1
	fi
	query="query(\$owner:String!,\$name:String!){repository(owner:\$owner,name:\$name){${aliases} }}"
	err_file="$(mktemp)" || return 1
	if ! response="$(gh_retry gh api graphql -f query="${query}" -f owner="${owner}" -f name="${name}" 2>"${err_file}")"; then
		# One short, single-line reason (auth, rate limit, query error): the last
		# stderr line, without a leading workflow-command prefix.
		err_line="$(grep -v '^[[:space:]]*$' "${err_file}" 2>/dev/null | tail -n1 | sed 's/^::[a-z]*:://' | tr -d '\r' | tr -c '[:print:]' ' ' | cut -c1-120 | tr -s ':' | sed 's/::/: :/g' || true)"
		rm -f "${err_file}"
		echo "::warning::Promote cycle hold: GraphQL label history read failed: ${err_line:-no error output}" >&2
		return 1
	fi
	rm -f "${err_file}"
	if ! printf '%s' "${response}" | jq -e 'type == "object" and ((.data.repository? // null) | type) == "object"' >/dev/null 2>&1; then
		return 1
	fi
	printf '%s\n' "${response}"
}

# promote_cycle_hold_decide <listing_json> <history_json|""> <now_epoch> <cap_secs>
# Prints one line per listed issue, in listing order:
#   hold <n>                       no latch label: holds as before
#   release <n> <label> <age_secs> newest latch at least cap_secs old
#   keep <n> <reason>              latch_recent, or history_unverified /
#                                  history_incomplete / history_unparseable
# <label> is the latch label whose newest event sets the age: with several
# latch labels the newest event sets the age (D2); the age never falls back to
# the issue's creation time. The history goes to jq through a file, not argv,
# so a large response cannot hit the per-argument limit (MAX_ARG_STRLEN).
promote_cycle_hold_decide()
{
	local listing="$1"
	local history="${2:-}"
	local now="$3"
	local cap="$4"
	local latch_json hist_file rc=0
	[ -n "${history}" ] || history="null"
	latch_json="$(printf '%s\n' "${PROMOTE_CYCLE_HOLD_LATCH_LABELS[@]}" | jq -R . | jq -sc .)"
	hist_file="$(mktemp)" || return 1
	printf '%s' "${history}" > "${hist_file}"
	printf '%s' "${listing}" | jq -r \
		--argjson L "${latch_json}" \
		--slurpfile hist "${hist_file}" \
		--argjson now "${now}" \
		--argjson cap "${cap}" '
		(($hist[0]? // null) | (try .data.repository catch null) | if type == "object" then . else {} end) as $repo
		| .[]
		| select((.number | type) == "number")
		| . as $issue
		| ($issue.number) as $n
		| [$L[] as $l | select(any(($issue.labels // [])[]; . == $l)) | $l] as $present
		| if ($present | length) == 0 then "hold \($n)"
		  else
			($repo["i\($n)"]) as $node
			| if ($node | type) != "object" or ($node.number != $n) then "keep \($n) history_unverified"
			  else
				[(((($node.timelineItems // {}) | if type == "object" then . else {} end).nodes) // [])[]? | select(type == "object")] as $events
				| [$present[] as $l | [$events[] | select(((.label // {}) | if type == "object" then (.name // "") else "" end) == $l) | .createdAt]] as $per
				| if any($per[]; length == 0) then "keep \($n) history_incomplete"
				  else
					[$per[][] | (try (if type == "string" then fromdateiso8601 else null end) catch null)] as $times
					| if any($times[]; . == null) then "keep \($n) history_unparseable"
					  else
						([range(0; $present | length) as $k | {l: $present[$k], t: ([$per[$k][] | fromdateiso8601] | max)}] | max_by(.t)) as $newest
						| (($now - $newest.t) | floor) as $age
						| if $age >= $cap then "release \($n) \($newest.l) \($age)"
						  else "keep \($n) latch_recent" end
					  end
				  end
			  end
		  end' || rc=$?
	rm -f "${hist_file}"
	return "${rc}"
}

# promote_cycle_hold_guard <listing_json> <in_flight_csv>
# Guard 2 when tracking issues are open. Exits through skip_cycle while any
# issue still holds; returns 0 when every issue was released. Runs in the
# main shell so skip_cycle really exits. Disabled, or with no latch label on
# any listed issue, it behaves exactly as before (no extra API call).
promote_cycle_hold_guard()
{
	local listing="$1"
	local in_flight_csv="$2"
	local latch_json candidates history_json="" decisions now cap_secs remaining=""
	local action issue_number detail age hours listed_count decided_count=0 tag_err_file
	local -a candidate_list=()
	if ! is_truthy "${PROMOTE_CYCLE_HOLD_RELEASE_ENABLED}"; then
		skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
	fi
	latch_json="$(printf '%s\n' "${PROMOTE_CYCLE_HOLD_LATCH_LABELS[@]}" | jq -R . | jq -sc .)"
	candidates="$(printf '%s' "${listing}" | jq -r --argjson L "${latch_json}" '.[] | . as $i | select(any($L[]; . as $l | any(($i.labels // [])[]; . == $l))) | .number' 2>/dev/null || true)"
	if [ -z "${candidates}" ]; then
		skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
	fi
	mapfile -t candidate_list <<< "${candidates}"
	if ! history_json="$(promote_cycle_hold_history "${GITHUB_REPOSITORY%%/*}" "${GITHUB_REPOSITORY#*/}" "${candidate_list[@]}")"; then
		echo "::warning::Promote cycle hold: label history unavailable; keeping tracking issues $(printf '%s\n' "${candidate_list[@]}" | paste -sd, -) holding."
		history_json=""
	fi
	now="$(date -u +%s)"
	cap_secs=$((PROMOTE_CYCLE_HOLD_MAX_HOURS * 3600))
	if ! decisions="$(promote_cycle_hold_decide "${listing}" "${history_json}" "${now}" "${cap_secs}" 2>/dev/null)"; then
		echo "::warning::Promote cycle hold: could not evaluate the latch ages; keeping every tracking issue holding."
		skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
	fi
	listed_count="$(printf '%s' "${listing}" | jq -r 'length' 2>/dev/null || echo "")"
	# While the freeze tag exists the follower keeps promote-candidate (which
	# every internal-*.yml wrapper calls) on the parked cycle's candidate, so a
	# new cycle would run old pipeline code: keep holding until the tag is gone.
	# With pinning off the follower ignores the tag. 1 read, only on a release.
	if printf '%s\n' "${decisions}" | grep -q '^release ' \
		&& [ "$(printf '%s' "${PROMOTE_CYCLE_PIN_ENABLED}" | tr '[:upper:]' '[:lower:]')" != "false" ]; then
		tag_err_file="$(mktemp)"
		if gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${PROMOTE_CYCLE_PIN_TAG}" >/dev/null 2>"${tag_err_file}"; then
			rm -f "${tag_err_file}"
			echo "::warning::Promote cycle hold: a tracking issue is past the latch cap, but the ${PROMOTE_CYCLE_PIN_TAG} tag still freezes ${PROMOTE_CYCLE_PIN_BRANCH}; keeping every tracking issue holding."
			skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv} tag=${PROMOTE_CYCLE_PIN_TAG}"
		fi
		if ! is_http_404 "${tag_err_file}"; then
			rm -f "${tag_err_file}"
			skip_cycle guard_unavailable "lookup=tag:${PROMOTE_CYCLE_PIN_TAG}"
		fi
		rm -f "${tag_err_file}"
	fi
	while IFS=' ' read -r action issue_number detail age; do
		[ -n "${action}" ] || continue
		decided_count=$((decided_count + 1))
		if ! [[ "${issue_number}" =~ ^[1-9][0-9]*$ ]]; then
			skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
		fi
		case "${action}" in
			hold)
				remaining+="${issue_number},"
				;;
			keep)
				remaining+="${issue_number},"
				case "${detail}" in
					history_*)
						echo "::warning::Promote cycle hold: tracking issue #${issue_number} keeps holding (${detail}); its latch age could not be proven."
						;;
				esac
				;;
			release)
				if ! [[ "${age}" =~ ^[0-9]+$ ]]; then
					skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
				fi
				hours=$((age / 3600))
				PROMOTE_CYCLE_HOLD_RELEASED_ISSUES+="${issue_number},"
				echo "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=${issue_number} reason=${detail} latch_age_secs=${age} max_hours=${PROMOTE_CYCLE_HOLD_MAX_HOURS}"
				echo "::warning::Promote cycle hold released: tracking issue #${issue_number} has been parked on ${detail} for ${hours}h (cap ${PROMOTE_CYCLE_HOLD_MAX_HOURS}h); the cycle no longer waits for it."
				promote_cycle_hold_alert "Promote cycle: tracking issue #${issue_number} in ${GITHUB_REPOSITORY} has been parked on ${detail} for ${hours}h (cap ${PROMOTE_CYCLE_HOLD_MAX_HOURS}h); the daily cycle no longer waits for it."
				;;
			*)
				skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
				;;
		esac
	done <<< "${decisions}"
	# Every listed issue must get exactly one decision; otherwise fail closed.
	if [ "${decided_count}" != "${listed_count}" ]; then
		echo "::warning::Promote cycle hold: ${decided_count} decision(s) for ${listed_count:-unknown} tracking issue(s); keeping every tracking issue holding."
		skip_cycle cycle_in_flight "tracking_issues=${in_flight_csv}"
	fi
	remaining="${remaining%,}"
	if [ -n "${remaining}" ]; then
		skip_cycle cycle_in_flight "tracking_issues=${remaining}"
	fi
	return 0
}

# --- Release-blocker reporters (stuck-states P4 / Phase 2b, Refs #8291) -------
# The Phase 2a reporter shape from auto_release_stable.sh, for the cycle side.
# Every function below is called as `<fn> || true` (the entry point
# release_blocker_on_skip runs from skip_cycle that way), so errexit is off
# inside them: each step is checked explicitly and none can fail or change the
# tick. No EXIT trap is set here: pin_release_freeze owns the EXIT trap, so
# the work dir is removed by release_blocker_cleanup instead.
release_blocker_fired="false"
release_blocker_on_skip_done="false"
# Set by guard 3 once main is ahead of the stable tag with code changes.
release_blocker_main_ahead="false"
release_blocker_oldest_commit_at=""
release_blocker_commits_truncated="false"
# Creation time of the last cycle's trusted baseline marker (guard 4).
release_blocker_baseline_at=""
# Set once guard 4's marker search succeeded; cycle_stale needs it so a skip
# before that read never falls back to the oldest-commit clock alone.
release_blocker_baseline_read="false"
RELEASE_BLOCKER_WORK_DIR=""

# Print an API-derived value reduced to safe characters, at most 200 bytes.
release_blocker_safe()
{
	local release_blocker_safe_value
	release_blocker_safe_value="$(printf '%s' "${1:-}" | tr -cd 'A-Za-z0-9_.:/@+=,-')"
	printf '%s' "${release_blocker_safe_value:0:200}"
}

# Print the epoch seconds of an ISO-8601 UTC timestamp; nothing when it is
# missing or unparseable.
release_blocker_epoch_from_iso()
{
	local release_blocker_iso="${1:-}"
	[ -n "${release_blocker_iso}" ] || return 0
	jq -rn --arg ts "${release_blocker_iso}" '$ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? | floor' 2>/dev/null || true
}

# Print floor(now - <iso>) clamped at 0; nothing for a missing or unparseable
# timestamp (callers treat empty as "age unknown").
release_blocker_age_from_iso()
{
	local release_blocker_iso="${1:-}"
	[ -n "${release_blocker_iso}" ] || return 0
	jq -rn --arg ts "${release_blocker_iso}" '($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601?) as $t | [((now - $t) | floor), 0] | max' 2>/dev/null || true
}

# Print the Actions URL of a run in this repository, or `none` when the id is
# not numeric (URLs are rebuilt from ids, never copied from html_url).
release_blocker_run_url()
{
	local release_blocker_url_id="${1:-}"
	if [[ "${release_blocker_url_id}" =~ ^[0-9]{1,20}$ ]]; then
		printf '%s/%s/actions/runs/%s' "$(release_blocker_safe "${GITHUB_SERVER_URL:-https://github.com}")" "${GITHUB_REPOSITORY}" "${release_blocker_url_id}"
	else
		printf 'none'
	fi
}

release_blocker_ensure_work_dir()
{
	if [ -z "${RELEASE_BLOCKER_WORK_DIR}" ]; then
		RELEASE_BLOCKER_WORK_DIR="$(mktemp -d 2>/dev/null)" || RELEASE_BLOCKER_WORK_DIR=""
	fi
	[ -n "${RELEASE_BLOCKER_WORK_DIR}" ] && [ -d "${RELEASE_BLOCKER_WORK_DIR}" ]
}

# Removes the evidence, log, payload, OIDC token and envelope files.
release_blocker_cleanup()
{
	if [ -n "${RELEASE_BLOCKER_WORK_DIR}" ]; then
		rm -rf "${RELEASE_BLOCKER_WORK_DIR}"
	fi
	RELEASE_BLOCKER_WORK_DIR=""
	return 0
}

# release_blocker_evidence_header <reason> <age_secs> <limit_text> <tip>
# The fixed first line keeps the intake fingerprint stable across ticks.
release_blocker_evidence_header()
{
	local release_blocker_header_reason="$1" release_blocker_header_age="$2" release_blocker_header_limit="$3" release_blocker_header_tip="${4:-}"
	[[ "${release_blocker_header_tip}" =~ ^[0-9a-f]{40}$ ]] || release_blocker_header_tip="none"
	printf 'reason=%s\n' "${release_blocker_header_reason}"
	printf 'branch=%s tip=%s tag=%s tag_commit=%s\n' "$(release_blocker_safe "${PROMOTE_CYCLE_DEFAULT_BRANCH}")" "${release_blocker_header_tip}" "$(release_blocker_safe "${PROMOTE_CYCLE_STABLE_TAG}")" "$(release_blocker_safe "${tag_commit:-none}")"
	printf 'age_secs=%s %s\n' "${release_blocker_header_age}" "${release_blocker_header_limit}"
}

release_blocker_alert()
{
	local release_blocker_alert_skip="$1" release_blocker_alert_reason="$2"
	local release_blocker_alert_pid release_blocker_alert_waited=0
	if ! type tg_send_msg >/dev/null 2>&1; then
		echo "::warning::promote_main_cycle: tg_helpers.sh not available; release-blocker alert not sent" >&2
		return 0
	fi
	# tg_send_msg's curl has no time limit, so the send runs in the background
	# and is abandoned after 30 seconds (150 polls of 0.2 s).
	( tg_send_msg "Release blocker report could not be dispatched (${release_blocker_alert_skip}): ${release_blocker_alert_reason} on $(release_blocker_safe "${PROMOTE_CYCLE_DEFAULT_BRANCH}") in ${GITHUB_REPOSITORY}. Refs #8291" WARNING >/dev/null 2>&1 || true ) &
	release_blocker_alert_pid=$!
	while kill -0 "${release_blocker_alert_pid}" 2>/dev/null && [ "${release_blocker_alert_waited}" -lt 150 ]; do
		sleep 0.2
		release_blocker_alert_waited=$((release_blocker_alert_waited + 1))
	done
	if kill -0 "${release_blocker_alert_pid}" 2>/dev/null; then
		pkill -P "${release_blocker_alert_pid}" 2>/dev/null || true
		kill "${release_blocker_alert_pid}" 2>/dev/null || true
		echo "::warning::Release blocker Telegram alert timed out after 30s; not sent."
	fi
	wait "${release_blocker_alert_pid}" 2>/dev/null || true
	return 0
}

# report_release_blocker <reason> <age_secs> <evidence_file> <head_sha>
# Build, sign, envelope and send one release_blocker heal report. No run_refs:
# the cycle reasons name no failed run (the intake drops claimed runs that did
# not fail). Returns 0.
report_release_blocker()
{
	local rb_reason="$1" rb_age="$2" rb_evidence="$3" rb_head_sha="${4:-}"
	local rb_branch="" rb_payload="" rb_identity="" rb_envelope="" rb_error="" rb_identity_status="" rb_detail="" rb_sha_shown="none"
	rb_branch="$(release_blocker_safe "${PROMOTE_CYCLE_DEFAULT_BRANCH}")"
	release_blocker_fired="true"
	if ! is_truthy "${RELEASE_BLOCKER_HEAL_ENABLED}"; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled blocker=${rb_reason}"
		return 0
	fi
	if ! release_blocker_ensure_work_dir; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=${rb_reason} stage=build detail=work_dir_unavailable"
		release_blocker_alert payload_invalid "${rb_reason}"
		return 0
	fi
	rb_payload="${RELEASE_BLOCKER_WORK_DIR}/payload.json"
	rb_identity="${RELEASE_BLOCKER_WORK_DIR}/report_identity.jwt"
	rb_envelope="${RELEASE_BLOCKER_WORK_DIR}/dispatch.json"
	rb_error="${RELEASE_BLOCKER_WORK_DIR}/error.txt"
	local -a rb_build_args=(
		build-blocker-payload
		--repo "${GITHUB_REPOSITORY}"
		--workflow-name "${GITHUB_WORKFLOW:-Promote main to stable}"
		--reason "${rb_reason}"
		--evidence-file "${rb_evidence}"
		--age-secs "${rb_age}"
		--head-branch "${PROMOTE_CYCLE_DEFAULT_BRANCH}"
	)
	if [[ "${rb_head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		rb_build_args+=(--head-sha "${rb_head_sha}")
		rb_sha_shown="${rb_head_sha}"
	fi
	if [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]{1,20}$ ]]; then
		rb_build_args+=(
			--reporter-run-id "${GITHUB_RUN_ID}"
			--reporter-run-url "$(release_blocker_run_url "${GITHUB_RUN_ID}")"
		)
		if [[ "${GITHUB_RUN_ATTEMPT:-1}" =~ ^[0-9]{1,10}$ ]]; then
			rb_build_args+=(--reporter-run-attempt "${GITHUB_RUN_ATTEMPT:-1}")
		fi
	fi
	if ! PYTHONDONTWRITEBYTECODE=1 python3 "${RELEASE_BLOCKER_HEAL_PY}" "${rb_build_args[@]}" > "${rb_payload}" 2> "${rb_error}"; then
		rb_detail="$(release_blocker_safe "$(head -c 200 "${rb_error}" 2>/dev/null | tr '\n' ' ' | tr ' ' '_')")"
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=${rb_reason} stage=build detail=${rb_detail:-none}"
		release_blocker_alert payload_invalid "${rb_reason}"
		return 0
	fi
	local -a rb_wrap_args=(wrap-dispatch --payload-json "${rb_payload}")
	if [ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
		rb_identity_status="$(PYTHONDONTWRITEBYTECODE=1 python3 "${RELEASE_BLOCKER_HEAL_PY}" request-report-identity --out "${rb_identity}" 2>/dev/null || echo "identity=absent reason=helper_failed")"
		rb_identity_status="$(printf '%s' "${rb_identity_status}" | head -1 | tr -cd 'A-Za-z0-9_=. -')"
		echo "Release blocker report identity: ${rb_identity_status:-identity=absent reason=unknown} blocker=${rb_reason}"
		if [ -s "${rb_identity}" ]; then
			rb_wrap_args+=(--report-identity-file "${rb_identity}")
		fi
	else
		echo "Release blocker report identity: identity=absent reason=oidc_unavailable blocker=${rb_reason}"
	fi
	if ! PYTHONDONTWRITEBYTECODE=1 python3 "${RELEASE_BLOCKER_HEAL_PY}" "${rb_wrap_args[@]}" > "${rb_envelope}" 2> "${rb_error}"; then
		rm -f "${rb_identity}"
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=${rb_reason} stage=wrap"
		release_blocker_alert payload_invalid "${rb_reason}"
		return 0
	fi
	rm -f "${rb_identity}"
	if ! gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/dispatches" --input "${rb_envelope}" >/dev/null 2> "${rb_error}"; then
		rm -f "${rb_envelope}"
		rb_detail="$(release_blocker_safe "$(head -c 200 "${rb_error}" 2>/dev/null | tr '\n' ' ' | tr ' ' '_')")"
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=dispatch_denied blocker=${rb_reason} detail=${rb_detail:-none}"
		echo "::warning::Release blocker report ${rb_reason} on ${rb_branch} could not be dispatched to ${GITHUB_REPOSITORY}."
		release_blocker_alert dispatch_denied "${rb_reason}"
		return 0
	fi
	rm -f "${rb_envelope}"
	echo "RELEASE_BLOCKER_REPORTED reason=${rb_reason} age=${rb_age} branch=${rb_branch} sha=${rb_sha_shown} run_ref=none"
	return 0
}

# release_blocker_check_skip_streak <current_reason> <current_detail>
# cycle_gate_unavailable: this tick and the previous
# RELEASE_BLOCKER_CYCLE_SKIP_STREAK-1 completed scheduled ticks all skipped
# gate_busy or smoke_gate_cancelled.
# API calls (§14): the runs come from guard 1's self-run listing. The skip
# reason of an earlier tick is in no listing this script reads (run objects
# carry only status and conclusion, a gate_busy tick leaves no gate run, and
# job summaries are not exposed by the REST API), so per earlier tick: 1 GET
# actions/runs/<id>/jobs and 1 GET actions/jobs/<id>/logs, at most 2x9 calls
# and none unless every earlier tick concluded success.
release_blocker_check_skip_streak()
{
	local rb_current="$1" rb_current_detail="${2:-}"
	local rb_needed=$((RELEASE_BLOCKER_CYCLE_SKIP_STREAK - 1))
	local rb_runs="" rb_count=0 rb_id="" rb_created="" rb_conclusion="" rb_jobs="" rb_job_id="" rb_log_file="" rb_matched=""
	local rb_oldest="" rb_lines="" rb_age="" rb_evidence=""
	if [ "${rb_needed}" -gt 0 ]; then
		rb_runs="$(printf '%s' "${self_runs_json:-}" | jq -r --arg me "${GITHUB_RUN_ID}" --argjson n "${rb_needed}" '
			[.workflow_runs[]? | select(.event == "schedule" and ((.id // "") | tostring) != $me and .status == "completed")]
			| sort_by([(.run_number // 0), ((.created_at // "") | tostring)]) | reverse | .[:$n][]
			| [((.id // "") | tostring), ((.created_at // "") | tostring), ((.conclusion // "") | tostring)] | @tsv' 2>/dev/null)" || rb_runs=""
		rb_count="$(printf '%s' "${rb_runs}" | grep -c . || true)"
		[[ "${rb_count}" =~ ^[0-9]+$ ]] || rb_count=0
		if [ "${rb_count}" -lt "${rb_needed}" ]; then
			return 0
		fi
		# A skip concludes success; any other conclusion breaks the streak
		# before a single extra API call.
		while IFS=$'\t' read -r rb_id rb_created rb_conclusion; do
			if [ "${rb_conclusion}" != "success" ] || ! [[ "${rb_id}" =~ ^[0-9]{1,20}$ ]]; then
				return 0
			fi
		done <<< "${rb_runs}"
		if ! release_blocker_ensure_work_dir; then
			echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_gate_unavailable lookup=work_dir"
			return 0
		fi
		rb_log_file="${RELEASE_BLOCKER_WORK_DIR}/cycle_job.log"
		while IFS=$'\t' read -r rb_id rb_created rb_conclusion; do
			if ! rb_jobs="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${rb_id}/jobs?per_page=100" 2>/dev/null)"; then
				echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_gate_unavailable lookup=jobs run=${rb_id}"
				return 0
			fi
			rb_job_id="$(printf '%s' "${rb_jobs}" | jq -r '[.jobs[]? | select(.name == "cycle")] | first | .id // empty' 2>/dev/null || true)"
			if ! [[ "${rb_job_id}" =~ ^[0-9]{1,20}$ ]]; then
				# No cycle job in that run: the streak is not proven.
				echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_gate_unavailable lookup=cycle_job run=${rb_id}"
				return 0
			fi
			# Job logs carry ANSI colour codes; gh rejects them without
			# --allow-escape-sequences (as in workflow_failure_heal_intake.sh).
			if ! gh_retry gh api --allow-escape-sequences "repos/${GITHUB_REPOSITORY}/actions/jobs/${rb_job_id}/logs" > "${rb_log_file}" 2>/dev/null; then
				echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_gate_unavailable lookup=logs run=${rb_id}"
				return 0
			fi
			# Log lines carry a timestamp prefix; match the fixed token only.
			rb_matched="$(tr -d '\r' < "${rb_log_file}" | sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep -oE '^[^ ]+ PROMOTE_CYCLE_SKIPPED reason=(gate_busy|smoke_gate_cancelled)( |$)' | tail -n 1 | sed -E 's/.* reason=([a-z_]+).*/\1/' || true)"
			if [ -z "${rb_matched}" ]; then
				echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=streak_broken blocker=cycle_gate_unavailable run=${rb_id}"
				return 0
			fi
			rb_lines+="run=$(release_blocker_run_url "${rb_id}") reason=${rb_matched}"$'\n'
			rb_oldest="${rb_created}"
		done <<< "${rb_runs}"
		rb_age="$(release_blocker_age_from_iso "${rb_oldest}")"
		if [ -z "${rb_age}" ]; then
			echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_gate_unavailable lookup=run_created_at"
			return 0
		fi
	else
		rb_age=0
	fi
	release_blocker_ensure_work_dir || return 0
	rb_evidence="${RELEASE_BLOCKER_WORK_DIR}/evidence.txt"
	{
		release_blocker_evidence_header cycle_gate_unavailable "${rb_age}" "streak=${RELEASE_BLOCKER_CYCLE_SKIP_STREAK}" "${main_tip:-}"
		printf 'run=%s reason=%s detail=%s\n' "$(release_blocker_run_url "${GITHUB_RUN_ID}")" "${rb_current}" "$(release_blocker_safe "$(printf '%s' "${rb_current_detail}" | tr ' ' '_')")"
		printf '%s' "${rb_lines}"
	} > "${rb_evidence}"
	report_release_blocker cycle_gate_unavailable "${rb_age}" "${rb_evidence}" "${main_tip:-}"
	return 0
}

# release_blocker_untrusted_comments <owner> <name> <issue_number>...
# Batching contract (§14):
#   input   1-5 issue numbers (the first ones of the cycle_in_flight skip),
#           each matching ^[1-9][0-9]*$; owner and name matching
#           ^[A-Za-z0-9_.-]+$.
#   output  the raw GraphQL response on stdout; one alias u<n> per issue with
#           its number and its last 30 comments (createdAt, body,
#           authorAssociation, author.login).
#   calls   exactly 1 (gh api graphql through gh_retry).
#   failure non-zero exit; the caller logs guard_unavailable and reports
#           nothing (fail open, no per-issue fallback loop).
# Audited calls: guard 2's REST issues listing carries labels only; the
# baseline-marker comments read runs after guard 2 (and REST returns the
# oldest comment page, where the poller's alert may be missing); the hold
# guard's GraphQL batch reads LabeledEvent history, not comments.
release_blocker_untrusted_comments()
{
	local owner="$1"
	local name="$2"
	shift 2
	local aliases="" issue_number count=0 query response
	if ! [[ "${owner}" =~ ^[A-Za-z0-9_.-]+$ ]] || ! [[ "${name}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
		return 1
	fi
	for issue_number in "$@"; do
		[[ "${issue_number}" =~ ^[1-9][0-9]*$ ]] || return 1
		count=$((count + 1))
		aliases+=" u${issue_number}:issue(number:${issue_number}){number comments(last:30){nodes{createdAt body authorAssociation author{login}}}}"
	done
	if [ "${count}" -lt 1 ] || [ "${count}" -gt 5 ]; then
		return 1
	fi
	query="query(\$owner:String!,\$name:String!){repository(owner:\$owner,name:\$name){${aliases} }}"
	if ! response="$(gh_retry gh api graphql -f query="${query}" -f owner="${owner}" -f name="${name}" 2>/dev/null)"; then
		return 1
	fi
	if ! printf '%s' "${response}" | jq -e 'type == "object" and ((.data.repository? // null) | type) == "object"' >/dev/null 2>&1; then
		return 1
	fi
	printf '%s\n' "${response}"
}

# release_blocker_check_untrusted_hold <skip_detail>
# cycle_held_untrusted_marker: a held tracking issue whose newest trusted
# "Untrusted apply-analysis marker" alert (posted by the poller,
# orchestrate_poll_process.sh) is at least RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS
# old and has no trusted baseline marker after it. Trusted = github-actions
# or COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS, as in
# last_cycle_baseline_sha. The oldest such alert is reported.
# API calls: 1 GraphQL read; +1 GET git/ref/heads/<default> only when a
# report is sent (main_tip is not known yet at guard 2).
release_blocker_check_untrusted_hold()
{
	local rb_detail="${1:-}" rb_csv="" rb_response="" rb_nums_json="" rb_found="" rb_issue="" rb_alerted_at="" rb_age="" rb_tip="" rb_evidence=""
	local -a rb_issues=()
	rb_csv="$(printf '%s' "${rb_detail}" | sed -nE 's/^(.* )?tracking_issues=([0-9,]+)( .*)?$/\2/p' | head -n 1)"
	if ! [[ "${rb_csv}" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]]; then
		return 0
	fi
	IFS=',' read -r -a rb_issues <<< "${rb_csv}"
	rb_issues=("${rb_issues[@]:0:5}")
	if ! rb_response="$(release_blocker_untrusted_comments "${GITHUB_REPOSITORY%%/*}" "${GITHUB_REPOSITORY#*/}" "${rb_issues[@]}")"; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_held_untrusted_marker lookup=untrusted_comments"
		return 0
	fi
	rb_nums_json="$(printf '%s\n' "${rb_issues[@]}" | jq -sc 'map(tonumber)' 2>/dev/null || echo '[]')"
	if ! rb_found="$(printf '%s' "${rb_response}" | jq -r \
		--argjson nums "${rb_nums_json}" \
		--argjson cap "${RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS}" \
		--arg trusted "${COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS}" '
		($trusted | split(",") | map(ascii_upcase | gsub("^\\s+|\\s+$"; ""))) as $ok
		| ((.data.repository // {}) | if type == "object" then . else {} end) as $repo
		| [ $nums[] as $n
			| ($repo["u\($n)"] // null) as $node
			| select(($node | type) == "object" and $node.number == $n)
			| [ ((($node.comments // {}) | if type == "object" then . else {} end).nodes // [])[]? | select(type == "object")
				| select((((.author // {}) | if type == "object" then (.login // "") else "" end) as $l | $l == "github-actions" or $l == "github-actions[bot]")
					or (((.authorAssociation // "") | tostring | ascii_upcase) as $a | ($ok | index($a)) != null))
				| {at: ((.createdAt // "") | tostring), body: ((.body // "") | tostring)}
				| . + {t: (.at | (try (sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch null))}
				| select(.t != null) ] as $tc
			| ([$tc[] | select(.body | test("^## .{0,8}Untrusted apply-analysis marker"))] | max_by(.t)) as $alert
			| select($alert != null)
			| select([$tc[] | select(.t > $alert.t and (.body | test("(?m)^apply-analysis-cycle-baseline-sha: [0-9a-f]{40}")))] | length == 0)
			| ((now - $alert.t) | floor) as $age
			| select($age >= $cap)
			| {n: $n, at: $alert.at, age: $age} ]
		| max_by(.age) // empty
		| "\(.n) \(.at) \(.age)"' 2>/dev/null)"; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_held_untrusted_marker lookup=untrusted_parse"
		return 0
	fi
	[ -n "${rb_found}" ] || return 0
	read -r rb_issue rb_alerted_at rb_age <<< "${rb_found}"
	if ! [[ "${rb_issue}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${rb_age}" =~ ^[0-9]+$ ]]; then
		return 0
	fi
	rb_tip="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${PROMOTE_CYCLE_DEFAULT_BRANCH}" 2>/dev/null | jq -r '.object.sha // empty' 2>/dev/null || true)"
	[[ "${rb_tip}" =~ ^[0-9a-f]{40}$ ]] || rb_tip=""
	release_blocker_ensure_work_dir || return 0
	rb_evidence="${RELEASE_BLOCKER_WORK_DIR}/evidence.txt"
	{
		release_blocker_evidence_header cycle_held_untrusted_marker "${rb_age}" "threshold_secs=${RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS}" "${rb_tip}"
		printf 'tracking_issue=%s/%s/issues/%s alerted_at=%s\n' "$(release_blocker_safe "${GITHUB_SERVER_URL:-https://github.com}")" "${GITHUB_REPOSITORY}" "${rb_issue}" "$(release_blocker_safe "${rb_alerted_at}")"
		printf 'checked_issues=%s\n' "$(printf '%s\n' "${rb_issues[@]}" | paste -sd, -)"
	} > "${rb_evidence}"
	report_release_blocker cycle_held_untrusted_marker "${rb_age}" "${rb_evidence}" "${rb_tip}"
	return 0
}

# release_blocker_check_cycle_stale <skip_reason>
# cycle_stale: main is ahead of the stable tag with code changes and the
# later of the last cycle's baseline marker and the oldest unreleased commit
# is at least RELEASE_BLOCKER_CYCLE_STALE_SECS old. Reads no extra data.
release_blocker_check_cycle_stale()
{
	local rb_skip_reason="${1:-}" rb_marker_t="" rb_commit_t="" rb_clock="" rb_age="" rb_now="" rb_evidence=""
	[ "${release_blocker_main_ahead}" = "true" ] || return 0
	[ "${release_blocker_baseline_read}" = "true" ] || return 0
	case "${rb_skip_reason}" in
		disabled|insufficient_docs|cycle_in_flight|another_run_active) return 0 ;;
	esac
	rb_marker_t="$(release_blocker_epoch_from_iso "${release_blocker_baseline_at}")"
	rb_commit_t="$(release_blocker_epoch_from_iso "${release_blocker_oldest_commit_at}")"
	[[ "${rb_marker_t}" =~ ^[0-9]+$ ]] || rb_marker_t=""
	[[ "${rb_commit_t}" =~ ^[0-9]+$ ]] || rb_commit_t=""
	rb_clock="${rb_marker_t}"
	if [ -n "${rb_commit_t}" ] && { [ -z "${rb_clock}" ] || [ "${rb_commit_t}" -gt "${rb_clock}" ]; }; then
		rb_clock="${rb_commit_t}"
	fi
	# Neither time is known: the age cannot be proven, so nothing is reported.
	[ -n "${rb_clock}" ] || return 0
	rb_now="$(date -u +%s)"
	rb_age=$((rb_now - rb_clock))
	[ "${rb_age}" -ge 0 ] || rb_age=0
	[ "${rb_age}" -ge "${RELEASE_BLOCKER_CYCLE_STALE_SECS}" ] || return 0
	release_blocker_ensure_work_dir || return 0
	rb_evidence="${RELEASE_BLOCKER_WORK_DIR}/evidence.txt"
	{
		release_blocker_evidence_header cycle_stale "${rb_age}" "threshold_secs=${RELEASE_BLOCKER_CYCLE_STALE_SECS}" "${main_tip:-}"
		printf 'baseline_marker_at=%s oldest_commit_at=%s commits_truncated=%s\n' "$(release_blocker_safe "${release_blocker_baseline_at:-none}")" "$(release_blocker_safe "${release_blocker_oldest_commit_at:-none}")" "${release_blocker_commits_truncated}"
		printf 'skip_reason=%s\n' "$(release_blocker_safe "${rb_skip_reason}")"
	} > "${rb_evidence}"
	report_release_blocker cycle_stale "${rb_age}" "${rb_evidence}" "${main_tip:-}"
	return 0
}

# release_blocker_on_skip <skip_reason> <skip_detail>: called first thing in
# skip_cycle. Runs once per tick and sends at most one report: the streak or
# the untrusted-hold check (by reason), then the stale check when neither
# reported. Always returns 0.
release_blocker_on_skip()
{
	local rb_skip_reason="${1:-}" rb_skip_detail="${2:-}" rb_relevant="false"
	[ "${release_blocker_on_skip_done}" = "true" ] && return 0
	release_blocker_on_skip_done="true"
	case "${rb_skip_reason}" in
		gate_busy|smoke_gate_cancelled|cycle_in_flight) rb_relevant="true" ;;
	esac
	if [ "${release_blocker_main_ahead}" = "true" ]; then
		case "${rb_skip_reason}" in
			disabled|insufficient_docs|cycle_in_flight|another_run_active) ;;
			*) rb_relevant="true" ;;
		esac
	fi
	[ "${rb_relevant}" = "true" ] || return 0
	# Kill switch: no detection read, no report.
	if ! is_truthy "${RELEASE_BLOCKER_HEAL_ENABLED}"; then
		echo "RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled blocker=cycle_reporters skip_reason=$(release_blocker_safe "${rb_skip_reason}")"
		return 0
	fi
	case "${rb_skip_reason}" in
		gate_busy|smoke_gate_cancelled)
			release_blocker_check_skip_streak "${rb_skip_reason}" "${rb_skip_detail}" || true
			;;
		cycle_in_flight)
			release_blocker_check_untrusted_hold "${rb_skip_detail}" || true
			;;
	esac
	if [ "${release_blocker_fired}" != "true" ]; then
		release_blocker_check_cycle_stale "${rb_skip_reason}" || true
	fi
	release_blocker_cleanup
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

# 2. A cycle already in flight (proving or verifying run open). A tracking
#    issue parked on a human latch label for PROMOTE_CYCLE_HOLD_MAX_HOURS no
#    longer holds (bounded hold, see promote_cycle_hold_guard).
encoded_labels="$(printf '%s' "ai:orchestrator-tracking,${PROMOTE_CYCLE_TRACKING_LABEL}" | sed 's/:/%3A/g; s/,/%2C/g')"
in_flight_listing="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues?state=open&labels=${encoded_labels}&per_page=100" | jq -c '[.[] | select(has("pull_request") | not) | {number, labels: [(.labels // [])[] | (.name? // empty)]}]')"
in_flight="$(printf '%s' "${in_flight_listing}" | jq -r '[.[].number] | join(",")')"
if [ -n "${in_flight}" ]; then
	promote_cycle_hold_guard "${in_flight_listing}" "${in_flight}"
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
		# cycle_stale reporter: main carries unreleased code from here on.
		release_blocker_main_ahead=true
		release_blocker_oldest_commit_at="${CODE_CHANGES_OLDEST_COMMIT_AT:-}"
		case "${CODE_CHANGES_OUT}" in
			"(compare payload truncated"*) release_blocker_commits_truncated=true ;;
		esac
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
last_baseline_with_time="$(last_cycle_baseline_sha with_time)"
last_baseline_rc=$?
set -e
if [ "${last_baseline_rc}" -ne 0 ]; then
	skip_cycle guard_unavailable "search=${CYCLE_BASELINE_MARKER}"
fi
release_blocker_baseline_read=true
# "<sha> <created_at>": the SHA keeps today's checks; the time feeds cycle_stale.
last_baseline="${last_baseline_with_time%% *}"
if [ "${last_baseline_with_time}" != "${last_baseline}" ]; then
	release_blocker_baseline_at="${last_baseline_with_time#* }"
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
