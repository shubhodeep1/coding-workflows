#!/usr/bin/env bash
# Single-issue security pass (port P1, docs/plans/replace-claude-sessions-with-cli-engine-plan.md
# Phase 8a).
#
# A standalone PR (base = the default branch) gets the same security audit an
# orchestrator project gets before it merges. Two modes:
#
#   gate    Run by review_autofix.yml right before "Enable auto-merge on PR",
#           after a clean review. Writes `hold=true|false` to GITHUB_OUTPUT.
#           hold=false lets the merge steps run; hold=true skips them for this
#           run. The PR is eligible when its base is the default branch, its
#           head is in this repository and is not an orchestrator integration
#           branch, it carries no `e2e-smoke-test` label, and its linked issue
#           is not a verified automation follow-up (scripts/security_pass_skip.py,
#           the #4623 rules, so follow-ups of follow-ups never recurse). For an
#           eligible PR the latest trusted marker decides:
#             - `status=clean` for the current head: hold=false.
#             - `status=pending` for the current head, younger than
#               SECURITY_PASS_PENDING_STALE_HOURS: hold=true, nothing else.
#             - `status=findings` for the current head: hold=true until the
#               follow-up fixes change the head.
#             - otherwise, with fewer than MAX_SECURITY_PASS_CYCLES cycles
#               used: dispatch the security audit for the PR head branch with
#               `pr_number`, post a `status=pending` marker, hold=true.
#             - cycles used up: label the PR `ai:security-pass-failed` once,
#               hold=true and exhausted=true only if this head has completed
#               with findings. Otherwise retry a bounded number of audits of
#               this head, then hold without entering security-exhaustion mode.
#               The review-blocked judge (scripts/review_rb_judge.sh) can
#               decide to merge only an exhausted, audited head.
#             Cycles available = MAX_SECURITY_PASS_CYCLES plus one per
#             trusted extension marker: the judge posts one each time it
#             pushes a [judge-fix] after the pass ran out, so that fix is
#             audited too.
#           A dispatch that fails (for example a consumer wrapper without the
#           `pr_number` input) holds the merge for a later retry.
#   status  Runs the gate's checks with no label, comment, dispatch or
#           GITHUB_OUTPUT write. May fetch missing Git history to verify
#           extension ancestry; prints
#           SINGLE_ISSUE_SECURITY_PASS_STATE=<skip|unverifiable|clean|pending|
#           findings|exhausted|exhausted_unaudited|needs_audit>. An exhausted
#           head also emits SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD=<sha>.
#           For findings it emits the validated, trusted head-bound findings
#           JSON or SINGLE_ISSUE_SECURITY_PASS_FINDINGS_RECORD_STATUS.
#           review_rb_judge.sh uses it to enter security-exhaustion mode.
#   report  Run by security-audit.yml after an audit dispatched with
#           `pr_number`. Posts the `status=clean|findings|failed` marker for
#           the audited commit. On clean, failed or final-cycle findings it
#           re-dispatches review, so the next review merges or escalates.
#           Findings become follow-up issues that target
#           the PR branch; their merges push to the PR and start a new review.
#
# Marker (last non-empty line of a comment by the pipeline account; any other
# author is ignored):
#   <!-- ai:single-issue-security-pass:v1 status=<s> head=<40 hex> cycle=<n> -->
# Immediately before a findings marker, in the same trusted comment:
#   <!-- ai:single-issue-security-pass-findings:v1 head=<40 hex> cycle=<n> count=<n> record=<base64 JSON> -->
# Extension marker, same trust rule, posted by review_rb_judge.sh:
#   <!-- ai:single-issue-security-pass-extension:v1 head=<40 hex> -->
#
# API budget (CLAUDE.md §15). gate: none for an ineligible PR; for an eligible
# one, reuses the PR payload and comments the review job already fetched, plus
# at most 3 GETs for the skip check and one /user identity read (the gate
# job's /user result is not exported), then one dispatch and one comment (or
# one label write). report: one PR read to bind the audit inputs, one /user
# identity read, one paginated comments read, one comment and at most one dispatch.
# status reuses the gate's comments and identity reads; the record adds no API calls.
#
# Log: SINGLE_ISSUE_SECURITY_PASS mode= pr= head= outcome= reason= cycle=
set -uo pipefail

