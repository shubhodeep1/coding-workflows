#!/usr/bin/env bash
# pr_checks_lib.sh — Shared PR check-runs merge gate.
#
# Single source of truth for the "are all blocking check-runs on a PR's
# head commit acceptable?" gate used by BOTH merge paths:
#   - scripts/orchestrate_poll_process.sh (orchestrator poller: the final
#     integration-merge gate AND the four review-blocked merge gates).
#   - scripts/review_rb_judge.sh (standalone review-blocked judge's
#     merge_with_followup gate).
#
# Before this library existed the two paths each carried their own copy
# of the gate and they drifted: the orchestrator's `_pr_checks_completed`
# grew a Layer-1 "required checks only" filter (branch protection ∪
# ORCH_FINAL_MERGE_REQUIRED_CHECKS) while review_rb_judge.sh stayed on the
# legacy "block on ANY failing check-run" behaviour. That asymmetry
# deadlocked review-blocked-judge merges whenever a non-required /
# environmental check (e.g. CodeQL when code scanning is disabled at the
# repo level) was permanently red: the judge approved, but the stricter
# gate refused, the issue stayed ai:review-blocked, and stall recovery
# re-fired forever. Hosting the gate here lets every caller share the
# required-checks filter so the two gates can never drift again.
#
# Dependencies (provided by the caller's environment, not re-declared
# here): `gh_retry` and `_safe_gh_jq` from scripts/gh_helpers.sh, plus
# `jq`. Source gh_helpers.sh before sourcing this file.
#
# Optional inputs read from the environment (all default-safe under
# `set -u`):
#   PR_CHECKS_REPOSITORY   — owner/repo to query; defaults to
#                            ${GITHUB_REPOSITORY}. review_rb_judge.sh sets
#                            this to its ${REPOSITORY}.
#   PR_CHECKS_SELF_RUN_ID  — when non-empty, check-runs whose details_url
#                            points at this GitHub Actions run id are
#                            excluded from the blocking count. Needed by
#                            review_rb_judge.sh, which runs INSIDE the
#                            `review / codex-agent` job that is itself a
#                            still-in_progress check-run on the polled SHA
#                            (see PR #2703). The orchestrator leaves this
#                            unset, so the exclusion is a no-op there.
#   ORCH_FINAL_MERGE_REQUIRED_CHECKS — operator override for the required
#                            set (sentinels: "*" = legacy block-on-any,
#                            "" = allow-all). Unset → built-in default.
#
# Output side-channel:
#   PR_CHECKS_LAST_REASON  — set by _pr_checks_completed on every return so
#                            callers that need a granular reason (e.g.
#                            review_rb_judge.sh's judge_skip_reason) can map
#                            it. Values: ok | blocking | query_failed |
#                            unresolved_head_sha | allow_all.
#   PR_CHECKS_LAST_PENDING — set by _pr_checks_completed (reset to 0 on
#                            entry): count of still-running check-runs on
#                            the head (self-run excluded). Read by
#                            _pr_wait_for_required_checks to tell a
#                            settled failure from a pending one.
#   AUTO_MERGE_WAIT_WORKFLOWS — workflow names (comma-separated, default
#                            "CI"; none/off disables) whose not-yet-completed
#                            runs on the head keep _pr_wait_for_required_checks
#                            waiting (_pr_head_ci_runs_pending).
#   PR_CHECKS_LAST_TOTAL   — set by _pr_checks_completed when it reads the
#                            check-runs: total non-self check-runs on the
#                            head (0 = none registered yet).
#   PR_CHECKS_LAST_CI_NAME — set by _pr_checks_completed on entry: the
#                            resolved AUTO_MERGE_REQUIRED_CI_CHECK name
#                            (`none` when the requirement is off).
#   PR_CHECKS_LAST_CI_STATE — set by _pr_checks_completed: state of that
#                            named check on the head, aggregated over every
#                            non-self check-run with that exact name:
#                            failure (any completed run whose conclusion is
#                            not success, so skipped/neutral/cancelled count
#                            as failure) > pending > success > absent;
#                            unknown when the check-runs could not be read;
#                            disabled when the name is `none`. Computed from
#                            the check-runs listing the gate already fetched
#                            (no extra API call) and enforced only by
#                            _pr_wait_for_required_checks; the return value
#                            of _pr_checks_completed does not depend on it.
#   PR_CHECKS_CI_RUN_IDS   — optional input: when set, only check-runs whose
#                            details_url run id is in this comma-separated
#                            list count toward PR_CHECKS_LAST_CI_STATE
#                            (_pr_wait_for_required_checks passes the
#                            AUTO_MERGE_WAIT_WORKFLOWS run ids on the head).
#                            Unset: match by name and head SHA only.
#   PR_CHECKS_GATE_SUPERSEDED — stderr log line (not a variable) written by
#                            _pr_checks_completed when the newest failed
#                            `review / gate` check-run on the head is checked
#                            against the `ai-review/head-gate` commit status
#                            (issue #9212): outcome=superseded when a newer
#                            success status for the exact head SHA drops it
#                            from the blocking set, outcome=skip reason=<token>
#                            otherwise (the check-run keeps blocking).
#   PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE / PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE_KEY
#                          — internal shell globals (not inputs) written by
#                            _pr_checks_completed: the token's own login for
#                            the #9212 creator check, keyed on a sha256
#                            fingerprint of GH_TOKEN|GITHUB_TOKEN. Written only
#                            after a successful, regex-valid `GET user` read (a
#                            failed or invalid read is never cached); they
#                            persist across calls in the same shell, and a
#                            token change re-reads.

# Built-in default required-check set. Defined set-if-unset so a caller
# that already declared it (the orchestrator does, near
# MAX_FINAL_MERGE_ATTEMPTS) keeps its value, and a caller that never set
# it (review_rb_judge.sh) still gets a SAFE non-empty default rather than
# the empty-string allow-all sentinel. Keep this string identical to the
# orchestrate_poll_process.sh ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT
# assignment; tests/test_pr_checks_lib_required_filter.py pins them equal.
: "${ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT:=CI,lint,Integration PR readiness check,Lint plan-archival completeness,Lint PR body for auto-close keywords against orchestrator-tracking issues,pr-body-lint,review / gate}"

