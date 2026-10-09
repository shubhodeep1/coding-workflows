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

# Built-in default required-check set. Defined set-if-unset so a caller
# that already declared it (the orchestrator does, near
# MAX_FINAL_MERGE_ATTEMPTS) keeps its value, and a caller that never set
# it (review_rb_judge.sh) still gets a SAFE non-empty default rather than
# the empty-string allow-all sentinel. Keep this string identical to the
# orchestrate_poll_process.sh ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT
# assignment; tests/test_pr_checks_lib_required_filter.py pins them equal.
: "${ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT:=CI,lint,Integration PR readiness check,Lint plan-archival completeness,Lint PR body for auto-close keywords against orchestrator-tracking issues,review / gate}"

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
# are in an unacceptable conclusion (success/neutral/skipped/cancelled
# are acceptable). Returns 0 when nothing is blocking, 1 otherwise
# (including API errors). Sets PR_CHECKS_LAST_REASON on every return.
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
# Usage:  _pr_checks_completed <PR_NUMBER> [<HEAD_SHA>] [<BASE_REF>]
#   HEAD_SHA optional: when omitted, fetched from the PR.
#   BASE_REF optional: callers that pass it (even as "") opt into the
#   required-checks filter. Legacy callers that omit BASE_REF entirely
#   keep the pre-filter "all check-runs must pass" behaviour.
_pr_checks_completed()
{
	PR_CHECKS_LAST_REASON="query_failed"
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

	# Allow-all sentinel: env var explicitly set to "" means treat every
	# check-run as advisory. Branch protection (when present) is enforced
	# server-side by GitHub at merge time.
	if [ "${required_names_csv}" = "" ]; then
		echo "  [check-runs] Required-checks gate is empty (allow-all); proceeding with merge for PR #${pr_number} (SHA ${head_sha:0:7})."
		PR_CHECKS_LAST_REASON="allow_all"
		return 0
	fi

	# Self-run exclusion predicate (jq): a check-run hosted by the current
	# GitHub Actions run is never counted as blocking. $self_run="" makes
	# the predicate always false, so the exclusion is a no-op for callers
	# (the orchestrator) that run from a separate workflow off the polled
	# SHA's check-run list. See PR #2703 for the review_rb_judge.sh case.
	local incomplete
	if [ "${required_names_csv}" = "*" ]; then
		# Legacy fail-closed-on-any-failure behaviour (with self-run exclusion).
		incomplete="$(printf '%s' "${check_runs_json}" | jq -r --arg self_run "${self_run}" '
			def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
			def _is_pending: .status != "completed" or (.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled");
			if (type == "array") then
				[.[]? | (.check_runs // [])[] | select(_is_pending and (_is_self_check_run | not))] | length
			elif (type == "object" and (.check_runs | type == "array")) then
				[.check_runs[] | select(_is_pending and (_is_self_check_run | not))] | length
			else
				empty
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
		incomplete="$(printf '%s' "${check_runs_json}" | jq -r --arg names "${required_names_csv}" --arg self_run "${self_run}" '
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
				[$runs[] | select(
					(
						(.status != "completed")
						or (
							(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled")
							and (.name as $n | $required | index($n))
						)
					)
					and (_is_self_check_run | not)
				)] | length
			end
		' 2>/dev/null | tail -n1)"
	fi
	if ! [[ "${incomplete}" =~ ^[0-9]+$ ]]; then
		echo "  [check-runs] Could not query check-runs for PR #${pr_number} (SHA ${head_sha:0:7}). Skipping merge."
		PR_CHECKS_LAST_REASON="query_failed"
		return 1
	fi

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
#       PR_BASE_FRESHNESS_HEAD_SHA, PR_BASE_FRESHNESS_BEHIND_BY and
#       PR_BASE_FRESHNESS_OVERLAP.
#   _pr_base_sync_for_fresh_ci <pr> <head_sha>
#       PUT /pulls/{n}/update-branch bound to <head_sha>; returns 0 when
#       GitHub accepted the update.
#   _pr_base_fresh_for_merge <pr> [<head_sha>] [<base_ref>]
#       The one call merge paths make. Returns 0 when the merge may go
#       ahead (fresh, clean, disabled, or unknown: an API failure is logged
#       and never holds a merge that today's gates allow), and 1 after
#       requesting the base update on overlap (also when that request
#       failed), so the caller skips this round and lets the synchronize
#       run re-validate.
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
		PR_BASE_FRESHNESS_LAST_REASON="base_diff_truncated"
		PR_BASE_FRESHNESS_OVERLAP="(base diff lists ${base_files_count} files; extent unknown)"
		PR_BASE_FRESHNESS_OUTCOME="overlap"
		echo "overlap"
		return 0
	fi

	# The PR's own paths: `--paginate --slurp` yields an array of pages,
	# each an array of file objects; renames count under both names.
	local pr_files_json tmp_compare tmp_pr_files overlap
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
		  else ([ $base_paths[] | select(. as $p | $pr_paths | index($p)) ] | unique | join(",")) end
	' 2>/dev/null | tail -n1)"
	rm -f "${tmp_compare}" "${tmp_pr_files}"
	if [ "${overlap}" = "__invalid__" ] || [ -z "${overlap+x}" ]; then
		PR_BASE_FRESHNESS_LAST_REASON="pr_files_failed"
		PR_BASE_FRESHNESS_OUTCOME="unknown"
		echo "unknown"
		return 0
	fi
	if [ -z "${overlap}" ]; then
		PR_BASE_FRESHNESS_LAST_REASON="no_shared_paths"
		PR_BASE_FRESHNESS_OUTCOME="clean"
		echo "clean"
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
			echo "MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-unknown} base=${base_ref:-unknown} outcome=overlap reason=${PR_BASE_FRESHNESS_LAST_REASON} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:--} overlap=${PR_BASE_FRESHNESS_OVERLAP:--}"
			echo "  [base-freshness] PR #${pr_number}: the base gained ${PR_BASE_FRESHNESS_BEHIND_BY:-?} commit(s) touching ${PR_BASE_FRESHNESS_OVERLAP:-shared paths}; updating the branch so CI and review run on the combined tree before merging."
			_pr_base_sync_for_fresh_ci "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}" || true
			return 1
			;;
		unknown)
			echo "::warning::MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${head_sha:-unknown} base=${base_ref:-unknown} outcome=unknown reason=${PR_BASE_FRESHNESS_LAST_REASON} action=proceed"
			return 0
			;;
		*)
			echo "MERGE_BASE_FRESHNESS pr=${pr_number} head_sha=${PR_BASE_FRESHNESS_HEAD_SHA:-${head_sha:-unknown}} base=${base_ref:-unknown} outcome=${PR_BASE_FRESHNESS_OUTCOME} reason=${PR_BASE_FRESHNESS_LAST_REASON} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:--} action=proceed"
			return 0
			;;
	esac
}