SECURITY_PASS_MARKER_RE='^<!-- ai:single-issue-security-pass:v1 status=(pending|clean|findings|failed) head=([0-9a-f]{40}) cycle=([1-9][0-9]*) -->$'
SECURITY_PASS_EXTENSION_MARKER_RE='^<!-- ai:single-issue-security-pass-extension:v1 head=([0-9a-f]{40}) -->$'
# `status` mode: evaluate the gate without side effects (see the header).
# Set only by the `status` subcommand, never from the environment: a gate
# run that skipped its hold write would let auto-merge through.
SINGLE_PASS_STATUS_ONLY="false"

single_pass_log()
{
	echo "SINGLE_ISSUE_SECURITY_PASS $*"
}

single_pass_output()
{
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
		return 0
	fi
	if [ -z "${GITHUB_OUTPUT:-}" ]; then
		echo "::error::GITHUB_OUTPUT is unset; cannot record the security-pass decision." >&2
		exit 1
	fi
	if ! printf 'hold=%s\n' "$1" >> "${GITHUB_OUTPUT}"; then
		echo "::error::Could not record the security-pass decision; failing closed." >&2
		exit 1
	fi
}

# Records exhausted=true for the review-blocked judge step. A failed write
# only logs: hold=true was already recorded, so the merge stays held.
single_pass_exhausted_output()
{
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ] || [ -z "${GITHUB_OUTPUT:-}" ]; then
		return 0
	fi
	printf 'exhausted=true\n' >> "${GITHUB_OUTPUT}" \
		|| echo "::warning::Could not record exhausted=true; the review-blocked judge will not run for this exhausted security pass." >&2
}

# In `status` mode, prints the gate's state for review_rb_judge.sh.
single_pass_state()
{
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
		echo "SINGLE_ISSUE_SECURITY_PASS_STATE=$1"
	fi
}

single_pass_marker()
{
	printf '<!-- ai:single-issue-security-pass:v1 status=%s head=%s cycle=%s -->' "$1" "$2" "$3"
}

# Validate the untrusted audit JSON for both publishing and reading. Emit
# canonical compact JSON so the comment is bounded and never contains text
# that can break out of its HTML marker.
single_pass_findings_record_validate()
{
	jq -sce --argjson expected "$1" '
		length == 1 and (.[0] |
		type == "array" and length == $expected and length <= 100 and
		all(.[]; type == "object" and
			(.finding_id | type == "string" and length > 0 and length <= 200 and (test("[\u0000-\u001f\u007f]") | not)) and
			(.severity == "critical" or .severity == "high" or .severity == "medium" or .severity == "low") and
			(.file | type == "string" and length > 0 and length <= 512 and (test("[\u0000-\u001f\u007f]") | not)) and
			(.line | type == "number" and . > 0 and . == floor)))
	' >/dev/null || return 1
}

single_pass_findings_record_line()
{
	local record_file="${SECURITY_PASS_FINDINGS_RECORD_FILE:-}" expected="$1" head="$2" cycle="$3" encoded
	[ -n "${record_file}" ] && [ -f "${record_file}" ] && [ -r "${record_file}" ] || return 1
	if ! single_pass_findings_record_validate "${expected}" < "${record_file}"; then
		return 1
	fi
	encoded="$(jq -sc '.[0]' "${record_file}" | tr -d '\n' | base64 -w 0)" || return 1
	printf '<!-- ai:single-issue-security-pass-findings:v1 head=%s cycle=%s count=%s record=%s -->' "${head}" "${cycle}" "${expected}" "${encoded}"
}