# jq definitions shared by the gate filters below and by
# _summarize_final_merge_blockers (scripts/orchestrate_poll_process.sh).
# Callers pass --arg ci (the resolved AUTO_MERGE_REQUIRED_CI_CHECK name)
# and --arg bound (PR_CHECKS_CI_RUN_IDS, or `__unbound__`).
# _mark_superseded tags each check-run with `_superseded`. For most names a
# run is superseded when a newer run of the same name exists (highest id,
# then latest started_at), so a re-run replaces a cancelled attempt. The
# configured CI check name can be shared by jobs of other workflows (here
# `lint` also exists in lint-plan-archival.yml, which re-runs on every
# body edit of a PR that changes docs/completed/**;
# the PR-body lint job, now in pr-checks.yml, was `lint` too until it was
# renamed `pr-body-lint` on 2026-11-18, and a branch that predates the
# rename still reports the old name), so for that
# name a newer run supersedes only when it comes from the same Actions run
# (a re-run attempt) or when both runs are bound CI runs. Otherwise an
# unrelated workflow's `lint` would hide a cancelled CI `lint`.
PR_CHECKS_JQ_SUPERSEDE_DEFS='
def _run_id: [(.details_url // "") | capture("/actions/runs/(?<id>[0-9]+)(/|$)") | .id] | first;
def _bound_ci_run: ($bound != "__unbound__") and (_run_id as $rid | ($rid != null) and (($bound | split(",") | index($rid)) != null));
def _newer_than($r): [(.id // 0), (.started_at // "")] > [($r.id // 0), ($r.started_at // "")];
def _mark_superseded:
	group_by(.name // "")
	| map(
		if ($ci != "" and $ci != "none" and $ci != "__invalid__" and ((.[0].name // "") == $ci)) then
			. as $grp
			| map(
				. as $r
				| ($r | _run_id) as $rrid
				| ($r | _bound_ci_run) as $rb
				| . + {_superseded: ([$grp[] | select(_newer_than($r) and (((_run_id) as $nid | ($nid != null and $nid == $rrid)) or ($rb and _bound_ci_run)))] | length > 0)}
			)
		else
			max_by([(.id // 0), (.started_at // "")]) as $newest | map(. + {_superseded: (. != $newest)})
		end
	)
	| add // [];
'

# Helper: returns the comma-separated list of check-run names treated as
# blocking for the given base ref. Resolution: branch protection's
# required_status_checks.contexts first (server-side truth), then the
# operator allowlist in ORCH_FINAL_MERGE_REQUIRED_CHECKS, then the
# built-in default. Sentinels passed through verbatim: "*" = legacy mode,
# "" = allow-all.
#
# §15 hygiene: this issues one extra `gh api` call per invocation when
# the base ref is known. The call is fail-fast (gh_retry recognises
# "Branch not protected" 404 as a non-retryable permanent failure), so
# unprotected branches resolve in one round trip and fall through to
# the env var.
_pr_required_check_names_for_base()
{
	local base_ref="$1"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local contexts=""
	if [ -n "${base_ref}" ]; then
		local protection_ref
		local protection_json
		# GitHub treats the branch name as a single path segment here, so
		# slash-containing refs (for example release/v1) must be URL-encoded.
		protection_ref="$(printf '%s' "${base_ref}" | jq -sRr '@uri' 2>/dev/null || echo "")"
		# Fall back to Python's byte-safe encoder when the jq helper is not
		# available. If no encoder is available for a slash-containing ref,
		# fail closed to legacy "*" rather than probing the wrong unencoded
		# protection path and silently downgrading branch-protection truth.
		if [ -z "${protection_ref}" ]; then
			protection_ref="$(printf '%s' "${base_ref}" | python3 -c 'import sys, urllib.parse; print(urllib.parse.quote_from_bytes(sys.stdin.buffer.read(), safe=""))' 2>/dev/null || echo "")"
		fi
		if [ -z "${protection_ref}" ]; then
			case "${base_ref}" in
				*/*)
					echo "*"
					return 0
					;;
			esac
		fi
		protection_json="$(gh_retry _safe_gh_jq "repos/${repo}/branches/${protection_ref:-${base_ref}}/protection" 2>/dev/null || echo "")"
		if [ -n "${protection_json}" ]; then
			contexts="$(printf '%s' "${protection_json}" | jq -r '
				if (type == "object" and (.required_status_checks // null) != null) then
					(.required_status_checks.contexts // []) | join(",")
				else
					""
				end
			' 2>/dev/null | tail -n1)"
		fi
	fi
	if [ -n "${contexts}" ]; then
		echo "${contexts}"
		return 0
	fi
	# ORCH_FINAL_MERGE_REQUIRED_CHECKS resolution: honour an explicit
	# value (possibly the empty string for allow-all) verbatim —
	# `${VAR-…}` not `${VAR:-…}` because an explicit empty string must
	# remain empty (allow-all sentinel). When the var was never set we
	# fall back to the built-in default declared above.
	echo "${ORCH_FINAL_MERGE_REQUIRED_CHECKS-${ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT}}"
}

# Helper: Check whether all check-runs on a PR's head commit have
# completed AND none of the check-runs that count as "required"
# are in an unacceptable conclusion (success/neutral/skipped are
# acceptable; cancelled, timed_out, action_required, failure and
# startup_failure block). Returns 0 when nothing is blocking, 1 otherwise
# (including API errors). Sets PR_CHECKS_LAST_REASON on every return.
#
# A cancelled check-run blocks only while it is the newest check-run of
# its name (highest id, then latest started_at): re-running a job creates
# a new check-run with the same name on the same SHA, so a cancelled run
# that a newer run superseded does not block. For the configured CI check
# name (shared with other workflows' jobs) only a newer run from the same
# Actions run, or a bound CI run, supersedes (PR_CHECKS_JQ_SUPERSEDE_DEFS).
# Every other check-run is evaluated on its own: a failed, timed-out or
# still-running run blocks even when a newer run of the same name exists.
# A check-run carries no workflow identity, so a newer same-name run from
# another workflow (this repo has two jobs named `lint`: ci.yml and
# lint-plan-archival.yml) must not hide a
# failure or an in-progress run, nor a cancelled run of the CI check.
# Newest is decided before the self-run
# exclusion, so an older cancelled attempt of the caller's own job never
# resurfaces. A cancelled required check with nothing newer is a settled
# failure (pending=0).
#
# The gate's "required" set is resolved per call as:
#   1. Branch protection's `required_status_checks.contexts` on the
#      PR's base ref, when non-empty.
#   2. Comma-separated names in ORCH_FINAL_MERGE_REQUIRED_CHECKS, otherwise.
#   3. The built-in ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT, if unset.
# Sentinel "*" restores legacy fail-closed-on-any-failure behaviour;
# sentinel "" (env var explicitly empty) makes the gate allow-all.
#
# The check-runs API call uses --paginate --slurp so commits with >100
# check-runs don't hide failures on later pages.
#
# Review-gate supersede (issue #9212). A re-dispatched review run's own
# `review / gate` check-run attaches to the commit the workflow runs on
# (`main`), not to the PR head, so it never supersedes a gate job that
# failed on the head in the PR's first review run (PR #9168: run
# 42736609134 died on a rate limit in 3 seconds and every later green
# review was refused with reason=required_checks_failed). When `review /
# gate` counts as blocking (legacy "*" mode, or it is in the required set)
# and its NEWEST check-run on the head completed with a conclusion other
# than success/neutral/skipped, the gate reads the head's commit statuses
# (one conditional `GET commits/<sha>/statuses?per_page=100`, first page,
# newest first; no call at all when the gate did not fail) and drops that
# one check-run from the blocking count only when the newest status with
# context `ai-review/head-gate` (pinned literal, the value
# scripts/review_head_gate.sh posts; not read from the environment) has state
# success, names the full 40-hex head SHA, was created strictly after
# the check-run's completed_at, and whose `creator.login` equals the
# caller token's own login (one `GET user`, only when every other condition
# holds; the same identity review_head_gate_post_status posts with), so a
# principal that can merely write commit statuses cannot relax the check.
# Every other case keeps blocking. Each
# evaluation logs one `PR_CHECKS_GATE_SUPERSEDED pr=<n> head=<sha>
# failed_run=<actions run id|unknown> check_run=<id> ... outcome=superseded
# status_created_at=<ts>` or `... outcome=skip reason=<token>` line to
# STDERR, because _pr_wait_for_required_checks discards this function's
# stdout and that is the auto-merge path PR #9168 was stuck on. Skip
# reasons: short_head_sha, status_read_failed, no_head_gate_status,
# status_not_success, status_sha_mismatch, status_not_newer,
# unparseable_timestamp, untrusted_status_creator, creator_check_failed.
# PR_CHECKS_LAST_REASON values, PENDING/TOTAL and
# the CI side channel are unaffected.
#
# Usage:  _pr_checks_completed <PR_NUMBER> [<HEAD_SHA>] [<BASE_REF>]
#   HEAD_SHA optional: when omitted, fetched from the PR.
#   BASE_REF optional: callers that pass it (even as "") opt into the
#   required-checks filter. Legacy callers that omit BASE_REF entirely
#   keep the pre-filter "all check-runs must pass" behaviour.
_pr_checks_completed()
{
	PR_CHECKS_LAST_REASON="query_failed"
	PR_CHECKS_LAST_PENDING=0
	PR_CHECKS_LAST_TOTAL=""
	# stderr dropped: _pr_wait_for_required_checks warns once per wait instead
	# of this call repeating an invalid-name warning on every poll.
	PR_CHECKS_LAST_CI_NAME="$(_pr_auto_merge_required_ci_check_name "${3:-}" 2>/dev/null)"
	PR_CHECKS_LAST_CI_STATE="unknown"
	if [ "${PR_CHECKS_LAST_CI_NAME}" = "none" ]; then
		PR_CHECKS_LAST_CI_STATE="disabled"
	fi
	local pr_number="$1"
	local head_sha="${2:-}"
	local base_ref="${3:-}"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local self_run="${PR_CHECKS_SELF_RUN_ID:-}"
	local use_required_filter="false"
	if [ "$#" -ge 3 ]; then
		use_required_filter="true"
	fi

	# Fetch the PR JSON once if we need the head SHA from it. Callers only
	# opt into required-check filtering when they pass BASE_REF explicitly;
	# every legacy caller that omits BASE_REF keeps the original fail-closed
	# "all check-runs on the head SHA must pass" behaviour.
	if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
		local pr_json
		pr_json="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr_number}" || echo "")"
		head_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' 2>/dev/null | tail -n1)"
		if [ "${use_required_filter}" = "true" ] && [ -z "${base_ref}" ]; then
			base_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .base.ref?) then .base.ref else empty end' 2>/dev/null | tail -n1)"
		fi
	fi
	if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
		echo "  [check-runs] Could not resolve head SHA for PR #${pr_number}. Skipping merge."
		PR_CHECKS_LAST_REASON="unresolved_head_sha"
		return 1
	fi

	local required_names_csv
	required_names_csv="*"
	if [ "${use_required_filter}" = "true" ]; then
		required_names_csv="$(_pr_required_check_names_for_base "${base_ref}")"
	fi

	local check_runs_json
	# Fallback to '{}' (NOT '[]') on API failure. The jq filter below has
	# two matching branches: `type == "array"` (production --paginate
	# --slurp shape) and the elif `type == "object"` (legacy single-object
	# shape). An empty object '{}' matches neither — falls through to
	# `else empty`, the numeric guard below trips, and the helper returns
	# 1 (fail-closed). An empty array '[]' would match the first branch
	# and produce `incomplete=0`, fail-OPEN — treating an API error as "no
	# check-runs" and letting the merge proceed.
	check_runs_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${repo}/commits/${head_sha}/check-runs?per_page=100" || echo "{}")"

	# Side channel for _pr_wait_for_required_checks (issue #6963): the state
	# of the configured CI check (AUTO_MERGE_REQUIRED_CI_CHECK) on this head,
	# from the listing fetched above. Computed before the allow-all return so
	# it is populated on every path that read the check-runs. Every matching
	# run counts (fail closed): a failed or still-running run with that name
	# outranks a successful one, and the API failure sentinel '{}' yields
	# `unknown`, never `success`. One exception, only when the runs are bound
	# to the CI workflow (PR_CHECKS_CI_RUN_IDS): a cancelled run superseded by
	# a newer bound run of the same name is dropped, as in the gate below, so
	# a re-run of a cancelled CI check unblocks the wait. Unbound (name-only)
	# matching keeps every run, since another workflow's same-name run could
	# otherwise hide the cancellation. The name reaches jq only through --arg.
	# `__invalid__` (a malformed explicit value with no repository default)
	# never matches a check-run: the state stays `unknown` and the wait times
	# out rather than silently dropping the requirement the operator set.
	if [ "${PR_CHECKS_LAST_CI_NAME}" != "none" ] && [ "${PR_CHECKS_LAST_CI_NAME}" != "__invalid__" ]; then
		local ci_state
		ci_state="$(printf '%s' "${check_runs_json}" | jq -r --arg ci "${PR_CHECKS_LAST_CI_NAME}" --arg self_run "${self_run}" --arg sha "${head_sha}" --arg bound "${PR_CHECKS_CI_RUN_IDS-__unbound__}" '
			def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
			def _is_bound_run: ($bound == "__unbound__") or (([(.details_url // "") | capture("/actions/runs/(?<id>[0-9]+)(/|$)") | .id] | first) as $rid
				| ($rid != null) and (($bound | split(",") | index($rid)) != null));
			(
				if (type == "array") then [.[]? | select(type == "object") | (.check_runs // [])[]]
				elif (type == "object" and (.check_runs | type == "array")) then .check_runs
				else null end
			) as $runs |
			if ($runs == null) then "unknown"
			else
				[$runs[] | select(type == "object" and .name == $ci and ((.head_sha // $sha) | tostring | startswith($sha)) and (_is_self_check_run | not) and _is_bound_run)] as $all |
				(if ($bound == "__unbound__") or (($all | length) == 0) then $all
				else ($all | max_by([(.id // 0), (.started_at // "")])) as $newest
					| [$all[] | select(.conclusion != "cancelled" or . == $newest)]
				end) as $m |
				if ([$m[] | select(.status == "completed" and .conclusion != "success")] | length) > 0 then "failure"
				elif ([$m[] | select(.status != "completed")] | length) > 0 then "pending"
				elif ([$m[] | select(.conclusion == "success")] | length) > 0 then "success"
				else "absent" end
			end
		' 2>/dev/null | tail -n1)"
		case "${ci_state}" in
			absent|pending|success|failure) PR_CHECKS_LAST_CI_STATE="${ci_state}" ;;
			*) PR_CHECKS_LAST_CI_STATE="unknown" ;;
		esac
	fi

	# Allow-all sentinel: env var explicitly set to "" means treat every
	# check-run as advisory. Branch protection (when present) is enforced
	# server-side by GitHub at merge time.
	if [ "${required_names_csv}" = "" ]; then
		echo "  [check-runs] Required-checks gate is empty (allow-all); proceeding with merge for PR #${pr_number} (SHA ${head_sha:0:7})."
		PR_CHECKS_LAST_REASON="allow_all"
		return 0
	fi

	# Review-gate supersede (issue #9212; see the comment above this
	# function). The check name is fixed on purpose (not an env var) so the
	# relaxation stays narrow: one check name, one commit, a verdict newer
	# than the failure.
	local gate_sup_check_name="review / gate"
	local gate_sup_drop_id=""
	local gate_sup_countable="false"
	if [ "${required_names_csv}" = "*" ]; then
		gate_sup_countable="true"
	elif jq -n -e --arg names "${required_names_csv}" --arg n "${gate_sup_check_name}" '
		($names | split(",") | map(gsub("^\\s+|\\s+$"; ""))) | index($n) != null
	' >/dev/null 2>&1; then
		gate_sup_countable="true"
	fi
	if [ "${gate_sup_countable}" = "true" ]; then
		# Newest `review / gate` run (same ordering as _newer_than, chosen
		# before the self-run exclusion), emitted only when it failed.
		local gate_sup_failed=""
		gate_sup_failed="$(printf '%s' "${check_runs_json}" | jq -r --arg n "${gate_sup_check_name}" --arg self_run "${self_run}" '
			def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
			(
				if (type == "array") then [.[]? | select(type == "object") | (.check_runs // [])[]]
				elif (type == "object" and (.check_runs | type == "array")) then .check_runs
				else null end
			) as $runs |
			if ($runs == null) then empty
			else
				[$runs[] | select(type == "object" and .name == $n)] as $g |
				if ($g | length) == 0 then empty
				else
					($g | max_by([(.id // 0), (.started_at // "")])) as $r |
					if ($r.status == "completed")
						and (($r.conclusion // "") as $c | ($c != "success" and $c != "neutral" and $c != "skipped"))
						and (($r.id | type) == "number")
						and ((($r.completed_at // "") | type) == "string") and (($r.completed_at // "") != "")
						and (($r | _is_self_check_run) | not)
					then
						"\($r.id)\t\($r.completed_at)\t\(([($r.details_url // "") | tostring | capture("/actions/runs/(?<id>[0-9]+)(/|$)") | .id] | first) // "unknown")"
					else empty end
				end
			end
		' 2>/dev/null | tail -n1)"
		if [ -n "${gate_sup_failed}" ]; then
			local gate_sup_check_id="" gate_sup_failed_at="" gate_sup_run_id="" gate_sup_reason="" gate_sup_created_at=""
			IFS=$'\t' read -r gate_sup_check_id gate_sup_failed_at gate_sup_run_id <<< "${gate_sup_failed}"
			[[ "${gate_sup_run_id}" =~ ^[0-9]+$ ]] || gate_sup_run_id="unknown"
			if ! [[ "${gate_sup_check_id}" =~ ^[0-9]+$ ]]; then
				# Unusable id: the drop could not target the run, keep blocking.
				gate_sup_reason="status_read_failed"
			elif ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
				# The status must name the full head SHA; a short or odd SHA
				# also never reaches the URL below.
				gate_sup_reason="short_head_sha"
			else
				# §14: no statuses read exists on this path to reuse. The only
				# other statuses read in this file is the combined
				# `commits/<sha>/status` in _pr_ruleset_required_check_blockers,
				# a separate helper the orchestrator calls only on
				# mergeable_state=blocked; there is no cycle-local statuses
				# cache. The list endpoint returns statuses newest first, so
				# the first page holds the newest head-gate status; a miss
				# keeps blocking (no_head_gate_status).
				local gate_sup_statuses_json="" gate_sup_decision=""
				# Pinned to the literal scripts/review_head_gate.sh posts, so an
				# inherited REVIEW_HEAD_GATE_CONTEXT cannot widen which status relaxes
				# the required check.
				local gate_sup_status_ctx="ai-review/head-gate"
				if gate_sup_statuses_json="$(gh_retry _safe_gh_jq "repos/${repo}/commits/${head_sha}/statuses?per_page=100" 2>/dev/null)"; then
					gate_sup_decision="$(printf '%s' "${gate_sup_statuses_json}" | jq -r \
						--arg ctx "${gate_sup_status_ctx}" \
						--arg sha "${head_sha}" --arg failed_at "${gate_sup_failed_at}" '
						if (type != "array") then "skip\tstatus_read_failed"
						else
							[.[] | select(type == "object" and .context == $ctx)] as $s |
							if ($s | length) == 0 then "skip\tno_head_gate_status"
							else
								($s | max_by([(.created_at // ""), (.id // 0)])) as $st |
								if ($st.state != "success") then "skip\tstatus_not_success"
								elif (((($st.url // "") | tostring) | endswith("/statuses/" + $sha)) | not) then "skip\tstatus_sha_mismatch"
								else
									(try ([($st.created_at | fromdateiso8601), ($failed_at | fromdateiso8601)]) catch null) as $ts |
									if ($ts == null) then "skip\tunparseable_timestamp"
									elif ($ts[0] > $ts[1]) then "superseded\t\($st.created_at)\t\(($st.creator.login? // "") | tostring)"
									else "skip\tstatus_not_newer" end
								end
							end
						end
					' 2>/dev/null | tail -n1)"
				fi
				case "${gate_sup_decision}" in
					superseded$'\t'*)
						# Bind the status to its author (review finding, #9212):
						# only a status the token's own login posted (the identity
						# review_head_gate_post_status uses) may relax the check.
						# §14: one `user` read, only on this candidate path (same read
						# as _pr_base_sync_count). A valid login is cached for the
						# shell in PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE, keyed on a
						# fingerprint of GH_TOKEN/GITHUB_TOKEN, so the polls of
						# _pr_wait_for_required_checks do not repeat it and a token
						# switch re-reads. A failed or invalid read is never cached
						# (retried next call). Any read or match failure blocks.
						local gate_sup_creator="" gate_sup_self_login="" gate_sup_token_fp=""
						IFS=$'\t' read -r gate_sup_created_at gate_sup_creator <<< "${gate_sup_decision#superseded$'\t'}"
						gate_sup_token_fp="$(printf '%s|%s' "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}" | sha256sum 2>/dev/null | cut -c1-64)"
						if [ -n "${gate_sup_token_fp}" ] && [ "${PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE_KEY:-}" = "${gate_sup_token_fp}" ]; then
							gate_sup_self_login="${PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE:-}"
						fi
						if [ -z "${gate_sup_self_login}" ]; then
							gate_sup_self_login="$(gh_retry _safe_gh_jq user 2>/dev/null | jq -r 'if type == "object" then (.login // "") else "" end' 2>/dev/null | tail -n1)"
							if [ -n "${gate_sup_token_fp}" ] && [[ "${gate_sup_self_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]]; then
								PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE="${gate_sup_self_login}"
								PR_CHECKS_GATE_SUP_SELF_LOGIN_CACHE_KEY="${gate_sup_token_fp}"
							fi
						fi
						if ! [[ "${gate_sup_self_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]]; then
							gate_sup_reason="creator_check_failed"
						elif [ "${gate_sup_creator}" != "${gate_sup_self_login}" ]; then
							gate_sup_reason="untrusted_status_creator"
						else
							gate_sup_drop_id="${gate_sup_check_id}"
						fi
						;;
					skip$'\t'no_head_gate_status|skip$'\t'status_not_success|skip$'\t'status_sha_mismatch|skip$'\t'status_not_newer|skip$'\t'unparseable_timestamp)
						gate_sup_reason="${gate_sup_decision#skip$'\t'}"
						;;
					*)
						gate_sup_reason="status_read_failed"
						;;
				esac
			fi
			if [ -n "${gate_sup_drop_id}" ]; then
				echo "PR_CHECKS_GATE_SUPERSEDED pr=${pr_number} head=${head_sha} failed_run=${gate_sup_run_id} check_run=${gate_sup_check_id} status_created_at=${gate_sup_created_at} outcome=superseded" >&2
			else
				echo "PR_CHECKS_GATE_SUPERSEDED pr=${pr_number} head=${head_sha} failed_run=${gate_sup_run_id} check_run=${gate_sup_check_id:-unknown} outcome=skip reason=${gate_sup_reason:-status_read_failed}" >&2
			fi
		fi
	fi

	# Self-run exclusion predicate (jq): a check-run hosted by the current
	# GitHub Actions run is never counted as blocking. $self_run="" makes
	# the predicate always false, so the exclusion is a no-op for callers
	# (the orchestrator) that run from a separate workflow off the polled
	# SHA's check-run list. See PR #2703 for the review_rb_judge.sh case.
	local incomplete
	if [ "${required_names_csv}" = "*" ]; then
		# Legacy fail-closed-on-any-failure behaviour (with self-run exclusion).
		incomplete="$(printf '%s' "${check_runs_json}" | jq -r --arg self_run "${self_run}" --arg gate_drop_id "${gate_sup_drop_id}" --arg ci "${PR_CHECKS_LAST_CI_NAME:-none}" --arg bound "${PR_CHECKS_CI_RUN_IDS-__unbound__}" "${PR_CHECKS_JQ_SUPERSEDE_DEFS}"'
			def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
			def _is_pending: .status != "completed" or (.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and (.conclusion != "cancelled" or (._superseded | not)));
			(
				if (type == "array") then
					[.[]? | (.check_runs // [])[]]
				elif (type == "object" and (.check_runs | type == "array")) then
					.check_runs
				else
					null
				end
			) as $runs |
			if ($runs == null) then empty
			else
				[$runs | _mark_superseded | .[] | select(_is_pending and (_is_self_check_run | not)) | select(($gate_drop_id == "") or (((.id // "") | tostring) != $gate_drop_id))] | length
			end
		' 2>/dev/null | tail -n1)"
	else
		# Required-set filter: pending check-runs (status != "completed")
		# always block — autofix or any other in-flight workflow shouldn't
		# race a merge — but FAILED check-runs only block when their
		# `name` appears in required_names_csv. This preserves the original
		# "pending blocks" semantic while letting non-required advisory
		# failures (Copilot, optional reviewers, CodeQL, etc.) through.
		# Whitespace around comma-separated tokens is trimmed so operators
		# can format the env var with spaces after commas.
		incomplete="$(printf '%s' "${check_runs_json}" | jq -r --arg names "${required_names_csv}" --arg self_run "${self_run}" --arg gate_drop_id "${gate_sup_drop_id}" --arg ci "${PR_CHECKS_LAST_CI_NAME:-none}" --arg bound "${PR_CHECKS_CI_RUN_IDS-__unbound__}" "${PR_CHECKS_JQ_SUPERSEDE_DEFS}"'
			def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
			($names | split(",") | map(gsub("^\\s+|\\s+$"; ""))) as $required |
			(
				if (type == "array") then
					[.[]? | (.check_runs // [])[]]
				elif (type == "object" and (.check_runs | type == "array")) then
					.check_runs
				else
					null
				end
			) as $runs |
			if ($runs == null) then empty
			else
				[$runs | _mark_superseded | .[] | select(
					(
						(.status != "completed")
						or (
							(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and (.conclusion != "cancelled" or (._superseded | not)))
							and (.name as $n | $required | index($n))
						)
					)
					and (_is_self_check_run | not)
					and (($gate_drop_id == "") or (((.id // "") | tostring) != $gate_drop_id))
				)] | length
			end
		' 2>/dev/null | tail -n1)"
	fi
	if ! [[ "${incomplete}" =~ ^[0-9]+$ ]]; then
		echo "  [check-runs] Could not query check-runs for PR #${pr_number} (SHA ${head_sha:0:7}). Skipping merge."
		PR_CHECKS_LAST_REASON="query_failed"
		return 1
	fi
	# Side channel for callers that wait: how many of the blocking runs are
	# still running (status != completed, self-run excluded). A blocking
	# result with zero pending runs is a settled failure, not a wait.
	# Every running check-run counts, as in the blocking count above, so a
	# blocking result with pending=0 is always a settled failure.
	PR_CHECKS_LAST_PENDING="$(printf '%s' "${check_runs_json}" | jq -r --arg self_run "${self_run}" '
		def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
		(
			if (type == "array") then [.[]? | (.check_runs // [])[]]
			elif (type == "object" and (.check_runs | type == "array")) then .check_runs
			else [] end
		) | [.[] | select(.status != "completed" and (_is_self_check_run | not))] | length
	' 2>/dev/null | tail -n1)"
	[[ "${PR_CHECKS_LAST_PENDING}" =~ ^[0-9]+$ ]] || PR_CHECKS_LAST_PENDING=0
	# Total non-self check-runs on the head: 0 on a fresh push means CI has
	# not registered yet, which the wait must not read as green.
	PR_CHECKS_LAST_TOTAL="$(printf '%s' "${check_runs_json}" | jq -r --arg self_run "${self_run}" '
		def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
		(
			if (type == "array") then [.[]? | (.check_runs // [])[]]
			elif (type == "object" and (.check_runs | type == "array")) then .check_runs
			else [] end
		) | [.[] | select(_is_self_check_run | not)] | length
	' 2>/dev/null | tail -n1)"
	# Unparseable count: treat as "none registered" so the wait polls the
	# grace intervals rather than reading a jq failure as green.
	[[ "${PR_CHECKS_LAST_TOTAL}" =~ ^[0-9]+$ ]] || PR_CHECKS_LAST_TOTAL=0

	if [ "${incomplete}" -gt 0 ]; then
		if [ "${required_names_csv}" = "*" ]; then
			echo "  [check-runs] PR #${pr_number} has ${incomplete} blocking check-run(s) (SHA ${head_sha:0:7}). Skipping merge."
		else
			echo "  [check-runs] PR #${pr_number} has ${incomplete} blocking required check-run(s) (SHA ${head_sha:0:7}, required-set=${required_names_csv}). Skipping merge."
		fi
		PR_CHECKS_LAST_REASON="blocking"
		return 1
	fi

	if [ "${required_names_csv}" = "*" ]; then
		echo "  [check-runs] All check-runs completed and acceptable for PR #${pr_number} (SHA ${head_sha:0:7}). Proceeding with merge."
	else
		echo "  [check-runs] All required check-runs completed and acceptable for PR #${pr_number} (SHA ${head_sha:0:7}). Proceeding with merge."
	fi
	PR_CHECKS_LAST_REASON="ok"
	return 0
}

# ---------------------------------------------------------------------------
# _pr_ruleset_required_check_blockers HEAD_SHA BASE_REF
#
# Names the repository-ruleset required status checks that are not passing
# on HEAD_SHA. _pr_checks_completed reads required names only from classic
# branch protection and judges only check-runs, so a ruleset-required commit
# status (for example `coding-workflows/workflow-integrity`) is invisible to
# it. shubhodeep1/fun-token-multi-chain#579 spent all three final-merge
# attempts on a PR GitHub reported as mergeable_state=blocked for exactly
# that reason.
#
# Input:  HEAD_SHA (40 hex), BASE_REF (branch name; URL-encoded here).
# Output: PR_RULESET_BLOCKERS_SUMMARY, a comma list of "context (state)"
#         where state is failure / error / pending / missing.
# Return: 0 no blockers (or the base has no ruleset-required checks),
#         1 at least one blocker,
#         2 a lookup failed; callers fail open and keep their old behaviour.
# API calls (§15): 1 (GET rules/branches/<base>) when no required checks
# are configured; otherwise +1 (GET commits/<sha>/status) and +1
# (GET commits/<sha>/check-runs) only when some context has no commit
# status. Callers should invoke it only when GitHub already reports the PR
# as mergeable_state=blocked, so unblocked merges pay nothing.
# Matching is by context name. A context is satisfied by its commit status
# (success) or, when it has none, by its newest check-run of that name
# (success / neutral / skipped). Neither present counts as `missing`, which
# GitHub also treats as blocking.
# ---------------------------------------------------------------------------
_pr_ruleset_required_check_blockers()
{
	PR_RULESET_BLOCKERS_SUMMARY=""
	local head_sha="$1"
	local base_ref="$2"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	[[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || return 2
	[ -n "${base_ref}" ] && [ -n "${repo}" ] || return 2

	local ruleset_base_enc
	ruleset_base_enc="$(printf '%s' "${base_ref}" | jq -sRr '@uri' 2>/dev/null || echo "")"
	[ -n "${ruleset_base_enc}" ] || return 2

	local rules_json ruleset_contexts_json
	rules_json="$(gh_retry _safe_gh_jq "repos/${repo}/rules/branches/${ruleset_base_enc}?per_page=100" 2>/dev/null)" || return 2
	ruleset_contexts_json="$(printf '%s' "${rules_json}" | jq -c '
		if type == "array" then
			[.[] | select(type == "object" and .type == "required_status_checks")
				| (.parameters.required_status_checks // [])[] | .context // empty | strings]
			| unique
		else error("unexpected rules payload") end
	' 2>/dev/null)" || return 2
	[ -n "${ruleset_contexts_json}" ] || return 2
	if [ "${ruleset_contexts_json}" = "[]" ]; then
		return 0
	fi

	local combined_status_json
	combined_status_json="$(gh_retry _safe_gh_jq "repos/${repo}/commits/${head_sha}/status?per_page=100" 2>/dev/null)" || return 2
	printf '%s' "${combined_status_json}" | jq -e 'type == "object"' >/dev/null 2>&1 || return 2

	# Check-runs are fetched only when a required context has no commit status.
	local ruleset_check_runs_json='[]'
	if printf '%s' "${combined_status_json}" | jq -e --argjson req "${ruleset_contexts_json}" '
		[(.statuses // [])[] | .context] as $seen | [$req[] as $ctx | select(($seen | index($ctx)) == null)] | length > 0
	' >/dev/null 2>&1; then
		ruleset_check_runs_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${repo}/commits/${head_sha}/check-runs?per_page=100" 2>/dev/null)" || return 2
		ruleset_check_runs_json="$(printf '%s' "${ruleset_check_runs_json}" | jq -c '
			if type == "array" then [.[]? | select(type == "object") | (.check_runs // [])[]]
			elif (type == "object" and (.check_runs | type == "array")) then .check_runs
			else error("unexpected check-runs payload") end
		' 2>/dev/null)" || return 2
	fi

	PR_RULESET_BLOCKERS_SUMMARY="$(printf '%s' "${combined_status_json}" | jq -r \
		--argjson req "${ruleset_contexts_json}" --argjson runs "${ruleset_check_runs_json}" '
		(.statuses // []) as $statuses |
		[$req[] as $ctx |
			([$statuses[] | select(.context == $ctx)] | first) as $st |
			if $st != null then
				(if $st.state == "success" then empty else "\($ctx) (\($st.state))" end)
			else
				([$runs[] | select(.name == $ctx)] | max_by([(.id // 0), (.started_at // "")])) as $run |
				if $run == null then "\($ctx) (missing)"
				elif $run.status != "completed" then "\($ctx) (pending)"
				elif ($run.conclusion == "success" or $run.conclusion == "neutral" or $run.conclusion == "skipped") then empty
				else "\($ctx) (\($run.conclusion // "unknown"))" end
			end
		] | join(", ")
	' 2>/dev/null)" || { PR_RULESET_BLOCKERS_SUMMARY=""; return 2; }

	[ -n "${PR_RULESET_BLOCKERS_SUMMARY}" ] && return 1
	return 0
}

# ---------------------------------------------------------------------------
# _pr_sync_merge_blocked_guard <pr_json|pr_number> [base_ref] [blockers_hint]
#
# Issue #9939. Refuses a synchronous (non --auto) `gh pr merge` while GitHub
# reports the PR as mergeable_state=blocked. The pipeline merges with the
# owner's admin GH_PAT, and a direct merge of a blocked PR by an admin is a
# ruleset bypass: every merge on main from 2026-11-21 12:23 UTC landed that
# way (rule-suite log: `Required status check "lint" is expected.`), because
# _pr_ruleset_required_check_blockers matches by name and counted the
# dispatched final CI's `lint` check-run, which GitHub never evaluates for
# the PR. Callers defer to their existing retry (next poll, the judge firing
# again, or GitHub's own auto-merge) instead.
#
# Input:  $1 the PR's REST JSON (a value starting with `{`, used as it is),
#         or a PR number (digits; then 1 GET pulls/<n>).
#         $2 optional base ref; wins over .base.ref.
#         $3 optional blockers summary the caller already computed; when
#         given, no ruleset lookup is made.
# Output: SYNC_MERGE_BLOCKED_GUARD_STATE (mergeable_state, or none /
#         unreadable), SYNC_MERGE_BLOCKED_GUARD_BLOCKERS.
# Return: 0 proceed, 1 defer.
# Decision:
#   blocked                  -> defer; blockers from $3, else from
#                               _pr_ruleset_required_check_blockers
#                               (rc 1 -> its summary, else `unknown`).
#   unknown / null / missing -> defer (GitHub is still computing it).
#   unreadable input         -> defer (fails closed: merging past a rule is
#                               security-sensitive).
#   anything else (clean, unstable, has_hooks, behind, dirty, draft)
#                            -> proceed, no output; GitHub itself refuses
#                               dirty and draft.
# Deferral log (one line; blockers last because the summary has spaces):
#   SYNC_MERGE_BLOCKED_GUARD outcome=deferred pr=<n> head=<sha|unknown>
#     state=<blocked|unknown|none|unreadable> blockers=<summary|unknown|none>
# Switch: PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED (default true). Only false / 0 /
# no / off (any case) disables it; then it logs
# `SYNC_MERGE_BLOCKED_GUARD outcome=disabled pr=<n>` and returns 0 with no
# API call. A consumer that relies on admin-bypass merges sets it to false.
# API calls (§15): 0 for JSON input, 1 GET for a PR number, plus the 1-3
# GETs of _pr_ruleset_required_check_blockers only when the state is
# blocked and no hint is given.
# Issue #9993 (G3): when the blockers are exactly
# `<FINAL_CI_WIRING_STATUS_CONTEXT> (missing)` (default `ci/final`), the
# deferral also calls _pr_final_ci_wiring_behind_update ... blocked_status
# (its own budget; it acts only on a head whose final CI run succeeded). The
# guard's return value and its SYNC_MERGE_BLOCKED_GUARD line are unchanged.
# ---------------------------------------------------------------------------
_pr_sync_merge_blocked_guard()
{
	local guard_input="${1:-}"
	local guard_base_ref="${2:-}"
	local guard_blockers_hint="${3:-}"
	local guard_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local guard_pr_json="" guard_pr="" guard_head="" guard_state="" guard_blockers=""
	SYNC_MERGE_BLOCKED_GUARD_STATE=""
	SYNC_MERGE_BLOCKED_GUARD_BLOCKERS=""

	if [[ "${guard_input}" =~ ^[0-9]+$ ]]; then
		guard_pr="${guard_input}"
	fi

	local guard_enabled="${PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED:-true}"
	case "$(printf '%s' "${guard_enabled}" | tr '[:upper:]' '[:lower:]')" in
		false|0|no|off)
			if [ -z "${guard_pr}" ] && [ "${guard_input:0:1}" = "{" ]; then
				guard_pr="$(printf '%s' "${guard_input}" | jq -r '.number // empty' 2>/dev/null || true)"
			fi
			echo "SYNC_MERGE_BLOCKED_GUARD outcome=disabled pr=${guard_pr:-unknown}"
			return 0
			;;
	esac

	if [ -n "${guard_pr}" ]; then
		if [ -n "${guard_repo}" ]; then
			guard_pr_json="$(gh_retry _safe_gh_jq "repos/${guard_repo}/pulls/${guard_pr}" 2>/dev/null)" || guard_pr_json=""
		fi
	elif [ "${guard_input:0:1}" = "{" ]; then
		guard_pr_json="${guard_input}"
	fi

	if [ -z "${guard_pr_json}" ] || ! printf '%s' "${guard_pr_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		guard_state="unreadable"
	else
		[ -n "${guard_pr}" ] || guard_pr="$(printf '%s' "${guard_pr_json}" | jq -r '(.number // empty) | tostring' 2>/dev/null || true)"
		guard_head="$(printf '%s' "${guard_pr_json}" | jq -r '.head.sha // empty' 2>/dev/null || true)"
		[[ "${guard_head}" =~ ^[0-9a-f]{40}$ ]] || guard_head=""
		[ -n "${guard_base_ref}" ] || guard_base_ref="$(printf '%s' "${guard_pr_json}" | jq -r '.base.ref // empty' 2>/dev/null || true)"
		guard_state="$(printf '%s' "${guard_pr_json}" | jq -r '.mergeable_state // empty' 2>/dev/null || true)"
		[[ "${guard_state}" =~ ^[a-z_]+$ ]] || guard_state=""
		[ -n "${guard_state}" ] || guard_state="none"
	fi

	case "${guard_state}" in
		blocked)
			if [ -n "${guard_blockers_hint}" ]; then
				guard_blockers="${guard_blockers_hint}"
			elif [ -n "${guard_head}" ] && declare -F _pr_ruleset_required_check_blockers >/dev/null 2>&1; then
				local guard_ruleset_rc=0
				_pr_ruleset_required_check_blockers "${guard_head}" "${guard_base_ref}" || guard_ruleset_rc=$?
				if [ "${guard_ruleset_rc}" = "1" ] && [ -n "${PR_RULESET_BLOCKERS_SUMMARY:-}" ]; then
					guard_blockers="${PR_RULESET_BLOCKERS_SUMMARY}"
				else
					guard_blockers="unknown"
				fi
			else
				guard_blockers="unknown"
			fi
			;;
		unknown|none|unreadable)
			guard_blockers="unknown"
			;;
		*)
			SYNC_MERGE_BLOCKED_GUARD_STATE="${guard_state}"
			return 0
			;;
	esac

	SYNC_MERGE_BLOCKED_GUARD_STATE="${guard_state}"
	SYNC_MERGE_BLOCKED_GUARD_BLOCKERS="${guard_blockers}"
	echo "SYNC_MERGE_BLOCKED_GUARD outcome=deferred pr=${guard_pr:-unknown} head=${guard_head:-unknown} state=${guard_state} blockers=${guard_blockers}"
	# Issue #9993 (G3): a green head blocked only by a missing final-CI status
	# may sit on a branch whose workflow copy cannot post it; route it to the
	# CI-wiring update. The guard still defers.
	if [ "${guard_state}" = "blocked" ] && [ -n "${guard_head}" ] && [ -n "${guard_pr}" ] \
		&& [ "${guard_blockers}" = "${FINAL_CI_WIRING_STATUS_CONTEXT:-ci/final} (missing)" ] \
		&& declare -F _pr_final_ci_wiring_behind_update >/dev/null 2>&1; then
		_pr_final_ci_wiring_behind_update "${guard_pr}" "${guard_head}" "${guard_base_ref}" "${REVIEW_FINAL_CI_WORKFLOW:-ci.yml}" blocked_status || true
	fi
	return 1
}

# ---------------------------------------------------------------------------
# Required-checks wait before enabling auto-merge (2026-10-09).
#
# `gh pr merge --auto` merges as soon as the base branch's required status
# checks pass. A base branch with no branch protection has none, so the
# merge happens the moment auto-merge is enabled, whatever CI says: #6906
# landed on `main` while its own pull-request CI run (37924079118) already
# showed `orchestrate-poll (3)` failing on the merge ref, and `main` went
# red. The orchestrator's direct merges already gate on
# `_pr_checks_completed`; the review workflow's auto-merge paths did not.
#
#   _pr_wait_for_required_checks <pr> <head_sha> <base_ref> [<max_minutes>]
#       Polls `_pr_checks_completed` (with PR_CHECKS_SELF_RUN_ID so the
#       calling run's own check-runs never count) until the required set is
#       green, a required check has failed with nothing left running, the
#       budget is spent, or the head cannot be resolved. A failed
#       check-runs query is retried until the budget is spent (a transient
#       API error must not refuse a green PR). Returns 0 only when the gate
#       reports ok (or allow-all). Sets PR_CHECKS_WAIT_WAITED_S to the
#       seconds slept and PR_CHECKS_WAIT_OUTCOME to
#       ok|allow_all|failed|timeout|query_failed|unresolved_head_sha and
#       logs `AUTOFIX_AUTO_MERGE_CHECKS pr=<n> head_sha=<sha> outcome=<...>
#       waited_s=<n> pending=<n> ci_check=<name> ci_state=<state>`.
#
# Env: AUTO_MERGE_CHECKS_WAIT_MINUTES (default 45; 0 = one check, no wait),
# AUTO_MERGE_CHECKS_POLL_SECONDS (default 60). One check-runs listing (plus
# the branch-protection read) per poll, and, while a named CI check is
# required, one `actions/runs?head_sha=` read per poll to bind it to a CI run.
# With REVIEW_FINAL_CI_WORKFLOW set (cut-ci-runner-time Phase 4) and the
# named CI check absent on the head, or every CI run on it cancelled, the
# first poll calls `_pr_ensure_final_ci` once with that poll's runs listing
# (no extra read; one `GET pulls/<pr>` when PR_CHECKS_HEAD_REF is unset, and
# at most one dispatch), then keeps waiting for the dispatched run.
#
# Named CI check (issue #6963, AUTO_MERGE_REQUIRED_CI_CHECK). An empty
# check-run list used to count as green after two grace intervals, and a
# non-CI check (for example an earlier run's `review / gate` success on the
# same head) could satisfy the required set although CI never ran. When a
# CI check name is configured, the wait returns ok only once a check-run of
# exactly that name exists on the reviewed head and every such run
# succeeded: absent/pending/unknown keep polling until the budget is spent
# (outcome timeout), and a failed, skipped, neutral or cancelled run ends
# the wait with outcome failed. The zero-check-runs grace applies only when
# the requirement is off. Every AUTOFIX_AUTO_MERGE_CHECKS line ends with
# `ci_check=<name> ci_state=<state>`. Actions pull_request CI records its
# check-runs on the PR head SHA (the job checks out the test-merge ref), so
# the reviewed-head binding and the callers' --match-head-commit stay as
# they are.

# Resolve AUTO_MERGE_REQUIRED_CI_CHECK. Prints the check-run name to require,
# or `none` when the requirement is off. Unset or empty means the repository
# default: `lint` (the aggregate job of ci.yml) in shubhodeep1/coding-workflows,
# `none` elsewhere, because consumer CI check names are unknown. Only an
# explicit `none` (any case) disables the requirement, since a repository
# variable cannot hold an empty string and the workflow passes `|| ''`. A
# value with a comma or control character, or longer than 100 characters,
# warns and falls back to the repository default.
# Optional $1: the PR's base ref, kept for the callers' signature. Every PR,
# for every base since issue #7283, gets one full ci.yml run when review
# finishes with its head (cut-ci-runner-time Phase 4), so the `lint`
# default applies to every base: a PR stacked on another branch (a fix PR
# routed to a failing PR's ai/issue-* head, a heal PR) waits for its own CI
# `lint` before it merges. An explicitly configured name also applies to
# every base.
_pr_auto_merge_required_ci_check_name()
{
	local raw="${AUTO_MERGE_REQUIRED_CI_CHECK-}"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	# shellcheck disable=SC2034  # base ref accepted for the callers' signature (#7283)
	local ci_check_base_ref="${1:-}"
	local default_name="none"
	case "$(printf '%s' "${repo}" | tr '[:upper:]' '[:lower:]')" in
		shubhodeep1/coding-workflows) default_name="lint" ;;
	esac
	raw="${raw#"${raw%%[![:space:]]*}"}"
	raw="${raw%"${raw##*[![:space:]]}"}"
	if [ -z "${raw}" ]; then
		echo "${default_name}"
		return 0
	fi
	if [ "$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')" = "none" ]; then
		echo "none"
		return 0
	fi
	if [ "${#raw}" -gt 100 ] || [[ "${raw}" == *,* ]] || [[ "${raw}" =~ [[:cntrl:]] ]]; then
		# Fail closed: an operator who set a value meant to require a check, so
		# an invalid value never turns the requirement off. With no repository
		# default, the sentinel `__invalid__` keeps it on and unsatisfiable.
		[ "${default_name}" = "none" ] && default_name="__invalid__"
		echo "::warning::AUTO_MERGE_REQUIRED_CI_CHECK is not a valid check-run name (comma, control character or over 100 characters); using '${default_name}'." >&2
		echo "${default_name}"
		return 0
	fi
	echo "${raw}"
}

# `_pr_head_ci_runs_pending <head_sha>` sets PR_CHECKS_RUNS_PENDING to the
# number of workflow runs on <head_sha> whose workflow name is listed in
# AUTO_MERGE_WAIT_WORKFLOWS (comma-separated, default "CI"; "none" disables)
# (matched case-insensitively, as the "none"/"off" check is) and whose
# status is not completed, the caller's own run excluded.
# Why: a workflow run exists (queued) as soon as the push event fires, but
# its jobs' check-runs appear only when they start. A fast check that
# already passed (the PR-body lint) made the head look green while CI was
# still queued, so the required-checks wait could return before CI ran.
# It also sets PR_CHECKS_RUNS_UNRUN to the number of listed workflows whose
# every run on <head_sha> completed as cancelled or startup_failure: such a
# run (for example cancelled while still queued) leaves no check-runs, so
# the check-run gate alone would read the head as green although CI never
# produced a result. The wait refuses with reason=ci_run_cancelled; a
# later re-run returns the workflow to pending.
# A run's `name` is its run name, so a `workflow_dispatch` final CI run of
# ci.yml is named `CI [pr:<n>]` (or `CI [pr:<n>] [<mode>]`), not `CI`
# (cut-ci-runner-time Phase 4). A `workflow_dispatch` run whose name is a
# listed workflow name followed by `[pr:<n>]` and an optional `[<mode>]`
# counts as a run of that workflow (PR_CHECKS_JQ_WAIT_WORKFLOW_KEY_DEF), so
# the final run's `lint` binds and a queued final run counts as pending.
# Without this, a head whose only CI run is its final run never binds
# `lint` and the wait always timed out (`ci_state=absent`).
# Input: head SHA. Output: PR_CHECKS_RUNS_PENDING, PR_CHECKS_RUNS_UNRUN
# (integers), PR_CHECKS_RUNS_IDS (comma-separated run ids of the listed
# workflows on the head, self excluded) and PR_CHECKS_RUNS_READ
# (true | false when the read failed or was skipped | disabled), return 0.
# API calls: one REST `actions/runs?head_sha=` read (per_page=100); none
# when disabled or the SHA/repository is unusable. Fail-open: a failed or
# malformed read counts as 0 pending (the check-run gate still applies).
# jq def shared by both _pr_head_ci_runs_pending filters: `_wait_wf_key`
# maps a workflow run to the listed workflow name it belongs to (lowercase),
# or "" when it belongs to none. `$wanted` is the lowercased name list.
PR_CHECKS_JQ_WAIT_WORKFLOW_KEY_DEF='
	def _wait_wf_key($wanted):
		(.name // "" | ascii_downcase) as $n
		| if ($wanted | index($n)) != null then $n
		elif (.event // "") == "workflow_dispatch" then
			([$n | capture("^(?<base>.+) \\[pr:[1-9][0-9]*\\]( \\[[a-z0-9-]+\\])?$")? | .base] | first) as $base
			| if ($base != null) and (($wanted | index($base)) != null) then $base else "" end
		else "" end;
'

_pr_head_ci_runs_pending()
{
	local head_sha="$1" repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" names_csv runs_json self_run
	PR_CHECKS_RUNS_PENDING=0
	PR_CHECKS_RUNS_UNRUN=0
	PR_CHECKS_RUNS_READ="false"
	PR_CHECKS_RUNS_IDS=""
	PR_CHECKS_RUNS_RAW_JSON=""
	names_csv="${AUTO_MERGE_WAIT_WORKFLOWS-CI}"
	case "$(printf '%s' "${names_csv}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
		""|none|off) PR_CHECKS_RUNS_READ="disabled"; return 0 ;;
	esac
	[[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] && [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 0
	self_run="${PR_CHECKS_SELF_RUN_ID:-${GITHUB_RUN_ID:-}}"
	runs_json="$(gh_retry _safe_gh_jq "repos/${repo}/actions/runs?head_sha=${head_sha}&per_page=100" 2>/dev/null || echo "")"
	# Kept for _pr_ensure_final_ci (cut-ci-runner-time Phase 4), so the
	# final-CI check in _pr_wait_for_required_checks reuses this listing
	# instead of reading the runs again (§15).
	PR_CHECKS_RUNS_RAW_JSON="${runs_json}"
	local counts
	counts="$(printf '%s' "${runs_json}" | jq -r --arg names "${names_csv}" --arg self_run "${self_run}" "${PR_CHECKS_JQ_WAIT_WORKFLOW_KEY_DEF}"'
		($names | split(",") | map(gsub("^\\s+|\\s+$"; "") | ascii_downcase) | map(select(length > 0))) as $wanted
		| [(.workflow_runs // [])[]
			| select((.id | tostring) != $self_run)
			| select(_wait_wf_key($wanted) != "")] as $runs
		| ($runs | map(select(.status != "completed")) | length) as $pending
		| ($runs | group_by(_wait_wf_key($wanted))
			| map(select(all(.[]; .status == "completed" and (.conclusion == "cancelled" or .conclusion == "startup_failure"))))
			| length) as $unrun
		| "\($pending) \($unrun)"' 2>/dev/null | tail -n1)"
	PR_CHECKS_RUNS_PENDING="${counts%% *}"
	PR_CHECKS_RUNS_UNRUN="${counts##* }"
	[[ "${PR_CHECKS_RUNS_PENDING}" =~ ^[0-9]+$ ]] || PR_CHECKS_RUNS_PENDING=0
	[[ "${PR_CHECKS_RUNS_UNRUN}" =~ ^[0-9]+$ ]] || PR_CHECKS_RUNS_UNRUN=0
	# Run ids of the listed workflows on this head (self excluded), for the
	# named CI check binding (issue #6963). Same listing, no extra call.
	local run_ids
	run_ids="$(printf '%s' "${runs_json}" | jq -r --arg names "${names_csv}" --arg self_run "${self_run}" "${PR_CHECKS_JQ_WAIT_WORKFLOW_KEY_DEF}"'
		($names | split(",") | map(gsub("^\\s+|\\s+$"; "") | ascii_downcase) | map(select(length > 0))) as $wanted
		| if (type == "object" and (.workflow_runs | type) == "array") then
			[.workflow_runs[] | select((.id | tostring) != $self_run)
				| select(_wait_wf_key($wanted) != "") | (.id | tostring)] | "ids:" + join(",")
		else "invalid" end' 2>/dev/null | tail -n1)"
	if [[ "${run_ids}" =~ ^ids:[0-9,]*$ ]]; then
		PR_CHECKS_RUNS_READ="true"
		PR_CHECKS_RUNS_IDS="${run_ids#ids:}"
	fi
	return 0
}

# ---------------------------------------------------------------------------
# Final CI run at the end of review (cut-ci-runner-time Phase 4,
# docs/plans/cut-ci-runner-time-plan.md, Refs #9528).
#
#   _pr_ensure_final_ci <repo> <pr> <head sha> [<head ref>]
#
# Makes sure the head that review finished with has (or gets) one run of the
# workflow file named by REVIEW_FINAL_CI_WORKFLOW (default empty: the helper
# is a no-op, so consumers and callers that never set it are unaffected).
# Decision, from the runs of that workflow file on <head sha>:
#   - a run that is queued / in progress / waiting, or one that completed
#     `success` or `failure`, already covers the head (a `push` run on an
#     orchestrator/project-* head counts too): nothing is dispatched. A head
#     whose run failed is never re-dispatched, so a head review cannot fix
#     stays red instead of looping;
#   - otherwise (no run, or only cancelled / timed_out / startup_failure
#     runs) it dispatches <workflow> on <head ref> with -f pr_number=<pr>
#     -f head_sha=<head sha>.
# Inputs: the arguments above; REVIEW_FINAL_CI_WORKFLOW (a bare
# `<name>.yml` file under .github/workflows); optional PR_FINAL_CI_RUNS_JSON,
# an `actions/runs?head_sha=<sha>` listing the caller already read (the
# helper then reads nothing; runs are matched by `.path`); optional
# PR_FINAL_CI_SYNC_QUICK=true (set only by _pr_base_sync_dispatch_final_ci for
# a verified freshness-update head): a dispatch that is not docs-only passes
# -f sync_quick=true, and when the workflow refuses that input the full suite
# is dispatched once more (one `REVIEW_FINAL_CI_SYNC_QUICK pr= head=
# outcome=dispatched|dispatch_refused` line).
# CI wiring behind the base (issue #9993): before it dispatches, the helper
# calls _pr_final_ci_wiring_behind_update <pr> <sha> <base ref> <workflow>
# dispatch, unless PR_FINAL_CI_WIRING_SKIP=true (set only by
# _pr_base_sync_dispatch_final_ci for a verified update head).
# scripts/review_enable_auto_merge.sh sets PR_FINAL_CI_WIRING_NO_UPDATE=true
# on its two exits that never merge (auto-merge off, e2e-smoke-test): the
# check still runs and a behind head is held (no dispatch on the stale
# workflow), but it takes no merge lease and gets no new head mid-gate. The base ref is
# the optional PR_FINAL_CI_BASE_REF, else PR_BASE_FRESHNESS_BASE_REF when the
# freshness side channel is for <sha>, else `.base.ref` of the PR read (made
# only on this dispatch path, and shared with the head-ref read). When that
# helper updated the branch, asked for an update, or holds it (lease, refused
# update, recorded refusal), nothing is dispatched on this head:
# PR_FINAL_CI_OUTCOME=deferred, PR_FINAL_CI_REASON=ci_wiring_behind and one
# `REVIEW_FINAL_CI pr= head= outcome=deferred reason=ci_wiring_behind` line.
# Any other result (not behind, switch off, comparison failed) dispatches as
# before (fail-open).
# Outputs: PR_FINAL_CI_OUTCOME = dispatched | exists | disabled | deferred |
# dispatch_failed | invalid_input; PR_FINAL_CI_REASON = the decision's reason
# token (in_flight, success, failure, no_run, ci_wiring_behind, ...; empty
# when there was none);
# PR_FINAL_CI_FAILED_AT = the newest failed full run's `updated_at` when the
# reason is `failure` (empty otherwise); one `REVIEW_FINAL_CI pr= head= outcome=
# reason=` line per decision (plus `outcome=read_failed action=dispatch` when
# the listing could not be read). Always returns 0 except for invalid input
# (1); it never fails its caller's step.
# API calls (§15): one `GET actions/workflows/<file>/runs?head_sha=<sha>&per_page=20`
# unless PR_FINAL_CI_RUNS_JSON is given; one `GET pulls/<pr>` only when
# <head ref> is empty; one `GET pulls/<pr>/files?per_page=100` only when it
# dispatches (docs-only final CI, run-volume plan item 4; one
# `REVIEW_FINAL_CI_DOCS_ONLY pr= head= docs_only= reason=` line); at most one
# `workflow_dispatch`, or two when a docs_only=true dispatch is refused and
# the full-suite dispatch is tried once more. On the dispatch path only, the
# wiring check above (§14/§15 API hygiene): 0 calls when the freshness side
# channel covers <sha>, else one `GET pulls/<pr>` when no base ref is known
# plus the helper's own budget (see _pr_final_ci_wiring_behind_update).
# Fail-open: a failed
# read dispatches anyway, because a duplicate run is harmless and a missing
# run blocks the merge (the required `lint` check never appears).
# The final-CI decision over an `actions/.../runs` listing ($path, $sha):
# "exists in_flight|success|failure", "dispatch sync_quick_failed|
# only_unfinished|no_run" or "read_failed". Shared with the review sweep's
# review_sweep_final_ci_state (scripts/review_sweep_head_gate.sh), so the two
# cannot drift apart.
PR_FINAL_CI_JQ_DECISION_DEF='def _pr_final_ci_decision:
		if (type == "object" and (.workflow_runs | type) == "array") then
			[.workflow_runs[]
				| select(type == "object")
				| select(((.path // "") | split("@") | .[0]) == $path)
				| select((.head_sha // "") == $sha)] as $runs
			| ($runs | map(select(((.display_title // "") | endswith(" [sync-quick]")) | not))) as $full_runs
			| if any($runs[]; (.status // "") != "completed") then "exists in_flight"
			elif any($runs[]; (.conclusion // "") == "success") then "exists success"
			elif any($full_runs[]; (.conclusion // "") == "failure") then "exists failure"
			elif any($runs[]; (.conclusion // "") == "failure") then "dispatch sync_quick_failed"
			elif ($runs | length) > 0 then "dispatch only_unfinished"
			else "dispatch no_run" end
		else "read_failed" end;'

_pr_ensure_final_ci()
{
	local repo="$1" pr="$2" sha="$3" ref="${4:-}"
	local wf="${REVIEW_FINAL_CI_WORKFLOW:-}" runs_json="" decision="" reason="" pr_json="" head_repo=""
	local final_ci_docs_only="false" final_ci_docs_only_classifier="" final_ci_docs_only_files="" final_ci_docs_only_verdict=""
	PR_FINAL_CI_OUTCOME=""
	PR_FINAL_CI_REASON=""
	PR_FINAL_CI_FAILED_AT=""
	if [ -z "${wf}" ]; then
		PR_FINAL_CI_OUTCOME="disabled"
		echo "REVIEW_FINAL_CI pr=${pr:-?} head=${sha:-?} outcome=disabled reason=workflow_unset"
		return 0
	fi
	if ! [[ "${wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]] \
		|| ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${repo}" == *..* ]] \
		|| ! [[ "${pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
		PR_FINAL_CI_OUTCOME="invalid_input"
		echo "::warning::REVIEW_FINAL_CI pr=${pr:-?} head=${sha:-?} outcome=invalid_input (workflow, repository, PR number or head SHA is not usable)"
		return 1
	fi
	if [ -n "${PR_FINAL_CI_RUNS_JSON:-}" ]; then
		runs_json="${PR_FINAL_CI_RUNS_JSON}"
	else
		runs_json="$(gh_retry _safe_gh_jq "repos/${repo}/actions/workflows/${wf}/runs?head_sha=${sha}&per_page=20" 2>/dev/null || echo "")"
	fi
	decision="$(printf '%s' "${runs_json}" | jq -r --arg path ".github/workflows/${wf}" --arg sha "${sha}" "${PR_FINAL_CI_JQ_DECISION_DEF}"' _pr_final_ci_decision' 2>/dev/null | tail -n1)"
	case "${decision}" in
		"exists "*)
			PR_FINAL_CI_OUTCOME="exists"
			PR_FINAL_CI_REASON="${decision#exists }"
			if [ "${PR_FINAL_CI_REASON}" = "failure" ]; then
				# Newest failed full run's completion time, for the review sweep's
				# base sync (scripts/review_final_ci_sweep.sh); no extra read.
				PR_FINAL_CI_FAILED_AT="$(printf '%s' "${runs_json}" | jq -r --arg path ".github/workflows/${wf}" --arg sha "${sha}" '
					[.workflow_runs[]? | select(type == "object")
						| select(((.path // "") | split("@") | .[0]) == $path and (.head_sha // "") == $sha)
						| select((.conclusion // "") == "failure" and (((.display_title // "") | endswith(" [sync-quick]")) | not))
						| (.updated_at // "")] | map(select(test("^[0-9]{4}-"))) | max // ""' 2>/dev/null | tail -n1)"
			fi
			echo "REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=exists reason=${PR_FINAL_CI_REASON}"
			return 0
			;;
		"dispatch "*) reason="${decision#dispatch }"; PR_FINAL_CI_REASON="${reason}" ;;
		*)
			reason="read_failed"
			echo "REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=read_failed action=dispatch"
			;;
	esac
	if [ -z "${ref}" ]; then
		pr_json="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr}" 2>/dev/null || echo "")"
		ref="$(printf '%s' "${pr_json}" | jq -r 'if type == "object" then (.head.ref // "") else "" end' 2>/dev/null | tail -n1)"
		head_repo="$(printf '%s' "${pr_json}" | jq -r 'if type == "object" then (.head.repo.full_name // "") else "" end' 2>/dev/null | tail -n1)"
		if [ -n "${head_repo}" ] && [ "${head_repo}" != "${repo}" ]; then
			PR_FINAL_CI_OUTCOME="dispatch_failed"
			echo "::warning::REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=dispatch_failed reason=fork_head (a workflow can only be dispatched on a branch of ${repo})"
			return 0
		fi
	fi
	if ! [[ "${ref}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${ref}" == *..* ]] || [[ "${ref}" == -* ]]; then
		PR_FINAL_CI_OUTCOME="dispatch_failed"
		echo "::warning::REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=dispatch_failed reason=head_ref_unavailable"
		return 0
	fi
	# CI wiring behind the base (issue #9993): GitHub runs the head branch's
	# own copy of <workflow> while its jobs run the base's scripts, so a branch
	# that lacks the base's commits to that file gets a false failure or no
	# `ci/final`. Update such a branch instead of dispatching on it.
	if [ "${PR_FINAL_CI_WIRING_SKIP:-}" != "true" ] && declare -F _pr_final_ci_wiring_behind_update >/dev/null 2>&1; then
		local final_ci_wiring_base=""
		# The base ref is resolved only with the switch on, so off makes no call.
		if _pr_final_ci_wiring_enabled; then
			final_ci_wiring_base="${PR_FINAL_CI_BASE_REF:-}"
			if [ -z "${final_ci_wiring_base}" ] && [ "${PR_BASE_FRESHNESS_HEAD_SHA:-}" = "${sha}" ]; then
				final_ci_wiring_base="${PR_BASE_FRESHNESS_BASE_REF:-}"
			fi
			if [ -z "${final_ci_wiring_base}" ]; then
				[ -n "${pr_json}" ] || pr_json="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr}" 2>/dev/null || echo "")"
				final_ci_wiring_base="$(printf '%s' "${pr_json}" | jq -r 'if type == "object" then (.base.ref // "") else "" end' 2>/dev/null | tail -n1)"
			fi
		fi
		# An e2e-smoke-test PR's branch is never updated mid-gate: the review
		# step's e2e exit is check-only, and the final-CI sweep reaches the same
		# head without that flag. The labels come from the PR read above (the
		# sweep path always makes it), so this costs no call.
		local final_ci_wiring_no_update="${PR_FINAL_CI_WIRING_NO_UPDATE:-}"
		if [ -n "${pr_json}" ] && printf '%s' "${pr_json}" | jq -e 'type == "object"
			and ([(.labels // [])[]? | select(type == "object") | (.name // "")] | any(. == "e2e-smoke-test"))' >/dev/null 2>&1; then
			final_ci_wiring_no_update="true"
		fi
		PR_FINAL_CI_WIRING_NO_UPDATE="${final_ci_wiring_no_update}" \
			_pr_final_ci_wiring_behind_update "${pr}" "${sha}" "${final_ci_wiring_base}" "${wf}" dispatch || true
		case "${PR_FINAL_CI_WIRING_OUTCOME:-}" in
			updated|requested|held)
				PR_FINAL_CI_OUTCOME="deferred"
				PR_FINAL_CI_REASON="ci_wiring_behind"
				echo "REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=deferred reason=ci_wiring_behind"
				return 0
				;;
		esac
	fi
	# Docs-only final CI (run-volume plan item 4): a PR whose changed files
	# are all on the scripts/ci_docs_only_paths.sh list gets docs_only=true,
	# so ci.yml skips its test jobs and `lint` re-checks the merged tree.
	# Anything unreadable, a full first page (100 files, maybe more), or a
	# missing classifier counts as code and dispatches the full suite.
	final_ci_docs_only="false"
	final_ci_docs_only_classifier="${BASH_SOURCE[0]%/*}/ci_docs_only_paths.sh"
	if [ -f "${final_ci_docs_only_classifier}" ]; then
		final_ci_docs_only_files="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr}/files?per_page=100" 2>/dev/null || echo "")"
		[ -n "${final_ci_docs_only_files}" ] && final_ci_docs_only_verdict="$(printf '%s' "${final_ci_docs_only_files}" \
			| jq -j 'if type == "array" and length > 0 and length < 100 and all(.[]; type == "object" and ((.filename // null) | type) == "string" and ((.previous_filename // "") | type) == "string") then .[] | .filename, (.previous_filename // empty) | (. + "\u0000") else empty end' 2>/dev/null \
			| bash "${final_ci_docs_only_classifier}" classify 2>/dev/null)"
		# The whole output must be the one word: anything else is code.
		[ "${final_ci_docs_only_verdict}" = "docs_only" ] && final_ci_docs_only="true"
		final_ci_docs_only_verdict="${final_ci_docs_only_verdict%%[[:space:]]*}"
		echo "REVIEW_FINAL_CI_DOCS_ONLY pr=${pr} head=${sha} docs_only=${final_ci_docs_only} reason=${final_ci_docs_only_verdict:-files_unreadable}"
	else
		echo "REVIEW_FINAL_CI_DOCS_ONLY pr=${pr} head=${sha} docs_only=false reason=classifier_missing"
	fi
	if [ "${final_ci_docs_only}" = "true" ]; then
		if gh workflow run "${wf}" --repo "${repo}" --ref "${ref}" -f pr_number="${pr}" -f head_sha="${sha}" -f docs_only=true >/dev/null 2>&1; then
			PR_FINAL_CI_OUTCOME="dispatched"
			echo "REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=dispatched reason=${reason} workflow=${wf}"
			return 0
		fi
		# A branch whose workflow predates the docs_only input refuses the
		# dispatch; the full-suite dispatch below still covers the head (a
		# duplicate run after a lost success response is harmless).
		echo "::warning::REVIEW_FINAL_CI_DOCS_ONLY pr=${pr} head=${sha} docs_only=dispatch_refused (retrying once without docs_only)"
	fi
	# Sync-quick final CI (merge-base sync churn): ci.yml's tests-sync-quick
	# re-checks the claim, so a wrong flag fails CI instead of skipping tests.
	# A failed sync-quick run is never answered with another quick run.
	if [ "${PR_FINAL_CI_SYNC_QUICK:-false}" = "true" ] && [ "${reason}" != "sync_quick_failed" ]; then
		if gh workflow run "${wf}" --repo "${repo}" --ref "${ref}" -f pr_number="${pr}" -f head_sha="${sha}" -f sync_quick=true >/dev/null 2>&1; then
			PR_FINAL_CI_OUTCOME="dispatched"
			echo "REVIEW_FINAL_CI_SYNC_QUICK pr=${pr} head=${sha} outcome=dispatched"
			echo "REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=dispatched reason=${reason} workflow=${wf}"
			return 0
		fi
		echo "::warning::REVIEW_FINAL_CI_SYNC_QUICK pr=${pr} head=${sha} outcome=dispatch_refused (retrying once with the full suite)"
	fi
	# One attempt: a retry after a lost success response would start a
	# second run; the review sweep's safety net retries a failed dispatch.
	if gh workflow run "${wf}" --repo "${repo}" --ref "${ref}" -f pr_number="${pr}" -f head_sha="${sha}" >/dev/null 2>&1; then
		PR_FINAL_CI_OUTCOME="dispatched"
		echo "REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=dispatched reason=${reason} workflow=${wf}"
		return 0
	fi
	PR_FINAL_CI_OUTCOME="dispatch_failed"
	# A branch created before ci.yml had its workflow_dispatch trigger
	# cannot be dispatched; updating the branch from its base fixes that.
	PR_FINAL_CI_REASON="dispatch_refused"
	echo "::warning::REVIEW_FINAL_CI pr=${pr} head=${sha} outcome=dispatch_failed reason=dispatch_refused workflow=${wf} (if the branch predates the workflow's workflow_dispatch trigger, update it from its base branch)"
	return 0
}

_pr_wait_for_required_checks()
{
	local pr_number="$1"
	local head_sha="${2:-}"
	local base_ref="${3:-}"
	local max_minutes="${4:-${AUTO_MERGE_CHECKS_WAIT_MINUTES:-45}}"
	local poll_seconds="${AUTO_MERGE_CHECKS_POLL_SECONDS:-60}"
	local waited=0 rc=0 ci_runs_fresh=0 ci_bind ci_required_name final_ci_checked=0
	ci_required_name="$(_pr_auto_merge_required_ci_check_name "${base_ref}")"
	PR_CHECKS_WAIT_OUTCOME="timeout"
	PR_CHECKS_WAIT_WAITED_S=0
	[[ "${max_minutes}" =~ ^[0-9]+$ ]] || max_minutes=45
	[[ "${poll_seconds}" =~ ^[1-9][0-9]*$ ]] || poll_seconds=60
	while :; do
		rc=0
		# Bind the named CI check to a run of an AUTO_MERGE_WAIT_WORKFLOWS
		# workflow on this head (issue #6963): other workflows also have a
		# job named `lint` (lint-plan-archival.yml; the PR-body lint job
		# too on a branch that predates its rename to `pr-body-lint`),
		# and their success must not stand in for CI. With the runs listing
		# unreadable, a success is not trusted (state unknown, keep polling);
		# with AUTO_MERGE_WAIT_WORKFLOWS=none the match is by name only.
		ci_runs_fresh=0
		ci_bind="__unbound__"
		if [ "${ci_required_name}" != "none" ]; then
			_pr_head_ci_runs_pending "${head_sha}"
			ci_runs_fresh=1
			[ "${PR_CHECKS_RUNS_READ}" = "true" ] && ci_bind="${PR_CHECKS_RUNS_IDS}"
		fi
		PR_CHECKS_CI_RUN_IDS="${ci_bind}" PR_CHECKS_SELF_RUN_ID="${PR_CHECKS_SELF_RUN_ID:-${GITHUB_RUN_ID:-}}" \
			_pr_checks_completed "${pr_number}" "${head_sha}" "${base_ref}" >/dev/null || rc=$?
		if [ "${ci_runs_fresh}" = "1" ] && [ "${PR_CHECKS_RUNS_READ}" = "false" ] && [ "${PR_CHECKS_LAST_CI_STATE}" = "success" ]; then
			PR_CHECKS_LAST_CI_STATE="unknown"
		fi
		# Final CI at the end of review (cut-ci-runner-time Phase 4): when the
		# required CI check is missing on the head (or every CI run on it was
		# cancelled), make sure the head gets its one full run, once per wait.
		# No-op unless REVIEW_FINAL_CI_WORKFLOW is set. Reuses the runs
		# listing _pr_head_ci_runs_pending just read (§15); PR_CHECKS_HEAD_REF
		# (optional) saves the helper's PR read.
		if [ "${final_ci_checked}" = "0" ] && [ -n "${REVIEW_FINAL_CI_WORKFLOW:-}" ] && [ "${ci_required_name}" != "none" ] \
			&& { [ "${PR_CHECKS_LAST_CI_STATE:-}" = "absent" ] || [ "${PR_CHECKS_RUNS_UNRUN:-0}" -gt 0 ]; }; then
			final_ci_checked=1
			PR_FINAL_CI_RUNS_JSON="${PR_CHECKS_RUNS_RAW_JSON:-}" PR_FINAL_CI_BASE_REF="${base_ref}" \
				_pr_ensure_final_ci "${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" \
				"${pr_number}" "${head_sha}" "${PR_CHECKS_HEAD_REF:-}" || true
			if [ "${PR_FINAL_CI_OUTCOME:-}" = "dispatched" ] && [ "${waited}" -lt $((max_minutes * 60)) ]; then
				echo "  [check-runs] PR #${pr_number}: dispatched the final CI run for ${head_sha:0:7}; waiting ${poll_seconds}s."
				sleep "${poll_seconds}"
				waited=$((waited + poll_seconds))
				PR_CHECKS_WAIT_WAITED_S="${waited}"
				continue
			fi
			# The branch's CI wiring is behind its base (issue #9993): the
			# head was updated (or waits for an update) instead of getting a
			# final CI run, so the required check cannot appear on it. Stop
			# now rather than hold the runner for the whole budget.
			if [ "${PR_FINAL_CI_OUTCOME:-}" = "deferred" ]; then
				PR_CHECKS_WAIT_OUTCOME="timeout"
				echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=timeout waited_s=${waited} pending=${PR_CHECKS_RUNS_PENDING:-0} reason=final_ci_wiring_behind ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
				return 1
			fi
			# The head has no CI run and none could be started (a branch
			# whose ci.yml predates the workflow_dispatch trigger, a fork
			# head, an unreadable head ref): the required check cannot
			# appear on this head, so stop now with the same timeout outcome
			# instead of holding the runner for the whole budget (PR #9579,
			# 2026-11-17: 45 minutes per review run, every sweep tick).
			if [ "${PR_FINAL_CI_OUTCOME:-}" = "dispatch_failed" ] && [ "${PR_CHECKS_LAST_CI_STATE:-}" = "absent" ] \
				&& [ "${PR_CHECKS_RUNS_PENDING:-0}" -eq 0 ]; then
				# A refused dispatch on a branch whose CI workflow predates the
				# workflow_dispatch trigger: update the approved branch from its
				# base once, so the update head can get its final CI.
				if [ "${PR_FINAL_CI_REASON:-}" = "dispatch_refused" ]; then
					_pr_final_ci_refused_base_update "${pr_number}" "${head_sha}" "${base_ref}" || true
				fi
				PR_CHECKS_WAIT_OUTCOME="timeout"
				echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=timeout waited_s=${waited} pending=0 reason=final_ci_dispatch_failed ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
				return 1
			fi
		fi
		case "${PR_CHECKS_LAST_REASON}" in
			ok|allow_all)
				# A listed CI workflow run on this head is still queued or
				# running: its check-runs may not exist yet, so keep waiting.
				# Not under allow_all: that sentinel ignores every check-run's
				# outcome (branch protection decides), so CI's result would be
				# ignored after the wait too.
				if [ "${PR_CHECKS_LAST_REASON}" = "ok" ]; then
					[ "${ci_runs_fresh}" = "1" ] || _pr_head_ci_runs_pending "${head_sha}"
					if [ "${PR_CHECKS_RUNS_PENDING:-0}" -gt 0 ] && [ "${waited}" -lt $((max_minutes * 60)) ]; then
						echo "  [check-runs] PR #${pr_number}: ${PR_CHECKS_RUNS_PENDING} CI workflow run(s) on ${head_sha:0:7} not completed yet; waiting ${poll_seconds}s."
						sleep "${poll_seconds}"
						waited=$((waited + poll_seconds))
						PR_CHECKS_WAIT_WAITED_S="${waited}"
						continue
					fi
					if [ "${PR_CHECKS_RUNS_PENDING:-0}" -gt 0 ]; then
						PR_CHECKS_WAIT_OUTCOME="timeout"
						echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=timeout waited_s=${waited} pending=${PR_CHECKS_RUNS_PENDING} reason=ci_run_pending ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
						if [ "${max_minutes}" -gt 0 ]; then _pr_final_ci_merge_pending_mark "${pr_number}" "${head_sha}"; fi
						return 1
					fi
					if [ "${PR_CHECKS_RUNS_UNRUN:-0}" -gt 0 ]; then
						PR_CHECKS_WAIT_OUTCOME="failed"
						echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=failed waited_s=${waited} pending=0 reason=ci_run_cancelled ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
						return 1
					fi
				fi
				# Named CI check required (issue #6963): it must exist on the
				# head and have succeeded. An empty listing never authorizes.
				if [ "${PR_CHECKS_LAST_CI_NAME:-none}" != "none" ]; then
					case "${PR_CHECKS_LAST_CI_STATE:-unknown}" in
						success) ;;
						failure)
							PR_CHECKS_WAIT_OUTCOME="failed"
							echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=failed waited_s=${waited} pending=${PR_CHECKS_LAST_PENDING:-0} ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
							return 1
							;;
						*)
							if [ "${waited}" -ge $((max_minutes * 60)) ]; then
								PR_CHECKS_WAIT_OUTCOME="timeout"
								echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=timeout waited_s=${waited} pending=${PR_CHECKS_LAST_PENDING:-0} ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
								if [ "${max_minutes}" -gt 0 ]; then _pr_final_ci_merge_pending_mark "${pr_number}" "${head_sha}"; fi
								return 1
							fi
							echo "  [check-runs] PR #${pr_number}: required CI check '${PR_CHECKS_LAST_CI_NAME}' is ${PR_CHECKS_LAST_CI_STATE:-unknown} on ${head_sha:0:7}; waiting ${poll_seconds}s (${waited}/$((max_minutes * 60))s)."
							sleep "${poll_seconds}"
							waited=$((waited + poll_seconds))
							PR_CHECKS_WAIT_WAITED_S="${waited}"
							continue
							;;
					esac
				# No check-run registered yet (fresh push, CI not queued): poll up
				# to two more intervals before reading "none" as green. A repo
				# with no CI at all still proceeds once that grace is spent. Only
				# when no named CI check is required.
				elif [ "${PR_CHECKS_LAST_REASON}" = "ok" ] && [ "${PR_CHECKS_LAST_TOTAL:-1}" = "0" ] \
					&& [ "${waited}" -lt $((poll_seconds * 2)) ] && [ "${waited}" -lt $((max_minutes * 60)) ]; then
					echo "  [check-runs] PR #${pr_number}: no check-runs registered on ${head_sha:0:7} yet; waiting ${poll_seconds}s for CI to register."
					sleep "${poll_seconds}"
					waited=$((waited + poll_seconds))
					PR_CHECKS_WAIT_WAITED_S="${waited}"
					continue
				fi
				PR_CHECKS_WAIT_OUTCOME="${PR_CHECKS_LAST_REASON}"
				echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=${PR_CHECKS_WAIT_OUTCOME} waited_s=${waited} pending=0 ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
				return 0
				;;
			blocking)
				if [ "${PR_CHECKS_LAST_PENDING:-0}" -eq 0 ]; then
					PR_CHECKS_WAIT_OUTCOME="failed"
					echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=failed waited_s=${waited} pending=0 ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
					return 1
				fi
				;;
			query_failed)
				# Transient API failure: keep polling within the budget.
				if [ "${waited}" -ge $((max_minutes * 60)) ]; then
					PR_CHECKS_WAIT_OUTCOME="query_failed"
					echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=query_failed waited_s=${waited} pending=${PR_CHECKS_LAST_PENDING:-0} ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
					return 1
				fi
				;;
			*)
				PR_CHECKS_WAIT_OUTCOME="${PR_CHECKS_LAST_REASON:-query_failed}"
				echo "::warning::AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=${PR_CHECKS_WAIT_OUTCOME} waited_s=${waited} pending=${PR_CHECKS_LAST_PENDING:-0} ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
				return 1
				;;
		esac
		if [ "${waited}" -ge $((max_minutes * 60)) ]; then
			PR_CHECKS_WAIT_OUTCOME="timeout"
			echo "AUTOFIX_AUTO_MERGE_CHECKS pr=${pr_number} head_sha=${head_sha:-unknown} outcome=timeout waited_s=${waited} pending=${PR_CHECKS_LAST_PENDING:-0} ci_check=${PR_CHECKS_LAST_CI_NAME:-none} ci_state=${PR_CHECKS_LAST_CI_STATE:-unknown}"
			if [ "${max_minutes}" -gt 0 ]; then _pr_final_ci_merge_pending_mark "${pr_number}" "${head_sha}"; fi
			return 1
		fi
		echo "  [check-runs] PR #${pr_number}: ${PR_CHECKS_LAST_PENDING:-0} required/pending check-run(s) still running; waiting ${poll_seconds}s (${waited}/$((max_minutes * 60))s)."
		sleep "${poll_seconds}"
		waited=$((waited + poll_seconds))
		PR_CHECKS_WAIT_WAITED_S="${waited}"
	done
}

# ---------------------------------------------------------------------------
# Merge-base freshness gate (operator decision Q35: A, 2026-10-09).
#
# A green PR whose base branch moved after its CI ran can merge code that
# was never tested against the current base: #6741 (issue #6729) merged on
# 2026-10-09 08:42 UTC with check-runs from the previous day, after #6549
# had changed the code its new tests exercised, and `main` CI was red for
# the next three hours. GitHub's auto-merge binds to the PR head, not to the
# base the checks ran against, and "require branches to be up to date" is
# not enabled because it costs one CI run per merged sibling on every open
# PR (the merge train holds 25-30 PRs that edit the same files).
#
# The gate re-validates only when it can matter: when the commits the base
# gained since the PR's merge-base touch a file the PR also touches. Then
# the branch is updated from the base (GitHub's update-branch merge), which
# fires `synchronize`, so CI and review run on the combined tree and the
# merge waits for that round. Base commits that touch only other files
# merge immediately, as before.
#
#   _pr_base_freshness <pr> [<head_sha>] [<base_ref>]
#       Prints exactly one word: fresh (the base gained no commits), clean
#       (the base commits touch no PR file), overlap (at least one shared
#       path, or the base-side diff is too large to prove otherwise),
#       disabled (MERGE_BASE_FRESHNESS_ENABLED is off), unknown (API
#       failure, or an unresolved head or base). Sets PR_BASE_FRESHNESS_OUTCOME to the
#       same word, plus PR_BASE_FRESHNESS_LAST_REASON,
#       PR_BASE_FRESHNESS_HEAD_SHA, PR_BASE_FRESHNESS_BEHIND_BY,
#       PR_BASE_FRESHNESS_OVERLAP and PR_BASE_FRESHNESS_BASE_SHA (the base
#       tip the compare saw, empty when its commit list was truncated), and
#       PR_BASE_FRESHNESS_BASE_PATHS (issue #9993: the compare's base-side
#       file names, one per line, `__truncated__` at 300+ files, empty when
#       the base gained no commits or the compare failed).
#   _pr_base_sync_for_fresh_ci <pr> <head_sha>
#       PUT /pulls/{n}/update-branch bound to <head_sha>; returns 0 when
#       GitHub accepted the update.
#   _pr_base_fresh_for_merge <pr> [<head_sha>] [<base_ref>]
#       The one call merge paths make. Returns 0 when the merge may go
#       ahead (fresh, clean, disabled), 1 after requesting the base update
#       on overlap (also when that request failed), so the caller skips this
#       round and lets the synchronize run re-validate, and 1 without any
#       update request on unknown (an API failure, or an unresolved head or
#       base): freshness could not be proven, so the merge defers this round
#       and the caller retries on its next poll tick or review round
#       (security-pass finding merge-freshness-unknown-proceeds; before it,
#       unknown proceeded, so a failed compare or PR-files read let a PR
#       merge without re-validating the combined tree).
#       MERGE_BASE_FRESHNESS_ENABLED=false remains the kill switch.
#
# API budget (§15): one `compare/{head}...{base}` call plus one paginated
# `pulls/{n}/files` listing per gated merge, one `pulls/{n}` read when the
# caller passes no head SHA or base ref, and the update-branch PUT on
# overlap. Log lines: `MERGE_BASE_FRESHNESS pr=<n> head_sha=<sha>
# base=<ref> outcome=<word> reason=<token> behind_by=<k> overlap=<paths|->`
# and `MERGE_BASE_SYNC pr=<n> head_sha=<sha> action=update_branch
# outcome=accepted|failed`.
#
# Env: MERGE_BASE_FRESHNESS_ENABLED (default true; true/1/yes/on,
# case-insensitive; anything else disables the gate and every merge path
# behaves as before).
#
# Soft path class (merge-lease Phase 4, Refs #9781): when every shared path
# is in MERGE_LEASE_SOFT_PATHS (see the "Soft path class" section), a
# `git merge-tree` probe of the head against the compared base tip decides:
# a clean merge gives `clean` with reason soft_paths_merge_clean; a conflict
# gives `overlap` with reason soft_paths_conflict; a probe that cannot
# decide (no complete base tip, fetch failure, a clean merge that combines
# content in a non-soft path such as a followed rename, ...) gives `overlap`
# with reason soft_paths_probe_unknown. Any shared non-soft path keeps
# reason shared_paths with no probe. PR_BASE_FRESHNESS_SOFT_OVERLAP holds
# the shared soft paths (comma-separated) in those three cases. The
# MERGE_BASE_FRESHNESS lines of _pr_base_fresh_for_merge end with
# `soft_overlap=<paths|->`. An empty MERGE_LEASE_SOFT_PATHS, `none` or
# `off` keeps the behaviour before Phase 4.
_pr_base_freshness()
{
	local pr_number="$1"
	local head_sha="${2:-}"
	local base_ref="${3:-}"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	PR_BASE_FRESHNESS_OUTCOME=""
	PR_BASE_FRESHNESS_LAST_REASON=""
	PR_BASE_FRESHNESS_HEAD_SHA=""
	PR_BASE_FRESHNESS_BEHIND_BY=""
	PR_BASE_FRESHNESS_OVERLAP=""
	PR_BASE_FRESHNESS_BASE_REF=""
	PR_BASE_FRESHNESS_BASE_SHA=""
	PR_BASE_FRESHNESS_SOFT_OVERLAP=""
	# The PR's own paths (newline-separated) and the PR they belong to, kept
	# for the merge lease (_merge_lease_gate) so it never re-reads the files.
	PR_BASE_FRESHNESS_PR_PATHS=""
	PR_BASE_FRESHNESS_PR_PATHS_FOR=""
	# The base-side paths of the compare (newline-separated; `__truncated__`
	# when it lists 300+ files), kept for _pr_final_ci_wiring_behind_update
	# (issue #9993) so it never re-reads the compare for this head.
	PR_BASE_FRESHNESS_BASE_PATHS=""
	case "$(printf '%s' "${MERGE_BASE_FRESHNESS_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) ;;
		*)
			PR_BASE_FRESHNESS_LAST_REASON="disabled"
			PR_BASE_FRESHNESS_OUTCOME="disabled"
			echo "disabled"
			return 0
			;;
	esac
	if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ] || [ -z "${base_ref}" ] || [ "${base_ref}" = "null" ]; then
		local pr_json
		pr_json="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr_number}" 2>/dev/null || echo "")"
		if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
			head_sha="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .head.sha?) then .head.sha else empty end' 2>/dev/null | tail -n1)"
		fi
		if [ -z "${base_ref}" ] || [ "${base_ref}" = "null" ]; then
			base_ref="$(printf '%s' "${pr_json}" | jq -r 'if (type == "object" and .base.ref?) then .base.ref else empty end' 2>/dev/null | tail -n1)"
		fi
	fi
	if ! [[ "${head_sha}" =~ ^[0-9a-f]{7,40}$ ]] || [ -z "${base_ref}" ]; then
		PR_BASE_FRESHNESS_LAST_REASON="unresolved_head_or_base"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	PR_BASE_FRESHNESS_HEAD_SHA="${head_sha}"
	PR_BASE_FRESHNESS_BASE_REF="${base_ref}"

	# `compare/{head}...{base}`: `ahead_by` counts the commits the base
	# gained since the merge-base and `files` is the base-side diff. GitHub
	# returns at most 300 files and does not flag the cut, so 300 means the
	# extent is unknown and counts as overlap (re-validate rather than
	# merge blind).
	local compare_json ahead_by base_files_count
	compare_json="$(gh_retry _safe_gh_jq "repos/${repo}/compare/${head_sha}...${base_ref}" 2>/dev/null || echo "")"
	ahead_by="$(printf '%s' "${compare_json}" | jq -r 'if (type == "object" and (.ahead_by | type) == "number") then .ahead_by else empty end' 2>/dev/null | tail -n1)"
	if ! [[ "${ahead_by}" =~ ^[0-9]+$ ]]; then
		PR_BASE_FRESHNESS_LAST_REASON="compare_failed"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	PR_BASE_FRESHNESS_BEHIND_BY="${ahead_by}"
	# The base tip the compare saw: the last listed commit, but only when the
	# list is complete (GitHub lists at most 250 commits). Used to bind the
	# freshness-update marker to the exact base side of the update commit.
	PR_BASE_FRESHNESS_BASE_SHA="$(printf '%s' "${compare_json}" | jq -r '
		if (type == "object" and (.commits | type) == "array" and (.commits | length) > 0 and (.commits | length) == .ahead_by)
		then (.commits[-1].sha // "") else "" end' 2>/dev/null | tail -n1)"
	[[ "${PR_BASE_FRESHNESS_BASE_SHA}" =~ ^[0-9a-f]{40}$ ]] || PR_BASE_FRESHNESS_BASE_SHA=""
	if [ "${ahead_by}" -eq 0 ]; then
		PR_BASE_FRESHNESS_LAST_REASON="up_to_date"
		PR_BASE_FRESHNESS_OUTCOME="fresh"
		echo "fresh"
		return 0
	fi
	base_files_count="$(printf '%s' "${compare_json}" | jq -r 'if (type == "object") then ((.files // []) | length) else empty end' 2>/dev/null | tail -n1)"
	if ! [[ "${base_files_count}" =~ ^[0-9]+$ ]]; then
		PR_BASE_FRESHNESS_LAST_REASON="compare_failed"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	if [ "${base_files_count}" -ge 300 ]; then
		PR_BASE_FRESHNESS_BASE_PATHS="__truncated__"
		PR_BASE_FRESHNESS_LAST_REASON="base_diff_truncated"
		PR_BASE_FRESHNESS_OVERLAP="(base diff lists ${base_files_count} files; extent unknown)"
		PR_BASE_FRESHNESS_OUTCOME="overlap"
		echo "overlap"
		return 0
	fi
	# Issue #9993: the base-side names (renames under both), no extra call.
	PR_BASE_FRESHNESS_BASE_PATHS="$(printf '%s' "${compare_json}" | jq -r '
		[ (.files // [])[]? | select(type == "object") | (.filename // empty), (.previous_filename // empty)
		  | select(type == "string") ] | unique | .[]' 2>/dev/null || true)"

	# The PR's own paths: `--paginate --slurp` yields an array of pages,
	# each an array of file objects; renames count under both names.
	local pr_files_json tmp_compare tmp_pr_files overlap freshness_soft_input=""
	pr_files_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${repo}/pulls/${pr_number}/files?per_page=100" 2>/dev/null || echo "{}")"
	if ! tmp_compare="$(mktemp "${TMPDIR:-/tmp}/pr_base_compare.XXXXXX" 2>/dev/null)"; then
		PR_BASE_FRESHNESS_LAST_REASON="tmp_failed"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	if ! tmp_pr_files="$(mktemp "${TMPDIR:-/tmp}/pr_base_files.XXXXXX" 2>/dev/null)"; then
		rm -f "${tmp_compare}"
		PR_BASE_FRESHNESS_LAST_REASON="tmp_failed"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	printf '%s' "${compare_json}" > "${tmp_compare}"
	printf '%s' "${pr_files_json}" > "${tmp_pr_files}"
	overlap="$(jq -n -r --slurpfile compare "${tmp_compare}" --slurpfile pr_files "${tmp_pr_files}" '
		def names: [ .[]? | select(type == "object") | (.filename // empty), (.previous_filename // empty) ];
		(($compare[0].files // []) | names) as $base_paths
		| ($pr_files[0]
			| if type == "array" then ([ .[]? | if type == "array" then .[] else . end ] | names)
			  else null end) as $pr_paths
		| if $pr_paths == null then "__invalid__"
		  elif ($pr_files[0] | [ .[]? | if type == "array" then .[] else . end ] | length) >= 3000 then "__capped__"
		  else ([ $base_paths[] | select(. as $p | $pr_paths | index($p)) ] | unique | join(",")) end
	' 2>/dev/null | tail -n1)"
	if [ "${overlap}" != "__invalid__" ] && [ "${overlap}" != "__capped__" ]; then
		# Phase 4: with the soft class on, a path holding CR/LF (or a name
		# that is not a string) makes the cached list `*` (overlaps every
		# holder), so the lease's newline split can never turn one hard path
		# into soft fragments.
		local freshness_soft_on=""
		[ -z "$(_merge_lease_soft_entries 2>/dev/null)" ] || freshness_soft_on="1"
		PR_BASE_FRESHNESS_PR_PATHS="$(jq -r --arg soft_on "${freshness_soft_on}" '
			[ .[]? | if type == "array" then .[] else . end | select(type == "object")
			  | (.filename // empty), (.previous_filename // empty) ] | unique
			| if $soft_on != "" and any(.[]; (type != "string") or test("[\n\r]")) then "*" else .[] end
		' "${tmp_pr_files}" 2>/dev/null || true)"
		PR_BASE_FRESHNESS_PR_PATHS_FOR="${pr_number}"
		# Phase 4: the real shared paths, one per line, for the soft split.
		# The comma-joined `overlap` is display only: a path holding a comma
		# would split into pieces that may each look soft. A path holding a
		# newline, or a name that is not a string, turns the list into
		# "/unsafe-path", which no soft entry can match (entries never start
		# with `/`), so the split fails closed.
		freshness_soft_input="$(jq -n -r --slurpfile compare "${tmp_compare}" --slurpfile pr_files "${tmp_pr_files}" '
			def names: [ .[]? | select(type == "object") | (.filename // empty), (.previous_filename // empty) ];
			(($compare[0].files // []) | names) as $base_paths
			| ([ $pr_files[0][]? | if type == "array" then .[] else . end ] | names) as $pr_paths
			| [ $base_paths[] | select(. as $p | $pr_paths | index($p)) ] | unique
			| if any(.[]; (type != "string") or test("[\n\r]")) then "/unsafe-path" else .[] end
		' 2>/dev/null || true)"
	fi
	rm -f "${tmp_compare}" "${tmp_pr_files}"
	if [ "${overlap}" = "__invalid__" ] || [ -z "${overlap+x}" ]; then
		PR_BASE_FRESHNESS_LAST_REASON="pr_files_failed"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	if [ "${overlap}" = "__capped__" ]; then
		# GitHub lists at most 3,000 PR files; beyond that a shared path
		# may be missing, so re-validate rather than merge blind.
		PR_BASE_FRESHNESS_LAST_REASON="pr_files_truncated"
		PR_BASE_FRESHNESS_OVERLAP="(PR file list capped at 3000; extent unknown)"
		PR_BASE_FRESHNESS_OUTCOME="overlap"
		echo "overlap"
		return 0
	fi
	if [ -z "${overlap}" ]; then
		PR_BASE_FRESHNESS_LAST_REASON="no_shared_paths"
		PR_BASE_FRESHNESS_OUTCOME="clean"
		echo "clean"
		return 0
	fi
	# Soft path class (Phase 4): only when every shared path is soft does a
	# merge-tree probe decide; any shared hard path keeps shared_paths.
	_merge_lease_split_soft "${freshness_soft_input}"
	if [ -z "${_MERGE_LEASE_HARD_LIST}" ] && [ -n "${_MERGE_LEASE_SOFT_LIST}" ]; then
		PR_BASE_FRESHNESS_SOFT_OVERLAP="$(printf '%s' "${_MERGE_LEASE_SOFT_LIST}" | paste -sd, -)"
		_PR_PROBE_RESULT="unknown"
		if [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] && [ -n "${PR_BASE_FRESHNESS_BASE_SHA}" ]; then
			_pr_soft_merge_tree_probe "${head_sha}" "${PR_BASE_FRESHNESS_BASE_SHA}"
		fi
		case "${_PR_PROBE_RESULT}" in
			none)
				PR_BASE_FRESHNESS_LAST_REASON="soft_paths_merge_clean"
				PR_BASE_FRESHNESS_OUTCOME="clean"
				echo "clean"
				return 0
				;;
			conflict) PR_BASE_FRESHNESS_LAST_REASON="soft_paths_conflict" ;;
			*) PR_BASE_FRESHNESS_LAST_REASON="soft_paths_probe_unknown" ;;
		esac
		PR_BASE_FRESHNESS_OVERLAP="${overlap}"
		PR_BASE_FRESHNESS_OUTCOME="overlap"
		echo "overlap"
		return 0
	fi
	PR_BASE_FRESHNESS_OVERLAP="${overlap}"
	PR_BASE_FRESHNESS_LAST_REASON="shared_paths"
	PR_BASE_FRESHNESS_OUTCOME="overlap"
	echo "overlap"
	return 0
}

_pr_base_sync_for_fresh_ci()
{
	local pr_number="$1"
	local head_sha="$2"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	if ! [[ "${head_sha}" =~ ^[0-9a-f]{7,40}$ ]]; then
		echo "::warning::MERGE_BASE_SYNC pr=${pr_number} head_sha=${head_sha:-unknown} action=update_branch outcome=failed reason=unresolved_head_sha"
		return 1
	fi
	if gh_retry _safe_gh_jq -X PUT "repos/${repo}/pulls/${pr_number}/update-branch" -f "expected_head_sha=${head_sha}" >/dev/null 2>&1; then
		echo "MERGE_BASE_SYNC pr=${pr_number} head_sha=${head_sha} action=update_branch outcome=accepted"
		return 0
	fi
	echo "::warning::MERGE_BASE_SYNC pr=${pr_number} head_sha=${head_sha} action=update_branch outcome=failed"
	return 1
}


# Freshness-update markers and the update cap (#7084, Q60: A; hardened by
# security-pass findings freshness-sync-signed-merge-review-bypass and
# merge-base-sync-cap-proceeds-on-overlap).
#
# Only merge paths call _pr_base_fresh_for_merge, so a head it updates has
# already been approved for merge. After each accepted update the helper
# waits for the commit GitHub produced, verifies it is GitHub's own two-parent
# merge of the approved head with the base tip the freshness check saw, and
# only then posts one PR comment ending in
#   <!-- ai:merge-base-sync:v1 pr=<n> from=<approved head> sync=<k> base=<base tip> merge=<update commit> -->
# The review gate (scripts/review_head_gate.sh freshness-sync) lets exactly
# that `merge` commit, with parents [from, base], skip the reviewer panel: the
# deterministic-skip-merge job only waits for CI, then merges. Any other head
# (for example a later conflict-resolver merge on top of a branch reset to the
# approved head) is reviewed normally. When the update commit cannot be
# observed or verified, no marker is posted and the synchronize run reviews
# normally (fail safe).
#
# Remaining overlap never authorizes a merge: the function always requests
# another head-bound update and returns 1, so the merge waits for CI on the
# combined tree of the current head and base. MERGE_BASE_FRESHNESS_MAX_SYNCS
# (default 2, 0-9; anything else uses 2) is now only a warning threshold:
# once that many markers exist (0: from the first update), each further
# update logs `::warning::MERGE_BASE_FRESHNESS ... action=update
# reason=max_syncs_exceeded`. Before, reaching the cap merged without
# validating the latest combined tree.
#
# `_pr_base_sync_count <pr>` sets PR_BASE_SYNC_COUNT to the number of markers
# (legacy and bound) for <pr> posted by the token's own login; returns 1
# (count unknown) when either read fails. It also sets
# PR_BASE_SYNC_FINAL_CI_COUNT to how many of them the review sweep posted
# after a failed final CI run (they carry `<!-- ai:final-ci-base-sync:v1 pr=<n> -->`
# in their text; empty when unknown), and PR_BASE_SYNC_LOGIN to the token's
# own login once the `user` read succeeded.
# API budget (§15): one `user` read and one paginated issue-comments read,
# only on the overlap path. On the CI-wiring update path (issue #9993),
# _pr_final_ci_wiring_note_seen read both once already for its hold marker;
# that path runs once per head, so the second read is accepted. The review
# gate's own comments read happens in another job and cannot be shared.
_pr_base_sync_count()
{
	local pr_number="$1"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local sync_login sync_comments_json
	PR_BASE_SYNC_COUNT=""
	PR_BASE_SYNC_FINAL_CI_COUNT=""
	PR_BASE_SYNC_LOGIN=""
	sync_login="$(gh_retry _safe_gh_jq user 2>/dev/null | jq -r 'if type == "object" then (.login // "") else "" end' 2>/dev/null | tail -n1)"
	[[ "${sync_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]] || return 1
	# Kept for the review sweep's base sync, which trusts only this login's
	# ai-review/head-gate status (scripts/review_final_ci_sweep.sh).
	PR_BASE_SYNC_LOGIN="${sync_login}"
	sync_comments_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${repo}/issues/${pr_number}/comments?per_page=100" 2>/dev/null)" || return 1
	PR_BASE_SYNC_COUNT="$(printf '%s' "${sync_comments_json}" | jq -r --arg login "${sync_login}" --arg pr "${pr_number}" '
		[ .[]? | if type == "array" then .[] else . end
		  | select(type == "object" and ((.user.login // "") == $login))
		  | (.body // "") | select(type == "string" and startswith("**Branch updated from `") and test("<!-- ai:merge-base-sync:v1 pr=" + $pr + " from=[0-9a-f]{40} sync=[0-9]+( base=[0-9a-f]{40} merge=[0-9a-f]{40})? -->\\s*$")) ]
		| length' 2>/dev/null | tail -n1)"
	if ! [[ "${PR_BASE_SYNC_COUNT}" =~ ^[0-9]+$ ]]; then
		PR_BASE_SYNC_COUNT=""
		return 1
	fi
	# The subset the review sweep made after a failed final CI run
	# (scripts/review_final_ci_sweep.sh); same read, same trust rules.
	PR_BASE_SYNC_FINAL_CI_COUNT="$(printf '%s' "${sync_comments_json}" | jq -r --arg login "${sync_login}" --arg pr "${pr_number}" '
		[ .[]? | if type == "array" then .[] else . end
		  | select(type == "object" and ((.user.login // "") == $login))
		  | (.body // "") | select(type == "string" and startswith("**Branch updated from `") and contains("<!-- ai:final-ci-base-sync:v1 pr=" + $pr + " -->") and test("<!-- ai:merge-base-sync:v1 pr=" + $pr + " from=[0-9a-f]{40} sync=[0-9]+( base=[0-9a-f]{40} merge=[0-9a-f]{40})? -->\\s*$")) ]
		| length' 2>/dev/null | tail -n1)"
	[[ "${PR_BASE_SYNC_FINAL_CI_COUNT}" =~ ^[0-9]+$ ]] || PR_BASE_SYNC_FINAL_CI_COUNT=""
}

# `_pr_base_sync_observe_update_commit <pr> <from head> [<base ref>]`
# Waits for the commit an accepted update-branch produced and verifies it.
# `PUT pulls/{n}/update-branch` answers 202 with no commit SHA, so the PR is
# polled until its head differs from <from head>: up to
# MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS reads (default 5, 1-20),
# MERGE_BASE_SYNC_HEAD_POLL_SECONDS apart (default 3, 1-30); invalid values use
# the defaults. The new head must have exactly two parents, the first being
# <from head>, be committed by `web-flow` with a verified signature, and its
# second parent must equal PR_BASE_FRESHNESS_BASE_SHA exactly. When the
# compare commit list was truncated (more than 250 base commits) that tip is
# unknown and no marker is posted (base_tip_unknown, no reads): mere
# containment in the base would also accept a later web conflict-resolver
# merge [approved head, newer base tip] made by a branch writer, so the next
# run reviews in full instead. Only an exact [approved head, compared tip]
# pair, which GitHub merged without conflict, admits no editable merge.
# <base ref> is accepted for compatibility and unused.
# Sets PR_BASE_SYNC_MERGE_SHA / PR_BASE_SYNC_BASE_SHA (plus, from the read
# that saw the new head, PR_BASE_SYNC_HEAD_REF, PR_BASE_SYNC_HEAD_REPO and
# PR_BASE_SYNC_MERGEABLE = true|false|null) and returns 0 on
# success; otherwise returns 1 with PR_BASE_SYNC_OBSERVE_REASON =
# head_not_observed | base_tip_unknown | pull_read_failed |
# commit_read_failed | parents_mismatch | not_github_merge | base_mismatch.
# API budget (§15): at most MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS `pulls/{n}`
# reads and one `commits/{sha}` read, all on the accepted-update path. The earlier
# `pulls/{n}` read in _pr_base_freshness cannot be reused: the head changes
# after the PUT.
_pr_base_sync_observe_update_commit()
{
	local pr_number="$1" from_head="$2"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local observe_attempts="${MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS:-5}" observe_sleep="${MERGE_BASE_SYNC_HEAD_POLL_SECONDS:-3}"
	local observe_i observe_head="" observe_pull_ok="false" observe_commit observe_parents observe_first observe_second observe_verified observe_committer
	PR_BASE_SYNC_MERGE_SHA=""
	PR_BASE_SYNC_BASE_SHA=""
	PR_BASE_SYNC_OBSERVE_REASON=""
	PR_BASE_SYNC_HEAD_REF=""
	PR_BASE_SYNC_HEAD_REPO=""
	PR_BASE_SYNC_MERGEABLE=""
	if [[ "${observe_attempts}" =~ ^[0-9]+$ ]] && [ "${observe_attempts}" -ge 1 ] && [ "${observe_attempts}" -le 20 ]; then :; else observe_attempts=5; fi
	if [[ "${observe_sleep}" =~ ^[0-9]+$ ]] && [ "${observe_sleep}" -ge 1 ] && [ "${observe_sleep}" -le 30 ]; then :; else observe_sleep=3; fi
	if ! [[ "${from_head}" =~ ^[0-9a-f]{40}$ ]]; then
		PR_BASE_SYNC_OBSERVE_REASON="head_not_observed"
		return 1
	fi
	if ! [[ "${PR_BASE_FRESHNESS_BASE_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
		PR_BASE_SYNC_OBSERVE_REASON="base_tip_unknown"
		return 1
	fi
	for (( observe_i = 1; observe_i <= observe_attempts; observe_i++ )); do
		sleep "${observe_sleep}"
		local observe_pull_json=""
		if observe_pull_json="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr_number}" 2>/dev/null)"; then
			observe_pull_ok="true"
			observe_head="$(printf '%s' "${observe_pull_json}" | jq -r 'if (type == "object") then (.head.sha // "") else "" end' 2>/dev/null | tail -n1)"
			if [[ "${observe_head}" =~ ^[0-9a-f]{40}$ ]] && [ "${observe_head}" != "${from_head}" ]; then
				# Kept for _pr_base_sync_dispatch_final_ci: the same read names the
				# head branch and whether GitHub has computed the merge ref yet.
				PR_BASE_SYNC_HEAD_REF="$(printf '%s' "${observe_pull_json}" | jq -r 'if type == "object" then (.head.ref // "") else "" end' 2>/dev/null | tail -n1)"
				PR_BASE_SYNC_HEAD_REPO="$(printf '%s' "${observe_pull_json}" | jq -r 'if type == "object" then (.head.repo.full_name // "") else "" end' 2>/dev/null | tail -n1)"
				PR_BASE_SYNC_MERGEABLE="$(printf '%s' "${observe_pull_json}" | jq -r 'if type == "object" then (.mergeable | tostring) else "null" end' 2>/dev/null | tail -n1)"
				break
			fi
		fi
		observe_head=""
	done
	if [ -z "${observe_head}" ]; then
		if [ "${observe_pull_ok}" = "true" ]; then
			PR_BASE_SYNC_OBSERVE_REASON="head_not_observed"
		else
			PR_BASE_SYNC_OBSERVE_REASON="pull_read_failed"
		fi
		return 1
	fi
	if ! observe_commit="$(gh_retry _safe_gh_jq "repos/${repo}/commits/${observe_head}" 2>/dev/null)"; then
		PR_BASE_SYNC_OBSERVE_REASON="commit_read_failed"
		return 1
	fi
	observe_parents="$(printf '%s' "${observe_commit}" | jq -r 'if type == "object" then ([.parents[]?.sha] | length) else -1 end' 2>/dev/null | tail -n1)"
	observe_first="$(printf '%s' "${observe_commit}" | jq -r 'if type == "object" then (.parents[0].sha // "") else "" end' 2>/dev/null | tail -n1)"
	observe_second="$(printf '%s' "${observe_commit}" | jq -r 'if type == "object" then (.parents[1].sha // "") else "" end' 2>/dev/null | tail -n1)"
	observe_verified="$(printf '%s' "${observe_commit}" | jq -r 'if type == "object" then ((.commit.verification.verified // false) | tostring) else "false" end' 2>/dev/null | tail -n1)"
	observe_committer="$(printf '%s' "${observe_commit}" | jq -r 'if type == "object" then (.committer.login // "") else "" end' 2>/dev/null | tail -n1)"
	if [ "${observe_parents}" != "2" ] || [ "${observe_first}" != "${from_head}" ] || ! [[ "${observe_second}" =~ ^[0-9a-f]{40}$ ]]; then
		PR_BASE_SYNC_OBSERVE_REASON="parents_mismatch"
		return 1
	fi
	if [ "${observe_verified}" != "true" ] || [ "${observe_committer}" != "web-flow" ]; then
		PR_BASE_SYNC_OBSERVE_REASON="not_github_merge"
		return 1
	fi
	if [ "${observe_second}" != "${PR_BASE_FRESHNESS_BASE_SHA}" ]; then
		PR_BASE_SYNC_OBSERVE_REASON="base_mismatch"
		return 1
	fi
	PR_BASE_SYNC_MERGE_SHA="${observe_head}"
	PR_BASE_SYNC_BASE_SHA="${observe_second}"
	return 0
}

# Post the marker for an accepted, verified update. Args: <pr> <approved head>
# <sync number> <sync max> <base sha> <merge sha>. Without a full 40-hex
# approved head, base and merge SHA no marker is posted, so the synchronize
# run reviews normally (fail safe).
_pr_base_sync_marker_post()
{
	local pr_number="$1" head_sha="$2" sync_number="$3" sync_max="$4" sync_base_sha="${5:-}" sync_merge_sha="${6:-}"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local base_label="${PR_BASE_FRESHNESS_BASE_REF:-the base branch}" sync_body sync_lead
	local overlap_label="${PR_BASE_FRESHNESS_OVERLAP:-shared paths}"
	# Keep the comment far below GitHub's 65,536-character body limit.
	[ "${#overlap_label}" -le 2000 ] || overlap_label="${overlap_label:0:2000}, ..."
	if ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "MERGE_BASE_SYNC_MARKER pr=${pr_number} head_sha=${head_sha:-unknown} outcome=skipped reason=short_head_sha"
		return 0
	fi
	if ! [[ "${sync_base_sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${sync_merge_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "MERGE_BASE_SYNC_MARKER pr=${pr_number} head_sha=${head_sha} outcome=skipped reason=unbound_update"
		return 0
	fi
	# PR_BASE_SYNC_MARKER_LEAD (optional): replaces the sentence that says why
	# the branch was updated; the review sweep sets it for a sync after a failed
	# final CI run. The heading and the closing marker line never change, so
	# _pr_base_sync_count and the review gate read every marker the same way.
	sync_lead="${PR_BASE_SYNC_MARKER_LEAD:-}"
	if [ -z "${sync_lead}" ]; then
		sync_lead="$(printf '`%s` gained %s commit(s) that touch files this PR also changes (%s), so CI must run on the combined tree first.' \
			"${base_label}" "${PR_BASE_FRESHNESS_BEHIND_BY:-?}" "${overlap_label}")"
	fi
	sync_body="$(printf '**Branch updated from `%s` before merging** (update %s; warning threshold %s).\n\n%s This head was already approved for merge: the next review run for update commit `%s` only waits for CI and then merges, without running the reviewers again. Any other head is reviewed normally. If the base changes these files again before the merge, the branch is updated again and the merge waits for CI on the new combined tree.\n\n<!-- ai:merge-base-sync:v1 pr=%s from=%s sync=%s base=%s merge=%s -->' \
		"${base_label}" "${sync_number}" "${sync_max}" "${sync_lead}" "${sync_merge_sha}" "${pr_number}" "${head_sha}" "${sync_number}" "${sync_base_sha}" "${sync_merge_sha}")"
	# One attempt only: a retried POST whose first response was lost would
	# post a duplicate marker.
	if _safe_gh_jq -X POST "repos/${repo}/issues/${pr_number}/comments" -f "body=${sync_body}" >/dev/null 2>&1; then
		echo "MERGE_BASE_SYNC_MARKER pr=${pr_number} head_sha=${head_sha} sync=${sync_number} merge=${sync_merge_sha} outcome=posted"
	else
		echo "::warning::MERGE_BASE_SYNC_MARKER pr=${pr_number} head_sha=${head_sha} sync=${sync_number} outcome=failed (the synchronize run reviews normally)"
	fi
	return 0
}

# Final CI at the moment of a verified freshness update (merge-base sync
# churn, 2026-11-17; operator decision Q3: B). Before this, the update head's
# final CI run was dispatched only when a later review run reached the
# required-checks wait, after the synchronize review (and, before the
# security-pass inheritance, a fresh audit) had waited for a runner; with
# multi-hour queues `main` usually moved again first. Now the merge path that
# made the update dispatches the run right away, so the review run that
# follows finds it in flight (`_pr_ensure_final_ci` outcome=exists) instead of
# starting it.
#
#   _pr_base_sync_dispatch_final_ci <pr> <approved head> <update head>
#
# No-op unless REVIEW_FINAL_CI_WORKFLOW is set and MERGE_BASE_SYNC_FINAL_CI_ENABLED
# (default true; true/1/yes/on) is on. Requires the observe step's read to
# name a same-repository head branch, and GitHub to have computed the merge
# ref for the update head (`mergeable` true): ci.yml tests refs/pull/<n>/merge,
# so a dispatch before that could test the previous head. The observe read
# counts as the first check; up to MERGE_BASE_SYNC_MERGE_REF_POLL_ATTEMPTS more
# `pulls/{n}` reads (default 3, 0-10), MERGE_BASE_SYNC_HEAD_POLL_SECONDS apart.
# Anything unmet logs and returns: the required-checks wait still dispatches
# the run later, as before.
# Sync-quick: with MERGE_BASE_SYNC_QUICK_CI_ENABLED (default true) on, when every
# overlap path is a CI-wiring file (scripts/ci_sync_quick.sh classify) and the
# approved head passed a full final CI run (ci_sync_quick.sh full-ci-green, one
# runs read), the dispatch passes sync_quick=true; ci.yml's tests-sync-quick
# re-checks both conditions.
# API budget (§15): the merge-ref reads above, at most one runs read for the
# sync-quick check, then _pr_ensure_final_ci's own runs read, PR-files read and
# dispatch. Always returns 0.
# Log: `MERGE_BASE_SYNC_FINAL_CI pr=<n> head=<sha> outcome=<word> reason=<token> sync_quick=true|false`.
_pr_base_sync_dispatch_final_ci()
{
	local pr_number="$1" sync_from_head="$2" sync_update_head="$3"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local sync_ci_attempts="${MERGE_BASE_SYNC_MERGE_REF_POLL_ATTEMPTS:-3}" sync_ci_sleep="${MERGE_BASE_SYNC_HEAD_POLL_SECONDS:-3}"
	local sync_ci_i sync_ci_pull sync_ci_pull_head sync_ci_quick="false" sync_ci_verdict="" sync_ci_classifier sync_ci_reason="full_suite"
	sync_ci_log()
	{
		echo "MERGE_BASE_SYNC_FINAL_CI pr=${pr_number:-?} head=${sync_update_head:-?} outcome=$1 reason=$2 sync_quick=${sync_ci_quick}"
	}
	case "$(printf '%s' "${MERGE_BASE_SYNC_FINAL_CI_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) ;;
		*) sync_ci_log skipped disabled; return 0 ;;
	esac
	if [ -z "${REVIEW_FINAL_CI_WORKFLOW:-}" ]; then
		sync_ci_log skipped workflow_unset
		return 0
	fi
	if ! [[ "${sync_from_head}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${sync_update_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		sync_ci_log skipped invalid_input
		return 0
	fi
	if [ "${PR_BASE_SYNC_HEAD_REPO:-}" != "${repo}" ] || ! [[ "${PR_BASE_SYNC_HEAD_REF:-}" =~ ^[A-Za-z0-9._/-]+$ ]] \
		|| [[ "${PR_BASE_SYNC_HEAD_REF}" == *..* ]] || [[ "${PR_BASE_SYNC_HEAD_REF}" == -* ]]; then
		sync_ci_log skipped head_branch_unusable
		return 0
	fi
	if [[ "${sync_ci_attempts}" =~ ^[0-9]+$ ]] && [ "${#sync_ci_attempts}" -le 2 ] && [ "$((10#${sync_ci_attempts}))" -le 10 ]; then
		sync_ci_attempts="$((10#${sync_ci_attempts}))"
	else
		sync_ci_attempts=3
	fi
	if [[ "${sync_ci_sleep}" =~ ^[0-9]+$ ]] && [ "${#sync_ci_sleep}" -le 2 ] && [ "$((10#${sync_ci_sleep}))" -ge 1 ] && [ "$((10#${sync_ci_sleep}))" -le 30 ]; then
		sync_ci_sleep="$((10#${sync_ci_sleep}))"
	else
		sync_ci_sleep=3
	fi
	sync_ci_i=0
	while [ "${PR_BASE_SYNC_MERGEABLE:-null}" = "null" ] && [ "${sync_ci_i}" -lt "${sync_ci_attempts}" ]; do
		sync_ci_i=$((sync_ci_i + 1))
		sleep "${sync_ci_sleep}"
		sync_ci_pull="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${pr_number}" 2>/dev/null || echo "")"
		sync_ci_pull_head="$(printf '%s' "${sync_ci_pull}" | jq -r 'if type == "object" then (.head.sha // "") else "" end' 2>/dev/null | tail -n1)"
		# A failed or unreadable read is not a head move: keep polling.
		[ -n "${sync_ci_pull_head}" ] || continue
		if [ "${sync_ci_pull_head}" != "${sync_update_head}" ]; then
			sync_ci_log skipped head_moved
			return 0
		fi
		PR_BASE_SYNC_MERGEABLE="$(printf '%s' "${sync_ci_pull}" | jq -r 'if type == "object" then (.mergeable | tostring) else "null" end' 2>/dev/null | tail -n1)"
	done
	if [ "${PR_BASE_SYNC_MERGEABLE:-null}" != "true" ]; then
		sync_ci_log skipped "merge_ref_${PR_BASE_SYNC_MERGEABLE:-null}"
		return 0
	fi
	case "$(printf '%s' "${MERGE_BASE_SYNC_QUICK_CI_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on)
			sync_ci_classifier="${BASH_SOURCE[0]%/*}/ci_sync_quick.sh"
			if [ -f "${sync_ci_classifier}" ] && [ -n "${PR_BASE_FRESHNESS_OVERLAP:-}" ]; then
				sync_ci_verdict="$(printf '%s' "${PR_BASE_FRESHNESS_OVERLAP}" | tr ',' '\n' | bash "${sync_ci_classifier}" classify 2>/dev/null)"
				# reason= stays one token: the classifier's `other <path>` is logged as overlap_other.
				if [ "${sync_ci_verdict}" != "ci_wiring_only" ]; then
					sync_ci_reason="overlap_other"
				elif bash "${sync_ci_classifier}" full-ci-green "${repo}" "${pr_number}" "${sync_from_head}" "${REVIEW_FINAL_CI_WORKFLOW:-}"; then
					sync_ci_quick="true"
					sync_ci_reason="ci_wiring_only"
				else
					sync_ci_reason="approved_head_not_full_green"
				fi
			fi
			;;
	esac
	# PR_FINAL_CI_WIRING_SKIP: the update head was just verified as GitHub's
	# merge with the base tip, so its wiring is current (issue #9993).
	PR_FINAL_CI_WIRING_SKIP=true PR_FINAL_CI_SYNC_QUICK="${sync_ci_quick}" \
		_pr_ensure_final_ci "${repo}" "${pr_number}" "${sync_update_head}" "${PR_BASE_SYNC_HEAD_REF}" || true
	sync_ci_log "${PR_FINAL_CI_OUTCOME:-unknown}" "${sync_ci_reason}"
	return 0
}

_pr_base_fresh_for_merge()
{
	local pr_number="$1"
	local head_sha="${2:-}"
	local base_ref="${3:-}"
	# Run in this shell (no command substitution) so the side-channel
	# variables survive for the log line and the update call.
	_pr_base_freshness "${pr_number}" "${head_sha}" "${base_ref}" >/dev/null
	case "${PR_BASE_FRESHNESS_OUTCOME:-unknown}" in
		overlap)
			echo "MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-unknown} base=${base_ref:-unknown} outcome=overlap reason=${PR_BASE_FRESHNESS_LAST_REASON} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:--} overlap=${PR_BASE_FRESHNESS_OVERLAP:--} soft_overlap=${PR_BASE_FRESHNESS_SOFT_OVERLAP:--}"
			# Merge lease: only the lease holder for this base updates its
			# branch. Any other PR waits without updating, so N approved PRs
			# no longer each spend a CI run on a combined tree the next merge
			# makes stale. A lease that cannot be read fails open (as before).
			if ! _merge_lease_gate "${pr_number}" "${base_ref:-${PR_BASE_FRESHNESS_BASE_REF:-}}" "${PR_BASE_FRESHNESS_HEAD_SHA:-}" update; then
				PR_BASE_FRESHNESS_OUTCOME="lease_wait"
				PR_BASE_FRESHNESS_LAST_REASON="merge_lease_${MERGE_LEASE_REASON:-held}"
				echo "  [merge-lease] PR #${pr_number}: the merge lease for ${base_ref:-${PR_BASE_FRESHNESS_BASE_REF:-the base}} is held by or queued for PR #${MERGE_LEASE_BLOCKER:-?}; not updating the branch. This PR is labelled ${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait} and is re-dispatched when the lease is granted to it."
				return 1
			fi
			local sync_max="${MERGE_BASE_FRESHNESS_MAX_SYNCS:-2}" sync_done=""
			[[ "${sync_max}" =~ ^[0-9]$ ]] || sync_max=2
			if [ "${sync_max}" -eq 0 ]; then
				sync_done=0
			elif _pr_base_sync_count "${pr_number}"; then
				sync_done="${PR_BASE_SYNC_COUNT}"
			fi
			# Remaining overlap never authorizes a merge: past the threshold the
			# branch is still updated, so the merge waits for CI on a commit
			# bound to the current head and base.
			if [ -n "${sync_done}" ] && [ "${sync_done}" -ge "${sync_max}" ]; then
				echo "::warning::MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-unknown} outcome=overlap action=update reason=max_syncs_exceeded syncs=${sync_done} max=${sync_max}"
			fi
			echo "  [base-freshness] PR #${pr_number}: the base gained ${PR_BASE_FRESHNESS_BEHIND_BY:-?} commit(s) touching ${PR_BASE_FRESHNESS_OVERLAP:-shared paths}; updating the branch so CI runs on the combined tree before merging (update $(( ${sync_done:-0} + 1 )), warning threshold ${sync_max})."
			if _pr_base_sync_for_fresh_ci "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}"; then
				if _pr_base_sync_observe_update_commit "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}" "${base_ref:-${PR_BASE_FRESHNESS_BASE_REF:-}}"; then
					_pr_base_sync_marker_post "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}" "$(( ${sync_done:-0} + 1 ))" "${sync_max}" "${PR_BASE_SYNC_BASE_SHA}" "${PR_BASE_SYNC_MERGE_SHA}"
					_pr_base_sync_dispatch_final_ci "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}" "${PR_BASE_SYNC_MERGE_SHA}"
				else
					echo "MERGE_BASE_SYNC_MARKER pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-unknown} outcome=skipped reason=${PR_BASE_SYNC_OBSERVE_REASON:-head_not_observed}"
				fi
			fi
			return 1
			;;
		unknown)
			# Fail closed: no verified overlap, so no branch update either.
			echo "::warning::MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-${head_sha:-unknown}} base=${base_ref:-unknown} outcome=unknown reason=${PR_BASE_FRESHNESS_LAST_REASON} action=defer"
			echo "  [base-freshness] PR #${pr_number}: could not verify the base against this PR's files (${PR_BASE_FRESHNESS_LAST_REASON}); deferring the merge to the next round."
			return 1
			;;
		*)
			# fresh / clean: no update is needed, but a merge that touches the
			# lease holder's files would make the holder's combined-tree CI run
			# stale, so it waits for the holder (Q3: A). disabled skips the lease.
			if [ "${PR_BASE_FRESHNESS_OUTCOME}" != "disabled" ] \
				&& ! _merge_lease_gate "${pr_number}" "${base_ref:-${PR_BASE_FRESHNESS_BASE_REF:-}}" "${PR_BASE_FRESHNESS_HEAD_SHA:-${head_sha:-}}" merge; then
				echo "MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-${head_sha:-unknown}} base=${base_ref:-unknown} outcome=${PR_BASE_FRESHNESS_OUTCOME} reason=${PR_BASE_FRESHNESS_LAST_REASON} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:--} action=lease_wait soft_overlap=${PR_BASE_FRESHNESS_SOFT_OVERLAP:--}"
				PR_BASE_FRESHNESS_OUTCOME="lease_wait"
				PR_BASE_FRESHNESS_LAST_REASON="merge_lease_${MERGE_LEASE_REASON:-held}"
				# Phase 5: a PR that is itself a live holder waits unlabelled on an
				# earlier holder (_merge_lease_gate_multi); the review sweep retries it.
				if [ "${MERGE_LEASE_REASON:-}" = "holder_shares_paths" ] && declare -F _merge_lease_holder_row >/dev/null 2>&1 \
					&& [ -n "$(_merge_lease_holder_row "${pr_number}")" ]; then
					echo "  [merge-lease] PR #${pr_number}: this PR holds the merge lease but now changes a file that earlier holder PR #${MERGE_LEASE_BLOCKER:-?} changes; merging now would make its CI run stale. No wait label is added; the review sweep retries this PR."
				else
					echo "  [merge-lease] PR #${pr_number}: PR #${MERGE_LEASE_BLOCKER:-?} holds the merge lease and changes a file this PR changes; merging now would make its CI run stale. Waiting under ${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}."
				fi
				return 1
			fi
			echo "MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-${head_sha:-unknown}} base=${base_ref:-unknown} outcome=${PR_BASE_FRESHNESS_OUTCOME} reason=${PR_BASE_FRESHNESS_LAST_REASON} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:--} action=proceed soft_overlap=${PR_BASE_FRESHNESS_SOFT_OVERLAP:--}"
			return 0
			;;
	esac
}

# Base update for a refused final-CI dispatch (operator decision Q2: A,
# 2026-11-17; PR #9579). `gh workflow run` refuses a branch whose copy of
# REVIEW_FINAL_CI_WORKFLOW has no workflow_dispatch trigger (branches cut
# before cut-ci-runner-time Phase 4b, #9563), so such an approved head can
# never get the required `lint` and never merges.
#
#   _pr_final_ci_refused_base_update <pr> <approved head> [<base ref>]
#
# Called only from _pr_wait_for_required_checks after _pr_ensure_final_ci
# ended `dispatch_failed reason=dispatch_refused`. Every caller of that wait
# first ran _pr_base_fresh_for_merge on the same approved head, so it acts
# only when PR_BASE_FRESHNESS_OUTCOME is `clean` for this head (the base is
# ahead and shares no file with the PR; `fresh` means an update cannot help).
# It then confirms the cause: the head's workflow file must exist and name no
# `workflow_dispatch` (one contents read), while the base's copy must have it
# (one contents read). After one update the head carries the base's file, so
# this runs at most once per PR for this cause. Then, exactly like the
# freshness overlap path: head-bound update-branch, verified update commit,
# the ai:merge-base-sync:v1 marker (so the update head skips the reviewer
# panel and only waits for CI) and the update head's final-CI dispatch.
# Switch: FINAL_CI_REFUSED_BASE_UPDATE_ENABLED (default true; true/1/yes/on).
# API budget (§15): two contents reads, then the freshness path's PUT,
# observe reads, marker POST and final-CI dispatch; nothing on other paths.
# Always returns 0 (the wait still ends `timeout`; the synchronize run merges).
# Log: `REVIEW_FINAL_CI_BASE_UPDATE pr=<n> head=<sha> outcome=updated|requested|skipped|failed reason=<token>`.
_pr_final_ci_refused_base_update()
{
	local refused_pr="$1" refused_head="$2" refused_base="${3:-}"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" refused_wf="${REVIEW_FINAL_CI_WORKFLOW:-}"
	local refused_head_has="" refused_base_has="" refused_base_ref refused_sync_number
	refused_log()
	{
		echo "REVIEW_FINAL_CI_BASE_UPDATE pr=${refused_pr:-?} head=${refused_head:-?} outcome=$1 reason=$2"
	}
	case "$(printf '%s' "${FINAL_CI_REFUSED_BASE_UPDATE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) ;;
		*) refused_log skipped disabled; return 0 ;;
	esac
	if ! [[ "${refused_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${refused_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${refused_wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]] || ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		refused_log skipped invalid_input
		return 0
	fi
	if [ "${PR_BASE_FRESHNESS_HEAD_SHA:-}" != "${refused_head}" ] || [ "${PR_BASE_FRESHNESS_OUTCOME:-}" != "clean" ]; then
		refused_log skipped "freshness_${PR_BASE_FRESHNESS_OUTCOME:-unchecked}"
		return 0
	fi
	refused_base_ref="${refused_base:-${PR_BASE_FRESHNESS_BASE_REF:-}}"
	if ! [[ "${refused_base_ref}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${refused_base_ref}" == *..* ]]; then
		refused_log skipped base_ref_unknown
		return 0
	fi
	# `yes` when the file at <ref> declares a workflow_dispatch trigger, `no`
	# when it does not, empty when the read failed or the file is missing.
	refused_has_dispatch()
	{
		gh_retry _safe_gh_jq "repos/${repo}/contents/.github/workflows/${refused_wf}?ref=$1" 2>/dev/null \
			| jq -r 'if type == "object" and (.encoding // "") == "base64" and ((.content // "") | type) == "string"
				then ((.content | gsub("\\s"; "") | @base64d) | if test("(^|\\n)\\s*\"?workflow_dispatch\"?\\s*:|[\\[,{]\\s*\"?workflow_dispatch\"?\\s*[\\],}:]") then "yes" else "no" end)
				else "" end' 2>/dev/null | tail -n1
	}
	refused_head_has="$(refused_has_dispatch "${refused_head}")"
	if [ "${refused_head_has}" != "no" ]; then
		refused_log skipped "head_workflow_${refused_head_has:-unreadable}"
		return 0
	fi
	refused_base_has="$(refused_has_dispatch "${refused_base_ref}")"
	if [ "${refused_base_has}" != "yes" ]; then
		refused_log skipped "base_workflow_${refused_base_has:-unreadable}"
		return 0
	fi
	if ! _pr_base_sync_for_fresh_ci "${refused_pr}" "${refused_head}"; then
		refused_log failed update_branch_refused
		return 0
	fi
	if _pr_base_sync_observe_update_commit "${refused_pr}" "${refused_head}" "${refused_base_ref}"; then
		# The update number counts earlier sync markers (freshness updates
		# included); an unreadable count falls back to 1.
		refused_sync_number=1
		if _pr_base_sync_count "${refused_pr}"; then
			refused_sync_number=$(( PR_BASE_SYNC_COUNT + 1 ))
		fi
		PR_BASE_SYNC_MARKER_LEAD="$(printf 'This branch'"'"'s `%s` predates its `workflow_dispatch` trigger, so the final CI run that the merge requires could not be started on it; `%s` (%s commit(s) ahead, none touching files this PR changes) has the trigger.' "${refused_wf}" "${refused_base_ref}" "${PR_BASE_FRESHNESS_BEHIND_BY:-?}")" \
			_pr_base_sync_marker_post "${refused_pr}" "${refused_head}" "${refused_sync_number}" "${MERGE_BASE_FRESHNESS_MAX_SYNCS:-2}" \
			"${PR_BASE_SYNC_BASE_SHA}" "${PR_BASE_SYNC_MERGE_SHA}"
		_pr_base_sync_dispatch_final_ci "${refused_pr}" "${refused_head}" "${PR_BASE_SYNC_MERGE_SHA}"
		refused_log updated workflow_predates_dispatch
	else
		echo "MERGE_BASE_SYNC_MARKER pr=${refused_pr} head_sha=${refused_head} outcome=skipped reason=${PR_BASE_SYNC_OBSERVE_REASON:-head_not_observed}"
		refused_log requested "${PR_BASE_SYNC_OBSERVE_REASON:-head_not_observed}"
	fi
	return 0
}

# ---------------------------------------------------------------------------
# Final CI on a branch whose CI wiring is behind its base (issue #9993).
#
# The final CI is dispatched on the PR's head ref, so GitHub runs the head
# branch's own copy of REVIEW_FINAL_CI_WORKFLOW, while its jobs check out the
# merge ref and run the base's scripts. A branch that lacks the base's commits
# to that workflow file runs old steps against new scripts: false failures
# (CI [pr:9725], run 45159799693: `not our ref` after #9727's
# CI_FINAL_MERGE_REF_LOCAL_MERGE handling) or no `ci/final` status at all
# (branches cut before #9956). _pr_base_fresh_for_merge does not help: it
# updates a branch only when the base moved under files the PR touches.
#
#   _pr_final_ci_wiring_enabled
#       0 when FINAL_CI_WIRING_BEHIND_UPDATE_ENABLED (default true;
#       true/1/yes/on, any case) is on.
#   _pr_final_ci_wiring_behind_update <pr> <head sha> <base ref> <workflow file> <entry>
#       <entry> is `dispatch` (from _pr_ensure_final_ci, before a dispatch)
#       or `blocked_status` (from _pr_sync_merge_blocked_guard, when the only
#       blocker is `<FINAL_CI_WIRING_STATUS_CONTEXT> (missing)`; it then also
#       requires a `success` final CI run on the head).
#       "Behind" means the base has commits to `.github/workflows/<file>`
#       that the head lacks: the file is in the base-side file list of
#       `compare/<head>...<base>`. A PR that edits the file itself is not
#       behind by this rule alone (G5). Only that one path is checked: ci.yml
#       calls no local reusable workflow (tests/test_review_final_ci.py pins
#       that), and local actions resolve from the checked-out merge ref, so
#       they already come from the base.
#       When behind: a recorded refusal for this head (below) holds without a
#       new attempt; otherwise the merge lease (`_merge_lease_gate ... update`)
#       must allow the update, then the freshness path's chain runs: head-bound
#       update-branch, verified update commit, the ai:merge-base-sync:v1
#       marker (so the update head only waits for CI) and the update head's
#       final CI. A refused update posts one comment ending in
#         <!-- ai:final-ci-wiring-behind:v1 pr=<n> head=<sha> reason=update_branch_refused -->
#       (only when the comment read succeeded, so a failing read cannot post
#       one comment per tick), and later calls for that head hold with
#       reason=recorded. Only comments by the token's own login count, so a
#       commenter cannot forge the marker to stall a PR. Before posting, one
#       PR read: a head that already moved (a concurrent caller updated the
#       branch) holds with reason=head_moved and posts nothing. The hold
#       marker needs `mergeable: false`; any other read (true, null, failed)
#       on the first refusal posts a one-time retry note ending in
#         <!-- ai:final-ci-wiring-behind:v1 pr=<n> head=<sha> reason=update_branch_failed -->
#       and holds with reason=update_branch_failed; the next call tries once
#       more, and a second refusal posts the hold marker. So the update lease
#       is taken at most twice per head without progress.
#       With PR_FINAL_CI_WIRING_NO_UPDATE=true (check-only callers; also set
#       by _pr_ensure_final_ci for a PR labelled e2e-smoke-test in its PR
#       read), a behind head holds with reason=no_update_path: no lease and no
#       update; one note per head ending in
#         <!-- ai:final-ci-wiring-behind:v1 pr=<n> head=<sha> reason=no_update_path -->
#       (only when the comment read succeeded), which is not a hold.
#       The update chain runs with REVIEW_FINAL_CI_WORKFLOW defaulted to
#       <workflow file>, so the poller's calls also dispatch the update
#       head's final CI.
#       Sets PR_FINAL_CI_WIRING_OUTCOME = current | updated | requested |
#       held | unknown and PR_FINAL_CI_WIRING_REASON; always returns 0.
#       Callers dispatch (or defer) as before on current and unknown.
# Env: FINAL_CI_WIRING_BEHIND_UPDATE_ENABLED (default true; off restores the
# behaviour before issue #9993 with no API call; read from the job
# environment, no workflow maps a repository variable to it),
# FINAL_CI_WIRING_STATUS_CONTEXT (default ci/final, the context
# scripts/ci_final_status.sh posts; read by the guard),
# PR_FINAL_CI_WIRING_NO_UPDATE (default unset).
# API budget (§14/§15 API hygiene): 0 calls to decide "behind" when the
# freshness side channel (PR_BASE_FRESHNESS_HEAD_SHA / _BASE_REF /
# _BASE_PATHS / _OUTCOME) covers this head and base; otherwise one
# `GET compare/<head>...<base>`, plus, only when that compare lists 300+
# files, one `GET commits?sha=<base>&path=<file>&per_page=1` and one
# `GET compare/<that commit>...<head>`. `blocked_status` adds one
# `GET actions/workflows/<file>/runs?head_sha=<head>&per_page=20` first. Only
# when behind: one `GET user` and one paginated issue-comments read, the
# lease gate's own calls, then the freshness update path's PUT, observe
# reads, sync count (_pr_base_sync_count reads `user` and the comments a
# second time: two of each on an updated head, once per head), marker POST
# and final-CI dispatch, or, when the update is refused, one `GET pulls/<pr>`
# and at most one comment POST (two refusals per head at most). A
# check-only hold reads `user` and the comments once and posts at most one
# note per head. No earlier call in these paths reads the
# compare for a head the side channel does not cover. A held head that
# stays a final-CI sweep candidate pays the compare, `GET user` and comments
# read again on each sweep tick (at most FINAL_CI_SWEEP_MAX_PER_TICK heads).
# Log (one line per decision): `REVIEW_FINAL_CI_WIRING pr=<n> head=<sha>
# entry=dispatch|blocked_status outcome=<word> reason=<token>`; on invalid
# input `pr=`, `head=` and `entry=` may read `invalid`
# (outcome=unknown reason=invalid_input).
_pr_final_ci_wiring_enabled()
{
	case "$(printf '%s' "${FINAL_CI_WIRING_BEHIND_UPDATE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) return 0 ;;
	esac
	return 1
}

# _pr_final_ci_wiring_note_seen <pr> <head sha>: 0 when the token's own login
# already posted the ai:final-ci-wiring-behind:v1 hold marker for this head
# (any reason except update_branch_failed), 3 when it posted only the
# one-time retry note (reason=update_branch_failed: a refusal without a
# reported conflict, so one more attempt is allowed), 1 when it posted
# neither, 2 when the login or the comments could not be read. The check-only
# note (reason=no_update_path) is not a hold either: it is counted apart in
# PR_FINAL_CI_WIRING_NOTE_NO_UPDATE_SEEN (0 or more; empty unless the read
# succeeded), so a merge path or the sweep still updates the branch.
_pr_final_ci_wiring_note_seen()
{
	local seen_pr="$1" seen_head="$2"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" seen_login="" seen_comments="" seen_count="" seen_hold="" seen_retry="" seen_no_update=""
	PR_FINAL_CI_WIRING_NOTE_NO_UPDATE_SEEN=""
	seen_login="$(gh_retry _safe_gh_jq user 2>/dev/null | jq -r 'if type == "object" then (.login // "") else "" end' 2>/dev/null | tail -n1)"
	[[ "${seen_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]] || return 2
	seen_comments="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${repo}/issues/${seen_pr}/comments?per_page=100" 2>/dev/null)" || return 2
	seen_count="$(printf '%s' "${seen_comments}" | jq -r --arg login "${seen_login}" --arg pr "${seen_pr}" --arg head "${seen_head}" '
		[ .[]? | if type == "array" then .[] else . end
		  | select(type == "object" and ((.user.login // "") == $login))
		  | (.body // "") | select(type == "string")
		  | (capture("<!-- ai:final-ci-wiring-behind:v1 pr=" + $pr + " head=" + $head + " reason=(?<r>[a-z_]+) -->\\s*$")? | .r) ]
		| "\(map(select(. != "update_branch_failed" and . != "no_update_path")) | length) \(map(select(. == "update_branch_failed")) | length) \(map(select(. == "no_update_path")) | length)"' 2>/dev/null | tail -n1)"
	read -r seen_hold seen_retry seen_no_update <<< "${seen_count}"
	[[ "${seen_hold}" =~ ^[0-9]+$ ]] && [[ "${seen_retry}" =~ ^[0-9]+$ ]] && [[ "${seen_no_update}" =~ ^[0-9]+$ ]] || return 2
	PR_FINAL_CI_WIRING_NOTE_NO_UPDATE_SEEN="${seen_no_update}"
	[ "${seen_hold}" -gt 0 ] && return 0
	[ "${seen_retry}" -gt 0 ] && return 3
	return 1
}

_pr_final_ci_wiring_behind_update()
{
	local wiring_pr="${1:-}" wiring_head="${2:-}" wiring_base="${3:-}" wiring_wf="${4:-}" wiring_entry="${5:-}"
	local repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local wiring_path="" wiring_paths="" wiring_behind="" wiring_tip="" wiring_behind_by="" wiring_compare="" wiring_count=""
	local wiring_runs="" wiring_decision="" wiring_file_sha="" wiring_status="" wiring_seen_rc=0 wiring_sync_number=1 wiring_body=""
	PR_FINAL_CI_WIRING_OUTCOME=""
	PR_FINAL_CI_WIRING_REASON=""
	_pr_final_ci_wiring_log()
	{
		PR_FINAL_CI_WIRING_OUTCOME="$1"
		# shellcheck disable=SC2034  # output for callers (documented above)
		PR_FINAL_CI_WIRING_REASON="$2"
		echo "REVIEW_FINAL_CI_WIRING pr=${wiring_pr:-?} head=${wiring_head:-?} entry=${wiring_entry:-?} outcome=$1 reason=$2"
	}
	if ! _pr_final_ci_wiring_enabled; then
		_pr_final_ci_wiring_log current disabled
		return 0
	fi
	if ! [[ "${wiring_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${wiring_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${wiring_wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]] \
		|| ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${repo}" == *..* ]] \
		|| ! [[ "${wiring_base}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${wiring_base}" == *..* ]] || [[ "${wiring_base}" == -* ]] \
		|| { [ "${wiring_entry}" != "dispatch" ] && [ "${wiring_entry}" != "blocked_status" ]; }; then
		[[ "${wiring_head}" =~ ^[0-9a-f]{40}$ ]] || wiring_head="invalid"
		[[ "${wiring_pr}" =~ ^[1-9][0-9]*$ ]] || wiring_pr="invalid"
		[ "${wiring_entry}" = "dispatch" ] || [ "${wiring_entry}" = "blocked_status" ] || wiring_entry="invalid"
		_pr_final_ci_wiring_log unknown invalid_input
		return 0
	fi
	# Built from the validated name only, never from API data.
	wiring_path=".github/workflows/${wiring_wf}"

	# G3: act only on a head whose final CI run succeeded (a run in flight or
	# a red run is not a wiring problem).
	if [ "${wiring_entry}" = "blocked_status" ]; then
		wiring_runs="$(gh_retry _safe_gh_jq "repos/${repo}/actions/workflows/${wiring_wf}/runs?head_sha=${wiring_head}&per_page=20" 2>/dev/null || echo "")"
		wiring_decision="$(printf '%s' "${wiring_runs}" | jq -r --arg path "${wiring_path}" --arg sha "${wiring_head}" "${PR_FINAL_CI_JQ_DECISION_DEF}"' _pr_final_ci_decision' 2>/dev/null | tail -n1)"
		case "${wiring_decision}" in
			"exists success") ;;
			"exists "*|"dispatch "*)
				_pr_final_ci_wiring_log current "final_ci_${wiring_decision#* }"
				return 0
				;;
			*)
				_pr_final_ci_wiring_log unknown final_ci_read_failed
				return 0
				;;
		esac
	fi

	# "Behind": the freshness side channel first (no call), then one compare.
	if [ "${PR_BASE_FRESHNESS_HEAD_SHA:-}" = "${wiring_head}" ] && [ "${PR_BASE_FRESHNESS_BASE_REF:-}" = "${wiring_base}" ]; then
		if [ "${PR_BASE_FRESHNESS_OUTCOME:-}" = "fresh" ]; then
			wiring_behind="no"
		elif [ -n "${PR_BASE_FRESHNESS_BASE_PATHS:-}" ]; then
			# Set only after a successful compare for this head; an empty list
			# is ambiguous (never set, or no base-side file) and re-reads below.
			wiring_paths="${PR_BASE_FRESHNESS_BASE_PATHS}"
			wiring_tip="${PR_BASE_FRESHNESS_BASE_SHA:-}"
			wiring_behind_by="${PR_BASE_FRESHNESS_BEHIND_BY:-}"
		fi
	fi
	if [ -z "${wiring_behind}" ] && [ -z "${wiring_paths}" ]; then
		wiring_compare="$(gh_retry _safe_gh_jq "repos/${repo}/compare/${wiring_head}...${wiring_base}" 2>/dev/null || echo "")"
		wiring_behind_by="$(printf '%s' "${wiring_compare}" | jq -r 'if (type == "object" and (.ahead_by | type) == "number") then .ahead_by else empty end' 2>/dev/null | tail -n1)"
		if ! [[ "${wiring_behind_by}" =~ ^[0-9]+$ ]]; then
			_pr_final_ci_wiring_log unknown compare_failed
			return 0
		fi
		if [ "${wiring_behind_by}" -eq 0 ]; then
			wiring_behind="no"
		else
			wiring_count="$(printf '%s' "${wiring_compare}" | jq -r 'if (type == "object") then ((.files // []) | length) else empty end' 2>/dev/null | tail -n1)"
			if ! [[ "${wiring_count}" =~ ^[0-9]+$ ]]; then
				_pr_final_ci_wiring_log unknown compare_failed
				return 0
			fi
			# Same base tip rule as _pr_base_freshness: the last listed commit,
			# only when the commit list is complete.
			wiring_tip="$(printf '%s' "${wiring_compare}" | jq -r '
				if (type == "object" and (.commits | type) == "array" and (.commits | length) > 0 and (.commits | length) == .ahead_by)
				then (.commits[-1].sha // "") else "" end' 2>/dev/null | tail -n1)"
			if [ "${wiring_count}" -ge 300 ]; then
				wiring_paths="__truncated__"
			else
				wiring_paths="$(printf '%s' "${wiring_compare}" | jq -r '
					[ (.files // [])[]? | select(type == "object") | (.filename // empty), (.previous_filename // empty)
					  | select(type == "string") ] | unique | .[]' 2>/dev/null || true)"
				[ -n "${wiring_paths}" ] || wiring_behind="no"
			fi
		fi
	fi
	[[ "${wiring_tip}" =~ ^[0-9a-f]{40}$ ]] || wiring_tip=""
	if [ -z "${wiring_behind}" ]; then
		if [ "${wiring_paths}" = "__truncated__" ]; then
			# GitHub lists at most 300 files: ask whether the newest base commit
			# to the file is in the head. An older base commit to the file that
			# is not its ancestor (unusual merge histories) is not seen; that
			# fails toward dispatching as before.
			wiring_file_sha="$(gh_retry _safe_gh_jq "repos/${repo}/commits?sha=${wiring_base}&path=${wiring_path}&per_page=1" 2>/dev/null \
				| jq -r 'if type == "array" and length > 0 and (.[0] | type) == "object" then (.[0].sha // "") else "" end' 2>/dev/null | tail -n1)"
			if ! [[ "${wiring_file_sha}" =~ ^[0-9a-f]{40}$ ]]; then
				_pr_final_ci_wiring_log unknown compare_failed
				return 0
			fi
			wiring_status="$(gh_retry _safe_gh_jq "repos/${repo}/compare/${wiring_file_sha}...${wiring_head}" 2>/dev/null \
				| jq -r 'if type == "object" then (.status // "") else "" end' 2>/dev/null | tail -n1)"
			case "${wiring_status}" in
				ahead|identical) wiring_behind="no" ;;
				behind|diverged) wiring_behind="yes" ;;
				*)
					_pr_final_ci_wiring_log unknown compare_failed
					return 0
					;;
			esac
		elif printf '%s\n' "${wiring_paths}" | grep -Fxq -- "${wiring_path}"; then
			wiring_behind="yes"
		else
			wiring_behind="no"
		fi
	fi
	if [ "${wiring_behind}" != "yes" ]; then
		_pr_final_ci_wiring_log current not_behind
		return 0
	fi
	# Check-only callers (scripts/review_enable_auto_merge.sh on its exits
	# that never merge, and an e2e-smoke-test PR reached by the sweep): no
	# lease, no branch update, and no dispatch on the stale workflow copy
	# (G2). One note per head records the reason on the PR (G2), only when
	# the comment read succeeded and nothing is recorded for the head yet;
	# it is not a hold, so a merge path or the final-CI sweep still updates
	# the branch.
	if [ "${PR_FINAL_CI_WIRING_NO_UPDATE:-}" = "true" ]; then
		wiring_seen_rc=0
		_pr_final_ci_wiring_note_seen "${wiring_pr}" "${wiring_head}" || wiring_seen_rc=$?
		if [ "${wiring_seen_rc}" -eq 1 ] && [ "${PR_FINAL_CI_WIRING_NOTE_NO_UPDATE_SEEN:-}" = "0" ]; then
			# shellcheck disable=SC2016  # Markdown backticks, not expansions
			wiring_body="$(printf '**Final CI held: this branch'"'"'s `%s` is behind `%s`**\n\n`%s` has commits to `%s` that this branch lacks. The final CI run starts from the branch'"'"'s own copy of that workflow but runs the scripts from `%s`, so on this head it would fail or never report its result. This review step does not update branches (auto-merge is off, or the PR is an e2e smoke test), so the final CI was not started on head `%s`. Update the branch from `%s`; the final CI then runs on the new head.\n\n<!-- ai:final-ci-wiring-behind:v1 pr=%s head=%s reason=no_update_path -->' \
				"${wiring_wf}" "${wiring_base}" "${wiring_base}" "${wiring_path}" "${wiring_base}" "${wiring_head}" "${wiring_base}" "${wiring_pr}" "${wiring_head}")"
			# One attempt only: a retried POST whose first response was lost
			# would post a duplicate.
			_safe_gh_jq -X POST "repos/${repo}/issues/${wiring_pr}/comments" -f "body=${wiring_body}" >/dev/null 2>&1 \
				|| echo "::warning::REVIEW_FINAL_CI_WIRING pr=${wiring_pr} head=${wiring_head} comment=post_failed (the next call posts the note again)"
		fi
		_pr_final_ci_wiring_log held no_update_path
		return 0
	fi

	# Once per head (an update costs a new review and a new final CI): a
	# recorded refusal holds without another attempt. Read before the lease,
	# so a held PR never takes the lease without updating.
	wiring_seen_rc=0
	_pr_final_ci_wiring_note_seen "${wiring_pr}" "${wiring_head}" || wiring_seen_rc=$?
	if [ "${wiring_seen_rc}" -eq 0 ]; then
		_pr_final_ci_wiring_log held recorded
		return 0
	fi
	if ! _merge_lease_gate "${wiring_pr}" "${wiring_base}" "${wiring_head}" update; then
		echo "  [merge-lease] PR #${wiring_pr}: the merge lease for ${wiring_base} is held by or queued for PR #${MERGE_LEASE_BLOCKER:-?}; not updating the branch's CI wiring. This PR is labelled ${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait} and is re-dispatched when the lease is granted to it."
		_pr_final_ci_wiring_log held "merge_lease_${MERGE_LEASE_REASON:-held}"
		return 0
	fi

	# The freshness update chain reads these side channels; shadow them for
	# this call only (bash dynamic scope), so a stale result for another head
	# never binds the marker and nothing leaks to the caller. No overlap: the
	# update head gets the full suite.
	local PR_BASE_FRESHNESS_BASE_SHA="${wiring_tip}" PR_BASE_FRESHNESS_BASE_REF="${wiring_base}"
	local PR_BASE_FRESHNESS_BEHIND_BY="${wiring_behind_by}" PR_BASE_FRESHNESS_OVERLAP=""
	# The update chain dispatches the update head's final CI only when this
	# is set; the poller never sets it, so default it to the workflow checked.
	local REVIEW_FINAL_CI_WORKFLOW="${REVIEW_FINAL_CI_WORKFLOW:-${wiring_wf}}"
	if ! _pr_base_sync_for_fresh_ci "${wiring_pr}" "${wiring_head}"; then
		if [ "${wiring_seen_rc}" -eq 1 ] || [ "${wiring_seen_rc}" -eq 3 ]; then
			# One PR read, only before a note would be posted: a head that
			# moved (another caller updated the branch) records nothing. The
			# hold marker needs a confirmed conflict (`mergeable: false`); any
			# other result (true, null while GitHub computes it, an unreadable
			# read) posts the one-time retry note first, so the next call tries
			# once more and the update lease is renewed at most twice per head.
			local wiring_after_json="" wiring_after_head="" wiring_after_mergeable=""
			wiring_after_json="$(gh_retry _safe_gh_jq "repos/${repo}/pulls/${wiring_pr}" 2>/dev/null || echo "")"
			wiring_after_head="$(printf '%s' "${wiring_after_json}" | jq -r 'if type == "object" then (.head.sha // "") else "" end' 2>/dev/null | tail -n1)"
			wiring_after_mergeable="$(printf '%s' "${wiring_after_json}" | jq -r 'if type == "object" then (.mergeable | tostring) else "" end' 2>/dev/null | tail -n1)"
			if [[ "${wiring_after_head}" =~ ^[0-9a-f]{40}$ ]] && [ "${wiring_after_head}" != "${wiring_head}" ]; then
				_pr_final_ci_wiring_log held head_moved
				return 0
			fi
			if [ "${wiring_after_mergeable}" != "false" ] && [ "${wiring_seen_rc}" -eq 1 ]; then
				# shellcheck disable=SC2016  # Markdown backticks, not expansions
				wiring_body="$(printf '**Final CI held: this branch'"'"'s `%s` is behind `%s`**\n\n`%s` has commits to `%s` that this branch lacks, and GitHub did not update the branch from `%s` this time (no merge conflict was reported), so the final CI was not started on head `%s`. The next check tries the update once more; if that also fails, the final CI waits until the branch is updated from `%s`.\n\n<!-- ai:final-ci-wiring-behind:v1 pr=%s head=%s reason=update_branch_failed -->' \
					"${wiring_wf}" "${wiring_base}" "${wiring_base}" "${wiring_path}" "${wiring_base}" "${wiring_head}" "${wiring_base}" "${wiring_pr}" "${wiring_head}")"
				# One attempt only: a retried POST whose first response was lost
				# would post a duplicate.
				_safe_gh_jq -X POST "repos/${repo}/issues/${wiring_pr}/comments" -f "body=${wiring_body}" >/dev/null 2>&1 \
					|| echo "::warning::REVIEW_FINAL_CI_WIRING pr=${wiring_pr} head=${wiring_head} comment=post_failed (the next call tries the update again)"
				_pr_final_ci_wiring_log held update_branch_failed
				return 0
			fi
			# The cause sentence follows the PR read: a conflict only when GitHub
			# reported one (a second refusal may come without one).
			local wiring_cause="because of a merge conflict"
			[ "${wiring_after_mergeable}" = "false" ] || wiring_cause="twice, without reporting a merge conflict"
			# shellcheck disable=SC2016  # Markdown backticks, not expansions
			wiring_body="$(printf '**Final CI held: this branch'"'"'s `%s` is behind `%s`**\n\n`%s` has commits to `%s` that this branch lacks. The final CI run starts from the branch'"'"'s own copy of that workflow but runs the scripts from `%s`, so on this head it would fail or never report its result. GitHub refused to update the branch from `%s` %s, so the final CI was not started on head `%s`. Update the branch from `%s` (resolving any conflict); the final CI then runs on the new head.\n\n<!-- ai:final-ci-wiring-behind:v1 pr=%s head=%s reason=update_branch_refused -->' \
				"${wiring_wf}" "${wiring_base}" "${wiring_base}" "${wiring_path}" "${wiring_base}" "${wiring_base}" "${wiring_cause}" "${wiring_head}" "${wiring_base}" "${wiring_pr}" "${wiring_head}")"
			# One attempt only: a retried POST whose first response was lost
			# would post a duplicate.
			_safe_gh_jq -X POST "repos/${repo}/issues/${wiring_pr}/comments" -f "body=${wiring_body}" >/dev/null 2>&1 \
				|| echo "::warning::REVIEW_FINAL_CI_WIRING pr=${wiring_pr} head=${wiring_head} comment=post_failed (the next call tries the update again)"
		else
			# The comment read failed: nothing is posted (no comment per tick),
			# so surface the refusal as a run-level warning instead.
			echo "::warning::REVIEW_FINAL_CI_WIRING pr=${wiring_pr} head=${wiring_head} comment=skipped reason=notes_unreadable (the branch update was refused; the next call tries again)"
		fi
		_pr_final_ci_wiring_log held update_branch_refused
		return 0
	fi
	if _pr_base_sync_observe_update_commit "${wiring_pr}" "${wiring_head}" "${wiring_base}"; then
		# The update number counts earlier sync markers; an unreadable count
		# falls back to 1 (as _pr_final_ci_refused_base_update).
		wiring_sync_number=1
		if _pr_base_sync_count "${wiring_pr}"; then
			wiring_sync_number=$(( PR_BASE_SYNC_COUNT + 1 ))
		fi
		# shellcheck disable=SC2016  # Markdown backticks, not expansions
		PR_BASE_SYNC_MARKER_LEAD="$(printf 'This branch'"'"'s `%s` lacks commits that `%s` (%s commit(s) ahead) made to it. The final CI run that the merge requires runs the branch'"'"'s copy of that workflow with the scripts from `%s`, so on the old head it would fail or never report its result.' "${wiring_path}" "${wiring_base}" "${wiring_behind_by:-?}" "${wiring_base}")" \
			_pr_base_sync_marker_post "${wiring_pr}" "${wiring_head}" "${wiring_sync_number}" "${MERGE_BASE_FRESHNESS_MAX_SYNCS:-2}" \
			"${PR_BASE_SYNC_BASE_SHA}" "${PR_BASE_SYNC_MERGE_SHA}"
		_pr_base_sync_dispatch_final_ci "${wiring_pr}" "${wiring_head}" "${PR_BASE_SYNC_MERGE_SHA}"
		_pr_final_ci_wiring_log updated ci_wiring_behind
	else
		echo "MERGE_BASE_SYNC_MARKER pr=${wiring_pr} head_sha=${wiring_head} outcome=skipped reason=${PR_BASE_SYNC_OBSERVE_REASON:-head_not_observed}"
		_pr_final_ci_wiring_log requested "${PR_BASE_SYNC_OBSERVE_REASON:-head_not_observed}"
	fi
	return 0
}

# ---------------------------------------------------------------------------
# Shared merge-tree conflict probe (merge-lease Phase 4, Refs #9781; plan
# docs/plans/merge-lease-parallel-merges-plan.md D9).
#
# Lifted near-verbatim from scripts/review_merge_train.sh _mt_conflict_probe
# (issue #6570) under the plan's `_pr_*` names, so the freshness gate and
# the merge lease can probe without sourcing the merge train. The train's
# `_mt_git`, `_mt_git_fetch`, `_mt_probe_repo_init`, `_mt_probe_repo_cleanup`
# and `_mt_conflict_probe` are thin wrappers over these (plan D9) whenever the
# staged library has them (`declare -F`); they keep their own body only as the
# fallback for an older staged library. The wrappers set these optional
# overrides as `local` variables, so they apply to that call only (bash
# dynamic scope) and never leak into the lease or freshness callers:
#   _PR_PROBE_DIR_PREFIX  probe directory name prefix (default
#                         pr-merge-tree-probe; the train: merge-train-probe)
#   _PR_PROBE_REPO_SLUG   owner/repo of the origin (default
#                         PR_CHECKS_REPOSITORY, then GITHUB_REPOSITORY)
#   _PR_PROBE_LOG_PREFIX  log prefix (default PR_MERGE_TREE_PROBE_REPO)
#   _PR_PROBE_LOG_FN      a function that prints the log line (default: echo
#                         to stderr; the train passes _mt_log, its stdout)
# One probe repository serves the whole process, whichever caller creates it.
#
#   _pr_merge_tree_probe <sha_a> <sha_b>
#       Sets _PR_PROBE_RESULT to none (the two commits merge cleanly),
#       conflict (_PR_PROBE_PATHS lists the conflicted paths) or unknown
#       (bad input, missing objects, fetch failure, too-shallow history, or
#       any other git outcome). Always returns 0 and prints nothing on
#       stdout, so callers that print one word stay intact.
#
# API budget: none. Missing commits are fetched by SHA (120 s timeout,
# --no-tags --no-write-fetch-head; --depth=200 only in the private
# repository or an already-shallow clone). Outside a git checkout the probe
# uses a private bare repository under RUNNER_TEMP (or TMPDIR) whose origin
# is ${GITHUB_SERVER_URL}/<repo>.git; GH_TOKEN (or GITHUB_TOKEN)
# authenticates the fetch through GIT_CONFIG_* variables exported in a
# subshell, never argv or a config file. Logs
# `PR_MERGE_TREE_PROBE_REPO outcome=created|unavailable` on stderr.
# merge-tree writes only objects; it runs no hooks.
declare -gA _PR_PROBE_FETCHED_SHA=()
_PR_GIT_PROBE_OK=""
_PR_PROBE_RESULT="unknown"
_PR_PROBE_PATHS=""
_PR_PROBE_TREE=""
_PR_PROBE_GIT_DIR=""
# Prefix of the probe directory this process created (the EXIT-trap cleanup
# runs outside the creating call, so it cannot read the caller's override).
_PR_PROBE_DIR_ACTIVE_PREFIX="pr-merge-tree-probe"

# git in the probe repository when one was created, else in the cwd checkout.
_pr_mt_git()
{
	if [ -n "${_PR_PROBE_GIT_DIR}" ]; then
		git --git-dir="${_PR_PROBE_GIT_DIR}" "$@"
	else
		git "$@"
	fi
}

# `_pr_mt_git_fetch <seconds> <fetch args...>`: git fetch under `timeout`, in
# the probe repository when one exists. For an https origin the token rides
# as an HTTP extraheader through GIT_CONFIG_* variables exported in a
# subshell, so it never reaches argv (ps) or a config file.
_pr_mt_git_fetch()
{
	local fetch_timeout="$1"
	shift
	(
		local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}" server="${GITHUB_SERVER_URL:-https://github.com}" basic
		local -a git_dir_args=()
		if [ -n "${_PR_PROBE_GIT_DIR}" ]; then
			git_dir_args=(--git-dir="${_PR_PROBE_GIT_DIR}")
			if [ -n "${token}" ] && [[ "${server}" =~ ^https:// ]]; then
				basic="$(printf 'x-access-token:%s' "${token}" | base64 | tr -d '\n')"
				export GIT_CONFIG_COUNT=1
				export GIT_CONFIG_KEY_0="http.${server%/}/.extraheader"
				export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic}"
			fi
		fi
		GIT_TERMINAL_PROMPT=0 timeout "${fetch_timeout}" git ${git_dir_args[@]+"${git_dir_args[@]}"} fetch "$@"
	)
}

_pr_probe_repo_cleanup()
{
	if [ -n "${_PR_PROBE_GIT_DIR}" ] && [[ "${_PR_PROBE_GIT_DIR}" == */"${_PR_PROBE_DIR_ACTIVE_PREFIX:-pr-merge-tree-probe}".* ]]; then
		rm -rf -- "${_PR_PROBE_GIT_DIR}"
	fi
}

# `_pr_probe_repo_log <message>`: one probe-repository log line through
# _PR_PROBE_LOG_FN when it names a function, else on stderr.
_pr_probe_repo_log()
{
	if [ -n "${_PR_PROBE_LOG_FN:-}" ] && declare -F "${_PR_PROBE_LOG_FN}" >/dev/null 2>&1; then
		"${_PR_PROBE_LOG_FN}" "$1"
	else
		echo "$1" >&2
	fi
}

# Create the private probe repository; returns 1 (probe stays unknown) when
# the repository slug is unusable or git cannot create it. The cleanup is
# chained onto the caller's EXIT trap instead of replacing it.
_pr_probe_repo_init()
{
	local server="${GITHUB_SERVER_URL:-https://github.com}" probe_dir
	local probe_repo="${_PR_PROBE_REPO_SLUG:-${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}}" probe_prev_trap=""
	local probe_log="${_PR_PROBE_LOG_PREFIX:-PR_MERGE_TREE_PROBE_REPO}" probe_prefix="${_PR_PROBE_DIR_PREFIX:-pr-merge-tree-probe}"
	# Overrides come from code, never from input: anything but [A-Za-z0-9_-]
	# falls back to the defaults.
	[[ "${probe_log}" =~ ^[A-Za-z0-9_]+$ ]] || probe_log="PR_MERGE_TREE_PROBE_REPO"
	[[ "${probe_prefix}" =~ ^[A-Za-z0-9_-]+$ ]] || probe_prefix="pr-merge-tree-probe"
	# `..` is rejected as in _pr_ensure_final_ci: the slug becomes part of the origin URL.
	if ! [[ "${probe_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${probe_repo}" == *..* ]]; then
		_pr_probe_repo_log "${probe_log} outcome=unavailable reason=invalid_repository"
		return 1
	fi
	if ! probe_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/${probe_prefix}.XXXXXX" 2>/dev/null)" \
		|| ! git init -q --bare "${probe_dir}" >/dev/null 2>&1 \
		|| ! git --git-dir="${probe_dir}" remote add origin "${server%/}/${probe_repo}.git" >/dev/null 2>&1; then
		[ -n "${probe_dir:-}" ] && rm -rf -- "${probe_dir}"
		_pr_probe_repo_log "${probe_log} outcome=unavailable reason=init_failed"
		return 1
	fi
	_PR_PROBE_GIT_DIR="${probe_dir}"
	_PR_PROBE_DIR_ACTIVE_PREFIX="${probe_prefix}"
	probe_prev_trap="$(trap -p EXIT)"
	if [ -n "${probe_prev_trap}" ]; then
		eval "set -- ${probe_prev_trap}"
		probe_prev_trap="${3:-}"
	fi
	# shellcheck disable=SC2064
	trap -- "_pr_probe_repo_cleanup${probe_prev_trap:+; ${probe_prev_trap}}" EXIT
	_pr_probe_repo_log "${probe_log} outcome=created origin=${server%/}/${probe_repo}.git"
	return 0
}

_pr_merge_tree_probe()
{
	local own_sha="$1" other_sha="$2" probe_sha probe_out="" probe_rc=0
	local -a probe_missing=() probe_depth=()
	_PR_PROBE_RESULT="unknown"
	_PR_PROBE_PATHS=""
	_PR_PROBE_TREE=""
	if ! [[ "${own_sha}" =~ ^[0-9a-f]{40}$ && "${other_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		return 0
	fi
	if [ -z "${_PR_GIT_PROBE_OK}" ]; then
		if git merge-tree --write-tree --name-only --no-messages HEAD HEAD >/dev/null 2>&1; then
			_PR_GIT_PROBE_OK="yes"
		elif ! git rev-parse --git-dir >/dev/null 2>&1 && _pr_probe_repo_init; then
			# No checkout here: probe in the private repository instead.
			_PR_GIT_PROBE_OK="yes"
		else
			_PR_GIT_PROBE_OK="no"
		fi
	fi
	[ "${_PR_GIT_PROBE_OK}" = "yes" ] || return 0
	for probe_sha in "${own_sha}" "${other_sha}"; do
		if ! _pr_mt_git cat-file -e "${probe_sha}^{commit}" 2>/dev/null; then
			[ "${_PR_PROBE_FETCHED_SHA[${probe_sha}]:-}" = "failed" ] && return 0
			probe_missing+=("${probe_sha}")
		fi
	done
	if [ "${#probe_missing[@]}" -gt 0 ]; then
		# The private probe repository starts empty: fetch shallow there too.
		if [ -n "${_PR_PROBE_GIT_DIR}" ] || [ "$(git rev-parse --is-shallow-repository 2>/dev/null || true)" = "true" ]; then
			probe_depth=(--depth=200)
		fi
		if ! _pr_mt_git_fetch 120 --no-tags --quiet --no-write-fetch-head \
			${probe_depth[@]+"${probe_depth[@]}"} origin "${probe_missing[@]}" >/dev/null 2>&1; then
			for probe_sha in "${probe_missing[@]}"; do
				_PR_PROBE_FETCHED_SHA[${probe_sha}]="failed"
			done
			return 0
		fi
		for probe_sha in "${probe_missing[@]}"; do
			if _pr_mt_git cat-file -e "${probe_sha}^{commit}" 2>/dev/null; then
				_PR_PROBE_FETCHED_SHA[${probe_sha}]="ok"
			else
				_PR_PROBE_FETCHED_SHA[${probe_sha}]="failed"
				return 0
			fi
		done
	fi
	probe_out="$(_pr_mt_git merge-tree --write-tree --name-only --no-messages "${own_sha}" "${other_sha}" 2>/dev/null)" || probe_rc=$?
	case "${probe_rc}" in
		0)
			_PR_PROBE_RESULT="none"
			_PR_PROBE_TREE="$(printf '%s\n' "${probe_out}" | sed -n '1p')"
			;;
		1)
			_PR_PROBE_RESULT="conflict"
			_PR_PROBE_PATHS="$(printf '%s\n' "${probe_out}" | sed '1d' | sed '/^$/d' | sort -u)"
			;;
		*) _PR_PROBE_RESULT="unknown" ;;
	esac
	return 0
}

# `_pr_soft_merge_tree_probe <own_sha> <other_sha>`: the probe the soft path
# class uses (freshness and merge-mode lease). A clean merge counts only when
# every path whose merged content matches neither side (content combined
# from both) is soft. merge-ort follows renames, and git before 2.45 has no
# switch to turn that off in merge-tree, so a soft path renamed to a hard
# path on one side and edited on the other merges "clean" into the hard
# path. That path then differs from both sides, and the result drops to
# unknown (fail closed: overlap / wait). _PR_PROBE_HARD_COMBINED lists such
# paths. Always returns 0 and prints nothing on stdout.
_PR_PROBE_HARD_COMBINED=""
_pr_soft_merge_tree_probe()
{
	local soft_own="$1" soft_other="$2" soft_from_own soft_from_other soft_combined soft_chk_entries soft_chk_p
	_PR_PROBE_HARD_COMBINED=""
	_pr_merge_tree_probe "${soft_own}" "${soft_other}"
	[ "${_PR_PROBE_RESULT}" = "none" ] || return 0
	if ! [[ "${_PR_PROBE_TREE}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! soft_from_own="$(_pr_mt_git diff-tree -r --no-renames --name-only "${soft_own}" "${_PR_PROBE_TREE}" 2>/dev/null)" \
		|| ! soft_from_other="$(_pr_mt_git diff-tree -r --no-renames --name-only "${soft_other}" "${_PR_PROBE_TREE}" 2>/dev/null)"; then
		_PR_PROBE_RESULT="unknown"
		return 0
	fi
	[ -n "${soft_from_own}" ] && [ -n "${soft_from_other}" ] || return 0
	soft_combined="$(grep -Fx -f <(printf '%s\n' "${soft_from_own}") <(printf '%s\n' "${soft_from_other}") | sort -u || true)"
	soft_chk_entries="$(_merge_lease_soft_entries 2>/dev/null)"
	while IFS= read -r soft_chk_p; do
		[ -n "${soft_chk_p}" ] || continue
		_merge_lease_soft_path "${soft_chk_p}" "${soft_chk_entries}" && continue
		_PR_PROBE_HARD_COMBINED+="${soft_chk_p}"$'\n'
	done <<< "${soft_combined}"
	_PR_PROBE_HARD_COMBINED="${_PR_PROBE_HARD_COMBINED%$'\n'}"
	[ -z "${_PR_PROBE_HARD_COMBINED}" ] || _PR_PROBE_RESULT="unknown"
	return 0
}

# ---------------------------------------------------------------------------
# Soft path class (merge-lease Phase 4, Refs #9781; plan D1/D2).
#
# Paths in MERGE_LEASE_SOFT_PATHS (fragment directories and generated or
# append-mostly docs) do not count as overlap on their own: when every path
# a PR shares with the base (freshness) or with the lease holder (merge
# mode) is soft, a `git merge-tree` probe decides, and only a clean merge
# relaxes the decision. Any shared non-soft path keeps today's rule before
# any probe runs; a probe that cannot decide keeps it too.
#
# Env: MERGE_LEASE_SOFT_PATHS (default
# README.md,agents.md,AGENTS.md,docs/INVENTORY.md,CHANGELOG.md,agents.d/,changelog.d/).
# Comma-separated; an entry ending in `/` is a literal directory prefix,
# any other entry an exact path. Entries with glob characters (`*`, `?`,
# `[`), a leading `/` or a `..` segment are skipped with a warning. An empty
# value, `none` or `off` (case-insensitive) turns the class off, which is
# exactly the behaviour before Phase 4.

# Prints the usable entries, one per line.
_merge_lease_soft_entries()
{
	local soft_raw soft_entry
	soft_raw="${MERGE_LEASE_SOFT_PATHS-README.md,agents.md,AGENTS.md,docs/INVENTORY.md,CHANGELOG.md,agents.d/,changelog.d/}"
	case "$(printf '%s' "${soft_raw}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
		""|none|off) return 0 ;;
	esac
	local -a soft_parts=()
	IFS=',' read -r -a soft_parts <<< "${soft_raw}"
	for soft_entry in ${soft_parts[@]+"${soft_parts[@]}"}; do
		soft_entry="${soft_entry#"${soft_entry%%[![:space:]]*}"}"
		soft_entry="${soft_entry%"${soft_entry##*[![:space:]]}"}"
		[ -n "${soft_entry}" ] || continue
		case "${soft_entry}" in
			*'*'*|*'?'*|*'['*|/*|..|../*|*/..|*/../*)
				echo "::warning::MERGE_LEASE_SOFT_PATHS entry '${soft_entry}' is not a literal relative path or directory prefix; ignoring it." >&2
				continue
				;;
		esac
		printf '%s\n' "${soft_entry}"
	done
	return 0
}

# 0 when <path> is in the soft class (literal comparison; no glob is
# evaluated). Optional <entries> (newline list) skips re-reading the env.
_merge_lease_soft_path()
{
	local soft_p="$1" soft_entries soft_e
	if [ "$#" -ge 2 ]; then
		soft_entries="$2"
	else
		soft_entries="$(_merge_lease_soft_entries 2>/dev/null)"
	fi
	[ -n "${soft_p}" ] && [ -n "${soft_entries}" ] || return 1
	while IFS= read -r soft_e; do
		[ -n "${soft_e}" ] || continue
		[[ "${soft_p}" == "${soft_e}" ]] && return 0
		[[ "${soft_e}" == */ && "${soft_p}" == "${soft_e}"* ]] && return 0
	done <<< "${soft_entries}"
	return 1
}

# `_merge_lease_split_soft <newline list>`: sets _MERGE_LEASE_HARD_LIST and
# _MERGE_LEASE_SOFT_LIST (newline-separated) in the caller's shell. With the
# class off every path is hard.
_merge_lease_split_soft()
{
	local split_list="$1" split_p split_entries
	_MERGE_LEASE_HARD_LIST=""
	_MERGE_LEASE_SOFT_LIST=""
	split_entries="$(_merge_lease_soft_entries 2>/dev/null)"
	# Ignored MERGE_LEASE_SOFT_PATHS entries warn once per process, not once
	# per call (the multi-holder paths call this per holder per candidate).
	if [ -z "${_MERGE_LEASE_SOFT_WARNED:-}" ]; then
		_MERGE_LEASE_SOFT_WARNED=1
		_merge_lease_soft_entries >/dev/null
	fi
	while IFS= read -r split_p; do
		[ -n "${split_p}" ] || continue
		if [ -n "${split_entries}" ] && _merge_lease_soft_path "${split_p}" "${split_entries}"; then
			_MERGE_LEASE_SOFT_LIST+="${split_p}"$'\n'
		else
			_MERGE_LEASE_HARD_LIST+="${split_p}"$'\n'
		fi
	done <<< "${split_list}"
	_MERGE_LEASE_HARD_LIST="${_MERGE_LEASE_HARD_LIST%$'\n'}"
	_MERGE_LEASE_SOFT_LIST="${_MERGE_LEASE_SOFT_LIST%$'\n'}"
	return 0
}

# ---------------------------------------------------------------------------
# Merge lease (2026-11-17; follow-up to #9642, answers Q1-Q5: A).
#
# Why: approved PRs into `main` were updated from `main` up to six times
# without merging (#9367 update 6, #9368 update 5). Every update made a head
# that needed a full CI run, which took 1.5-2.5 hours on the congested runner
# pool, and `main` moved again under a shared file before it finished. N
# approved PRs racing meant every merge invalidated the others.
#
# The lease serializes the expensive part per base branch. Only the PR that
# holds the lease for a base may update its branch from that base (and so
# start the combined-tree CI run). Another PR that needs an update waits
# without updating, labelled MERGE_LEASE_WAIT_LABEL. While a lease is held, a
# PR that needs no update still merges at once unless it changes a file the
# holder changes (that merge would make the holder's CI run stale).
#
# Storage: one ref per base, MERGE_LEASE_REF_PREFIX<base> (default
# refs/merge-lease/<base>), pointing at a commit whose message is the lease
# record:
#   merge-lease v1 / state=held|released / base= / pr= / head= / expires=<epoch>
#   / updated=<epoch> / paths: followed by one path per line, or `*`
# Every write creates a child of the commit the writer read and moves the ref
# with a fast-forward-only PATCH (force=false), so two writers that read the
# same record cannot both win: the second PATCH is not a fast-forward and is
# refused. That is the compare-and-swap. A ref outside refs/heads and
# refs/tags starts no workflow. The first write creates the ref (POST
# git/refs; a concurrent create loses with "already exists").
#
# Order (who gets a free lease): MERGE_TRAIN_PRIORITY_LABELS on the PR first,
# then the lowest PR number, the merge train's rule. The merge-train release
# step (scripts/review_merge_train.sh, every poll tick and every PR close)
# frees a lease whose holder closed or whose expiry passed, then reserves it
# for the first waiter and re-dispatches that waiter's review.
#
# Fail-open: the lease only ever delays an update or a merge. Any read or
# write failure logs MERGE_LEASE ... outcome=unavailable and the caller
# behaves as before the lease existed.
#
# Env: MERGE_LEASE_ENABLED (default true; true/1/yes/on), MERGE_LEASE_TTL_MINUTES
# (default 240, 30-1440; renewed each time the holder updates),
# MERGE_LEASE_MAX_HOLD_MINUTES (default 720, at least the TTL; no renewal
# after that long since first acquisition),
# MERGE_LEASE_REF_PREFIX (default refs/merge-lease/), MERGE_LEASE_WAIT_LABEL
# (default ai:merge-lease-wait), MERGE_LEASE_MAX_PATHS (default 200; a holder
# with more paths is recorded as `*`, which every PR overlaps).
#
# API budget (§15): a read is one `git/matching-refs` call plus one
# `git/commits/<sha>` read when the ref exists. A write adds one `git/commits`
# POST, one ref PATCH (or POST on first use) and, on first use only, one
# `commits/<base>` read for a tree. The update path adds one paginated
# `pulls?base=` listing when the lease is free (queue order). A wait adds one
# label POST. The holder's paths come from _pr_base_freshness's existing
# files read (PR_BASE_FRESHNESS_PR_PATHS); a merge-mode check against a
# holder reads the PR's files only when that cache is for another PR.
#
# Log: `MERGE_LEASE pr=<n> base=<ref> mode=update|merge|tick outcome=<word>
# reason=<token> holder=<n|-> blocker=<n|->`.
# Metrics (observability only; merge-lease-parallel Phase 1, Refs #9781):
# `MERGE_LEASE_METRIC event=<e> base=<ref|-> pr=<n|-> mode=update|merge|tick
# reason=<token> holder=<n|-> hold_s=<n|-> waiters=<n|-> ts=<epoch|->`, one
# line after each gate and tick decision (_merge_lease_metric). Env:
# MERGE_LEASE_METRICS_ENABLED (default true; true/1/yes/on). No API call;
# a metric failure never changes a decision.
# Revocation, grant-time eligibility and skip-until-head-changes
# (merge-lease-parallel Phase 2, Refs #9781): _merge_lease_pr_eligible tells
# whether a holder or waiter can still merge (not draft, no `[skip ai]` title,
# a pipeline-posted ai-review/head-gate success on the head, final CI not
# failed). The merge-train tick revokes an ineligible or idle holder and
# grants only to an eligible waiter. A PR released for being expired or
# ineligible is recorded as `skip=<pr>:<head>` lines in the lease record
# (between `updated=` and `paths:`, at most 50) and is passed over in queue
# order until its head SHA changes. Env (defaults here):
# MERGE_LEASE_REVOKE_ENABLED (default true; false turns off revocation,
# eligibility, the skip list and `skip=` writes, so the record is byte-for-byte
# the old format), MERGE_LEASE_IDLE_MINUTES (default 60, 0-1440; 0 turns the
# idle test off). Every read failure is eligible (fail-open).
# Forward-merge priority (merge-lease-parallel Phase 3, Refs #9781): the
# order is a priority label or a same-repository head ref starting with an
# entry of MERGE_LEASE_PRIORITY_HEAD_PREFIXES, then the lowest PR number.
# Env (default here): MERGE_LEASE_PRIORITY_HEAD_PREFIXES (default
# auto/forward-merge-stable-; comma-separated literal prefixes; empty, none
# or off turns it off). The head ref comes from the listing already read.
# Several holders per base (merge-lease-parallel Phase 5, Refs #9781):
# MERGE_LEASE_MAX_HOLDERS (default 1, 1-5; not a whole number gives 1 with a
# warning, out of range is clamped) PRs whose paths are pairwise disjoint
# (Phase 4 soft class included) may hold one base's lease at once. With 2+
# live holders the record keeps pr=/head=/acquired=/updated= for the primary,
# expires= is the latest holder expiry (an old v1 reader then sees the primary
# plus the union of paths: a stricter lease, never a weaker one), and lines
# before `paths:` list every holder:
#   holder=<pr> head=<sha|> expires=<epoch> acquired=<epoch> updated=<epoch>
#   holder_path=<pr> <path>   (one per path; `*` for a holder over the cap)
# `paths:` holds the union, or `*`. One holder is written in the v1 shape, so
# with the default of 1 the code path, record bytes, log lines and API calls
# are those of Phases 1-4. _merge_lease_read sets MERGE_LEASE_HOLDER_ROWS
# (live rows `pr head expires acquired updated`, head `-` when unknown,
# primary first), MERGE_LEASE_HOLDER_ALL_ROWS (expired rows too),
# MERGE_LEASE_HOLDER_PATHS (`pr<TAB>path` lines) and MERGE_LEASE_HOLDER_COUNT;
# the scalar MERGE_LEASE_* fields describe the primary (an expired primary
# is replaced in memory by the oldest-acquired live holder) and
# MERGE_LEASE_PATHS is the union. MERGE_LEASE_BLOCKER stays one PR number.
# The `MERGE_LEASE` line gains a trailing ` holders=<n>` only with 2+ live
# holders. API: none extra at 1; above 1 an update-mode acquisition reads the
# files of each queue candidate ahead of the caller (at most 10).
# ---------------------------------------------------------------------------

_merge_lease_enabled()
{
	case "$(printf '%s' "${MERGE_LEASE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) return 0 ;;
	esac
	return 1
}

_merge_lease_ttl_seconds()
{
	local lease_ttl_minutes="${MERGE_LEASE_TTL_MINUTES:-240}"
	[[ "${lease_ttl_minutes}" =~ ^[0-9]+$ ]] || lease_ttl_minutes=240
	[ "${lease_ttl_minutes}" -ge 30 ] || lease_ttl_minutes=30
	[ "${lease_ttl_minutes}" -le 1440 ] || lease_ttl_minutes=1440
	echo $((lease_ttl_minutes * 60))
}

# Longest total hold from first acquisition (MERGE_LEASE_MAX_HOLD_MINUTES,
# default 720, never below the TTL), after which the holder stops renewing.
_merge_lease_max_hold_seconds()
{
	local lease_hold_minutes="${MERGE_LEASE_MAX_HOLD_MINUTES:-720}" lease_hold_ttl
	[[ "${lease_hold_minutes}" =~ ^[0-9]+$ ]] || lease_hold_minutes=720
	lease_hold_ttl="$(_merge_lease_ttl_seconds)"
	[ $((lease_hold_minutes * 60)) -ge "${lease_hold_ttl}" ] || lease_hold_minutes=$((lease_hold_ttl / 60))
	echo $((lease_hold_minutes * 60))
}

# Phase 2 kill switch: MERGE_LEASE_REVOKE_ENABLED (default true; true/1/yes/on).
_merge_lease_revoke_enabled()
{
	case "$(printf '%s' "${MERGE_LEASE_REVOKE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) return 0 ;;
	esac
	return 1
}

# Idle window in seconds (MERGE_LEASE_IDLE_MINUTES, default 60, clamped to
# 0-1440; anything that is not a number uses 60). 0 turns the idle test off.
_merge_lease_idle_seconds()
{
	local lease_idle_minutes="${MERGE_LEASE_IDLE_MINUTES:-60}"
	[[ "${lease_idle_minutes}" =~ ^[0-9]+$ ]] || lease_idle_minutes=60
	if [ "${#lease_idle_minutes}" -gt 4 ]; then
		lease_idle_minutes=1440
	fi
	lease_idle_minutes=$((10#${lease_idle_minutes}))
	[ "${lease_idle_minutes}" -le 1440 ] || lease_idle_minutes=1440
	echo $((lease_idle_minutes * 60))
}

# Phase 5 (Refs #9781): holders allowed per base. MERGE_LEASE_MAX_HOLDERS
# (default 1, range 1-5). A value that is not a whole number of at most three
# digits uses 1 and prints one ::warning:: to stderr (the value shown is cut
# to [0-9A-Za-z_-] and 32 characters); 0 gives 1, above 5 gives 5.
_merge_lease_max_holders()
{
	local lease_mh_raw="${MERGE_LEASE_MAX_HOLDERS:-1}" lease_mh_shown
	lease_mh_raw="${lease_mh_raw#"${lease_mh_raw%%[![:space:]]*}"}"
	lease_mh_raw="${lease_mh_raw%"${lease_mh_raw##*[![:space:]]}"}"
	[ -n "${lease_mh_raw}" ] || lease_mh_raw=1
	if ! [[ "${lease_mh_raw}" =~ ^[0-9]{1,3}$ ]]; then
		lease_mh_shown="$(printf '%s' "${lease_mh_raw}" | tr -cd '0-9A-Za-z_-' | cut -c1-32)"
		echo "::warning::MERGE_LEASE_MAX_HOLDERS=${lease_mh_shown} is not an integer; using 1" >&2
		echo 1
		return 0
	fi
	lease_mh_raw=$((10#${lease_mh_raw}))
	[ "${lease_mh_raw}" -ge 1 ] || lease_mh_raw=1
	[ "${lease_mh_raw}" -le 5 ] || lease_mh_raw=5
	echo "${lease_mh_raw}"
}

# _merge_lease_skips_normalize <skips> [<drop pr>]: the skip list (newline-
# separated `pr:head`) with malformed entries and <drop pr>'s entries removed,
# duplicates collapsed to their last position, and only the newest 50 kept.
_merge_lease_skips_normalize()
{
	local lease_skip_drop="${2:-}"
	{ printf '%s\n' "${1:-}" | grep -E '^[1-9][0-9]*:[0-9a-f]{40}$' || true; } \
		| awk -F: -v drop="${lease_skip_drop}" '$1 != drop { line[NR] = $0; last[$0] = NR }
			END { for (i = 1; i <= NR; i++) if ((i in line) && last[line[i]] == i) print line[i] }' \
		| tail -n 50
	return 0
}

# Ref name without the leading "refs/" (the git/ref API path form).
_merge_lease_ref_name()
{
	local lease_prefix="${MERGE_LEASE_REF_PREFIX:-refs/merge-lease/}"
	case "${lease_prefix}" in
		refs/*/) ;;
		*) lease_prefix="refs/merge-lease/" ;;
	esac
	printf '%s%s' "${lease_prefix#refs/}" "$1"
}

_merge_lease_valid_base()
{
	[[ "${1:-}" =~ ^[A-Za-z0-9._/-]+$ ]] && [[ "$1" != *..* ]] && [[ "$1" != /* ]] && [[ "$1" != */ ]]
}

# _merge_lease_read <base>: 0 found (MERGE_LEASE_* set), 2 no lease ref yet,
# 1 read failed. MERGE_LEASE_SHA is the record commit the next write must
# descend from.
_merge_lease_read()
{
	local lease_base="$1" lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local lease_ref lease_refs_json lease_commit_json lease_message
	MERGE_LEASE_SHA=""
	MERGE_LEASE_TREE=""
	MERGE_LEASE_STATE=""
	MERGE_LEASE_PR=""
	MERGE_LEASE_HEAD=""
	MERGE_LEASE_EXPIRES=0
	MERGE_LEASE_ACQUIRED=0
	MERGE_LEASE_PATHS=""
	MERGE_LEASE_UPDATED=0
	MERGE_LEASE_SKIPS=""
	MERGE_LEASE_HOLDER_ROWS=""
	MERGE_LEASE_HOLDER_ALL_ROWS=""
	MERGE_LEASE_HOLDER_PATHS=""
	MERGE_LEASE_HOLDER_COUNT=0
	lease_ref="$(_merge_lease_ref_name "${lease_base}")"
	lease_refs_json="$(gh_retry _safe_gh_jq "repos/${lease_repo}/git/matching-refs/${lease_ref}" 2>/dev/null)" || return 1
	jq -e 'type == "array"' >/dev/null 2>&1 <<< "${lease_refs_json}" || return 1
	MERGE_LEASE_SHA="$(jq -r --arg r "refs/${lease_ref}" '[ .[] | select(type == "object" and .ref == $r) | .object.sha // empty ][0] // ""' <<< "${lease_refs_json}" 2>/dev/null)"
	if [ -z "${MERGE_LEASE_SHA}" ]; then
		return 2
	fi
	[[ "${MERGE_LEASE_SHA}" =~ ^[0-9a-f]{40}$ ]] || { MERGE_LEASE_SHA=""; return 1; }
	lease_commit_json="$(gh_retry _safe_gh_jq "repos/${lease_repo}/git/commits/${MERGE_LEASE_SHA}" 2>/dev/null)" || return 1
	MERGE_LEASE_TREE="$(jq -r '.tree.sha // ""' <<< "${lease_commit_json}" 2>/dev/null)"
	lease_message="$(jq -r '.message // ""' <<< "${lease_commit_json}" 2>/dev/null)"
	[[ "${MERGE_LEASE_TREE}" =~ ^[0-9a-f]{40}$ ]] || return 1
	[ "$(printf '%s\n' "${lease_message}" | head -n1)" = "merge-lease v1" ] || return 1
	MERGE_LEASE_STATE="$(printf '%s\n' "${lease_message}" | sed -n 's/^state=//p' | head -n1)"
	MERGE_LEASE_PR="$(printf '%s\n' "${lease_message}" | sed -n 's/^pr=//p' | head -n1)"
	MERGE_LEASE_HEAD="$(printf '%s\n' "${lease_message}" | sed -n 's/^head=//p' | head -n1)"
	MERGE_LEASE_EXPIRES="$(printf '%s\n' "${lease_message}" | sed -n 's/^expires=//p' | head -n1)"
	MERGE_LEASE_ACQUIRED="$(printf '%s\n' "${lease_message}" | sed -n 's/^acquired=//p' | head -n1)"
	MERGE_LEASE_PATHS="$(printf '%s\n' "${lease_message}" | sed -n '/^paths:$/,$p' | tail -n +2)"
	# Phase 2 fields (optional; a record without them parses as before).
	MERGE_LEASE_UPDATED="$(printf '%s\n' "${lease_message}" | sed -n 's/^updated=//p' | head -n1)"
	MERGE_LEASE_SKIPS="$(_merge_lease_skips_normalize "$(printf '%s\n' "${lease_message}" | sed -n '/^paths:$/q; s/^skip=//p')")"
	[[ "${MERGE_LEASE_UPDATED}" =~ ^[0-9]+$ ]] || MERGE_LEASE_UPDATED=0
	[[ "${MERGE_LEASE_EXPIRES}" =~ ^[0-9]+$ ]] || MERGE_LEASE_EXPIRES=0
	[[ "${MERGE_LEASE_ACQUIRED}" =~ ^[0-9]+$ ]] || MERGE_LEASE_ACQUIRED=0
	[[ "${MERGE_LEASE_PR}" =~ ^[1-9][0-9]*$ ]] || MERGE_LEASE_PR=""
	case "${MERGE_LEASE_STATE}" in
		held|released) ;;
		*) MERGE_LEASE_STATE="released" ;;
	esac
	[ -n "${MERGE_LEASE_PR}" ] || MERGE_LEASE_STATE="released"
	# Phase 5 (Refs #9781): the holder list (see the header).
	_merge_lease_parse_holders "${lease_message}"
	return 0
}

# _merge_lease_parse_holders <record message> (Phase 5, Refs #9781): sets
# MERGE_LEASE_HOLDER_ROWS/ALL_ROWS/PATHS/COUNT from the record
# _merge_lease_read just parsed. Optional `holder=` / `holder_path=` lines
# before `paths:` are matched strictly (malformed lines are dropped; the first
# line for a PR wins). Without them a held record is its one holder, with
# the union `paths:` as its paths. With them the primary's scalars come from
# its own `holder=` line, so another holder's renewal never looks like the
# primary's activity (Phase 2 idle test); an expired primary is replaced in
# memory by the oldest-acquired live holder (then the lowest PR number).
_merge_lease_parse_holders()
{
	local hold_msg="$1" hold_line hold_now hold_all="" hold_paths="" hold_seen="," hold_pr hold_head hold_live="" hold_rest
	local hold_re='^holder=([1-9][0-9]{0,9}) head=([0-9a-f]{40})? expires=([0-9]{1,12}) acquired=([0-9]{1,12}) updated=([0-9]{1,12})$'
	local hold_path_re='^holder_path=([1-9][0-9]{0,9}) (.+)$'
	local hold_primary_row="" hold_f_pr hold_f_head hold_f_exp hold_f_acq hold_f_upd
	MERGE_LEASE_HOLDER_ROWS=""
	MERGE_LEASE_HOLDER_ALL_ROWS=""
	MERGE_LEASE_HOLDER_PATHS=""
	MERGE_LEASE_HOLDER_COUNT=0
	[ "${MERGE_LEASE_STATE:-}" = "held" ] && [ -n "${MERGE_LEASE_PR:-}" ] || return 0
	hold_now="$(date -u +%s 2>/dev/null)"
	[[ "${hold_now}" =~ ^[0-9]+$ ]] || hold_now=0
	while IFS= read -r hold_line; do
		if [[ "${hold_line}" =~ ${hold_re} ]]; then
			hold_pr="${BASH_REMATCH[1]}"
			case "${hold_seen}" in *",${hold_pr},"*) continue ;; esac
			hold_seen+="${hold_pr},"
			hold_all+="${hold_pr} ${BASH_REMATCH[2]:--} $((10#${BASH_REMATCH[3]})) $((10#${BASH_REMATCH[4]})) $((10#${BASH_REMATCH[5]}))"$'\n'
		elif [[ "${hold_line}" =~ ${hold_path_re} ]]; then
			hold_paths+="${BASH_REMATCH[1]}"$'\t'"${BASH_REMATCH[2]}"$'\n'
		fi
	done < <(printf '%s\n' "${hold_msg}" | sed -n '/^paths:$/q; /^holder/p')
	if [ -z "${hold_all}" ]; then
		# A v1 or single-holder record: the record itself is the one holder.
		hold_head="${MERGE_LEASE_HEAD:-}"
		[[ "${hold_head}" =~ ^[0-9a-f]{40}$ ]] || hold_head="-"
		MERGE_LEASE_HOLDER_ALL_ROWS="${MERGE_LEASE_PR} ${hold_head} ${MERGE_LEASE_EXPIRES:-0} ${MERGE_LEASE_ACQUIRED:-0} ${MERGE_LEASE_UPDATED:-0}"
		if [ "${MERGE_LEASE_EXPIRES:-0}" -gt "${hold_now}" ]; then
			MERGE_LEASE_HOLDER_ROWS="${MERGE_LEASE_HOLDER_ALL_ROWS}"
			MERGE_LEASE_HOLDER_COUNT=1
		fi
		while IFS= read -r hold_line; do
			[ -n "${hold_line}" ] || continue
			MERGE_LEASE_HOLDER_PATHS+="${MERGE_LEASE_PR}"$'\t'"${hold_line}"$'\n'
		done <<< "${MERGE_LEASE_PATHS}"
		MERGE_LEASE_HOLDER_PATHS="${MERGE_LEASE_HOLDER_PATHS%$'\n'}"
		return 0
	fi
	hold_all="${hold_all%$'\n'}"
	MERGE_LEASE_HOLDER_PATHS="${hold_paths%$'\n'}"
	hold_primary_row="$(printf '%s\n' "${hold_all}" | awk -v p="${MERGE_LEASE_PR}" '$1 == p' | head -n1)"
	if [ -n "${hold_primary_row}" ]; then
		hold_rest="$(printf '%s\n' "${hold_all}" | awk -v p="${MERGE_LEASE_PR}" 'NF && $1 != p')"
		hold_all="$(printf '%s\n%s\n' "${hold_primary_row}" "${hold_rest}" | sed '/^$/d')"
	fi
	MERGE_LEASE_HOLDER_ALL_ROWS="${hold_all}"
	hold_live="$(printf '%s\n' "${hold_all}" | awk -v now="${hold_now}" 'NF && ($3 + 0) > (now + 0)')"
	if [ -n "${hold_primary_row}" ] && printf '%s\n' "${hold_live}" | awk -v p="${MERGE_LEASE_PR}" '$1 == p { found = 1 } END { exit found ? 0 : 1 }'; then
		:
	elif [ -n "${hold_live}" ]; then
		hold_primary_row="$(printf '%s\n' "${hold_live}" | sort -k4,4n -k1,1n | head -n1)"
		hold_f_pr="${hold_primary_row%% *}"
		hold_rest="$(printf '%s\n' "${hold_live}" | awk -v p="${hold_f_pr}" 'NF && $1 != p')"
		hold_live="$(printf '%s\n%s\n' "${hold_primary_row}" "${hold_rest}" | sed '/^$/d')"
	fi
	if [ -n "${hold_primary_row}" ]; then
		read -r hold_f_pr hold_f_head hold_f_exp hold_f_acq hold_f_upd <<< "${hold_primary_row}"
		[ "${hold_f_head}" != "-" ] || hold_f_head=""
		MERGE_LEASE_PR="${hold_f_pr}"
		MERGE_LEASE_HEAD="${hold_f_head}"
		MERGE_LEASE_EXPIRES="${hold_f_exp}"
		MERGE_LEASE_ACQUIRED="${hold_f_acq}"
		MERGE_LEASE_UPDATED="${hold_f_upd}"
	fi
	MERGE_LEASE_HOLDER_ROWS="${hold_live}"
	[ -z "${hold_live}" ] || MERGE_LEASE_HOLDER_COUNT="$(printf '%s\n' "${hold_live}" | grep -c .)"
	return 0
}

# Phase 5 helpers over the globals _merge_lease_read sets (no API call).
# _merge_lease_holder_paths <pr>: that holder's paths, one per line; the
# record's union `paths:` when it has no `holder_path=` lines, `*` when that
# is empty too (the stricter reading).
_merge_lease_holder_paths()
{
	local hp_pr="$1" hp_line hp_out=""
	while IFS= read -r hp_line; do
		[[ "${hp_line}" == *$'\t'* ]] || continue
		[ "${hp_line%%$'\t'*}" = "${hp_pr}" ] || continue
		hp_out+="${hp_line#*$'\t'}"$'\n'
	done <<< "${MERGE_LEASE_HOLDER_PATHS:-}"
	hp_out="${hp_out%$'\n'}"
	[ -n "${hp_out}" ] || hp_out="${MERGE_LEASE_PATHS:-}"
	[ -n "${hp_out}" ] || hp_out="*"
	printf '%s\n' "${hp_out}"
}

# _merge_lease_holder_paths_set <pr> <paths>: replaces <pr>'s lines in
# MERGE_LEASE_HOLDER_PATHS (the writer reads them from there).
_merge_lease_holder_paths_set()
{
	local hps_pr="$1" hps_paths="${2:-}" hps_line hps_out=""
	while IFS= read -r hps_line; do
		[ -n "${hps_line}" ] || continue
		[ "${hps_line%%$'\t'*}" != "${hps_pr}" ] || continue
		hps_out+="${hps_line}"$'\n'
	done <<< "${MERGE_LEASE_HOLDER_PATHS:-}"
	[ -n "${hps_paths}" ] || hps_paths="*"
	while IFS= read -r hps_line; do
		[ -n "${hps_line}" ] || continue
		hps_out+="${hps_pr}"$'\t'"${hps_line}"$'\n'
	done <<< "${hps_paths}"
	MERGE_LEASE_HOLDER_PATHS="${hps_out%$'\n'}"
}

# _merge_lease_holder_row <pr> [<rows>]: that PR's row (default: the live rows).
_merge_lease_holder_row()
{
	printf '%s\n' "${2-${MERGE_LEASE_HOLDER_ROWS:-}}" | awk -v p="$1" 'NF && $1 == p { print; exit }'
}

# _merge_lease_rows_without <pr> [<rows>]: the rows without <pr>. When <pr>
# is the primary (first row) the rest is re-ordered oldest acquired first,
# then lowest PR number, so that holder becomes the primary (plan step 5.1).
_merge_lease_rows_without()
{
	local rw_pr="$1" rw_rows rw_first rw_rest
	rw_rows="$(printf '%s\n' "${2-${MERGE_LEASE_HOLDER_ROWS:-}}" | sed '/^$/d')"
	rw_first="$(printf '%s\n' "${rw_rows}" | head -n1 | awk '{ print $1 }')"
	rw_rest="$(printf '%s\n' "${rw_rows}" | awk -v p="${rw_pr}" 'NF && $1 != p')"
	if [ -n "${rw_rest}" ] && [ "${rw_first}" = "${rw_pr}" ]; then
		rw_rest="$(printf '%s\n' "${rw_rest}" | sort -k4,4n -k1,1n)"
	fi
	[ -z "${rw_rest}" ] || printf '%s\n' "${rw_rest}"
}

# True when the record just read is a live lease (held and not expired).
# Phase 5: or any holder in it is still live.
_merge_lease_is_held()
{
	[ "${MERGE_LEASE_STATE:-}" = "held" ] || return 1
	[ "${MERGE_LEASE_EXPIRES:-0}" -gt "$(date -u +%s)" ] && return 0
	[ "${MERGE_LEASE_HOLDER_COUNT:-0}" -gt 0 ] 2>/dev/null
}

# _merge_lease_write <base> <state> <pr> <head> <paths> [<acquired epoch>]:
# writes a record (acquired defaults to now; a renewal passes the original) that
# descends from the one _merge_lease_read saw (MERGE_LEASE_SHA; empty = create
# the ref). 0 written, 3 lost the race (re-read shows another record), 1 error.
# With MERGE_LEASE_REVOKE_ENABLED on, MERGE_LEASE_SKIPS is written as `skip=`
# lines (normalized; a held write drops the new holder's own entries).
_merge_lease_write()
{
	local lease_base="$1" lease_state="$2" lease_pr="$3" lease_head="$4" lease_paths="$5" lease_acquired="${6:-}"
	local lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local lease_now lease_expires lease_msg lease_max_paths
	lease_now="$(date -u +%s)"
	[[ "${lease_acquired}" =~ ^[1-9][0-9]*$ ]] || lease_acquired="${lease_now}"
	lease_expires=0
	[ "${lease_state}" = "held" ] && lease_expires=$((lease_now + $(_merge_lease_ttl_seconds)))
	lease_max_paths="${MERGE_LEASE_MAX_PATHS:-200}"
	[[ "${lease_max_paths}" =~ ^[1-9][0-9]*$ ]] || lease_max_paths=200
	lease_paths="$(printf '%s\n' "${lease_paths}" | sed '/^$/d')"
	if [ -z "${lease_paths}" ] || [ "$(printf '%s\n' "${lease_paths}" | wc -l | tr -d ' ')" -gt "${lease_max_paths}" ]; then
		lease_paths="*"
	fi
	[ "${lease_state}" = "released" ] && lease_paths=""
	local lease_skip_lines="" lease_skip_block="" lease_skip_drop=""
	[ "${lease_state}" = "held" ] && lease_skip_drop="${lease_pr}"
	lease_skip_lines="$(_merge_lease_skip_block "${lease_skip_drop}")"
	[ -z "${lease_skip_lines}" ] || lease_skip_block="${lease_skip_lines}"$'\n'
	lease_msg="merge-lease v1
state=${lease_state}
base=${lease_base}
pr=${lease_pr}
head=${lease_head}
expires=${lease_expires}
acquired=${lease_acquired}
updated=${lease_now}
${lease_skip_block}paths:
${lease_paths}"
	_merge_lease_cas_commit "${lease_base}" "${lease_msg}"
}

# _merge_lease_skip_block <drop_pr>...: the `skip=` lines of the next write
# (no trailing newline; empty with MERGE_LEASE_REVOKE_ENABLED off), with each
# <drop_pr>'s own entries removed (a holder is never on the skip list).
_merge_lease_skip_block()
{
	local skip_lines="${MERGE_LEASE_SKIPS:-}" skip_drop
	_merge_lease_revoke_enabled || return 0
	for skip_drop in "$@"; do
		skip_lines="$(_merge_lease_skips_normalize "${skip_lines}" "${skip_drop}")"
	done
	[ -z "${skip_lines}" ] || printf '%s\n' "${skip_lines}" | sed 's/^/skip=/'
}

# _merge_lease_cas_commit <base> <message>: the compare-and-swap shared by
# _merge_lease_write and _merge_lease_write_holders (Phase 5 extraction, body
# unchanged). Commits <message> as a child of MERGE_LEASE_SHA (empty: create
# the ref) and moves the ref with a fast-forward-only PATCH. 0 written, 3 lost
# the race (re-read shows another record), 1 error.
_merge_lease_cas_commit()
{
	local lease_base="$1" lease_msg="$2"
	local lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local lease_ref lease_tree lease_parent lease_new_sha
	local -a lease_commit_args=()
	lease_ref="$(_merge_lease_ref_name "${lease_base}")"
	lease_parent="${MERGE_LEASE_SHA:-}"
	lease_tree="${MERGE_LEASE_TREE:-}"
	if [ -z "${lease_tree}" ]; then
		# First use only: any existing tree will do; take the base tip's.
		lease_tree="$(gh_retry _safe_gh_jq "repos/${lease_repo}/commits/${lease_base}" --jq '.commit.tree.sha // ""' 2>/dev/null | tail -n1)"
		[[ "${lease_tree}" =~ ^[0-9a-f]{40}$ ]] || return 1
	fi
	lease_commit_args=(-X POST "repos/${lease_repo}/git/commits" -f "message=${lease_msg}" -f "tree=${lease_tree}")
	[ -n "${lease_parent}" ] && lease_commit_args+=(-f "parents[]=${lease_parent}")
	lease_new_sha="$(GH_RETRY_IDEMPOTENT=true gh_retry _safe_gh_jq "${lease_commit_args[@]}" --jq '.sha // ""' 2>/dev/null | tail -n1)"
	[[ "${lease_new_sha}" =~ ^[0-9a-f]{40}$ ]] || return 1
	if [ -n "${lease_parent}" ]; then
		if gh_retry _safe_gh_jq -X PATCH "repos/${lease_repo}/git/refs/${lease_ref}" -f "sha=${lease_new_sha}" -F force=false >/dev/null 2>&1; then
			MERGE_LEASE_SHA="${lease_new_sha}"
			return 0
		fi
	elif gh_retry _safe_gh_jq -X POST "repos/${lease_repo}/git/refs" -f "ref=refs/${lease_ref}" -f "sha=${lease_new_sha}" >/dev/null 2>&1; then
		MERGE_LEASE_SHA="${lease_new_sha}"
		MERGE_LEASE_TREE="${lease_tree}"
		return 0
	fi
	# Refused: a lost compare-and-swap, or an error. Re-read to tell them apart.
	local lease_reread_rc=0
	_merge_lease_read "${lease_base}" || lease_reread_rc=$?
	if [ "${lease_reread_rc}" -eq 0 ] && [ "${MERGE_LEASE_SHA}" != "${lease_parent}" ]; then
		return 3
	fi
	return 1
}

# _merge_lease_paths_record_form <paths> <max>: one holder's paths as written
# to the record: `*` when empty, over <max>, or when any path holds CR or
# starts with `*` (fails closed; a path can never forge a record line).
_merge_lease_paths_record_form()
{
	local rf_paths rf_max="${2:-200}"
	rf_paths="$(printf '%s\n' "${1:-}" | sed '/^$/d')"
	if [ -z "${rf_paths}" ] || [ "$(printf '%s\n' "${rf_paths}" | wc -l | tr -d ' ')" -gt "${rf_max}" ] \
		|| printf '%s\n' "${rf_paths}" | grep -q -e $'\r' -e '^\*'; then
		echo "*"
		return 0
	fi
	printf '%s\n' "${rf_paths}"
}

# _merge_lease_write_holders <base> <rows> (Phase 5, Refs #9781): writes a
# held record for the ordered holder list <rows> (`pr head expires acquired
# updated`, head `-` when unknown; the first row is the primary). Each
# holder's paths come from _merge_lease_holder_paths. One row writes the v1
# shape with that row's own times, byte-identical to a single-holder record,
# so releasing the other holder never extends this one's lease. Two or more
# rows write pr=/head=/acquired=/updated= of the primary, expires= the latest
# expiry, the skip lines (every holder's own entries dropped), one `holder=`
# line per row, the `holder_path=` lines, then `paths:` with the union (`*`
# when a holder is `*` or the union exceeds MERGE_LEASE_MAX_PATHS). Same
# compare-and-swap and return codes as _merge_lease_write (1 also for
# malformed rows).
_merge_lease_write_holders()
{
	local wh_base="$1" wh_rows wh_pr wh_head wh_exp wh_acq wh_upd wh_max_exp=0 wh_n=0 wh_max_paths wh_line
	local wh_paths wh_union="" wh_union_star=0 wh_holder_lines="" wh_path_lines="" wh_skip_lines wh_skip_block="" wh_msg
	local wh_p_pr="" wh_p_head="" wh_p_acq="" wh_p_upd="" wh_p_paths=""
	local -a wh_drop=() wh_row_arr=()
	wh_rows="$(printf '%s\n' "${2:-}" | sed '/^$/d')"
	[ -n "${wh_rows}" ] || return 1
	wh_max_paths="${MERGE_LEASE_MAX_PATHS:-200}"
	[[ "${wh_max_paths}" =~ ^[1-9][0-9]*$ ]] || wh_max_paths=200
	mapfile -t wh_row_arr <<< "${wh_rows}"
	for wh_line in "${wh_row_arr[@]}"; do
		read -r wh_pr wh_head wh_exp wh_acq wh_upd <<< "${wh_line}"
		[[ "${wh_pr}" =~ ^[1-9][0-9]*$ && "${wh_exp}" =~ ^[0-9]+$ && "${wh_acq}" =~ ^[0-9]+$ && "${wh_upd}" =~ ^[0-9]+$ ]] || return 1
		[[ "${wh_head}" =~ ^[0-9a-f]{40}$ ]] || wh_head=""
		wh_n=$((wh_n + 1))
		wh_drop+=("${wh_pr}")
		[ "${wh_exp}" -le "${wh_max_exp}" ] || wh_max_exp="${wh_exp}"
		wh_paths="$(_merge_lease_paths_record_form "$(_merge_lease_holder_paths "${wh_pr}")" "${wh_max_paths}")"
		if [ "${wh_n}" -eq 1 ]; then
			wh_p_pr="${wh_pr}"
			wh_p_head="${wh_head}"
			wh_p_acq="${wh_acq}"
			wh_p_upd="${wh_upd}"
			wh_p_paths="${wh_paths}"
		fi
		wh_holder_lines+="holder=${wh_pr} head=${wh_head} expires=${wh_exp} acquired=${wh_acq} updated=${wh_upd}"$'\n'
		while IFS= read -r wh_line; do
			[ -n "${wh_line}" ] || continue
			wh_path_lines+="holder_path=${wh_pr} ${wh_line}"$'\n'
		done <<< "${wh_paths}"
		if [ "${wh_paths}" = "*" ]; then
			wh_union_star=1
		else
			wh_union+="${wh_paths}"$'\n'
		fi
	done
	wh_skip_lines="$(_merge_lease_skip_block "${wh_drop[@]}")"
	[ -z "${wh_skip_lines}" ] || wh_skip_block="${wh_skip_lines}"$'\n'
	if [ "${wh_n}" -eq 1 ]; then
		wh_msg="merge-lease v1
state=held
base=${wh_base}
pr=${wh_p_pr}
head=${wh_p_head}
expires=${wh_max_exp}
acquired=${wh_p_acq}
updated=${wh_p_upd}
${wh_skip_block}paths:
${wh_p_paths}"
	else
		wh_union="$(printf '%s' "${wh_union}" | sed '/^$/d' | awk '!seen[$0]++')"
		if [ "${wh_union_star}" = 1 ] || [ -z "${wh_union}" ] \
			|| [ "$(printf '%s\n' "${wh_union}" | wc -l | tr -d ' ')" -gt "${wh_max_paths}" ]; then
			wh_union="*"
		fi
		wh_msg="merge-lease v1
state=held
base=${wh_base}
pr=${wh_p_pr}
head=${wh_p_head}
expires=${wh_max_exp}
acquired=${wh_p_acq}
updated=${wh_p_upd}
${wh_skip_block}${wh_holder_lines}${wh_path_lines}paths:
${wh_union}"
	fi
	_merge_lease_cas_commit "${wh_base}" "${wh_msg}"
}

# Paths this PR changes, newline-separated: the freshness check's cache when
# it is for this PR, else one paginated files read. Returns 1 on failure.
_merge_lease_pr_paths()
{
	local lease_pr="$1" lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" lease_files_json
	if [ "${PR_BASE_FRESHNESS_PR_PATHS_FOR:-}" = "${lease_pr}" ] && [ -n "${PR_BASE_FRESHNESS_PR_PATHS:-}" ]; then
		printf '%s\n' "${PR_BASE_FRESHNESS_PR_PATHS}"
		return 0
	fi
	lease_files_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${lease_repo}/pulls/${lease_pr}/files?per_page=100" 2>/dev/null)" || return 1
	jq -e 'type == "array"' >/dev/null 2>&1 <<< "${lease_files_json}" || return 1
	# Phase 4 (Refs #9781): with the soft class on, a path holding CR/LF
	# yields `*` (fail closed) instead of newline-split fragments that could
	# each look soft; so does a non-string name, which would otherwise make
	# jq fail and the gate take its fail-open pr_files_failed path. With the
	# class off the list is as before.
	local lease_soft_on=""
	[ -z "$(_merge_lease_soft_entries 2>/dev/null)" ] || lease_soft_on="1"
	jq -r --arg soft_on "${lease_soft_on}" '[ .[]? | if type == "array" then .[] else . end | select(type == "object")
		| (.filename // empty), (.previous_filename // empty) ] | unique
		| if $soft_on != "" and any(.[]; (type != "string") or test("[\n\r]")) then "*" else .[] end' <<< "${lease_files_json}" 2>/dev/null
}

# 0 when the two newline lists share a path (`*` shares every path).
# Optional <own_head> <holder_head> (Phase 4, Refs #9781): when every shared
# path is in the soft class (MERGE_LEASE_SOFT_PATHS) and both heads are full
# SHAs, a merge-tree probe of the two heads decides; only a clean merge
# returns 1, and sets MERGE_LEASE_SOFT_SHARED to the shared soft paths
# (comma-separated). Any shared hard path, or a probe that cannot decide
# (including a clean merge that combines content in a non-soft path, see
# _pr_soft_merge_tree_probe), returns 0. With two arguments, or the class off, the check is as before.
_merge_lease_paths_intersect()
{
	local lease_left="$1" lease_right="$2"
	local lease_own_head="${3:-}" lease_holder_head="${4:-}" lease_shared="" lease_soft_entries=""
	[ "${lease_left}" = "*" ] && return 0
	[ "${lease_right}" = "*" ] && return 0
	[ -n "${lease_left}" ] && [ -n "${lease_right}" ] || return 1
	if [ "$#" -ge 4 ]; then
		lease_soft_entries="$(_merge_lease_soft_entries 2>/dev/null)"
		# Same once-per-process warning as _merge_lease_split_soft.
		if [ -z "${_MERGE_LEASE_SOFT_WARNED:-}" ]; then
			_MERGE_LEASE_SOFT_WARNED=1
			_merge_lease_soft_entries >/dev/null
		fi
	fi
	if [ -z "${lease_soft_entries}" ]; then
		grep -Fxq -f <(printf '%s\n' "${lease_left}" | sed '/^$/d') <(printf '%s\n' "${lease_right}" | sed '/^$/d')
		return
	fi
	lease_shared="$(grep -Fx -f <(printf '%s\n' "${lease_left}" | sed '/^$/d') <(printf '%s\n' "${lease_right}" | sed '/^$/d') | sort -u)"
	[ -n "${lease_shared}" ] || return 1
	_merge_lease_split_soft "${lease_shared}"
	[ -z "${_MERGE_LEASE_HARD_LIST}" ] || return 0
	[[ "${lease_own_head}" =~ ^[0-9a-f]{40}$ && "${lease_holder_head}" =~ ^[0-9a-f]{40}$ ]] || return 0
	_pr_soft_merge_tree_probe "${lease_own_head}" "${lease_holder_head}"
	if [ "${_PR_PROBE_RESULT}" = "none" ]; then
		MERGE_LEASE_SOFT_SHARED="$(printf '%s' "${_MERGE_LEASE_SOFT_LIST}" | paste -sd, -)"
		return 1
	fi
	return 0
}

# Order key for queue position: "0" for a PR carrying a priority label, "1"
# otherwise, then the zero-padded PR number. Lower sorts first.
# Optional <head_ref> (Phase 3, Refs #9781): a ref starting with an entry of
# MERGE_LEASE_PRIORITY_HEAD_PREFIXES (default auto/forward-merge-stable-;
# comma-separated literal prefixes, no globs; empty, none or off turns it
# off) also gives "0". The caller passes the ref only for a same-repository
# head. Without it, or with an empty ref, the key is as before.
_merge_lease_order_key()
{
	local lease_pr="$1" lease_labels="$2" lease_prio_raw lease_label lease_prio=1
	local lease_head_ref="${3:-}" lease_head_prefixes_raw lease_head_prefix
	lease_prio_raw="${MERGE_TRAIN_PRIORITY_LABELS-ai:workflow-heal,ai:security}"
	case "$(printf '%s' "${lease_prio_raw}" | tr '[:upper:]' '[:lower:]')" in
		""|none|off) lease_prio_raw="" ;;
	esac
	while IFS= read -r lease_label; do
		[ -n "${lease_label}" ] || continue
		if printf ',%s,' "${lease_prio_raw}" | grep -Fq ",${lease_label},"; then
			lease_prio=0
			break
		fi
	done < <(printf '%s\n' "${lease_labels}" | tr ',' '\n')
	if [ "${lease_prio}" = 1 ] && [ -n "${lease_head_ref}" ]; then
		lease_head_prefixes_raw="${MERGE_LEASE_PRIORITY_HEAD_PREFIXES-auto/forward-merge-stable-}"
		case "$(printf '%s' "${lease_head_prefixes_raw}" | tr '[:upper:]' '[:lower:]')" in
			""|none|off) lease_head_prefixes_raw="" ;;
		esac
		while IFS= read -r lease_head_prefix; do
			lease_head_prefix="${lease_head_prefix#"${lease_head_prefix%%[![:space:]]*}"}"
			lease_head_prefix="${lease_head_prefix%"${lease_head_prefix##*[![:space:]]}"}"
			[ -n "${lease_head_prefix}" ] || continue
			# Quoted pattern: the prefix is literal, `*`, `?` and `[` never glob.
			if [[ "${lease_head_ref}" == "${lease_head_prefix}"* ]]; then
				lease_prio=0
				break
			fi
		done < <(printf '%s\n' "${lease_head_prefixes_raw}" | tr ',' '\n')
	fi
	printf '%s%012d' "${lease_prio}" "${lease_pr}"
}

# First waiter for <base> from a `pulls` listing (array of PR objects, the
# REST shape), or the given candidate PR when it orders before every waiter.
# Prints a PR number; empty when nobody waits and no candidate was given.
# Optional (Phase 2): <skips> (newline `pr:head`) passes over a row whose
# number and current head match an entry; <exclude_csv> passes over those
# numbers. Both apply to the candidate too. The head is `.head.sha` (REST
# listing) or `.head_sha` (merge-train listing).
# Phase 3 (Refs #9781): the head ref, `.head.ref` (REST listing, when
# `.head.repo.full_name` is this repository) or `.head` (merge-train listing,
# when `.head_repo` is this repository), feeds _merge_lease_order_key's
# forward-merge priority. A fork head or an unknown repository gives no ref,
# so no priority; it never blocks a PR.
# Phase 1 queue snapshot (Refs #9781): also sets MERGE_LEASE_QUEUE_LEN (the
# rows considered: waiters, plus the candidate, after the skip and exclude
# filters) and MERGE_LEASE_QUEUE_PRS (their numbers in queue order, comma-
# separated; empty when none). Callers that read the line through `$(...)`
# lose them; _merge_lease_queue_order prints the list instead.
_merge_lease_queue_first()
{
	local lease_pulls_json="$1" lease_candidate="${2:-}" lease_label="${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}"
	local lease_skips="${3:-}" lease_exclude="${4:-}" lease_row_head lease_row_rest lease_row_ref
	local lease_row_repo
	lease_row_repo="$(printf '%s' "${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" | tr '[:upper:]' '[:lower:]')"
	local lease_best="" lease_best_key="" lease_row lease_num lease_labels lease_key lease_queue_keys=""
	MERGE_LEASE_QUEUE_LEN=0
	MERGE_LEASE_QUEUE_PRS=""
	while IFS= read -r lease_row; do
		[ -n "${lease_row}" ] || continue
		lease_num="${lease_row%%$'\t'*}"
		lease_row_rest="${lease_row#*$'\t'}"
		lease_row_head="${lease_row_rest%%$'\t'*}"
		lease_row_rest="${lease_row_rest#*$'\t'}"
		lease_row_ref="${lease_row_rest%%$'\t'*}"
		lease_labels="${lease_row_rest#*$'\t'}"
		[[ "${lease_num}" =~ ^[1-9][0-9]*$ ]] || continue
		if [ "${lease_num}" != "${lease_candidate}" ] && ! printf ',%s,' "${lease_labels}" | grep -Fq ",${lease_label},"; then
			continue
		fi
		if [ -n "${lease_exclude}" ] && printf ',%s,' "${lease_exclude}" | grep -Fq ",${lease_num},"; then
			continue
		fi
		if [ -n "${lease_skips}" ] && [ -n "${lease_row_head}" ] && printf '%s\n' "${lease_skips}" | grep -Fxq -- "${lease_num}:${lease_row_head}"; then
			continue
		fi
		lease_key="$(_merge_lease_order_key "${lease_num}" "${lease_labels}" "${lease_row_ref}")"
		lease_queue_keys+="${lease_key} ${lease_num}"$'\n'
		if [ -z "${lease_best_key}" ] || [[ "${lease_key}" < "${lease_best_key}" ]]; then
			lease_best_key="${lease_key}"
			lease_best="${lease_num}"
		fi
	done < <(jq -r --arg repo "${lease_row_repo}" '[ .[]? | if type == "array" then .[] else . end | select(type == "object" and (.draft // false) == false)
		| "\(.number)\t\(((if (.head | type) == "object" then .head.sha else .head_sha end) // "") | tostring)\t\((if $repo == "" then ""
			elif (.head | type) == "object" then (if ((.head.repo.full_name // "") | tostring | ascii_downcase) == $repo then (.head.ref // "") else "" end)
			elif (.head | type) == "string" then (if ((.head_repo // "") | tostring | ascii_downcase) == $repo then .head else "" end)
			else "" end) | tostring | gsub("[\t\n\r]"; ""))\t\([ (.labels // [])[] | if type == "object" then .name else . end ] | join(","))" ] | .[]' <<< "${lease_pulls_json}" 2>/dev/null)
	if [ -n "${lease_queue_keys}" ]; then
		# The order keys are fixed width (priority digit plus 12 digits), so a
		# plain sort gives queue order; duplicate numbers collapse.
		MERGE_LEASE_QUEUE_PRS="$(printf '%s' "${lease_queue_keys}" | sort -u | awk '!seen[$2]++ { print $2 }' | paste -sd, -)"
		MERGE_LEASE_QUEUE_LEN="$(printf '%s\n' "${MERGE_LEASE_QUEUE_PRS}" | tr ',' '\n' | grep -c .)"
	fi
	printf '%s' "${lease_best}"
}

# _merge_lease_queue_order <pulls> [<skips>] [<exclude_csv>] (Phase 1, Refs
# #9781): the labelled waiters of a `pulls` listing in queue order, comma-
# separated (empty when none), with the same skip and exclude filters as
# _merge_lease_queue_first. No API call; prints only digits and commas.
_merge_lease_queue_order()
{
	_merge_lease_queue_first "${1:-}" "" "${2:-}" "${3:-}" >/dev/null 2>&1 || true
	[[ "${MERGE_LEASE_QUEUE_PRS:-}" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || return 0
	printf '%s' "${MERGE_LEASE_QUEUE_PRS}"
}

# _merge_lease_pr_eligible <pr> <head_sha> <title> <draft> [<base>] [<trusted_login>] [<hold_since_epoch>]
# Phase 2 (Refs #9781): 0 when the PR can still merge or eligibility cannot
# be read (fail-open), 1 when it cannot (MERGE_LEASE_INELIGIBLE_REASON =
# draft|skip_ai|no_head_gate|final_ci_failed). Also sets MERGE_LEASE_CI_ACTIVE
# (true|false|unknown): whether a CI run or required check is still running.
# Checks, cheapest first: draft, a `[skip ai]` title (fixed-string,
# case-insensitive; the title is never logged or evaluated), the newest
# ai-review/head-gate status on the head (counted only when <trusted_login>
# created it; failure/error is ineligible; absent or pending is ineligible
# for a waiter, and for a holder (<hold_since_epoch> given) only once
# MERGE_LEASE_IDLE_MINUTES have passed since it, so a holder whose updated
# head is still being reviewed keeps the lease), then the required checks
# with a 0-minute budget (no final-CI dispatch, no pending marker).
# API: 1 statuses read plus the reads of _pr_wait_for_required_checks; none
# for draft or `[skip ai]`, or without a valid login or head.
_merge_lease_pr_eligible()
{
	local elig_pr="$1" elig_head="$2" elig_title="$3" elig_draft="$4" elig_base="${5:-}" elig_login="${6:-}" elig_since="${7:-}"
	local elig_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" elig_statuses elig_gate elig_idle elig_now elig_rc=0
	MERGE_LEASE_INELIGIBLE_REASON=""
	MERGE_LEASE_CI_ACTIVE="unknown"
	if [ "${elig_draft}" = "true" ]; then
		MERGE_LEASE_INELIGIBLE_REASON="draft"
		return 1
	fi
	if printf '%s' "${elig_title}" | grep -Fqi -- '[skip ai]'; then
		MERGE_LEASE_INELIGIBLE_REASON="skip_ai"
		return 1
	fi
	elig_login="$(printf '%s' "${elig_login}" | tr '[:upper:]' '[:lower:]')"
	[[ "${elig_login}" =~ ^[a-z0-9][a-z0-9-]*(\[bot\])?$ ]] || return 0
	[[ "${elig_head}" =~ ^[0-9a-f]{40}$ ]] || return 0
	[[ "${elig_pr}" =~ ^[1-9][0-9]*$ ]] || return 0
	elig_statuses="$(gh_retry _safe_gh_jq "repos/${elig_repo}/commits/${elig_head}/statuses?per_page=100" 2>/dev/null)" || return 0
	jq -e 'type == "array"' >/dev/null 2>&1 <<< "${elig_statuses}" || return 0
	# Newest first: the latest head-gate status decides.
	elig_gate="$(jq -r '[ .[] | select(type == "object" and .context == "ai-review/head-gate") ][0] | if . == null then "" else "\(.state) \((.creator.login // "") | ascii_downcase)" end' <<< "${elig_statuses}" 2>/dev/null)" || return 0
	case "${elig_gate}" in
		"success ${elig_login}") ;;
		"failure ${elig_login}"|"error ${elig_login}")
			MERGE_LEASE_INELIGIBLE_REASON="no_head_gate"
			return 1
			;;
		*)
			if [ -n "${elig_since}" ]; then
				[[ "${elig_since}" =~ ^[0-9]+$ ]] || return 0
				elig_idle="$(_merge_lease_idle_seconds)"
				# MERGE_LEASE_IDLE_MINUTES=0 turns off only the idle test (plan
				# 2.3): the head-gate grace then uses the 60-minute default.
				[ "${elig_idle}" -gt 0 ] || elig_idle=3600
				elig_now="$(date -u +%s)"
				if [ "$((10#${elig_since}))" -gt 0 ] && [ "${elig_idle}" -gt 0 ] && [ $((elig_now - 10#${elig_since})) -ge "${elig_idle}" ]; then
					MERGE_LEASE_INELIGIBLE_REASON="no_head_gate"
					return 1
				fi
				return 0
			fi
			MERGE_LEASE_INELIGIBLE_REASON="no_head_gate"
			return 1
			;;
	esac
	PR_CHECKS_RUNS_READ=""
	PR_CHECKS_RUNS_PENDING=0
	PR_CHECKS_LAST_PENDING=0
	PR_CHECKS_RUNS_UNRUN=0
	PR_CHECKS_WAIT_OUTCOME=""
	PR_CHECKS_REPOSITORY="${elig_repo}" REVIEW_FINAL_CI_WORKFLOW="" FINAL_CI_MERGE_RESUME_ENABLED=false \
		_pr_wait_for_required_checks "${elig_pr}" "${elig_head}" "${elig_base}" 0 || elig_rc=$?
	# Plan D3 revokes only for a CI run that concluded with failure: runs that
	# were all cancelled or hit startup_failure (reason=ci_run_cancelled) are
	# not a failure, so they stay eligible (fail-open; the idle test still
	# applies to a holder).
	if [ "${elig_rc}" -ne 0 ] && [ "${PR_CHECKS_WAIT_OUTCOME:-}" = "failed" ] \
		&& [ "${PR_CHECKS_LAST_REASON:-}" = "ok" ] && [ "${PR_CHECKS_RUNS_UNRUN:-0}" -gt 0 ]; then
		# The runs were read on this path: report them so the tick's idle
		# test does not read actions/runs again (section 14 budget).
		if [ "${PR_CHECKS_RUNS_READ:-}" = "true" ]; then
			MERGE_LEASE_CI_ACTIVE="false"
			[ "${PR_CHECKS_RUNS_PENDING:-0}" -eq 0 ] || MERGE_LEASE_CI_ACTIVE="true"
		fi
		return 0
	fi
	if [ "${elig_rc}" -ne 0 ] && [ "${PR_CHECKS_WAIT_OUTCOME:-}" = "failed" ]; then
		MERGE_LEASE_INELIGIBLE_REASON="final_ci_failed"
		return 1
	fi
	case "${PR_CHECKS_WAIT_OUTCOME:-}" in
		ok|allow_all|timeout)
			if [ "${PR_CHECKS_RUNS_READ:-}" = "true" ] && [ "${PR_CHECKS_RUNS_PENDING:-0}" -gt 0 ]; then
				MERGE_LEASE_CI_ACTIVE="true"
			elif [ "${PR_CHECKS_WAIT_OUTCOME}" = "timeout" ] && [ "${PR_CHECKS_LAST_PENDING:-0}" -gt 0 ]; then
				MERGE_LEASE_CI_ACTIVE="true"
			elif [ "${PR_CHECKS_RUNS_READ:-}" = "true" ]; then
				MERGE_LEASE_CI_ACTIVE="false"
			fi
			;;
	esac
	return 0
}

# Phase 5 (Refs #9781): ` holders=<n>` is appended only with 2+ live holders;
# otherwise the line is unchanged. holder= is the primary.
_merge_lease_log()
{
	local lease_log_holders=""
	if [[ "${MERGE_LEASE_HOLDER_COUNT:-0}" =~ ^[0-9]+$ ]] && [ "${MERGE_LEASE_HOLDER_COUNT:-0}" -gt 1 ]; then
		lease_log_holders=" holders=${MERGE_LEASE_HOLDER_COUNT}"
	fi
	echo "MERGE_LEASE pr=${1:-?} base=${2:-?} mode=${3:-?} outcome=${4:-?} reason=${5:-?} holder=${MERGE_LEASE_PR:--} blocker=${MERGE_LEASE_BLOCKER:--}${lease_log_holders}"
}

# Metric kill switch: MERGE_LEASE_METRICS_ENABLED (default true; true/1/yes/on).
_merge_lease_metrics_enabled()
{
	case "$(printf '%s' "${MERGE_LEASE_METRICS_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) return 0 ;;
	esac
	return 1
}

# Seconds since <acquired epoch>; empty when the record has no acquired= (0).
_merge_lease_metric_hold_seconds()
{
	local metric_acquired="${1:-0}" metric_now
	[[ "${metric_acquired}" =~ ^[1-9][0-9]*$ ]] || return 0
	metric_now="$(date -u +%s 2>/dev/null)" || return 0
	[[ "${metric_now}" =~ ^[0-9]+$ ]] || return 0
	[ "${metric_now}" -ge "${metric_acquired}" ] || return 0
	printf '%s' "$((metric_now - metric_acquired))"
}

# _merge_lease_metric <event> <base> <pr> <mode> <reason> [<hold_s>] [<waiters>] [<holder>] [<ts>] [<merged>]
# Prints one MERGE_LEASE_METRIC line (stdout, like _merge_lease_log). Holder
# defaults to MERGE_LEASE_PR (pass `-` for "no holder"; an empty argument still
# falls back). ts defaults to now; a caller passes the decision's epoch when
# the line is printed after later API calls (the tick's grant). Numbers that
# are not digits and tokens outside [a-z_] print as `-`; no path, label or
# record text is printed. Makes no gh call, sets no MERGE_LEASE_* global and always returns 0 (fail-open).
_merge_lease_metric()
{
	{
		_merge_lease_metrics_enabled || return 0
		local metric_event="${1:-}" metric_base="${2:-}" metric_pr="${3:-}" metric_mode="${4:-}" metric_reason="${5:-}"
		local metric_hold="${6:-}" metric_waiters="${7:-}" metric_holder="${8:-${MERGE_LEASE_PR:-}}" metric_ts="${9:-}"
		# Optional <merged> (10th; the tick's `released` lines): a trailing
		# ` merged=true|false` for the wasted-hold share (plan K1). Anything else
		# prints nothing, so every other line keeps its exact format.
		local metric_merged_field=""
		case "${10:-}" in true|false) metric_merged_field=" merged=${10}" ;; esac
		[[ "${metric_event}" =~ ^[a-z_]+$ ]] || metric_event="-"
		[[ "${metric_mode}" =~ ^[a-z_]+$ ]] || metric_mode="-"
		[[ "${metric_reason}" =~ ^[a-z_]+$ ]] || metric_reason="-"
		_merge_lease_valid_base "${metric_base}" || metric_base="-"
		[[ "${metric_pr}" =~ ^[0-9]+$ ]] || metric_pr="-"
		[[ "${metric_holder}" =~ ^[0-9]+$ ]] || metric_holder="-"
		[[ "${metric_hold}" =~ ^[0-9]+$ ]] || metric_hold="-"
		[[ "${metric_waiters}" =~ ^[0-9]+$ ]] || metric_waiters="-"
		[[ "${metric_ts}" =~ ^[0-9]+$ ]] || { metric_ts="$(date -u +%s)" || metric_ts=""; }
		[[ "${metric_ts}" =~ ^[0-9]+$ ]] || metric_ts="-"
		echo "MERGE_LEASE_METRIC event=${metric_event} base=${metric_base} pr=${metric_pr} mode=${metric_mode} reason=${metric_reason} holder=${metric_holder} hold_s=${metric_hold} waiters=${metric_waiters} ts=${metric_ts}${metric_merged_field}" || true
	} 2>/dev/null
	return 0
}

_merge_lease_label_wait()
{
	local lease_pr="$1" lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	GH_RETRY_IDEMPOTENT=true gh_retry _safe_gh_jq -X POST "repos/${lease_repo}/issues/${lease_pr}/labels" \
		-f "labels[]=${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" >/dev/null 2>&1 \
		|| echo "::warning::MERGE_LEASE pr=${lease_pr} outcome=label_failed (the merge-train release cannot find this waiter; the review sweep still re-dispatches it)"
}

_merge_lease_unlabel_wait()
{
	local lease_pr="$1" lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	gh_retry _safe_gh_jq -X DELETE "repos/${lease_repo}/issues/${lease_pr}/labels/$(printf '%s' "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" | jq -sRr @uri)" >/dev/null 2>&1 || true
}

# _merge_lease_gate_multi_wait <pr> <base> <mode> <reason> [<holder>]: the
# Phase 5 wait (log, metric, wait label); always returns 1.
_merge_lease_gate_multi_wait()
{
	MERGE_LEASE_OUTCOME="wait"
	MERGE_LEASE_REASON="$4"
	_merge_lease_log "$1" "$2" "$3" wait "$4"
	_merge_lease_metric wait "$2" "$1" "$3" "$4" "" "" "${5:-}" || true
	_merge_lease_label_wait "$1"
	return 1
}

# _merge_lease_gate_multi <pr> <base> <head> update|merge <max_holders>
# Phase 5 (Refs #9781): _merge_lease_gate's decision when the record just
# read has a live holder and MERGE_LEASE_MAX_HOLDERS > 1, or 2+ live holders.
# 0 proceed, 1 wait (labelled, except a holder's merge-mode wait), 3 lost the compare-and-swap (the caller reads
# again and decides on the winning record). Outcome and reason words are the
# single-holder ones; MERGE_LEASE_BLOCKER is one PR: the first holder (record
# order) sharing a path, the primary at the holder cap, or the first waiter
# ahead in queue order whose paths are disjoint from every holder.
# - merge: a holder proceeds (self) unless its current files now share a path
#   with an earlier holder in record order (then it waits holder_shares_paths
#   on that holder); another PR waits on the first holder it
#   shares a path with (_merge_lease_paths_intersect with both heads, so the
#   Phase 4 soft class applies), else proceeds. Its own unreadable files fail
#   open (pr_files_failed), as in the single-holder path.
# - update: a holder renews its own row (max-hold on its own acquired time);
#   at the cap a newcomer waits `held`; below it a newcomer waits on a holder
#   it shares a path with, then on the first disjoint waiter ahead (waiters
#   overlapping a holder are passed over; at most 10 are read), else is added
#   as a holder. Unreadable files of the caller or a waiter count as `*`.
_merge_lease_gate_multi()
{
	local gm_pr="$1" gm_base="$2" gm_head="$3" gm_mode="$4" gm_max="$5"
	local gm_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" gm_rows="${MERGE_LEASE_HOLDER_ROWS:-}"
	local gm_count="${MERGE_LEASE_HOLDER_COUNT:-0}" gm_own_row gm_own_paths="" gm_own_read=0 gm_line gm_h gm_h_head gm_hpaths
	local gm_soft="" gm_now gm_rc=0 gm_keep_acq gm_new_rows gm_pulls_json gm_first gm_excluded="" gm_cand_n=0 gm_cand_paths
	local gm_cand_head gm_overlap gm_was_waiting=0 gm_skips="" gm_ttl gm_row_head
	local -a gm_row_arr=()
	mapfile -t gm_row_arr < <(printf '%s\n' "${gm_rows}" | sed '/^$/d')
	gm_own_row="$(_merge_lease_holder_row "${gm_pr}" "${gm_rows}")"
	if [ "${gm_mode}" = "merge" ]; then
		if [ -n "${gm_own_row}" ]; then
			# A holder whose PR grew onto an earlier holder's paths (record
			# order) after it joined waits on that holder; a later holder waits
			# on it instead, so two holders never block each other. One files
			# read, only when an earlier holder exists; an unreadable or empty
			# list proceeds as before (fail-open).
			for gm_line in ${gm_row_arr[@]+"${gm_row_arr[@]}"}; do
				read -r gm_h gm_h_head _ <<< "${gm_line}"
				[ "${gm_h}" != "${gm_pr}" ] || break
				[ "${gm_h_head}" != "-" ] || gm_h_head=""
				if [ "${gm_own_read}" = 0 ]; then
					gm_own_read=1
					gm_own_paths="$(_merge_lease_pr_paths "${gm_pr}" 2>/dev/null)" || break
					[ -n "${gm_own_paths}" ] || break
				fi
				MERGE_LEASE_SOFT_SHARED=""
				if _merge_lease_paths_intersect "$(_merge_lease_holder_paths "${gm_h}")" "${gm_own_paths}" "${gm_head}" "${gm_h_head}"; then
					MERGE_LEASE_BLOCKER="${gm_h}"
					MERGE_LEASE_SOFT_SHARED=""
					# No wait label here: this PR is still a holder, and the
					# tick excludes holders from its grant pass while the sweep
					# skips labelled PRs, so a label would leave nothing to
					# re-dispatch it once the earlier holder merges. Unlabelled,
					# the review sweep retries it as before the lease.
					MERGE_LEASE_OUTCOME="wait"
					MERGE_LEASE_REASON="holder_shares_paths"
					_merge_lease_log "${gm_pr}" "${gm_base}" merge wait holder_shares_paths
					_merge_lease_metric wait "${gm_base}" "${gm_pr}" merge holder_shares_paths "" "" "${gm_h}" || true
					return 1
				fi
			done
			MERGE_LEASE_SOFT_SHARED=""
			MERGE_LEASE_OUTCOME="self"
			MERGE_LEASE_REASON="self"
			_merge_lease_log "${gm_pr}" "${gm_base}" merge proceed self
			_merge_lease_metric proceed "${gm_base}" "${gm_pr}" merge self "" "" "${gm_pr}" || true
			return 0
		fi
		MERGE_LEASE_BLOCKER="${MERGE_LEASE_PR}"
		for gm_line in ${gm_row_arr[@]+"${gm_row_arr[@]}"}; do
			read -r gm_h gm_h_head _ <<< "${gm_line}"
			[ "${gm_h_head}" != "-" ] || gm_h_head=""
			gm_hpaths="$(_merge_lease_holder_paths "${gm_h}")"
			if [ "${gm_hpaths}" != "*" ] && [ "${gm_own_read}" = 0 ]; then
				gm_own_read=1
				if ! gm_own_paths="$(_merge_lease_pr_paths "${gm_pr}")"; then
					MERGE_LEASE_OUTCOME="unavailable"
					MERGE_LEASE_REASON="pr_files_failed"
					echo "::warning::$(_merge_lease_log "${gm_pr}" "${gm_base}" merge unavailable pr_files_failed) (fail-open)"
					_merge_lease_metric unavailable "${gm_base}" "${gm_pr}" merge pr_files_failed || true
					return 0
				fi
			fi
			MERGE_LEASE_SOFT_SHARED=""
			if _merge_lease_paths_intersect "${gm_hpaths}" "${gm_own_paths:-*}" "${gm_head}" "${gm_h_head}"; then
				MERGE_LEASE_BLOCKER="${gm_h}"
				MERGE_LEASE_SOFT_SHARED=""
				_merge_lease_gate_multi_wait "${gm_pr}" "${gm_base}" merge holder_shares_paths "${gm_h}"
				return 1
			fi
			[ -z "${MERGE_LEASE_SOFT_SHARED}" ] || gm_soft="${gm_soft:+${gm_soft},}${MERGE_LEASE_SOFT_SHARED}"
		done
		MERGE_LEASE_SOFT_SHARED="${gm_soft}"
		MERGE_LEASE_OUTCOME="no_overlap"
		MERGE_LEASE_REASON="no_shared_paths_with_holder"
		[ -n "${gm_soft}" ] && MERGE_LEASE_REASON="soft_paths_merge_clean"
		_merge_lease_log "${gm_pr}" "${gm_base}" merge proceed "${MERGE_LEASE_REASON}"
		_merge_lease_metric proceed "${gm_base}" "${gm_pr}" merge "${MERGE_LEASE_REASON}" || true
		return 0
	fi
	gm_now="$(date -u +%s)"
	gm_ttl="$(_merge_lease_ttl_seconds)"
	gm_row_head="${gm_head}"
	[[ "${gm_row_head}" =~ ^[0-9a-f]{40}$ ]] || gm_row_head="-"
	if [ -n "${gm_own_row}" ]; then
		# Renewal of our own row; the other holders' rows are kept as read.
		read -r _ _ _ gm_keep_acq _ <<< "${gm_own_row}"
		if [ "${gm_keep_acq}" -gt 0 ] && [ $((gm_now - gm_keep_acq)) -ge "$(_merge_lease_max_hold_seconds)" ]; then
			MERGE_LEASE_OUTCOME="self"
			MERGE_LEASE_REASON="max_hold_reached"
			_merge_lease_log "${gm_pr}" "${gm_base}" update proceed max_hold_reached
			_merge_lease_metric proceed "${gm_base}" "${gm_pr}" update max_hold_reached "$(_merge_lease_metric_hold_seconds "${gm_keep_acq}" 2>/dev/null)" "" "${gm_pr}" || true
			return 0
		fi
		[ "${gm_keep_acq}" -gt 0 ] || gm_keep_acq="${gm_now}"
		# An unreadable files list keeps the paths already recorded for this
		# holder instead of widening it to `*`.
		if ! gm_own_paths="$(_merge_lease_pr_paths "${gm_pr}" 2>/dev/null)"; then
			gm_own_paths="$(_merge_lease_holder_paths "${gm_pr}")"
			# No holder_path= line of its own with 2+ holder rows (expired rows
			# included): the helper fell back to the union of every holder;
			# record `*` (unknown) instead.
			if [ "$(printf '%s\n' "${MERGE_LEASE_HOLDER_ALL_ROWS:-}" | grep -c .)" -gt 1 ] && ! printf '%s\n' "${MERGE_LEASE_HOLDER_PATHS:-}" | grep -q "^${gm_pr}"$'\t'; then
				gm_own_paths="*"
			fi
		fi
		_merge_lease_holder_paths_set "${gm_pr}" "${gm_own_paths:-*}"
		gm_new_rows="$(printf '%s\n' "${gm_rows}" | awk -v p="${gm_pr}" -v row="${gm_pr} ${gm_row_head} $((gm_now + gm_ttl)) ${gm_keep_acq} ${gm_now}" 'NF { if ($1 == p) print row; else print }')"
		_merge_lease_write_holders "${gm_base}" "${gm_new_rows}" || gm_rc=$?
		case "${gm_rc}" in
			0)
				MERGE_LEASE_OUTCOME="renewed"
				MERGE_LEASE_REASON="renewed"
				_merge_lease_log "${gm_pr}" "${gm_base}" update renewed "ttl_s=${gm_ttl}"
				_merge_lease_metric renewed "${gm_base}" "${gm_pr}" update renewed "$(_merge_lease_metric_hold_seconds "${gm_keep_acq}" 2>/dev/null)" "" "${gm_pr}" || true
				return 0
				;;
			3)
				_merge_lease_log "${gm_pr}" "${gm_base}" update retry lost_race
				_merge_lease_metric retry "${gm_base}" "${gm_pr}" update lost_race || true
				return 3
				;;
			*)
				MERGE_LEASE_OUTCOME="unavailable"
				MERGE_LEASE_REASON="write_failed"
				echo "::warning::$(_merge_lease_log "${gm_pr}" "${gm_base}" update unavailable write_failed) (fail-open: updating as before the lease)"
				_merge_lease_metric unavailable "${gm_base}" "${gm_pr}" update write_failed || true
				return 0
				;;
		esac
	fi
	if [ "${gm_count}" -ge "${gm_max}" ]; then
		MERGE_LEASE_BLOCKER="${MERGE_LEASE_PR}"
		_merge_lease_gate_multi_wait "${gm_pr}" "${gm_base}" update held
		return 1
	fi
	gm_own_paths="$(_merge_lease_pr_paths "${gm_pr}" 2>/dev/null)" || gm_own_paths="*"
	[ -n "${gm_own_paths}" ] || gm_own_paths="*"
	for gm_line in ${gm_row_arr[@]+"${gm_row_arr[@]}"}; do
		read -r gm_h gm_h_head _ <<< "${gm_line}"
		[ "${gm_h_head}" != "-" ] || gm_h_head=""
		gm_excluded="${gm_excluded:+${gm_excluded},}${gm_h}"
		MERGE_LEASE_SOFT_SHARED=""
		if _merge_lease_paths_intersect "$(_merge_lease_holder_paths "${gm_h}")" "${gm_own_paths}" "${gm_head}" "${gm_h_head}"; then
			MERGE_LEASE_BLOCKER="${gm_h}"
			MERGE_LEASE_SOFT_SHARED=""
			_merge_lease_gate_multi_wait "${gm_pr}" "${gm_base}" update holder_shares_paths "${gm_h}"
			return 1
		fi
	done
	MERGE_LEASE_SOFT_SHARED=""
	# Queue: the first waiter ahead whose paths are disjoint from every holder
	# goes first. The listing is the single-holder path's call.
	gm_pulls_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${gm_repo}/pulls?state=open&base=$(printf '%s' "${gm_base}" | jq -sRr @uri)&per_page=100" 2>/dev/null)" || gm_pulls_json=""
	if jq -e 'type == "array"' >/dev/null 2>&1 <<< "${gm_pulls_json}"; then
		_merge_lease_revoke_enabled && gm_skips="${MERGE_LEASE_SKIPS:-}"
		if printf '%s' "${gm_pulls_json}" | jq -e --argjson n "${gm_pr}" --arg l "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" \
			'[ .[]? | if type == "array" then .[] else . end | select(type == "object" and .number == $n) | (.labels // [])[] | (if type == "object" then .name else . end) | select(. == $l) ] | length > 0' >/dev/null 2>&1; then
			gm_was_waiting=1
		fi
		while :; do
			gm_first="$(_merge_lease_queue_first "${gm_pulls_json}" "${gm_pr}" "${gm_skips}" "${gm_excluded}")"
			[ -n "${gm_first}" ] && [ "${gm_first}" != "${gm_pr}" ] || break
			gm_cand_n=$((gm_cand_n + 1))
			if [ "${gm_cand_n}" -gt 10 ]; then
				MERGE_LEASE_BLOCKER="${gm_first}"
				_merge_lease_gate_multi_wait "${gm_pr}" "${gm_base}" update queued_behind
				return 1
			fi
			gm_cand_head="$(jq -r --argjson n "${gm_first}" '[ .[]? | if type == "array" then .[] else . end | select(type == "object" and .number == $n)
				| (if (.head | type) == "object" then .head.sha else .head_sha end) // "" ][0] // ""' <<< "${gm_pulls_json}" 2>/dev/null)" || gm_cand_head=""
			gm_cand_paths="$(_merge_lease_pr_paths "${gm_first}" 2>/dev/null)" || gm_cand_paths="*"
			[ -n "${gm_cand_paths}" ] || gm_cand_paths="*"
			gm_overlap=0
			for gm_line in ${gm_row_arr[@]+"${gm_row_arr[@]}"}; do
				read -r gm_h gm_h_head _ <<< "${gm_line}"
				[ "${gm_h_head}" != "-" ] || gm_h_head=""
				if _merge_lease_paths_intersect "$(_merge_lease_holder_paths "${gm_h}")" "${gm_cand_paths}" "${gm_cand_head}" "${gm_h_head}"; then
					gm_overlap=1
					break
				fi
			done
			MERGE_LEASE_SOFT_SHARED=""
			if [ "${gm_overlap}" = 1 ]; then
				gm_excluded="${gm_excluded:+${gm_excluded},}${gm_first}"
				continue
			fi
			MERGE_LEASE_BLOCKER="${gm_first}"
			_merge_lease_gate_multi_wait "${gm_pr}" "${gm_base}" update queued_behind
			return 1
		done
	fi
	# Disjoint from every holder and first among the disjoint waiters: join.
	_merge_lease_holder_paths_set "${gm_pr}" "${gm_own_paths}"
	gm_new_rows="$(printf '%s\n%s\n' "${gm_rows}" "${gm_pr} ${gm_row_head} $((gm_now + gm_ttl)) ${gm_now} ${gm_now}" | sed '/^$/d')"
	_merge_lease_write_holders "${gm_base}" "${gm_new_rows}" || gm_rc=$?
	case "${gm_rc}" in
		0)
			MERGE_LEASE_OUTCOME="acquired"
			MERGE_LEASE_REASON="acquired"
			MERGE_LEASE_HOLDER_ROWS="${gm_new_rows}"
			MERGE_LEASE_HOLDER_COUNT=$((gm_count + 1))
			_merge_lease_log "${gm_pr}" "${gm_base}" update acquired "ttl_s=${gm_ttl}"
			_merge_lease_metric acquired "${gm_base}" "${gm_pr}" update acquired 0 "" "${gm_pr}" || true
			[ "${gm_was_waiting}" = "1" ] && _merge_lease_unlabel_wait "${gm_pr}"
			return 0
			;;
		3)
			_merge_lease_log "${gm_pr}" "${gm_base}" update retry lost_race
			_merge_lease_metric retry "${gm_base}" "${gm_pr}" update lost_race || true
			return 3
			;;
		*)
			MERGE_LEASE_OUTCOME="unavailable"
			MERGE_LEASE_REASON="write_failed"
			echo "::warning::$(_merge_lease_log "${gm_pr}" "${gm_base}" update unavailable write_failed) (fail-open: updating as before the lease)"
			_merge_lease_metric unavailable "${gm_base}" "${gm_pr}" update write_failed || true
			return 0
			;;
	esac
}

# _merge_lease_gate <pr> <base> <head> update|merge
# Returns 0 to proceed (MERGE_LEASE_OUTCOME acquired|renewed|free|no_overlap|
# self|unavailable|disabled), 1 to wait (MERGE_LEASE_OUTCOME=wait,
# MERGE_LEASE_REASON, MERGE_LEASE_BLOCKER = the PR waited on). A waiting PR is
# labelled MERGE_LEASE_WAIT_LABEL; a PR that acquires the lease is unlabelled.
# Phase 5 exception: a live holder whose files now share a path with an
# earlier holder waits in merge mode (holder_shares_paths) without the label,
# because the tick never grants a holder; the review sweep retries it.
_merge_lease_gate()
{
	local lease_pr="$1" lease_base="$2" lease_head="$3" lease_mode="$4"
	local lease_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local lease_rc=0 lease_own_paths="" lease_pulls_json="" lease_first="" lease_attempt lease_was_waiting=0
	local lease_max_holders=1 lease_multi_rc=0
	MERGE_LEASE_OUTCOME=""
	MERGE_LEASE_REASON=""
	MERGE_LEASE_BLOCKER=""
	MERGE_LEASE_SOFT_SHARED=""
	if ! _merge_lease_enabled; then
		MERGE_LEASE_OUTCOME="disabled"
		return 0
	fi
	if ! [[ "${lease_pr}" =~ ^[1-9][0-9]*$ ]] || ! _merge_lease_valid_base "${lease_base}"; then
		MERGE_LEASE_OUTCOME="unavailable"
		MERGE_LEASE_REASON="invalid_input"
		_merge_lease_log "${lease_pr}" "${lease_base}" "${lease_mode}" unavailable invalid_input
		# No record was read in this call: holder `-`, not a stale MERGE_LEASE_PR.
		_merge_lease_metric unavailable "${lease_base}" "${lease_pr}" "${lease_mode}" invalid_input "" "" - || true
		return 0
	fi
	# Phase 5 (Refs #9781): above 1, or with 2+ live holders in the record,
	# _merge_lease_gate_multi decides; otherwise the single-holder code below
	# runs unchanged.
	lease_max_holders="$(_merge_lease_max_holders)"
	for lease_attempt in 1 2; do
		lease_rc=0
		_merge_lease_read "${lease_base}" || lease_rc=$?
		if [ "${lease_rc}" -eq 1 ]; then
			MERGE_LEASE_OUTCOME="unavailable"
			MERGE_LEASE_REASON="read_failed"
			echo "::warning::$(_merge_lease_log "${lease_pr}" "${lease_base}" "${lease_mode}" unavailable read_failed) (fail-open: proceeding as before the lease)"
			_merge_lease_metric unavailable "${lease_base}" "${lease_pr}" "${lease_mode}" read_failed || true
			return 0
		fi
		if [ "${lease_rc}" -eq 0 ] && [ "${MERGE_LEASE_HOLDER_COUNT:-0}" -ge 1 ] \
			&& { [ "${lease_max_holders}" -gt 1 ] || [ "${MERGE_LEASE_HOLDER_COUNT:-0}" -gt 1 ]; }; then
			lease_multi_rc=0
			_merge_lease_gate_multi "${lease_pr}" "${lease_base}" "${lease_head}" "${lease_mode}" "${lease_max_holders}" || lease_multi_rc=$?
			[ "${lease_multi_rc}" -eq 3 ] && continue
			return "${lease_multi_rc}"
		fi
		if [ "${lease_rc}" -eq 0 ] && _merge_lease_is_held && [ "${MERGE_LEASE_PR}" != "${lease_pr}" ]; then
			MERGE_LEASE_BLOCKER="${MERGE_LEASE_PR}"
			if [ "${lease_mode}" = "merge" ]; then
				if [ "${MERGE_LEASE_PATHS}" != "*" ]; then
					if ! lease_own_paths="$(_merge_lease_pr_paths "${lease_pr}")"; then
						MERGE_LEASE_OUTCOME="unavailable"
						MERGE_LEASE_REASON="pr_files_failed"
						echo "::warning::$(_merge_lease_log "${lease_pr}" "${lease_base}" merge unavailable pr_files_failed) (fail-open)"
						_merge_lease_metric unavailable "${lease_base}" "${lease_pr}" merge pr_files_failed || true
						return 0
					fi
				fi
				MERGE_LEASE_SOFT_SHARED=""
				if ! _merge_lease_paths_intersect "${MERGE_LEASE_PATHS}" "${lease_own_paths:-*}" "${lease_head}" "${MERGE_LEASE_HEAD:-}"; then
					MERGE_LEASE_OUTCOME="no_overlap"
					MERGE_LEASE_REASON="no_shared_paths_with_holder"
					# Phase 4 (Refs #9781): cleared only by a clean merge-tree
					# probe of soft paths shared with the holder.
					[ -n "${MERGE_LEASE_SOFT_SHARED}" ] && MERGE_LEASE_REASON="soft_paths_merge_clean"
					_merge_lease_log "${lease_pr}" "${lease_base}" merge proceed "${MERGE_LEASE_REASON}"
					_merge_lease_metric proceed "${lease_base}" "${lease_pr}" merge "${MERGE_LEASE_REASON}" || true
					return 0
				fi
				MERGE_LEASE_REASON="holder_shares_paths"
			else
				MERGE_LEASE_REASON="held"
			fi
			MERGE_LEASE_OUTCOME="wait"
			_merge_lease_log "${lease_pr}" "${lease_base}" "${lease_mode}" wait "${MERGE_LEASE_REASON}"
			_merge_lease_metric wait "${lease_base}" "${lease_pr}" "${lease_mode}" "${MERGE_LEASE_REASON}" || true
			_merge_lease_label_wait "${lease_pr}"
			return 1
		fi
		if [ "${lease_mode}" = "merge" ]; then
			MERGE_LEASE_OUTCOME="free"
			[ "${lease_rc}" -eq 0 ] && _merge_lease_is_held && MERGE_LEASE_OUTCOME="self"
			MERGE_LEASE_REASON="${MERGE_LEASE_OUTCOME}"
			_merge_lease_log "${lease_pr}" "${lease_base}" merge proceed "${MERGE_LEASE_OUTCOME}"
			_merge_lease_metric proceed "${lease_base}" "${lease_pr}" merge "${MERGE_LEASE_OUTCOME}" || true
			return 0
		fi
		# Update mode, lease free or already ours. A free lease goes to the
		# first PR in queue order; ours is renewed.
		if ! { [ "${lease_rc}" -eq 0 ] && _merge_lease_is_held && [ "${MERGE_LEASE_PR}" = "${lease_pr}" ]; }; then
			lease_pulls_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${lease_repo}/pulls?state=open&base=$(printf '%s' "${lease_base}" | jq -sRr @uri)&per_page=100" 2>/dev/null)" || lease_pulls_json=""
			if jq -e 'type == "array"' >/dev/null 2>&1 <<< "${lease_pulls_json}"; then
				if _merge_lease_revoke_enabled; then
					# Phase 2: a PR on the record's skip list (unchanged head) does
					# not jump ahead of another waiter; with no other waiter it
					# still acquires, and its write drops its own entry.
					lease_first="$(_merge_lease_queue_first "${lease_pulls_json}" "${lease_pr}" "${MERGE_LEASE_SKIPS:-}")"
				else
					lease_first="$(_merge_lease_queue_first "${lease_pulls_json}" "${lease_pr}")"
				fi
				if printf '%s' "${lease_pulls_json}" | jq -e --argjson n "${lease_pr}" --arg l "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" \
					'[ .[]? | if type == "array" then .[] else . end | select(type == "object" and .number == $n) | (.labels // [])[] | (if type == "object" then .name else . end) | select(. == $l) ] | length > 0' >/dev/null 2>&1; then
					lease_was_waiting=1
				fi
				if [ -n "${lease_first}" ] && [ "${lease_first}" != "${lease_pr}" ]; then
					MERGE_LEASE_BLOCKER="${lease_first}"
					MERGE_LEASE_OUTCOME="wait"
					MERGE_LEASE_REASON="queued_behind"
					_merge_lease_log "${lease_pr}" "${lease_base}" update wait queued_behind
					_merge_lease_metric wait "${lease_base}" "${lease_pr}" update queued_behind || true
					_merge_lease_label_wait "${lease_pr}"
					return 1
				fi
			fi
			# An unreadable listing proves no queue: take the lease (fail-open).
		fi
		local lease_keep_acquired=""
		if [ "${lease_rc}" -eq 0 ] && _merge_lease_is_held && [ "${MERGE_LEASE_PR}" = "${lease_pr}" ]; then
			# Renewal. A holder past MERGE_LEASE_MAX_HOLD_MINUTES since it
			# first acquired keeps the lease only until its current expiry,
			# so a holder whose update keeps failing cannot hold it forever.
			lease_keep_acquired="${MERGE_LEASE_ACQUIRED:-0}"
			if [ "${lease_keep_acquired}" -gt 0 ] && [ $(( $(date -u +%s) - lease_keep_acquired )) -ge "$(_merge_lease_max_hold_seconds)" ]; then
				MERGE_LEASE_OUTCOME="self"
				MERGE_LEASE_REASON="max_hold_reached"
				_merge_lease_log "${lease_pr}" "${lease_base}" update proceed max_hold_reached
				_merge_lease_metric proceed "${lease_base}" "${lease_pr}" update max_hold_reached "$(_merge_lease_metric_hold_seconds "${lease_keep_acquired}" 2>/dev/null)" || true
				return 0
			fi
		fi
		[ -n "${lease_own_paths}" ] || lease_own_paths="$(_merge_lease_pr_paths "${lease_pr}" 2>/dev/null || true)"
		lease_rc=0
		_merge_lease_write "${lease_base}" held "${lease_pr}" "${lease_head}" "${lease_own_paths:-*}" "${lease_keep_acquired}" || lease_rc=$?
		case "${lease_rc}" in
			0)
				if [ "${MERGE_LEASE_PR:-}" = "${lease_pr}" ] && [ "${MERGE_LEASE_STATE:-}" = "held" ]; then
					MERGE_LEASE_OUTCOME="renewed"
				else
					MERGE_LEASE_OUTCOME="acquired"
				fi
				MERGE_LEASE_PR="${lease_pr}"
				MERGE_LEASE_STATE="held"
				MERGE_LEASE_REASON="${MERGE_LEASE_OUTCOME}"
				_merge_lease_log "${lease_pr}" "${lease_base}" update "${MERGE_LEASE_OUTCOME}" "ttl_s=$(_merge_lease_ttl_seconds)"
				# Our own record past expiry leaves lease_keep_acquired empty, so the
				# write stamped acquired=now: for the metric that is a new hold
				# (event=acquired hold_s=0). The MERGE_LEASE line keeps `renewed`.
				if [ "${MERGE_LEASE_OUTCOME}" = "renewed" ] && [ -n "${lease_keep_acquired}" ]; then
					_merge_lease_metric renewed "${lease_base}" "${lease_pr}" update renewed "$(_merge_lease_metric_hold_seconds "${lease_keep_acquired}" 2>/dev/null)" || true
				else
					_merge_lease_metric acquired "${lease_base}" "${lease_pr}" update acquired 0 || true
				fi
				[ "${lease_was_waiting}" = "1" ] && _merge_lease_unlabel_wait "${lease_pr}"
				return 0
				;;
			3)
				# Lost the race: decide again on the record that won.
				_merge_lease_log "${lease_pr}" "${lease_base}" update retry lost_race
				_merge_lease_metric retry "${lease_base}" "${lease_pr}" update lost_race || true
				continue
				;;
			*)
				MERGE_LEASE_OUTCOME="unavailable"
				MERGE_LEASE_REASON="write_failed"
				echo "::warning::$(_merge_lease_log "${lease_pr}" "${lease_base}" update unavailable write_failed) (fail-open: updating as before the lease; check that GitHub accepts ${MERGE_LEASE_REF_PREFIX:-refs/merge-lease/} refs)"
				_merge_lease_metric unavailable "${lease_base}" "${lease_pr}" update write_failed || true
				return 0
				;;
		esac
	done
	MERGE_LEASE_OUTCOME="unavailable"
	MERGE_LEASE_REASON="contended"
	echo "::warning::$(_merge_lease_log "${lease_pr}" "${lease_base}" "${lease_mode}" unavailable contended) (fail-open)"
	_merge_lease_metric unavailable "${lease_base}" "${lease_pr}" "${lease_mode}" contended || true
	return 0
}

# ---------------------------------------------------------------------------
# Final-CI merge resume (2026-11-17, Q4: A).
#
# Why: the merge paths wait at most AUTO_MERGE_CHECKS_WAIT_MINUTES (45) for
# the head's final CI run, holding a runner. With the pool congested a final
# run took 1.5-2.5 hours, so the wait timed out (#9603 gave up 76 s before
# its run went green) and nothing merged the PR when CI later passed: only the
# 30-minute sweep re-dispatched a whole review run, which then queued for
# hours behind ~66 others (#9545, #9597, #9603 on 2026-11-17).
#
# When a merge path's wait (AUTO_MERGE_CHECKS_WAIT_MINUTES > 0) times out,
# _pr_final_ci_merge_pending_mark records the approval on the head itself: a
# `pending` commit status FINAL_CI_MERGE_PENDING_CONTEXT
# (default ai-review/final-ci-merge-pending) on that SHA, posted by the
# pipeline token, plus the PR label FINAL_CI_MERGE_PENDING_LABEL
# (default ai:final-ci-merge-pending) so the tick can find it with the open-PR
# listing it already reads. The status is the authorization (SHA-bound, only
# writers can post statuses, and the tick checks its creator); the label is
# only an index. The merge-train release step (every poll tick) then enables
# head-bound auto-merge once the final CI run is green (see
# scripts/review_merge_train.sh _mt_final_ci_merge_resume).
#
# Env: FINAL_CI_MERGE_RESUME_ENABLED (default true). API: one status POST and
# one label POST per timed-out wait. Log: `FINAL_CI_MERGE_PENDING pr=<n>
# head=<sha> outcome=marked|failed|disabled|skipped reason=<token>`.
# ---------------------------------------------------------------------------
_pr_final_ci_merge_pending_mark()
{
	local pending_pr="$1" pending_head="$2" pending_repo="${PR_CHECKS_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	case "$(printf '%s' "${FINAL_CI_MERGE_RESUME_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) ;;
		*)
			echo "FINAL_CI_MERGE_PENDING pr=${pending_pr:-?} head=${pending_head:-?} outcome=disabled reason=switch_off"
			return 0
			;;
	esac
	if ! [[ "${pending_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${pending_head}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "FINAL_CI_MERGE_PENDING pr=${pending_pr:-?} head=${pending_head:-?} outcome=skipped reason=invalid_input"
		return 0
	fi
	if ! gh_retry _safe_gh_jq -X POST "repos/${pending_repo}/statuses/${pending_head}" \
		-f state=pending -f "context=${FINAL_CI_MERGE_PENDING_CONTEXT:-ai-review/final-ci-merge-pending}" \
		-f "description=Approved; merges when this head's final CI run passes" >/dev/null 2>&1; then
		echo "::warning::FINAL_CI_MERGE_PENDING pr=${pending_pr} head=${pending_head} outcome=failed reason=status_post_failed (the review sweep still retries)"
		return 0
	fi
	if ! GH_RETRY_IDEMPOTENT=true gh_retry _safe_gh_jq -X POST "repos/${pending_repo}/issues/${pending_pr}/labels" \
		-f "labels[]=${FINAL_CI_MERGE_PENDING_LABEL:-ai:final-ci-merge-pending}" >/dev/null 2>&1; then
		echo "::warning::FINAL_CI_MERGE_PENDING pr=${pending_pr} head=${pending_head} outcome=failed reason=label_post_failed (the review sweep still retries)"
		return 0
	fi
	echo "FINAL_CI_MERGE_PENDING pr=${pending_pr} head=${pending_head} outcome=marked reason=checks_wait_timeout"
	return 0
}
