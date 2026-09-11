_linked_prs_by_branch_name()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" \
		--head "ai/issue-${issue_num}" --state open \
		--json number --jq '.[].number' 2>/dev/null
}

_linked_prs_by_body_reference()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	local candidates_json
	candidates_json="$(gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" --state open \
		--search "#${issue_num} in:body" \
		--json number,body --limit 100 2>/dev/null || echo "[]")"
	printf '%s' "${candidates_json}" | jq -r --arg n "${issue_num}" '
		(. // [])
		| .[]
		| select((.body // "") | test("(?i)(close[sd]?|fix(es|ed)?|resolve[sd]?):?[[:space:]]+#" + $n + "\\b"))
		| .number
	' 2>/dev/null || true
}

_find_all_linked_prs()
{
	local issue_num="$1"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0
	{
		_issue_cross_ref_pr_numbers_unique "${issue_num}" 2>/dev/null || true
		_linked_prs_by_branch_name "${issue_num}" 2>/dev/null || true
		_linked_prs_by_body_reference "${issue_num}" 2>/dev/null || true
	} | grep -E '^[0-9]+$' | sort -u
}

_linked_pr_is_issue_implementation()
{
	local issue_num="$1"
	local head_ref="${2:-}"
	local body="${3:-}"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 1
	# Trailing boundary is a non-word character or end of line (same as the
	# body keyword below; cancel_zombie_runs_for_issue uses the looser
	# `([^0-9]|$)`), so `ai/issue-77a` / `ai/issue-77_x` are not treated as
	# issue 77; `ai/issue-77-retry` and `ai/77-slug` still are.
	if printf '%s\n' "${head_ref}" | grep -Eq "(^|/)(ai/(issue-)?|ai-(implement-)?)${issue_num}([^0-9A-Za-z_]|$)"; then
		return 0
	fi
	# Keyword substrings inside larger words are rejected, and the trailing
	# boundary is a non-word character or end of line, so neither
	# `prefixes #77` nor `Closes #77a` targets issue 77.
	if printf '%s\n' "${body}" | grep -Eiq "(^|[^[:alnum:]_-])(close[sd]?|fix(es|ed)?|resolve[sd]?):?[[:space:]]+#${issue_num}([^0-9A-Za-z_]|$)"; then
		return 0
	fi
	return 1
}

close_linked_pr() {
  local issue_num="$1"
  local close_reason="${2:-Closed by orchestrator stall recovery.}"
  local pr_nums
  pr_nums="$(_find_all_linked_prs "${issue_num}" 2>/dev/null || true)"

  # Diagnostic: previously this helper only consulted the timeline
  # cross-reference event and silently no-op'd when it returned nothing,
  # which is exactly how PR #2568 was orphaned in prod (issue #2552 was
  # re-issued but the PR stayed open).  Log every candidate source so any
  # future miss leaves a trail in the workflow log.
  if [ -z "${pr_nums}" ]; then
    echo "  close_linked_pr: no linked PRs found for issue #${issue_num} (timeline/branch/body lookups all empty)." >&2
    return 0
  fi

  local pr_num scanned=0 closed=0
  while IFS= read -r pr_num; do
    [[ "${pr_num}" =~ ^[0-9]+$ ]] || continue
    scanned=$((scanned + 1))
    # One `pulls/<n>` request per candidate (§15): the same call that
    # answers "is it still open?" also carries the head ref and body the
    # implementation-PR filter below needs, so its --jq is extended rather
    # than issuing a second request.  A failed call yields an empty blob,
    # which parses to state="" and is skipped as unknown, exactly as the
    # previous `.state`-only lookup behaved.
    local pr_meta_json pr_state pr_head_ref pr_base_ref pr_body
    pr_meta_json="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" \
      --jq '{state: (.state // ""), head_ref: (.head.ref // ""), base_ref: (.base.ref // ""), body: (.body // "")}' || echo "")"
    pr_state="$(printf '%s' "${pr_meta_json}" | jq -r '.state // ""' 2>/dev/null | grep -xE 'open|closed|merged' || echo "")"
    pr_head_ref="$(printf '%s' "${pr_meta_json}" | jq -r '.head_ref // ""' 2>/dev/null || echo "")"
    pr_base_ref="$(printf '%s' "${pr_meta_json}" | jq -r '.base_ref // ""' 2>/dev/null || echo "")"
    pr_body="$(printf '%s' "${pr_meta_json}" | jq -r '.body // ""' 2>/dev/null || echo "")"
    if [ "${pr_state}" != "open" ]; then
      echo "  close_linked_pr: skipping PR #${pr_num} for issue #${issue_num} (state=${pr_state:-unknown})."
      continue
    fi
    # Only the issue's own implementation PR is closed.  A PR that merely
    # cross-references the issue from an unrelated branch (see
    # _linked_pr_is_issue_implementation) is left open and logged so the
    # skip leaves the same trail the other outcomes do.
    if ! _linked_pr_is_issue_implementation "${issue_num}" "${pr_head_ref}" "${pr_body}"; then
      echo "  close_linked_pr: skipping PR #${pr_num} for issue #${issue_num} (cross-reference only; head=${pr_head_ref:-?} does not match the issue's implementation branch pattern and the body carries no close keyword; base=${pr_base_ref:-?} is shown for context and is not evaluated)."
      continue
    fi
    echo "  close_linked_pr: closing linked PR #${pr_num} for issue #${issue_num} (state=open)."
    if gh_retry gh pr close "${pr_num}" --repo "${GITHUB_REPOSITORY}" \
        --comment "${close_reason}" 2>/dev/null; then
      closed=$((closed + 1))
    fi
  done <<< "${pr_nums}"
  echo "  close_linked_pr: issue=#${issue_num} scanned=${scanned} closed=${closed}"
}

surface_reissue_closed_without_pr()
{
	local issue_num="$1"
	local phase="${2:-}"
	local stall_minutes="${3:-0}"
	local recovery_count="${4:-0}"
	local source_label="${5:-unknown}"
	[[ "${issue_num}" =~ ^[0-9]+$ ]] || return 0

	local issue_body parent_num
	issue_body="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body // ""' 2>/dev/null || echo "")"
	parent_num="$(printf '%s' "${issue_body}" | grep -oE 'Re-issued from #[0-9]+' | head -n1 | grep -oE '[0-9]+' | head -n1 || true)"
	if [ -z "${parent_num}" ]; then
		return 0
	fi

	local pr_nums
	pr_nums="$(_find_all_linked_prs "${issue_num}" 2>/dev/null || true)"
	if [ -n "${pr_nums}" ]; then
		return 0
	fi

	# Stable structured log prefix — documented in agents.md so
	# downstream alerting can grep it without parsing free-form text.
	echo "REISSUE_CLOSED_WITHOUT_PR issue=${issue_num} parent=${parent_num} phase=${phase} stall_minutes=${stall_minutes} recovery_count=${recovery_count} source=${source_label}"
	echo "::warning title=Re-issue closed without PR::Re-issue #${issue_num} (from #${parent_num}) closed without producing a PR; phase=${phase}, stuck ${stall_minutes}m, attempts=${recovery_count}, source=${source_label}."

	gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" -f body="$(cat <<COMMENT_EOF
⚠️ **Re-issue closed without producing a PR**

This issue was created by orchestrator stall recovery as a re-issue of #${parent_num}, but no PR was ever opened against \`ai/issue-${issue_num}\` before stall recovery exhausted. Surfacing the close-out for operational review.

- Last phase: \`${phase}\`
- Stalled for: ${stall_minutes} minutes
- Recovery attempts: ${recovery_count}
- Source loop: ${source_label}

The orchestrator will still create the next re-issue in the chain so forward progress continues, but a human should review issue #${parent_num} and the re-issue chain before further automation.
COMMENT_EOF
)" >/dev/null 2>&1 || true

	if declare -F memory_record_run_event >/dev/null 2>&1; then
		local meta
		meta="$(jq -cn \
			--arg repo "${GITHUB_REPOSITORY:-}" \
			--arg issue "${issue_num}" \
			--arg parent "${parent_num}" \
			--arg phase "${phase}" \
			--arg stall "${stall_minutes}" \
			--arg attempts "${recovery_count}" \
			--arg source "${source_label}" \
			'{repository:$repo,issue_number:$issue,parent_issue_number:$parent,last_phase:$phase,stall_minutes:$stall,recovery_count:$attempts,source:$source}' 2>/dev/null || echo '{}')"
		memory_record_run_event \
			--run-id "${GITHUB_RUN_ID:-local}" \
			--workflow "orchestrate_poll" \
			--event-type "reissue_closed_without_pr" \
			--status "warning" \
			--message "Re-issue #${issue_num} (from #${parent_num}) closed without producing a PR" \
			--issue-number "${issue_num}" \
			--actor "${GITHUB_ACTOR:-orchestrator}" \
			--metadata-json "${meta}" >/dev/null 2>&1 || true
	fi
}