# Read only the comment identified by the authenticated findings marker.
# A malformed/duplicated record in that comment is invalid, not a reason to
# fall back to an older comment or to a ticket-only merge decision.
single_pass_findings_record_read()
{
	local comments_file="$1" marker_row="$2" marker_id marker_cycle marker_head marker_author body record_line encoded record_count decoded
	marker_id="$(printf '%s' "${marker_row}" | cut -f2)"
	marker_head="$(printf '%s' "${marker_row}" | cut -f4)"
	marker_cycle="$(printf '%s' "${marker_row}" | cut -f5)"
	marker_author="${SECURITY_PASS_AUTHOR_LOGIN:-}"
	[ -n "${marker_author}" ] || return 2
	if ! body="$(jq -er --arg id "${marker_id}" --arg author "${marker_author}" '
		[.[] | select((.id | tostring) == $id and .user.login == $author) | .body] | if length == 1 and (.[0] | type == "string") then .[0] else error("comment missing") end
	' "${comments_file}" 2>/dev/null)"; then
		return 2
	fi
	record_line="$(printf '%s\n' "${body}" | grep -E '^<!-- ai:single-issue-security-pass-findings:v1 ' || true)"
	[ -n "${record_line}" ] || return 1
	if [[ ! "${record_line}" =~ ^\<\!\-\-\ ai:single-issue-security-pass-findings:v1\ head=([0-9a-f]{40})\ cycle=([1-9][0-9]*)\ count=([0-9]+)\ record=([A-Za-z0-9+/=]+)\ \-\-\>$ ]]; then
		return 2
	fi
	[ "${BASH_REMATCH[1]}" = "${marker_head}" ] && [ "${BASH_REMATCH[2]}" = "${marker_cycle}" ] || return 2
	record_count="${BASH_REMATCH[3]}"
	encoded="${BASH_REMATCH[4]}"
	[ "${record_count}" -le 100 ] 2>/dev/null && [ "${#encoded}" -le 100000 ] || return 2
	decoded="$(printf '%s' "${encoded}" | base64 -d 2>/dev/null)" || return 2
	if ! single_pass_findings_record_validate "${record_count}" <<< "${decoded}"; then
		return 2
	fi
	jq -sc '.[0]' <<< "${decoded}" 2>/dev/null
}

# Prints the pipeline's markers as TSV: created_at, id, status, head, cycle.
single_pass_markers()
{
	local comments_file="$1"
	[ -n "${SECURITY_PASS_AUTHOR_LOGIN:-}" ] || return 1
	[ -s "${comments_file}" ] || return 0
	jq -r --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" '
		.[]?
		| select(type == "object")
		| select(.user.login == $author)
		| [(.created_at // ""), (.id // 0), ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | gsub("^\\s+|\\s+$"; ""))]
		| @tsv
	' "${comments_file}" 2>/dev/null | while IFS=$'\t' read -r created id line; do
		if [[ "${line}" =~ ${SECURITY_PASS_MARKER_RE} ]]; then
			printf '%s\t%s\t%s\t%s\t%s\n' "${created}" "${id}" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
		fi
	done | sort -t $'\t' -k1,1 -k2,2n
}

# Prints how many extra audit cycles the security-exhaustion judge granted:
# one per trusted extension whose fix commit is reachable from the audited
# branch head. A marker posted before a failed push must not extend the budget.
single_pass_extensions()
{
	local comments_file="$1" audited_head="$2" checkout="$3" branch_ref="$4" count=0 extension_line extension_sha extension_lines extension_ancestor_rc
	local -A single_pass_extension_seen=()
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN:-}" ] || [ ! -s "${comments_file}" ] \
		|| [ "$(git -C "${checkout}" rev-parse HEAD 2>/dev/null || true)" != "${audited_head}" ]; then
		return 1
	fi
	if ! extension_lines="$(jq -r --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" '
			.[]?
			| select(type == "object")
			| select(.user.login == $author)
			| ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | gsub("^\\s+|\\s+$"; ""))
		' "${comments_file}" 2>/dev/null)"; then
		return 1
	fi
	while IFS= read -r extension_line; do
		if [[ "${extension_line}" =~ ${SECURITY_PASS_EXTENSION_MARKER_RE} ]]; then
			extension_sha="${BASH_REMATCH[1]}"
			if [ -n "${single_pass_extension_seen[${extension_sha}]:-}" ]; then
				continue
			fi
			single_pass_extension_seen["${extension_sha}"]=1
			if ! git -C "${checkout}" cat-file -e "${extension_sha}^{commit}" 2>/dev/null; then
				if [ "$(git -C "${checkout}" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
					# An absent object in a shallow clone may be a real fix outside
					# the fetched history, not an orphan from a failed push.
					if ! git check-ref-format --branch "${branch_ref}" >/dev/null 2>&1 \
						|| ! git -C "${checkout}" fetch --no-tags --unshallow origin "+refs/heads/${branch_ref}:refs/remotes/origin/${branch_ref}" 2>/dev/null; then
						return 1
					fi
				fi
				if ! git -C "${checkout}" cat-file -e "${extension_sha}^{commit}" 2>/dev/null; then
					continue
				fi
			fi
			extension_ancestor_rc=0
			git -C "${checkout}" merge-base --is-ancestor "${extension_sha}" "${audited_head}" 2>/dev/null || extension_ancestor_rc=$?
			if [ "${extension_ancestor_rc}" -eq 1 ] && [ "$(git -C "${checkout}" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
				# An existing commit can still have an incomplete path to HEAD.
				if ! git check-ref-format --branch "${branch_ref}" >/dev/null 2>&1 \
					|| ! git -C "${checkout}" fetch --no-tags --unshallow origin "+refs/heads/${branch_ref}:refs/remotes/origin/${branch_ref}" 2>/dev/null; then
					return 1
				fi
				extension_ancestor_rc=0
				git -C "${checkout}" merge-base --is-ancestor "${extension_sha}" "${audited_head}" 2>/dev/null || extension_ancestor_rc=$?
			fi
			case "${extension_ancestor_rc}" in
				0) count=$((count + 1)) ;;
				1) ;;
				*) return 1 ;;
			esac
		fi
	done <<< "${extension_lines}"
	echo "${count}"
}

single_pass_audit_workflow()
{
	if [ "${REPOSITORY}" = "shubhodeep1/coding-workflows" ]; then
		echo "security-audit.yml"
	else
		echo "ai-security-audit.yml"
	fi
}

single_pass_review_workflow()
{
	if [ "${REPOSITORY}" = "shubhodeep1/coding-workflows" ]; then
		echo "internal-review.yml"
	else
		echo "ai-review.yml"
	fi
}

single_pass_gate()
{
	local pr_json="${PR_PAYLOAD_FILE:-}" comments="${PR_ISSUE_COMMENTS_FILE:-}" default_branch="${DEFAULT_BRANCH:-}"
	local max_cycles="${MAX_SECURITY_PASS_CYCLES:-5}" stale_hours="${SECURITY_PASS_PENDING_STALE_HOURS:-6}"
	local exhausted_head_limit="${SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS:-2}" exhausted_retry="false" head_attempts=0
	local pattern="${ORCH_INTEGRATION_BRANCH_PATTERN:-^orchestrator/project-}"
	local state base head_ref head_sha head_repo labels linked skip_json markers latest latest_status latest_head latest_created
	local cycles_used next_cycle age_hours workflow body extensions effective_max completed_findings record_value record_rc
	[[ "${max_cycles}" =~ ^[1-9][0-9]*$ ]] || max_cycles=5
	[[ "${stale_hours}" =~ ^[1-9][0-9]*$ ]] || stale_hours=6
	[[ "${exhausted_head_limit}" =~ ^[1-9][0-9]*$ ]] || exhausted_head_limit=2
	if [ "${SINGLE_ISSUE_SECURITY_PASS_ENABLED:-true}" = "false" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER:-} outcome=skip reason=disabled"
		single_pass_state skip
		single_pass_output false
		return 0
	fi
	if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || [ ! -s "${pr_json}" ] || [ -z "${default_branch}" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER:-} outcome=skip reason=missing_input"
		single_pass_state skip
		single_pass_output false
		return 0
	fi
	state="$(jq -r '.state // ""' "${pr_json}")"
	base="$(jq -r '.base.ref // ""' "${pr_json}")"
	head_ref="$(jq -r '.head.ref // ""' "${pr_json}")"
	head_sha="$(jq -r '.head.sha // ""' "${pr_json}")"
	head_repo="$(jq -r '.head.repo.full_name // ""' "${pr_json}")"
	labels="$(jq -r '[.labels[]?.name] | join(",")' "${pr_json}")"
	if [ "${state}" != "open" ] || [ "${base}" != "${default_branch}" ] || [ "${head_repo}" != "${REPOSITORY}" ] \
		|| ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || printf '%s\n' "${head_ref}" | grep -Eq -- "${pattern}" \
		|| printf ',%s,' "${labels}" | grep -q ',e2e-smoke-test,'; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=skip reason=not_standalone"
		single_pass_state skip
		single_pass_output false
		return 0
	fi
	# Skip only when the sole linked issue is verified; mixed-issue PRs
	# must not inherit one follow-up's exemption.
	linked="$(printf '%s' "${LINKED_ISSUES_JSON:-[]}" | jq -r 'if type == "array" and length == 1 then .[0].number // empty else empty end' 2>/dev/null || true)"
	if [[ "${linked}" =~ ^[0-9]+$ ]] && [ -f "${SUPPORT_SCRIPTS_DIR:-scripts}/security_pass_skip.py" ]; then
		skip_json="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR:-scripts}/security_pass_skip.py" --repo "${REPOSITORY}" --issue "${linked}" 2>/dev/null || true)"
		if [ "$(printf '%s' "${skip_json}" | jq -r '.skip // false' 2>/dev/null)" = "true" ]; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=skip reason=verified_followup issue=${linked}"
			single_pass_state skip
			single_pass_output false
			return 0
		fi
	fi
	# The gate job also probes /user, but its identity is not exported to this job.
	SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		SECURITY_PASS_AUTHOR_LOGIN="${SECURITY_PASS_AUTHOR_LOGIN_FALLBACK:-}"
	fi
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ] || [ ! -s "${comments}" ] \
		|| ! jq -e 'type == "array"' "${comments}" >/dev/null 2>&1; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=markers_unverifiable"
		single_pass_state unverifiable
		single_pass_output true
		return 0
	fi
	if ! markers="$(single_pass_markers "${comments}")"; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=markers_unverifiable"
		single_pass_state unverifiable
		single_pass_output true
		return 0
	fi
	cycles_used="$(printf '%s\n' "${markers}" | awk -F'\t' 'NF >= 5 && $5 + 0 > m { m = $5 + 0 } END { print m + 0 }')"
	if ! extensions="$(single_pass_extensions "${comments}" "${head_sha}" . "${head_ref}")"; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=extensions_unverifiable"
		single_pass_state unverifiable
		single_pass_output true
		return 0
	fi
	effective_max=$((max_cycles + extensions))
	latest="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" '$4 == h' | tail -n 1)"
	latest_created="$(printf '%s' "${latest}" | cut -f1)"
	latest_status="$(printf '%s' "${latest}" | cut -f3)"
	latest_head="$(printf '%s' "${latest}" | cut -f4)"
	completed_findings="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" '$3 == "findings" && $4 == h' | tail -n 1)"
	if [ "${latest_status}" = "clean" ] && [ "${latest_head}" = "${head_sha}" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=clean"
		single_pass_state clean
		single_pass_output false
		return 0
	fi
	if [ "${latest_status}" = "pending" ] && [ "${latest_head}" = "${head_sha}" ]; then
		age_hours=0
		if [ -n "${latest_created}" ]; then
			age_hours="$(python3 -c 'import datetime,sys; t=datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")); print(int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds()//3600))' "${latest_created}" 2>/dev/null || echo 0)"
		fi
		if [ "${age_hours}" -lt "${stale_hours}" ]; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=audit_pending cycle=${cycles_used}"
			single_pass_state pending
			single_pass_output true
			return 0
		fi
	fi
	if [ "${latest_status}" = "findings" ] && [ "${latest_head}" = "${head_sha}" ] && [ "${cycles_used}" -lt "${effective_max}" ]; then
		# The follow-up fixes merge into this branch and change the head; the
		# next cycle audits that head.
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=awaiting_followups cycle=${cycles_used}"
		single_pass_state findings
		single_pass_output true
		return 0
	fi
	if [ "${cycles_used}" -ge "${effective_max}" ]; then
		if [ -z "${completed_findings}" ]; then
			head_attempts="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" -v cap="${effective_max}" '$3 == "pending" && $4 == h && $5 + 0 > cap { n++ } END { print n + 0 }')"
			if [ "${head_attempts}" -lt "${exhausted_head_limit}" ]; then
				exhausted_retry="true"
			fi
		fi
		if [ "${exhausted_retry}" != "true" ]; then
			if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
				if [ -n "${completed_findings}" ]; then
					single_pass_log "mode=status pr=${PR_NUMBER} head=${head_sha} outcome=exhausted cycle=${cycles_used} max=${effective_max}"
					single_pass_state exhausted
					echo "SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD=${head_sha}"
					record_rc=0
					record_value="$(single_pass_findings_record_read "${comments}" "${completed_findings}")" || record_rc=$?
					if [ "${record_rc}" -eq 0 ]; then
						echo "SINGLE_ISSUE_SECURITY_PASS_FINDINGS_RECORD=${record_value}"
					else
						if [ "${record_rc}" -eq 1 ]; then
							echo "SINGLE_ISSUE_SECURITY_PASS_FINDINGS_RECORD_STATUS=missing"
						else
							echo "SINGLE_ISSUE_SECURITY_PASS_FINDINGS_RECORD_STATUS=invalid"
						fi
					fi
				else
					single_pass_state exhausted_unaudited
				fi
				return 0
			fi
			if ! printf ',%s,' "${labels}" | grep -q ',ai:security-pass-failed,'; then
				if ! gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/labels" -f 'labels[]=ai:security-pass-failed' >/dev/null 2>&1; then
					echo "::error::Could not label PR #${PR_NUMBER} ai:security-pass-failed; failing closed so workflow recovery can retry."
					single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=failed reason=label_write_failed cycle=${cycles_used}"
					single_pass_output true
					return 1
				fi
				if [ -n "${completed_findings}" ]; then
					body="## Single-issue security pass exhausted

The security audit of this PR has used ${cycles_used} of ${effective_max} cycles, so auto-merge stays off. The PR is labelled \`ai:security-pass-failed\`. The review-blocked judge may merge with medium/low findings open, fix blocking high/critical/unrated findings while retries remain, or hold the PR for a clean audit or human decision."
				else
					body="## Single-issue security pass: unaudited head

No completed audit exists for \`${head_sha}\`. Auto-merge stays off. A new push starts a fresh per-head audit budget; /re-run rechecks this head."
				fi
				gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${body}" >/dev/null 2>&1 \
					|| echo "::warning::Could not post the security-pass exhaustion comment on PR #${PR_NUMBER}."
			fi
			if [ -n "${completed_findings}" ]; then
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=cycles_exhausted cycle=${cycles_used}"
				single_pass_output true
				single_pass_exhausted_output
			else
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=exhausted_without_completed_audit cycle=${cycles_used} head_attempts=${head_attempts}"
				single_pass_output true
			fi
			return 0
		fi
	fi
	next_cycle=$((cycles_used + 1))
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
		single_pass_state needs_audit
		return 0
	fi
	workflow="$(single_pass_audit_workflow)"
	if ! gh workflow run "${workflow}" -R "${REPOSITORY}" --ref "${default_branch}" -f ref="${head_ref}" -f pr_number="${PR_NUMBER}" >/dev/null 2>&1; then
		if [ "${exhausted_retry}" = "true" ]; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=dispatch_failed_exhausted cycle=${next_cycle}"
		else
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=dispatch_failed workflow=${workflow} cycle=${next_cycle}"
		fi
		echo "::warning::Could not dispatch ${workflow} for PR #${PR_NUMBER}; holding the merge until an audit can run."
		single_pass_output true
		return 0
	fi
	if [ "${exhausted_retry}" = "true" ]; then
		body="## Single-issue security pass: retry audit

Auto-merge waits for a retry audit of the exhausted pass on \`${head_sha}\` (head attempt $((head_attempts + 1)) of ${exhausted_head_limit}).

$(single_pass_marker pending "${head_sha}" "${next_cycle}")"
	else
		body="## Single-issue security pass

Auto-merge waits for the security audit of \`${head_sha}\` (cycle ${next_cycle} of ${effective_max}). Findings become follow-up issues against this branch; a clean audit re-runs the review, which then merges.

$(single_pass_marker pending "${head_sha}" "${next_cycle}")"
	fi
	gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${body}" >/dev/null 2>&1 \
		|| echo "::warning::Could not post the pending security-pass marker on PR #${PR_NUMBER}."
	single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=dispatched cycle=${next_cycle} workflow=${workflow}$([ "${exhausted_retry}" = "true" ] && printf ' reason=exhausted_retry')"
	single_pass_output true
}

single_pass_report()
{
	local pr_number="${SECURITY_PASS_PR_NUMBER:-}" head_sha="${SECURITY_PASS_HEAD_SHA:-}" outcome="${SECURITY_PASS_AUDIT_OUTCOME:-}"
	local findings="${SECURITY_PASS_FINDINGS:-}" default_branch="${DEFAULT_BRANCH:-}" comments_file status cycle body review_workflow pr_head
	local extensions=0 record_line="" record_status="valid"
	local max_cycles="${MAX_SECURITY_PASS_CYCLES:-5}"
	[[ "${max_cycles}" =~ ^[1-9][0-9]*$ ]] || max_cycles=5
	if ! [[ "${pr_number}" =~ ^[0-9]+$ ]] || ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		single_pass_log "mode=report pr=${pr_number} outcome=skip reason=missing_input"
		return 0
	fi
	# Audit data and PR number are independent dispatch inputs. Bind the result
	# to the live same-repository PR before trusting it as a merge authorization.
	if [ -z "${SECURITY_PASS_AUDIT_BRANCH:-}" ]; then
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pr_head_mismatch"
		return 0
	fi
	if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -f "${GITHUB_WORKSPACE}/scripts/gh_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${GITHUB_WORKSPACE}/scripts/gh_helpers.sh"
	fi
	if type gh_retry >/dev/null 2>&1; then
		pr_head="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${pr_number}" 2>/dev/null)" || pr_head=""
	else
		pr_head="$(gh api "repos/${REPOSITORY}/pulls/${pr_number}" 2>/dev/null)" || pr_head=""
	fi
	if [ -z "${pr_head}" ]; then
		echo "::warning::Could not verify PR #${pr_number} for the security-pass report; leaving its audit result unpublished."
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pr_lookup_failed"
		return 0
	fi
	if ! jq -e --arg sha "${head_sha}" --arg branch "${SECURITY_PASS_AUDIT_BRANCH}" --arg repo "${REPOSITORY}" --arg base "${default_branch}" \
			'.state == "open" and .head.sha == $sha and .head.ref == $branch and .head.repo.full_name == $repo and .base.ref == $base' <<< "${pr_head}" >/dev/null 2>&1; then
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pr_head_mismatch"
		return 0
	fi
	# No other report-side call provides the identity of the comment author.
	SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		SECURITY_PASS_AUTHOR_LOGIN="${SECURITY_PASS_AUTHOR_LOGIN_FALLBACK:-}"
	fi
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=author_unverifiable"
		return 0
	fi
	if [ "${outcome}" = "success" ] && [[ "${findings}" =~ ^[0-9]+$ ]]; then
		if [ "${findings}" -eq 0 ]; then
			status="clean"
		else
			status="findings"
		fi
	else
		status="failed"
	fi
	comments_file="$(mktemp)"
	if { if type gh_retry >/dev/null 2>&1; then
		gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100"
	else
		gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100"
	fi; } 2>/dev/null | jq -s 'add // []' > "${comments_file}" 2>/dev/null \
		&& jq -e 'type == "array"' "${comments_file}" >/dev/null 2>&1; then
		cycle="$(single_pass_markers "${comments_file}" | awk -F'\t' -v h="${head_sha}" '$3 == "pending" && $4 == h { c = $5 } END { print c + 0 }')"
		if ! extensions="$(single_pass_extensions "${comments_file}" "${head_sha}" "${GITHUB_WORKSPACE:-.}/audit-data" "${SECURITY_PASS_AUDIT_BRANCH}")"; then
			rm -f "${comments_file}"
			single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=extensions_unverifiable"
			return 0
		fi
	else
		cycle=0
	fi
	rm -f "${comments_file}"
	if [ "${cycle}" -lt 1 ]; then
		echo "::warning::No verified pending marker for PR #${pr_number} at ${head_sha}; refusing to publish an audit result."
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pending_unverifiable"
		return 0
	fi
	case "${status}" in
		clean) body="## Single-issue security pass: clean

The audit of \`${head_sha}\` found nothing to fix. The review re-runs and merges this head." ;;
		findings) body="## Single-issue security pass: ${findings} finding(s)

The audit of \`${head_sha}\` filed follow-up issues against this branch. Their merges start a new review, and the next cycle audits the new head." ;;
		*) body="## Single-issue security pass: audit failed

The audit of \`${head_sha}\` did not finish. The review re-runs and starts the next cycle." ;;
	esac
	if [ "${status}" = "findings" ]; then
		record_line="$(single_pass_findings_record_line "${findings}" "${head_sha}" "${cycle}")" || record_status="missing"
		if [ -n "${record_line}" ]; then
			body+=$'\n\n'"${record_line}"
		fi
	fi
	body+=$'\n\n'"$(single_pass_marker "${status}" "${head_sha}" "${cycle}")"
	if ! gh api "repos/${REPOSITORY}/issues/${pr_number}/comments" -f body="${body}" >/dev/null 2>&1; then
		echo "::warning::Could not post the security-pass result on PR #${pr_number}."
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=comment_write_failed cycle=${cycle}"
		return 0
	fi
	if { [ "${status}" != "findings" ] || [ "${cycle}" -ge "$((max_cycles + extensions))" ]; } && [ -n "${default_branch}" ]; then
		review_workflow="$(single_pass_review_workflow)"
		gh workflow run "${review_workflow}" -R "${REPOSITORY}" --ref "${default_branch}" -f pr_number="${pr_number}" >/dev/null 2>&1 \
			|| echo "::warning::Could not re-dispatch ${review_workflow} for PR #${pr_number}; the next review event picks the result up."
	fi
	single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=${status} cycle=${cycle} findings=${findings:-unknown} record=${record_status}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	case "${1:-}" in
		gate) single_pass_gate ;;
		status)
			SINGLE_PASS_STATUS_ONLY="true"
			single_pass_gate
			;;
		report) single_pass_report ;;
		*)
			echo "usage: $0 gate|status|report" >&2
			exit 2
			;;
	esac
fi
